import copy

import httpx
import pytest
from conftest import error_text

from beacons_mcp.clients import UNIPROT_SEARCH_URL, UNIPROT_TAXONOMY_URL, USER_AGENT


async def search(client, **arguments):
    return await client.call_tool("search_proteins", arguments)


def with_taxon(body, taxon_id):
    """The fixture with every entry in one taxon (the search is by taxon ID)."""
    body = copy.deepcopy(body)
    for item in body["results"]:
        item["organism"]["taxonId"] = taxon_id
    return body


def all_human(body):
    return with_taxon(body, 9606)


def mock_search(respx_mock, body, *, total=None, status=200):
    headers = {"x-total-results": str(total)} if total is not None else {}
    return respx_mock.get(UNIPROT_SEARCH_URL).respond(
        status, json=body, headers=headers
    )


@pytest.mark.anyio
async def test_name_in_organism_maps_to_ranked_candidate(
    client, respx_mock, load_fixture
):
    route = mock_search(
        respx_mock, load_fixture("uniprot_search_cbp1_human_gene.json"), total=1
    )

    result = await search(client, query="CBP1", organism="human")

    assert not result.is_error
    data = result.structured_content
    assert data["total_matches"] == 1
    assert data["candidates"] == [
        {
            "accession": "P50454",
            "entry_name": "SERPH_HUMAN",
            "protein_name": "Serpin H1",
            # "CBP1" is a synonym of the primary gene name: primary first, then synonyms.
            "gene_names": ["SERPINH1", "CBP1", "CBP2", "HSP47", "SERPINH2"],
            "organism": "Homo sapiens (Human)",
            "reviewed": True,
        }
    ]
    assert "P50454" in data["note"]

    sent = route.calls.last.request
    query = sent.url.params["query"]
    assert 'gene:"CBP1"' in query
    assert 'protein_name:"CBP1"' in query
    assert "organism_id:9606" in query
    assert "organism_name" not in query
    assert sent.url.params["format"] == "json"
    assert (
        sent.url.params["fields"]
        == "accession,id,protein_name,gene_names,organism_name,reviewed"
    )
    assert sent.headers["user-agent"] == USER_AGENT


@pytest.mark.anyio
async def test_numeric_organism_is_sent_as_taxon_id(client, respx_mock, load_fixture):
    route = mock_search(respx_mock, load_fixture("uniprot_search_cbp1_human_gene.json"))

    await search(client, query="CBP1", organism="9606")

    assert "organism_id:9606" in route.calls.last.request.url.params["query"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("name", "taxon"),
    [("human", 9606), ("Homo sapiens", 9606), ("Mouse", 10090), ("Mus musculus", 10090),
     ("Homo sapiens (Human)", 9606), ("mouse-ear cress", 3702)],
)
async def test_names_resolve_to_taxon_ids(client, respx_mock, load_fixture, name, taxon):
    route = mock_search(respx_mock, all_human(load_fixture("uniprot_search_cbp1_human_gene.json")))

    await search(client, query="CBP1", organism=name)

    assert f"organism_id:{taxon}" in route.calls.last.request.url.params["query"]


@pytest.mark.anyio
async def test_mouse_never_matches_mouse_ear_cress(client, respx_mock, load_fixture):
    """Regression: 'mouse' used to be sent as organism_name:"mouse", which also matches
    Arabidopsis ("Mouse-ear cress"). p53 + mouse must return Mus musculus only."""
    body = copy.deepcopy(load_fixture("uniprot_search_cbp1_any_organism.json"))
    mouse, plant = body["results"][0], copy.deepcopy(body["results"][0])
    mouse["organism"] = {"scientificName": "Mus musculus", "commonName": "Mouse", "taxonId": 10090}
    plant["primaryAccession"] = "Q42578"
    plant["organism"] = {
        "scientificName": "Arabidopsis thaliana", "commonName": "Mouse-ear cress", "taxonId": 3702,
    }
    body["results"] = [plant, mouse]  # even if UniProt returned the plant, it is dropped
    route = mock_search(respx_mock, body)

    result = await search(client, query="p53", organism="mouse")

    assert not result.is_error
    query = route.calls.last.request.url.params["query"]
    assert "organism_id:10090" in query and "organism_name" not in query
    organisms = [c["organism"] for c in result.structured_content["candidates"]]
    assert organisms == ["Mus musculus (Mouse)"]
    assert result.structured_content["organism"] == "mouse → Mus musculus (taxon 10090)"


