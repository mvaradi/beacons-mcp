import re
from collections import Counter
from pathlib import Path

import httpx
import pytest
from conftest import error_text

from beacons_mcp.clients import HUB_BASE_URL, REGISTRY_URL, USER_AGENT
from beacons_mcp.registry import MAX_DESCRIPTION_CHARS

# The provider-description fetch runs alongside the Hub call, and is cancelled when
# the Hub call fails first, so its route is not always used.
pytestmark = pytest.mark.respx(assert_all_called=False)


@pytest.fixture(autouse=True)
def registry_route(respx_mock, load_fixture):
    """Serve the captured registry as the 'live' one."""
    return respx_mock.get(REGISTRY_URL).respond(
        200, json=load_fixture("registry_providers.json")
    )


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


def item(
    model_id, category, start, end, *, provider="P", resolution=None, confidence=None
):
    """A minimal Hub 'structure' item for synthetic ranking tests."""
    summary = {
        "provider": provider,
        "model_identifier": model_id,
        "model_category": category,
        "model_url": f"https://example.org/{model_id}.cif",
        "model_format": "MMCIF",
        "uniprot_start": start,
        "uniprot_end": end,
    }
    if resolution is not None:
        summary["resolution"] = resolution
    if confidence is not None:
        summary["confidence_avg_local_score"] = confidence
    return {"summary": summary}


def mock_items(respx_mock, items, *, length=1000, accession="P69905"):
    respx_mock.get(hub_url(accession)).respond(
        200,
        json={
            "uniprot_entry": {
                "ac": accession,
                "id": "TEST_HUMAN",
                "sequence_length": length,
            },
            "structures": items,
        },
    )


def by_id(models):
    return {m["model_id"]: m for m in models}


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
    # Totals count every item the Hub returned, whatever max_results is.
    items = [i["summary"] for i in fixture["structures"]]
    assert data["total_models"] == len(items) == 13
    providers = Counter(s["provider"] for s in items)
    categories = Counter(s["model_category"].lower() for s in items)
    assert {p["provider"]: p["models"] for p in data["by_provider"]} == dict(providers)
    assert data["by_category"] == dict(categories)
    # Largest provider first.
    counts = [p["models"] for p in data["by_provider"]]
    assert counts == sorted(counts, reverse=True)

    # Default max_results is 10: ten shown, three left out, and the output says so.
    assert len(data["models"]) == 10
    assert data["models_not_shown"] == 3

    assert route.calls.last.request.headers["user-agent"] == USER_AGENT


@pytest.mark.anyio
async def test_best_experimental_model_first_then_the_rest_by_span(client, respx_mock):
    # A 1000-residue protein: the experimental structures are fragments, the
    # predictions are (nearly) full length.
    mock_items(
        respx_mock,
        [
            item("tiny-xray", "EXPERIMENTALLY DETERMINED", 1, 50, resolution=1.0),
            item("bigger-xray", "EXPERIMENTALLY DETERMINED", 1, 100, resolution=3.5),
            item("full-pred", "AB-INITIO", 1, 1000, confidence=90.0),
            item("near-full-pred", "AB-INITIO", 5, 990, confidence=95.0),
            item("half-template", "TEMPLATE-BASED", 1, 500),
        ],
    )

    result = await find(client, "P69905")

    ids = [m["model_id"] for m in result.structured_content["models"]]
    # The experimental model spanning most of the protein is first (even with the
    # worse resolution), the rest follow by span, and the tiny one comes last.
    assert ids == [
        "bigger-xray",
        "full-pred",
        "near-full-pred",
        "half-template",
        "tiny-xray",
    ]


