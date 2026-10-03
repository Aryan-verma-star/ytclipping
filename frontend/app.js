/* YouTube Clipper — Phase 3.5 editor UI: instant load.
 *
 * Flow: paste URL → the backend resolves a direct stream + duration → the
 * player + timeline render IMMEDIATELY (video plays through the backend
 * proxy) while the full-quality file downloads in the background (progress
 * pill + timeline fill) → filmstrip thumbnails arrive when the cache lands
 * → the clip job cuts from that cache. Talks to the FastAPI backend on the
 * same origin. Inside the build-sandbox gateway (?XTransformPort=PORT)
 * every request and media URL transparently carries the same query
 * parameter; in production the parameter is absent.
 */
"use strict";

var GATEWAY_PORT = new URLSearchParams(window.location.search).get("XTransformPort") || "";
var API_BASE = String(window.CLIPPER_API_BASE || "").replace(/\/+$/, "");

function apiUrl(path) {
  if (API_BASE) return API_BASE + path; // split deploy: absolute backend URL
  if (!GATEWAY_PORT) return path;      // same-origin production
  var url = new URL(path, window.location.origin); // build-sandbox gateway
  url.searchParams.set("XTransformPort", GATEWAY_PORT);
  return url.pathname + url.search;
}

function request(path, options) {
  var opts = options || {};
  opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  return fetch(apiUrl(path), opts).then(function (response) {
    return response.json().catch(function () {
      return null;
    }).then(function (body) {
      if (!response.ok) {
        var err = (body && body.error) || {};
        var details = (err.details || [])
          .map(function (d) {
            return (d.field || d.param || "") + ": " + d.message;
          })
          .join("\n");
        var message = [err.message || "HTTP " + response.status, details].filter(Boolean).join("\n");
        var error = new Error(message);
        error.status = response.status;
        throw error;
      }
      return body;
    });
  });
}

/* ------------------------------ formatting ------------------------------ */

function fmtShort(t) {
  t = Math.max(0, Number(t) || 0);
  var h = Math.floor(t / 3600);
  var m = Math.floor((t % 3600) / 60);
  var s = t - 60 * (m + 60 * h);
  var ss = s < 10 ? "0" + s.toFixed(1) : s.toFixed(1);
  return h > 0 ? h + ":" + String(m).padStart(2, "0") + ":" + ss : m + ":" + ss;
}

function fmtTimecode(s) {
  var total = Math.max(0, Math.round(Number(s) || 0));
  var h = String(Math.floor(total / 3600)).padStart(2, "0");
  var m = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
  var sec = String(total % 60).padStart(2, "0");
  return h + ":" + m + ":" + sec;
}

function fmtBytes(b) {
  if (b == null) return "—";
  if (b > 1048576) return (b / 1048576).toFixed(1) + " MB";
  if (b > 1024) return (b / 1024).toFixed(0) + " KB";
  return b + " B";
}