@pytest.mark.anyio
@pytest.mark.parametrize("organism", ["yeast", "frog"])
async def test_ambiguous_organism_is_an_actionable_error(client, respx_mock, organism):
    result = await search(client, query="p53", organism=organism)

    text = error_text(result)
    assert "ambiguous" in text and "Ask the user" in text and "taxon" in text
    assert not respx_mock.calls


def taxon(tax_id, scientific, common=None, rank="species", reviewed=10):
    entry = {
        "taxonId": tax_id,
        "scientificName": scientific,
        "rank": rank,
        "statistics": {"reviewedProteinCount": reviewed},
    }
    if common:
        entry["commonName"] = common
    return entry


def mock_taxonomy(respx_mock, *results):
    return respx_mock.get(UNIPROT_TAXONOMY_URL).respond(200, json={"results": list(results)})


@pytest.mark.anyio
@pytest.mark.parametrize("organism", ["any", "Any", "anything", "all"])
async def test_any_organism_is_not_a_thing_and_is_never_searched(
    client, respx_mock, organism
):
    """'any' was never supported: the organism is required and never assumed. It is an
    error that says to ask, not an organism name sent to UniProt."""
    result = await search(client, query="p53", organism=organism)

    text = error_text(result)
    assert "Ask the user" in text and "do not assume human" in text
    assert not respx_mock.calls


@pytest.mark.anyio
async def test_unlisted_names_are_resolved_by_the_uniprot_taxonomy_search(
    client, respx_mock, load_fixture
):
    lookup = mock_taxonomy(
        respx_mock,
        taxon(1642515, "Zebra finch circovirus"),  # a text hit, not an exact name
        taxon(59729, "Taeniopygia guttata", "Zebra finch"),
    )
    route = mock_search(respx_mock, {"results": []})

    await search(client, query="p53", organism="zebra finch")

    assert "zebra finch" in lookup.calls.last.request.url.params["query"]
    assert "organism_id:59729" in route.calls.last.request.url.params["query"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("given", "taxon_id", "expected"),
    [
        ("human", 9606, "human → Homo sapiens (taxon 9606)"),
        ("mouse", 10090, "mouse → Mus musculus (taxon 10090)"),
        ("Homo sapiens", 9606, "Homo sapiens (taxon 9606)"),
        ("9606", 9606, "Homo sapiens (taxon 9606)"),
        ("Zebra Finch", 59729, "Zebra Finch → Taeniopygia guttata (taxon 59729)"),
    ],
)
async def test_the_resolved_organism_is_in_the_output(
    client, respx_mock, load_fixture, given, taxon_id, expected
):
    mock_taxonomy(respx_mock, taxon(59729, "Taeniopygia guttata", "Zebra finch"))
    body = with_taxon(load_fixture("uniprot_search_cbp1_human_gene.json"), taxon_id)
    mock_search(respx_mock, body)

    result = await search(client, query="CBP1", organism=given)

    assert not result.is_error
    assert result.structured_content["organism"] == expected


@pytest.mark.anyio
async def test_several_plausible_taxa_is_an_error_that_lists_them(client, respx_mock):
    mock_taxonomy(
        respx_mock,
        taxon(111, "Gus alpha", "Blue gus"),
        taxon(222, "Gus beta", "Blue Gus"),
        taxon(333, "Gus gamma", "Blue gus", rank="genus"),  # wrong rank: not plausible
    )

    result = await search(client, query="p53", organism="blue gus")

    text = error_text(result)
    assert "matches several organisms" in text
    assert "Gus alpha, Blue gus (taxon 111)" in text and "taxon 222" in text
    assert "taxon 333" not in text
    assert "Ask the user" in text
    assert len(respx_mock.calls) == 1  # no protein search was made


