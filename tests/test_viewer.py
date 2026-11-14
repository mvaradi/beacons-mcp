import base64
import gzip
import os
import re
from urllib.parse import urlsplit

import httpx
import pytest
from conftest import error_text
from mcp import Client

from beacons_mcp import safe_fetch, viewer
from beacons_mcp.clients import HUB_BASE_URL, REGISTRY_URL
from beacons_mcp.models import ModelSummary
from beacons_mcp.registry import snapshot_origins
from beacons_mcp.server import mcp
from beacons_mcp.structure_file import CHUNK_CHARS

# Routes set up for the happy path may go unused when a call is refused early.
pytestmark = [pytest.mark.anyio, pytest.mark.respx(assert_all_called=False)]

MMCIF = b"data_model\n#\nloop_\n_atom_site.id\n_atom_site.Cartn_x\n1 11.104\n"
PDB_SAMPLE = (
    b"MODEL      1\nATOM      1  N   MET A   1      11.104  13.207   9.000\nENDMDL\n"
)


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    async def resolve(host):
        return ["93.184.216.34"]

    monkeypatch.setattr(safe_fetch, "_resolve", resolve)


@pytest.fixture(autouse=True)
def registry_route(respx_mock, load_fixture):
    """Serve the captured registry as the 'live' one."""
    return respx_mock.get(REGISTRY_URL).respond(
        200, json=load_fixture("registry_providers.json")
    )


def hub_url(accession):
    return f"{HUB_BASE_URL}/uniprot/summary/{accession}.json"


def model_item(
    model_id,
    *,
    url,
    category="AB-INITIO",
    provider="TestProvider",
    start=1,
    end=142,
    **extra,
):
    summary = {
        "provider": provider,
        "model_identifier": model_id,
        "model_category": category,
        "model_url": url,
        "model_format": "MMCIF",
        "model_page_url": f"https://example.org/entry/{model_id}",
        "uniprot_start": start,
        "uniprot_end": end,
        **extra,
    }
    return {"summary": summary}


def mock_hub(respx_mock, items, *, accession="P69905", length=142):
    return respx_mock.get(hub_url(accession)).respond(
        200,
        json={
            "uniprot_entry": {
                "ac": accession,
                "id": "HBA_HUMAN",
                "sequence_length": length,
            },
            "structures": items,
        },
    )


async def show(client, model_id="m1", accession="P69905"):
    return await client.call_tool(
        "show_structure", {"uniprot_accession": accession, "model_id": model_id}
    )


async def fetch(client, token, chunk=0):
    return await client.call_tool(
        "fetch_structure_file", {"token": token, "chunk": chunk}
    )


def unpack(chunks):
    return gzip.decompress(b"".join(base64.b64decode(c, validate=True) for c in chunks))


FILE_URL = "https://files.example.org/m1.cif"


@pytest.fixture
def hub_with_model(respx_mock):
    """One predicted model whose file is served by a mocked provider."""
    mock_hub(
        respx_mock,
        [
            model_item(
                "m1",
                url=FILE_URL,
                confidence_avg_local_score=94.5,
                confidence_type="pLDDT",
            )
        ],
    )
    return respx_mock.get(FILE_URL).respond(200, content=MMCIF)


# ---- how the tools are declared ---------------------------------------------------


async def test_show_structure_declares_the_viewer_and_stays_visible_to_the_model(
    client,
):
    tools = {t.name: t for t in (await client.list_tools()).tools}

    assert (
        tools["show_structure"].meta["ui"]["resourceUri"]
        == viewer.VIEWER_URI
        == "ui://beacons/viewer"
    )
    # No visibility entry means the default: visible to the model and the app.
    assert "visibility" not in tools["show_structure"].meta["ui"]


async def test_the_file_tool_is_app_only_and_points_at_the_same_viewer(client):
    tools = {t.name: t for t in (await client.list_tools()).tools}

    ui = tools["fetch_structure_file"].meta["ui"]
    assert ui["visibility"] == ["app"]  # hosts must keep it from the model
    assert ui["resourceUri"] == viewer.VIEWER_URI
    # The model-facing description names no file tool: nothing invites the model to call it.
    assert "fetch_structure_file" not in tools["show_structure"].description


