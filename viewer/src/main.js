// The viewer app. The host runs it in a sandboxed frame and gives it the result of the
// show_structure tool: the model's details, the web address of its file, and a token.
//
// The file reaches the page in one of two ways, tried in this order:
//   1. The page fetches it from the provider itself. This works wherever the chat app lets
//      the page connect to the provider (the server declares the providers' addresses in the
//      page's content security policy) and the provider allows it (CORS).
//   2. The page asks the server for it, through the app-only fetch_structure_file tool, in
//      chunks. This works only where the chat app lets an app call server tools (it does
//      not for a local server in the Claude Code tab: "IPC not available for MCP tool call").
// Then Mol* draws it.
import {
  App,
  applyDocumentTheme,
  applyHostStyleVariables,
} from '@modelcontextprotocol/ext-apps';
import { Viewer } from 'molstar/lib/apps/viewer/app';
import 'molstar/build/viewer/molstar.css';

import { decodeChunks } from './decode.js';
import { describeResult, hostMessage, payloadOf } from './payload.js';
import { DEFAULT_MAX_BYTES, FileProblem, loadByFetch } from './structure.js';
import './style.css';

const el = (id) => document.getElementById(id);
const app = new App({ name: 'beacons-structure-viewer', version: '0.1.0' });

let viewerPromise;
const getViewer = () =>
  (viewerPromise ??= Viewer.create('viewer', {
    layoutIsExpanded: false,
    layoutShowControls: false,
    layoutShowRemoteState: false,
    layoutShowSequence: false,
    layoutShowLog: false,
    layoutShowLeftPanel: false,
    viewportShowExpand: false,
    viewportShowSelectionMode: false,
    viewportShowAnimation: false,
  }));

function status(text) {
  el('status').textContent = text;
}

function showError(message, links = {}) {
  status('');
  el('error-text').textContent = message;
  el('error').hidden = false;
  for (const [id, url] of [
    ['open-file', links.fileUrl],
    ['open-page', links.pageUrl],
  ]) {
    const button = el(id);
    button.hidden = !url;
    button.onclick = () => app.openLink({ url });
  }
}

function clearError() {
  el('error').hidden = true;
}

const textOf = (result) =>
  (result.content ?? [])
    .filter((part) => part.type === 'text')
    .map((part) => part.text)
    .join(' ')
    .trim();

function describe(info) {
  const parts = [info.provider, info.model_id, info.category];
  if (info.span_percent != null) parts.push(`${info.span_percent}% of the protein`);
  if (info.is_sample) {
    parts.push(
      info.conformers
        ? `sample of an ensemble (${info.conformers} conformers in all)`
        : 'sample of an ensemble',
    );
  }
  return parts.join(' · ');
}

/** Say what went wrong when a result has no usable data: the host's own words if it gave any. */
function missingData(result, what) {
  const said = hostMessage(result);
  return said
    ? `${what} The chat app answered: "${said}"`
    : `${what} (What arrived: ${describeResult(result)}.)`;
}

// ---- getting the file ---------------------------------------------------------------

// The browser reports a blocked connection and a provider's CORS refusal alike as a failed
// request. A policy violation event says which one it was when the chat app blocked it.
let lastBlocked = null;
document.addEventListener('securitypolicyviolation', (event) => {
  lastBlocked = event.blockedURI || '';
});

function hostOf(url) {
  try {
    return new URL(url).host;
  } catch {
    return 'the provider';
  }
}

function explainFailedRequest(url) {
  const host = hostOf(url);
  if (lastBlocked && lastBlocked.includes(host)) {
    return `The chat app does not allow the viewer to connect to ${host}.`;
  }
  return `${host} did not let the viewer load its file (it may not allow other sites to fetch it, or it could not be reached).`;
}

