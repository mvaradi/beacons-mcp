"""Structure models for a UniProt accession, from the 3D-Beacons Hub."""

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from beacons_mcp import registry
from beacons_mcp.clients import HUB_BASE_URL, get
from beacons_mcp.models import ModelSummary, ProteinInfo, ProviderTotal, StructureResult

logger = logging.getLogger("beacons_mcp")

# The accession format from the UniProt documentation, canonical entries only.
# Version suffixes ("P69905.1") are rejected on purpose: the Hub answers them with
# a partial result from a single provider.
ACCESSION_RE = re.compile(
    r"[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2}"
)

# Experimental evidence first, then ensembles (derived from experiments), then
# predictions. Categories are the Hub's own strings, lower-cased; an unknown one
# sorts last.
_CATEGORY_RANK = {
    "experimentally determined": 0,
    "conformational ensemble": 1,
    "template-based": 2,
    "ab-initio": 3,
}
_UNRANKED = 9

_Segment = tuple[int, int]


@dataclass
class _Entry:
    """One model, with every residue segment the Hub reported for it."""

    raw: dict[str, Any]
    segments: list[_Segment] = field(default_factory=list)


def normalise_accession(raw: str) -> str:
    accession = raw.strip().upper()
    if not ACCESSION_RE.fullmatch(accession):
        raise ToolError(
            f"'{raw.strip()}' is not a UniProt accession (expected something like P69905 "
            "or Q92793). If you only have a protein or gene name, use search_proteins first "
            "to find the accession."
        )
    return accession


async def fetch_summary(client: httpx.AsyncClient, accession: str) -> dict[str, Any]:
    response = await get(
        client,
        f"{HUB_BASE_URL}/uniprot/summary/{accession}.json",
        service="The 3D-Beacons Hub",
    )
    if response.status_code == 404:
        raise ToolError(
            f"The 3D-Beacons Hub does not know the accession '{accession}'. "
            "Use search_proteins first to find a valid UniProt accession."
        )
    if response.status_code != 200:
        logger.warning("Hub returned HTTP %d for %s", response.status_code, accession)
        raise ToolError(
            f"The 3D-Beacons Hub could not answer for '{accession}' "
            f"(HTTP {response.status_code}). Check the accession with search_proteins."
        )
    return response.json()


def _merge(segments: list[_Segment]) -> list[_Segment]:
    merged: list[_Segment] = []
    for start, end in sorted(segments):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _group_entries(structures: list[dict[str, Any]]) -> list[_Entry]:
    """Merge items that are the same model reported once per residue segment."""
    entries: dict[tuple[Any, ...], _Entry] = {}
    for item in structures:
        summary = item["summary"]
        key = (
            summary.get("provider"),
            summary.get("model_identifier"),
            summary.get("model_url"),
        )
        entry = entries.setdefault(key, _Entry(raw=summary))
        start, end = summary.get("uniprot_start"), summary.get("uniprot_end")
        if start is not None and end is not None:
            entry.segments.append((int(start), int(end)))
    return list(entries.values())


def _ensemble_note(conformers: int, file_format: str | None) -> str:
    return (
        f"An ensemble of {conformers} conformers; model_url serves only a sample of it "
        "(the first models), not the whole ensemble. The Hub labels the format "
        f"{file_format or 'unknown'}, but the file may be multi-model PDB text. "
        "Check the content before parsing."
    )


def _summarise(
    entry: _Entry, sequence_length: int | None
) -> tuple[tuple[Any, ...], ModelSummary]:
    raw = entry.raw
    merged = _merge(entry.segments)
    covered = sum(end - start + 1 for start, end in merged)
    # The Hub's own "coverage" field is not used: it disagrees with the residue range
    # for some providers (an ensemble spanning 8% of a protein reports 1.0).
    percent = (
        round(100 * min(covered / sequence_length, 1.0), 1) if sequence_length else None
    )
    conformers = raw.get("number_of_conformers")
    conformers = int(conformers) if conformers is not None else None
    file_format = raw.get("model_format")
    category = (raw.get("model_category") or "unknown").lower()
    resolution = raw.get("resolution")
    confidence = raw.get("confidence_avg_local_score")

    model = ModelSummary(
        provider=raw.get("provider", "unknown"),
        model_id=str(raw.get("model_identifier", "")),
        category=category,
        residues=", ".join(f"{start}-{end}" for start, end in merged) or "unknown",
        coverage_percent=percent,
        resolution_angstrom=resolution,
        experimental_method=raw.get("experimental_method"),
        confidence_type=raw.get("confidence_type"),
        confidence_score=confidence,
        model_url=raw.get("model_url", ""),
        file_format=file_format,
        page_url=raw.get("model_page_url"),
        conformers=conformers,
        note=_ensemble_note(conformers, file_format)
        if conformers is not None
        else None,
    )
    sort_key = (
        _CATEGORY_RANK.get(category, _UNRANKED),
        -(percent or 0.0),
        resolution if resolution is not None else float("inf"),
        -(confidence or 0.0),
    )
    return sort_key, model


def _select(
    ranked: list[tuple[tuple[Any, ...], ModelSummary]], limit: int
) -> list[ModelSummary]:
    """Pick ``limit`` models: the best of each provider first, then the next best, and so on.

    A protein can have hundreds of models from one provider; plain ranking would
    show nothing from the others.
    """
    by_provider: dict[str, list[tuple[tuple[Any, ...], ModelSummary]]] = {}
    for item in sorted(ranked, key=lambda pair: pair[0]):
        by_provider.setdefault(item[1].provider, []).append(item)
    queues = sorted(by_provider.items(), key=lambda kv: (kv[1][0][0], kv[0]))

    chosen: list[ModelSummary] = []
    depth = 0
    while len(chosen) < limit and any(len(models) > depth for _, models in queues):
        for _, models in queues:
            if depth < len(models) and len(chosen) < limit:
                chosen.append(models[depth][1])
        depth += 1
    return chosen


def build_result(summary: dict[str, Any], max_results: int) -> StructureResult:
    entry = summary.get("uniprot_entry", {})
    length = entry.get("sequence_length")
    summarised = [
        _summarise(e, length) for e in _group_entries(summary.get("structures", []))
    ]

    models = [model for _, model in summarised]
    providers = Counter(m.provider for m in models)
    categories = Counter(m.category for m in models)
    shown = _select(summarised, max_results)

    if models:
        note = (
            "Totals cover every model found. 'models' shows the best of each provider first, "
            "so it is a selection, not the full set."
        )
    else:
        note = "The 3D-Beacons network has no structure models for this accession."
    return StructureResult(
        protein=ProteinInfo(
            accession=entry.get("ac", ""),
            entry_name=entry.get("id"),
            sequence_length=length,
        ),
        total_models=len(models),
        by_provider=[
            ProviderTotal(
                provider=name, models=count, description=registry.describe(name)
            )
            for name, count in sorted(providers.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        by_category=dict(categories.most_common()),
        models=shown,
        models_not_shown=len(models) - len(shown),
        note=note,
    )


async def find_structures(
    client: httpx.AsyncClient, uniprot_accession: str, max_results: int
) -> StructureResult:
    accession = normalise_accession(uniprot_accession)
    summary = await fetch_summary(client, accession)
    result = build_result(summary, max_results)
    logger.info("find_structures %s: %d models", accession, result.total_models)
    return result
