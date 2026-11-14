import logging
import sys

import pytest

from beacons_mcp.clients import HUB_BASE_URL, REGISTRY_URL, UNIPROT_SEARCH_URL
from beacons_mcp.server import configure_logging


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:], root.level = handlers, level


@pytest.mark.anyio
async def test_no_tool_writes_to_stdout(
    client, respx_mock, load_fixture, capfd, restore_logging
):
    """stdout carries the MCP protocol on stdio: a stray write would corrupt it."""
    configure_logging()
    respx_mock.get(UNIPROT_SEARCH_URL).respond(
        200, json=load_fixture("uniprot_search_cbp1_human_gene.json")
    )
    respx_mock.get(f"{HUB_BASE_URL}/uniprot/summary/P69905.json").respond(
        200, json=load_fixture("hub_summary_P69905.json")
    )
    respx_mock.get(f"{HUB_BASE_URL}/uniprot/summary/P99999.json").respond(404, json={})
    respx_mock.get(REGISTRY_URL).respond(
        200, json=load_fixture("registry_providers.json")
    )
    capfd.readouterr()  # drop anything printed while the fixtures were set up

    await client.call_tool("search_proteins", {"query": "CBP1", "organism": "human"})
    await client.call_tool("find_structures", {"uniprot_accession": "P69905"})
    failed = await client.call_tool("find_structures", {"uniprot_accession": "P99999"})
    invalid = await client.call_tool("find_structures", {"uniprot_accession": "nope"})

    assert failed.is_error and invalid.is_error  # error paths log too
    captured = capfd.readouterr()
    assert captured.out == ""
    assert "find_structures" in captured.err  # logging does reach stderr


@pytest.mark.anyio
async def test_registry_fallback_does_not_write_to_stdout(
    client, respx_mock, load_fixture, capfd, restore_logging
):
    configure_logging()
    respx_mock.get(f"{HUB_BASE_URL}/uniprot/summary/P69905.json").respond(
        200, json=load_fixture("hub_summary_P69905.json")
    )
    respx_mock.get(REGISTRY_URL).respond(503)
    capfd.readouterr()

    result = await client.call_tool("find_structures", {"uniprot_accession": "P69905"})

    assert not result.is_error
    captured = capfd.readouterr()
    assert captured.out == ""
    assert "registry unavailable" in captured.err  # the fallback is logged, to stderr


def test_logging_is_configured_for_stderr_only(restore_logging):
    configure_logging()

    handlers = logging.getLogger().handlers
    assert handlers
    for handler in handlers:
        assert isinstance(handler, logging.StreamHandler)
        assert handler.stream is sys.stderr


@pytest.mark.anyio
async def test_the_viewer_tools_do_not_write_to_stdout(
    client, respx_mock, capfd, restore_logging, monkeypatch
):
    from beacons_mcp import safe_fetch

    async def resolve(host):
        return ["93.184.216.34"]

    monkeypatch.setattr(safe_fetch, "_resolve", resolve)
    configure_logging()
    file_url = "https://files.example.org/m1.cif"
    respx_mock.get(f"{HUB_BASE_URL}/uniprot/summary/P69905.json").respond(
        200,
        json={
            "uniprot_entry": {
                "ac": "P69905",
                "id": "HBA_HUMAN",
                "sequence_length": 142,
            },
            "structures": [
                {
                    "summary": {
                        "provider": "TestProvider",
                        "model_identifier": "m1",
                        "model_category": "AB-INITIO",
                        "model_url": file_url,
                        "model_format": "MMCIF",
                        "uniprot_start": 1,
                        "uniprot_end": 142,
                    }
                }
            ],
        },
    )
    respx_mock.get(file_url).respond(200, content=b"data_x\n_a 1\n")
    capfd.readouterr()

    shown = await client.call_tool(
        "show_structure", {"uniprot_accession": "P69905", "model_id": "m1"}
    )
    token = shown.structured_content["token"]
    fetched = await client.call_tool(
        "fetch_structure_file", {"token": token, "chunk": 0}
    )
    bad_token = await client.call_tool(
        "fetch_structure_file", {"token": "nope", "chunk": 0}
    )
    unknown = await client.call_tool(
        "show_structure", {"uniprot_accession": "P69905", "model_id": "missing"}
    )

    assert not shown.is_error and not fetched.is_error  # the happy path ran
    assert bad_token.is_error and unknown.is_error  # and the error paths log too
    captured = capfd.readouterr()
    assert captured.out == ""
    assert "show_structure" in captured.err
