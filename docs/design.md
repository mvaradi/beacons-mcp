# Design notes

The README says what the server does and why. This file holds the detail behind each
design decision: what the code does, and the reasoning. Dates are as of November 2026.

Contents:

1. [Choosing which models to show](#choosing-which-models-to-show)
2. [Asking which protein](#asking-which-protein)
3. [`show_structure` takes a `model_id`, never a URL](#show_structure-takes-a-model_id-never-a-url)
4. [How a structure reaches the viewer](#how-a-structure-reaches-the-viewer)
5. [Provider descriptions and the registry](#provider-descriptions-and-the-registry)
6. [Provider CORS check](#provider-cors-check)
7. [Logging and stdout](#logging-and-stdout)
8. [Public SDK only](#public-sdk-only)
9. [Where it has been tried](#where-it-has-been-tried)

## Choosing which models to show

A protein can have hundreds of models. `find_structures` returns totals for all of them and a
selection of up to `max_results`, chosen like this:

- The experimental model that spans the most of the protein comes first, even if it is small,
  so measured evidence is always represented.
- The rest are ordered by how much of the protein they span. Ties go to experimental models,
  then to better resolution or confidence.
- The slots are shared round-robin across providers (each provider's best-spanning model,
  then each one's next best, and so on), so that one provider with hundreds of models cannot
  crowd out the others. The first model counts as its provider's turn.
- Totals always cover every model the Hub returned, and the number left out is reported.

For a long protein such as CREB-binding protein (Q92793, 2442 residues), whose experimental
structures are domains, this puts a full-length prediction ahead of further fragments.

**Rows are the Hub's.** Each Hub item is reported as the Hub lists it, so an entry the Hub
lists once per residue segment appears once per segment, and totals match the Hub's own
counts. `span_percent` is the one derived number: the share of the protein between the first
and last residue of the reported range. The Hub's own `coverage` field is not used, because it
disagrees with the residue range for some providers (an ensemble spanning 8% of a protein
reports 1.0).

**Conformational ensembles** (models that report a conformer count) come with a note: the file
URL serves only a sample (the first models) of the ensemble, and the format the Hub reports
may not match the file.

Categories are the Hub's own strings, lower-cased: `experimentally determined`,
`conformational ensemble`, `template-based` and `ab-initio`. An unknown category is passed
through and sorts last.

## Asking which protein

`search_proteins` needs an organism and never assumes one: the tool description tells the model
to ask the user. The organism is resolved to an NCBI taxonomy ID (`taxonomy.py`,
`data/organisms.json`) and the search filters on `organism_id`, never on a name: "mouse" is a
substring of "Mouse-ear cress". Names not in that file are looked up in the UniProt taxonomy
search and accepted only on one exact scientific- or common-name match at species rank or
below; zero or several matches are an error that lists the options. "any" is an error too: the
organism is never assumed. The result reports what was searched, e.g.
`mouse → Mus musculus (taxon 10090)`. The filter is exact (`organism_id`), so a species-level ID
does not include entries filed under its strains.

When a search is ambiguous (several reviewed entries, or only unreviewed ones), the server
asks the user to choose, using MCP
[elicitation](https://modelcontextprotocol.io/specification/latest/client/elicitation): a small
form with up to five candidates, labelled like `P69905 - Hemoglobin subunit alpha (HBA_HUMAN)`.
Labels are plain ASCII, because terminals differ in how they show anything else. When there are
several reviewed entries only those are offered. When the list is only part of the matches
(UniProt reports the total), the form says so and tells the user to decline if theirs is not
there.

- **Accepted:** the result holds only the user's choice, with `confirmed_by_user: true` and a
  note not to ask again. The flag is set only after the user accepted a pick, so the model
  cannot skip the question or guess.
- **Declined or cancelled:** the result keeps all candidates and tells the model to ask.
- **Client without elicitation, or a dialog that fails:** the same result as for a decline.
- A single clear match (one reviewed entry, even among unreviewed ones) never opens a dialog.

The note on every ambiguous result tells the model how to ask: "use your interactive question
tool if you have one (one option per candidate, showing its accession, protein name and gene),
otherwise ask in chat". The server cannot force that choice. In the Code tab in Claude Desktop
the dialog is not offered, and the model used its own question tool with the candidates as
options (see the [lab notebook](lab-notebook/2026-11-14-structure-viewer-desktop-vs-code.md)).
The server logs on stderr whether the client declared support for elicitation.

## `show_structure` takes a `model_id`, never a URL

The tool arguments come from the model, and the model can be steered by whatever text it has
read. The server runs on the user's machine, so a model-supplied URL could point it at localhost
or the private network. Instead `show_structure(uniprot_accession, model_id)` asks the Hub again
on every call and only ever uses a file URL that the Hub returned for that accession. Inputs
are validated first: the accession by UniProt's format (version suffixes such as `P69905.1` are
rejected because the Hub answers them with a partial result), and the model ID by length (200
characters).

The result carries a readable summary twice: as text, and as a `summary` field in the
structured data. In Claude the model is given the structured data as a JSON string in place of
the text, so the summary has to be in the structured data for the model to read it.

## How a structure reaches the viewer

The viewer is an [MCP Apps](https://modelcontextprotocol.io/extensions/apps/overview) page: one
self-contained HTML file served as the resource `ui://beacons/viewer` and drawn with
[Mol*](https://molstar.org). The point is that the file goes from the provider to the page,
and not through the model. That holds for the direct route. For the server route the chunks
travel as results of the app's own tool calls, and Anthropic's documentation does not say
whether such results reach the model's context, so that is unverified (and the route does not
work in the Code tab anyway). `show_structure` returns only the model's details, the file's web
address, the size limit and a token.

The page gets the file in this order:

1. **It fetches the file from the provider itself.** The viewer's content security policy
   (CSP) allows connections to the providers' service hosts and nowhere else. Those hosts come
   from the [3D-Beacons registry](https://github.com/3D-Beacons/3d-beacons-registry): the
   bundled snapshot at first, then the live registry once `show_structure` has run, so a
   provider added later is picked up without a code change. The CSP declares no outside
   scripts, styles, images or fonts. This works where the chat app applies the declared
   origins and the provider sends CORS headers.
2. **Otherwise the server fetches it.** If the first attempt fails (the provider sends no CORS
   headers, or the chat app blocks the connection), the page asks the app-only
   `fetch_structure_file` tool for the file, in compressed base64 chunks of 96,000 characters,
   because tool results are limited in size (Anthropic documents about 150,000 characters). This
   only works where the chat app lets an app call server tools, which it does not for a local
   server in the Code tab in Claude Desktop (it answers "IPC not available for MCP tool call").
3. **If both fail,** the viewer says why in plain words and offers buttons for the file's web
   address and the entry page. It also names the host when the chat app's policy blocked the
   connection.

Why this order: the server route was built first and is the more controlled one (the server
downloads, checks and compresses the file), but it needs a host that runs app-initiated tool
calls. The direct route works in more hosts but depends on provider CORS headers, so each route
covers the other's gap.

**What the page and the server check, whichever route is used:**

- The real format is read from the file's bytes, not from the Hub's label or the
  `Content-Type`, neither of which can be trusted (one provider labels PDB text as mmCIF; others
  send mmCIF as `text/plain` and PDB as `application/x-pilot`). Only mmCIF and PDB text are
  accepted. BinaryCIF, HTML error pages and anything else are refused with the file's address.
- Gzip is unpacked within the size limit (so a small file that unpacks huge is refused).
- Files over 8 MB (unpacked) are refused, with the address to download from. The limit is
  `BEACONS_MCP_MAX_FILE_MB`. The page enforces it too, and stops reading once it is passed.

**The server's own download** (the second route) only reaches public web addresses: never
localhost or private networks, link-local or cloud-metadata addresses, and the same holds for
every redirect (at most five). A host name is checked by resolving it, and one private answer
is enough to refuse it. The size limit is applied while streaming. One limit: the name is
resolved here to check it and again by the HTTP client to connect, so an attacker who controls
DNS could in principle answer differently the second time.

**Tokens.** The token from `show_structure` lasts 15 minutes and names one Hub-resolved model
(its file URL and details). At most 16 are kept, oldest dropped first. A prepared file is
kept with its token, so it is downloaded once.

**The page.** It is built by Vite from `viewer/` (Mol* and the MCP Apps client) into one HTML
file of about 5 MB that is not committed. The build stubs out Mol*'s MP4 encoder, which fetches
a `data:` URL when it loads: an app sandbox's CSP blocks that and the whole page then fails to
start. The third-party licence notices are kept as a comment at the top of the page. Experimental
models are coloured by chain and predicted models (`ab-initio` and `template-based`) by
confidence (the `plddt-confidence` theme). If applying the theme throws, Mol*'s default colours
are kept. Ensembles keep the default colours. The confidence colouring has been checked
visually only on an AlphaFold model. For other predicted files Mol* reads the B-factor column as
confidence, which may not be what the provider stored there.

**Reading results.** Hosts hand the app a tool result in different shapes. Claude gives the
model the structured data as JSON text, and the app appears to receive it in the same form
(the first attempt in the Code tab found no data where the viewer looked). So the viewer
reads `structuredContent`, and failing that a JSON object in the text. When a host sends something
else it shows the host's own words (one answers "IPC not available for MCP tool call"), not a
vague message, and it surfaces page errors instead of staying blank.

**App-only visibility.** `fetch_structure_file` is marked `_meta.ui.visibility: ["app"]`. The
MCP Apps specification requires hosts to keep such a tool out of the model's tool list, and in
the Code tab session it was absent from the list. That keeps the model from calling it and
keeps the file chunks out of its context. It is a label that the host enforces, not a security
boundary: the tool still needs a valid token.

**The CSP follows the registry.** The resource's `_meta` is built from the snapshot when the
server starts and widened to the live registry's hosts when `show_structure` runs. The SDK
registers a copy of a resource that has no explicit `mime_type`, and a later update would then
be ignored. So the resource sets it, and a test pins the behaviour. Only `https` origins with
public host names are accepted from the registry. This assumes that a provider serves its model
files from the same host as its service, which held for every provider in the data checked; one
that served files from another host would be blocked until its host is listed.

## Provider descriptions and the registry

`find_structures` returns no provider descriptions (they were about 1.4 kB per result for
little value); `by_provider` has the name, the count and, for a few providers whose models read
differently, a one-line `caveat` from `data/provider_caveats.json`.

The registry (`resources/registry.json`) supplies the origins the viewer may load files from.
It is fetched when `show_structure` runs, cached for six hours, and never allowed to fail a call.
The fetch times out after five seconds. If it fails, the last good copy is used, or the bundled
snapshot (`src/beacons_mcp/data/registry_providers.json`) if there never was one, and the
failure is not retried for five minutes. Caveats match the Hub's display name against the
caveat file's keys after lower-casing and dropping punctuation, on the whole name. No provider name or address appears in the code; they are data.
A test scans the source to keep it that way.

## Provider CORS check

Whether the page can load a file itself depends on the provider sending
`Access-Control-Allow-Origin`. On 14 November 2026 I requested one model file per provider from
the Hub's results for P69905 and Q92793 with an `Origin` header and read the response headers:

| Provider | `Access-Control-Allow-Origin` |
|---|---|
| PDBe | `*` |
| SWISS-MODEL | `*` |
| AlphaFold DB | `*` |
| SASBDB | `*` |
| AlphaFill | `*` |
| isoform.io | `*` |
| ModelArchive | `*` |
| PED | none |
| levylab | none |
| Bindome | none |

So three of the ten providers that appeared send no CORS headers. The registry lists 13
providers; the other three did not appear in the data I checked. This is a snapshot: a
provider can change its headers, so the check should be repeated before relying on it.

The registry's service hosts covered every file host in the Hub data for P69905, Q92793 and
P37840 (a test checks this against saved responses), which is why they can be used as the
CSP's allowed origins.

## Logging and stdout

On the stdio transport the protocol itself travels over stdout, one JSON message per line.
A stray `print` or log line there corrupts the stream and the client drops the connection. So
all logging goes to stderr. The SDK installs its own log handler when the server is built, so
`configure_logging()` replaces it (`force=True`). A test fails if any tool writes to stdout.

## Public SDK only

The server uses the MCP Python SDK's public API: `Apps.tool`, `Apps.add_resource`,
`ResourceCsp` and `MCPServer(extensions=[...])`. Code that works around a gap by patching the
SDK's internals breaks when the SDK changes, so a test fails on any import of a private SDK
module, any use of a private attribute, or any monkeypatching of the SDK.

## Where it has been tried

See the [lab notebook](lab-notebook/2026-11-14-structure-viewer-desktop-vs-code.md) for what
was tried in which app, what failed, and what was learnt. In short, as of November 2026:

| Where | Result |
|---|---|
| MCP Inspector | Everything works, including both file routes |
| The Code tab in Claude Desktop | Tools work. The model asks which protein using its own question tool. The viewer renders through the direct route (seen with a PDBe model); the server route is unavailable |
| Claude Code (terminal) | Not tried. Anthropic documents that it shows text only |
| Claude Desktop (chat) | Not tried |
| claude.ai | Cannot use this server: it runs over stdio and claude.ai needs a remote server |
