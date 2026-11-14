"""Fetch a model file from a provider, safely.

The file URL comes from the 3D-Beacons Hub, which relays what each provider
publishes, and this server runs on the user's machine. So a URL is only fetched if
it points at the public internet: never at localhost or a private network, and the
same holds for every redirect. The download is also capped in size, counting bytes
after decompression, so a huge or compressed-bomb file cannot fill memory.

Known limit: the host name is resolved here to check it and again by the HTTP client
to connect, so a hostile DNS server could in principle answer differently the second
time. The checks stop the realistic cases (a provider publishing a private address,
a redirect to one), not a determined attacker who controls DNS.
"""

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urljoin, urlsplit

import httpx
from mcp.server.mcpserver.exceptions import ToolError

logger = logging.getLogger("beacons_mcp")

MAX_REDIRECTS = 5
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")


async def _resolve(host: str) -> list[str]:
    """The addresses a host name resolves to (a seam for tests)."""
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, None, type=socket.SOCK_STREAM
    )
    return [str(info[4][0]) for info in infos]


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def is_public_hostname(host: str) -> bool:
    """Whether ``host`` could be a public name: not localhost, not a local suffix, not an IP.

    A syntax check only (no DNS). Used where an address will be allowed to be contacted.
    """
    host = host.lower().strip(".")
    if not host or host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return False


async def check_public_url(url: str) -> None:
    """Raise a ToolError unless ``url`` is http(s) and resolves only to public addresses."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    refusal = ToolError(
        f"The model's file URL ({url}) is not a public web address, so the server did "
        "not fetch it. Open it yourself only if you trust the source."
    )
    if parts.scheme not in ("http", "https") or not host:
        raise refusal
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        raise refusal
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            addresses = await _resolve(host)
        except OSError as exc:
            logger.warning("could not resolve %s: %r", host, exc)
            raise ToolError(
                f"Could not find the file's host ({host}). The provider may be down; "
                f"try again later or download the file from {url}."
            ) from exc
    if not addresses or not all(_is_public(a) for a in addresses):
        logger.warning("refusing non-public address for %s", host)
        raise refusal


async def fetch_limited(client: httpx.AsyncClient, url: str, max_bytes: int) -> bytes:
    """GET ``url`` (following redirects only to public addresses), at most ``max_bytes``."""
    unavailable = ToolError(
        "The provider is slow or unavailable right now. Try again in a moment, or "
        f"download the file from {url}."
    )
    too_large = ToolError(
        f"The file is larger than the {max_bytes / 1048576:g} MB limit for the in-chat "
        f"viewer. Download it from {url} and open it in a desktop molecular viewer, or "
        "raise the limit with the BEACONS_MCP_MAX_FILE_MB environment variable."
    )
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        await check_public_url(current)
        try:
            async with client.stream(
                "GET", current, follow_redirects=False
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise unavailable
                    current = urljoin(current, location)
                    continue
                if response.status_code >= 500:
                    raise unavailable
                if response.status_code != 200:
                    raise ToolError(
                        f"The provider answered HTTP {response.status_code} for the file. "
                        f"Download it from {url} instead."
                    )
                declared = response.headers.get("content-length", "")
                # The declared length is the size on the wire; decoded it can only be
                # larger, so a declared length over the cap is already too big.
                if declared.isdigit() and int(declared) > max_bytes:
                    raise too_large
                body = bytearray()
                async for part in response.aiter_bytes():
                    body.extend(part)
                    if len(body) > max_bytes:
                        raise too_large
                return bytes(body)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            logger.warning("file fetch failed: %r", exc)
            raise unavailable from exc
    raise ToolError(
        f"The file URL redirected too many times. Download it from {url} instead."
    )