async def test_show_structure_takes_no_url_so_a_url_can_never_be_supplied(client):
    tool = next(
        t for t in (await client.list_tools()).tools if t.name == "show_structure"
    )

    assert set(tool.input_schema["properties"]) == {"uniprot_accession", "model_id"}
    assert tool.input_schema["required"] == ["uniprot_accession", "model_id"]


async def test_the_viewer_resource_has_the_app_mime_type_and_a_provider_only_csp(
    client, tmp_path, monkeypatch
):
    page = tmp_path / "viewer.html"
    page.write_text("<!doctype html><title>stub viewer</title>", encoding="utf-8")
    monkeypatch.setenv(viewer.HTML_OVERRIDE_ENV, str(page))

    listed = (await client.list_resources()).resources
    read = await client.read_resource(viewer.VIEWER_URI)

    [resource] = [r for r in listed if str(r.uri) == viewer.VIEWER_URI]
    assert resource.mime_type == "text/html;profile=mcp-app"
    # The page may connect to the providers' hosts (from the registry) and nowhere else.
    expected = {
        "ui": {"csp": {"connectDomains": snapshot_origins(), "resourceDomains": []}}
    }
    assert resource.meta == expected
    [content] = read.contents
    assert content.mime_type == "text/html;profile=mcp-app"
    assert content.text == page.read_text(encoding="utf-8")
    assert content.meta == expected
    csp = content.meta["ui"]["csp"]
    assert set(csp) == {
        "connectDomains",
        "resourceDomains",
    }  # no frame or base-uri entries
    assert csp["resourceDomains"] == []  # no outside scripts, styles, images or fonts


async def test_the_csp_lists_only_plain_https_provider_hosts(client):
    [resource] = (await client.list_resources()).resources

    origins = resource.meta["ui"]["csp"]["connectDomains"]

    assert len(origins) >= 10
    assert origins == sorted(set(origins))
    for origin in origins:
        assert re.fullmatch(r"https://[a-z0-9.-]+(:\d+)?", origin), origin
        assert "*" not in origin
        assert not re.search(
            r"localhost|\.local$|\.internal$|//\d+\.\d+\.\d+\.\d+", origin
        )


def test_the_registry_hosts_cover_every_file_host_seen_in_real_hub_data(load_fixture):
    """If a provider served files from a host the registry does not list, the viewer's
    CSP would block it. Real Hub data from three proteins says that does not happen."""
    hosts = {
        urlsplit(item["summary"]["model_url"]).hostname
        for name in ("P69905", "Q92793", "P37840")
        for item in load_fixture(f"hub_summary_{name}.json")["structures"]
    }
    allowed = {urlsplit(origin).hostname for origin in snapshot_origins()}

    assert len(hosts) >= 8
    assert hosts - allowed == set()


async def csp_origins(client):
    [resource] = (await client.list_resources()).resources
    return resource.meta["ui"]["csp"]["connectDomains"]


def live_registry(load_fixture, origin="https://files.new-beacon.example.org/api/"):
    registry = load_fixture("registry_providers.json")
    registry["providers"].append(
        {
            "providerId": "newbeacon",
            "providerName": "New Beacon",
            "providerDescription": "Added after the snapshot.",
            "providerUrl": "https://new-beacon.example.org/",
            "baseServiceUrl": origin,
        }
    )
    return registry


async def test_showing_a_structure_widens_the_csp_to_the_live_registry(
    client, respx_mock, load_fixture, registry_route
):
    registry_route.respond(200, json=live_registry(load_fixture))
    mock_hub(respx_mock, [model_item("m1", url=FILE_URL)])
    assert "https://files.new-beacon.example.org" not in await csp_origins(client)

    result = await show(client)

    assert not result.is_error
    origins = await csp_origins(client)
    assert "https://files.new-beacon.example.org" in origins
    assert set(snapshot_origins()) <= set(origins)  # nothing known was lost
    assert origins == sorted(set(origins))


async def test_the_registry_is_fetched_alongside_the_hub_call_not_at_startup(
    client, respx_mock, registry_route
):
    # Starting the server (the client fixture) made no registry request.
    assert registry_route.call_count == 0
    mock_hub(respx_mock, [model_item("m1", url=FILE_URL)])

    await show(client)
    await show(client)

    assert registry_route.call_count == 1  # fetched once, then cached


