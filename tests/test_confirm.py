import logging
from types import SimpleNamespace

import pytest
from mcp.server.elicitation import (
    AcceptedElicitation,
    CancelledElicitation,
    DeclinedElicitation,
)

from beacons_mcp.confirm import (
    DISMISSED_NOTE,
    MAX_CHOICES,
    MAX_NAME_CHARS,
    choices,
    confirm_candidate,
    label,
)
from beacons_mcp.models import ProteinCandidate, ProteinSearchResult
from beacons_mcp.uniprot import parse_candidate


class FakeSession:
    def __init__(self, supports):
        self.supports = supports
        self.capability_checks = 0

    def check_client_capability(self, capability):
        self.capability_checks += 1
        return self.supports


class FakeContext:
    """Just enough of the tool Context for the picker: capability check and elicit()."""

    def __init__(self, *, supports=True, reply=None, error=None):
        self.session = FakeSession(supports)
        self.request_context = SimpleNamespace(session=self.session)
        self._reply, self._error = reply, error
        self.asked = []

    async def elicit(self, message, schema):
        self.asked.append((message, schema))
        if self._error:
            raise self._error
        return self._reply(schema)


def candidate(accession, name="Some protein", *, reviewed=True):
    return ProteinCandidate(
        accession=accession,
        entry_name=f"{accession}_HUMAN",
        protein_name=name,
        gene_names=[],
        organism="Homo sapiens (Human)",
        reviewed=reviewed,
    )


def search_result(candidates, query="hemoglobin", organism="human"):
    return ProteinSearchResult(
        query=query,
        organism=organism,
        candidates=candidates,
        total_matches=len(candidates),
        note="Plain chat-confirmation note.",
    )


@pytest.fixture
def hemoglobin(load_fixture):
    """Five reviewed human hemoglobin entries, as UniProt returned them."""
    items = load_fixture("uniprot_search_hemoglobin_human.json")["results"]
    return search_result([parse_candidate(item) for item in items])


def pick(accession):
    def reply(schema):
        options = schema.model_json_schema()["properties"]["protein"]["enum"]
        chosen = next(o for o in options if o.startswith(accession))
        return AcceptedElicitation(data=schema(protein=chosen))

    return reply


# ---- when to ask ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flags", "offered"),
    [
        ([True], 0),  # one reviewed entry: nothing to choose
        ([False], 0),  # one unreviewed entry: nothing to choose
        ([True, False, False], 0),  # one reviewed among unreviewed fragments
        ([True, True], 2),
        ([True, True, False], 2),  # only the reviewed ones are offered
        ([False, False], 2),  # no reviewed entry: offer what there is
        ([True] * 9, MAX_CHOICES),  # a long list is capped
        ([False] * 9, MAX_CHOICES),
        ([], 0),
    ],
)
def test_choices_only_when_the_search_is_ambiguous(flags, offered):
    candidates = [candidate(f"P{n:05d}", reviewed=r) for n, r in enumerate(flags)]

    options = choices(candidates)

    assert len(options) == offered
    if len(options) > 1 and any(flags):
        assert all(c.reviewed for c in options)


def test_label_has_accession_name_and_entry_name():
    assert label(candidate("P69905", "Hemoglobin subunit alpha")) == (
        "P69905 - Hemoglobin subunit alpha (P69905_HUMAN)"
    )


def test_label_flags_unreviewed_entries_and_shortens_long_names():
    unreviewed = label(candidate("A0A000", "Fragment", reviewed=False))
    assert unreviewed.endswith("- unreviewed")

    long = label(candidate("P11111", "x" * 200))
    assert len(long) < MAX_NAME_CHARS + 40
    assert long.count("...") == 1

    assert "unnamed protein" in label(candidate("P22222", None))


def test_labels_of_real_candidates_are_unique(hemoglobin):
    labels = [label(c) for c in hemoglobin.candidates]
    assert len(set(labels)) == len(labels) == 5


# ---- the picker -------------------------------------------------------------------