@pytest.mark.anyio
async def test_ties_in_span_prefer_experimental_then_better_quality(client, respx_mock):
    mock_items(
        respx_mock,
        [
            item("pred-low", "AB-INITIO", 1, 1000, confidence=60.0),
            item("pred-high", "AB-INITIO", 1, 1000, confidence=90.0),
            item("template", "TEMPLATE-BASED", 1, 1000),
            item("xray-poor", "EXPERIMENTALLY DETERMINED", 1, 1000, resolution=3.0),
            item("xray-good", "EXPERIMENTALLY DETERMINED", 1, 1000, resolution=1.2),
        ],
    )

    result = await find(client, "P69905")

    ids = [m["model_id"] for m in result.structured_content["models"]]
    assert ids == ["xray-good", "xray-poor", "template", "pred-high", "pred-low"]


@pytest.mark.anyio
async def test_without_experimental_models_it_is_just_span_order(client, respx_mock):
    mock_items(
        respx_mock,
        [
            item("small", "AB-INITIO", 1, 100, confidence=99.0),
            item("big", "AB-INITIO", 1, 900, confidence=50.0),
        ],
    )

    result = await find(client, "P69905")

    ids = [m["model_id"] for m in result.structured_content["models"]]
    assert ids == ["big", "small"]


@pytest.mark.anyio
async def test_one_provider_cannot_crowd_out_the_others(client, respx_mock):
    many_from_one = [
        item(
            f"x{n}",
            "EXPERIMENTALLY DETERMINED",
            1,
            1000 - n,
            provider="Big",
            resolution=2.0,
        )
        for n in range(30)
    ]
    others = [
        item("a", "AB-INITIO", 1, 400, provider="A", confidence=80.0),
        item("b", "AB-INITIO", 1, 300, provider="B", confidence=80.0),
        item("c", "AB-INITIO", 1, 200, provider="C", confidence=80.0),
    ]
    mock_items(respx_mock, many_from_one + others)

    result = await find(client, "P69905", max_results=5)

    models = result.structured_content["models"]
    assert models[0]["model_id"] == "x0"  # the best experimental model, pinned first
    assert {m["provider"] for m in models} == {"Big", "A", "B", "C"}
    assert Counter(m["provider"] for m in models)["Big"] == 2  # pinned + one more
    assert result.structured_content["models_not_shown"] == 28


@pytest.mark.anyio
async def test_selection_on_real_data_puts_the_longest_experimental_model_first(
    client, respx_mock, load_fixture
):
    mock_hub(respx_mock, load_fixture, "Q92793")
    fixture = load_fixture("hub_summary_Q92793.json")
    length = fixture["uniprot_entry"]["sequence_length"]
    longest = max(
        s["uniprot_end"] - s["uniprot_start"] + 1
        for s in (i["summary"] for i in fixture["structures"])
        if s["model_category"] == "EXPERIMENTALLY DETERMINED"
    )

    result = await find(client, "Q92793", max_results=10)

    models = result.structured_content["models"]
    assert models[0]["category"] == "experimentally determined"
    assert models[0]["span_percent"] == round(100 * longest / length, 1)
    # After the first, never more span than the one before.
    spans = [m["span_percent"] for m in models[1:]]
    assert spans == sorted(spans, reverse=True)


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
    assert experimental["span_percent"] == 99.3  # 141 of 142 residues
    assert experimental["resolution_angstrom"] == 1.07
    assert experimental["experimental_method"] == "X-RAY DIFFRACTION"
    assert experimental["confidence_score"] is None
    assert experimental["model_url"].startswith("https://")
    assert experimental["file_format"] == "MMCIF"

    predicted = models["AF-0000000211046780"]
    assert predicted["category"] == "ab-initio"
    assert predicted["span_percent"] == 100.0
    assert predicted["confidence_type"] == "pLDDT"
    assert predicted["confidence_score"] == 94.52
    assert predicted["resolution_angstrom"] is None
    assert predicted["note"] is None


