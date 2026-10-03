/* VidsSave client — browser-side YouTube resolution via vidssave.com.
 *
 * Reverse-engineered from vidssave.com's own frontend (2026-10-03):
 *
 *   1. POST {api}/media/parse     (origin=source, link=<yt url>)
 *      -> {status, data: <AES JSON>} with {title, duration, resources[]}
 *   2. POST {api}/media/download  (request=<resource_content>, no_encrypt=1)
 *      -> {status, data: <AES JSON>} with {task_id}
 *   3. GET  {sse}/media/download_query?task_id=...  (Server-Sent Events)
 *      -> events: running{progress} / success{download_link, filesize}
 *   4. GET  download_link -> 302 -> CDN mp4 (signed, any-IP, Range-capable)
 *
 * The API sends `Access-Control-Allow-Origin: *`, so the BROWSER can call it
 * directly — every vidssave request below leaves from the USER's IP. That is
 * the whole point: vidssave's risk check flags datacenter IPs (analyze_risk)
 * but passes residential browsers, and its CDN never IP-locks the result.
 *
 * CRITICAL (found live 2026-10-03): their risk engine ALSO flags any request
 * carrying a FOREIGN Referer (their own site sends https://vidssave.com/).
 * Every request here therefore omits the Referer — via the fetch option
 * below AND the <meta name="referrer" content="no-referrer"> in index.html
 * (the SSE EventSource cannot set a per-request policy).
 *
 * Response `data` fields are AES-256-CBC encrypted (base64, ZeroPadding).
 * Keys/IVs are extracted from the site's JS bundle; decryption uses the
 * vendored aes-js (vendor/aes-js/aes.js). Mirror of their readResponse():
 * try plain JSON first, then decrypt.
 *
 * API hosts: production api.vidssave.com first, then their staging endpoint
 * (test-api.vidssave.com) as an AUTOMATIC fallback. vidssave's risk check
 * flags many otherwise-fine networks (analyze_risk) on the production API
 * only, while the staging endpoint currently accepts them (verified live
 * 2026-10-03 — it even serves datacenter IPs). When the production parse is
 * refused, every call is retried once against staging; the host that
 * succeeded is remembered for the rest of the tab session (sessionStorage
 * "ytcc-vs-sticky") so the download task + SSE poll hit the same backend
 * that issued the resource token.
 *
 * Force a specific host for testing with ?vsapi=dev|prod or
 * window.CLIPPER_VIDSSAVE_API = "dev" | "prod" (this disables the fallback).
 *
 * Exposed as window.VidsSave.
 */
"use strict";

