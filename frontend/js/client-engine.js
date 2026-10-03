/* ClientEngine — the client-side download + clip architecture.
 *
 * Shifts the network identity (stream requests leave from the USER's IP) and
 * the compute load (ffmpeg.wasm in a Web Worker) off the server entirely:
 *
 *   1. resolve  — vidssave.com resolver called DIRECTLY from the browser
 *                 (js/vidssave-client.js; works from residential IPs with
 *                 zero setup — no extension, no proxy) — or, as a fallback,
 *                 the innertube player API via extension / CORS proxy
 *   2. playback — <video> streams the resolved URL instantly (Range)
 *   3. cache    — chunked download into OPFS (disk, not RAM) with progress
 *   4. filmstrip— thumbnails drawn from the local file via canvas
 *   5. clip     — ffmpeg.wasm cuts + reframes to 9:16, blob download
 *
 * Resolution paths, best first:
 *   vidssave  — window.VidsSave (built-in; user's IP hits vidssave's API,
 *               which tolerates residential browsers and proxies the media
 *               through its own CDN — no YouTube contact from any datacenter)
 *   extension — window.__YTCP__ injected by the companion Chrome extension
 *               (user's IP; bridge contract in proxy/DEPLOY.md)
 *   proxy     — proxy/worker.js (Cloudflare Worker in production, or the
 *               Node dev server in the sandbox)
 *
 * vidssave's media CDN sends no CORS headers, so its bytes flow through the
 * backend /api/media/proxy endpoint (same-origin, Range passthrough). That
 * hop is IP-agnostic — the CDN happily serves datacenter IPs.
 *
 * Exposed as window.ClientEngine.
 */
"use strict";