@pytest.mark.anyio
async def test_provider_rows_are_reported_as_the_hub_lists_them(
    client, respx_mock, load_fixture
):
    """The Hub lists some entries once per residue segment. We do not merge them."""
    mock_hub(respx_mock, load_fixture, "Q92793")
    fixture = load_fixture("hub_summary_Q92793.json")

    result = await find(client, "Q92793", max_results=50)

    data = result.structured_content
    assert data["total_models"] == len(fixture["structures"]) == len(data["models"])
    rows = [m for m in data["models"] if m["model_id"] == "9h0k"]
    assert sorted((m["residues"], m["span_percent"]) for m in rows) == [
        ("1322-1555", 9.6),
        ("1618-1700", 3.4),
    ]


@pytest.mark.anyio
async def test_span_is_computed_from_residues_not_the_hubs_coverage_field(
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
    assert ensemble["span_percent"] == 8.4  # 206 / 2442


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
    assert ensemble["span_percent"] == 7.9  # 11 / 140
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
async def test_providers_are_described_from_the_live_registry(
    client, respx_mock, load_fixture, registry_route
):
    mock_hub(respx_mock, load_fixture, "P69905")
    live = load_fixture("registry_providers.json")
    for provider in live["providers"]:
        if provider["providerId"] == "levylab":
            provider["providerDescription"] = (
                "A description only the live registry has. More."
            )
    registry_route.respond(200, json=live)

    result = await find(client, "P69905")

    totals = {t["provider"]: t for t in result.structured_content["by_provider"]}
    assert (
        totals["levylab"]["description"] == "A description only the live registry has."
    )
    for total in totals.values():
        assert total["description"], f"no description for {total['provider']}"
        assert len(total["description"]) <= MAX_DESCRIPTION_CHARS


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure",
    [httpx.ReadTimeout("slow"), httpx.ConnectError("down"), 404, 500, "not json"],
    ids=["timeout", "connection-error", "http-404", "http-500", "bad-body"],
)
async def test_registry_trouble_never_fails_the_tool(
    client, respx_mock, load_fixture, registry_route, failure
):
    mock_hub(respx_mock, load_fixture, "P69905")
    if isinstance(failure, int):
        registry_route.respond(failure)
    elif isinstance(failure, str):
        registry_route.respond(200, text=failure)
    else:
        registry_route.mock(side_effect=failure)

    result = await find(client, "P69905")

    assert not result.is_error
    # The bundled snapshot stands in, so the descriptions are still there.
    totals = result.structured_content["by_provider"]
    assert all(t["description"] for t in totals)


@pytest.mark.anyio
async def test_registry_is_fetched_once_per_server_run(
    client, respx_mock, load_fixture, registry_route
):
    mock_hub(respx_mock, load_fixture, "P69905")

    await find(client, "P69905")
    await find(client, "P69905")

    assert registry_route.call_count == 1


@pytest.mark.anyio
async def test_max_results_limits_models_and_reports_the_rest(
    client, respx_mock, load_fixture
):
    mock_hub(respx_mock, load_fixture, "P69905")

    result = await find(client, "P69905", max_results=1)

    data = result.structured_content
    assert len(data["models"]) == 1
    assert data["models"][0]["category"] == "experimentally determined"
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
async def test_bad_accession_format_is_rejected_without_calling_anything(
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
    mock_items(respx_mock, [], length=68, accession="P00000")

    result = await find(client, "P00000")

    assert not result.is_error
    data = result.structured_content
    assert data["total_models"] == 0
    assert data["models"] == []
    assert data["models_not_shown"] == 0
    assert "no structure models" in data["note"]


@pytest.mark.anyio
async def test_unknown_category_and_provider_are_passed_through(client, respx_mock):
    mock_items(
        respx_mock,
        [
            item("a", "BRAND NEW KIND", 1, 1000, provider="SomeNewProvider"),
            item("b", "AB-INITIO", 1, 1000, provider="SomeNewProvider"),
        ],
    )

    result = await find(client, "P69905")

    data = result.structured_content
    # An unknown category sorts after the known ones when the span is the same.
    assert [m["model_id"] for m in data["models"]] == ["b", "a"]
    assert data["by_category"] == {"brand new kind": 1, "ab-initio": 1}
    # A provider the registry does not know just has no description.
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
