// Hosts hand tool results to the app in different shapes. The spec says `structuredContent`;
// Claude also turns `structuredContent` into a JSON string in `content` for the model. So the
// data is read from `structuredContent` when it is there, and otherwise from a JSON object
// in a text part of `content`. Whatever the host did, the viewer gets the same object.

const isPlainObject = (value) =>
  value !== null && typeof value === 'object' && !Array.isArray(value);

/** The data object in a tool result, or null if there is none. */
export function payloadOf(result) {
  if (!isPlainObject(result)) return null;
  if (isPlainObject(result.structuredContent) && Object.keys(result.structuredContent).length) {
    return result.structuredContent;
  }
  for (const part of result.content ?? []) {
    if (part?.type !== 'text' || typeof part.text !== 'string') continue;
    const text = part.text.trim();
    if (!text.startsWith('{')) continue;
    try {
      const parsed = JSON.parse(text);
      if (isPlainObject(parsed)) return parsed;
    } catch {
      // Not JSON: an ordinary text summary. Keep looking.
    }
  }
  return null;
}

/**
 * The text a host put in a result, shortened. When a host fails it may answer with a plain
 * message instead of our data (one says "IPC not available for MCP tool call"), and the
 * viewer should repeat that message to the user, not hide it.
 */
export function hostMessage(result, limit = 300) {
  const parts = Array.isArray(result?.content) ? result.content : [];
  const text = parts
    .filter((part) => part?.type === 'text' && typeof part.text === 'string')
    .map((part) => part.text.trim())
    .filter(Boolean)
    .join(' ');
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

/** A short description of what arrived, for error messages (never the data itself). */
export function describeResult(result) {
  if (!isPlainObject(result)) return `a ${result === null ? 'null' : typeof result} value`;
  const parts = (Array.isArray(result.content) ? result.content : []).map((part) =>
    part?.type === 'text' ? `text of ${part.text?.length ?? 0} characters` : String(part?.type),
  );
  const structured = isPlainObject(result.structuredContent)
    ? `keys ${Object.keys(result.structuredContent).join(', ') || 'none'}`
    : 'absent';
  return [
    `fields: ${Object.keys(result).join(', ') || 'none'}`,
    `content: ${parts.join(', ') || 'none'}`,
    `structuredContent: ${structured}`,
    `isError: ${result.isError ?? 'unset'}`,
  ].join('; ');
}
