/* ============================================================
 * Wallpaper Reveal — 水墨晕染遮罩（ES Module）
 * ------------------------------------------------------------
 * 原 wallpaper-reveal.js IIFE 改为 ES module export。
 *
 * 对接现有 DOM：
 *   <div id="wallpaper"></div>          （hero，壁纸背景层）
 *   <canvas id="wallpaper-mask">        （遮罩 canvas，aria-hidden）
 *
 * #wallpaper / #wallpaper-mask 的 z-index 为负值（-4 / -3），
 * 在内容层之下收不到鼠标事件，因此监听 window 并把
 * clientX/Y 映射到 canvas 自身坐标系。
 * 墨色随主题 --bg-page 变化（MD3 / Fluent2 切换时自动跟随）。
 * 触屏设备（no-hover）直接跳过，回退到壁纸静态展示。
 * ============================================================ */

export function initWallpaperReveal(options) {
  options = options || {};
  var heroId = options.heroId || "wallpaper";
  var maskId = options.maskId || "wallpaper-mask";

  var hero = document.getElementById(heroId);
  var canvas = document.getElementById(maskId);
  if (!hero || !canvas) return null;

  // 触屏设备无 hover，直接显示壁纸更自然，不启用 canvas
  if (!window.matchMedia || !window.matchMedia("(hover: hover)").matches) return null;
  var ctx = canvas.getContext("2d");
  if (!ctx) return null;

  // ---- 参数 ----
  var DPR = Math.min(window.devicePixelRatio || 1, 2);
  var R_START = 8;
  var R_END = 140;
  var R_VARY = 0.30;
  var LIFETIME = 1800;
  var STAMP_STEP = 10;
  var MAX_STAMPS = 160;
  var BRUSH_SIZE = 320;
  var BRUSH_VARIANTS = 5;

  // ---- 预渲染墨刷变体 ----
  var brushes = [];
  for (var v = 0; v < BRUSH_VARIANTS; v++) {
    var brush = document.createElement("canvas");
    brush.width = BRUSH_SIZE;
    brush.height = BRUSH_SIZE;
    var bctx = brush.getContext("2d");
    if (!bctx) continue;

    var cx = BRUSH_SIZE / 2;
    var cy = BRUSH_SIZE / 2;
    var radius = BRUSH_SIZE / 2;
    var seed = v * 1.37 + 0.3;

    var grad = bctx.createRadialGradient(cx, cy, radius * 0.12, cx, cy, radius);
    grad.addColorStop(0, "rgba(0, 0, 0, 1)");
    grad.addColorStop(0.45, "rgba(0, 0, 0, 0.62)");
    grad.addColorStop(0.85, "rgba(0, 0, 0, 0.14)");
    grad.addColorStop(1, "rgba(0, 0, 0, 0)");

    bctx.fillStyle = grad;
    bctx.beginPath();

    var segs = 96;
    for (var i = 0; i <= segs; i++) {
      var a = (i / segs) * Math.PI * 2;
      var wob =
        0.94 +
        0.04 * Math.sin(a * 3 + seed) +
        0.02 * Math.sin(a * 6 + seed * 1.7);
      var rr = radius * wob;
      var px = cx + Math.cos(a) * rr;
      var py = cy + Math.sin(a) * rr;
      if (i === 0) bctx.moveTo(px, py);
      else bctx.lineTo(px, py);
    }

    bctx.closePath();
    bctx.fill();
    brushes.push(brush);
  }

  if (!brushes.length) return null;

  // ---- 状态 ----
  var w = 0, h = 0;
  var stamps = [];
  var lastX = null, lastY = null;
  var running = false;

  function getMaskColor() {
    return getComputedStyle(document.documentElement)
      .getPropertyValue("--bg-page").trim() || "#f8f9ff";
  }

  function paintMask() {
    ctx.globalCompositeOperation = "source-over";
    ctx.fillStyle = getMaskColor();
    ctx.fillRect(0, 0, w, h);
  }

  function resize() {
    var rect = hero.getBoundingClientRect();
    w = rect.width; h = rect.height;
    canvas.width = Math.round(w * DPR);
    canvas.height = Math.round(h * DPR);
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    paintMask();
  }

  function addStamp(x, y) {
    if (stamps.length >= MAX_STAMPS) stamps.shift();
    stamps.push({
      x: x, y: y,
      born: performance.now(),
      brush: Math.floor(Math.random() * brushes.length),
      rmax: R_END * (1 - R_VARY + Math.random() * R_VARY)
    });
  }

  function stampAlong(x, y) {
    if (lastX === null) {
      addStamp(x, y);
    } else {
      var dx = x - lastX, dy = y - lastY;
      var dist = Math.hypot(dx, dy);
      var steps = Math.max(1, Math.ceil(dist / STAMP_STEP));
      for (var i = 1; i <= steps; i++) {
        addStamp(lastX + (dx * i) / steps, lastY + (dy * i) / steps);
      }
    }
    lastX = x; lastY = y;
  }

  function carveInk(x, y, r, alpha, brushIdx) {
    var d = r * 2;
    ctx.globalAlpha = alpha;
    ctx.drawImage(brushes[brushIdx], x - r, y - r, d, d);
    ctx.globalAlpha = 1;
  }

  function loop() {
    var now = performance.now();
    paintMask();
    ctx.globalCompositeOperation = "destination-out";

    for (var i = stamps.length - 1; i >= 0; i--) {
      var t = (now - stamps[i].born) / LIFETIME;
      if (t >= 1) { stamps.splice(i, 1); continue; }

      var ease = 1 - Math.pow(1 - t, 1.45);
      var r = R_START + (stamps[i].rmax - R_START) * ease;
      var alpha = 1 - Math.pow(t, 1.25);
      carveInk(stamps[i].x, stamps[i].y, r, alpha, stamps[i].brush);
    }

    if (stamps.length) {
      requestAnimationFrame(loop);
    } else {
      running = false;
    }
  }

  function start() {
    if (!running) { running = true; requestAnimationFrame(loop); }
  }

  function pointInCanvas(e) {
    var rect = canvas.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  }

  function onMouseMove(e) {
    var p = pointInCanvas(e);
    if (p.x < 0 || p.y < 0 || p.x > w || p.y > h) {
      lastX = null; lastY = null; return;
    }
    stampAlong(p.x, p.y); start();
  }

  function onMouseOut(e) {
    if (!e.relatedTarget && !e.toElement) { lastX = null; lastY = null; }
  }

  resize();
  window.addEventListener("resize", resize);
  window.addEventListener("mousemove", onMouseMove);
  window.addEventListener("mouseout", onMouseOut);

  // Expose resize handle for external use (e.g., route change layout updates)
  window._wallpaperResize = resize;

  return {
    resize: resize,
    destroy: function () {
      window.removeEventListener("resize", resize);
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseout", onMouseOut);
      if (window._wallpaperResize === resize) window._wallpaperResize = null;
    }
  };
}
