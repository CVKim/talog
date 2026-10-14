/* ── 작은 차트 (SVG) ────────────────────────────────────────── */
function spark(vals, {warnBelow = null} = {}) {
  const pts = vals.map((v, i) => [i, v]).filter(p => p[1] != null); if (pts.length < 2) return "";
  const ys = pts.map(p => p[1]); const mn = Math.min(...ys), mx = Math.max(...ys); const W = 200, H = 30, n = vals.length - 1 || 1;
  const X = i => (i / n) * W, Y = v => H - 3 - ((v - mn) / ((mx - mn) || 1)) * (H - 6);
  const d = pts.map((p, k) => `${k ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(" "); const last = pts[pts.length - 1];
  const bad = warnBelow != null && last[1] < warnBelow;
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true"><path d="${d}" fill="none" stroke="${bad ? "var(--crit)" : "var(--ink-2)"}" stroke-width="1.6" vector-effect="non-scaling-stroke"/><circle cx="${X(last[0])}" cy="${Y(last[1])}" r="2.6" fill="${bad ? "var(--crit)" : "var(--brand)"}"/></svg>`; }
function tolChart(vals, label, W = 640) {
  const H = 220, L = 40, R = 12, T = 12, B = 26; const lo = -20, hi = 100;
  const X = h => L + (h / 23) * (W - L - R), Y = v => T + (1 - (Math.max(lo, Math.min(hi, v)) - lo) / (hi - lo)) * (H - T - B);
  const pts = vals.map((v, h) => [h, v]).filter(p => p[1] != null);
  let s = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}">`;
  s += `<rect x="${L}" y="${Y(20)}" width="${W - L - R}" height="${Y(lo) - Y(20)}" fill="var(--crit-soft)"/><rect x="${L}" y="${Y(40)}" width="${W - L - R}" height="${Y(20) - Y(40)}" fill="var(--warn-soft)"/>`;
  [0, 20, 40, 60, 80, 100].forEach(v => { s += `<line class="grid-l" x1="${L}" x2="${W - R}" y1="${Y(v)}" y2="${Y(v)}"/><text x="${L - 6}" y="${Y(v) + 4}" text-anchor="end">${v}%</text>`; });
  [0, 3, 6, 9, 12, 15, 18, 21, 23].forEach(h => { s += `<text x="${X(h)}" y="${H - 8}" text-anchor="middle">${h}시</text>`; });
  if (pts.length) {
    s += `<path d="${pts.map((p, k) => `${k ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(" ")}" fill="none" stroke="var(--ink-2)" stroke-width="2" stroke-linejoin="round"/>`;
    pts.forEach(([h, v]) => { const bad = v < 20; s += `<circle cx="${X(h)}" cy="${Y(v)}" r="${bad ? 4.5 : 3}" fill="${bad ? "var(--crit)" : "var(--surface)"}" stroke="${bad ? "var(--crit)" : "var(--ink-2)"}" stroke-width="1.6"><title>${h}시 — 최저 내성 ${v}%</title></circle>`; });
    const mn = pts.reduce((a, p) => p[1] < a[1] ? p : a), right = X(mn[0]) < W - R - 90;     // 최저점 이름표는 점 옆에 (선과 겹치지 않게)
    s += `<text x="${X(mn[0]) + (right ? 9 : -9)}" y="${Y(mn[1]) + 4}" text-anchor="${right ? "start" : "end"}" style="fill:${mn[1] < 20 ? "var(--crit-ink)" : "var(--sub)"};font-weight:700">최저 ${mn[0]}시 ${mn[1]}%</text>`; }
  return s + "</svg>"; }