@pytest.mark.parametrize("failure", [httpx.ConnectError("down"), 503, "not json"])
async def test_a_failed_registry_leaves_the_snapshot_csp_in_force(
    client, respx_mock, registry_route, failure
):
    if isinstance(failure, int):
        registry_route.respond(failure)
    elif isinstance(failure, str):
        registry_route.respond(200, text=failure)
    else:
        registry_route.mock(side_effect=failure)
    mock_hub(respx_mock, [model_item("m1", url=FILE_URL)])

    result = await show(client)

    assert not result.is_error  # the registry is optional
    assert await csp_origins(client) == snapshot_origins()


async def test_every_server_run_starts_from_the_snapshot_again(
    respx_mock, load_fixture, registry_route
):
    """The CSP widened in one run must not leak into the next."""
    registry_route.respond(200, json=live_registry(load_fixture))
    mock_hub(respx_mock, [model_item("m1", url=FILE_URL)])

    async with Client(mcp) as first:
        await show(first)
        assert "https://files.new-beacon.example.org" in await csp_origins(first)

    async with Client(mcp) as second:
        assert await csp_origins(second) == snapshot_origins()


async def test_a_refused_request_does_not_touch_the_registry(
    client, respx_mock, registry_route
):
    result = await show(client, "m1", accession="not-an-accession")

    assert result.is_error
    assert registry_route.call_count == 0
    assert not respx_mock.calls


async def test_a_missing_build_is_reported_with_the_fix(client, tmp_path, monkeypatch):
    monkeypatch.setenv(viewer.HTML_OVERRIDE_ENV, str(tmp_path / "not-built.html"))

    with pytest.raises(Exception, match="npm run build"):
        await client.read_resource(viewer.VIEWER_URI)


# ---- show_structure ---------------------------------------------------------------


async def test_show_structure_returns_a_summary_and_data_for_the_viewer(
    client, hub_with_model
):
    result = await show(client)

    assert not result.is_error
    data = result.structured_content
    assert isinstance(data["token"], str) and len(data["token"]) >= 20
    assert data["accession"] == "P69905"
    assert data["model_id"] == "m1"
    assert data["provider"] == "TestProvider"
    assert data["category"] == "ab-initio"
    assert data["residues"] == "1-142"
    assert data["span_percent"] == 100.0
    assert data["is_sample"] is False and data["conformers"] is None
    assert data["file_url"] == FILE_URL
    assert data["page_url"] == "https://example.org/entry/m1"
    # Claude gives the model this data as JSON in place of the text, so it carries the summary.
    assert data["summary"] == result.content[0].text
    assert f"File: {FILE_URL}" in data["summary"]


async def test_the_text_fallback_is_useful_on_its_own(client, hub_with_model):
    result = await show(client)

    text = result.content[0].text
    assert "Model m1 from TestProvider (ab-initio) for HBA_HUMAN." in text
    assert "Residues 1-142, 100.0% of the protein." in text
    assert "Confidence (pLDDT): 94.5." in text
    assert f"File: {FILE_URL}" in text
    assert "https://example.org/entry/m1" in text  # the model's web page
    # Nothing in it is for the viewer's eyes only.
    assert result.structured_content["token"] not in text


async def test_an_experimental_models_text_gives_resolution_and_method(
    client, respx_mock
):
    mock_hub(
        respx_mock,
        [
            model_item(
                "2w72",
                url=FILE_URL,
                category="EXPERIMENTALLY DETERMINED",
                resolution=1.07,
                experimental_method="X-RAY DIFFRACTION",
                start=2,
            )
        ],
    )

    result = await show(client, "2w72")

    text = result.content[0].text
    assert "Resolution 1.07 Å (X-RAY DIFFRACTION)." in text
    assert "Residues 2-142, 99.3% of the protein." in text


async def test_the_model_is_always_looked_up_again_in_the_hub(
    client, hub_with_model, respx_mock
):
    await show(client)
    await show(client)

    # Nothing about a model is remembered or trusted: each call asks the Hub again.
    hub_calls = [
        c for c in respx_mock.calls if "/uniprot/summary/" in str(c.request.url)
    ]
    assert len(hub_calls) == 2


