import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from beacons_mcp.server import mcp

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    async with Client(mcp) as c:
        yield c


@pytest.fixture
def load_fixture() -> Callable[[str], Any]:
    """Load a JSON fixture from tests/fixtures by file name."""

    def load(name: str) -> Any:
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    return load


def error_text(result) -> str:
    """The text the model would read from a failed tool call."""
    assert result.is_error, "expected the tool call to fail"
    return " ".join(block.text for block in result.content if hasattr(block, "text"))
