/* YouTube Clipper — Timeline component (no dependencies).
 *
 * A scrollable filmstrip with a dual-handle selection, a time ruler with
 * adaptive ticks, a draggable playhead, and pan/zoom. Exposed as
 * window.Timeline; app.js owns the video element and calls back through
 * onChange (selection moved) and onSeek (playhead scrubbed).
 */
"use strict";

(function () {
  var SNAP = 0.05; // seconds
  var MIN_PPS = 1.5; // px per second lower zoom bound
  var MAX_PPS = 480; // upper zoom bound
  var HANDLE_KEY_STEP = 0.5; // seconds per arrow press

  var STEPS = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];

  function clamp(v, lo, hi) {
    return Math.min(hi, Math.max(lo, v));
  }

  function snap(t) {
    return Math.round(t / SNAP) * SNAP;
  }

  function fmtRuler(t) {
    t = Math.max(0, t);
    var h = Math.floor(t / 3600);
    var m = Math.floor((t % 3600) / 60);
    var s = t - 60 * (m + 60 * h);
    if (h > 0) return h + ":" + String(m).padStart(2, "0") + ":" + String(Math.floor(s)).padStart(2, "0");
    return m + ":" + String(Math.floor(s)).padStart(2, "0");
  }

  function Timeline(opts) {
    this.viewport = opts.viewport;
    this.track = opts.track;
    this.strip = opts.strip;
    this.ruler = opts.ruler;
    this.handleL = opts.handleL;
    this.handleR = opts.handleR;
    this.dimL = opts.dimL;
    this.dimR = opts.dimR;
    this.sel = opts.sel;
    this.playhead = opts.playhead;
    this.duration = opts.duration || 0;
    this.maxLen = Math.min(opts.maxLen || this.duration || Infinity, this.duration || Infinity);
    this.minLen = Math.min(opts.minLen != null ? opts.minLen : 1, this.duration || 1);
    this.thumbs = opts.thumbs || [];
    this.progressEl = opts.progress || null;
    this.onChange = opts.onChange || function () {};
    this.onSeek = opts.onSeek || function () {};

    this.pps = 10;
    this.selStart = 0;
    this.selEnd = Math.min(this.minLen, this.duration);
    this.playT = 0;
    this.userZoomed = false;
    this._tiles = [];
    this._raf = 0;

    this._buildStrip();
    this._bind();
    this.fit();
  }

  /* ---------------- geometry ---------------- */

  Timeline.prototype.trackWidth = function () {
    return Math.max(this.viewport.clientWidth, this.duration * this.pps);
  };

  Timeline.prototype.xOf = function (t) {
    return t * this.pps;
  };

  Timeline.prototype.tOf = function (x) {
    return clamp(x / this.pps, 0, this.duration);
  };

  Timeline.prototype.fit = function () {
    var w = this.viewport.clientWidth || 600;
    this.pps = clamp(w / Math.max(this.duration, 0.001), MIN_PPS, MAX_PPS);
    this.userZoomed = false;
    this.layout();
  };

  Timeline.prototype.zoom = function (factor, anchorTime) {
    var before = this.pps;
    this.pps = clamp(this.pps * factor, MIN_PPS, MAX_PPS);
    this.userZoomed = true;
    this.layout();
    if (anchorTime != null && this.pps !== before) {
      // keep the anchor time at the same viewport x it had before zooming
      this.viewport.scrollLeft += anchorTime * this.pps - anchorTime * before;
    }
  };

  Timeline.prototype.scrollIntoView = function (t, margin) {
    var x = this.xOf(t);
    var sl = this.viewport.scrollLeft;
    var vw = this.viewport.clientWidth;
    var m = margin || 64;
    if (x < sl || x > sl + vw - m) {
      this.viewport.scrollLeft = clamp(x - m, 0, this.trackWidth());
    }
  };

  /* ---------------- filmstrip ---------------- */

  Timeline.prototype._buildStrip = function () {
    this.strip.innerHTML = "";
    var frag = document.createDocumentFragment();
    for (var i = 0; i < this.thumbs.length; i++) {
      var img = document.createElement("img");
      img.src = this.thumbs[i];
      img.alt = "";
      img.draggable = false;
      frag.appendChild(img);
    }
    this.strip.appendChild(frag);
    this._tiles = Array.prototype.slice.call(this.strip.children);
  };

  /* Live upgrades while the background download runs: the filmstrip arrives
   * after the timeline is already on screen (thumbnails are generated from
   * the finished file) and the cache progress fills the track underneath. */
  Timeline.prototype.setThumbs = function (urls) {
    this.thumbs = urls || [];
    this._buildStrip();
    this.layout();
  };

  Timeline.prototype.setProgress = function (fraction) {
    var el = this.progressEl;
    if (!el) return;
    if (fraction == null) {
      el.classList.remove("on", "fill");
      el.classList.add("indet");
      return;
    }
    var f = Math.max(0, Math.min(1, Number(fraction) || 0));
    if (f >= 0.999) {
      el.classList.remove("on", "indet");
      el.classList.add("fill");
      return;
    }
    el.classList.remove("indet", "fill");
    el.classList.add("on");
    el.style.setProperty("--pct", (f * 100).toFixed(1) + "%");
  };

  Timeline.prototype.clearProgress = function () {
    var el = this.progressEl;
    if (!el) return;
    el.classList.remove("on", "indet", "fill");
  };

  Timeline.prototype.layout = function () {
    var w = this.trackWidth();
    this.track.style.width = w + "px";
    if (this._tiles.length) {
      var tileW = w / this._tiles.length;
      for (var i = 0; i < this._tiles.length; i++) {
        this._tiles[i].style.width = tileW + "px";
      }
    }
    this._placeSelection();
    this._placePlayhead();
    this._drawRuler();
  };

  /* ---------------- selection ---------------- */

  Timeline.prototype.setSelection = function (start, end, opts) {
    opts = opts || {};
    start = snap(clamp(start, 0, this.duration));
    end = snap(clamp(end, 0, this.duration));
    if (end - start < this.minLen) {
      if (opts.anchor === "end") start = end - this.minLen;
      else end = start + this.minLen;
      start = clamp(start, 0, Math.max(0, this.duration - this.minLen));
      end = clamp(end, this.minLen, this.duration);
    }
    if (end - start > this.maxLen) {
      if (opts.anchor === "end") start = end - this.maxLen;
      else end = start + this.maxLen;
    }
    this.selStart = start;
    this.selEnd = end;
    this._placeSelection();
    this.onChange(start, end);
  };

  Timeline.prototype._placeSelection = function () {
    var l = this.xOf(this.selStart);
    var r = this.xOf(this.selEnd);
    this.sel.style.left = l + "px";
    this.sel.style.width = Math.max(2, r - l) + "px";
    this.dimL.style.width = l + "px";
    this.dimR.style.left = r + "px";
    this.dimR.style.width = Math.max(0, this.trackWidth() - r) + "px";
    this.handleL.style.left = l + "px";
    this.handleR.style.left = r + "px";
    this.handleL.setAttribute("aria-valuemax", this.duration);
    this.handleR.setAttribute("aria-valuemax", this.duration);
    this.handleL.setAttribute("aria-valuenow", this.selStart);
    this.handleR.setAttribute("aria-valuenow", this.selEnd);
    this.handleL.setAttribute("aria-valuetext", this.selStart.toFixed(1) + "s");
    this.handleR.setAttribute("aria-valuetext", this.selEnd.toFixed(1) + "s");
  };

  Timeline.prototype._bubble = function (handle, text) {
    var b = handle.querySelector(".bubble");
    if (!b) return;
    if (text != null) {
      b.textContent = text;
      b.classList.add("show");
    } else {
      b.classList.remove("show");
    }
  };

  /* ---------------- playhead ---------------- */

  Timeline.prototype.setPlayhead = function (t) {
    this.playT = clamp(t, 0, this.duration);
    this._placePlayhead();
    this._drawRuler();
  };

  Timeline.prototype._placePlayhead = function () {
    this.playhead.style.left = this.xOf(this.playT) + "px";
  };

  /* ---------------- ruler ---------------- */

  Timeline.prototype._drawRuler = function () {
    var canvas = this.ruler;
    var vw = this.viewport.clientWidth || 600;
    var dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(vw * dpr)) {
      canvas.width = Math.round(vw * dpr);
      canvas.height = Math.round(26 * dpr);
      canvas.style.width = vw + "px";
      canvas.style.height = "26px";
    }
    var ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, vw, 26);

    var sl = this.viewport.scrollLeft;
    var t0 = this.tOf(sl);
    var t1 = this.tOf(sl + vw);

    var step = STEPS[STEPS.length - 1];
    for (var i = 0; i < STEPS.length; i++) {
      if (STEPS[i] * this.pps >= 78) {
        step = STEPS[i];
        break;
      }
    }
    var minor = step / 5;

    ctx.lineWidth = 1;
    ctx.strokeStyle = "rgba(232,235,242,0.14)";
    ctx.beginPath();
    for (var t = Math.floor(t0 / minor) * minor; t <= t1; t += minor) {
      var x = Math.round(this.xOf(t) - sl) + 0.5;
      ctx.moveTo(x, 19);
      ctx.lineTo(x, 26);
    }
    ctx.stroke();

    ctx.strokeStyle = "rgba(232,235,242,0.42)";
    ctx.beginPath();
    var first = Math.ceil(t0 / step) * step;
    for (t = first; t <= t1 + 1e-9; t += step) {
      x = Math.round(this.xOf(t) - sl) + 0.5;
      ctx.moveTo(x, 11);
      ctx.lineTo(x, 26);
    }
    ctx.stroke();

    ctx.font = "10px ui-monospace, SFMono-Regular, Menlo, monospace";
    ctx.fillStyle = "rgba(152,161,179,0.95)";
    ctx.textBaseline = "top";
    for (t = first; t <= t1 + 1e-9; t += step) {
      x = this.xOf(t) - sl;
      ctx.fillText(fmtRuler(t), x + 4, 2);
    }

    // playhead marker on the ruler
    var px = this.xOf(this.playT) - sl;
    if (px >= -6 && px <= vw + 6) {
      ctx.fillStyle = "#ff4438";
      ctx.beginPath();
      ctx.moveTo(px - 5, 26);
      ctx.lineTo(px + 5, 26);
      ctx.lineTo(px, 15);
      ctx.closePath();
      ctx.fill();
    }
  };

  /* ---------------- interactions ---------------- */

  Timeline.prototype._timeAtEvent = function (ev) {
    var rect = this.viewport.getBoundingClientRect();
    return this.tOf(this.viewport.scrollLeft + (ev.clientX - rect.left));
  };

  Timeline.prototype._bind = function () {
    this.viewport.addEventListener(
      "wheel",
      (e) => {
        if (e.ctrlKey || e.metaKey) {
          e.preventDefault();
          const rect = this.viewport.getBoundingClientRect();
          const anchor = this.tOf(this.viewport.scrollLeft + (e.clientX - rect.left));
          this.zoom(Math.pow(1.0018, -e.deltaY), anchor);
        } else if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
          e.preventDefault();
          this.viewport.scrollLeft += e.deltaY;
        }
      },
      { passive: false }
    );

    this.viewport.addEventListener("scroll", () => {
      if (this._raf) return;
      this._raf = requestAnimationFrame(() => {
        this._raf = 0;
        this._drawRuler();
      });
    });

    this._onResize = () => {
      if (!this.userZoomed) this.fit();
      else this.layout();
    };
    window.addEventListener("resize", this._onResize);

    // scrub by dragging on the ruler
    this.ruler.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      const move = (ev) => {
        const rect = this.ruler.getBoundingClientRect();
        this.onSeek(this.tOf(this.viewport.scrollLeft + (ev.clientX - rect.left)));
      };
      move(e);
      const up = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    });

    // everything inside the track
    this.track.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      const handle = e.target.closest ? e.target.closest(".tl-handle") : null;
      const x0 = e.clientX;
      const t0 = this._timeAtEvent(e);
      let mode;
      if (handle === this.handleL) mode = "left";
      else if (handle === this.handleR) mode = "right";
      else if (this.sel.contains(e.target)) mode = "move";
      else mode = "new";

      const startSelStart = this.selStart;
      const startSelEnd = this.selEnd;
      let moved = false;
      document.body.classList.add("tl-dragging");

      const move = (ev) => {
        if (Math.abs(ev.clientX - x0) > 3) moved = true;
        if (!moved) return;
        const t = this._timeAtEvent(ev);
        if (mode === "left") {
          this.setSelection(Math.min(t, this.selEnd - SNAP), this.selEnd);
          this._bubble(this.handleL, this.selStart.toFixed(1) + "s");
          this.onSeek(this.selStart);
        } else if (mode === "right") {
          this.setSelection(this.selStart, Math.max(t, this.selStart + SNAP), { anchor: "end" });
          this._bubble(this.handleR, this.selEnd.toFixed(1) + "s");
          this.onSeek(this.selEnd);
        } else if (mode === "move") {
          const len = startSelEnd - startSelStart;
          let s = snap(t - len / 2);
          s = clamp(s, 0, Math.max(0, this.duration - len));
          this.setSelection(s, s + len);
        } else {
          const a = Math.min(t0, t);
          const b = Math.max(t0, t);
          if (b - a >= this.minLen) this.setSelection(a, b);
        }
      };

      const up = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
        document.body.classList.remove("tl-dragging");
        this._bubble(this.handleL, null);
        this._bubble(this.handleR, null);
        if (!moved && mode === "new") this.onSeek(t0);
      };

      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    });

    // keyboard: nudge the focused handle
    const keyNudge = (e, which) => {
      const step = e.shiftKey ? HANDLE_KEY_STEP * 10 : HANDLE_KEY_STEP;
      let t = null;
      if (e.key === "ArrowLeft") t = -step;
      else if (e.key === "ArrowRight") t = step;
      else if (e.key === "Home") t = -Infinity;
      else if (e.key === "End") t = Infinity;
      if (t === null) return;
      e.preventDefault();
      if (which === "left") {
        const target = t === -Infinity ? 0 : t === Infinity ? this.selEnd : this.selStart + t;
        this.setSelection(Math.min(target, this.selEnd - SNAP), this.selEnd);
        this.scrollIntoView(this.selStart);
      } else {
        const target = t === -Infinity ? this.selStart : t === Infinity ? this.duration : this.selEnd + t;
        this.setSelection(this.selStart, Math.max(target, this.selStart + SNAP), { anchor: "end" });
        this.scrollIntoView(this.selEnd);
      }
    };
    this.handleL.addEventListener("keydown", (e) => keyNudge(e, "left"));
    this.handleR.addEventListener("keydown", (e) => keyNudge(e, "right"));
  };

Timeline.prototype.destroy = function () {
    window.removeEventListener("resize", this._onResize);
    if (this._raf) cancelAnimationFrame(this._raf);
  };

  window.Timeline = Timeline;
})();
