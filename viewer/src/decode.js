// The server sends a structure file as gzip + base64 in chunks. Each chunk is valid
// base64 on its own, so they are decoded one by one and the bytes joined before unzipping.

export function base64ToBytes(base64) {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

/** Join and unzip the chunks (an array of base64 strings) into the file's text. */
export async function decodeChunks(chunks) {
  const gzipped = new Blob(chunks.map(base64ToBytes));
  const stream = gzipped.stream().pipeThrough(new DecompressionStream('gzip'));
  return new Response(stream).text();
}
