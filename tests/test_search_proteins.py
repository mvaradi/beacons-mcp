import copy

import httpx
import pytest
from conftest import error_text

from beacons_mcp.clients import UNIPROT_SEARCH_URL, USER_AGENT


async def search(client, **arguments):
    return await client.call_tool("search_proteins", arguments)


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
    assert 'organism_name:"human"' in query
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
async def test_quotes_in_the_query_cannot_break_the_uniprot_query(
    client, respx_mock, load_fixture
):
    route = mock_search(respx_mock, load_fixture("uniprot_search_cbp1_human_gene.json"))

    await search(client, query='he"llo\\ world', organism='hu"man')

    query = route.calls.last.request.url.params["query"]
    assert 'gene:"he llo world"' in query
    assert 'organism_name:"hu man"' in query


@pytest.mark.anyio
async def test_reviewed_entries_come_first(client, respx_mock, load_fixture):
    body = copy.deepcopy(load_fixture("uniprot_search_cbp1_any_organism.json"))
    body["results"][0]["entryType"] = "UniProtKB unreviewed (TrEMBL)"
    first_accession = body["results"][0]["primaryAccession"]
    mock_search(respx_mock, body)

    result = await search(client, query="CBP1", organism="anything", max_results=25)

    flags = [c["reviewed"] for c in result.structured_content["candidates"]]
    assert flags == sorted(flags, reverse=True)  # all True first, then False
    assert result.structured_content["candidates"][-1]["accession"] == first_accession


@pytest.mark.anyio
async def test_several_reviewed_entries_means_confirm_with_the_user(
    client, respx_mock, load_fixture
):
    mock_search(respx_mock, load_fixture("uniprot_search_cbp1_any_organism.json"))

    result = await search(client, query="CBP1", organism="anything", max_results=3)

    data = result.structured_content
    assert len(data["candidates"]) == 3  # max_results caps what is returned
    assert "confirm" in data["note"]
    assert "more than one protein is plausible" in data["note"]


@pytest.mark.anyio
async def test_one_reviewed_entry_among_unreviewed_ones(
    client, respx_mock, load_fixture
):
    body = copy.deepcopy(load_fixture("uniprot_search_cbp1_any_organism.json"))
    body["results"] = body["results"][:3]
    for item in body["results"][1:]:
        item["entryType"] = "UniProtKB unreviewed (TrEMBL)"
    mock_search(respx_mock, body)

    result = await search(client, query="CBP1", organism="anything")

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
    assert "confirm" in result.structured_content["note"]


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
