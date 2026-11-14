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
| `show_structure(uniprot_accession, model_id)` | Opens one model from `find_structures` in an interactive 3D viewer ([Mol*](https://molstar.org)) inside the chat, in apps that support [MCP Apps](https://modelcontextprotocol.io/extensions/apps/overview). Other clients get a text summary and the file URL. See "Viewing a structure in chat" below. |

A fourth tool, `fetch_structure_file`, exists only for the viewer: it is app-only, so hosts
keep it from the model.

All tools are read-only and report failures (no match, unknown accession, a slow or
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

### Viewing a structure in chat

`show_structure` re-resolves the model through the Hub (it takes an accession and a
`model_id`, never a URL) and returns a text summary: provider, model ID, category, residues,
resolution or confidence, and the file URL. In a chat app that supports MCP Apps, such as
Claude Desktop, an interactive viewer opens as well, drawn with Mol*. Everywhere else, for
example the Claude Code terminal, you get the text summary and the file URL.

How the file reaches the viewer, in this order:

1. **The page fetches it from the provider itself.** The viewer's content security policy
   allows connections to the providers' service hosts and nowhere else. Those hosts come
   from the [3D-Beacons registry](https://github.com/3D-Beacons/3d-beacons-registry): the
   bundled snapshot at first, then the live registry once `show_structure` has run, so a
   provider added later is picked up without a code change. Nothing else is allowed: no
   outside scripts, styles, images or fonts.
2. **Otherwise the server fetches it.** If the first attempt fails (the provider sends no
   CORS headers, or the chat app blocks the connection), the page asks the app-only
   `fetch_structure_file` tool for the file, in compressed base64 chunks of about 96,000
   characters because tool results are limited in size. This only works where the chat app
   lets an app call server tools, which it does not for a local server in the Claude Code
   tab (it answers "IPC not available for MCP tool call").
3. **If both fail,** the viewer says why in plain words and offers buttons for the file's
   web address and the entry page.

Either way the file is checked the same way: its real format is read from its bytes (the
Hub's label and the `Content-Type` are not reliable), gzip is unpacked within the size limit,
and anything that is not mmCIF or PDB text is refused with the file's address.

The server's own download only reaches public web addresses: never localhost or private
networks, and the same holds for redirects. A token from `show_structure` lasts 15 minutes
and names one Hub-resolved model.

Limits:

- **Providers without CORS headers** (three of ten providers in the data I checked: the
  ones behind the PED ensembles, levylab and Bindome) cannot be loaded by the page itself.
  They load only through the server route, so in the Code tab they show the links instead.
- **Size.** Files over 8 MB (unpacked) are refused, with the address to download from.
  Change it with `BEACONS_MCP_MAX_FILE_MB`.
- **Formats.** mmCIF and PDB text only.
- **Ensembles** show the provider's sample file (the first models) and say so; whole
  ensembles are not rendered.
- **Colours.** Experimental models are coloured by chain, predicted models by confidence.
- **Speed.** The page is about 5 MB, so the first draw takes a few seconds to half a minute.
- **Clients.** Claude Desktop and claude.ai are listed as supporting MCP Apps. The Claude
  Code terminal does not render them and shows the text. What your own client does is worth
  checking: see `docs/lab-notebook/`.

## Development

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest            # unit tests, no network needed
uv run pytest -m live    # also call the real UniProt and 3D-Beacons services
uv run ruff check .
```

The tests for the viewer build (`tests/test_viewer_build.py`) are skipped until the viewer
has been built (next section).

The unit tests mock HTTP with [respx](https://lundberg.github.io/respx/) against real
responses saved in `tests/fixtures/` (see the README there).

Run the server over stdio, or try it in the MCP Inspector:

```bash
uv run beacons-mcp
uv run mcp dev src/beacons_mcp/server.py
```

### Build the viewer

The viewer is a small Node project in `viewer/` (Mol* and the MCP Apps client, bundled by
Vite into one HTML file). The built page is **not committed**: build it once from a
checkout, or `show_structure` falls back to text and says so.

```bash
cd viewer
npm install
npm run build    # writes src/beacons_mcp/ui/viewer.html (about 5 MB)
npm test         # the decoder's unit tests
```

Needs **Node 22.12 or newer** (Mol* requires 22; Vite requires 20.19+). The build stubs out
Mol*'s MP4 encoder, which fetches a `data:` URL at start-up that an app sandbox blocks, and
keeps the third-party licence notices as a comment at the top of the page.

The MCP Inspector can render the viewer for local testing: `uv run mcp dev
src/beacons_mcp/server.py`, then the **Apps** tab, then `show_structure`.

### Use it in Claude Desktop

Claude Desktop runs local servers from its own config file. That file lives on your machine
and holds absolute paths, so **it is never committed**. In Claude Desktop open Settings,
Developer, Edit Config (this creates `claude_desktop_config.json`, on Windows under
`%APPDATA%\Claude`) and add the server, with the path to your checkout:

```json
{
  "mcpServers": {
    "beacons": {
      "command": "uv",
      "args": ["run", "--directory", "C:\\path\\to\\beacons-mcp", "beacons-mcp"]
    }
  }
}
```

Build the viewer first (above), then restart Claude Desktop, or turn on Developer Mode
(Help, Troubleshooting) and use Developer, Reload MCP Configuration. Claude asks for
permission before it shows an app. Then try: "Show me the
AlphaFold model of human hemoglobin alpha."

### Use it with Claude Code

`.mcp.json` registers the server at project scope (`uv run beacons-mcp`, no absolute
paths), so opening this folder in Claude Code offers it as `beacons`. Approve the
project server when prompted, then check that it shows as connected with `/mcp`. To add
it by hand instead:

```bash
claude mcp add --scope project beacons -- uv run beacons-mcp
```

Claude Code does not render MCP Apps, so `show_structure` returns the text summary and the
file URL there.

## License

[MIT](LICENSE)
