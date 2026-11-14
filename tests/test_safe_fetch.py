from typing import ClassVar

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from beacons_mcp import safe_fetch
from beacons_mcp.safe_fetch import check_public_url, fetch_limited

pytestmark = [pytest.mark.anyio, pytest.mark.respx(assert_all_called=False)]

PUBLIC = "https://files.example.org/model.cif"
BODY = b"data_test\n_x 1\n"


@pytest.fixture
def dns(monkeypatch):
    """Control name resolution: tests set ``dns.answers`` per host name."""

    class Dns:
        answers: ClassVar[dict[str, list[str]]] = {}

    async def resolve(host):
        return Dns.answers.get(host, ["93.184.216.34"])  # a public address by default

    monkeypatch.setattr(safe_fetch, "_resolve", resolve)
    return Dns


# ---- which URLs are acceptable ----------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://files.example.org/a.cif",
        "http://files.example.org:8080/a.cif",
        "https://93.184.216.34/a.cif",
        "https://[2606:2800:220:1:248:1893:25c8:1946]/a.cif",
    ],
)
async def test_public_urls_are_accepted(dns, url):
    await check_public_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://127.0.0.1:8000/x",
        "http://localhost/x",
        "http://LOCALHOST:3000/x",
        "http://app.localhost/x",
        "http://printer.local/x",
        "http://db.internal/x",
        "http://10.0.0.5/x",
        "http://172.16.3.4/x",
        "http://192.168.1.10/x",
        "http://169.254.169.254/latest/meta-data",  # cloud metadata address
        "http://0.0.0.0/x",
        "http://[::1]/x",
        "http://[fe80::1]/x",
        "http://[::ffff:127.0.0.1]/x",
        "file:///etc/passwd",
        "ftp://files.example.org/a.cif",
        "gopher://files.example.org/",
        "javascript:alert(1)",
        "not a url",
        "",
    ],
)
async def test_local_private_and_odd_urls_are_refused(dns, url):
    with pytest.raises(ToolError, match="not a public web address"):
        await check_public_url(url)


async def test_a_name_that_resolves_to_a_private_address_is_refused(dns):
    dns.answers = {
        "sneaky.example.org": ["93.184.216.34", "10.1.2.3"]
    }  # one bad answer is enough

    with pytest.raises(ToolError, match="not a public web address"):
        await check_public_url("https://sneaky.example.org/a.cif")


async def test_a_name_that_does_not_resolve_says_what_to_do(monkeypatch):
    async def fail(host):
        raise OSError("no such host")

    monkeypatch.setattr(safe_fetch, "_resolve", fail)

    with pytest.raises(ToolError, match="Could not find the file's host"):
        await check_public_url("https://gone.example.org/a.cif")


# ---- fetching ---------------------------------------------------------------------


async def test_fetches_a_public_file(dns, respx_mock):
    respx_mock.get(PUBLIC).respond(200, content=BODY)

    async with httpx.AsyncClient() as client:
        assert await fetch_limited(client, PUBLIC, 1000) == BODY


async def test_follows_a_redirect_to_another_public_address(dns, respx_mock):
    respx_mock.get(PUBLIC).respond(
        302, headers={"location": "https://cdn.example.net/m.cif"}
    )
    respx_mock.get("https://cdn.example.net/m.cif").respond(200, content=BODY)

    async with httpx.AsyncClient() as client:
        assert await fetch_limited(client, PUBLIC, 1000) == BODY


async def test_follows_a_relative_redirect(dns, respx_mock):
    respx_mock.get(PUBLIC).respond(301, headers={"location": "/new/model.cif"})
    respx_mock.get("https://files.example.org/new/model.cif").respond(200, content=BODY)

    async with httpx.AsyncClient() as client:
        assert await fetch_limited(client, PUBLIC, 1000) == BODY


