import assert from 'node:assert/strict';
import { test } from 'node:test';

import { describeResult, hostMessage, payloadOf } from '../src/payload.js';

const data = { token: 'abc', model_id: 'AF-P69905-F1' };

test('reads structuredContent when the host passes it', () => {
  const result = { content: [{ type: 'text', text: 'A readable summary.' }], structuredContent: data };
  assert.deepEqual(payloadOf(result), data);
});

test('reads a JSON object from the text when structuredContent was turned into text', () => {
  // What Claude does for the model, and apparently for the app too: the structured data
  // arrives as a JSON string in content.
  const result = { content: [{ type: 'text', text: JSON.stringify(data) }] };
  assert.deepEqual(payloadOf(result), data);
});

test('structuredContent wins over text', () => {
  const result = {
    content: [{ type: 'text', text: JSON.stringify({ token: 'from-text' }) }],
    structuredContent: data,
  };
  assert.equal(payloadOf(result).token, 'abc');
});

test('skips ordinary text and finds JSON in a later part', () => {
  const result = {
    content: [
      { type: 'text', text: 'Model m1 from P (ab-initio).' },
      { type: 'image', data: '...' },
      { type: 'text', text: ` ${JSON.stringify(data)} ` },
    ],
  };
  assert.deepEqual(payloadOf(result), data);
});

test('returns null when there is nothing usable', () => {
  assert.equal(payloadOf(null), null);
  assert.equal(payloadOf(undefined), null);
  assert.equal(payloadOf('text'), null);
  assert.equal(payloadOf({}), null);
  assert.equal(payloadOf({ content: [] }), null);
  assert.equal(payloadOf({ content: [{ type: 'text', text: 'just words' }] }), null);
  assert.equal(payloadOf({ content: [{ type: 'text', text: '{ broken json' }] }), null);
  assert.equal(payloadOf({ content: [{ type: 'text', text: '[1, 2, 3]' }] }), null);
  assert.equal(payloadOf({ structuredContent: {}, content: [] }), null);
  assert.equal(payloadOf({ structuredContent: 'not an object' }), null);
});

test('a big file chunk is read the same way', () => {
  const chunk = { chunk: 0, chunks: 1, format: 'mmcif', data: 'A'.repeat(96_000) };
  assert.equal(payloadOf({ content: [{ type: 'text', text: JSON.stringify(chunk) }] }).data.length, 96_000);
});

test('describeResult says what arrived without echoing the data', () => {
  const text = describeResult({
    content: [{ type: 'text', text: 'x'.repeat(412) }],
    structuredContent: data,
    isError: false,
  });
  assert.match(text, /fields: content, structuredContent, isError/);
  assert.match(text, /text of 412 characters/);
  assert.match(text, /structuredContent: keys token, model_id/);
  assert.ok(!text.includes('abc'));
});

test('describeResult copes with odd values', () => {
  assert.match(describeResult(null), /null/);
  assert.match(describeResult('x'), /string/);
  assert.match(describeResult({}), /fields: none; content: none; structuredContent: absent/);
});

test('hostMessage repeats a plain message the host sent instead of our data', () => {
  const result = { content: [{ type: 'text', text: 'IPC not available for MCP tool call' }] };
  assert.equal(hostMessage(result), 'IPC not available for MCP tool call');
  assert.equal(payloadOf(result), null); // and it is not mistaken for data
});

test('hostMessage joins parts, skips non-text and shortens long text', () => {
  const result = {
    content: [
      { type: 'text', text: ' first ' },
      { type: 'image', data: 'x' },
      { type: 'text', text: 'second' },
    ],
  };
  assert.equal(hostMessage(result), 'first second');
  assert.equal(hostMessage({ content: [{ type: 'text', text: 'y'.repeat(500) }] }).length, 300);
  assert.equal(hostMessage({}), '');
  assert.equal(hostMessage(null), '');
});
