import re
from collections import Counter
from pathlib import Path

import httpx
import pytest
from conftest import error_text

from beacons_mcp.clients import HUB_BASE_URL, USER_AGENT
from beacons_mcp.registry import MAX_DESCRIPTION_CHARS


def hub_url(accession):
    return f"{HUB_BASE_URL}/uniprot/summary/{accession}.json"


async def find(client, accession, **arguments):
    return await client.call_tool(
        "find_structures", {"uniprot_accession": accession, **arguments}
    )


def mock_hub(respx_mock, load_fixture, accession):
    return respx_mock.get(hub_url(accession)).respond(
        200, json=load_fixture(f"hub_summary_{accession}.json")
    )


def by_id(models):
    return {m["model_id"]: m for m in models}


def unique_models(fixture):
    """(provider, model id, category) of each distinct model in a Hub response.

    The Hub can list one model several times, once per residue segment; we merge
    those, so the expected number of models is the number of distinct ones.
    """
    return {
        (s["provider"], s["model_identifier"], s["model_category"].lower())
        for s in (i["summary"] for i in fixture["structures"])
    }


@pytest.mark.anyio
async def test_summarises_protein_totals_and_models(client, respx_mock, load_fixture):
    route = mock_hub(respx_mock, load_fixture, "P69905")
    fixture = load_fixture("hub_summary_P69905.json")

    result = await find(client, "P69905")

    assert not result.is_error
    data = result.structured_content
    assert data["protein"] == {
        "accession": "P69905",
        "entry_name": "HBA_HUMAN",
        "sequence_length": 142,
    }
    # Totals count every model in the response, whatever max_results is. The
    # fixture has 13 items but one model is listed twice (see below), so 12 models.
    models = unique_models(fixture)
    assert len(fixture["structures"]) == 13
    assert data["total_models"] == len(models) == 12
    providers = Counter(provider for provider, _, _ in models)
    categories = Counter(category for _, _, category in models)
    assert {p["provider"]: p["models"] for p in data["by_provider"]} == dict(providers)
    assert data["by_category"] == dict(categories)
    # Largest provider first.
    counts = [p["models"] for p in data["by_provider"]]
    assert counts == sorted(counts, reverse=True)

    # Default max_results is 10: ten shown, two left out, and the output says so.
    assert len(data["models"]) == 10
    assert data["models_not_shown"] == 2

    sent = route.calls.last.request
    assert sent.headers["user-agent"] == USER_AGENT


@pytest.mark.anyio
async def test_selection_shows_the_best_of_each_provider_first(
    client, respx_mock, load_fixture
):
    mock_hub(respx_mock, load_fixture, "P69905")
    provider_count = len(
        {
            i["summary"]["provider"]
            for i in load_fixture("hub_summary_P69905.json")["structures"]
        }
    )

    result = await find(client, "P69905", max_results=provider_count)

    models = result.structured_content["models"]
    # One model from every provider, not a run of models from the biggest one.
    assert len({m["provider"] for m in models}) == provider_count
    # Measured structures are listed before predictions.
    categories = [m["category"] for m in models]
    assert categories[0] == "experimentally determined"
    assert categories.index("ab-initio") > categories.index("template-based")
    # Within the experimental provider the best resolution wins.
    best = next(m for m in models if m["provider"] == models[0]["provider"])
    assert best["resolution_angstrom"] == 1.07


@pytest.mark.anyio
async def test_model_fields_for_experimental_and_predicted_models(
    client, respx_mock, load_fixture
):
    mock_hub(respx_mock, load_fixture, "P69905")

    result = await find(client, "P69905", max_results=50)

    models = by_id(result.structured_content["models"])
    experimental = models["2w72"]
    assert experimental["category"] == "experimentally determined"
    assert experimental["residues"] == "2-142"
    assert experimental["coverage_percent"] == 99.3  # 141 of 142 residues
    assert experimental["resolution_angstrom"] == 1.07
    assert experimental["experimental_method"] == "X-RAY DIFFRACTION"
    assert experimental["confidence_score"] is None
    assert experimental["model_url"].startswith("https://")
    assert experimental["file_format"] == "MMCIF"

    predicted = models["AF-0000000211046780"]
    assert predicted["category"] == "ab-initio"
    assert predicted["coverage_percent"] == 100.0
    assert predicted["confidence_type"] == "pLDDT"
    assert predicted["confidence_score"] == 94.52
    assert predicted["resolution_angstrom"] is None
    assert predicted["note"] is None