(function () {
  var PROD_API = "https://api.vidssave.com/api/contentsite_api";
  var PROD_SSE = "https://api.vidssave.com/sse/contentsite_api";
  var DEV_API = "https://test-api.vidssave.com/vapi/contentsite_api";
  var DEV_SSE = "https://test-api.vidssave.com/vsse/contentsite_api";

  /* Request identity mirrored from the site (chunk 1074, module 57502). */
  var FORM = {
    hostname: "vidssave.com",
    auth: "4c9b7d21",
    domain: "api-ak.vidssave.com",
  };
  var SSE_QUERY = {
    auth: "20250901majwlqo",
    domain: "api-ak.vidssave.com",
    download_domain: "vidssave.com",
    origin: "content_site",
  };

  /* AES key candidates + IV derivation, exactly as the site does:
   *   key = Utf8(keyStr), iv = Utf8(keyStr.slice(0, 16)), CBC, ZeroPadding. */
  var KEYS = [
    "4c9b7d2e4c9b7d2e4c9b7d2e4c9b7d21", // "4c9b7d2e" x3 + "4c9b7d21" (32B)
    "rz18efAXUbdiaO7k", // 16B fallback
  ];

  var STICKY_KEY = "ytcc-vs-sticky";

  /* Host that last succeeded (this tab). Survives reloads via sessionStorage
   * so a network that is flagged on prod skips straight to staging next
   * time instead of re-paying the failed round-trip. */
  var sticky = (function () {
    try {
      var v = window.sessionStorage.getItem(STICKY_KEY);
      return v === "dev" || v === "prod" ? v : "";
    } catch (e) {
      return ""; // sessionStorage unavailable — in-memory stickiness only
    }
  })();

  function setSticky(mode) {
    sticky = mode;
    try {
      window.sessionStorage.setItem(STICKY_KEY, mode);
    } catch (e) {
      /* ignore */
    }
  }

  function forcedMode() {
    var override = String(window.CLIPPER_VIDSSAVE_API || "").trim().toLowerCase();
    if (!override) {
      override = new URLSearchParams(window.location.search).get("vsapi") || "";
    }
    return override === "dev" || override === "prod" ? override : "";
  }

  function apiMode() {
    return forcedMode() || sticky || "prod";
  }

  function hostsFor(mode) {
    return mode === "dev"
      ? { api: DEV_API, sse: DEV_SSE }
      : { api: PROD_API, sse: PROD_SSE };
  }

  function apiUrl() {
    return hostsFor(apiMode()).api;
  }

  function sseUrl() {
    return hostsFor(apiMode()).sse;
  }

  /* ------------------------------- crypto -------------------------------- */

  function bytesFromAscii(str) {
    var out = new Uint8Array(str.length);
    for (var i = 0; i < str.length; i++) out[i] = str.charCodeAt(i) & 0xff;
    return out;
  }

  function bytesToUtf8(bytes) {
    /* decode as UTF-8; throws on invalid sequences (wrong-key garbage). */
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  }

  function base64ToBytes(b64) {
    var clean = b64.replace(/\s+/g, "");
    var bin = atob(clean);
    var out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  function looksLikeCiphertext(b64) {
    var s = String(b64 || "").trim();
    return s.length > 0 && s.length % 4 === 0 && /^[A-Za-z0-9+/]+={0,2}$/.test(s);
  }

  /* AES-CBC decrypt with ZeroPadding stripping; returns plaintext or null. */
  function decrypt(b64) {
    if (!looksLikeCiphertext(b64)) return null;
    var ct;
    try {
      ct = base64ToBytes(b64.trim());
    } catch (e) {
      return null;
    }
    if (ct.length === 0 || ct.length % 16 !== 0) return null;
    if (!window.aesjs) return null;
    for (var k = 0; k < KEYS.length; k++) {
      var key = KEYS[k];
      if ([16, 24, 32].indexOf(key.length) === -1) continue;
      try {
        var cbc = new window.aesjs.ModeOfOperation.cbc(
          bytesFromAscii(key),
          bytesFromAscii(key.slice(0, 16))
        );
        var pt = cbc.decrypt(ct);
        var end = pt.length;
        while (end > 0 && pt[end - 1] === 0) end--; // ZeroPadding
        if (end === 0) return null;
        var text = bytesToUtf8(pt.subarray(0, end));
        if (!text) return null;
        return text;
      } catch (e) {
        /* wrong key — try the next candidate */
      }
    }
    return null;
  }

  /* Mirror of the site's readResponse() chain. */
  function unwrap(payload) {
    var obj = payload;
    if (typeof obj === "string") {
      try {
        obj = JSON.parse(obj);
      } catch (e) {
        var raw = decrypt(obj);
        if (raw == null) throw new Error("vidssave: could not decode the response.");
        try {
          return JSON.parse(raw);
        } catch (e2) {
          throw new Error("vidssave: decrypted response was not JSON.");
        }
      }
    }
    if (obj && obj.status === 1 && typeof obj.data === "string") {
      var s = obj.data.trim();
      try {
        obj.data = JSON.parse(s); // plain JSON fast path
        return obj;
      } catch (e) {
        var dec = decrypt(s);
        if (dec == null) return obj; // not decryptable — hand back as-is
        try {
          obj.data = JSON.parse(dec);
        } catch (e2) {
          obj.data = dec;
        }
        return obj;
      }
    }
    return obj;
  }

  /* ------------------------------- requests ------------------------------- */

  function postFormTo(hosts, path, fields, signal) {
    var body = Object.assign({}, FORM, fields || {});
    return fetch(hosts.api + path, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams(body).toString(),
      credentials: "omit",
      referrerPolicy: "no-referrer", // their risk engine flags foreign Referers
      signal: signal || undefined,
    }).then(function (response) {
      return response.text().then(function (text) {
        if (!response.ok) {
          throw new Error("vidssave API HTTP " + response.status);
        }
        try {
          return unwrap(JSON.parse(text));
        } catch (e) {
          var unwrapped = unwrap(text);
          if (unwrapped == null) throw e;
          return unwrapped;
        }
      });
    });
  }

  function postForm(path, fields, signal) {
    return postFormTo(hostsFor(apiMode()), path, fields, signal);
  }

  /* ------------------------------- parsing -------------------------------- */

  function qualityNumber(quality) {
    var m = String(quality || "").match(/(\d{3,4})\s*p/i);
    return m ? Number(m[1]) : 0;
  }

  function isRiskError(err) {
    return !!(err && err.status_code === "analyze_risk");
  }

  /* Single media/parse against one host; throws Error with .status_code. */
  function parseOnce(mode, url, signal) {
    return postFormTo(
      hostsFor(mode),
      "/media/parse",
      { origin: "source", link: url },
      signal
    ).then(function (envelope) {
      if (envelope && envelope.status === 1 && envelope.data) return envelope;
      var code = (envelope && envelope.status_code) || "";
      var msg = (envelope && envelope.msg) || "analysis failed";
      var err = new Error(
        code === "analyze_risk"
          ? "vidssave flagged this network (analyze_risk) — " +
              "the resolver only accepts residential IPs."
          : "vidssave could not analyze this URL (" + (code || msg) + ")."
      );
      err.status_code = code;
      throw err;
    });
  }

  function mapParse(envelope) {
    var data = envelope.data;
    var videos = [];
    var audios = [];
    (data.resources || []).forEach(function (r) {
      if (!r || !r.resource_content) return;
      var entry = {
        resourceContent: r.resource_content,
        quality: r.quality || "",
        height: qualityNumber(r.quality),
        size: r.size || null,
        format: r.format || r.original_format || "MP4",
        hasAudio: !!r.has_audio,
        outputFormat: r.output_format || null,
        directUrl: r.download_url && r.download_mode === "direct" ? r.download_url : null,
      };
      if (r.type === "video") videos.push(entry);
      else if (r.type === "audio") audios.push(entry);
    });
    /* The download task always returns a muxed file (video+audio), so a
     * video entry is usable regardless of its has_audio flag. */
    videos.sort(function (a, b) {
      return (b.height || 0) - (a.height || 0) || (b.size || 0) - (a.size || 0);
    });
    return {
      title: data.title || "video",
      duration: data.duration ? Number(data.duration) : null,
      thumbnail: data.thumbnail || null,
      author: (data.user_item && data.user_item.name) || "",
      videos: videos,
      audios: audios,
      raw: data,
    };
  }

  /**
   * resolve(url, {signal}) -> Promise<{
   *   title, duration, thumbnail, author,
   *   videos: [{ resourceContent, quality, height, size, format, hasAudio }],
   *   audios: [...], raw
   * }>
   * videos are sorted best-first (highest quality with a usable resource).
   *
   * Tries the current host (forced > sticky > prod) and, unless a host was
   * forced with ?vsapi=/CLIPPER_VIDSSAVE_API, retries once against the
   * other endpoint when the first refuses (analyze_risk or any parse
   * failure). A successful fallback is remembered for the tab session.
   */
  function resolve(url, opts) {
    opts = opts || {};
    var forced = forcedMode();
    var first = apiMode();
    var attempt = parseOnce(first, url, opts.signal);
    if (forced) return attempt.then(mapParse);

    var other = first === "prod" ? "dev" : "prod";
    return attempt.then(mapParse).catch(function (err) {
      /* The primary host refused — vidssave's staging endpoint accepts many
       * networks the production API flags (analyze_risk); one retry there
       * rescues those users without affecting anyone else. */
      return parseOnce(other, url, opts.signal).then(
        function (envelope) {
          setSticky(other);
          return mapParse(envelope);
        },
        function (err2) {
          if (isRiskError(err) && isRiskError(err2)) {
            var both = new Error(
              "vidssave flagged this network (analyze_risk) on both of its " +
                "endpoints — YouTube downloads won't work from this network " +
                "right now."
            );
            both.status_code = "analyze_risk";
            throw both;
          }
          throw err2;
        }
      );
    });
  }

  /**
   * createTask(resourceContent, {outputFormat, signal}) -> Promise<taskId>
   * Runs against the current host (the one that issued the token); if it
   * refuses, the other endpoint gets one try too (it owns the token when
   * the parse fell back there).
   */
  function createTask(resourceContent, opts) {
    opts = opts || {};
    var fields = { request: resourceContent, no_encrypt: "1" };
    if (opts.outputFormat) fields.output = opts.outputFormat;

    function attempt(mode) {
      return postFormTo(hostsFor(mode), "/media/download", fields, opts.signal).then(
        function (envelope) {
          if (
            envelope &&
            envelope.status === 1 &&
            envelope.data &&
            envelope.data.task_id
          ) {
            return envelope.data.task_id;
          }
          var code = (envelope && envelope.status_code) || "";
          throw new Error(
            "vidssave refused to prepare the download" +
              (code ? " (" + code + ")" : "") +
              "."
          );
        }
      );
    }

    var first = apiMode();
    return attempt(first).catch(function (err) {
      if (forcedMode()) throw err;
      var other = first === "prod" ? "dev" : "prod";
      return attempt(other).then(function (taskId) {
        setSticky(other);
        return taskId;
      });
    });
  }

  /**
   * pollTask(taskId, {onProgress, timeoutMs}) -> Promise<{downloadLink, filesize}>
   * Server-Sent Events; resolves on the "success" event, rejects on "failed"
   * or timeout. The task_id link is cancelled on timeout (site behaviour).
   */
  function pollTask(taskId, opts) {
    opts = opts || {};
    var timeoutMs = opts.timeoutMs || 10 * 60 * 1000;
    var onProgress = opts.onProgress || function () {};

    return new Promise(function (resolve, reject) {
      var params = new URLSearchParams(Object.assign({ task_id: taskId }, SSE_QUERY));
      var source = new EventSource(sseUrl() + "/media/download_query?" + params.toString());
      var settled = false;
      var timer = setTimeout(function () {
        finish(function () {
          reject(new Error("vidssave download task timed out."));
        });
        cancelTask(taskId);
      }, timeoutMs);

      function finish(action) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        try {
          source.close();
        } catch (e) {
          /* ignore */
        }
        action();
      }

      source.addEventListener("running", function (event) {
        try {
          var data = JSON.parse(event.data || "{}");
          if (data && data.progress != null) onProgress(Number(data.progress) || 0);
        } catch (e) {
          /* ignore malformed progress frames */
        }
      });

      source.addEventListener("success", function (event) {
        var data;
        try {
          data = JSON.parse(event.data || "{}");
        } catch (e) {
          data = null;
        }
        if (!data || !data.download_link) {
          finish(function () {
            reject(new Error("vidssave finished the task without a download link."));
          });
          return;
        }
        finish(function () {
          resolve({
            downloadLink: data.download_link,
            filesize: data.filesize || null,
            downloadType: data.download_type || "",
          });
        });
      });

      source.addEventListener("failed", function () {
        finish(function () {
          reject(new Error("vidssave could not prepare this file (task failed)."));
        });
      });

      source.onerror = function () {
        /* Transient hiccups fire onerror without closing; only a permanently
         * closed stream (readyState 2) with no success is fatal. */
        if (source.readyState === 2) {
          finish(function () {
            reject(new Error("Lost the connection to vidssave's task stream."));
          });
        }
      };
    });
  }

  function cancelTask(taskId) {
    /* Fire-and-forget, exactly like the site's modal-close path. */
    try {
      postForm("/media/download_cancel", { task_id: taskId }).catch(function () {});
    } catch (e) {
      /* ignore */
    }
  }

  /**
   * getMedia(resourceContent, {onProgress, timeoutMs, signal})
   *   -> Promise<{ downloadLink, filesize }>
   * Convenience: createTask + pollTask in one call.
   */
  function getMedia(resourceContent, opts) {
    opts = opts || {};
    return createTask(resourceContent, opts).then(function (taskId) {
      return pollTask(taskId, opts);
    });
  }

  window.VidsSave = {
    resolve: resolve,
    createTask: createTask,
    pollTask: pollTask,
    cancelTask: cancelTask,
    getMedia: getMedia,
    apiMode: apiMode,
  };
})();
