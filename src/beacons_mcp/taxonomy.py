"""Resolve an organism the user names to an NCBI taxonomy ID.

The UniProt search is filtered by taxonomy ID, never by a substring of the organism's
name: "mouse" is part of "Mouse-ear cress" (Arabidopsis), so a name match returns
plants. The one exception is an explicit "any organism" (see ``ANY_WORDS``): then no
taxon filter is used and only reviewed entries are searched, and the result says so.
Names are resolved in this order: a taxon ID as given; the model organisms in
``data/organisms.json`` (common and Latin names); then UniProt's taxonomy search, taking
only an exact scientific- or common-name match at species rank or below. Zero or several
plausible matches are an error that says what to do: a guess could search the wrong
organism.
"""

import json
import logging
import re
from dataclasses import dataclass
from functools import cache
from importlib import resources

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from beacons_mcp.clients import UNIPROT_TAXONOMY_URL, get

logger = logging.getLogger("beacons_mcp")

LOOKUP_URL = "https://www.uniprot.org/taxonomy"


# Words that mean the user does not care which organism: search all of them (reviewed
# entries only). Empty is not one of them: an omitted organism is still asked about.
ANY_WORDS = {
    "any", "all", "anything", "every", "everything", "whatever",
    "all organisms", "any organism", "all species", "any species", "every species",
    "dont care", "do not care", "doesnt matter", "does not matter", "no preference",
    "dont mind", "do not mind", "i dont care",
}
ALL_ORGANISMS_LABEL = "all organisms, reviewed only"
# Ranks a user can mean by an organism name. A genus or family would match no entry.
RANKS = {"species", "subspecies", "strain", "varietas"}
LOOKUP_SIZE = 25
MAX_LISTED = 5


@dataclass(frozen=True)
class Organism:
    taxon_id: int | None  # None: all organisms
    name: str
    given: str = ""

    @property
    def label(self) -> str:
        """What was searched, for the result: 'mouse → Mus musculus (taxon 10090)'."""
        if self.taxon_id is None:
            return ALL_ORGANISMS_LABEL
        base = f"{self.name} (taxon {self.taxon_id})"
        given = self.given.strip()
        if not given or given.isdigit() or _key(given) == _key(self.name):
            return base
        return f"{given} → {base}"


def _key(text: str) -> str:
    text = re.sub(r"\([^)]*\)", " ", text).lower()
    text = re.sub(r"['’`.!?]", "", text)  # "don't" == "dont", "S. pombe" == "S pombe"
    return " ".join(text.split())


@cache
def _table() -> tuple[dict[str, int], dict[int, str], dict[str, list[int]]]:
    raw = json.loads(
        (resources.files("beacons_mcp") / "data" / "organisms.json").read_text(
            encoding="utf-8"
        )
    )
    by_name: dict[str, int] = {}
    scientific: dict[int, str] = {}
    for entry in raw["organisms"]:
        taxon = entry["taxon_id"]
        scientific[taxon] = entry["scientific_name"]
        for name in [entry["scientific_name"], *entry["names"]]:
            by_name[_key(name)] = taxon
    return by_name, scientific, raw["ambiguous"]


def _label(taxon: int, scientific: dict[int, str]) -> str:
    return f"{scientific.get(taxon, 'taxon')} (taxon {taxon})"


def _empty(text: str) -> ToolError | None:
    if text:
        return None
    return ToolError(
        "organism is required. Ask the user which organism they mean (for example "
        "'human', 'mouse' or a taxon ID like 9606); do not assume human. If they say "
        "they do not care, pass 'any' to search all organisms (reviewed entries only)."
    )


def _lookup_query(name: str) -> str:
    phrase = " ".join(name.replace('"', " ").replace("\\", " ").split())
    return f'scientific:"{phrase}" OR common:"{phrase}"'


def _plausible(results: list[dict], text: str) -> list[dict]:
    """Exact name matches at species rank or below; if several, those with proteins."""
    wanted = _key(text)
    exact = [
        r
        for r in results
        if r.get("rank") in RANKS
        and wanted in {_key(r.get("scientificName", "")), _key(r.get("commonName", ""))}
    ]
    reviewed = [r for r in exact if r.get("statistics", {}).get("reviewedProteinCount")]
    return reviewed if len(reviewed) >= 1 and len(exact) > 1 else exact


def _option(r: dict) -> str:
    common = f", {r['commonName']}" if r.get("commonName") else ""
    return f"{r['scientificName']}{common} (taxon {r['taxonId']})"


async def _lookup(client: httpx.AsyncClient, text: str) -> Organism:
    response = await get(
        client,
        UNIPROT_TAXONOMY_URL,
        service="UniProt taxonomy",
        params={"query": _lookup_query(text), "format": "json", "size": LOOKUP_SIZE},
    )
    results = response.json().get("results", []) if response.status_code == 200 else []
    found = _plausible(results, text)
    if len(found) == 1:
        logger.info("organism %r resolved to taxon %s", text, found[0]["taxonId"])
        return Organism(found[0]["taxonId"], found[0]["scientificName"], text)
    if found:
        options = "; ".join(_option(r) for r in found[:MAX_LISTED])
        raise ToolError(
            f"'{text}' matches several organisms: {options}. Ask the user which one they "
            "mean, then search again with its taxon ID."
        )
    raise ToolError(
        f"Unknown organism '{text}': no species has exactly that name. Check the spelling, "
        "try the scientific name, or give the NCBI taxon ID (look it up at "
        f"{LOOKUP_URL}). Ask the user if unsure."
    )


async def resolve(client: httpx.AsyncClient, organism: str) -> Organism:
    """The organism for a taxon ID, a common name or a scientific name.

    Raises ToolError, with what to do next, if the name is unknown or ambiguous.
    """
    text = organism.strip()
    if error := _empty(text):
        raise error
    if _key(text) in ANY_WORDS:
        return Organism(None, "all organisms", text)
    by_name, scientific, ambiguous = _table()
    if text.isdigit() and int(text) > 0:
        taxon = int(text)
        return Organism(taxon, scientific.get(taxon, f"taxon {taxon}"), text)
    key = _key(text)
    if key in ambiguous:
        options = "; ".join(_label(t, scientific) for t in ambiguous[key])
        raise ToolError(
            f"'{text}' is ambiguous: it could be {options}. Ask the user which one "
            "they mean, then search again with its taxon ID."
        )
    if key in by_name:
        taxon = by_name[key]
        return Organism(taxon, scientific[taxon], text)
    return await _lookup(client, text)