async def test_a_text_summary_says_when_the_viewer_is_not_built(
    client, hub_with_model, tmp_path, monkeypatch
):
    monkeypatch.setenv(viewer.HTML_OVERRIDE_ENV, str(tmp_path / "missing.html"))

    result = await show(client)

    assert not result.is_error
    text = result.content[0].text
    assert "npm run build" in text
    assert f"File: {FILE_URL}" in text  # still something to open


async def test_an_ensemble_is_labelled_a_sample(client, respx_mock):
    mock_hub(
        respx_mock,
        [
            model_item(
                "ens1",
                url=FILE_URL,
                category="CONFORMATIONAL ENSEMBLE",
                number_of_conformers=576.0,
                start=73,
                end=83,
            )
        ],
        length=140,
    )

    result = await show(client, "ens1")

    data = result.structured_content
    assert data["is_sample"] is True
    assert data["conformers"] == 576
    text = result.content[0].text
    assert "576 conformers" in text
    assert "only a sample" in text


@pytest.mark.parametrize(
    "bad", ["P69905.1", "hemoglobin", "", "../health", "http://evil.example/x"]
)
async def test_a_bad_accession_is_refused_before_anything_is_fetched(
    client, respx_mock, bad
):
    result = await show(client, "m1", accession=bad)

    assert "not a UniProt accession" in error_text(result)
    assert not respx_mock.calls


@pytest.mark.parametrize("bad", ["", "   ", "x" * 201])
async def test_a_bad_model_id_is_refused_before_anything_is_fetched(
    client, respx_mock, bad
):
    result = await show(client, bad)

    assert "model_id" in error_text(result)
    assert not respx_mock.calls


async def test_an_unknown_model_id_says_to_call_find_structures_first(
    client, respx_mock
):
    mock_hub(respx_mock, [model_item("m1", url=FILE_URL)])

    result = await show(client, "does-not-exist")

    text = error_text(result)
    assert "does-not-exist" in text
    assert "find_structures first" in text


async def test_an_unknown_accession_is_reported_like_find_structures_does(
    client, respx_mock
):
    respx_mock.get(hub_url("P99999")).respond(404, json={})

    result = await show(client, "m1", accession="P99999")

    assert "Use search_proteins first" in error_text(result)


async def test_a_slow_hub_is_reported(client, respx_mock):
    respx_mock.get(hub_url("P69905")).mock(side_effect=httpx.ReadTimeout("slow"))

    result = await show(client)

    assert "slow or unavailable" in error_text(result)


async def test_a_model_without_a_file_url_cannot_be_shown(client, respx_mock):
    mock_hub(respx_mock, [model_item("m1", url="")])

    result = await show(client)

    assert "no file URL" in error_text(result)


# ---- fetch_structure_file ---------------------------------------------------------


async def test_the_file_comes_back_compressed_and_intact(client, hub_with_model):
    token = (await show(client)).structured_content["token"]

    result = await fetch(client, token)

    assert not result.is_error
    data = result.structured_content
    assert data["chunk"] == 0 and data["chunks"] == 1
    assert data["format"] == "mmcif"
    assert data["encoding"] == "gzip+base64"
    assert data["decoded_bytes"] == len(MMCIF)
    assert data["is_sample"] is False
    assert unpack([data["data"]]) == MMCIF


async def test_the_payload_stays_out_of_the_text_the_model_could_read(
    client, hub_with_model
):
    token = (await show(client)).structured_content["token"]

    result = await fetch(client, token)

    text = result.content[0].text
    assert text == "Structure file chunk 1 of 1."
    assert result.structured_content["data"] not in text


async def test_a_big_file_arrives_in_chunks_that_reassemble(client, respx_mock):
    body = b"data_big\n" + b"\n".join(
        os.urandom(24).hex().encode() for _ in range(20_000)
    )
    mock_hub(respx_mock, [model_item("big", url=FILE_URL)])
    file_route = respx_mock.get(FILE_URL).respond(200, content=body)
    token = (await show(client, "big")).structured_content["token"]

    first = (await fetch(client, token, 0)).structured_content
    rest = [
        (await fetch(client, token, i)).structured_content
        for i in range(1, first["chunks"])
    ]

    assert first["chunks"] > 2
    chunks = [first, *rest]
    assert [c["chunk"] for c in chunks] == list(range(first["chunks"]))
    assert all(len(c["data"]) <= CHUNK_CHARS for c in chunks)
    assert unpack([c["data"] for c in chunks]) == body
    assert file_route.call_count == 1  # downloaded once, then served from memory


