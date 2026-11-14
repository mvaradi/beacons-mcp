# Structure viewer: renders in Claude Desktop, falls back in Claude Code?

Status: **stub.** The server side and the viewer are verified in the MCP Inspector. The
three client checks below are for whoever runs them; they are not done yet.

## Question

When a user asks "show me the AlphaFold model of human haemoglobin alpha", does
`show_structure` give:

- an interactive Mol* viewer inline in **Claude Desktop** (chat), and
- the text summary with the file URL in **Claude Code** (terminal and the desktop app's Code
  tab), with nothing broken or missing?

## Expectation (from Anthropic's and the MCP docs, not yet observed here)

| Client | Expected |
|---|---|
| Claude Desktop, chat | Viewer renders after a permission prompt; local stdio servers are documented to work |
| Claude Code, terminal | Text only: "calls the tool as text and doesn't render the UI" |
| Claude Code, desktop app Code tab | Not stated in the docs: that is the open question |

Sources: <https://claude.com/docs/connectors/building/mcp-apps/quickstart>,
<https://claude.com/docs/connectors/building/mcp-apps/getting-started.md>,
<https://modelcontextprotocol.io/extensions/apps/overview>.

## Setup

- Server: `beacons-mcp` at commit `<fill in>`, viewer built with `npm run build` in `viewer/`
  (Node `<fill in>`), file size of `src/beacons_mcp/ui/viewer.html`: `<fill in>`.
- Claude Desktop: version `<fill in>`; server added to `claude_desktop_config.json` as in the
  README (absolute path stays local, not committed); Developer Mode on.
- Claude Code: version `<fill in>`; project server approved via `.claude/settings.local.json`.
- Prompt used in each: "Show me the AlphaFold model of human haemoglobin alpha."

## Already verified (2026-11-14, MCP Inspector, Apps tab)

- The viewer renders inside a host's app sandbox with **no** outside website allowed
  (the resource declares an empty CSP).
- `show_structure` then `fetch_structure_file` end to end against the real services:
  - AlphaFold P69905 (`AF-P69905-F1`): 1 chunk, coloured by confidence.
  - PDBe `2w72` (1.3 MB unpacked): several chunks, coloured by chain.
  - PED `PED00006e001` (alpha-synuclein, 1.06 MB, 4 chunks): read as PDB although the Hub
    says mmCIF; header says "sample of an ensemble (576 conformers in all)".
- Without the MP4-encoder stub the page never starts in a sandbox (`data:` fetch blocked).

## Observations (fill in)

### Claude Desktop, chat

- [ ] Permission prompt shown?
- [ ] Viewer rendered inline? Time from prompt to first draw:
- [ ] Did the model call `show_structure` after `find_structures`, with a `model_id` it was
      given? Did it ever call `fetch_structure_file` (it should not see it)?
- [ ] Any CSP errors, blank frame, or "viewer not built" text?
- Notes / screenshot:

### Claude Code, terminal

- [ ] Model called `show_structure`; got the text summary with the file URL?
- [ ] Anything missing or confusing in the text?
- Notes:

### Claude Code, desktop app Code tab

- [ ] Rendered, text only, or an error?
- Notes:

## Result

`<renders in Desktop / falls back in Code / other>`

## Follow-ups

- If a size or timing limit shows up in Claude (the docs mention roughly 150,000 characters
  per tool result), note which file and what happened.
- If the Code tab renders the viewer, update the README's client notes.
