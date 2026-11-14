// Stand-in for the h264-mp4-encoder package (see vite.config.js). Mol* only calls this
// to export a movie, which this viewer does not offer.
export async function createH264MP4Encoder() {
  throw new Error('MP4 export is not available in this viewer');
}
