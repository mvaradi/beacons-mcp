import asyncio
import json
from importlib import resources

import httpx
import pytest

from beacons_mcp.clients import REGISTRY_URL
from beacons_mcp.registry import (
    FETCH_TIMEOUT,
    MAX_DESCRIPTION_CHARS,
    Registry,
    describe,
    first_sentence,
    parse,
    snapshot,
)

# ---- matching and the bundled snapshot ------------------------------------------


def test_hub_display_names_resolve_to_registry_descriptions(load_fixture):
    # Every provider string seen in the captured Hub responses must resolve, even
    # though the Hub's display names differ from the registry's provider names.
    seen = {
        item["summary"]["provider"]
        for name in ("P69905", "Q92793", "P37840")
        for item in load_fixture(f"hub_summary_{name}.json")["structures"]
    }
    assert len(seen) >= 8
    descriptions = snapshot()
    assert {name for name in seen if describe(name, descriptions) is None} == set()


def test_unknown_providers_have_no_description():
    assert describe("Some Brand New Provider", snapshot()) is None
    assert describe("", snapshot()) is None


def test_matching_ignores_case_and_punctuation_and_prefers_the_longest_id():
    descriptions = {"alpha": "Short id.", "alphafold": "Long id."}

    assert describe("ALPHA", descriptions) == "Short id."
    assert describe("AlphaFold DB", descriptions) == "Long id."
    assert describe("alpha-fold.x", descriptions) == "Long id."
    assert describe("Alph", descriptions) is None


def test_bundled_snapshot_is_readable_text_and_parses():
    """Guards against the encoding damage that once turned apostrophes into mojibake."""
    text = (
        resources.files("beacons_mcp") / "data" / "registry_providers.json"
    ).read_text(encoding="utf-8")
    assert "â€" not in text
    assert parse(json.loads(text)) == snapshot()
    assert len(snapshot()) >= 10


def test_parse_skips_incomplete_entries_and_rejects_an_empty_registry():
    document = {
        "providers": [
            {"providerId": "ok", "providerDescription": "Fine."},
            {"providerId": "", "providerDescription": "No id."},
            {"providerId": "nodesc"},
        ]
    }
    assert parse(document) == {"ok": "Fine."}
    with pytest.raises(ValueError):
        parse({"providers": []})
    with pytest.raises(KeyError):
        parse({"unexpected": "shape"})


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("One sentence. And another one.", "One sentence."),
        ("No full stop at all", "No full stop at all"),
        ("Uses the program (DeepView). Then more.", "Uses the program (DeepView)."),
        ("Line one\n  wrapped.  Next.", "Line one wrapped."),
        ("Version 2.5 is fine. Done.", "Version 2.5 is fine."),
    ],
)
def test_first_sentence(text, expected):
    assert first_sentence(text) == expected


def test_first_sentence_is_cut_to_the_limit():
    cut = first_sentence("word " * 200)
    assert len(cut) <= MAX_DESCRIPTION_CHARS
    assert cut.endswith("…")


# ---- the live registry, its cache and its fallback -------------------------------

LIVE = {"providers": [{"providerId": "newbeacon", "providerDescription": "Live only."}]}


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

    descriptions = await Registry(http).descriptions()

    assert descriptions == {"newbeacon": "Live only."}
    assert describe("NewBeacon", descriptions) == "Live only."


@pytest.mark.anyio
async def test_fetch_uses_a_short_timeout(http, respx_mock):
    route = respx_mock.get(REGISTRY_URL).respond(200, json=LIVE)

    await Registry(http).descriptions()

    assert (
        route.calls.last.request.extensions["timeout"]["read"]
        == FETCH_TIMEOUT.read
        == 5.0
    )


@pytest.mark.anyio
async def test_fetched_once_within_the_ttl_and_again_after_it(http, respx_mock, clock):
    route = respx_mock.get(REGISTRY_URL).respond(200, json=LIVE)
    registry = Registry(http, ttl=100, clock=clock)

    await registry.descriptions()
    clock.now += 99
    await registry.descriptions()
    assert route.call_count == 1

    clock.now += 2  # past the ttl
    await registry.descriptions()
    assert route.call_count == 2


@pytest.mark.anyio
async def test_concurrent_callers_share_one_fetch(http, respx_mock):
    route = respx_mock.get(REGISTRY_URL).respond(200, json=LIVE)
    registry = Registry(http)

    results = await asyncio.gather(*(registry.descriptions() for _ in range(5)))

    assert route.call_count == 1
    assert all(r == {"newbeacon": "Live only."} for r in results)


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

    assert await Registry(http).descriptions() == snapshot()


@pytest.mark.anyio
async def test_failure_is_not_retried_until_the_retry_window_has_passed(
    http, respx_mock, clock
):
    route = respx_mock.get(REGISTRY_URL).respond(500)
    registry = Registry(http, retry_after=60, clock=clock)

    assert await registry.descriptions() == snapshot()
    clock.now += 59
    assert await registry.descriptions() == snapshot()
    assert route.call_count == 1  # no hammering a service that is down

    route.respond(200, json=LIVE)  # it recovers
    clock.now += 2
    assert await registry.descriptions() == {"newbeacon": "Live only."}
    assert route.call_count == 2


@pytest.mark.anyio
async def test_a_stale_live_copy_beats_the_snapshot_when_a_refresh_fails(
    http, respx_mock, clock
):
    route = respx_mock.get(REGISTRY_URL).respond(200, json=LIVE)
    registry = Registry(http, ttl=100, retry_after=10, clock=clock)
    await registry.descriptions()

    route.respond(503)
    clock.now += 101  # expired, and the refresh fails

    assert await registry.descriptions() == {"newbeacon": "Live only."}
    assert route.call_count == 2
