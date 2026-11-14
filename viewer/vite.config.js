// Builds the viewer into ONE self-contained HTML file inside the Python package, so the
// page needs no outside website at runtime (the server's CSP declares none).
//
// Two ideas here follow apps/structure-explorer/vite.config.ts in PDBe-MCP-Servers
// (Apache-2.0): stubbing Mol*'s MP4 encoder, and keeping third-party licence notices
// inside the bundle. No code was copied; both are written for this project.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import { viteSingleFile } from 'vite-plugin-singlefile';

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');

// Mol* and the MCP Apps client are MIT-licensed; the licence requires keeping the notice.
const notices = [
  ['molstar', read('./node_modules/molstar/LICENSE')],
  ['@modelcontextprotocol/ext-apps', read('./node_modules/@modelcontextprotocol/ext-apps/LICENSE')],
]
  .map(([name, text]) => `${name}\n${text.trim()}`)
  .join('\n\n====\n\n')
  .replaceAll('--', '- -'); // "--" may not appear inside an HTML comment

// Keep the notices as an HTML comment at the top of the page. (A bundler banner is
// dropped by the minifier in this toolchain; a comment in the HTML is not.)
const keepNotices = {
  name: 'keep-third-party-notices',
  transformIndexHtml: {
    order: 'post',
    handler: (html) => html.replace(/<!doctype html>/i, `<!doctype html>\n<!--\nThird-party notices\n\n${notices}\n-->`),
  },
};

export default defineConfig({
  resolve: {
    alias: {
      // Mol*'s MP4 export bundles an encoder that fetches a data: URL when it loads, which
      // an app sandbox's CSP blocks, and the whole page then fails to start. The viewer
      // has no movie export, so a stub that throws is enough (and saves about 1 MB).
      'h264-mp4-encoder': fileURLToPath(new URL('./src/h264-mp4-encoder-stub.js', import.meta.url)),
    },
  },
  plugins: [viteSingleFile(), keepNotices],
  build: {
    target: 'esnext',
    chunkSizeWarningLimit: 100000,
    outDir: fileURLToPath(new URL('../src/beacons_mcp/ui', import.meta.url)),
    emptyOutDir: false,
    rollupOptions: {
      input: 'viewer.html',
    },
  },
});
