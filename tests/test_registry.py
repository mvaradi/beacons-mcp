import asyncio
import json
from importlib import resources

import httpx
import pytest

from beacons_mcp.clients import REGISTRY_URL
from beacons_mcp.registry import (
    FETCH_TIMEOUT,
    Registry,
    caveat,
    parse_origins,
    snapshot_origins,
)

# ---- the bundled snapshot and the provider caveats ------------------------------


def test_bundled_snapshot_is_readable_text_and_has_origins():
    """Guards against the encoding damage that once turned apostrophes into mojibake."""
    text = (
        resources.files("beacons_mcp") / "data" / "registry_providers.json"
    ).read_text(encoding="utf-8")
    assert "â€" not in text
    assert json.loads(text)["providers"]
    assert len(snapshot_origins()) >= 10


def test_caveats_match_the_whole_name_ignoring_case_and_punctuation():
    assert "esigned" in caveat("Bindome")
    assert caveat("BINDOME") == caveat("bind-ome")
    assert caveat("AlphaFold DB") is None
    assert caveat("Some Brand New Provider") is None
    assert caveat("") is None


# ---- the live registry, its cache and its fallback -------------------------------

LIVE = {"providers": [{"providerId": "newbeacon", "baseServiceUrl": "https://files.newbeacon.example.org/api/"}]}
LIVE_ORIGINS = ["https://files.newbeacon.example.org"]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as client:
        yield client


@pytest.mark.anyio
async def test_live_registry_is_used_and_a_new_beacon_is_found(http, respx_mock):
    respx_mock.get(REGISTRY_URL).respond(200, json=LIVE)

    assert await Registry(http).origins() == LIVE_ORIGINS


@pytest.mark.anyio
async def test_fetch_uses_a_short_timeout(http, respx_mock):
    route = respx_mock.get(REGISTRY_URL).respond(200, json=LIVE)

    await Registry(http).origins()

    assert (
        route.calls.last.request.extensions["timeout"]["read"]
        == FETCH_TIMEOUT.read
        == 5.0
    )


@pytest.mark.anyio
async def test_fetched_once_within_the_ttl_and_again_after_it(http, respx_mock, clock):
    route = respx_mock.get(REGISTRY_URL).respond(200, json=LIVE)
    registry = Registry(http, ttl=100, clock=clock)

    await registry.origins()
    clock.now += 99
    await registry.origins()
    assert route.call_count == 1

    clock.now += 2  # past the ttl
    await registry.origins()
    assert route.call_count == 2


@pytest.mark.anyio
async def test_concurrent_callers_share_one_fetch(http, respx_mock):
    route = respx_mock.get(REGISTRY_URL).respond(200, json=LIVE)
    registry = Registry(http)

    results = await asyncio.gather(*(registry.origins() for _ in range(5)))

    assert route.call_count == 1
    assert all(r == LIVE_ORIGINS for r in results)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("down"),
        404,
        500,
        "not json",
        "{}",
        "[]",
    ],
    ids=[
        "timeout",
        "connection",
        "404",
        "500",
        "not-json",
        "no-providers-key",
        "wrong-shape",
    ],
)
async def test_snapshot_is_the_fallback_when_there_never_was_a_live_copy(
    http, respx_mock, failure
):
    route = respx_mock.get(REGISTRY_URL)
    if isinstance(failure, int):
        route.respond(failure)
    elif isinstance(failure, str):
        route.respond(200, text=failure)
    else:
        route.mock(side_effect=failure)

    assert await Registry(http).origins() == snapshot_origins()


@pytest.mark.anyio
async def test_failure_is_not_retried_until_the_retry_window_has_passed(
    http, respx_mock, clock
):
    route = respx_mock.get(REGISTRY_URL).respond(500)
    registry = Registry(http, retry_after=60, clock=clock)

    assert await registry.origins() == snapshot_origins()
    clock.now += 59
    assert await registry.origins() == snapshot_origins()
    assert route.call_count == 1  # no hammering a service that is down

    route.respond(200, json=LIVE)  # it recovers
    clock.now += 2
    assert await registry.origins() == LIVE_ORIGINS
    assert route.call_count == 2


@pytest.mark.anyio
async def test_a_stale_live_copy_beats_the_snapshot_when_a_refresh_fails(
    http, respx_mock, clock
):
    route = respx_mock.get(REGISTRY_URL).respond(200, json=LIVE)
    registry = Registry(http, ttl=100, retry_after=10, clock=clock)
    await registry.origins()

    route.respond(503)
    clock.now += 101  # expired, and the refresh fails

    assert await registry.origins() == LIVE_ORIGINS
    assert route.call_count == 2


# ---- origins (where the viewer may load model files from) ---------------------------

def test_origins_are_the_https_service_hosts_sorted_and_unique(load_fixture):
    registry = load_fixture("registry_providers.json")

    origins = parse_origins(registry)

    assert origins == sorted(set(origins))
    assert len(origins) == 12  # 13 providers; one is plain http and is left out
    assert all(o.startswith("https://") for o in origins)
    assert "http://genome3d.eu" not in origins and "https://genome3d.eu" not in origins
    assert snapshot_origins() == origins


@pytest.mark.parametrize(
    "url",
    [
        "http://files.example.org/",  # not https
        "ftp://files.example.org/",
        "https://localhost/",
        "https://app.localhost/",
        "https://printer.local/",
        "https://db.internal/",
        "https://127.0.0.1/",
        "https://10.0.0.5/",
        "https://[::1]/",
        "not a url",
        "",
        None,
        42,
    ],
)
def test_origins_that_are_not_public_https_hosts_are_left_out(url):
    document = {
        "providers": [
            {"baseServiceUrl": url},
            {"baseServiceUrl": "https://ok.example.org/x"},
        ]
    }

    assert parse_origins(document) == ["https://ok.example.org"]


def test_origins_keep_a_port_but_not_the_default_one_and_drop_paths():
    document = {
        "providers": [
            {"baseServiceUrl": "https://a.example.org:8443/v1/"},
            {"baseServiceUrl": "https://B.Example.org:443/api"},
            {"baseServiceUrl": "https://b.example.org/other"},
        ]
    }

    assert parse_origins(document) == [
        "https://a.example.org:8443",
        "https://b.example.org",
    ]


def test_a_registry_with_no_usable_addresses_is_an_error():
    with pytest.raises(ValueError):
        parse_origins(
            {"providers": [{"baseServiceUrl": "http://only-http.example.org/"}, {}]}
        )


LIVE_BOTH = {
    "providers": [
        {
            "providerId": "newbeacon",
            "baseServiceUrl": "https://files.newbeacon.example.org/api/",
        }
    ]
}


@pytest.mark.anyio
async def test_live_origins_are_used(http, respx_mock):
    respx_mock.get(REGISTRY_URL).respond(200, json=LIVE_BOTH)

    assert await Registry(http).origins() == ["https://files.newbeacon.example.org"]


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [httpx.ConnectError("down"), 500, "not json", "{}"])
async def test_origins_fall_back_to_the_snapshot_when_the_registry_fails(
    http, respx_mock, failure
):
    route = respx_mock.get(REGISTRY_URL)
    if isinstance(failure, int):
        route.respond(failure)
    elif isinstance(failure, str):
        route.respond(200, text=failure)
    else:
        route.mock(side_effect=failure)

    assert await Registry(http).origins() == snapshot_origins()
