"""Live checks against the real services. Skipped by default; run with `pytest -m live`."""

import pytest

pytestmark = [pytest.mark.live, pytest.mark.anyio]


async def test_live_search_proteins(client):
    result = await client.call_tool(
        "search_proteins", {"query": "CBP1", "organism": "human"}
    )

    assert not result.is_error, result.content
    candidates = result.structured_content["candidates"]
    # "CBP1" is a synonym of the SERPINH1 gene in human (observed 2026-11-14).
    serpin = next(c for c in candidates if c["accession"] == "P50454")
    assert serpin["reviewed"] is True
    assert "SERPINH1" in serpin["gene_names"]


async def test_live_find_structures(client):
    result = await client.call_tool("find_structures", {"uniprot_accession": "P69905"})

    assert not result.is_error, result.content
    data = result.structured_content
    assert data["protein"]["entry_name"] == "HBA_HUMAN"
    assert data["protein"]["sequence_length"] == 142
    assert data["total_models"] > 50
    assert data["models"]
    assert "experimentally determined" in data["by_category"]
    assert all(m["model_url"].startswith("http") for m in data["models"])
    assert all(p["provider"] for p in data["by_provider"])
