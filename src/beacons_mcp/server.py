"""MCP server for the 3D-Beacons protein structure API.

Importing this module has no side effects beyond constructing the server object:
nothing is started, no connection is made and logging is not configured until
``main()`` is called.
"""

import inspect
import logging
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

import httpx
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from beacons_mcp import __version__, hub, uniprot
from beacons_mcp.clients import make_client
from beacons_mcp.models import ProteinSearchResult, StructureResult

logger = logging.getLogger("beacons_mcp")


@dataclass
class AppState:
    """What the tools share for the life of the server."""

    client: httpx.AsyncClient


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[AppState]:
    # One HTTP client for the whole server run, closed when the server stops.
    async with make_client() as client:
        yield AppState(client=client)


mcp = MCPServer(
    "beacons-mcp",
    version=__version__,
    lifespan=lifespan,
    instructions=(
        "Finds 3D structures of proteins through the 3D-Beacons network. "
        "Typical flow: search_proteins to turn a protein or gene name plus an organism "
        "into a UniProt accession (confirm the choice with the user if it is ambiguous), "
        "then find_structures to list structure models for that accession."
    ),
)

_READ_ONLY = ToolAnnotations(
    read_only_hint=True, idempotent_hint=True, open_world_hint=True
)


def _tool(func: Callable[..., Any]) -> Callable[..., Any]:
    """Register ``func`` as a read-only tool, with its docstring as a clean description.

    The SDK would send the docstring verbatim, hard line wraps and indentation
    included. Re-flow each paragraph onto one line so the model sees plain text.
    """
    paragraphs = inspect.cleandoc(func.__doc__ or "").split("\n\n")
    func.__doc__ = "\n\n".join(" ".join(p.split()) for p in paragraphs)
    return mcp.tool(annotations=_READ_ONLY)(func)


def _client(ctx: Context) -> httpx.AsyncClient:
    state: AppState = ctx.request_context.lifespan_context
    return state.client


@_tool
async def search_proteins(
    query: Annotated[
        str,
        Field(
            description=(
                "A protein name or gene symbol, e.g. 'hemoglobin alpha' or 'BRCA1'. "
                "Gene synonyms also match."
            ),
            examples=["hemoglobin alpha", "BRCA1"],
        ),
    ],
    organism: Annotated[
        str,
        Field(
            description=(
                "The organism the protein is from: a common name, a scientific name or a "
                "numeric taxon ID, e.g. 'human', 'Homo sapiens' or '9606'. Required: ask the "
                "user if they have not said."
            ),
            examples=["human", "Homo sapiens", "9606"],
        ),
    ],
    max_results: Annotated[
        int,
        Field(
            ge=1,
            le=25,
            description="How many candidates to return (1-25). Example: 5.",
            examples=[5],
        ),
    ] = 5,
    *,
    ctx: Context,
) -> ProteinSearchResult:
    """Find the UniProt accession for a protein or gene name in a given organism.

    Use this first whenever the user names a protein or gene and you do not already
    have its UniProt accession. The organism is required. If the user has not named an
    organism, ask them. Do not assume human.

    Searches gene names (including synonyms) and protein names in UniProtKB and returns
    ranked candidates, reviewed (Swiss-Prot, curated) entries first. If more than one
    candidate is plausible, say so and confirm with the user which one they mean before
    calling find_structures. Unreviewed entries are less reliable and are often fragments
    or duplicates of a reviewed one. If nothing matches, the call fails with advice on
    what to try next.
    """
    return await uniprot.search_proteins(_client(ctx), query, organism, max_results)


@_tool
async def find_structures(
    uniprot_accession: Annotated[
        str,
        Field(
            description=(
                "A UniProt accession, e.g. 'P69905'. Not a protein name: use "
                "search_proteins to get the accession from a name."
            ),
            examples=["P69905", "Q92793"],
        ),
    ],
    max_results: Annotated[
        int,
        Field(
            ge=1,
            le=50,
            description="How many models to list in detail (1-50). Example: 10.",
            examples=[10],
        ),
    ] = 10,
    *,
    ctx: Context,
) -> StructureResult:
    """List the 3D structure models of a protein, from every provider in 3D-Beacons.

    Takes a UniProt accession. If you only have a name, call search_proteins first.
    The 3D-Beacons Hub queries all its structure providers, so this can take a few
    seconds, and popular proteins have hundreds of models.

    Returns the protein, totals of models by provider and by category, and up to
    max_results models showing the best of each provider first (so the list is a
    selection, not everything; the number left out is given). Each model has its
    provider, model ID, category, residue range and the percent of the protein it covers,
    its resolution (experimental models) or confidence score (predicted models), and the
    URL and format of its file.

    Categories: 'experimentally determined' models are measured structures (lower
    resolution values are better); 'template-based' and 'ab-initio' models are
    predictions (check the confidence score); a 'conformational ensemble' is a set of
    conformers, and its file URL serves only a sample of it. Models often cover just part
    of a protein, so check the coverage before presenting one as the whole protein.
    This tool returns links; it does not download or analyse structure files.
    """
    return await hub.find_structures(_client(ctx), uniprot_accession, max_results)


def configure_logging() -> None:
    """Send all logging to stderr.

    stdout carries the MCP protocol on the stdio transport, so a stray write there
    would corrupt it.
    """
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        # MCPServer() already installed a root handler at construction time;
        # without force=True this call would be silently ignored.
        force=True,
    )


def main() -> None:
    """Configure logging to stderr and run the server over stdio."""
    configure_logging()
    logger.info("starting beacons-mcp %s on stdio", __version__)
    mcp.run(transport="stdio")
