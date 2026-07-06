(function () {
  function initInkReveal(options) {
    const root =
      typeof options.root === "string"
        ? document.querySelector(options.root)
        : options.root;
    if (!root) return null;

    const canvas = root.querySelector(options.maskSelector || ".ink-reveal__mask");
    const bg = root.querySelector(options.bgSelector || ".ink-reveal__bg");
    if (!canvas || !bg) return null;

    const canHover = window.matchMedia("(hover: hover)").matches;
    if (!canHover) return null;

    const ctx = canvas.getContext("2d");
    if (!ctx) return null;

    const maskColor = options.maskColor || "252, 250, 248";
    const rStart = options.rStart ?? 8;
    const rEnd = options.rEnd ?? 128;
    const rVary = options.rVary ?? 0.30;
    const lifetime = options.lifetime ?? 1800;
    const stampStep = options.stampStep ?? 10;
    const maxStamps = options.maxStamps ?? 160;
    const dpr = Math.min(window.devicePixelRatio || 1, options.maxDpr ?? 2);

    const brushSize = options.brushSize ?? 320;
    const brushVariants = options.brushVariants ?? 5;
    const brushes = [];

    for (let v = 0; v < brushVariants; v++) {
      const brush = document.createElement("canvas");
      brush.width = brushSize;
      brush.height = brushSize;
      const bctx = brush.getContext("2d");
      if (!bctx) continue;

      const cx = brushSize / 2;
      const cy = brushSize / 2;
      const radius = brushSize / 2;
      const seed = v * 1.37 + 0.3;

      const grad = bctx.createRadialGradient(cx, cy, radius * 0.12, cx, cy, radius);
      grad.addColorStop(0, "rgba(0, 0, 0, 1)");
      grad.addColorStop(0.45, "rgba(0, 0, 0, 0.62)");
      grad.addColorStop(0.85, "rgba(0, 0, 0, 0.14)");
      grad.addColorStop(1, "rgba(0, 0, 0, 0)");

      bctx.fillStyle = grad;
      bctx.beginPath();

      const segs = 96;
      for (let i = 0; i <= segs; i++) {
        const a = (i / segs) * Math.PI * 2;
        const wob =
          0.94 +
          0.04 * Math.sin(a * 3 + seed) +
          0.02 * Math.sin(a * 6 + seed * 1.7);
        const rr = radius * wob;
        const px = cx + Math.cos(a) * rr;
        const py = cy + Math.sin(a) * rr;
        if (i === 0) bctx.moveTo(px, py);
        else bctx.lineTo(px, py);
      }

      bctx.closePath();
      bctx.fill();
      brushes.push(brush);
    }

    let w = 0;
    let h = 0;
    const stamps = [];
    let lastX = null;
    let lastY = null;
    let running = false;

    function paintMask() {
      ctx.globalCompositeOperation = "source-over";
      ctx.fillStyle = "rgb(" + maskColor + ")";
      ctx.fillRect(0, 0, w, h);
    }

    function resize() {
      const rect = root.getBoundingClientRect();
      w = rect.width;
      h = rect.height;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      canvas.style.width = w + "px";
      canvas.style.height = h + "px";
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      paintMask();
    }

    function addStamp(x, y) {
      if (stamps.length >= maxStamps) stamps.shift();
      stamps.push({
        x: x,
        y: y,
        born: performance.now(),
        brush: Math.floor(Math.random() * brushes.length),
        rmax: rEnd * (1 - rVary + Math.random() * rVary),
      });
    }

    function stampAlong(x, y) {
      if (lastX === null) {
        addStamp(x, y);
      } else {
        const dx = x - lastX;
        const dy = y - lastY;
        const dist = Math.hypot(dx, dy);
        const steps = Math.max(1, Math.ceil(dist / stampStep));
        for (let i = 1; i <= steps; i++) {
          addStamp(lastX + (dx * i) / steps, lastY + (dy * i) / steps);
        }
      }
      lastX = x;
      lastY = y;
    }

    function carveInk(x, y, r, alpha, brushIdx) {
      const d = r * 2;
      ctx.globalAlpha = alpha;
      ctx.drawImage(brushes[brushIdx], x - r, y - r, d, d);
      ctx.globalAlpha = 1;
    }

    function loop() {
      const now = performance.now();
      paintMask();
      ctx.globalCompositeOperation = "destination-out";

      for (let i = stamps.length - 1; i >= 0; i--) {
        const t = (now - stamps[i].born) / lifetime;
        if (t >= 1) {
          stamps.splice(i, 1);
          continue;
        }

        const ease = 1 - Math.pow(1 - t, 1.45);
        const r = rStart + (stamps[i].rmax - rStart) * ease;
        const alpha = 1 - Math.pow(t, 1.25);
        carveInk(stamps[i].x, stamps[i].y, r, alpha, stamps[i].brush);
      }

      if (stamps.length) {
        requestAnimationFrame(loop);
      } else {
        running = false;
      }
    }

    function start() {
      if (!running) {
        running = true;
        requestAnimationFrame(loop);
      }
    }

    function onEnter(e) {
      const rect = root.getBoundingClientRect();
      lastX = e.clientX - rect.left;
      lastY = e.clientY - rect.top;
      stampAlong(lastX, lastY);
      start();
    }

    function onMove(e) {
      const rect = root.getBoundingClientRect();
      stampAlong(e.clientX - rect.left, e.clientY - rect.top);
      start();
    }

    function onLeave() {
      lastX = null;
      lastY = null;
    }

    resize();
    window.addEventListener("resize", resize);
    root.addEventListener("mouseenter", onEnter);
    root.addEventListener("mousemove", onMove);
    root.addEventListener("mouseleave", onLeave);

    return {
      resize: resize,
      destroy: function () {
        window.removeEventListener("resize", resize);
        root.removeEventListener("mouseenter", onEnter);
        root.removeEventListener("mousemove", onMove);
        root.removeEventListener("mouseleave", onLeave);
      },
    };
  }

  window.initInkReveal = initInkReveal;
})();
