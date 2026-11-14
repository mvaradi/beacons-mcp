"""The shared HTTP client, service endpoints and common error handling."""

import logging

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from beacons_mcp import __version__

logger = logging.getLogger("beacons_mcp")

UNIPROT_SEARCH_URL = "https://rest.uniprot.org/uniprotkb/search"
HUB_BASE_URL = "https://www.ebi.ac.uk/pdbe/pdbe-kb/3dbeacons/api/v2"
# The registry is where 3D-Beacons publishes its providers and their descriptions.
REGISTRY_URL = "https://raw.githubusercontent.com/3D-Beacons/3d-beacons-registry/HEAD/resources/registry.json"

USER_AGENT = f"beacons-mcp/{__version__}"

# The Hub queries every beacon before answering and takes 1-2 s in practice, so
# the read timeout is generous; connecting should be fast or not at all.
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def make_client() -> httpx.AsyncClient:
    """Create the one HTTP client shared by all tools for the life of the server."""
    return httpx.AsyncClient(
        timeout=TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    )


async def get(
    client: httpx.AsyncClient,
    url: str,
    *,
    service: str,
    params: dict[str, str | int] | None = None,
) -> httpx.Response:
    """GET ``url``, turning timeouts, network errors and 5xx into a ToolError.

    4xx responses are returned to the caller, which knows what they mean.
    """
    unavailable = ToolError(
        f"{service} is slow or unavailable right now. Wait a moment and try again; "
        "if it keeps failing, tell the user the service seems to be down."
    )
    try:
        response = await client.get(url, params=params)
    except httpx.TimeoutException as exc:
        logger.warning("%s timed out: %r", service, exc)
        raise unavailable from exc
    except httpx.TransportError as exc:
        logger.warning("%s unreachable: %r", service, exc)
        raise unavailable from exc
    if response.status_code >= 500:
        logger.warning("%s returned HTTP %d", service, response.status_code)
        raise unavailable
    return response
