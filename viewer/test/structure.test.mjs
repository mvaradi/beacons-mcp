import assert from 'node:assert/strict';
import { gzipSync } from 'node:zlib';
import { test } from 'node:test';

import {
  FileProblem,
  gunzipLimited,
  loadByFetch,
  readLimited,
  sniffFormat,
  toStructure,
} from '../src/structure.js';

const enc = (text) => new TextEncoder().encode(text);
const MMCIF = 'data_AF-P69905-F1\n#\nloop_\n_atom_site.id\n1\n';
const PDB = 'ATOM      1  N   MET A   1      11.104  13.207   9.000  1.00 90.00           N\n';
const ok = (body, headers = {}) => async () => new Response(body, { status: 200, headers });

test('the format is read from the bytes', () => {
  assert.equal(sniffFormat(enc(MMCIF)), 'mmcif');
  assert.equal(sniffFormat(enc('\n\n# comment\ndata_x\n')), 'mmcif');
  assert.equal(sniffFormat(enc('﻿data_x\r\n_a 1\r\n')), 'mmcif');
  assert.equal(sniffFormat(enc(PDB)), 'pdb');
  assert.equal(sniffFormat(enc('MODEL      1\nATOM      1\nENDMDL\n')), 'pdb');
  assert.equal(sniffFormat(enc('HEADER    OXYGEN TRANSPORT\n')), 'pdb');
});

test('other content is refused as unsupported', () => {
  for (const text of ['', '  \n\n', '# only a comment\n', '<!DOCTYPE html><html>', '{"detail":"x"}', 'just words\n']) {
    assert.throws(() => sniffFormat(enc(text)), (e) => e instanceof FileProblem && e.kind === 'unsupported', text);
  }
});

test('a fetched file comes back as text with its real format', async () => {
  const result = await loadByFetch('https://files.example.org/m.cif', 1000, ok(MMCIF));
  assert.deepEqual(result, { text: MMCIF, format: 'mmcif', bytes: MMCIF.length });
});

test('PDB text labelled as something else is still read as PDB', async () => {
  // e.g. a provider serving PDB text with an mmCIF content type
  const result = await loadByFetch('https://x.example/m', 1000, ok(PDB, { 'content-type': 'chemical/x-mmcif' }));
  assert.equal(result.format, 'pdb');
});

test('a gzipped file is unpacked first', async () => {
  const result = await loadByFetch('https://x.example/m.gz', 10_000, ok(gzipSync(Buffer.from(MMCIF))));
  assert.equal(result.format, 'mmcif');
  assert.equal(result.text, MMCIF);
});

test('a file over the limit is refused', async () => {
  await assert.rejects(
    () => loadByFetch('https://x.example/m', 100, ok('x'.repeat(500))),
    (e) => e.kind === 'too-large' && /limit/.test(e.message),
  );
});

test('a declared length over the limit is refused without reading the body', async () => {
  let read = false;
  // highWaterMark 0: the stream must not be pulled until something actually reads it.
  const body = new ReadableStream(
    { pull(c) { read = true; c.enqueue(enc('x')); c.close(); } },
    { highWaterMark: 0 },
  );
  await assert.rejects(
    () => loadByFetch('https://x.example/m', 100, ok(body, { 'content-length': '99999' })),
    (e) => e.kind === 'too-large',
  );
  assert.equal(read, false);
});

test('reading stops soon after the limit is passed', async () => {
  let pulled = 0;
  const body = new ReadableStream({ pull(c) { pulled += 1; c.enqueue(enc('x'.repeat(100))); } });
  await assert.rejects(() => readLimited(new Response(body), 1000), (e) => e.kind === 'too-large');
  assert.ok(pulled < 50, `pulled ${pulled} chunks`);
});

test('a gzip bomb is refused', async () => {
  const bomb = gzipSync(Buffer.from(`data_x\n${'A'.repeat(5_000_000)}`));
  assert.ok(bomb.length < 10_000);
  await assert.rejects(() => gunzipLimited(bomb, 1_000_000), (e) => e.kind === 'too-large');
  await assert.rejects(() => toStructure(bomb, 1_000_000), (e) => e.kind === 'too-large');
});

test('broken gzip is refused as unsupported', async () => {
  const broken = new Uint8Array([0x1f, 0x8b, 1, 2, 3, 4, 5]);
  await assert.rejects(() => toStructure(broken, 1000), (e) => e.kind === 'unsupported');
});

test('an HTML error page is refused as unsupported', async () => {
  await assert.rejects(
    () => loadByFetch('https://x.example/m', 1000, ok('<html>404 not found</html>')),
    (e) => e.kind === 'unsupported' && /starts with/.test(e.message),
  );
});

test('a bad status is an http problem', async () => {
  const notFound = async () => new Response('no', { status: 404 });
  await assert.rejects(
    () => loadByFetch('https://x.example/m', 1000, notFound),
    (e) => e.kind === 'http' && /HTTP 404/.test(e.message),
  );
});

test('a failed request (blocked, CORS, unreachable) is a network problem', async () => {
  const blocked = async () => { throw new TypeError('Failed to fetch'); };
  await assert.rejects(
    () => loadByFetch('https://x.example/m', 1000, blocked),
    (e) => e.kind === 'network' && /Failed to fetch/.test(e.message),
  );
});

test('the request sends no credentials', async () => {
  let seen;
  await loadByFetch('https://x.example/m', 1000, async (url, init) => {
    seen = init;
    return new Response(MMCIF);
  });
  assert.equal(seen.credentials, 'omit');
});