async function fetchChunk(token, chunk) {
  const result = await app.callServerTool({
    name: 'fetch_structure_file',
    arguments: { token, chunk },
  });
  if (result.isError) throw new Error(textOf(result) || 'The server could not send the file.');
  const payload = payloadOf(result);
  if (typeof payload?.data !== 'string') {
    throw new Error(missingData(result, 'The viewer did not get the structure file.'));
  }
  return payload;
}

async function loadByServer(token) {
  const first = await fetchChunk(token, 0);
  const chunks = [first.data];
  for (let i = 1; i < first.chunks; i += 1) {
    status(`Fetching the structure file from the server (part ${i + 1} of ${first.chunks})…`);
    chunks.push((await fetchChunk(token, i)).data);
  }
  status('Unpacking…');
  return { text: await decodeChunks(chunks), format: first.format };
}

async function getFile(info) {
  const limit = Number(info.max_bytes) > 0 ? Number(info.max_bytes) : DEFAULT_MAX_BYTES;
  const tried = [];

  if (info.file_url) {
    status('Fetching the structure file…');
    try {
      return await loadByFetch(info.file_url, limit);
    } catch (error) {
      // A failed request is worth a second try through the server. A refused file (too big,
      // not a structure, bad status) would be refused there too.
      if (!(error instanceof FileProblem) || error.kind !== 'network') throw error;
      tried.push(explainFailedRequest(info.file_url));
    }
  }
  if (info.token) {
    status('Asking the server for the structure file…');
    try {
      return await loadByServer(info.token);
    } catch (error) {
      tried.push(`Asking the server for it did not work either: ${error?.message ?? error}`);
    }
  }
  throw new Error(tried.join(' ') || 'The viewer has no way to load this file.');
}

// ---- drawing ------------------------------------------------------------------------

/** Default look: cartoon, chains coloured for measured models, confidence for predictions. */
async function applyDefaultColours(viewer, category) {
  const theme =
    category === 'experimentally determined'
      ? 'chain-id'
      : category === 'ab-initio' || category === 'template-based'
        ? 'plddt-confidence'
        : null;
  if (!theme) return;
  try {
    const { plugin } = viewer;
    const components = plugin.managers.structure.hierarchy.current.structures.flatMap(
      (structure) => structure.components,
    );
    await plugin.managers.structure.component.updateRepresentationsTheme(components, {
      color: theme,
    });
  } catch (error) {
    // Not applicable to this file: keep Mol*'s own default colours.
    console.warn('colour theme not applied', error);
  }
}

async function show(result) {
  clearError();
  if (result.isError) {
    showError(textOf(result) || 'The structure could not be opened.');
    return;
  }
  const info = payloadOf(result);
  if (!info?.token && !info?.file_url) {
    showError(missingData(result, 'The viewer received nothing to show.'));
    return;
  }
  el('header').textContent = describe(info);
  el('header').hidden = false;

  try {
    const file = await getFile(info);
    status('Drawing…');
    const viewer = await getViewer();
    await viewer.plugin.clear();
    await viewer.loadStructureFromData(file.text, file.format, { dataLabel: info.model_id });
    await applyDefaultColours(viewer, info.category);
    drawn = true;
    status('');
  } catch (error) {
    showError(error?.message || String(error), {
      fileUrl: info.file_url,
      pageUrl: info.page_url,
    });
  }
}

// A blank frame tells the user nothing. Until something is drawn, show any error the page or
// the host connection raises (afterwards stray warnings are left alone).
let drawn = false;
const surface = (error) => {
  if (!drawn) showError(`The viewer hit a problem: ${error?.message ?? error}`);
};
window.addEventListener('error', (event) => surface(event.error ?? event.message));
window.addEventListener('unhandledrejection', (event) => surface(event.reason));
app.onerror = surface;

app.onhostcontextchanged = (context) => {
  if (context.theme) applyDocumentTheme(context.theme);
  if (context.styles?.variables) applyHostStyleVariables(context.styles.variables);
};
app.ontoolinput = () => status('Preparing the structure…');
app.ontoolresult = (result) => {
  void show(result);
};

await app.connect();
