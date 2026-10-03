/* ffmpeg.wasm client worker — the "compute load" shift.
 *
 * Runs the vendored @ffmpeg/core (WebAssembly) inside this dedicated module
 * worker so the UI never freezes. The main thread posts:
 *
 *   { type: "load" }
 *   { type: "clip", file: ArrayBuffer, args: [...], inName, outName }
 *
 * and receives:
 *   { type: "loaded" } | { type: "progress", ratio } | { type: "log", text }
 *   { type: "done", data: ArrayBuffer } | { type: "error", message }
 *
 * The core is driven directly (same calls the official @ffmpeg/ffmpeg worker
 * makes): createFFmpegCore({ mainScriptUrlOrBlob }) → FS.writeFile →
 * exec(...args) → FS.readFile.
 */
"use strict";

let core = null;
let loading = null;

/* Asset URLs: absolute, carrying the sandbox gateway query (if any) so the
 * wasm also routes through ?XTransformPort=PORT when the page is embedded
 * in the sandbox preview. */
function assetUrl(path) {
  return self.location.origin + path + (self.location.search || "");
}

async function loadCore() {
  if (core) return core;
  if (loading) return loading;

  loading = (async () => {
    const coreURL = assetUrl("/vendor/ffmpeg/ffmpeg-core.js");
    const wasmURL = assetUrl("/vendor/ffmpeg/ffmpeg-core.wasm");
    const module = await import(coreURL);
    const factory = module.default;
    if (typeof factory !== "function") {
      throw new Error("ffmpeg-core.js did not export a factory");
    }
    /* locateFile hack understood by the patched core build: the wasm URL is
     * base64-encoded into the fragment of mainScriptUrlOrBlob. */
    const mainScriptUrlOrBlob =
      coreURL + "#" + btoa(JSON.stringify({ wasmURL, workerURL: wasmURL }));
    core = await factory({ mainScriptUrlOrBlob });
    core.setLogger(function (entry) {
      self.postMessage({ type: "log", text: entry && entry.message ? entry.message : String(entry) });
    });
    core.setProgress(function (data) {
      const ratio = data && data.progress != null && isFinite(data.progress)
        ? Math.max(0, Math.min(1, data.progress))
        : null;
      if (ratio != null) self.postMessage({ type: "progress", ratio: ratio });
    });
    return core;
  })();

  try {
    return await loading;
  } finally {
    loading = null;
  }
}

/* Write the source into the wasm filesystem. OPFS Files are streamed in
 * 16 MB slices so peak main-heap usage stays ~file + one slice (never 2x);
 * plain ArrayBuffers (tests, small sources) are written in one call. */
async function writeInput(ffmpeg, inName, file) {
  if (typeof Blob !== "undefined" && file instanceof Blob) {
    const SLICE = 16 * 1024 * 1024;
    const fd = ffmpeg.FS.open(inName, "w+");
    try {
      for (let off = 0; off < file.size; off += SLICE) {
        const buf = new Uint8Array(await file.slice(off, off + SLICE).arrayBuffer());
        ffmpeg.FS.write(fd, buf, 0, buf.length, off);
      }
    } finally {
      ffmpeg.FS.close(fd);
    }
  } else {
    ffmpeg.FS.writeFile(inName, new Uint8Array(file));
  }
}

async function runClip(msg) {
  const ffmpeg = await loadCore();
  const inName = msg.inName || "in.mp4";
  const outName = msg.outName || "out.mp4";

  await writeInput(ffmpeg, inName, msg.file);
  try {
    ffmpeg.setTimeout(-1);
    ffmpeg.exec.apply(ffmpeg, msg.args.concat([outName]));
    const ret = ffmpeg.ret;
    ffmpeg.reset();
    if (ret !== 0) {
      throw new Error("ffmpeg exited with code " + ret + " (see the log lines)");
    }
    const data = ffmpeg.FS.readFile(outName);
    const copy = new Uint8Array(data); // ensure a plain, transferable buffer
    try {
      ffmpeg.FS.unlink(outName);
      ffmpeg.FS.unlink(inName);
    } catch (e) {
      /* best effort cleanup */
    }
    return copy.buffer;
  } catch (err) {
    try {
      ffmpeg.FS.unlink(outName);
      ffmpeg.FS.unlink(inName);
    } catch (e) {
      /* ignore */
    }
    throw err;
  }
}

self.onmessage = async function (event) {
  const msg = event.data || {};
  try {
    if (msg.type === "load") {
      await loadCore();
      self.postMessage({ type: "loaded" });
    } else if (msg.type === "clip") {
      const data = await runClip(msg);
      self.postMessage({ type: "done", data }, [data]);
    } else {
      throw new Error("unknown message type: " + msg.type);
    }
  } catch (err) {
    self.postMessage({ type: "error", message: String((err && err.message) || err) });
  }
};
