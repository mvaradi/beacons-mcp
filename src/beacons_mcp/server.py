"""MCP server for the 3D-Beacons protein structure API.

Importing this module has no side effects beyond constructing the server object:
nothing is started and logging is not configured until ``main()`` is called.
"""

import logging
import sys
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

logger = logging.getLogger("beacons_mcp")

mcp = MCPServer("beacons-mcp", version="0.1.0")


@mcp.tool()
def hello(
    name: Annotated[
        str,
        Field(
            description=(
                'The name to greet, e.g. "Ada". Must contain at least one '
                "non-whitespace character; an empty or blank name returns an error."
            )
        ),
    ],
) -> str:
    """Return a one-sentence greeting, e.g. "Hello, Ada!".

    This is a placeholder tool used to verify that the server connection works.
    It does not query any protein or structure data. Call it only when asked to
    test the connection or say hello; never use it to look up structures.
    """
    if not name.strip():
        raise ToolError("name must not be empty. Provide a non-empty name to greet.")
    logger.info("hello called")
    return f"Hello, {name}!"


def main() -> None:
    """Configure logging to stderr and run the server over stdio."""
    # stdout carries the MCP protocol on stdio transport, so log to stderr only.
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        # MCPServer() already installed a root handler at construction time;
        # without force=True this call would be silently ignored.
        force=True,
    )
    logger.info("starting beacons-mcp on stdio")
    mcp.run(transport="stdio")
