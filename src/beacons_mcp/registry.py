"""Provider descriptions from a bundled snapshot of the 3D-Beacons registry.

Provider names are data, not code: nothing here (or anywhere else in the package)
names a provider. The snapshot is ``data/registry_providers.json``, taken from
``resources/registry.json`` in the 3D-Beacons/3d-beacons-registry repository.
Refresh it by copying a newer ``providers`` list (keeping providerId, providerName,
providerDescription and providerUrl).

The Hub names providers with short display strings that often differ from the
registry's ``providerName`` (a display string can be an id plus a suffix), so
matching is by normalised registry ``providerId``: a display string matches the
longest id it equals or starts with. A provider the snapshot does not know simply
has no description.
"""

import json
import re
from functools import cache
from importlib import resources

MAX_DESCRIPTION_CHARS = 220


def _normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


@cache
def _providers() -> dict[str, str]:
    """Map normalised provider id -> full description."""
    text = (
        resources.files("beacons_mcp") / "data" / "registry_providers.json"
    ).read_text(encoding="utf-8")
    return {
        _normalise(p["providerId"]): p["providerDescription"]
        for p in json.loads(text)["providers"]
    }


def first_sentence(text: str, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    """The first sentence of ``text``, cut to ``limit`` characters."""
    text = " ".join(text.split())
    match = re.search(r"(?<=[a-z0-9\)])\.\s", text)
    sentence = text[: match.start() + 1] if match else text
    if len(sentence) > limit:
        sentence = sentence[: limit - 1].rstrip() + "…"
    return sentence


def describe(provider: str) -> str | None:
    """A one-sentence description of ``provider`` (a name as the Hub reports it)."""
    wanted = _normalise(provider)
    matches = [pid for pid in _providers() if wanted.startswith(pid)]
    if not matches:
        return None
    return first_sentence(_providers()[max(matches, key=len)])