@pytest.mark.anyio
async def test_user_pick_becomes_the_only_confirmed_candidate(hemoglobin):
    ctx = FakeContext(reply=pick("P69905"))

    result = await confirm_candidate(ctx, hemoglobin)

    assert result.confirmed_by_user is True
    assert [c.accession for c in result.candidates] == ["P69905"]
    assert result.candidates[0].entry_name == "HBA_HUMAN"
    assert "P69905" in result.note
    assert "do not ask again" in result.note
    # What the user was shown: the question and the five labelled options.
    message, schema = ctx.asked[0]
    assert "hemoglobin" in message
    assert "human" in message
    options = schema.model_json_schema()["properties"]["protein"]["enum"]
    assert options == [label(c) for c in hemoglobin.candidates]


@pytest.mark.anyio
@pytest.mark.parametrize("answer", [DeclinedElicitation(), CancelledElicitation()])
async def test_dismissing_the_picker_falls_back_to_asking_in_chat(hemoglobin, answer):
    ctx = FakeContext(reply=lambda schema: answer)

    result = await confirm_candidate(ctx, hemoglobin)

    assert result.confirmed_by_user is False
    assert (
        result.candidates == hemoglobin.candidates
    )  # nothing was decided for the user
    assert result.note == DISMISSED_NOTE
    assert "Ask them in chat" in result.note


@pytest.mark.anyio
async def test_client_without_elicitation_gets_the_plain_result(hemoglobin):
    ctx = FakeContext(supports=False, reply=pick("P69905"))

    result = await confirm_candidate(ctx, hemoglobin)

    assert result == hemoglobin  # unchanged, the original chat-confirmation note
    assert ctx.asked == []


@pytest.mark.anyio
async def test_a_failing_picker_falls_back_to_chat_and_is_logged(hemoglobin, caplog):
    ctx = FakeContext(error=RuntimeError("no back-channel"))

    with caplog.at_level(logging.WARNING, logger="beacons_mcp"):
        result = await confirm_candidate(ctx, hemoglobin)

    assert result == hemoglobin
    assert "elicitation failed" in caplog.text


@pytest.mark.anyio
async def test_unambiguous_results_never_ask_or_even_check_the_client(load_fixture):
    items = load_fixture("uniprot_search_cbp1_human_gene.json")["results"]
    single = search_result([parse_candidate(items[0])], query="CBP1")
    ctx = FakeContext(reply=pick("P50454"))

    result = await confirm_candidate(ctx, single)

    assert result == single
    assert ctx.asked == []
    assert ctx.session.capability_checks == 0


@pytest.mark.anyio
async def test_unreviewed_fragments_are_not_offered_next_to_reviewed_entries():
    mixed = search_result(
        [
            candidate("P00001", "Alpha"),
            candidate("P00002", "Beta"),
            candidate("A0A111", "Fragment", reviewed=False),
        ]
    )
    ctx = FakeContext(reply=pick("P00002"))

    result = await confirm_candidate(ctx, mixed)

    _, schema = ctx.asked[0]
    options = schema.model_json_schema()["properties"]["protein"]["enum"]
    assert [o.split(" ")[0] for o in options] == ["P00001", "P00002"]
    assert result.candidates[0].accession == "P00002"


def test_labels_are_plain_ascii(hemoglobin):
    assert all(label(c).isascii() for c in hemoglobin.candidates)
    assert label(candidate("P33333", "Protein with a long name " * 10)).isascii()


@pytest.mark.anyio
async def test_dialog_says_when_it_shows_only_some_of_the_matches(hemoglobin):
    many = hemoglobin.model_copy(update={"total_matches": 15})
    ctx = FakeContext(reply=pick("P69905"))

    await confirm_candidate(ctx, many)

    message, _ = ctx.asked[0]
    assert "Showing 5 of 15 matches" in message
    assert "decline" in message


@pytest.mark.anyio
async def test_dialog_does_not_claim_a_partial_list_when_it_shows_every_match(
    hemoglobin,
):
    complete = hemoglobin.model_copy(update={"total_matches": 5})
    ctx = FakeContext(reply=pick("P69905"))

    await confirm_candidate(ctx, complete)

    message, _ = ctx.asked[0]
    assert "Showing" not in message