async def test_a_chunk_that_does_not_exist_is_refused(client, hub_with_model):
    token = (await show(client)).structured_content["token"]

    result = await fetch(client, token, 5)

    assert "does not exist" in error_text(result)


async def test_negative_chunk_numbers_are_rejected(client, hub_with_model):
    token = (await show(client)).structured_content["token"]

    result = await fetch(client, token, -1)

    assert result.is_error


async def test_a_bad_token_is_rejected_and_nothing_is_fetched(client, respx_mock):
    result = await fetch(client, "not-a-real-token")

    assert "not valid any more" in error_text(result)
    assert "show_structure again" in error_text(result)
    assert not respx_mock.calls


async def test_a_token_cannot_be_forged_by_guessing(client, hub_with_model):
    real = (await show(client)).structured_content["token"]

    for guess in (
        real[:-1] + ("A" if real[-1] != "A" else "B"),
        real.upper(),
        real + "x",
        "",
    ):
        assert (await fetch(client, guess)).is_error


async def test_a_file_over_the_limit_is_refused_with_the_url(
    client, hub_with_model, monkeypatch
):
    monkeypatch.setenv(viewer.MAX_FILE_ENV, "0.00001")  # about 10 bytes
    token = (await show(client)).structured_content["token"]

    result = await fetch(client, token)

    text = error_text(result)
    assert "limit" in text
    assert FILE_URL in text
    assert "Download it" in text


async def test_the_default_limit_is_eight_megabytes():
    assert viewer.max_file_bytes() == 8 * 1048576


@pytest.mark.parametrize(
    ("value", "expected_mb"), [("20", 20), ("0.5", 0.5), ("  3 ", 3)]
)
def test_the_limit_can_be_configured(monkeypatch, value, expected_mb):
    monkeypatch.setenv(viewer.MAX_FILE_ENV, value)

    assert viewer.max_file_bytes() == int(expected_mb * 1048576)


@pytest.mark.parametrize("value", ["abc", "0", "-5", "nan", ""])
def test_an_invalid_limit_falls_back_to_the_default(monkeypatch, caplog, value):
    monkeypatch.setenv(viewer.MAX_FILE_ENV, value)

    assert viewer.max_file_bytes() == 8 * 1048576


async def test_an_ensemble_sample_is_served_as_pdb_and_labelled_a_sample(
    client, respx_mock
):
    mock_hub(
        respx_mock,
        [
            model_item(
                "ens1",
                url=FILE_URL,
                category="CONFORMATIONAL ENSEMBLE",
                number_of_conformers=576.0,
                start=73,
                end=83,
            )
        ],
        length=140,
    )
    respx_mock.get(FILE_URL).respond(
        200, content=PDB_SAMPLE
    )  # the Hub label says mmCIF; the bytes say PDB
    token = (await show(client, "ens1")).structured_content["token"]

    data = (await fetch(client, token)).structured_content

    assert data["format"] == "pdb"  # sniffed, not the Hub's label
    assert data["is_sample"] is True
    assert data["conformers"] == 576
    assert unpack([data["data"]]) == PDB_SAMPLE


async def test_a_page_that_is_not_a_structure_is_refused_with_the_url(
    client, respx_mock
):
    mock_hub(respx_mock, [model_item("m1", url=FILE_URL)])
    respx_mock.get(FILE_URL).respond(200, content=b"<html>404 not found</html>")
    token = (await show(client)).structured_content["token"]

    result = await fetch(client, token)

    text = error_text(result)
    assert "mmCIF and PDB" in text
    assert FILE_URL in text


async def test_a_gzip_bomb_is_refused(client, respx_mock, monkeypatch):
    monkeypatch.setenv(viewer.MAX_FILE_ENV, "1")
    mock_hub(respx_mock, [model_item("m1", url=FILE_URL)])
    respx_mock.get(FILE_URL).respond(
        200, content=gzip.compress(b"data_x\n" + b"A" * 20_000_000)
    )
    token = (await show(client)).structured_content["token"]

    result = await fetch(client, token)

    assert "limit once unpacked" in error_text(result)


