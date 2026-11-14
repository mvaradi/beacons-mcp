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

from beacons_mcp import __version__, confirm, hub, uniprot
from beacons_mcp.clients import make_client
from beacons_mcp.models import ProteinSearchResult, StructureResult
from beacons_mcp.registry import Registry

logger = logging.getLogger("beacons_mcp")


@dataclass
class AppState:
    """What the tools share for the life of the server."""

    client: httpx.AsyncClient
    registry: Registry


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[AppState]:
    # One HTTP client for the whole server run, closed when the server stops.
    async with make_client() as client:
        yield AppState(client=client, registry=Registry(client))


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


def _state(ctx: Context) -> AppState:
    return ctx.request_context.lifespan_context


@_tool
async def search_proteins(
    query: Annotated[
        str,
        Field(
            description=(
                "Protein name or gene symbol, e.g. 'hemoglobin alpha' or 'BRCA1'. "
                "Gene synonyms match."
            ),
            examples=["hemoglobin alpha", "BRCA1"],
        ),
    ],
    organism: Annotated[
        str,
        Field(
            description=(
                "Common name, scientific name or taxon ID, e.g. 'human', 'Homo sapiens' "
                "or '9606'. Required: ask the user if they have not said."
            ),
            examples=["human", "Homo sapiens", "9606"],
        ),
    ],
    max_results: Annotated[
        int,
        Field(
            ge=1,
            le=25,
            description="Candidates to return (1-25).",
            examples=[5],
        ),
    ] = 5,
    *,
    ctx: Context,
) -> ProteinSearchResult:
    """Find the UniProt accession for a protein or gene name in an organism.

    Use first when the user names a protein or gene and you lack its accession. The
    organism is required. If the user has not named an organism, ask them. Do not
    assume human.

    Matches gene names (with synonyms) and protein names; reviewed (Swiss-Prot)
    entries come first. When several are plausible the user may be shown a picker; if
    the result has confirmed_by_user, use that protein. Otherwise say so and confirm
    with the user before calling find_structures. Fails with next steps if nothing
    matches.
    """
    result = await uniprot.search_proteins(
        _state(ctx).client, query, organism, max_results
    )
    return await confirm.confirm_candidate(ctx, result)


@_tool
async def find_structures(
    uniprot_accession: Annotated[
        str,
        Field(
            description=(
                "UniProt accession, e.g. 'P69905'. Not a name: use search_proteins for that."
            ),
            examples=["P69905", "Q92793"],
        ),
    ],
    max_results: Annotated[
        int,
        Field(
            ge=1,
            le=50,
            description="Models to list (1-50).",
            examples=[10],
        ),
    ] = 10,
    *,
    ctx: Context,
) -> StructureResult:
    """List 3D structure models (experimental and predicted) for a UniProt accession.

    Covers all 3D-Beacons providers. If you only have a name, call search_proteins
    first. Can take a few seconds.

    Returns totals by provider and category, plus up to max_results models: the
    experimental model spanning most of the protein first, then others by span,
    spread across providers. Each gives its residue range, span_percent, resolution
    (experimental) or confidence (predicted), and file URL and format. Many models
    cover only part of the protein, so check span_percent before presenting one as
    the whole protein. Returns links only; nothing is downloaded.
    """
    return await hub.find_structures(
        _state(ctx).client, _state(ctx).registry, uniprot_accession, max_results
    )


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