(function () {
  var PROXY_BASE = String(window.CLIPPER_YT_PROXY || "").replace(/\/+$/, "");
  var GATEWAY_PORT =
    new URLSearchParams(window.location.search).get("XTransformPort") || "";
  var PROXY_PORT = String(window.CLIPPER_PROXY_PORT || "8020");

  /* Client-mode caps (browser RAM/wasm address space, not server policy). */
  var MAX_SOURCE_BYTES = 400 * 1024 * 1024; // 400 MB
  var CHUNK = 8 * 1024 * 1024; // 8 MB range chunks

  /* ------------------------------ URL helpers ------------------------------ */

  /* Same logic as apiUrl() in app.js but for the proxy origin (its own
   * sandbox port). Priority: explicit CLIPPER_YT_PROXY (production CF
   * worker) > sandbox gateway > same-origin /ytproxy prefix. */
  function proxyUrl(path) {
    if (PROXY_BASE) return PROXY_BASE + path;
    if (GATEWAY_PORT) {
      var url = new URL(path, window.location.origin);
      url.searchParams.set("XTransformPort", PROXY_PORT);
      return url.pathname + url.search;
    }
    return "/ytproxy" + path;
  }

  function proxiedStreamUrl(mediaUrl) {
    return (
      proxyUrl("/stream?u=") + encodeURIComponent(mediaUrl)
    );
  }

  /* Same-origin backend media proxy (Range passthrough). Used for vidssave
   * media: its CDN sends no CORS headers, so the browser cannot read the
   * bytes cross-origin — the backend relays them instead. Mirror of
   * apiUrl() in app.js (kept local to avoid script load-order coupling). */
  function backendMediaProxyUrl(mediaUrl) {
    var path = "/api/media/proxy?url=" + encodeURIComponent(mediaUrl);
    if (GATEWAY_PORT) {
      var url = new URL(path, window.location.origin);
      url.searchParams.set("XTransformPort", GATEWAY_PORT);
      return url.pathname + url.search;
    }
    return path;
  }

  /* ------------------------------- transports ------------------------------- */

  function extensionTransport() {
    var bridge = window.__YTCP__;
    if (!bridge || bridge.version !== 1) return null;
    return {
      name: "extension",
      /* innertube POST executed by the extension background worker —
       * egress = the user's own residential IP, with real browser fetch. */
      post: function (path, body, headers, signal) {
        return bridge.yti(path, body, headers).then(function (payload) {
          if (payload && payload.error) throw new Error(payload.error);
          return payload;
        });
      },
      playbackUrl: function (directUrl) {
        /* <video> can play googlevideo directly (no CORS for playback);
         * thumbnails come from the local OPFS file, so no ACAO needed. */
        return directUrl;
      },
      needsCrossOrigin: false,
      openStream: function (url, init) {
        /* ranged GET through the extension (CORS-exempt). */
        return bridge.fetchRange(url, init && init.headers && init.headers.Range);
      },
    };
  }

  function proxyTransport() {
    return {
      name: "proxy",
      post: function (path, body, headers) {
        return fetch(proxyUrl("/yti"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: path, body: body, headers: headers }),
        }).then(function (response) {
          return response.json().then(function (json) {
            if (!response.ok) {
              throw new Error((json && json.error) || "proxy HTTP " + response.status);
            }
            return json;
          });
        });
      },
      playbackUrl: function (directUrl) {
        return proxiedStreamUrl(directUrl);
      },
      needsCrossOrigin: true, // CF worker is cross-origin in production
      openStream: function (url, init) {
        /* the Range header MUST be forwarded — chunked downloads and the
         * size probe depend on 206 responses (the worker allows the Range
         * header through CORS preflight). */
        var opts = Object.assign({}, init || {}, { credentials: "omit" });
        return fetch(url, opts);
      },
    };
  }

  function availableTransport() {
    return extensionTransport() || proxyTransport();
  }

  function available() {
    if (window.VidsSave) return "vidssave";
    var t = availableTransport();
    return t ? t.name : null;
  }

  /* ------------------------------ vidssave path ------------------------------ */

  /* vidssave resolution is two-stage: media/parse returns metadata + a
   * resource token instantly, but the playable URL only exists after a
   * server-side muxing task (media/download + SSE polling). resolve()
   * therefore returns an info object whose getStreamUrl() lazily runs the
   * task; the URL it resolves to is cached for playback + OPFS download. */
  var PREFERRED_HEIGHT = 720;

  function vidssaveResolve(url) {
    var videoId = window.YTResolver.videoIdFromUrl(url);
    return window.VidsSave.resolve(url).then(function (result) {
      var videos = result.videos || [];
      if (!videos.length) {
        throw new Error("vidssave returned no downloadable video formats.");
      }
      /* Best format not above the preferred height (fall back to the top). */
      var pick = null;
      for (var i = 0; i < videos.length; i++) {
        if (!videos[i].height || videos[i].height <= PREFERRED_HEIGHT) {
          pick = videos[i];
          break;
        }
      }
      pick = pick || videos[0];

      var info = {
        kind: "vidssave",
        videoId: videoId,
        title: result.title,
        author: result.author,
        duration: result.duration,
        thumbnail: result.thumbnail,
        directUrl: null, // set once the muxing task finishes
        playbackUrl: null,
        size: pick.size || null,
        height: pick.height || null,
        quality: pick.quality || "",
        transport: "vidssave",
        resolvedWith: pick.quality || "vidssave",
        resourceContent: pick.resourceContent,
        allFormats: videos,
        _streamPromise: null,
        getStreamUrl: function (opts) {
          opts = opts || {};
          if (info._streamPromise) return info._streamPromise;
          info._streamPromise = window.VidsSave.getMedia(
            info.resourceContent,
            opts
          ).then(function (res) {
            info.directUrl = res.downloadLink;
            info.playbackUrl = backendMediaProxyUrl(res.downloadLink);
            info.size = res.filesize || info.size;
            return { url: res.downloadLink, proxied: info.playbackUrl, size: res.filesize || info.size };
          });
          info._streamPromise.catch(function () {
            info._streamPromise = null; // allow a retry on the next attempt
          });
          return info._streamPromise;
        },
      };
      return info;
    });
  }

  /* -------------------------------- resolve -------------------------------- */

  /**
   * resolve(url) -> Promise<{
   *   kind: "youtube" | "direct",
   *   videoId, title, author, duration,
   *   directUrl, playbackUrl,   // raw media URL + what <video> should use
   *   size, transport, resolvedWith
   * }>
   */
  function resolve(url) {
    if (window.YTResolver.directMediaUrl(url)) {
      return Promise.resolve({
        kind: "direct",
        videoId: null,
        title: decodeURIComponent(url.split("/").pop().split("?")[0]) || "video",
        author: "",
        duration: null, // probed during caching / by the player
        directUrl: url,
        playbackUrl: url, // same-origin direct file — playable as-is
        size: null,
        transport: "direct",
        resolvedWith: "direct-url",
      });
    }

    var videoId = window.YTResolver.videoIdFromUrl(url);
    if (!videoId) {
      return Promise.reject(new Error("Not a YouTube URL or direct media URL."));
    }

    /* vidssave first: works from any residential browser with no setup.
     * Only when it refuses (network flagged, site down, no formats) do we
     * fall back to the innertube transports. */
    if (window.VidsSave) {
      return vidssaveResolve(url).catch(function (err) {
        var transport = availableTransport();
        if (!transport) throw err;
        return innertubeResolve(url, videoId, transport).catch(function () {
          throw err; // report the vidssave failure — it is the primary path
        });
      });
    }

    var transport = availableTransport();
    if (!transport) {
      return Promise.reject(new Error("No browser engine transport available."));
    }
    return innertubeResolve(url, videoId, transport);
  }

  function innertubeResolve(url, videoId, transport) {
    return window.YTResolver.resolve(videoId, transport).then(function (result) {
      var best = result.best;
      if (!best) {
        throw new Error(
          "No muxed (video+audio) stream was returned for this video."
        );
      }
      return {
        kind: "youtube",
        videoId: result.videoId,
        title: result.title,
        author: result.author,
        duration: result.duration,
        directUrl: best.url,
        playbackUrl: transport.playbackUrl(best.url),
        size: best.size,
        height: best.height,
        transport: transport.name,
        resolvedWith: result.resolvedWith,
      };
    });
  }

  /* ------------------------------ OPFS caching ------------------------------ */

  /**
   * cacheSource(playbackInfo, { onProgress, onTaskProgress, taskTimeoutMs })
   *   -> Promise<{ file, size }>
   * Streams the source into OPFS (Origin Private File System) in 8 MB
   * range chunks so tab RAM stays flat regardless of file size. vidssave
   * infos additionally wait on their muxing task first (onTaskProgress
   * receives the SSE progress 0..100 while the file is prepared).
   */
  /* vidssave infos carry no URL until their muxing task finishes — resolve
   * it (memoized) and hand back the same-origin proxied URL to fetch. */
  function ensureStream(info, opts) {
    if (info.kind !== "vidssave") return Promise.resolve(null);
    if (info.playbackUrl) return Promise.resolve(info.playbackUrl);
    return info.getStreamUrl(opts).then(function (res) {
      return res.proxied;
    });
  }

  function cacheSource(info, opts) {
    opts = opts || {};
    var onProgress = opts.onProgress || function () {};
    var transport = availableTransport();

    var opfsRoot;
    var handle;
    var writer;
    var total = info.size || null;
    var mediaFetchUrl = null; // vidssave: proxied URL once the task lands

    return Promise.resolve()
      .then(function () {
        if (!navigator.storage || !navigator.storage.getDirectory) {
          throw new Error("OPFS is not available in this browser.");
        }
        return navigator.storage.getDirectory();
      })
      .then(function (root) {
        opfsRoot = root;
        return root.getFileHandle("ytcc-source.mp4", { create: true });
      })
      .then(function (fh) {
        handle = fh;
        return fh.createWritable();
      })
      .then(function (w) {
        writer = w;
        /* vidssave: run the muxing task first (its progress is reported
         * through opts.onTaskProgress by app.js); the SSE filesize also
         * refines our total before the probe. */
        return ensureStream(info, {
          onProgress: opts.onTaskProgress,
          timeoutMs: opts.taskTimeoutMs,
        }).then(function (url) {
          if (url) {
            mediaFetchUrl = url;
            if (info.size) total = info.size;
          }
        });
      })
      .then(function () {
        if (mediaFetchUrl && total != null) {
          /* vidssave: the SSE task already reported the exact muxed filesize —
           * skip the probe entirely (their CDN's 206s don't always carry
           * Content-Range, and every spared request avoids a flaky hop). */
          if (total > MAX_SOURCE_BYTES) {
            throw new Error(
              "Source is " +
                (total / 1048576).toFixed(0) +
                " MB — the browser engine caps at 400 MB. Use the server engine for this video."
            );
          }
          return downloadLoop();
        }
        return probe().then(function () {
          if (total != null && total > MAX_SOURCE_BYTES) {
            throw new Error(
              "Source is " +
                (total / 1048576).toFixed(0) +
                " MB — the browser engine caps at 400 MB. Use the server engine for this video."
            );
          }
          return downloadLoop();
        });
      })
      .then(function (bytesWritten) {
        return writer.close().then(function () {
          return handle.getFile();
        }).then(function (file) {
          if (file.size !== bytesWritten) {
            throw new Error(
              "Cached file is incomplete (" + file.size + " of " + bytesWritten + " bytes)."
            );
          }
          return { file: file, size: file.size };
        });
      })
      .catch(function (err) {
        if (writer) {
          try {
            writer.abort();
          } catch (e) {
            /* ignore */
          }
        }
        throw err;
      });

    function probe() {
      /* A tiny ranged GET reveals the total size (and warms nothing).
       * Same URL construction as the download loop — through the transport
       * or the backend media proxy, never a direct cross-origin hit. */
      var rangeInit = { headers: { Range: "bytes=0-0" } };
      var p = mediaFetchUrl
        ? fetch(mediaFetchUrl, rangeInit)
        : info.kind === "direct"
          ? fetch(info.directUrl, rangeInit)
          : transport.openStream(buildStreamUrl(info), rangeInit);
      return p.then(function (response) {
        if (response.status === 206) {
          var cr = response.headers.get("Content-Range"); // bytes 0-0/12345
          var m = cr && cr.match(/\/(\d+)$/);
          if (m) total = Number(m[1]);
          /* discard the 1-byte body */
          return response.arrayBuffer().then(function () {
            return response.status;
          });
        }
        /* No range support: consume nothing, remember it. */
        return response.status;
      });
    }

    function downloadLoop() {
      var pos = 0;
      var noRange = false;
      /* vidssave's CDN intermittently 403s perfectly good signed links
       * (observed live on their staging pipeline) — retry those with a
       * backoff, resuming from pos, instead of failing the download. */
      var RETRYABLE = { 403: 1, 408: 1, 429: 1, 500: 1, 502: 1, 503: 1, 504: 1 };
      var MAX_TRIES = 5;

      function delay(ms) {
        return new Promise(function (resolve) {
          setTimeout(resolve, ms);
        });
      }

      function next() {
        if (total != null && pos >= total) return Promise.resolve(pos);
        if (noRange && pos > 0) return Promise.resolve(pos); // full body consumed

        var init = {};
        if (!noRange) {
          var end = total != null ? Math.min(pos + CHUNK - 1, total - 1) : pos + CHUNK - 1;
          init.headers = { Range: "bytes=" + pos + "-" + end };
        }

        return attemptFetch(init, 0).then(function (bytes) {
          if (bytes === 0) {
            /* nothing more to read — the reported total was optimistic */
            total = pos;
            return pos;
          }
          pos += bytes;
          if (total != null) {
            onProgress(Math.min(1, pos / total), pos, total);
          } else {
            onProgress(null, pos, null);
          }
          return next();
        });
      }

      function attemptFetch(init, tries) {
        var requestPromise = mediaFetchUrl
          ? fetch(mediaFetchUrl, init)
          : info.kind === "direct"
            ? fetch(info.directUrl, init)
            : transport.openStream(buildStreamUrl(info), init);

        return requestPromise.then(function (response) {
          if (!response.ok && response.status !== 206) {
            if (RETRYABLE[response.status] && tries < MAX_TRIES) {
              return delay(800 * (tries + 1)).then(function () {
                return attemptFetch(init, tries + 1);
              });
            }
            if (response.status === 416) return 0; // past EOF — we are done
            throw new Error("Download failed with HTTP " + response.status);
          }
          if (response.status === 200 && pos === 0 && total == null) {
            var len = response.headers.get("Content-Length");
            if (len) total = Number(len);
          }
          if (response.status === 200 && init.headers && pos > 0) {
            /* asked to resume but got the whole file from byte 0 — the
             * bytes before pos are already written, so retry; if the host
             * insists, fail with a clear message. */
            if (tries < MAX_TRIES) {
              return delay(800 * (tries + 1)).then(function () {
                return attemptFetch(init, tries + 1);
              });
            }
            throw new Error("The media host refused to resume the download.");
          }
          if (response.status === 200 && init.headers) {
            noRange = true; // server ignored Range — full body from here on
          }
          return pumpBody(response);
        });
      }

      function pumpBody(response) {
        var reader = response.body.getReader();
        var bytes = 0;
        function step() {
          return reader.read().then(function (r) {
            if (r.done) return bytes;
            bytes += r.value.byteLength;
            return writer.write(r.value).then(function () {
              if (total != null) {
                onProgress(Math.min(0.999, (pos + bytes) / total), pos + bytes, total);
              }
              return step();
            });
          });
        }
        return step();
      }

      return next();
    }
  }

  function buildStreamUrl(info) {
    /* The proxy transport's openStream already targets the right place when
     * given the PROXIED url; the direct transport gets the raw URL. */
    var transport = availableTransport();
    if (transport.name === "extension") return info.directUrl;
    return proxiedStreamUrl(info.directUrl);
  }

  /* ------------------------------ filmstrip ------------------------------ */

  /**
   * generateThumbs(file, duration, count) -> Promise<[dataURL,…]>
   * Draws evenly-spaced frames from the LOCAL cached file — no network,
   * no server ffmpeg.
   */
  function generateThumbs(file, duration, count) {
    count = count || 60;
    var objectUrl = URL.createObjectURL(file);
    var video = document.createElement("video");
    video.muted = true;
    video.preload = "auto";
    video.playsInline = true;
    video.src = objectUrl;

    var W = 128;
    var H = 72;
    var canvas = document.createElement("canvas");
    canvas.width = W;
    canvas.height = H;
    var ctx = canvas.getContext("2d");

    var seekedResolve;
    var seekedReject;

    function seek(t) {
      return new Promise(function (resolve, reject) {
        seekedResolve = resolve;
        seekedReject = reject;
        video.currentTime = t;
        setTimeout(function () {
          if (seekedResolve === resolve) {
            seekedResolve = null;
            resolve(); // defensive timeout — draw whatever frame is current
          }
        }, 2500);
      });
    }

    video.addEventListener("seeked", function () {
      var resolve = seekedResolve;
      seekedResolve = null;
      if (resolve) resolve();
    });
    video.addEventListener("error", function () {
      var reject = seekedReject;
      seekedReject = null;
      if (reject) reject(new Error("Could not decode the cached video."));
    });

    return new Promise(function (resolve, reject) {
      video.addEventListener("loadedmetadata", function () {
        if (video.videoWidth && video.videoHeight) {
          H = Math.round((W * video.videoHeight) / video.videoWidth) || 72;
          canvas.height = H;
        }
        var thumbs = [];
        var index = 0;

        function grab() {
          if (index >= count) {
            URL.revokeObjectURL(objectUrl);
            resolve(thumbs);
            return;
          }
          var t = ((index + 0.5) / count) * duration;
          seek(t)
            .then(function () {
              try {
                ctx.drawImage(video, 0, 0, W, H);
                thumbs.push(canvas.toDataURL("image/jpeg", 0.55));
              } catch (e) {
                thumbs.push(""); // keep tile alignment even on decode hiccups
              }
              index += 1;
              grab();
            })
            .catch(reject);
        }
        grab();
      });
      video.addEventListener("error", function () {
        reject(new Error("Could not open the cached video for thumbnails."));
      });
      video.load();
    });
  }

  /* --------------------------------- clip --------------------------------- */

  /**
   * clip({ file, start, end, background, resolution, onProgress, onLog })
   *   -> Promise<{ blob, url, duration, size }>
   *
   * Ports the backend "Original" style 1:1 — 9:16 frame, source centered,
   * blurred-backdrop or black fill — at 1080×1920 or 720×1280 (fast).
   */
  function clip(opts) {
    var start = Math.max(0, Number(opts.start) || 0);
    var end = Math.max(start + 0.1, Number(opts.end) || start + 1);
    var duration = end - start;
    var background = opts.background === "black" ? "black" : "blur";
    var res = opts.resolution === 1080 ? [1080, 1920] : [720, 1280];
    var w = res[0];
    var h = res[1];

    var vf, maps;
    if (background === "black") {
      vf =
        "scale=" + w + ":" + h + ":force_original_aspect_ratio=decrease:" +
        "force_divisible_by=2,pad=" + w + ":" + h + ":(ow-iw)/2:(oh-ih)/2:black," +
        "setsar=1,format=yuv420p";
      maps = ["-vf", vf];
    } else {
      var fc =
        "[0:v]split=2[bg][fg];" +
        "[bg]scale=" + (w / 5) + ":" + (h / 5) + ":force_original_aspect_ratio=increase," +
        "crop=" + (w / 5) + ":" + (h / 5) + ",setsar=1,gblur=sigma=5," +
        "scale=" + w + ":" + h + ",setsar=1[bgb];" +
        "[fg]scale=" + w + ":" + h + ":force_original_aspect_ratio=decrease:" +
        "force_divisible_by=2,setsar=1[fgc];" +
        "[bgb][fgc]overlay=(W-w)/2:(H-h)/2,format=yuv420p[vout]";
      maps = ["-filter_complex", fc, "-map", "[vout]", "-map", "0:a?"];
    }

    var args = [
      "-ss", start.toFixed(3),
      "-i", "in.mp4",
      "-t", duration.toFixed(3),
    ].concat(maps, [
      "-c:v", "libx264",
      "-preset", "veryfast",
      "-crf", "20",
      "-pix_fmt", "yuv420p",
      "-c:a", "aac",
      "-b:a", "128k",
      "-movflags", "+faststart",
    ]);

    return runFFmpeg({
      file: opts.file,
      args: args,
      onProgress: opts.onProgress,
      onLog: opts.onLog,
    }).then(function (buffer) {
      var blob = new Blob([buffer], { type: "video/mp4" });
      return {
        blob: blob,
        url: URL.createObjectURL(blob),
        duration: duration,
        size: blob.size,
        width: w,
        height: h,
      };
    });
  }

  /* ------------------------------ ffmpeg worker ------------------------------ */

  var worker = null;
  var workerUrl = null;

  function getWorker() {
    if (worker) return worker;
    var src = "js/ffmpeg-client.js" + (window.location.search || "");
    worker = new Worker(src, { type: "module" });
    workerUrl = src;
    return worker;
  }

  function runFFmpeg(job) {
    return new Promise(function (resolve, reject) {
      var w = getWorker();
      var onMessage = function (event) {
        var msg = event.data || {};
        if (msg.type === "progress" && job.onProgress) job.onProgress(msg.ratio);
        else if (msg.type === "log" && job.onLog) job.onLog(msg.text);
        else if (msg.type === "done") {
          cleanup();
          resolve(msg.data);
        } else if (msg.type === "error") {
          cleanup();
          reject(new Error(msg.message));
        }
      };
      var onError = function (event) {
        cleanup();
        reject(new Error("ffmpeg worker crashed: " + (event.message || "unknown error")));
      };
      function cleanup() {
        w.removeEventListener("message", onMessage);
        w.removeEventListener("error", onError);
      }
      w.addEventListener("message", onMessage);
      w.addEventListener("error", onError);
      /* NOTE: no transfer list — job.file is an OPFS File (cloneable, cheap:
       * structured clone passes a reference to the same blob storage, not a
       * byte copy), and only ArrayBuffer/MessagePort are transferable. */
      w.postMessage({
        type: "clip",
        file: job.file,
        args: job.args,
        inName: "in.mp4",
        outName: "out.mp4",
      });
    });
  }

  /* Preload the wasm core (called opportunistically after a source caches). */
  function warmUp() {
    try {
      var w = getWorker();
      w.postMessage({ type: "load" });
      return true;
    } catch (e) {
      return false;
    }
  }

  window.ClientEngine = {
    available: available,
    resolve: resolve,
    cacheSource: cacheSource,
    generateThumbs: generateThumbs,
    clip: clip,
    warmUp: warmUp,
    proxyUrl: proxyUrl,
    backendMediaProxyUrl: backendMediaProxyUrl,
    MAX_SOURCE_BYTES: MAX_SOURCE_BYTES,
  };
})();
