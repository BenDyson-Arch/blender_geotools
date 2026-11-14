// Draw faint topographic contours of an invented terrain into <svg class="contours">.
// Deterministic (no randomness), so every render of a template is the same.
(function () {
  function field(x, y) {
    // A few smooth hills and a valley, in a 0..1 square; heights are arbitrary.
    const bumps = [
      [0.78, 0.38, 0.16, 1.0],
      [0.62, 0.86, 0.12, 0.55],
      [0.94, 0.9, 0.2, 0.7],
      [0.2, 0.7, 0.22, -0.45],
      [0.45, 0.15, 0.1, 0.35],
      [0.1, 0.18, 0.14, 0.4],
    ];
    let h = 0.25 * x + 0.1 * y;
    for (const [cx, cy, r, a] of bumps) {
      const dx = (x - cx) / r;
      const dy = (y - cy) / (r * 0.8);
      h += a * Math.exp(-(dx * dx + dy * dy));
    }
    return h + 0.03 * Math.sin(x * 23.0) * Math.cos(y * 17.0);
  }

  function draw(svg) {
    const w = svg.clientWidth;
    const h = svg.clientHeight;
    const step = Number(svg.dataset.step || 0.06);
    const majorEvery = Number(svg.dataset.major || 5);
    const nx = Math.ceil(w / 6);
    const ny = Math.ceil(h / 6);
    const aspect = w / h;
    const v = [];
    for (let j = 0; j <= ny; j++) {
      const row = [];
      for (let i = 0; i <= nx; i++) row.push(field((i / nx) * aspect * 0.5 + 0.1, j / ny));
      v.push(row);
    }
    let lo = Infinity;
    let hi = -Infinity;
    for (const row of v) for (const z of row) (lo = Math.min(lo, z)), (hi = Math.max(hi, z));
    const paths = { minor: [], major: [] };
    const sx = w / nx;
    const sy = h / ny;
    const first = Math.ceil(lo / step);
    for (let k = first; k * step < hi; k++) {
      const level = k * step;
      const out = k % majorEvery === 0 ? paths.major : paths.minor;
      for (let j = 0; j < ny; j++) {
        for (let i = 0; i < nx; i++) {
          const a = v[j][i], b = v[j][i + 1], c = v[j + 1][i + 1], d = v[j + 1][i];
          const pts = [];
          const edge = (z0, z1, x0, y0, x1, y1) => {
            if ((z0 < level) !== (z1 < level)) {
              const t = (level - z0) / (z1 - z0);
              pts.push([(x0 + t * (x1 - x0)) * sx, (y0 + t * (y1 - y0)) * sy]);
            }
          };
          edge(a, b, i, j, i + 1, j);
          edge(b, c, i + 1, j, i + 1, j + 1);
          edge(c, d, i + 1, j + 1, i, j + 1);
          edge(d, a, i, j + 1, i, j);
          for (let p = 0; p + 1 < pts.length; p += 2) {
            out.push(`M${pts[p][0].toFixed(1)} ${pts[p][1].toFixed(1)}L${pts[p + 1][0].toFixed(1)} ${pts[p + 1][1].toFixed(1)}`);
          }
        }
      }
    }
    svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    svg.innerHTML =
      `<path d="${paths.minor.join("")}" fill="none" stroke="var(--line)" stroke-width="1"/>` +
      `<path d="${paths.major.join("")}" fill="none" stroke="var(--line)" stroke-width="2"/>`;
  }

  document.querySelectorAll("svg.contours").forEach(draw);
  document.documentElement.dataset.contours = "done";
})();
