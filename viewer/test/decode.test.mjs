import assert from 'node:assert/strict';
import { gzipSync } from 'node:zlib';
import { test } from 'node:test';

import { base64ToBytes, decodeChunks } from '../src/decode.js';

// What the server does (src/beacons_mcp/structure_file.py): gzip, base64, split into
// chunks whose length is a multiple of 4.
function encode(text, chunkChars) {
  const encoded = gzipSync(Buffer.from(text, 'utf8')).toString('base64');
  const chunks = [];
  for (let i = 0; i < encoded.length; i += chunkChars) chunks.push(encoded.slice(i, i + chunkChars));
  return chunks;
}

test('base64ToBytes decodes bytes', () => {
  assert.deepEqual([...base64ToBytes('AAEC/w==')], [0, 1, 2, 255]);
});

test('a single chunk round-trips', async () => {
  const text = 'data_test\n_atom_site.id 1\n';
  assert.equal(await decodeChunks(encode(text, 96_000)), text);
});

test('many chunks round-trip, including a final short one', async () => {
  const text = Array.from({ length: 4000 }, (_, i) => `ATOM  ${i} line ${Math.sqrt(i)}`).join('\n');
  const chunks = encode(text, 400); // small, to force many chunks
  assert.ok(chunks.length > 5);
  assert.ok(chunks.slice(0, -1).every((c) => c.length % 4 === 0));
  assert.equal(await decodeChunks(chunks), text);
});

test('non-ASCII text survives', async () => {
  const text = 'REMARK resolution 1.07 Å\n';
  assert.equal(await decodeChunks(encode(text, 96_000)), text);
});

test('data that is not gzip is rejected', async () => {
  await assert.rejects(() => decodeChunks([Buffer.from('plain text').toString('base64')]));
});
