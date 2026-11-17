"""Protein and gene name search against UniProtKB."""

import logging
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from beacons_mcp import taxonomy
from beacons_mcp.clients import UNIPROT_SEARCH_URL, get
from beacons_mcp.models import ProteinCandidate, ProteinSearchResult

logger = logging.getLogger("beacons_mcp")

# "lineage" returns the taxon IDs of the entry's ancestors, which the descendant guard uses.
FIELDS = "accession,id,protein_name,gene_names,organism_name,reviewed,lineage"

# UniProt ranks by relevance; ask for more than we show so that re-ordering
# reviewed entries to the front can pull in ones relevance ranked lower.
FETCH_FACTOR = 3
MIN_FETCH = 10
MAX_FETCH = 50
MAX_LISTED_ACCESSIONS = 2


def _clean(text: str) -> str:
    """Make ``text`` safe to put inside a quoted UniProt query phrase."""
    return " ".join(text.replace('"', " ").replace("\\", " ").split())


def build_query(query: str, taxon_id: int, reviewed: bool | None = None) -> str:
    """Build the UniProt query: gene or protein names, in a taxon and everything below it.

    ``taxonomy_id`` includes descendants (strains, subspecies); ``organism_id`` is exact.
    """
    name = _clean(query)
    if not name:
        raise ToolError(
            "query is empty. Provide a protein or gene name such as 'hemoglobin alpha' or 'BRCA1'."
        )
    text = f'(gene:"{name}" OR protein_name:"{name}") AND taxonomy_id:{taxon_id}'
    if reviewed is not None:
        text += f" AND reviewed:{str(reviewed).lower()}"
    return text


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
        taxon_id=item.get("organism", {}).get("taxonId"),
    )


def _in_taxon(item: dict[str, Any], wanted: int) -> bool:
    """True for an entry of ``wanted`` itself or of any descendant of it.

    The ancestors come from UniProt's own ``lineages`` (taxon IDs) in the entry; they
    exclude the entry's own taxon, which is checked separately. No taxon, no match.
    """
    if item.get("organism", {}).get("taxonId") == wanted:
        return True
    return any(t.get("taxonId") == wanted for t in item.get("lineages", []))


def _filed_under(candidates: list[ProteinCandidate], wanted: int) -> str:
    """Receipt text for hits filed under a descendant taxon of the one searched.

    Grouped by taxon: "P60010 filed under strain 559292", or with several hits in one
    strain "Q5ALV2, Q5A961 and 3 more filed under strain 237561".
    """
    groups: dict[int, list[ProteinCandidate]] = {}
    for c in candidates:
        if c.taxon_id is not None and c.taxon_id != wanted:
            groups.setdefault(c.taxon_id, []).append(c)
    parts = []
    for taxon_id, hits in groups.items():
        kind = "strain" if "(strain" in hits[0].organism else "taxon"
        names = [h.accession for h in hits[:MAX_LISTED_ACCESSIONS]]
        more = len(hits) - len(names)
        who = ", ".join(names) + (f" and {more} more" if more else "")
        parts.append(f"{who} filed under {kind} {taxon_id}")
    return "; ".join(parts)


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


async def _fetch(
    client: httpx.AsyncClient, query: str, wanted: int, reviewed: bool, size: int
) -> tuple[list[dict[str, Any]], int | None]:
    response = await get(
        client,
        UNIPROT_SEARCH_URL,
        service="UniProt",
        params={
            "query": build_query(query, wanted, reviewed),
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
    # taxonomy_id already restricts to the taxon and its descendants; this guards against
    # a surprise from UniProt: an entry outside that taxon is never offered.
    results = [i for i in response.json().get("results", []) if _in_taxon(i, wanted)]
    total = response.headers.get("x-total-results")
    return results, int(total) if total and total.isdigit() else None


async def search_proteins(
    client: httpx.AsyncClient, query: str, organism: str, max_results: int
) -> ProteinSearchResult:
    wanted = await taxonomy.resolve(client, organism)
    build_query(query, wanted.taxon_id)  # reject an empty query before any request
    size = min(max(max_results * FETCH_FACTOR, MIN_FETCH), MAX_FETCH)

    # A search below a species (E. coli: thousands of strain entries) is dominated by
    # unreviewed entries, so ask for the reviewed ones first and top up from the rest.
    reviewed, reviewed_total = await _fetch(client, query, wanted.taxon_id, True, size)
    reviewed = reviewed[:max_results]
    room = max_results - len(reviewed)
    unreviewed, unreviewed_total = await _fetch(
        client, query, wanted.taxon_id, False, max(room, 1)
    )
    results = reviewed + unreviewed[:room]
    if not results:
        raise ToolError(
            f"No UniProt entry matches '{query.strip()}' in '{organism.strip()}'. "
            "Check the spelling, try the full protein name (or the official gene symbol), "
            "and check that the organism is right."
        )

    candidates = [parse_candidate(item) for item in results]
    totals = [t for t in (reviewed_total, unreviewed_total) if t is not None]
    total = sum(totals) if len(totals) == 2 else None
    logger.info("search_proteins matched %s entries", total or len(results))
    receipt = wanted.label
    if filed := _filed_under(candidates, wanted.taxon_id):
        receipt = f"{receipt}; {filed}"
    return ProteinSearchResult(
        query=query.strip(),
        organism=receipt,
        candidates=candidates,
        total_matches=total,
        note=_next_step_note(candidates),
    )
