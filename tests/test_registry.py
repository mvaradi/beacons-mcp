import json

import pytest

from beacons_mcp.registry import MAX_DESCRIPTION_CHARS, describe, first_sentence


def test_hub_display_names_resolve_to_registry_descriptions(load_fixture):
    # Every provider string seen in the captured Hub responses must resolve, even
    # though the Hub's display names differ from the registry's provider names.
    seen = {
        item["summary"]["provider"]
        for name in ("P69905", "Q92793", "P37840")
        for item in load_fixture(f"hub_summary_{name}.json")["structures"]
    }
    assert len(seen) >= 8
    missing = {name for name in seen if describe(name) is None}
    assert not missing


def test_longest_id_wins_and_unknown_providers_have_no_description():
    assert describe("Some Brand New Provider") is None
    assert describe("") is None


def test_matching_ignores_case_and_punctuation(load_fixture):
    providers = load_fixture("registry_providers.json")["providers"]
    for provider in providers:
        pid = provider["providerId"]
        assert describe(pid.upper()) == describe(pid)
        assert describe(f"{pid}-x.y") == describe(pid)  # id plus a suffix still matches


def test_snapshot_has_the_fields_we_read(load_fixture):
    providers = load_fixture("registry_providers.json")["providers"]
    assert providers
    for provider in providers:
        assert provider["providerId"]
        assert provider["providerDescription"]


def test_bundled_snapshot_is_readable_text():
    """Guards against the encoding damage that once turned apostrophes into mojibake."""
    from importlib import resources

    text = (
        resources.files("beacons_mcp") / "data" / "registry_providers.json"
    ).read_text(encoding="utf-8")
    assert "â€" not in text
    assert json.loads(text)["providers"]


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
