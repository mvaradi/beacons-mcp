"""Resolve an organism the user names to an NCBI taxonomy ID.

The UniProt search is filtered by taxonomy ID, never by a substring of the organism's
name: "mouse" is part of "Mouse-ear cress" (Arabidopsis), so a name match returns
plants. Names are data (``data/organisms.json``); anything not listed must be given as a
taxon ID, and ambiguous names are an error that lists the choices.
"""

import json
import re
from dataclasses import dataclass
from functools import cache
from importlib import resources

from mcp.server.mcpserver.exceptions import ToolError

LOOKUP_URL = "https://www.uniprot.org/taxonomy"


@dataclass(frozen=True)
class Organism:
    taxon_id: int
    name: str


def _key(text: str) -> str:
    return " ".join(re.sub(r"\([^)]*\)", " ", text).lower().split())


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


def resolve(organism: str) -> Organism:
    """The organism for a taxon ID, a common name or a scientific name.

    Raises ToolError, with what to do next, if the name is unknown or ambiguous.
    """
    text = organism.strip()
    if not text:
        raise ToolError(
            "organism is required. Ask the user which organism they mean "
            "(for example 'human', 'mouse' or a taxon ID like 9606); do not assume human."
        )
    by_name, scientific, ambiguous = _table()
    if text.isdigit() and int(text) > 0:
        taxon = int(text)
        return Organism(taxon, scientific.get(taxon, f"taxon {taxon}"))
    key = _key(text)
    if key in ambiguous:
        options = "; ".join(_label(t, scientific) for t in ambiguous[key])
        raise ToolError(
            f"'{text}' is ambiguous: it could be {options}. Ask the user which one "
            "they mean, then search again with its taxon ID."
        )
    if key in by_name:
        taxon = by_name[key]
        return Organism(taxon, scientific[taxon])
    raise ToolError(
        f"Unknown organism '{text}'. Use its NCBI taxon ID (look it up at {LOOKUP_URL}), "
        "or one of the known names: "
        + ", ".join(sorted({n for n in by_name if " " not in n and "." not in n}))
        + ". Ask the user if unsure."
    )
