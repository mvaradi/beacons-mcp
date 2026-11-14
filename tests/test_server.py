import tomllib
from pathlib import Path

import pytest
from conftest import error_text
from mcp.server.mcpserver import MCPServer

from beacons_mcp import __version__
from beacons_mcp.server import mcp


def flat(text: str) -> str:
    return " ".join(text.split())


async def get_tool(client, name):
    listed = await client.list_tools()
    return next(t for t in listed.tools if t.name == name)


def test_version_matches_pyproject():
    pyproject = tomllib.loads(
        (Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert pyproject["project"]["version"] == __version__
    assert mcp.version == __version__


@pytest.mark.anyio
async def test_exposes_exactly_the_two_tools(client):
    listed = await client.list_tools()
    assert {t.name for t in listed.tools} == {"search_proteins", "find_structures"}


@pytest.mark.anyio
async def test_tool_descriptions_stay_short(client):
    """The descriptions are sent to the model on every turn: keep them lean."""
    listed = await client.list_tools()
    sizes = {t.name: len(t.description) for t in listed.tools}

    assert sizes["search_proteins"] < 600
    assert sizes["find_structures"] < 750
    assert sum(sizes.values()) < 1300


@pytest.mark.anyio
async def test_search_proteins_schema(client):
    tool = await get_tool(client, "search_proteins")

    # Print so `pytest -s` shows exactly what the model would be shown.
    print("DESCRIPTION:\n", tool.description)
    print("INPUT SCHEMA:\n", tool.input_schema)

    # The description is plain paragraphs: no docstring "Args:" block, no
    # indentation, and the organism rule is stated verbatim.
    assert "Args:" not in tool.description
    assert "\n    " not in tool.description
    assert (
        "If the user has not named an organism, ask them. Do not assume human."
        in tool.description
    )
    assert "reviewed" in tool.description

    schema = tool.input_schema
    assert schema["required"] == ["query", "organism"]
    assert set(schema["properties"]) == {"query", "organism", "max_results"}  # no ctx
    for prop in schema["properties"].values():
        assert prop["description"]
        assert prop["examples"]  # every parameter documents an example value
    assert schema["properties"]["organism"]["type"] == "string"
    assert schema["properties"]["max_results"]["default"] == 5
    assert schema["properties"]["max_results"]["minimum"] == 1
    assert schema["properties"]["max_results"]["maximum"] == 25
    assert tool.output_schema["properties"]["candidates"]
    assert tool.annotations.read_only_hint is True


@pytest.mark.anyio
async def test_find_structures_schema(client):
    tool = await get_tool(client, "find_structures")

    print("DESCRIPTION:\n", tool.description)
    print("INPUT SCHEMA:\n", tool.input_schema)

    assert "Args:" not in tool.description
    assert "search_proteins" in tool.description  # tells the model where names go
    assert "span_percent" in flat(tool.description)  # tells the model to check coverage

    schema = tool.input_schema
    assert schema["required"] == ["uniprot_accession"]
    assert set(schema["properties"]) == {"uniprot_accession", "max_results"}
    for prop in schema["properties"].values():
        assert prop["description"]
        assert prop["examples"]
    assert schema["properties"]["max_results"]["default"] == 10
    assert schema["properties"]["max_results"]["maximum"] == 50
    assert tool.output_schema["properties"]["models"]
    assert tool.annotations.read_only_hint is True


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("search_proteins", {"query": "HBA", "organism": "human", "max_results": 0}),
        ("search_proteins", {"query": "HBA", "organism": "human", "max_results": 26}),
        ("find_structures", {"uniprot_accession": "P69905", "max_results": 0}),
        ("find_structures", {"uniprot_accession": "P69905", "max_results": 51}),
        ("search_proteins", {"query": "HBA"}),  # organism is required
    ],
)
async def test_invalid_arguments_are_rejected_before_any_http(
    client, respx_mock, tool, arguments
):
    result = await client.call_tool(tool, arguments)

    assert result.is_error
    assert not respx_mock.calls


@pytest.mark.anyio
async def test_learning_unexpected_exception_message_is_not_leaked():
    """Learning test: pins SDK behaviour we depend on.

    A tool that crashes with a non-ToolError exception must not leak that
    exception's text to the model. Uses a throwaway server so the public
    server never gets a deliberately broken tool.
    """
    from mcp import Client

    throwaway = MCPServer("throwaway", version="0.0.0")

    @throwaway.tool()
    def boom() -> str:
        """Always crashes."""
        raise ValueError("secret-xyz")

    async with Client(throwaway) as c:
        result = await c.call_tool("boom", {})

    assert result.is_error
    texts = [block.text for block in result.content if hasattr(block, "text")]
    assert texts, "expected at least one text block explaining the failure"
    assert all("secret-xyz" not in text for text in texts)
    # Stronger: the secret must not appear anywhere in the serialised result.
    assert "secret-xyz" not in result.model_dump_json()


@pytest.mark.anyio
async def test_unexpected_crash_in_a_real_tool_does_not_leak(client, respx_mock):
    """The same guarantee on our own tools: a malformed Hub body is not echoed back."""
    respx_mock.get(url__regex=r".*/uniprot/summary/P69905\.json").respond(
        200,
        json={
            "uniprot_entry": {"ac": "P69905"},
            "structures": [{"unexpected": "secret-xyz"}],
        },
    )

    result = await client.call_tool("find_structures", {"uniprot_accession": "P69905"})

    assert "secret-xyz" not in error_text(result)
