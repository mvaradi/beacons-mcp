import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer

from beacons_mcp.server import mcp


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    async with Client(mcp) as c:
        yield c


@pytest.mark.anyio
async def test_hello_returns_greeting(client):
    result = await client.call_tool("hello", {"name": "Ada"})

    assert not result.is_error
    assert result.structured_content == {"result": "Hello, Ada!"}


@pytest.mark.anyio
async def test_hello_tool_schema(client):
    listed = await client.list_tools()
    tool = next(t for t in listed.tools if t.name == "hello")

    # Print so `pytest -s` shows exactly what the model would be shown.
    print("DESCRIPTION:\n", tool.description)
    print("INPUT SCHEMA:\n", tool.input_schema)

    # The description is the docstring: summary plus usage guidance. Parameter
    # docs deliberately do NOT live here (no "Args:" block); see below.
    assert tool.description.startswith("Return a one-sentence greeting")
    assert "never use it to look up structures" in tool.description
    assert "Args:" not in tool.description

    # The input schema is generated from the signature: one required string.
    schema = tool.input_schema
    assert schema["type"] == "object"
    assert schema["required"] == ["name"]
    assert set(schema["properties"]) == {"name"}
    name_schema = schema["properties"]["name"]
    assert name_schema["type"] == "string"
    # Pattern for every tool: parameters are documented with
    # Annotated[..., Field(description=...)], which lands in the schema where
    # the model reads it per parameter.
    assert "non-whitespace" in name_schema["description"]


@pytest.mark.anyio
async def test_learning_unexpected_exception_message_is_not_leaked():
    """Learning test: pins SDK behaviour we depend on (Q4).

    A tool that crashes with a non-ToolError exception must not leak that
    exception's text to the model. Uses a throwaway server so the public
    server never gets a deliberately broken tool.
    """
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
