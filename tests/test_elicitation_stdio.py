"""search_proteins end to end over real stdio, with a client that can answer forms."""

import sys
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ElicitResult

SERVER = Path(__file__).parent / "stdio_stub_server.py"
PARAMS = StdioServerParameters(command=sys.executable, args=[str(SERVER)])

pytestmark = pytest.mark.anyio


async def search(query, elicitation_callback):
    with anyio.fail_after(60):
        async with (
            stdio_client(PARAMS) as (read, write),
            ClientSession(
                read, write, elicitation_callback=elicitation_callback
            ) as session,
        ):
            await session.initialize()
            result = await session.call_tool(
                "search_proteins", {"query": query, "organism": "human"}
            )
    assert not result.is_error, result.content
    return result.structured_content


def options_of(params):
    return params.requested_schema["properties"]["protein"]["enum"]


async def test_user_picks_a_protein_in_the_dialog():
    shown = {}

    async def choose_alpha(context, params):
        shown["message"] = params.message
        shown["options"] = options_of(params)
        alpha = next(o for o in shown["options"] if o.startswith("P69905"))
        return ElicitResult(action="accept", content={"protein": alpha})

    data = await search("hemoglobin", choose_alpha)

    assert data["confirmed_by_user"] is True
    assert [c["accession"] for c in data["candidates"]] == ["P69905"]
    assert "do not ask again" in data["note"]
    # What the user saw: the question, and five labelled choices.
    assert "hemoglobin" in shown["message"]
    assert len(shown["options"]) == 5
    assert any("Hemoglobin subunit alpha" in o for o in shown["options"])


async def test_user_declines_the_dialog():
    async def decline(context, params):
        return ElicitResult(action="decline")

    data = await search("hemoglobin", decline)

    assert data["confirmed_by_user"] is False
    assert len(data["candidates"]) == 5  # all candidates come back for the chat
    assert "dismissed" in data["note"]
    assert "Ask them in chat" in data["note"]


async def test_client_without_elicitation_gets_the_chat_fallback():
    data = await search("hemoglobin", None)

    assert data["confirmed_by_user"] is False
    assert len(data["candidates"]) == 5
    assert "confirm" in data["note"]
    assert "dismissed" not in data["note"]


async def test_unambiguous_search_never_opens_the_dialog():
    asked = []

    async def should_not_be_called(context, params):
        asked.append(params.message)
        return ElicitResult(action="decline")

    data = await search("CBP1", should_not_be_called)

    assert asked == []
    assert [c["accession"] for c in data["candidates"]] == ["P50454"]
    assert data["confirmed_by_user"] is False
