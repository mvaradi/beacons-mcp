"""Protein and gene name search against UniProtKB."""

import logging
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from beacons_mcp.clients import UNIPROT_SEARCH_URL, get
from beacons_mcp.models import ProteinCandidate, ProteinSearchResult

logger = logging.getLogger("beacons_mcp")

FIELDS = "accession,id,protein_name,gene_names,organism_name,reviewed"

# UniProt ranks by relevance; ask for more than we show so that re-ordering
# reviewed entries to the front can pull in ones relevance ranked lower.
FETCH_FACTOR = 3
MIN_FETCH = 10
MAX_FETCH = 50


def _clean(text: str) -> str:
    """Make ``text`` safe to put inside a quoted UniProt query phrase."""
    return " ".join(text.replace('"', " ").replace("\\", " ").split())


def build_query(query: str, organism: str) -> str:
    """Build the UniProt query: gene names (with synonyms) or protein names, in an organism."""
    name = _clean(query)
    if not name:
        raise ToolError(
            "query is empty. Provide a protein or gene name such as 'hemoglobin alpha' or 'BRCA1'."
        )
    organism = organism.strip()
    if not organism:
        raise ToolError(
            "organism is required. Ask the user which organism they mean "
            "(for example 'human', 'mouse' or a taxon ID like 9606); do not assume human."
        )
    if organism.isdigit():
        organism_clause = f"organism_id:{organism}"
    else:
        organism_clause = f'organism_name:"{_clean(organism)}"'
    return f'(gene:"{name}" OR protein_name:"{name}") AND {organism_clause}'


def _protein_name(item: dict[str, Any]) -> str | None:
    description = item.get("proteinDescription", {})
    name = description.get("recommendedName")
    if not name:
        submitted = description.get("submissionNames") or []
        name = submitted[0] if submitted else {}
    return name.get("fullName", {}).get("value")


def _gene_names(item: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for gene in item.get("genes", []):
        primary = gene.get("geneName", {}).get("value")
        synonyms = [s["value"] for s in gene.get("synonyms", [])]
        for name in [primary, *synonyms]:
            if name and name not in names:
                names.append(name)
    return names


def _organism(item: dict[str, Any]) -> str:
    organism = item.get("organism", {})
    scientific = organism.get("scientificName", "unknown organism")
    common = organism.get("commonName")
    return f"{scientific} ({common})" if common else scientific


def parse_candidate(item: dict[str, Any]) -> ProteinCandidate:
    return ProteinCandidate(
        accession=item["primaryAccession"],
        entry_name=item["uniProtkbId"],
        protein_name=_protein_name(item),
        gene_names=_gene_names(item),
        organism=_organism(item),
        reviewed=item.get("entryType", "").startswith("UniProtKB reviewed"),
    )


# What to do when it is unclear which protein the user means. The model decides how to
# ask, so say what is best: its interactive question tool (a clickable list) if it has
# one, plain chat otherwise.
ASK_WHICH_ONE = (
    "Ask the user which one they mean before calling find_structures: use your "
    "interactive question tool if you have one (one option per candidate, showing its "
    "accession, protein name and gene), otherwise ask in chat."
)


def _next_step_note(candidates: list[ProteinCandidate]) -> str:
    reviewed = [c for c in candidates if c.reviewed]
    if len(reviewed) == 1:
        best = f"{reviewed[0].accession} ({reviewed[0].entry_name})"
        if len(candidates) == 1:
            return f"One entry matches: {best}."
        return (
            f"One reviewed entry matches: {best}. Use it unless the user meant "
            "something else; the unreviewed entries are usually fragments or duplicates of it."
        )
    if reviewed:
        return (
            f"{len(reviewed)} reviewed entries match, so more than one protein is "
            f"plausible. {ASK_WHICH_ONE}"
        )
    return (
        "Only unreviewed (TrEMBL) entries match, which are less reliable. "
        f"{ASK_WHICH_ONE}"
    )


async def search_proteins(
    client: httpx.AsyncClient, query: str, organism: str, max_results: int
) -> ProteinSearchResult:
    uniprot_query = build_query(query, organism)
    size = min(max(max_results * FETCH_FACTOR, MIN_FETCH), MAX_FETCH)
    response = await get(
        client,
        UNIPROT_SEARCH_URL,
        service="UniProt",
        params={
            "query": uniprot_query,
            "format": "json",
            "fields": FIELDS,
            "size": size,
        },
    )
    if response.status_code != 200:
        logger.warning("UniProt search returned HTTP %d", response.status_code)
        raise ToolError(
            "UniProt could not run that search. Try a simpler name without special characters."
        )

    results = response.json().get("results", [])
    if not results:
        raise ToolError(
            f"No UniProt entry matches '{query.strip()}' in '{organism.strip()}'. "
            "Check the spelling, try the full protein name (or the official gene symbol), "
            "and check that the organism is right."
        )

    candidates = [parse_candidate(item) for item in results]
    candidates.sort(
        key=lambda c: not c.reviewed
    )  # stable: reviewed first, UniProt order kept
    candidates = candidates[:max_results]

    total = response.headers.get("x-total-results")
    logger.info("search_proteins matched %s entries", total or len(results))
    return ProteinSearchResult(
        query=query.strip(),
        organism=organism.strip(),
        candidates=candidates,
        total_matches=int(total) if total and total.isdigit() else None,
        note=_next_step_note(candidates),
    )
