"""Provider descriptions, read live from the 3D-Beacons registry.

The registry (``resources/registry.json`` in the 3D-Beacons/3d-beacons-registry
repository) is where 3D-Beacons publishes its providers and what they are. It is
fetched once per server run and refreshed after ``TTL_SECONDS``. Descriptions are
optional enrichment, so the fetch is short and can never fail a tool call: if it
fails, the last good copy is used, or the bundled snapshot
(``data/registry_providers.json``) if there never was one. The snapshot is only a
fallback and goes stale; refresh it by copying a newer ``providers`` list (keeping
providerId, providerName, providerDescription and providerUrl).

Provider names are data, not code: nothing in this package names a provider. The Hub
names providers with short display strings that often differ from the registry's
``providerName`` (a display string can be an id plus a suffix), so matching is by
normalised registry ``providerId``: a display string matches the longest id it equals
or starts with. A provider the registry does not know simply has no description.
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

import httpx

from beacons_mcp.clients import REGISTRY_URL

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


@cache
def snapshot() -> dict[str, str]:
    """The bundled copy of the registry, used only when the live one is unavailable."""
    text = (
        resources.files("beacons_mcp") / "data" / "registry_providers.json"
    ).read_text(encoding="utf-8")
    return parse(json.loads(text))


class Registry:
    """Live provider descriptions, cached for the life of the server."""

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
        self._descriptions: dict[str, str] | None = None
        self._expires = 0.0

    async def descriptions(self) -> dict[str, str]:
        """Normalised provider id -> description. Never raises."""
        if self._descriptions is not None and self._clock() < self._expires:
            return self._descriptions
        async with self._lock:
            if self._descriptions is not None and self._clock() < self._expires:
                return self._descriptions
            try:
                response = await self._client.get(REGISTRY_URL, timeout=FETCH_TIMEOUT)
                response.raise_for_status()
                self._descriptions = parse(response.json())
                self._expires = self._clock() + self._ttl
                logger.info("registry refreshed: %d providers", len(self._descriptions))
            except Exception as exc:  # noqa: BLE001 - optional enrichment must never break a tool
                fallback = (
                    "last good copy" if self._descriptions else "bundled snapshot"
                )
                logger.warning("registry unavailable (%r); using the %s", exc, fallback)
                self._descriptions = self._descriptions or snapshot()
                self._expires = self._clock() + self._retry_after
            return self._descriptions


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
