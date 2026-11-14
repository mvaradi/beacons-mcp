// The viewer app. The host runs it in a sandboxed frame and gives it the result of the
// show_structure tool. It trades the token in that result for the file, in chunks, through
// the app-only fetch_structure_file tool, and draws the file with Mol*.
import {
  App,
  applyDocumentTheme,
  applyHostStyleVariables,
} from '@modelcontextprotocol/ext-apps';
import { Viewer } from 'molstar/lib/apps/viewer/app';
import 'molstar/build/viewer/molstar.css';

import { decodeChunks } from './decode.js';
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

function showError(message, fileUrl) {
  status('');
  el('error-text').textContent = message;
  el('error').hidden = false;
  const button = el('open-file');
  button.hidden = !fileUrl;
  button.onclick = () => app.openLink({ url: fileUrl });
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

async function fetchChunk(token, chunk) {
  const result = await app.callServerTool({
    name: 'fetch_structure_file',
    arguments: { token, chunk },
  });
  if (result.isError) throw new Error(textOf(result) || 'The server could not send the file.');
  return result.structuredContent;
}

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
  const info = result.structuredContent;
  if (!info?.token) {
    showError('The server sent nothing to show. Ask for the structure again.');
    return;
  }
  el('header').textContent = describe(info);
  el('header').hidden = false;

  try {
    status('Fetching the structure file…');
    const first = await fetchChunk(info.token, 0);
    const chunks = [first.data];
    for (let i = 1; i < first.chunks; i += 1) {
      status(`Fetching the structure file (part ${i + 1} of ${first.chunks})…`);
      chunks.push((await fetchChunk(info.token, i)).data);
    }
    status('Unpacking…');
    const text = await decodeChunks(chunks);

    status('Drawing…');
    const viewer = await getViewer();
    await viewer.plugin.clear();
    await viewer.loadStructureFromData(text, first.format, { dataLabel: info.model_id });
    await applyDefaultColours(viewer, info.category);
    status('');
  } catch (error) {
    showError(error?.message || String(error), info.file_url);
  }
}

app.onhostcontextchanged = (context) => {
  if (context.theme) applyDocumentTheme(context.theme);
  if (context.styles?.variables) applyHostStyleVariables(context.styles.variables);
};
app.ontoolinput = () => status('Preparing the structure…');
app.ontoolresult = (result) => {
  void show(result);
};

await app.connect();
