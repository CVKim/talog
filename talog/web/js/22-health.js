/* ── 설비 건강 (talog/health.py · GET /api/health) ─────────────────────────────────────
   '곧 손볼 곳'을 점수 하나와 이유 한 줄로 — 점수 = 가장 낮은 항목(처리 시간 흐름 · 버틸 여유 vs 지난 순간 튐 · 판정 분포 ·
   장비 · 마무리 안 된 사건). 경보 규칙은 바꾸지 않는다. 지난 기록(재현)만 있으면 '지금'이라 쓰지 않는다 — 맨 위 점수는
   '—', 자세히 → 건강 탭에만 '지난 기록 기준'으로 보인다. 남은 시간(언제 한계에 닿나)은 보이지 않는다(CHANGELOG 2.2.0). */
const HST = {ok: ["좋음", "ok", "check-circle"], warn: ["주의", "warn", "alert"], risk: ["점검", "crit", "alert"], none: ["기록 쌓는 중", "", "loader"]};
async function loadHealth(force = false) {
  if (!force && S.healthAt && Date.now() - S.healthAt < 30000) return;
  S.healthAt = Date.now();
  try { S.health = await api("/api/health"); } catch (e) { return; }
  if (S.st) renderHero(S.st);
  renderHealth(); }
const liveHealth = () => S.health && S.health.basis === "live" && S.health.score != null ? S.health : null;
/** 하루 정리(GET /api/daily) — 새 탭에 인쇄용 한 장. 지난 기록뿐이면 그 기록의 날로 */
function dailyLinks(H) { const ymd = d => `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}`;
  const a = (day, rep, label, cls) => `<a class="btn sm${cls}" href="/api/daily?day=${day}&replay=${rep}" target="_blank" rel="noopener">${ic("file-text", "sm")}${esc(label)}</a>`;
  if (H.basis === "replay") { const d = (H.last_hour || "").slice(0, 10).replace(/-/g, "");
    return d ? a(d, 1, `지난 기록 정리 (${d.slice(4, 6)}-${d.slice(6)})`, "") : ""; }
  const t = new Date(), y = new Date(t.getTime() - 864e5);
  return a(ymd(t), 0, "오늘 정리", " primary") + a(ymd(y), 0, "어제 정리", ""); }
/** 맨 위 카드 오른쪽 점수 — 누르면 자세히 → 건강 */
function healthBadge() { const h = liveHealth(), [w, c] = h ? HST[h.state] : HST.none;
  return `<button class="hbadge ${c}" type="button" data-next="health" aria-label="설비 건강 ${h ? `${h.score}점 ${w}` : "기록 쌓는 중"} — 자세히 보기">
    <span class="hb-l">설비 건강</span><b>${h ? h.score : "—"}</b><span class="hb-w">${esc(h ? w : "기록 쌓는 중")}</span></button>`; }
/** 자세히 → 건강 탭 — 급한 항목부터, 좋음·기록 쌓는 중은 접어 둔다 */
function renderHealth() { const H = S.health, el = $("#healthBox"); if (!el) return;
  if (!H) { el.innerHTML = '<span class="sk"></span><span class="sk" style="width:60%"></span>'; return; }
  const basis = H.basis === "live" ? `지금 기준 · ${esc(H.at)} 계산 · 시간별 기록 ${esc((H.last_hour || "").slice(5))}까지`
    : H.basis === "replay" ? `<b>지난 기록(${esc((H.last_hour || "").slice(5, 10))} 재현) 기준</b> — 지금 상태가 아닙니다`
    : "아직 시간별 기록이 없습니다 — 감시가 한 시간 이상 돌면 계산합니다";
  const row = i => { const [w, c, icn] = HST[i.state] || HST.none;
    return `<li class="${c}"><span class="st-chip ${c}">${ic(icn, "sm")}${esc(w)}</span><div><b>${esc(i.title)}</b><span>${esc(i.reason)}</span>${i.todo ? `<span class="todo">${ic("wrench", "sm")}${esc(i.todo)}</span>` : ""}</div>${i.score != null ? `<span class="hs">${i.score}</span>` : ""}</li>`; };
  const items = H.items || [], hot = items.filter(i => i.state === "warn" || i.state === "risk"), rest = items.filter(i => !hot.includes(i));
  const [w, c] = H.score != null ? HST[H.state] : HST.none;
  el.innerHTML = `<div class="hhead"><span class="hbig ${c}"><b>${H.score != null ? H.score : "—"}</b><span>${esc(w)}</span></span>
      <p><b>설비 건강</b>${term("health")} · ${basis}${H.top && H.score != null ? `<br>가장 급한 것: ${esc(H.top.reason)}${H.more ? ` · 같은 흐름 ${H.more}곳 더` : ""}` : ""}</p>
      <div class="hday">${dailyLinks(H)}</div></div>
    ${hot.length ? `<ol class="hl-list">${hot.map(row).join("")}</ol>` : `<p class="sum ok">${ic("check-circle")}<span>주의·점검 항목이 없습니다.</span></p>`}
    ${rest.length ? `<details class="disclosure" data-k="hrest" ${openAttr("hrest", false)}><summary>좋음 ${rest.filter(i => i.state === "ok").length}개 · 기록 쌓는 중 ${rest.filter(i => i.state === "none").length}개</summary><ol class="hl-list">${rest.map(row).join("")}</ol></details>` : ""}
    <p class="howto"><b>읽는 법</b> — 점수는 가장 낮은 항목입니다(80 이상 좋음 · 50~79 주의 · 50 아래 점검). <b>처리 시간 흐름</b>은 그룹별 느린 검사(p99) 처리 시간이 기준선보다 꾸준히 늘고 있는지(누적합), <b>버틸 여유</b>는 지금 여유가 지난 기록의 가장 큰 순간 튐을 버티는지, <b>판정 분포</b>는 NG 비율과 결함 종류 비율이 평소와 다른지 봅니다. 언제 한계에 닿을지는 예측하지 않습니다 — 순간 튐은 예측되지 않기 때문입니다.</p>`;
  hydrateTerms(el); }
