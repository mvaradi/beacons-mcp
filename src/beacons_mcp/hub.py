"""Structure models for a UniProt accession, from the 3D-Beacons Hub."""

import asyncio
import logging
import re
from collections import Counter
from collections.abc import Mapping
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

EXPERIMENTAL = "experimentally determined"

# How much a category counts when two models span equally much of the protein:
# experimental evidence first, then ensembles (derived from experiments), then
# predictions. Categories are the Hub's own strings, lower-cased; an unknown one
# sorts last.
_CATEGORY_RANK = {
    EXPERIMENTAL: 0,
    "conformational ensemble": 1,
    "template-based": 2,
    "ab-initio": 3,
}
_UNRANKED = 9

_Ranked = tuple[tuple[Any, ...], ModelSummary]


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


def _ensemble_note(conformers: int, file_format: str | None) -> str:
    return (
        f"An ensemble of {conformers} conformers; model_url serves only a sample of it "
        "(the first models), not the whole ensemble. The Hub labels the format "
        f"{file_format or 'unknown'}, but the file may be multi-model PDB text. "
        "Check the content before parsing."
    )


def _summarise(raw: dict[str, Any], sequence_length: int | None) -> _Ranked:
    """One Hub item as reported: the Hub's values, plus a derived span percentage."""
    start, end = raw.get("uniprot_start"), raw.get("uniprot_end")
    if start is not None and end is not None:
        residues = f"{start}-{end}"
        span = (
            round(100 * min((end - start + 1) / sequence_length, 1.0), 1)
            if sequence_length
            else None
        )
    else:
        residues, span = "unknown", None
    # The Hub's own "coverage" field is not used: it disagrees with the residue range
    # for some providers (an ensemble spanning 8% of a protein reports 1.0).
    conformers = raw.get("number_of_conformers")
    conformers = int(conformers) if conformers is not None else None
    file_format = raw.get("model_format")
    category = (raw.get("model_category") or "unknown").lower()
    resolution = raw.get("resolution")
    confidence = raw.get("confidence_avg_local_score")
    model_id = str(raw.get("model_identifier", ""))

    model = ModelSummary(
        provider=raw.get("provider", "unknown"),
        model_id=model_id,
        category=category,
        residues=residues,
        span_percent=span,
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
    # Most of the protein first. Ties: category, then better resolution or confidence.
    sort_key = (
        -(span or 0.0),
        _CATEGORY_RANK.get(category, _UNRANKED),
        resolution if resolution is not None else float("inf"),
        -(confidence or 0.0),
        model_id,
    )
    return sort_key, model


def model_summary(raw: dict[str, Any], sequence_length: int | None) -> ModelSummary:
    """One Hub item as find_structures shows it (for other tools describing a model)."""
    return _summarise(raw, sequence_length)[1]


def _select(ranked: list[_Ranked], limit: int) -> list[ModelSummary]:
    """Choose and order the ``limit`` models to show.

    1. The experimental model that spans the most of the protein goes first, however
       small: measured evidence is worth having even when it is one domain.
    2. The other slots are filled round-robin across providers (each provider's
       best-spanning model, then each one's next best, and so on), so a provider with
       hundreds of models cannot crowd out the rest. The pinned model counts as its
       provider's turn in the round it belongs to.
    3. After the first, models are listed by how much of the protein they span.
    """
    ranked = sorted(ranked, key=lambda pair: pair[0])
    pinned = next((r for r in ranked if r[1].category == EXPERIMENTAL), None)
    slots = limit - (1 if pinned else 0)

    by_provider: dict[str, list[_Ranked]] = {}
    for item in ranked:
        by_provider.setdefault(item[1].provider, []).append(item)
    queues = sorted(by_provider.values(), key=lambda q: (q[0][0], q[0][1].provider))

    picked: list[_Ranked] = []
    depth = 0
    while len(picked) < slots and any(len(q) > depth for q in queues):
        for queue in queues:
            if (
                depth < len(queue)
                and queue[depth] is not pinned
                and len(picked) < slots
            ):
                picked.append(queue[depth])
        depth += 1
    picked.sort(key=lambda pair: pair[0])
    return [r[1] for r in ([pinned] if pinned else []) + picked]


def build_result(
    summary: dict[str, Any], max_results: int, descriptions: Mapping[str, str]
) -> StructureResult:
    entry = summary.get("uniprot_entry", {})
    length = entry.get("sequence_length")
    ranked = [
        _summarise(item["summary"], length) for item in summary.get("structures", [])
    ]

    models = [model for _, model in ranked]
    providers = Counter(m.provider for m in models)
    categories = Counter(m.category for m in models)
    shown = _select(ranked, max_results)

    if models:
        note = (
            "Totals count every model the Hub returned. 'models' is a selection: the "
            "experimental model spanning most of the protein first, then others by how "
            "much of the protein they span, spread across providers."
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
                provider=name,
                models=count,
                description=registry.describe(name, descriptions),
            )
            for name, count in sorted(providers.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        by_category=dict(categories.most_common()),
        models=shown,
        models_not_shown=len(models) - len(shown),
        note=note,
    )


async def find_structures(
    client: httpx.AsyncClient,
    provider_registry: registry.Registry,
    uniprot_accession: str,
    max_results: int,
) -> StructureResult:
    accession = normalise_accession(uniprot_accession)
    # Provider descriptions are fetched alongside the (slower) Hub call, so they add no
    # waiting. They are optional and never raise.
    descriptions = asyncio.create_task(provider_registry.descriptions())
    try:
        summary = await fetch_summary(client, accession)
    except BaseException:
        descriptions.cancel()
        raise
    result = build_result(summary, max_results, await descriptions)
    logger.info("find_structures %s: %d models", accession, result.total_models)
    return result