function fmtDate(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

function $(id) {
  return document.getElementById(id);
}

function el(tag, cls, text) {
  var node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

/* --------------------------------- state --------------------------------- */

var state = {
  meta: null,
  styles: [],
  preview: null, // latest preview payload (may still be downloading)
  urlAtPreviewLoad: "", // raw input value when the preview was loaded
  playerPreviewId: "", // preview id the <video> src is currently bound to
  timeline: null,
  jobTimer: null,
  previewTimer: null,
  previewToken: 0,
  reelsRunning: false,
  rafId: 0,
  /* browser-engine state (engine === "browser") */
  engine: "server", // "browser" | "server" — toggled via the header chip
  client: null, // { info, file, url } while a client-engine source is loaded
  clientCacheQueue: Promise.resolve(), // serializes OPFS downloads
};

/* --------------------------- browser engine ------------------------------ */

/* The browser engine is usable when the client engine loaded AND a
 * resolution path is actually reachable: the built-in vidssave resolver
 * (works from any residential browser — no setup), the companion extension,
 * an explicitly configured Cloudflare Worker proxy, or the sandbox dev
 * proxy. */
function clientEngineUsable() {
  if (!window.ClientEngine || !window.YTResolver) return false;
  if (window.VidsSave && window.aesjs) return true; // vidssave path
  if (window.__YTCP__ && window.__YTCP__.version === 1) return true;
  if (String(window.CLIPPER_YT_PROXY || "").trim()) return true;
  if (GATEWAY_PORT) return true; // sandbox preview + node proxy/dev-server.mjs
  return false;
}

function updateEngineChip() {
  var chip = $("engine-chip");
  if (!chip) return;
  var usable = clientEngineUsable();
  var browser = state.engine === "browser";
  chip.textContent = "engine: " + (browser ? "browser" : "server");
  chip.classList.toggle("active", browser);
  chip.title = browser
    ? "Browser engine: uploaded files render locally with ffmpeg.wasm (never uploaded). YouTube links are handled by the server by default — vidssave's download links are network-bound, so a hosted backend cannot relay them; forcing the browser engine for YouTube works on local/same-IP setups or with the companion extension. Click to switch the server engine."
    : usable
      ? "Server engine active. Click to switch to the browser engine (uploads render on your device; YouTube resolves in your browser — needs a local/same-IP backend or the companion extension)."
      : "Server engine active. The browser engine needs the companion extension or a configured proxy (see proxy/DEPLOY.md).";
}

function clientClipName() {
  var base = (state.client && state.client.info && state.client.info.title || "clip")
    .replace(/\.(mp4|webm|mov|m4v|mkv)$/i, "") // drop a file-type suffix
    .replace(/[^\w\s-]/g, "")
    .trim()
    .replace(/\s+/g, "-")
    .slice(0, 48);
  return (base || "clip") + "-9x16.mp4";
}

/* ----------------------------- preview load ----------------------------- */

var LOOKS_LIKE_YT = /(^|\s)((https?:\/\/)?((www|m|music)\.)?(youtube\.com|youtu\.be)\/\S*)/i;

function setLoadStatus(kind, text) {
  var box = $("load-status");
  box.className = "load-status " + kind;
  box.textContent = text;
}

function clearLoadStatus() {
  var box = $("load-status");
  box.className = "load-status hidden";
  box.textContent = "";
}

function loadPreview(url) {
  url = (url || "").trim();
  if (!url) return;
  /* Routing: the browser engine resolves YouTube from the USER's IP, but
   * vidssave's download links are bound to the network that created them
   * (and its CDN sends no CORS headers), so a HOSTED backend on a different
   * IP cannot relay the bytes. YouTube therefore goes to the server engine
   * (whose vidssave provider creates AND downloads the task itself) unless
   * the user explicitly chose the browser engine — right for local
   * same-IP setups and the companion extension. Uploads and direct media
   * URLs always stay client-side when the engine allows it. */
  var ytUrl = LOOKS_LIKE_YT.test(url);
  var wantsClient =
    state.engine === "browser" &&
    clientEngineUsable() &&
    (state.engineExplicit || !ytUrl);
  if (wantsClient) {
    clientLoad(url);
    return;
  }
  if (state.engine === "browser" && ytUrl && clientEngineUsable()) {
    /* fresh default on a hosted deployment — say why the server is handling it */
    setLoadStatus(
      "busy",
      "Loading via the server (its resolver works from any IP) — the browser " +
        "engine still renders uploaded files locally. Toggle the engine chip to override."
    );
  } else {
    setLoadStatus("busy", "Loading video…");
  }
  var token = ++state.previewToken;
  if (state.previewTimer) {
    clearTimeout(state.previewTimer);
    state.previewTimer = null;
  }
  $("load-btn").disabled = true;

  request("/api/previews", { method: "POST", body: JSON.stringify({ url: url }) })
    .then(function (preview) {
      if (token !== state.previewToken) return;
      if (preview.duration) {
        // dedupe hit or instant resolve — show the editor right away
        applyPreview(preview, url);
        if (preview.status === "ready") finishPreview(preview);
        else pollPreview(preview.id, token);
        return;
      }
      pollPreview(preview.id, token);
    })
    .catch(function (err) {
      if (token !== state.previewToken) return;
      $("load-btn").disabled = false;
      setLoadStatus("error", err.message);
      showManualFallbackNote();
    });
}

function pollPreview(id, token) {
  var attempt = 0;
  var failures = 0;
  var tick = function () {
    if (token !== state.previewToken) return;
    request("/api/previews/" + id)
      .then(function (preview) {
        if (token !== state.previewToken) return;
        failures = 0;
        var editorLive = state.preview && state.preview.id === id;

        if (preview.status === "failed" || preview.status === "expired") {
          $("load-btn").disabled = false;
          var message =
            (preview.error || "The preview could not be prepared.") +
            " You can still create a clip by entering the times manually below.";
          if (editorLive) {
            // the editor stays usable for playback/manual times, but clip
            // reuse must stop pointing at the dead preview
            state.preview = null;
            setCachePill("error", "Source download failed");
            showError(message);
          } else {
            setLoadStatus("error", message);
          }
          updateCacheNote();
          showManualFallbackNote();
          return;
        }

        // the moment we know the duration — or can play and let the player
        // report its own metadata — the editor goes live
        if ((preview.duration || preview.video_url) && !editorLive) {
          applyPreview(preview, $("url").value.trim());
          editorLive = true;
        } else if (editorLive) {
          updateEditorProgress(preview);
        }

        if (preview.status === "ready") {
          $("load-btn").disabled = false;
          finishPreview(preview);
          return;
        }

        if (!editorLive) {
          var labels = {
            pending: "Queued…",
            resolving: "Identifying the video…",
            downloading: "Downloading the source video…",
            streaming: "Starting the instant stream…",
            processing: "Generating timeline thumbnails…",
          };
          setLoadStatus("busy", (labels[preview.status] || "Working…") + " (" + preview.status + ")");
        }
        attempt += 1;
        if (attempt > 400) {
          $("load-btn").disabled = false;
          setLoadStatus("error", "Gave up waiting for the preview — try again.");
          return;
        }
        state.previewTimer = setTimeout(tick, editorLive ? 1500 : 700);
      })
      .catch(function (err) {
        if (token !== state.previewToken) return;
        failures += 1;
        if (failures >= 6) {
          $("load-btn").disabled = false;
          setLoadStatus("error", err.message);
          showManualFallbackNote();
          return;
        }
        state.previewTimer = setTimeout(tick, 2500);
      });
  };
  tick();
}

function showManualFallbackNote() {
  /* nothing extra needed — the clip card is always usable; just surface a hint */
  var hint = $("limits");
  if (hint && state.meta) hint.textContent = manualHintText();
}

function manualHintText() {
  return (
    "Timeline unavailable — enter start/end times manually. " +
    "Max clip " + state.meta.limits.max_clip_timecode +
    " · max source " + state.meta.limits.max_source_timecode +
    " · files kept " + state.meta.retention_hours + " h"
  );
}

/* --------------------------- file upload (own videos) ---------------------- */

/* A user-uploaded file is the one source that needs NO network at all: the
 * browser already holds the bytes, so the player, filmstrip and (in the
 * browser engine) the 9:16 render all run off the local File object. The
 * server engine only sees the file at submit time (POST /api/uploads). */
function handleFilePick(file) {
  if (!file) return;
  if (!window.ClientEngine) {
    setLoadStatus("error", "The page did not finish loading — refresh and try again.");
    return;
  }
  if (file.size === 0) {
    setLoadStatus("error", "That file is empty.");
    return;
  }
  var cap = window.ClientEngine.MAX_SOURCE_BYTES || 400 * 1048576;
  if (file.size > cap) {
    setLoadStatus(
      "error",
      "That file is " + fmtBytes(file.size) + " — the limit is " + fmtBytes(cap) +
        ". Try a smaller file."
    );
    return;
  }
  resetEditor();
  var token = ++state.previewToken;
  var fileName = file.name || "uploaded-video";
  var cleanTitle =
    fileName.replace(/\.[^.]+$/, "").replace(/[^\w\s.-]/g, "").trim() || "uploaded video";
  state.client = {
    info: {
      kind: "upload",
      title: cleanTitle,
      fileName: fileName,
      duration: null,
      transport: "upload",
    },
    file: file, // a local File — playable + clippable as-is (no OPFS hop)
    url: "upload://" + fileName,
  };
  clientApplyUpload(token);
}

/* Light the editor from the uploaded file itself: object URL for the
 * player, timeline from the player's metadata, filmstrip from the local
 * bytes. Works identically under BOTH engines (the engine only decides
 * where the final clip renders). */
function clientApplyUpload(token) {
  state.preview = null; // server preview path is off for uploads
  state.urlAtPreviewLoad = "";
  clearLoadStatus();
  $("load-btn").disabled = false;

  var info = state.client.info;
  var file = state.client.file;
  var editor = $("editor");
  editor.classList.remove("hidden");

  $("video-title").textContent = info.fileName || info.title;
  $("video-sub").textContent =
    fmtBytes(file.size) + " · uploaded file · preview runs locally";

  var saveLink = $("save-original"); // the user already has this file
  saveLink.classList.add("hidden");
  saveLink.removeAttribute("href");

  var video = $("player");
  video.removeAttribute("poster");
  video.src = URL.createObjectURL(file);
  state.playerPreviewId = "upload:" + file.name + ":" + file.size;
  video.classList.remove("dimmed");
  $("player-preparing").classList.add("hidden");

  var onMeta = function () {
    video.removeEventListener("loadedmetadata", onMeta);
    if (token !== state.previewToken || state.timeline) return;
    var d = video.duration && isFinite(video.duration) ? video.duration : 0;
    if (!d) return; // undecodable locally — manual times still work
    buildTimeline({ duration: d, thumbs: [] });
    onSelectionChange(state.timeline.selStart, state.timeline.selEnd);
    state.timeline.setProgress(0);

    setCachePill("done", "Uploaded file · " + fmtBytes(file.size));
    window.ClientEngine.warmUp(); // preload ffmpeg.wasm while picking a range
    setCachePill("busy", "Generating thumbnails…");
    window.ClientEngine.generateThumbs(file, d, 60).then(
      function (thumbs) {
        if (token !== state.previewToken) return;
        if (state.timeline && thumbs && thumbs.length) state.timeline.setThumbs(thumbs);
        setCachePill("done", "Uploaded file · " + fmtBytes(file.size));
      },
      function () {
        if (token !== state.previewToken) return;
        setCachePill("done", "Uploaded file · " + fmtBytes(file.size)); // cosmetic only
      }
    );
  };
  video.addEventListener("loadedmetadata", onMeta);
  if (video.readyState >= 1) onMeta();

  startReelsPreview();
  updateTransport();
  updateClientCacheNote();
  editor.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

/* Client-engine variant of loadPreview: resolve the stream in the browser
 * (innertube via the transport), light the editor up immediately with the
 * playable URL, then cache the source to OPFS and draw the filmstrip from
 * the local file. */
function clientLoad(url) {
  var token = ++state.previewToken;
  if (state.previewTimer) {
    clearTimeout(state.previewTimer);
    state.previewTimer = null;
  }
  $("load-btn").disabled = true;
  setLoadStatus("busy", "Resolving in your browser…");
  state.client = { info: null, file: null, url: url };

  window.ClientEngine.resolve(url)
    .then(function (info) {
      if (token !== state.previewToken) return;
      state.client.info = info;
      clientApplyInfo(info);
      clientCache(info, token);
    })
    .catch(function (err) {
      if (token !== state.previewToken) return;
      state.client = null;
      $("load-btn").disabled = false;
      var hint = "";
      if (err && err.status_code === "analyze_risk") {
        hint = " — upload the video file directly instead.";
      } else if (err && err.exhausted) {
        hint = " — switch the engine to server, or try the companion extension.";
      }
      setLoadStatus("error", err.message + hint);
    });
}

/* Client-engine variant of applyPreview. */
function clientApplyInfo(info) {
  state.preview = null; // server preview path is off in browser mode
  state.urlAtPreviewLoad = $("url").value.trim();
  clearLoadStatus();
  $("load-btn").disabled = false;

  var editor = $("editor");
  editor.classList.remove("hidden");

  $("video-title").textContent = info.title || "Source video";
  var bits = [];
  if (info.duration) bits.push(fmtShort(info.duration));
  if (info.height) bits.push(info.height + "p");
  bits.push("via browser · " + info.transport + (info.resolvedWith ? " (" + info.resolvedWith + ")" : ""));
  $("video-sub").textContent = bits.join(" · ");

  buildTimeline({ duration: info.duration || 0, thumbs: [] });
  if (state.timeline) state.timeline.setProgress(0);

  var video = $("player");
  state.playerPreviewId = "client:" + (info.videoId || info.directUrl);
  if (info.thumbnail && !video.getAttribute("poster")) {
    video.poster = info.thumbnail; // vidssave meta arrives before the URL
  }
  if (info.playbackUrl) {
    video.src = info.playbackUrl;
    video.classList.remove("dimmed");
    $("player-preparing").classList.add("hidden");
  } else {
    /* vidssave: no playable URL until the muxing task finishes — the
     * timeline is already fully usable (duration known from the parse);
     * clientCache lights the player up the moment the link lands. */
    video.removeAttribute("src");
    video.classList.add("dimmed");
    $("player-preparing").classList.remove("hidden");
  }
  updateSaveOriginalLink(info);

  // direct files may not know their duration until the player decodes it
  if (!state.timeline) {
    var onMeta = function () {
      video.removeEventListener("loadedmetadata", onMeta);
      if (
        state.client &&
        state.client.info === info &&
        !state.timeline &&
        video.duration &&
        isFinite(video.duration)
      ) {
        buildTimeline({ duration: video.duration, thumbs: [] });
        state.timeline.setSelection(0, Math.min(15, video.duration));
        onSelectionChange(state.timeline.selStart, state.timeline.selEnd);
        state.timeline.setProgress(0);
      }
    };
    video.addEventListener("loadedmetadata", onMeta);
    if (video.readyState >= 1) onMeta();
  }

  startReelsPreview();
  updateTransport();
  setCachePill("busy", info.kind === "vidssave" ? "Preparing file…" : "Caching in your browser…");
  updateClientCacheNote();
  editor.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

/* "Save original" — a plain <a> to the remote file. Browser navigations
 * ignore CORS, so vidssave's redirect link downloads natively without the
 * backend proxy (zero server bandwidth). Hidden until a URL exists. */
function updateSaveOriginalLink(info) {
  var bar = $("video-sub");
  var link = $("save-original");
  if (!bar || !link) return;
  var url = info && (info.directUrl || null);
  if (!url) {
    link.classList.add("hidden");
    link.removeAttribute("href");
    return;
  }
  link.href = url;
  link.setAttribute("download", clientClipName().replace(/-9x16\.mp4$/, ".mp4"));
  link.classList.remove("hidden");
}

/* Cache to OPFS (serialized — the engine writes one fixed file name) and
 * generate the filmstrip from the local copy. Progress goes to the pill +
 * timeline fill, exactly like the server flow. vidssave sources additionally
 * run their server-side muxing task first (progress shown as "Preparing"). */
function clientCache(info, token) {
  updateClientCacheNote();

  /* vidssave: kick the task ourselves (memoized in the info object) so the
   * player lights up with the URL the moment it exists — before the OPFS
   * download starts. cacheSource()'s ensureStream() reuses the same promise. */
  var prepare =
    info.kind === "vidssave" && !info.playbackUrl
      ? info.getStreamUrl({
          onProgress: function (pct) {
            if (token !== state.previewToken) return;
            setCachePill("busy", "Preparing file · " + Math.round(pct) + "%");
          },
        }).then(function (res) {
          if (token !== state.previewToken) return null;
          var video = $("player");
          video.src = res.proxied;
          video.classList.remove("dimmed");
          $("player-preparing").classList.add("hidden");
          updateSaveOriginalLink(info);
          return res;
        })
      : Promise.resolve(null);

  var run = function () {
    return prepare.then(function () {
      return window.ClientEngine.cacheSource(info, {
        onTaskProgress: function (pct) {
          if (token !== state.previewToken) return;
          setCachePill("busy", "Preparing file · " + Math.round(pct) + "%");
        },
        onProgress: function (ratio, done) {
          if (token !== state.previewToken) return;
          var label = ratio == null ? fmtBytes(done) : Math.round(ratio * 100) + "%";
          setCachePill("busy", "Caching in your browser · " + label);
          if (state.timeline) {
            state.timeline.setProgress(ratio == null ? null : Math.min(0.999, ratio));
          }
          updateClientCacheNote(label);
        },
      });
    });
  };

  state.clientCacheQueue = state.clientCacheQueue.then(run, run).then(
    function (res) {
      if (token !== state.previewToken) return;
      state.client.file = res.file;
      /* The <video> element can give up on the proxy stream while vidssave's
       * CDN is still materializing the file (slow first byte → ERR_ABORTED).
       * If it never received any data, play the freshly cached local copy. */
      var player = $("player");
      if (player.readyState === 0 && state.client.info) {
        player.src = URL.createObjectURL(res.file);
        player.classList.remove("dimmed");
      }
      setCachePill("done", "Cached locally · " + fmtBytes(res.size));
      if (state.timeline) {
        state.timeline.setProgress(1);
        state.timeline.clearProgress();
      }
      updateClientCacheNote();
      window.ClientEngine.warmUp(); // preload ffmpeg.wasm while the user picks a range

      var duration = info.duration || (state.timeline ? state.timeline.duration : 0);
      if (!duration || duration < 1) return;
      setCachePill("busy", "Generating thumbnails…");
      return window.ClientEngine.generateThumbs(res.file, duration, 60).then(
        function (thumbs) {
          if (token !== state.previewToken) return;
          if (state.timeline && thumbs && thumbs.length) {
            state.timeline.setThumbs(thumbs);
          }
          setCachePill("done", "Cached locally · " + fmtBytes(res.size));
        }
      );
    },
    function (err) {
      if (token !== state.previewToken) return;
      setCachePill("error", "Local cache failed");
      var relayed = /HTTP 5\d\d|HTTP 403/.test(String(err && err.message));
      showError(
        "Browser download failed: " + err.message +
        (relayed
          ? " — vidssave download links are bound to the network that created them, " +
            "so a hosted backend cannot relay them. Switch the engine to server " +
            "(top-right chip) or upload the video file directly."
          : "")
      );
      updateClientCacheNote();
    }
  );
}

function updateClientCacheNote(pct) {
  var note = $("cache-note");
  if (state.client && state.client.info && state.client.info.kind === "upload") {
    note.textContent =
      "Uploaded file — the 9:16 clip renders " +
      (state.engine === "browser"
        ? "entirely on your device (ffmpeg.wasm); the file never leaves your machine."
        : "on the server: the file uploads when you press Create clip (progress shown below), then the server cuts it.");
    note.classList.remove("hidden");
  } else if (state.client && state.client.info && !state.client.file) {
    note.textContent =
      "Browser engine: the source is downloading to your device (OPFS) — " +
      "the clip is cut locally with ffmpeg.wasm the moment it finishes" +
      (pct != null ? " (" + pct + ")" : "") + ".";
    note.classList.remove("hidden");
  } else if (state.client && state.client.file) {
    note.textContent =
      "Browser engine: source cached locally (" + fmtBytes(state.client.file.size) +
      ") — clipping runs entirely on your device; nothing touches the server.";
    note.classList.remove("hidden");
  } else {
    note.classList.add("hidden");
  }
}

/* ------------------------------ editor setup ----------------------------- */

function applyPreview(preview, rawUrl) {
  state.preview = preview;
  state.urlAtPreviewLoad = rawUrl;
  clearLoadStatus();

  var editor = $("editor");
  editor.classList.remove("hidden");

  $("video-title").textContent = preview.title || "Source video (" + preview.video_id + ")";
  updateVideoSub(preview);

  buildTimeline(preview);
  updatePlayerSource(preview);

  // rare: playable stream whose duration the backend could not probe — let
  // the player's own metadata light the timeline up
  if (!state.timeline && preview.video_url) {
    var video = $("player");
    var onMeta = function () {
      video.removeEventListener("loadedmetadata", onMeta);
      if (
        state.preview &&
        state.preview.id === preview.id &&
        !state.timeline &&
        video.duration &&
        isFinite(video.duration)
      ) {
        buildTimeline(Object.assign({}, state.preview, { duration: video.duration }));
        state.timeline.setSelection(0, Math.min(15, video.duration));
        onSelectionChange(state.timeline.selStart, state.timeline.selEnd);
      }
    };
    video.addEventListener("loadedmetadata", onMeta);
    if (video.readyState >= 1) onMeta();
  }

  startReelsPreview();
  updateTransport();
  updateCachePill(preview);
  updateCacheNote();
  editor.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

/* Progressively refresh the live editor from each poll: player source
 * appearing, cache pill %, timeline fill, richer metadata. */
function updateEditorProgress(preview) {
  if (!state.preview || state.preview.id !== preview.id) return;
  state.preview = preview;
  updateVideoSub(preview);
  updatePlayerSource(preview);
  updateCachePill(preview);
  updateCacheNote();
  if (!state.timeline && preview.duration) {
    // duration arrived late (real probe of the downloaded file)
    buildTimeline(preview);
    state.timeline.setSelection(0, Math.min(15, preview.duration));
    onSelectionChange(state.timeline.selStart, state.timeline.selEnd);
  }
  if (state.timeline) {
    if (preview.status === "streaming" || preview.status === "downloading") {
      state.timeline.setProgress(preview.progress == null ? null : preview.progress);
    } else if (preview.status === "processing") {
      state.timeline.setProgress(1);
    }
  }
}

/* Final upgrade when the preview turns ready: filmstrip in, pill green. */
function finishPreview(preview) {
  state.preview = preview;
  updateEditorProgress(preview);
  if (state.timeline) {
    var thumbs = (preview.thumbs || []).map(function (t) {
      return apiUrl(t);
    });
    if (thumbs.length) state.timeline.setThumbs(thumbs);
    state.timeline.clearProgress();
  }
  updateCachePill(preview);
  updateCacheNote();
}

function updateVideoSub(preview) {
  var bits = [fmtShort(preview.duration || 0)];
  if (preview.width && preview.height) bits.push(preview.width + "×" + preview.height);
  var via = preview.provider || preview.stream_provider;
  if (via) bits.push("via " + via);
  $("video-sub").textContent = bits.join(" · ");
}

/* Bind the <video> to the preview's /stream URL once — it proxies the
 * provider's direct URL until the local file lands, then serves the file,
 * so the element never needs to switch sources mid-session. */
function updatePlayerSource(preview) {
  var video = $("player");
  var preparing = $("player-preparing");
  if (preview.video_url && state.playerPreviewId !== preview.id) {
    state.playerPreviewId = preview.id;
    video.src = apiUrl(preview.video_url);
  }
  var playable = !!preview.video_url;
  preparing.classList.toggle("hidden", playable);
  video.classList.toggle("dimmed", !playable);
  if (!playable) {
    var sub = $("player-preparing-sub");
    if (sub) {
      sub.textContent =
        preview.status === "downloading"
          ? "Synthesizing/caching the source — playback starts in a moment"
          : "The source is being cached in the background";
    }
  }
}

function setCachePill(kind, text) {
  var pill = $("cache-pill");
  var label = $("cache-pill-text");
  pill.classList.remove("hidden", "busy", "done", "error");
  pill.classList.add(kind);
  label.textContent = text;
}

function updateCachePill(preview) {
  if (!preview) return; // failure path sets the pill itself
  if (preview.status === "ready") {
    setCachePill("done", "Source cached");
  } else if (preview.status === "processing") {
    setCachePill("busy", "Finishing timeline…");
  } else if (preview.progress != null) {
    setCachePill("busy", "Caching in background · " + Math.round(preview.progress * 100) + "%");
  } else if (preview.status === "streaming" || preview.status === "downloading") {
    setCachePill("busy", "Caching in background…");
  } else {
    setCachePill("busy", "Preparing…");
  }
}

function updateCacheNote() {
  var note = $("cache-note");
  var waiting =
    state.preview &&
    state.urlAtPreviewLoad === $("url").value.trim() &&
    state.preview.status !== "ready" &&
    (state.preview.status === "streaming" ||
      state.preview.status === "downloading" ||
      state.preview.status === "processing");
  if (waiting) {
    var pct = state.preview.progress != null
      ? " (" + Math.round(state.preview.progress * 100) + "% cached)"
      : "";
    note.textContent =
      "Real-time mode: the clip will be cut from the background download" +
      pct +
      " the moment it finishes — you can keep browsing and selecting now.";
    note.classList.remove("hidden");
  } else {
    note.classList.add("hidden");
  }
}

function buildTimeline(preview) {
  var duration = preview.duration || 0;
  if (!duration) return;

  if (state.timeline) state.timeline.destroy();

  var thumbs = (preview.thumbs || []).map(function (t) {
    return apiUrl(t);
  });

  var maxLen = state.meta ? state.meta.limits.max_clip_seconds : 600;
  state.timeline = new window.Timeline({
    viewport: $("tl-viewport"),
    track: $("tl-track"),
    strip: $("tl-strip"),
    ruler: $("tl-ruler"),
    handleL: $("tl-handle-l"),
    handleR: $("tl-handle-r"),
    dimL: $("tl-dim-l"),
    dimR: $("tl-dim-r"),
    sel: $("tl-sel"),
    playhead: $("tl-playhead"),
    progress: $("tl-progress"),
    duration: duration,
    thumbs: thumbs,
    maxLen: maxLen,
    minLen: Math.min(1, duration),
    onChange: onSelectionChange,
    onSeek: onScrub,
  });

  // default selection: first 15 seconds (or the whole video when shorter)
  state.timeline.setSelection(0, Math.min(15, duration));
  onSelectionChange(state.timeline.selStart, state.timeline.selEnd);
  if (
    preview.status === "streaming" ||
    preview.status === "downloading" ||
    preview.status === "processing"
  ) {
    state.timeline.setProgress(preview.status === "processing" ? 1 : preview.progress || null);
  }
  $("tl-viewport").focus({ preventScroll: true });
}

function onSelectionChange(start, end) {
  $("start").value = start.toFixed(2);
  $("end").value = end.toFixed(2);
  $("sel-in").textContent = fmtShort(start);
  $("sel-out").textContent = fmtShort(end);
  var len = end - start;
  var maxLen = state.meta ? state.meta.limits.max_clip_seconds : 600;
  var lenEl = $("sel-len");
  lenEl.textContent = fmtShort(len) + (len >= maxLen - 0.05 ? " (max)" : "");
  lenEl.classList.toggle("warn", len >= maxLen - 0.05);
}

function onScrub(t) {
  var video = $("player");
  try {
    video.currentTime = t;
  } catch (e) {
    /* seeking before metadata is ready — ignore */
  }
  if (state.timeline) state.timeline.setPlayhead(t);
  updateTransport();
}

/* --------------------------- player + transport -------------------------- */

function updateTransport() {
  var video = $("player");
  var duration = state.timeline ? state.timeline.duration : video.duration || 0;
  $("time-readout").textContent = fmtShort(video.currentTime || 0) + " / " + fmtShort(duration);
  $("play-btn").classList.toggle("playing", !video.paused && !video.ended);
}

function togglePlay() {
  var video = $("player");
  if (video.paused) video.play().catch(function () {});
  else video.pause();
}

/* smooth playhead while playing + reels canvas */
function startFrameLoop() {
  if (state.rafId) return;
  var video = $("player");
  var loop = function () {
    state.rafId = requestAnimationFrame(loop);
    if (!state.timeline) return;
    if (!video.paused) {
      state.timeline.setPlayhead(video.currentTime);
      state.timeline.scrollIntoView(video.currentTime, 72);
    }
    drawReelsFrame();
  };
  state.rafId = requestAnimationFrame(loop);
}

/* ------------------------------ reels canvas ----------------------------- */

function drawReelsFrame() {
  var canvas = $("reels-canvas");
  if (!canvas || !state.reelsRunning) return;
  var video = $("player");
  var ctx = canvas.getContext("2d");
  var W = canvas.width;
  var H = canvas.height;
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, W, H);
  var vw = video.videoWidth;
  var vh = video.videoHeight;
  if (video.readyState < 2 || !vw || !vh) {
    ctx.fillStyle = "rgba(232,235,242,0.35)";
    ctx.font = "600 13px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("9:16 preview", W / 2, H / 2);
    return;
  }
  // backdrop: zoomed to cover + blurred (the classic reels treatment)
  var coverScale = Math.max(W / vw, H / vh);
  var cw = vw * coverScale;
  var ch = vh * coverScale;
  try {
    ctx.filter = "blur(13px) saturate(1.25) brightness(0.72)";
  } catch (e) {
    /* filter unsupported → plain dark cover */
  }
  ctx.drawImage(video, (W - cw) / 2, (H - ch) / 2, cw, ch);
  ctx.filter = "none";
  ctx.fillStyle = "rgba(0,0,0,0.18)";
  ctx.fillRect(0, 0, W, H);
  // foreground: fit inside the 9:16 frame, centered
  var fitScale = Math.min(W / vw, H / vh);
  var fw = vw * fitScale;
  var fh = vh * fitScale;
  ctx.drawImage(video, (W - fw) / 2, (H - fh) / 2, fw, fh);
}

function startReelsPreview() {
  state.reelsRunning = true;
  startFrameLoop();
}

/* ------------------------------ style params ----------------------------- */

function renderStyleParams() {
  var select = $("style");
  var box = $("style-params");
  box.innerHTML = "";
  var style = state.styles.filter(function (s) {
    return s.id === select.value;
  })[0];
  if (!style || !style.parameters || !style.parameters.length) return;

  style.parameters.forEach(function (p) {
    var wrap = el("div", "param");
    var label = el("label", null);
    if (p.type === "boolean") {
      var check = el("input");
      check.type = "checkbox";
      check.dataset.param = p.name;
      check.checked = !!p.default;
      label.appendChild(check);
      label.appendChild(el("span", null, " " + p.name.replace(/_/g, " ")));
    } else if (p.type === "enum") {
      label.appendChild(el("span", null, p.name.replace(/_/g, " ")));
      var select2 = el("select");
      select2.dataset.param = p.name;
      (p.choices || []).forEach(function (c) {
        var option = el("option", null, c);
        option.value = c;
        select2.appendChild(option);
      });
      select2.value = p.default;
      label.appendChild(select2);
    } else {
      label.appendChild(el("span", null, p.name.replace(/_/g, " ")));
      var input = el("input");
      input.type = p.type === "number" ? "number" : "text";
      if (p.type === "number") input.step = "0.1";
      input.dataset.param = p.name;
      input.value = p.default != null ? p.default : "";
      label.appendChild(input);
    }
    wrap.appendChild(label);
    if (p.description) wrap.appendChild(el("div", "param-desc", p.description));
    box.appendChild(wrap);
  });
}

function collectStyleParams() {
  var out = {};
  var nodes = document.querySelectorAll("#style-params [data-param]");
  nodes.forEach(function (node) {
    var value = node.type === "checkbox" ? node.checked : node.value;
    if (node.type === "number") value = Number(value);
    out[node.dataset.param] = value;
  });
  return out;
}

/* ----------------------------- job lifecycle ----------------------------- */

function submitJob(event) {
  event.preventDefault();
  hideError();
  if (state.engine === "browser" && state.client && state.client.info) {
    if (!state.client.file) {
      showError(
        "The source is still downloading to your browser — the local clip starts once the cache finishes."
      );
      return;
    }
    clientSubmitJob();
    return;
  }
  // server engine + uploaded file: ship the bytes to the backend first
  if (state.client && state.client.info && state.client.info.kind === "upload") {
    uploadSubmitJob();
    return;
  }
  var btn = $("submit-btn");
  btn.disabled = true;
  btn.textContent = "Creating…";

  var body = {
    url: $("url").value.trim(),
    start_time: $("start").value.trim(),
    end_time: $("end").value.trim(),
    style_id: $("style").value,
    style_params: collectStyleParams(),
  };
  // reuse the preview's background download whenever the URL hasn't changed
  // since loading — even while it is still caching (the job waits server-side)
  if (state.preview && state.urlAtPreviewLoad === body.url) {
    body.preview_id = state.preview.id;
  }

  request("/api/jobs", { method: "POST", body: JSON.stringify(body) })
    .then(function (job) {
      btn.disabled = false;
      btn.textContent = "Create clip";
      $("status-section").classList.remove("hidden");
      $("result").classList.add("hidden");
      $("status-section").scrollIntoView({ behavior: "smooth", block: "nearest" });
      poll(job.id);
    })
    .catch(function (err) {
      btn.disabled = false;
      btn.textContent = "Create clip";
      $("status-section").classList.remove("hidden");
      $("result").classList.add("hidden");
      showError(err.message);
    });
}

/* ------------------------- browser engine: clipping ----------------------- */

/* Client-engine variant of submitJob/poll/renderResult: the 9:16 cut runs
 * in ffmpeg.wasm inside a Web Worker and the result is a local blob —
 * no server round-trip, no job record. */
function clientSubmitJob() {
  var btn = $("submit-btn");
  btn.disabled = true;
  btn.textContent = "Clipping…";

  $("status-section").classList.remove("hidden");
  $("result").classList.add("hidden");
  $("status-section").scrollIntoView({ behavior: "smooth", block: "nearest" });

  var start = parseFloat($("start").value) || 0;
  var end = parseFloat($("end").value) || start + 15;
  var params = collectStyleParams();
  var logs = []; // tail of the ffmpeg log — surfaced on failure

  var line = $("status-line");
  line.innerHTML = "";
  line.appendChild(el("span", "badge clipping", "clipping"));
  var text = el("span", "status-text", " Cutting to 9:16 on your device (ffmpeg.wasm)…");
  line.appendChild(text);
  line.appendChild(
    el("span", "muted", " " + (state.client.info.title || "") + " · local render")
  );

  window.ClientEngine.clip({
    file: state.client.file,
    start: start,
    end: end,
    background: params.background === "black" ? "black" : "blur",
    resolution: Number(params.resolution) === 1080 ? 1080 : 720,
    onProgress: function (r) {
      if (r != null) {
        text.textContent = " Cutting to 9:16 on your device… " + Math.round(r * 100) + "%";
      }
    },
    onLog: function (t) {
      logs.push(String(t));
      if (logs.length > 60) logs.shift();
    },
  }).then(
    function (out) {
      btn.disabled = false;
      btn.textContent = "Create clip";
      var video = $("preview");
      video.src = out.url;
      $("result").classList.remove("hidden");
      $("result-meta").textContent =
        (state.client.info.title || "clip") + " · " + fmtShort(start) + "–" + fmtShort(end) +
        " · " + out.duration.toFixed(1) + "s · " + fmtBytes(out.size) +
        " · rendered in your browser";
      var dims = $("result-dims");
      dims.textContent = "checking dimensions…";
      video.addEventListener(
        "loadedmetadata",
        function onMeta() {
          video.removeEventListener("loadedmetadata", onMeta);
          var ratio = video.videoWidth && video.videoHeight
            ? (video.videoHeight / video.videoWidth).toFixed(2)
            : "?";
          dims.textContent =
            video.videoWidth + "×" + video.videoHeight + " · aspect " + ratio + " (9:16 = 1.78)";
        }
      );
      var dl = $("download-btn");
      dl.href = out.url;
      dl.setAttribute("download", clientClipName());
    },
    function (err) {
      btn.disabled = false;
      btn.textContent = "Create clip";
      var tail = logs
        .filter(function (l) {
          return /\[error\]|error|invalid|no such|failed|unknown/i.test(l);
        })
        .slice(-8);
      showError(
        "Browser clip failed: " + err.message +
        (tail.length ? " · ffmpeg: " + tail.join(" · ") : "")
      );
    }
  );
}

/* --------------------- server engine: upload + clip ------------------------ */

/* Server-engine variant for uploaded files: multipart POST /api/uploads
 * (XHR — fetch cannot report upload progress), then a normal job that
 * references the upload_id. The editor itself was already fully local. */
function uploadSubmitJob() {
  var btn = $("submit-btn");
  btn.disabled = true;
  btn.textContent = "Uploading…";

  $("status-section").classList.remove("hidden");
  $("result").classList.add("hidden");
  $("status-section").scrollIntoView({ behavior: "smooth", block: "nearest" });

  var line = $("status-line");
  function setStatus(pct) {
    line.innerHTML = "";
    line.appendChild(el("span", "badge queued", "uploading"));
    line.appendChild(
      el(
        "span",
        "status-text",
        pct == null
          ? " Uploading your video to the server engine…"
          : " Uploading your video to the server engine… " + pct + "%"
      )
    );
    line.appendChild(
      el("span", "muted", " " + (state.client.info.fileName || "") + " · " + fmtBytes(state.client.file.size))
    );
  }
  setStatus(null);

  function fail(err) {
    btn.disabled = false;
    btn.textContent = "Create clip";
    showError(err.message);
  }

  var fd = new FormData();
  fd.append("file", state.client.file, state.client.info.fileName || "upload.mp4");

  var xhr = new XMLHttpRequest();
  xhr.open("POST", apiUrl("/api/uploads"));
  xhr.responseType = "json";
  xhr.upload.onprogress = function (ev) {
    if (ev.lengthComputable && ev.total > 0) {
      setStatus(Math.round((ev.loaded / ev.total) * 100));
    }
  };
  xhr.onerror = function () {
    fail(new Error("Upload failed — network error (the file may be too large for your connection)."));
  };
  xhr.onload = function () {
    if (xhr.status < 200 || xhr.status >= 300) {
      var body = xhr.response;
      var message =
        body && body.error && body.error.message
          ? body.error.message
          : "Upload failed (HTTP " + xhr.status + ").";
      fail(new Error(message));
      return;
    }
    request("/api/jobs", {
      method: "POST",
      body: JSON.stringify({
        upload_id: xhr.response.id,
        start_time: $("start").value.trim(),
        end_time: $("end").value.trim(),
        style_id: $("style").value,
        style_params: collectStyleParams(),
      }),
    }).then(
      function (job) {
        btn.disabled = false;
        btn.textContent = "Create clip";
        poll(job.id);
      },
      fail
    );
  };
  xhr.send(fd);
}

function poll(jobId) {
  if (state.jobTimer) clearTimeout(state.jobTimer);
  var failures = 0;
  var render = function () {
    request("/api/jobs/" + jobId)
      .then(function (job) {
        failures = 0;
        renderStatus(job);
        if (job.status === "completed") {
          renderResult(job);
          loadHistory();
          return;
        }
        if (job.status === "failed") {
          showError(job.error || "Job failed.");
          loadHistory();
          return;
        }
        state.jobTimer = setTimeout(render, 1500);
      })
      .catch(function (err) {
        // transient failures (rate limit, blip) must not orphan a running job
        failures += 1;
        if (failures >= 6) {
          showError(err.message);
          return;
        }
        state.jobTimer = setTimeout(render, 2500);
      });
  };
  render();
}

function renderStatus(job) {
  var line = $("status-line");
  var labels = {
    queued: state.preview && state.preview.id === job.preview_id && state.preview.status !== "ready"
      ? "Waiting for the background download (" +
        (state.preview.progress != null ? Math.round(state.preview.progress * 100) + "% cached" : "in progress") +
        ")…"
      : "Queued…",
    downloading: "Downloading source…",
    clipping: "Clipping to 9:16…",
  };
  line.innerHTML = "";
  line.appendChild(el("span", "badge " + job.status, job.status));
  line.appendChild(el("span", "status-text", " " + (labels[job.status] || "")));
  line.appendChild(
    el(
      "span",
      "muted",
      " " + job.source_url + " · " + job.start_timecode + " → " + job.end_timecode +
        (job.video_title ? " · " + job.video_title : "")
    )
  );
}

function renderResult(job) {
  $("result").classList.remove("hidden");
  var video = $("preview");
  video.src = apiUrl(job.clip_url);
  $("result-meta").textContent =
    (job.video_title || job.video_id) + " · " + job.start_timecode + "–" + job.end_timecode +
    " · " + (job.output_duration_seconds || 0).toFixed(1) + "s · " + fmtBytes(job.output_size_bytes) +
    (job.provider ? " · via " + job.provider : "") +
    (job.notes ? " · " + job.notes : "");
  var dims = $("result-dims");
  dims.textContent = "checking dimensions…";
  video.addEventListener(
    "loadedmetadata",
    function onMeta() {
      video.removeEventListener("loadedmetadata", onMeta);
      var ratio = video.videoWidth && video.videoHeight ? (video.videoHeight / video.videoWidth).toFixed(2) : "?";
      dims.textContent = video.videoWidth + "×" + video.videoHeight + " · aspect " + ratio + " (9:16 = 1.78)";
    }
  );
  var dl = $("download-btn");
  dl.href = apiUrl(job.download_url);
}

/* -------------------------------- history -------------------------------- */

function loadHistory() {
  request("/api/jobs?limit=25&offset=0")
    .then(function (page) {
      var body = $("history-body");
      body.innerHTML = "";
      $("history-empty").classList.toggle("hidden", page.total > 0);
      page.items.forEach(function (job) {
        var tr = document.createElement("tr");
        var clipCell = job.clip_url
          ? '<a href="' + apiUrl(job.clip_url) + '" target="_blank" rel="noreferrer">open</a> · ' +
            '<a href="' + apiUrl(job.download_url) + '">download</a>'
          : '<span class="muted">—</span>';
        var errorNote = job.error
          ? '<div class="muted" title="' + job.error.replace(/"/g, "&quot;") + '">' + job.error.slice(0, 80) + "</div>"
          : "";
        // uploaded-file jobs carry a pseudo source_url — only real http(s)
        // links become anchors
        var titleText = job.video_title || job.video_id || job.source_url;
        var safeTitle = String(titleText)
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;");
        var srcCell = /^https?:/i.test(job.source_url)
          ? '<a href="' + job.source_url + '" target="_blank" rel="noreferrer">' + safeTitle + "</a>"
          : "<span>" + (job.video_title ? safeTitle : "<span class=\"muted\">uploaded file</span>") + "</span>";
        tr.innerHTML =
          "<td>" + fmtDate(job.created_at) + "</td>" +
          "<td>" + srcCell + "</td>" +
          "<td>" + job.start_timecode + " → " + job.end_timecode + "</td>" +
          "<td>" + job.style_id + "</td>" +
          '<td><span class="badge ' + job.status + '">' + job.status + "</span>" + errorNote + "</td>" +
          "<td>" + clipCell + "</td>";
        body.appendChild(tr);
      });
      $("history-meta").textContent = page.total + " job(s) on record";
    })
    .catch(function (err) {
      $("history-meta").textContent = "History unavailable: " + err.message;
    });
}

/* ------------------------------ error helpers ---------------------------- */

function showError(message) {
  var box = $("error-box");
  box.textContent = message;
  box.classList.remove("hidden");
}

function hideError() {
  $("error-box").classList.add("hidden");
}

/* ---------------------------------- boot ---------------------------------- */

function loadStyles() {
  return request("/api/styles").then(function (styles) {
    state.styles = styles;
    var select = $("style");
    select.innerHTML = "";
    styles.forEach(function (s) {
      var option = el("option", null, s.name);
      option.value = s.id;
      select.appendChild(option);
    });
    renderStyleParams();
  });
}

function loadMeta() {
  return request("/api/meta").then(function (meta) {
    state.meta = meta;
    var text =
      "Max clip " + meta.limits.max_clip_timecode +
      " · max source " + meta.limits.max_source_timecode +
      " · files kept " + meta.retention_hours + " h";
    if (meta.providers.indexOf("sample") !== -1) {
      text += " · demo provider chain: " + meta.providers.join(" → ");
    }
    $("limits").textContent = text;

    var chip = $("provider-chip");
    if (meta.providers.indexOf("sample") !== -1) {
      chip.textContent = "demo mode · sample provider";
      chip.classList.remove("hidden");
      chip.classList.add("demo");
    } else {
      chip.textContent = meta.providers.join(" → ");
      chip.classList.remove("hidden");
    }
  });
}

function resetEditor() {
  state.previewToken++;
  if (state.previewTimer) clearTimeout(state.previewTimer);
  state.preview = null;
  state.client = null;
  state.urlAtPreviewLoad = "";
  state.playerPreviewId = "";
  if (state.timeline) {
    state.timeline.destroy();
    state.timeline = null;
  }
  state.reelsRunning = false;
  var video = $("player");
  video.pause();
  video.removeAttribute("src");
  video.load();
  video.classList.remove("dimmed");
  video.removeAttribute("poster");
  $("player-preparing").classList.add("hidden");
  $("cache-pill").classList.add("hidden");
  $("cache-note").classList.add("hidden");
  $("editor").classList.add("hidden");
  var fileInput = $("file-input");
  if (fileInput) fileInput.value = ""; // re-picking the same file must fire change
  var saveLink = $("save-original");
  if (saveLink) {
    saveLink.classList.add("hidden");
    saveLink.removeAttribute("href");
  }
  updateClientCacheNote();
  $("url").focus();
}

function bindEvents() {
  $("load-form").addEventListener("submit", function (e) {
    e.preventDefault();
    loadPreview($("url").value);
  });

  // auto-load shortly after a YouTube-looking URL is pasted/typed
  var debounce = null;
  var considerAutoLoad = function (immediate) {
    if (debounce) clearTimeout(debounce);
    var value = $("url").value.trim();
    if (!value) return;
    debounce = setTimeout(function () {
      if (LOOKS_LIKE_YT.test($("url").value.trim())) {
        var current = $("url").value.trim();
        if (current !== state.urlAtPreviewLoad && current !== state.lastAutoLoad) {
          state.lastAutoLoad = current;
          loadPreview(current);
        }
      }
    }, immediate ? 350 : 900);
  };
  $("url").addEventListener("input", function () {
    considerAutoLoad(false);
  });
  // a paste is the strongest signal the user wants this video — react fast
  $("url").addEventListener("paste", function () {
    considerAutoLoad(true);
  });

  $("change-video-btn").addEventListener("click", resetEditor);

  // browser-engine toggle (header chip)
  var engineChip = $("engine-chip");
  if (engineChip) {
    engineChip.addEventListener("click", function () {
      var target = state.engine === "browser" ? "server" : "browser";
      if (target === "browser" && !clientEngineUsable()) {
        setLoadStatus(
          "error",
          "Browser engine unavailable — install the companion extension or configure window.CLIPPER_YT_PROXY (see proxy/DEPLOY.md)."
        );
        return;
      }
      state.engine = target;
      state.engineExplicit = true;
      try {
        localStorage.setItem("ytcc-engine-v2", target);
      } catch (e) {
        /* private mode — fine, next boot defaults again */
      }
      updateEngineChip();
      // an uploaded file stays loaded across the switch — its editor state
      // is fully local, only the submit destination changes
      if (state.client && state.client.info && state.client.info.kind === "upload") {
        updateClientCacheNote();
        return;
      }
      resetEditor();
      var value = $("url").value.trim();
      if (value) loadPreview(value);
    });
  }

  // file upload (button + hidden input + drag & drop onto the load card)
  var uploadBtn = $("upload-btn");
  var fileInput = $("file-input");
  if (uploadBtn && fileInput) {
    uploadBtn.addEventListener("click", function () {
      fileInput.click();
    });
    fileInput.addEventListener("change", function () {
      var file = fileInput.files && fileInput.files[0];
      fileInput.value = ""; // allow re-picking the same file later
      handleFilePick(file);
    });
  }
  var loadCard = $("load-card");
  if (loadCard) {
    ["dragenter", "dragover"].forEach(function (evt) {
      loadCard.addEventListener(evt, function (e) {
        e.preventDefault();
        loadCard.classList.add("drag");
      });
    });
    loadCard.addEventListener("dragleave", function (e) {
      if (e.target === loadCard) loadCard.classList.remove("drag");
    });
    loadCard.addEventListener("drop", function (e) {
      e.preventDefault();
      loadCard.classList.remove("drag");
      var file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (file) handleFilePick(file);
    });
  }

  // timeline tools
  $("set-in-btn").addEventListener("click", function () {
    if (!state.timeline) return;
    var t = $("player").currentTime || 0;
    state.timeline.setSelection(Math.min(t, state.timeline.selEnd - 0.1), state.timeline.selEnd);
    state.timeline.scrollIntoView(state.timeline.selStart);
  });
  $("set-out-btn").addEventListener("click", function () {
    if (!state.timeline) return;
    var t = $("player").currentTime || 0;
    state.timeline.setSelection(state.timeline.selStart, Math.max(t, state.timeline.selStart + 0.1), { anchor: "end" });
    state.timeline.scrollIntoView(state.timeline.selEnd);
  });
  $("zoom-in-btn").addEventListener("click", function () {
    if (state.timeline) state.timeline.zoom(1.5);
  });
  $("zoom-out-btn").addEventListener("click", function () {
    if (state.timeline) state.timeline.zoom(1 / 1.5);
  });
  $("zoom-fit-btn").addEventListener("click", function () {
    if (state.timeline) state.timeline.fit();
  });

  // transport
  $("play-btn").addEventListener("click", togglePlay);
  $("mute-btn").addEventListener("click", function () {
    var video = $("player");
    video.muted = !video.muted;
    $("mute-btn").classList.toggle("muted", video.muted);
  });
  var video = $("player");
  video.addEventListener("play", updateTransport);
  video.addEventListener("pause", updateTransport);
  video.addEventListener("ended", updateTransport);
  video.addEventListener("timeupdate", function () {
    if (state.timeline && video.paused) state.timeline.setPlayhead(video.currentTime);
    updateTransport();
  });
  video.addEventListener("loadeddata", function () {
    drawReelsFrame();
  });

  // start the shared frame loop lazily with the first play
  video.addEventListener("play", startFrameLoop);

  // numeric inputs ↔ timeline
  function applyTimeInputs() {
    if (!state.timeline) return;
    var s = parseFloat($("start").value);
    var e = parseFloat($("end").value);
    if (isNaN(s) || isNaN(e)) return;
    state.timeline.setSelection(s, e);
    state.timeline.scrollIntoView(s);
  }
  $("start").addEventListener("change", applyTimeInputs);
  $("end").addEventListener("change", applyTimeInputs);

  // style params re-render on change
  $("style").addEventListener("change", renderStyleParams);

  // keyboard shortcuts (ignored while typing)
  document.addEventListener("keydown", function (e) {
    var tag = (e.target && e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "select" || tag === "textarea" || e.metaKey || e.ctrlKey) return;
    if (e.code === "Space") {
      e.preventDefault();
      togglePlay();
    } else if (e.key === "i" || e.key === "I") {
      $("set-in-btn").click();
    } else if (e.key === "o" || e.key === "O") {
      $("set-out-btn").click();
    } else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      if (!state.timeline) return;
      e.preventDefault();
      var step = (e.shiftKey ? 5 : 1) * (e.key === "ArrowLeft" ? -1 : 1);
      onScrub(Math.max(0, Math.min(state.timeline.duration, ($("player").currentTime || 0) + step)));
    }
  });

  $("clip-form").addEventListener("submit", submitJob);
  $("refresh-history").addEventListener("click", loadHistory);
}

state.lastAutoLoad = "";

/* Engine boot: default to the browser engine when one of its transports is
 * reachable, then honor the persisted user choice (downgrading a stale
 * "browser" choice when no transport is available anymore). YouTube URLs
 * route to the server engine unless the choice was explicit — see
 * loadPreview(). */
state.engineExplicit = false;
state.engine = clientEngineUsable() ? "browser" : "server";
try {
  var savedEngine = localStorage.getItem("ytcc-engine-v2");
  if (savedEngine === "browser" || savedEngine === "server") {
    state.engine = savedEngine;
    state.engineExplicit = true;
  }
} catch (e) {
  /* private mode */
}
if (state.engine === "browser" && !clientEngineUsable()) state.engine = "server";

bindEvents();
updateEngineChip();
loadStyles().catch(function (err) {
  showError("Could not load styles: " + err.message);
});
loadMeta().catch(function () {});
loadHistory();
