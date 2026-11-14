"""Live checks against the real services. Skipped by default; run with `pytest -m live`."""

import logging

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


async def test_live_find_structures(client, caplog):
    with caplog.at_level(logging.INFO, logger="beacons_mcp"):
        result = await client.call_tool(
            "find_structures", {"uniprot_accession": "Q92793"}
        )

    assert not result.is_error, result.content
    data = result.structured_content
    assert data["protein"]["entry_name"] == "CBP_HUMAN"
    assert data["protein"]["sequence_length"] == 2442
    assert data["total_models"] > 50
    assert "experimentally determined" in data["by_category"]
    models = data["models"]
    assert all(m["model_url"].startswith("http") for m in models)
    # CREB-binding protein is long and its experimental structures are fragments:
    # the first model is the best-spanning experimental one, and the rest follow by span.
    assert models[0]["category"] == "experimentally determined"
    assert any(m["span_percent"] == 100.0 for m in models[1:])
    spans = [m["span_percent"] for m in models[1:]]
    assert spans == sorted(spans, reverse=True)
    # The provider descriptions came from the live registry, not the bundled snapshot.
    assert "registry refreshed" in caplog.text
    assert "using the bundled snapshot" not in caplog.text
    assert all(p["description"] for p in data["by_provider"])


async def test_live_show_structure_and_fetch_structure_file(client):
    import base64
    import gzip

    shown = await client.call_tool(
        "show_structure", {"uniprot_accession": "P69905", "model_id": "AF-P69905-F1"}
    )

    assert not shown.is_error, shown.content
    info = shown.structured_content
    assert info["model_id"] == "AF-P69905-F1"
    assert info["file_url"].startswith("https://")

    first = await client.call_tool(
        "fetch_structure_file", {"token": info["token"], "chunk": 0}
    )
    assert not first.is_error, first.content
    parts = [first.structured_content]
    for n in range(1, first.structured_content["chunks"]):
        more = await client.call_tool(
            "fetch_structure_file", {"token": info["token"], "chunk": n}
        )
        parts.append(more.structured_content)
    text = gzip.decompress(
        b"".join(base64.b64decode(p["data"]) for p in parts)
    ).decode()

    assert parts[0]["format"] == "mmcif"
    assert text.startswith("data_")
    assert "_atom_site.Cartn_x" in text
    atoms = [line for line in text.splitlines() if line.startswith("ATOM ")]
    assert len(atoms) > 500  # a whole hemoglobin alpha chain, not an error page
    assert parts[0]["decoded_bytes"] == len(text.encode())
