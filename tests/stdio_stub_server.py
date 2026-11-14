"""The real server over stdio, with UniProt answered from captured fixtures.

Used by test_elicitation_stdio.py. A real stdio connection is the only way to test
elicitation end to end: the in-memory test client has no channel for requests that the
server starts. Not a test module itself (the name does not match test_*.py).
"""

import json
from pathlib import Path

import httpx

from beacons_mcp import server

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "rest.uniprot.org":
        ambiguous = "hemoglobin" in request.url.params["query"]
        name = (
            "uniprot_search_hemoglobin_human.json"
            if ambiguous
            else "uniprot_search_cbp1_human_gene.json"
        )
        return httpx.Response(200, json=_load(name))
    return httpx.Response(404, json={})


def _make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(_handler))


# The server's lifespan looks make_client up in its own module at call time.
server.make_client = _make_client

if __name__ == "__main__":
    server.main()