@pytest.mark.anyio
async def test_segments_of_one_model_are_merged(client, respx_mock, load_fixture):
    mock_hub(respx_mock, load_fixture, "Q92793")

    result = await find(client, "Q92793", max_results=50)

    data = result.structured_content
    assert data["protein"]["sequence_length"] == 2442
    two_segments = by_id(data["models"])["9h0k"]
    # The Hub lists this entry once per segment; we report one model.
    assert two_segments["residues"] == "1322-1555, 1618-1700"
    assert two_segments["coverage_percent"] == 13.0  # (234 + 83) / 2442
    assert sum(1 for m in data["models"] if m["model_id"] == "9h0k") == 1
    fixture = load_fixture("hub_summary_Q92793.json")
    assert data["total_models"] == len(data["models"]) == len(unique_models(fixture))
    assert len(unique_models(fixture)) < len(fixture["structures"])  # merging happened


@pytest.mark.anyio
async def test_coverage_is_computed_from_residues_not_the_hubs_field(
    client, respx_mock, load_fixture
):
    mock_hub(respx_mock, load_fixture, "Q92793")
    raw = {
        i["summary"]["model_identifier"]: i["summary"]
        for i in load_fixture("hub_summary_Q92793.json")["structures"]
    }
    # Observed live: the Hub reports coverage 1.0 for an ensemble spanning residues 1-206.
    assert raw["PED09322e001"]["coverage"] == 1.0
    assert raw["PED09322e001"]["uniprot_end"] == 206

    result = await find(client, "Q92793", max_results=50)

    ensemble = by_id(result.structured_content["models"])["PED09322e001"]
    assert ensemble["residues"] == "1-206"
    assert ensemble["coverage_percent"] == 8.4  # 206 / 2442


@pytest.mark.anyio
async def test_ensemble_is_flagged_as_a_sample_with_a_format_caveat(
    client, respx_mock, load_fixture
):
    mock_hub(respx_mock, load_fixture, "P37840")

    result = await find(client, "P37840", max_results=50)

    models = by_id(result.structured_content["models"])
    ensemble = models["PED00006e001"]
    assert ensemble["category"] == "conformational ensemble"
    assert ensemble["conformers"] == 576
    assert ensemble["residues"] == "73-83"
    assert ensemble["coverage_percent"] == 7.9  # 11 / 140
    assert ensemble["file_format"] == "MMCIF"  # as the Hub reports it...
    note = ensemble["note"]  # ...with the caveat that the file may differ
    assert "576 conformers" in note
    assert "only a sample" in note
    assert "multi-model PDB" in note
    assert models["PED00189e001"]["conformers"] == 34
    # Only ensembles carry a conformer count and a note.
    others = [m for m in models.values() if m["category"] != "conformational ensemble"]
    assert others
    assert all(m["conformers"] is None and m["note"] is None for m in others)


@pytest.mark.anyio
async def test_providers_are_described_from_the_bundled_registry(
    client, respx_mock, load_fixture
):
    mock_hub(respx_mock, load_fixture, "P69905")

    result = await find(client, "P69905")

    totals = result.structured_content["by_provider"]
    for total in totals:
        assert total["description"], f"no description for {total['provider']}"
        assert len(total["description"]) <= MAX_DESCRIPTION_CHARS


@pytest.mark.anyio
async def test_max_results_limits_models_and_reports_the_rest(
    client, respx_mock, load_fixture
):
    mock_hub(respx_mock, load_fixture, "P69905")

    result = await find(client, "P69905", max_results=1)

    data = result.structured_content
    assert len(data["models"]) == 1
    assert data["models_not_shown"] == data["total_models"] - 1


@pytest.mark.anyio
async def test_lowercase_accession_is_normalised(client, respx_mock, load_fixture):
    route = mock_hub(respx_mock, load_fixture, "P69905")

    result = await find(client, " p69905 ")

    assert not result.is_error
    assert route.called


