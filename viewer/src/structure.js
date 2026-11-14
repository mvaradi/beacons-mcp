// Loading a structure file in the page: fetch it, keep it within the size limit, unpack it
// if it is gzipped, and read its real format from its bytes (a provider's format label and
// Content-Type are not reliable). The same rules as the server's proxy path, so a file is
// accepted or refused the same way whichever route it takes.

export const DEFAULT_MAX_BYTES = 8 * 1024 * 1024;

const PDB_RECORDS = new Set([
  'HEADER', 'TITLE', 'REMARK', 'ATOM', 'HETATM', 'MODEL', 'CRYST1', 'COMPND', 'SEQRES',
  'EXPDTA', 'AUTHOR', 'SOURCE', 'KEYWDS', 'REVDAT', 'JRNL', 'DBREF', 'HELIX', 'SHEET', 'TER',
  'CONECT', 'MTRIX1', 'ORIGX1', 'SCALE1',
]);

/** A problem with a file, with a kind the caller can act on. */
export class FileProblem extends Error {
  constructor(message, kind) {
    super(message);
    this.name = 'FileProblem';
    this.kind = kind; // 'network' | 'http' | 'too-large' | 'unsupported'
  }
}

/** 'mmcif' or 'pdb', from the first meaningful line of the file. */
export function sniffFormat(bytes) {
  const head = new TextDecoder('utf-8').decode(bytes.subarray(0, 4096)).replace(/^﻿/, '');
  for (const raw of head.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    if (line.startsWith('data_')) return 'mmcif';
    if (PDB_RECORDS.has(line.slice(0, 6).trim())) return 'pdb';
    throw new FileProblem(
      `The viewer shows mmCIF and PDB files, but this file starts with "${line.slice(0, 40)}".`,
      'unsupported',
    );
  }
  throw new FileProblem('The viewer shows mmCIF and PDB files, but this file is empty.', 'unsupported');
}

function tooLarge(maxBytes) {
  const megabytes = Math.round((maxBytes / 1048576) * 100) / 100;
  return new FileProblem(`The file is larger than the ${megabytes} MB limit for the viewer.`, 'too-large');
}

/** Read a response body, stopping as soon as it passes ``maxBytes``. */
export async function readLimited(response, maxBytes) {
  const declared = Number(response.headers.get('content-length'));
  if (Number.isFinite(declared) && declared > maxBytes) throw tooLarge(maxBytes);
  const reader = response.body.getReader();
  const parts = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.length;
    if (total > maxBytes) {
      await reader.cancel();
      throw tooLarge(maxBytes);
    }
    parts.push(value);
  }
  return concat(parts, total);
}

function concat(parts, total) {
  const out = new Uint8Array(total);
  let offset = 0;
  for (const part of parts) {
    out.set(part, offset);
    offset += part.length;
  }
  return out;
}

/** Unpack gzip data, refusing anything that unpacks past ``maxBytes``. */
export async function gunzipLimited(bytes, maxBytes) {
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'));
  const reader = stream.getReader();
  const parts = [];
  let total = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.length;
      if (total > maxBytes) {
        await reader.cancel();
        throw tooLarge(maxBytes);
      }
      parts.push(value);
    }
  } catch (error) {
    if (error instanceof FileProblem) throw error;
    throw new FileProblem('The file looks gzipped but could not be unpacked.', 'unsupported');
  }
  return concat(parts, total);
}

/** Bytes to structure text: unpack if gzipped, then read the format. */
export async function toStructure(bytes, maxBytes) {
  const raw = bytes[0] === 0x1f && bytes[1] === 0x8b ? await gunzipLimited(bytes, maxBytes) : bytes;
  const format = sniffFormat(raw);
  return { text: new TextDecoder('utf-8').decode(raw), format, bytes: raw.length };
}

/**
 * Fetch ``url`` from the page itself. Fails with a FileProblem: kind 'network' when the
 * request could not be made (unreachable, blocked by the chat app's policy, or refused
 * by the provider's CORS rules: the browser does not say which), 'http' for a bad status.
 */
export async function loadByFetch(url, maxBytes = DEFAULT_MAX_BYTES, fetchFn = fetch) {
  let response;
  try {
    response = await fetchFn(url, { credentials: 'omit', redirect: 'follow' });
  } catch (error) {
    throw new FileProblem(`The request for the file failed (${error?.message ?? error}).`, 'network');
  }
  if (!response.ok) {
    throw new FileProblem(`The provider answered HTTP ${response.status} for the file.`, 'http');
  }
  return toStructure(await readLimited(response, maxBytes), maxBytes);
}
