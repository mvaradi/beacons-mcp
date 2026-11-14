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
| `search_proteins(query, organism, max_results=5)` | Maps a protein or gene name plus an **organism** to UniProt accessions, using the [UniProt REST API](https://www.uniprot.org/help/api). Gene synonyms match. Reviewed (Swiss-Prot) entries come first. The organism is required and never assumed: the model is told to ask the user. When several proteins match, the user can pick one in a dialog (see below). |
| `find_structures(uniprot_accession, max_results=10)` | Lists the structure models for an accession from the [3D-Beacons Hub](https://www.ebi.ac.uk/pdbe/pdbe-kb/3dbeacons/api/v2/openapi.json) (OpenAPI spec). Returns totals by provider and category, then a selection of models with residue range, how much of the protein it spans, resolution or confidence, and file URL and format. It returns links; it does not download structures. |

Both tools are read-only and report failures (no match, unknown accession, a slow or
unavailable service) as tool errors that tell the model what to try next.

### Example

> **You:** What 3D structures exist for the human protein CBP1?

The model calls `search_proteins("CBP1", "human")`. UniProt matches one reviewed entry,
**P50454** (Serpin H1), because `CBP1` is a synonym of its gene `SERPINH1`. The model
confirms that is the protein you meant, then calls `find_structures("P50454")` and
summarises the experimental and predicted models it finds.

### Picking the right protein

When a search is ambiguous (several reviewed entries, or only unreviewed ones),
`search_proteins` asks the user to choose, using MCP
[elicitation](https://modelcontextprotocol.io/specification/latest/client/elicitation): a
small form with up to five candidates, labelled like
`P69905 - Hemoglobin subunit alpha (HBA_HUMAN)`. The pick comes back to the model as
`confirmed_by_user`, so it cannot skip the question or guess. When the list is only part of
the matches the form says so, and declining it means "none of these".

This needs a client that supports elicitation. Where it does not, or the user declines, the
tool returns all candidates and tells the model to ask in chat instead. A single clear match
never opens a dialog. The server logs on stderr whether the client declared support.

Things worth knowing when reading results:

- **Which models are shown.** A protein can have hundreds. The experimental model that
  spans the most of the protein comes first, even if it is small, so measured evidence is
  always represented. The rest are ordered by how much of the protein they span, and the
  slots are shared round-robin across providers so that one provider with hundreds of
  models cannot crowd out the others. Totals always cover every model the Hub returned,
  and the number left out is reported. For a long protein such as CREB-binding protein
  (Q92793, 2442 residues), whose experimental structures are domains, this puts a
  full-length prediction ahead of further fragments.
- **Rows are the Hub's.** Each Hub item is reported as the Hub lists it, so an entry the
  Hub lists once per residue segment appears once per segment, and totals match the Hub's
  own counts. `span_percent` is the one derived number: the share of the protein between
  the first and last residue of the reported range. The Hub's own `coverage` field is not
  used, because it disagrees with the residue range for some providers.
- **Conformational ensembles** come with a note: the file URL serves only a sample (the
  first models) of the ensemble, and the format the Hub reports may not match the file.
- **Provider descriptions** are read live from the
  [3D-Beacons registry](https://github.com/3D-Beacons/3d-beacons-registry), fetched
  alongside the Hub request, cached for six hours and never allowed to fail a call. If the
  registry cannot be reached, a bundled snapshot is used instead (and goes stale), and a
  provider it does not know is listed without a description.

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