@pytest.mark.parametrize(
    "failure",
    [httpx.ReadTimeout("slow"), httpx.ConnectError("down"), 503],
    ids=["timeout", "connection", "503"],
)
async def test_an_unavailable_provider_is_reported_with_the_url(
    client, respx_mock, failure
):
    mock_hub(respx_mock, [model_item("m1", url=FILE_URL)])
    route = respx_mock.get(FILE_URL)
    route.respond(failure) if isinstance(failure, int) else route.mock(
        side_effect=failure
    )
    token = (await show(client)).structured_content["token"]

    result = await fetch(client, token)

    assert "slow or unavailable" in error_text(result)
    assert FILE_URL in error_text(result)


async def test_a_failed_download_can_be_retried(client, respx_mock):
    mock_hub(respx_mock, [model_item("m1", url=FILE_URL)])
    route = respx_mock.get(FILE_URL).respond(503)
    token = (await show(client)).structured_content["token"]
    assert (await fetch(client, token)).is_error

    route.respond(200, content=MMCIF)  # the provider recovers

    assert not (await fetch(client, token)).is_error


# ---- never fetching somewhere unsafe, whatever the Hub says -----------------------


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://127.0.0.1:8080/steal",
        "http://localhost/x",
        "http://10.0.0.5/x",
        "http://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
    ],
)
async def test_a_hub_url_pointing_at_the_local_machine_is_never_fetched(
    client, respx_mock, bad_url
):
    mock_hub(respx_mock, [model_item("m1", url=bad_url)])
    attempted = respx_mock.get(url__regex=r".*").respond(200, content=b"secret")
    attempted.calls.clear()
    token = (await show(client)).structured_content["token"]

    result = await fetch(client, token)

    assert "not a public web address" in error_text(result)
    assert not [c for c in attempted.calls if str(c.request.url) == bad_url]


async def test_a_redirect_to_the_local_machine_is_never_followed(client, respx_mock):
    mock_hub(respx_mock, [model_item("m1", url=FILE_URL)])
    respx_mock.get(FILE_URL).respond(
        302, headers={"location": "http://127.0.0.1:8080/admin"}
    )
    local = respx_mock.get("http://127.0.0.1:8080/admin").respond(200, content=MMCIF)
    token = (await show(client)).structured_content["token"]

    result = await fetch(client, token)

    assert "not a public web address" in error_text(result)
    assert not local.called


# ---- the token store --------------------------------------------------------------


def make_model(model_id="m1"):
    return ModelSummary(
        provider="P", model_id=model_id, category="ab-initio", residues="1-10", span_percent=100.0,
        resolution_angstrom=None, experimental_method=None, confidence_type=None, confidence_score=None,
        model_url=FILE_URL, file_format="MMCIF", page_url=None, conformers=None, note=None,
    )  # fmt: skip


def test_tokens_are_unguessable_and_unique():
    store = viewer.ViewerStore()

    tokens = {store.issue(FILE_URL, make_model()) for _ in range(200)}

    assert len(tokens) == 200
    assert all(len(t) >= 20 for t in tokens)


def test_a_token_stops_working_after_its_lifetime():
    now = [1000.0]
    store = viewer.ViewerStore(ttl=60, clock=lambda: now[0])
    token = store.issue(FILE_URL, make_model())

    now[0] += 59
    assert store.get(token).url == FILE_URL
    now[0] += 2
    with pytest.raises(Exception, match="not valid any more"):
        store.get(token)


def test_the_store_does_not_grow_without_limit():
    store = viewer.ViewerStore(max_entries=3)

    tokens = [store.issue(FILE_URL, make_model(f"m{n}")) for n in range(5)]

    with pytest.raises(Exception, match="not valid any more"):
        store.get(tokens[0])  # the oldest were dropped
    assert store.get(tokens[-1]).model.model_id == "m4"


def test_expired_tokens_are_cleared_when_new_ones_are_issued():
    now = [0.0]
    store = viewer.ViewerStore(ttl=10, clock=lambda: now[0])
    old = store.issue(FILE_URL, make_model())

    now[0] = 100
    store.issue(FILE_URL, make_model("m2"))

    assert old not in store._entries
