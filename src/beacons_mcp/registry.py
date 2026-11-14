"""Provider descriptions and file origins, read live from the 3D-Beacons registry.

The registry (``resources/registry.json`` in the 3D-Beacons/3d-beacons-registry
repository) is where 3D-Beacons publishes its providers: what they are, and the web
address of each one's service. It is fetched once per server run and refreshed after
``TTL_SECONDS``. Both uses are optional enrichment, so the fetch is short and can never
fail a tool call: if it fails, the last good copy is used, or the bundled snapshot
(``data/registry_providers.json``) if there never was one. The snapshot is only a
fallback and goes stale; refresh it by copying a newer ``providers`` list (keeping
providerId, providerName, providerDescription, providerUrl and baseServiceUrl).

Two things come from it:

* **Descriptions** of the providers in a result. The Hub names providers with short
  display strings that often differ from the registry's ``providerName`` (a display
  string can be an id plus a suffix), so matching is by normalised registry
  ``providerId``: a display string matches the longest id it equals or starts with. A
  provider the registry does not know simply has no description.
* **Origins** the 3D viewer is allowed to load model files from (its CSP). A provider
  serves its model files from the same host as its service, so the service hosts are the
  list. Only https origins with public host names are kept.

Provider names and addresses are data, not code: nothing in this package names one.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from functools import cache
from importlib import resources
from typing import Any
from urllib.parse import urlsplit

import httpx

from beacons_mcp.clients import REGISTRY_URL
from beacons_mcp.safe_fetch import is_public_hostname

logger = logging.getLogger("beacons_mcp")

MAX_DESCRIPTION_CHARS = 220
TTL_SECONDS = 6 * 60 * 60
RETRY_AFTER_FAILURE_SECONDS = 5 * 60
FETCH_TIMEOUT = httpx.Timeout(5.0)


def _normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def parse(registry: Mapping[str, Any]) -> dict[str, str]:
    """Map normalised provider id -> description from a registry document."""
    found = {
        _normalise(p["providerId"]): p["providerDescription"]
        for p in registry["providers"]
        if p.get("providerId") and p.get("providerDescription")
    }
    if not found:
        raise ValueError("registry lists no providers with descriptions")
    return found


def _origin(url: object) -> str | None:
    """``https://host[:port]`` for a public https address, else None."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        host, port = (parts.hostname or "").lower(), parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or not is_public_hostname(host):
        return None
    return f"https://{host}" + (f":{port}" if port not in (None, 443) else "")


def parse_origins(registry: Mapping[str, Any]) -> list[str]:
    """The https origins of the providers' services, sorted and without duplicates."""
    found = {_origin(p.get("baseServiceUrl")) for p in registry["providers"]} - {None}
    if not found:
        raise ValueError("registry lists no usable service addresses")
    return sorted(found)  # type: ignore[type-var]


@cache
def _snapshot_document() -> dict[str, Any]:
    text = (
        resources.files("beacons_mcp") / "data" / "registry_providers.json"
    ).read_text(encoding="utf-8")
    return json.loads(text)


@cache
def snapshot() -> dict[str, str]:
    """Descriptions from the bundled copy, used only when the live registry is unavailable."""
    return parse(_snapshot_document())


@cache
def snapshot_origins() -> list[str]:
    """Origins from the bundled copy, used only when the live registry is unavailable."""
    return parse_origins(_snapshot_document())


class Registry:
    """Live provider descriptions and origins, cached for the life of the server."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        ttl: float = TTL_SECONDS,
        retry_after: float = RETRY_AFTER_FAILURE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._ttl = ttl
        self._retry_after = retry_after
        self._clock = clock
        self._lock = asyncio.Lock()
        self._current: tuple[dict[str, str], list[str]] | None = None
        self._expires = 0.0

    async def _load(self) -> tuple[dict[str, str], list[str]]:
        if self._current is not None and self._clock() < self._expires:
            return self._current
        async with self._lock:
            if self._current is not None and self._clock() < self._expires:
                return self._current
            try:
                response = await self._client.get(REGISTRY_URL, timeout=FETCH_TIMEOUT)
                response.raise_for_status()
                document = response.json()
                descriptions = parse(document)
                try:
                    origins = parse_origins(document)
                except (ValueError, KeyError, TypeError) as exc:
                    # Descriptions are fine but the addresses are not: keep what we had.
                    logger.warning("registry has no usable addresses (%r)", exc)
                    origins = self._current[1] if self._current else snapshot_origins()
                self._current = (descriptions, origins)
                self._expires = self._clock() + self._ttl
                logger.info("registry refreshed: %d providers", len(self._current[0]))
            except Exception as exc:  # noqa: BLE001 - optional enrichment must never break a tool
                fallback = "last good copy" if self._current else "bundled snapshot"
                logger.warning("registry unavailable (%r); using the %s", exc, fallback)
                self._current = self._current or (snapshot(), snapshot_origins())
                self._expires = self._clock() + self._retry_after
            return self._current

    async def descriptions(self) -> dict[str, str]:
        """Normalised provider id -> description. Never raises."""
        return (await self._load())[0]

    async def origins(self) -> list[str]:
        """https origins of the providers' services. Never raises."""
        return (await self._load())[1]


def first_sentence(text: str, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    """The first sentence of ``text``, cut to ``limit`` characters."""
    text = " ".join(text.split())
    match = re.search(r"(?<=[a-z0-9\)])\.\s", text)
    sentence = text[: match.start() + 1] if match else text
    if len(sentence) > limit:
        sentence = sentence[: limit - 1].rstrip() + "…"
    return sentence


def describe(provider: str, descriptions: Mapping[str, str]) -> str | None:
    """A one-sentence description of ``provider`` (a name as the Hub reports it)."""
    wanted = _normalise(provider)
    matches = [pid for pid in descriptions if wanted.startswith(pid)]
    if not matches:
        return None
    return first_sentence(descriptions[max(matches, key=len)])
