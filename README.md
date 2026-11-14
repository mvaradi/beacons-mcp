# beacons-mcp

An [MCP](https://modelcontextprotocol.io) server that lets AI agents find 3D protein
structures through the [3D-Beacons](https://www.ebi.ac.uk/pdbe/pdbe-kb/3dbeacons/)
network, which federates experimental and predicted structure models from many
providers behind one API.

> **Status: early work in progress.** A personal project for learning MCP, not an
> official 3D-Beacons or UniProt product.

## Tools

| Tool | What it does |
|---|---|
| `search_proteins(query, organism, max_results=5)` | Maps a protein or gene name plus an **organism** to UniProt accessions, using the [UniProt REST API](https://www.uniprot.org/help/api). Gene synonyms match. Reviewed (Swiss-Prot) entries come first. The organism is required and never assumed: the model is told to ask the user. |
| `find_structures(uniprot_accession, max_results=10)` | Lists the structure models for an accession from the [3D-Beacons Hub](https://www.ebi.ac.uk/pdbe/pdbe-kb/3dbeacons/api/v2/openapi.json) (OpenAPI spec). Returns totals by provider and category, then a selection of models showing the best of each provider first, with residue coverage, resolution or confidence, and file URL and format. It returns links; it does not download structures. |

Both tools are read-only and report failures (no match, unknown accession, a slow or
unavailable service) as tool errors that tell the model what to try next.

### Example

> **You:** What 3D structures exist for the human protein CBP1?

The model calls `search_proteins("CBP1", "human")`. UniProt matches one reviewed entry,
**P50454** (Serpin H1), because `CBP1` is a synonym of its gene `SERPINH1`. The model
confirms that is the protein you meant, then calls `find_structures("P50454")` and
summarises the experimental and predicted models it finds.

Things worth knowing when reading results:

- **Models differ in coverage.** Coverage is computed from the residue ranges the Hub
  reports, because the Hub's own `coverage` field is not reliable for every provider.
- **Conformational ensembles** come with a note: the file URL serves only a sample (the
  first models) of the ensemble, and the format the Hub reports may not match the file.
- **Provider descriptions** come from a bundled snapshot of the
  [3D-Beacons registry](https://github.com/3D-Beacons/3d-beacons-registry), so a provider
  added after the snapshot is listed without a description.

## Development

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest            # unit tests, no network needed
uv run pytest -m live    # also call the real UniProt and 3D-Beacons services
uv run ruff check .
```

The unit tests mock HTTP with [respx](https://lundberg.github.io/respx/) against real
responses saved in `tests/fixtures/` (see the README there).

Run the server over stdio, or try it in the MCP Inspector:

```bash
uv run beacons-mcp
uv run mcp dev src/beacons_mcp/server.py
```

### Use it with Claude Code

`.mcp.json` registers the server at project scope (`uv run beacons-mcp`, no absolute
paths), so opening this folder in Claude Code offers it as `beacons`. Approve the
project server when prompted, then check that it shows as connected with `/mcp`. To add
it by hand instead:

```bash
claude mcp add --scope project beacons -- uv run beacons-mcp
```

## License

[MIT](LICENSE)