@pytest.mark.parametrize(
    "target",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://127.0.0.1:9/x",
        "http://localhost/x",
        "file:///etc/passwd",
    ],
)
async def test_a_redirect_to_a_private_address_is_refused(dns, respx_mock, target):
    respx_mock.get(PUBLIC).respond(302, headers={"location": target})
    private = respx_mock.get(url__regex=r"http://(169|127|localhost).*").respond(
        200, content=b"secret"
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(ToolError, match="not a public web address"):
            await fetch_limited(client, PUBLIC, 1000)

    assert not private.called  # it was never even requested


async def test_a_redirect_to_a_name_with_a_private_address_is_refused(dns, respx_mock):
    dns.answers = {"rebind.example.net": ["192.168.0.9"]}
    respx_mock.get(PUBLIC).respond(
        302, headers={"location": "https://rebind.example.net/m.cif"}
    )
    hit = respx_mock.get("https://rebind.example.net/m.cif").respond(
        200, content=b"secret"
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(ToolError, match="not a public web address"):
            await fetch_limited(client, PUBLIC, 1000)

    assert not hit.called


async def test_a_redirect_loop_gives_up(dns, respx_mock):
    respx_mock.get(PUBLIC).respond(302, headers={"location": PUBLIC})

    async with httpx.AsyncClient() as client:
        with pytest.raises(ToolError, match="redirected too many times"):
            await fetch_limited(client, PUBLIC, 1000)


async def test_a_redirect_without_a_location_is_a_provider_problem(dns, respx_mock):
    respx_mock.get(PUBLIC).respond(302)

    async with httpx.AsyncClient() as client:
        with pytest.raises(ToolError, match="slow or unavailable"):
            await fetch_limited(client, PUBLIC, 1000)


# ---- the size limit ---------------------------------------------------------------


async def test_a_file_over_the_limit_is_refused_with_the_url_and_a_way_out(
    dns, respx_mock
):
    respx_mock.get(PUBLIC).respond(200, content=b"x" * 5000)

    async with httpx.AsyncClient() as client:
        with pytest.raises(ToolError) as caught:
            await fetch_limited(client, PUBLIC, 1000)

    message = str(caught.value)
    assert "limit" in message
    assert PUBLIC in message  # where to download it instead
    assert "BEACONS_MCP_MAX_FILE_MB" in message


async def test_a_file_exactly_at_the_limit_is_accepted(dns, respx_mock):
    respx_mock.get(PUBLIC).respond(200, content=b"x" * 1000)

    async with httpx.AsyncClient() as client:
        assert len(await fetch_limited(client, PUBLIC, 1000)) == 1000


async def test_a_declared_length_over_the_limit_is_refused_without_reading_the_body(
    dns, respx_mock
):
    consumed = []

    async def body():
        consumed.append(True)
        yield b"x" * 10

    respx_mock.get(PUBLIC).respond(
        200, headers={"content-length": "999999"}, content=body()
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(ToolError, match="limit"):
            await fetch_limited(client, PUBLIC, 1000)

    assert consumed == []


async def test_reading_stops_as_soon_as_the_limit_is_passed(dns, respx_mock):
    produced = []

    async def endless():
        for _ in range(10_000):
            produced.append(1)
            yield b"x" * 100

    respx_mock.get(PUBLIC).respond(200, content=endless())

    async with httpx.AsyncClient() as client:
        with pytest.raises(ToolError, match="limit"):
            await fetch_limited(client, PUBLIC, 1000)

    assert len(produced) < 100  # nowhere near the whole stream


# ---- provider trouble -------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [httpx.ReadTimeout("slow"), httpx.ConnectError("down"), 500, 503],
    ids=["timeout", "connection-error", "http-500", "http-503"],
)
async def test_slow_or_unavailable_providers_say_so(dns, respx_mock, failure):
    route = respx_mock.get(PUBLIC)
    route.respond(failure) if isinstance(failure, int) else route.mock(
        side_effect=failure
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(ToolError) as caught:
            await fetch_limited(client, PUBLIC, 1000)

    assert "slow or unavailable" in str(caught.value)
    assert PUBLIC in str(caught.value)


async def test_other_http_errors_name_the_status_and_the_url(dns, respx_mock):
    respx_mock.get(PUBLIC).respond(404)

    async with httpx.AsyncClient() as client:
        with pytest.raises(ToolError) as caught:
            await fetch_limited(client, PUBLIC, 1000)

    assert "HTTP 404" in str(caught.value)
    assert PUBLIC in str(caught.value)
