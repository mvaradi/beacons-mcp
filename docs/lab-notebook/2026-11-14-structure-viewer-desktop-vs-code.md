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

## First attempt in a Claude app (reported by the user, 2026-11-14)

Prompt about human haemoglobin alpha; the model ran `search_proteins`, `find_structures`,
then `show_structure`. What happened:

- The app host created the widget ("Widget from beacons: Show structure") and **our page
  loaded and ran inside Claude's sandbox**. That alone is a result: the CSP, the single-file
  bundle and the MP4-encoder stub all survived Claude's host.
- The frame stayed blank and the page said "The server sent nothing to show", the message
  of the viewer's first check: it received a tool result with no data in the place it looked
  (`structuredContent.token`). The file was never requested.
- The user then saw the text **"IPC not available for MCP tool call"**. It appears nowhere in
  this repo, the Python SDK, Mol* or the MCP Apps client, so it is the Claude app's own
  message (an internal channel between its window and its main process). The surface was the
  Claude desktop app's **Code tab** (confirmed by the user), which the Claude Code docs say
  only returns text, yet it drew a widget frame and ran our page.
- Separately: in Claude the **model is given `structuredContent` as a JSON string instead
  of `content`** (seen directly: a `show_structure` call from a Claude Code session returned
  the JSON, token included, not the text summary). `fetch_structure_file` (app-only) was
  correctly absent from the model's tool list.

Changes made in response: the viewer now reads its data from `structuredContent` or, failing
that, a JSON object in the text; when a host sends something else it shows the host's own
words (or a description of what arrived) instead of a vague message, and it surfaces page
errors instead of staying blank; `show_structure`'s structured data now includes a `summary`
so the model sees the readable text.

## Second attempt, Code tab, with the tolerant viewer (2026-11-14)

- The header line now appears: "AlphaFold DB · AF-P69905-F1 · ab-initio · 100% of the
  protein". So the app **does receive the first tool result** and the new reader finds the
  data in it. (The first attempt's "nothing to show" most likely came from the data arriving
  as JSON text rather than as `structuredContent`; both are now accepted.)
- Below it the viewer's error line reads **"IPC not available for MCP tool call"**, with the
  "Open the file's web address" button. That line is the viewer's handler for the file fetch,
  so the failing step is the app's own `callServerTool("fetch_structure_file")`: **the Code
  tab does not run app-initiated tool calls for this (local stdio) server.**
- This matches Anthropic's docs: app tools "need a connector that's backed by a remote
  server" (<https://claude.com/docs/connectors/building/mcp-apps/getting-started.md>).
- The model still told the user it "opened the model in the 3D viewer": it cannot see the
  widget fail. The `summary` now says the viewer opens only in chat apps that support it.

Consequence for the design, decided with the user: the file proxy through an app-only tool
cannot work for a local stdio server in the Code tab. The viewer now **fetches the file
itself** from the provider, which needs two things: the chat app must apply the viewer's
declared `connectDomains`, and the provider must send CORS headers (three of ten do not;
accepted). The server proxy stays as a second route for hosts that run app tool calls.

Anthropic's docs say `connectDomains` entries must start with `https://`, are applied one by
one, and that `Access-Control-Allow-Origin: *` suffices for public APIs; they do not say
whether a local server is treated differently. **That is the open question for the Code
tab.** If the chat app ignores the declared origins, the viewer will say "The chat app does
not allow the viewer to connect to <host>" (it listens for the policy-violation event).

Verified in the MCP Inspector (a host that applies the policy and runs app tool calls):

- AlphaFold P69905: loaded directly by the page, with no `fetch_structure_file` call and no
  server download.
- PED `PED00006e001` (no CORS headers): the direct fetch failed, the page fell back to the
  server route, and the ensemble sample drew correctly.

Also seen in the Code tab: the model asked which protein to use in plain text instead of
the app's question tool. The server does not control that choice; the note on an ambiguous
search now tells the model to use an interactive question tool if it has one.

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
