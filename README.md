# beacons-mcp

An [MCP](https://modelcontextprotocol.io) server that will expose the
[3D-Beacons](https://www.ebi.ac.uk/pdbe/pdbe-kb/3dbeacons/) protein structure API
to AI agents.

> **Status: early work in progress.** This is a personal project for learning MCP.
> The server currently has a single placeholder tool, `hello`, and does not query
> 3D-Beacons yet.

## Development

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest
```

Run the server over stdio:

```bash
uv run beacons-mcp
```

Try it in the MCP Inspector:

```bash
uv run mcp dev src/beacons_mcp/server.py
```

## License

[MIT](LICENSE)