@pytest.mark.anyio
@pytest.mark.parametrize(
    "accession",
    ["P69905.1", "hemoglobin", "", "HBA_HUMAN", "12345", "P69905-2", "../health"],
)
async def test_bad_accession_format_is_rejected_without_calling_the_hub(
    client, respx_mock, accession
):
    result = await find(client, accession)

    text = error_text(result)
    assert "not a UniProt accession" in text
    assert "use search_proteins first" in text
    assert not respx_mock.calls


@pytest.mark.anyio
async def test_unknown_accession_says_to_use_search_first(client, respx_mock):
    # The real Hub answers an unknown accession with 404 and an empty JSON object.
    respx_mock.get(hub_url("P99999")).respond(404, json={})

    result = await find(client, "P99999")

    text = error_text(result)
    assert "P99999" in text
    assert "Use search_proteins first" in text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure",
    [httpx.ReadTimeout("slow"), httpx.ConnectError("down"), 502, 503],
    ids=["timeout", "connection-error", "http-502", "http-503"],
)
async def test_slow_or_unavailable_hub_is_reported(client, respx_mock, failure):
    route = respx_mock.get(hub_url("P69905"))
    if isinstance(failure, int):
        route.respond(failure)
    else:
        route.mock(side_effect=failure)

    result = await find(client, "P69905")

    assert "3D-Beacons Hub is slow or unavailable" in error_text(result)


@pytest.mark.anyio
async def test_unexpected_client_error_is_reported_with_its_status(client, respx_mock):
    respx_mock.get(hub_url("P69905")).respond(422, json={"detail": "secret-xyz"})

    result = await find(client, "P69905")

    text = error_text(result)
    assert "HTTP 422" in text
    assert "secret-xyz" not in text


@pytest.mark.anyio
async def test_accession_without_models_is_not_an_error(client, respx_mock):
    respx_mock.get(hub_url("P00000")).respond(
        200,
        json={
            "uniprot_entry": {
                "ac": "P00000",
                "id": "DEFGA_MYTGA",
                "sequence_length": 68,
            },
            "structures": [],
        },
    )

    result = await find(client, "P00000")

    assert not result.is_error
    data = result.structured_content
    assert data["total_models"] == 0
    assert data["models"] == []
    assert data["models_not_shown"] == 0
    assert "no structure models" in data["note"]


@pytest.mark.anyio
async def test_unknown_category_is_passed_through_and_ranked_last(client, respx_mock):
    def item(model_id, category):
        return {
            "summary": {
                "provider": "SomeNewProvider",
                "model_identifier": model_id,
                "model_category": category,
                "model_url": f"https://example.org/{model_id}.cif",
                "model_format": "MMCIF",
                "uniprot_start": 1,
                "uniprot_end": 10,
            }
        }

    respx_mock.get(hub_url("P69905")).respond(
        200,
        json={
            "uniprot_entry": {"ac": "P69905", "id": "HBA_HUMAN", "sequence_length": 10},
            "structures": [item("a", "BRAND NEW KIND"), item("b", "AB-INITIO")],
        },
    )

    result = await find(client, "P69905")

    data = result.structured_content
    assert [m["model_id"] for m in data["models"]] == ["b", "a"]
    assert data["by_category"] == {"brand new kind": 1, "ab-initio": 1}
    # An unknown provider just has no description.
    assert data["by_provider"][0]["description"] is None


def test_source_code_names_no_provider():
    """Provider names must come from data, never from code."""
    import json

    from beacons_mcp import registry

    root = Path(registry.__file__).parent
    snapshot = json.loads(
        (root / "data" / "registry_providers.json").read_text("utf-8")
    )
    names = {
        name.lower()
        for p in snapshot["providers"]
        for name in (p["providerId"], p["providerName"])
    }
    for path in root.glob("*.py"):
        text = re.sub(r"https?://\S+", "", path.read_text(encoding="utf-8")).lower()
        for name in names:
            assert not re.search(rf"\b{re.escape(name)}\b", text), (
                f"{name!r} in {path.name}"
            )