@pytest.mark.anyio
async def test_several_exact_matches_prefer_the_ones_with_proteins(client, respx_mock):
    mock_taxonomy(
        respx_mock,
        taxon(111, "Gus alpha", "Blue gus", reviewed=0),
        taxon(222, "Gus beta", "Blue gus", reviewed=12),
    )
    route = mock_search(respx_mock, {"results": []})

    await search(client, query="p53", organism="blue gus")

    assert "organism_id:222" in route.calls.last.request.url.params["query"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "results", [[], [taxon(1, "Something else entirely")], [taxon(2, "Unicorn", rank="genus")]]
)
async def test_unknown_organism_is_an_actionable_error(client, respx_mock, results):
    mock_taxonomy(respx_mock, *results)

    result = await search(client, query="p53", organism="unicorn")

    text = error_text(result)
    assert "Unknown organism 'unicorn'" in text
    assert "taxon ID" in text and "uniprot.org/taxonomy" in text
    assert len(respx_mock.calls) == 1  # only the taxonomy lookup


@pytest.mark.anyio
async def test_a_failing_taxonomy_lookup_is_reported_not_guessed(client, respx_mock):
    respx_mock.get(UNIPROT_TAXONOMY_URL).respond(503)

    result = await search(client, query="p53", organism="zebra finch")

    assert "UniProt taxonomy is slow or unavailable" in error_text(result)


@pytest.mark.anyio
async def test_unlisted_taxon_id_is_passed_through(client, respx_mock, load_fixture):
    route = mock_search(respx_mock, {"results": []})

    await search(client, query="p53", organism="9999999")

    assert "organism_id:9999999" in route.calls.last.request.url.params["query"]


@pytest.mark.anyio
async def test_quotes_in_the_query_cannot_break_the_uniprot_query(
    client, respx_mock, load_fixture
):
    route = mock_search(respx_mock, load_fixture("uniprot_search_cbp1_human_gene.json"))

    await search(client, query=r'he"llo\ world', organism="human")

    assert 'gene:"he llo world"' in route.calls.last.request.url.params["query"]


@pytest.mark.anyio
async def test_reviewed_entries_come_first(client, respx_mock, load_fixture):
    body = all_human(load_fixture("uniprot_search_cbp1_any_organism.json"))
    body["results"][0]["entryType"] = "UniProtKB unreviewed (TrEMBL)"
    first_accession = body["results"][0]["primaryAccession"]
    mock_search(respx_mock, body)

    result = await search(client, query="CBP1", organism="human", max_results=25)

    flags = [c["reviewed"] for c in result.structured_content["candidates"]]
    assert flags == sorted(flags, reverse=True)  # all True first, then False
    assert result.structured_content["candidates"][-1]["accession"] == first_accession


@pytest.mark.anyio
async def test_several_reviewed_entries_means_confirm_with_the_user(
    client, respx_mock, load_fixture
):
    mock_search(respx_mock, all_human(load_fixture("uniprot_search_cbp1_any_organism.json")))

    result = await search(client, query="CBP1", organism="human", max_results=3)

    data = result.structured_content
    assert len(data["candidates"]) == 3  # max_results caps what is returned
    assert "Ask the user which one they mean" in data["note"]
    assert "more than one protein is plausible" in data["note"]


@pytest.mark.anyio
async def test_one_reviewed_entry_among_unreviewed_ones(
    client, respx_mock, load_fixture
):
    body = all_human(load_fixture("uniprot_search_cbp1_any_organism.json"))
    body["results"] = body["results"][:3]
    for item in body["results"][1:]:
        item["entryType"] = "UniProtKB unreviewed (TrEMBL)"
    mock_search(respx_mock, body)

    result = await search(client, query="CBP1", organism="human")

    note = result.structured_content["note"]
    assert note.startswith("One reviewed entry matches")
    assert "unreviewed" in note


