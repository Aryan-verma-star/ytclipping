/* YTResolver — Layer 1 of the client-side architecture: stream resolution.
 *
 * Speaks YouTube's innertube player API DIRECTLY FROM THE BROWSER (or via
 * the companion extension, which uses the user's own IP + cookies). Client
 * contexts mirror the current yt-dlp configuration (2026-08):
 *
 *   TVHTML5 (Cobalt UA) — no PO-token requirement, best first bet
 *   ANDROID              — pre-signed URLs when accepted
 *   IOS                  — same
 *   TVHTML5_SIMPLY       — last resort
 *
 * The resolver never touches the network itself — it takes a `transport`:
 *   { post(path, body, headers) -> Promise<playerJSON> }
 * which is either the CORS proxy (proxy/worker.js /yti) or the extension
 * bridge (background fetch, user's residential IP).
 *
 * Exposed as window.YTResolver.
 */
"use strict";

(function () {
  /* Client contexts (kept in sync with yt-dlp 2026.08 _base.py). */
  var CLIENTS = [
    {
      name: "TVHTML5",
      clientName: "TVHTML5",
      clientVersion: "7.20260707.07.00",
      clientHeader: "7",
      userAgent:
        "Mozilla/5.0 (ChromiumStylePlatform) Cobalt/25.lts.30.1034943-gold (unlike Gecko), Unknown_TV_Unknown_0/Unknown (Unknown, Unknown)",
    },
    {
      name: "ANDROID",
      clientName: "ANDROID",
      clientVersion: "21.26.364",
      clientHeader: "3",
      userAgent: "com.google.android.youtube/21.26.364 (Linux; U; Android 11) gzip",
      extra: { androidSdkVersion: 30, osName: "Android", osVersion: "11", gl: "US" },
    },
    {
      name: "IOS",
      clientName: "IOS",
      clientVersion: "21.26.4",
      clientHeader: "5",
      userAgent:
        "com.google.ios.youtube/21.26.4 (iPhone16,2; U; CPU iOS 18_3_2 like Mac OS X;)",
      extra: { deviceMake: "Apple", deviceModel: "iPhone16,2", osName: "iPhone", osVersion: "18.3.2.22D82", gl: "US" },
    },
    {
      name: "TVHTML5_SIMPLY",
      clientName: "TVHTML5_SIMPLY",
      clientVersion: "1.0",
      clientHeader: "75",
      userAgent:
        "Mozilla/5.0 (PlayStation; PlayStation 4/12.00) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.0 Safari/605.1.15",
    },
  ];

  /* Muxed (video+audio) itags, best first. 22 = 720p, 18 = 360p. */
  var MUXED_ITAGS = [22, 18];

  function videoIdFromUrl(url) {
    var m =
      url.match(/(?:youtube\.com\/(?:watch\?.*?v=|shorts\/|embed\/|live\/)|youtu\.be\/)([a-zA-Z0-9_-]{11})/) ||
      url.match(/^[a-zA-Z0-9_-]{11}$/);
    return m ? m[1] : null;
  }

  function isYouTubeUrl(url) {
    return /((www|m|music)\.)?(youtube\.com|youtu\.be)\//i.test(url);
  }

  /* Direct, already-playable media URLs (also used for tests + self-hosted
   * sources): http(s)….(mp4|webm|mov|m4v|mkv) with optional query. */
  function directMediaUrl(url) {
    return /^https?:\/\/\S+\.(mp4|webm|mov|m4v|mkv)(\?\S*)?$/i.test(url.trim());
  }

  function mimeTypeIs(mime, type) {
    return String(mime || "").split(";")[0].trim().toLowerCase() === type;
  }

  /* Parse one format entry from a player response. */
  function parseFormat(entry) {
    if (!entry || !entry.itag) return null;
    var url = entry.url || null;
    if (!url && entry.signatureCipher) {
      /* WEB-client ciphered URL — the client engine cannot decipher these
       * without executing YouTube's player JS; skip (TV/Android/IOS clients
       * return pre-signed urls). */
      return null;
    }
    var mime = entry.mimeType || "";
    var isVideo = mime.indexOf("video/") === 0;
    var isAudio = mimeTypeIs(mime, "audio/mp4");
    return {
      itag: entry.itag,
      url: url,
      mimeType: mime,
      video: isVideo,
      audio: isAudio || (!isVideo && String(entry.audioQuality || "").length > 0),
      width: entry.width || null,
      height: entry.height || null,
      fps: entry.fps || null,
      qualityLabel: entry.qualityLabel || entry.quality || null,
      bitrate: entry.bitrate || null,
      size: entry.contentLength ? Number(entry.contentLength) : null,
      durationMs: entry.approxDurationMs ? Number(entry.approxDurationMs) : null,
    };
  }

  /* Ask the transport for a player response with one client context. */
  function fetchPlayer(transport, videoId, client, signal) {
    var context = {
      client: {
        clientName: client.clientName,
        clientVersion: client.clientVersion,
        hl: "en",
      },
    };
    Object.assign(context.client, client.extra || {});
    return transport.post(
      "/youtubei/v1/player",
      {
        context: context,
        videoId: videoId,
        contentCheckOk: true,
        racyCheckOk: true,
      },
      {
        "User-Agent": client.userAgent,
        "X-YouTube-Client-Name": client.clientHeader,
        "X-YouTube-Client-Version": client.clientVersion,
        "Accept-Language": "en-US,en;q=0.9",
      },
      signal
    );
  }

  /**
   * resolve(videoId, transport, opts) -> Promise<result>
   * result: {
   *   videoId, title, author, duration,
   *   formats: [...] (all usable formats, url present),
   *   best: format|null (best muxed),
   *   resolvedWith: client name,
   *   playability, reason
   * }
   * Rejects only on transport-level failures; YouTube-level refusals are
   * reported via resolve() of the LAST client with playability/reason set,
   * so the UI can show the exact reason.
   */
  function resolve(videoId, transport, opts) {
    opts = opts || {};
    var clients = opts.clients || CLIENTS;
    var index = 0;
    var lastResult = null;

    function attempt() {
      if (index >= clients.length) {
        var err = new Error(
          (lastResult && lastResult.reason) ||
            "YouTube refused to return streams for this video."
        );
        err.playability = lastResult ? lastResult.playability : null;
        err.exhausted = true;
        return Promise.reject(err);
      }
      var client = clients[index++];
      return fetchPlayer(transport, videoId, client, opts.signal).then(function (data) {
        var playability =
          data && data.playabilityStatus ? data.playabilityStatus.status : "UNKNOWN";
        var reason =
          data && data.playabilityStatus ? data.playabilityStatus.reason || "" : "";
        var sd = (data && data.streamingData) || {};
        var rawFormats = (sd.formats || []).concat(sd.adaptiveFormats || []);
        var formats = [];
        rawFormats.forEach(function (entry) {
          var f = parseFormat(entry);
          if (f) formats.push(f);
        });
        var details = (data && data.videoDetails) || {};

        if (playability !== "OK" || !formats.length) {
          lastResult = { playability: playability, reason: reason, resolvedWith: client.name };
          if (playability === "LOGIN_REQUIRED") {
            /* IP reputation refusal — no point hammering other contexts */
            var err = new Error(
              "YouTube asked this network to sign in (bot check). " +
                "Try the companion extension, the Cloudflare proxy, or the server engine."
            );
            err.playability = playability;
            err.exhausted = true;
            return Promise.reject(err);
          }
          return attempt(); // try the next client context
        }

        var muxed = formats.filter(function (f) {
          return f.video && f.audio;
        });
        muxed.sort(function (a, b) {
          var ia = MUXED_ITAGS.indexOf(a.itag);
          var ib = MUXED_ITAGS.indexOf(b.itag);
          if (ia !== -1 || ib !== -1) {
            return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
          }
          return (b.height || 0) - (a.height || 0);
        });

        return {
          videoId: videoId,
          title: details.title || videoId,
          author: details.author || "",
          duration: details.lengthSeconds ? Number(details.lengthSeconds) : null,
          formats: formats,
          best: muxed[0] || null,
          resolvedWith: client.name,
          playability: playability,
          reason: reason,
        };
      });
    }

    return attempt();
  }

  window.YTResolver = {
    resolve: resolve,
    videoIdFromUrl: videoIdFromUrl,
    isYouTubeUrl: isYouTubeUrl,
    directMediaUrl: directMediaUrl,
  };
})();