@pytest.mark.anyio
async def test_only_unreviewed_entries_are_flagged(client, respx_mock, load_fixture):
    body = copy.deepcopy(load_fixture("uniprot_search_cbp1_human_gene.json"))
    body["results"][0]["entryType"] = "UniProtKB unreviewed (TrEMBL)"
    mock_search(respx_mock, body)

    result = await search(client, query="CBP1", organism="human")

    assert "unreviewed" in result.structured_content["note"]
    assert "Ask the user which one they mean" in result.structured_content["note"]


@pytest.mark.anyio
async def test_no_match_suggests_next_steps(client, respx_mock, load_fixture):
    mock_search(respx_mock, load_fixture("uniprot_search_no_match.json"))

    result = await search(client, query="ZZZZNOTAGENE", organism="human")

    text = error_text(result)
    assert "ZZZZNOTAGENE" in text
    assert "spelling" in text
    assert "full protein name" in text
    assert "organism" in text


@pytest.mark.anyio
@pytest.mark.parametrize("organism", ["", "   "])
async def test_organism_is_required_and_never_assumed(client, respx_mock, organism):
    result = await search(client, query="CBP1", organism=organism)

    text = error_text(result)
    assert "Ask the user" in text
    assert "do not assume human" in text
    assert not respx_mock.calls


@pytest.mark.anyio
async def test_empty_query_is_rejected_without_calling_uniprot(client, respx_mock):
    result = await search(client, query="  ", organism="human")

    assert "query is empty" in error_text(result)
    assert not respx_mock.calls


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure",
    [httpx.ReadTimeout("slow"), httpx.ConnectError("down"), 503],
    ids=["timeout", "connection-error", "http-503"],
)
async def test_slow_or_unavailable_uniprot_is_reported(client, respx_mock, failure):
    route = respx_mock.get(UNIPROT_SEARCH_URL)
    if isinstance(failure, int):
        route.respond(failure)
    else:
        route.mock(side_effect=failure)

    result = await search(client, query="CBP1", organism="human")

    assert "UniProt is slow or unavailable" in error_text(result)


@pytest.mark.anyio
async def test_rejected_query_is_reported(client, respx_mock):
    respx_mock.get(UNIPROT_SEARCH_URL).respond(400, json={"messages": ["bad query"]})

    result = await search(client, query="CBP1", organism="human")

    text = error_text(result)
    assert "could not run that search" in text
    assert "bad query" not in text  # the upstream message is not echoed to the model


@pytest.mark.anyio
async def test_ambiguous_names_tell_the_model_to_use_its_selector_when_it_has_one(
    client, respx_mock, load_fixture
):
    """The 'CBP' case: several reviewed proteins, and a client with no dialog of its own.

    The model decides how to ask. The note must name the best way (an interactive
    question tool: a clickable list) and the fallback (plain chat), and say what each
    option should show, so it does not just type a paragraph.
    """
    mock_search(respx_mock, all_human(load_fixture("uniprot_search_cbp1_any_organism.json")))

    result = await search(client, query="CBP", organism="human")

    data = result.structured_content
    assert data["confirmed_by_user"] is False
    note = data["note"]
    assert note.startswith(
        f"{sum(c['reviewed'] for c in data['candidates'])} reviewed entries match"
    )
    assert "interactive question tool" in note
    assert "one option per candidate" in note
    assert "accession, protein name and gene" in note
    assert "otherwise ask in chat" in note
    assert "before calling find_structures" in note


@pytest.mark.anyio
async def test_a_single_clear_match_does_not_tell_the_model_to_ask(
    client, respx_mock, load_fixture
):
    mock_search(respx_mock, load_fixture("uniprot_search_cbp1_human_gene.json"))

    result = await search(client, query="CBP1", organism="human")

    assert "interactive question tool" not in result.structured_content["note"]
    assert result.structured_content["note"].startswith("One entry matches")


@pytest.mark.anyio
async def test_only_unreviewed_matches_also_point_at_the_selector(
    client, respx_mock, load_fixture
):
    body = all_human(load_fixture("uniprot_search_cbp1_any_organism.json"))
    for item in body["results"]:
        item["entryType"] = "UniProtKB unreviewed (TrEMBL)"
    mock_search(respx_mock, body)

    result = await search(client, query="CBP1", organism="human")

    assert "interactive question tool" in result.structured_content["note"]
