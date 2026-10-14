/* ── 운영 현황 ─────────────────────────────────────────── */
async function loadStatus() {
  let st; try { st = await api("/api/status"); } catch (e) { $("#runText").textContent = "연결 끊김"; $("#runPill").className = "live off"; return; }
  S.st = st;
  $("#ver").textContent = "v" + st.version; $("#siteName").textContent = st.site || "설비 이름 미지정"; $("#siteData").textContent = st.data_dir || "";
  $("#runText").textContent = st.running ? "감시 중" : "정지"; $("#runPill").className = "live " + (st.running ? "on" : "");
  const rb = $("#btnRun"); if (rb.getAttribute("aria-busy") !== "true") { rb.className = "btn " + (st.running ? "" : "primary");
    rb.innerHTML = `${ic(st.running ? "square" : "play")}<span class="lbl">${st.running ? "감시 정지" : "감시 시작"}</span>`; rb.setAttribute("aria-label", st.running ? "감시 정지" : "감시 시작"); }
  renderHero(st); renderKpis(st); renderFlow(st); renderOpsLists(st); renderCapacity(); renderGpu(st);
  const n = st.incidents.filter(r => r.severity === "crit").length; $("#incCnt").textContent = n; $("#incCnt").classList.toggle("hidden", !n); $("#incCnt").setAttribute("aria-label", `오늘 심각 사건 ${n}건`);
  if (st.detect.pattern_errors && st.detect.pattern_errors.length && !S.patWarned) { S.patWarned = true; toast("규칙 설정 오류: " + st.detect.pattern_errors.join(" / "), "err"); }
  if (S.view === "set") renderGpuFree();
  if (st.job && st.job.state === "running" && S.view === "ana") pollJob();
}
function renderHero(st) {
  const D = st.detect, crit = st.incidents.filter(r => r.severity === "crit"), warnN = D.warn;
  const up = st.uptime_s ? (st.uptime_s >= 3600 ? (st.uptime_s / 3600).toFixed(1) + "시간" : Math.round(st.uptime_s / 60) + "분") : "-";
  let cls, icon, title, sub, act = "";
  if (!st.running) { cls = "idle"; icon = "square"; title = "감시가 멈춰 있습니다"; sub = "멈춘 동안에는 새 경보·사건·메일이 나오지 않습니다."; act = `<button class="btn primary" type="button" data-act="start">${ic("play")}감시 시작</button>`; }
  else if (crit.length || D.crit) { cls = "crit"; icon = "alert"; title = `심각 ${crit.length || D.crit}건 — 확인이 필요합니다`; sub = crit[0] ? `${crit[0].time} ${crit[0].title} · ${crit[0].cause || ""}` : (D.last ? D.last.title : ""); act = `<a class="btn primary" href="#inc">${ic("siren")}사건 보기</a>`; }
  else if (!st.collect.exists) { cls = "warn"; icon = "folder-open"; title = "오늘 로그 폴더가 아직 없습니다"; sub = "로그 루트 아래에 오늘 날짜 폴더가 생기면 자동으로 읽기 시작합니다."; act = `<button class="btn" type="button" data-act="collect">${ic("sliders")}수집 설정</button>`; }
  else if (warnN) { cls = "warn"; icon = "alert"; title = `주의 ${warnN}건`; sub = D.last ? `마지막: ${D.last.title}` : ""; act = `<a class="btn" href="#inc">${ic("siren")}사건 보기</a>`; }
  else { cls = "ok"; icon = "check-circle"; title = "정상 감시 중"; sub = "오늘 경보·사건이 없습니다."; }
  $("#opsHero").className = "hero " + cls;
  $("#opsHero").innerHTML = `<div class="hi">${ic(icon)}</div><div class="ht"><h2>${esc(title)}</h2><p>${esc(sub)}</p><div class="hmeta"><span>가동 ${esc(up)}</span><span>마지막 읽음 ${esc(st.collect.last_read || "-")}</span><span>규칙 ${st.detect.rules}개</span><span>${hhmmss()} 기준</span></div></div>${act ? `<div>${act}</div>` : ""}`;
  const sb = $("#opsHero [data-act=start]"); if (sb) sb.onclick = () => $("#btnRun").click();
  const cb = $("#opsHero [data-act=collect]"); if (cb) cb.onclick = () => goSec("s-collect"); }
const rowLab = r => `${mmdd(r.day)}${r.replay ? " 재현" : S.tl && r.day === S.tl.today ? " 오늘" : ""}`;
const lastRowWith = key => ((S.tl && S.tl.days) || []).find(r => r[key].some(x => x != null));
const lastIdx = arr => { for (let h = arr.length - 1; h >= 0; h--) if (arr[h] != null) return h; return -1; };
function renderKpis(st) {
  const inc = st.incidents, crit = inc.filter(r => r.severity === "crit").length, warn = inc.filter(r => r.severity === "warn").length;
  const RN = lastRowWith("n"), hN = RN ? lastIdx(RN.n) : -1;
  const cap = (st.capacity || []).filter(r => r.limit_s && r.tolerance_s != null).map(r => [r.group, 100 * r.tolerance_s / r.limit_s]);
  const minCap = cap.length ? cap.reduce((a, b) => b[1] < a[1] ? b : a) : null;
  let tolV = null, tolSub = "그룹별 기준선을 잡는 중", tolSp = "";
  if (minCap) { tolV = minCap[1]; tolSub = `지금 · 그룹 ${esc(String(minCap[0]))}`; }
  else { const RT = lastRowWith("tol_pct"); if (RT) { const h = lastIdx(RT.tol_pct); tolV = RT.tol_pct[h]; tolSub = `${rowLab(RT)} ${h}시 · 모든 그룹 중 최저`; tolSp = spark(RT.tol_pct, {warnBelow: 20}); } }
  const tolState = tolV == null ? "" : tolV < 20 ? ' · <b class="crit">거부 위험</b>' : tolV < 40 ? ' · <b class="warn">여유 감소</b>' : " · 여유 있음";
  const J = st.judge;
  const tiles = [
    ["siren", "오늘 사건", `${inc.length}<small>건</small>`, `<span class="sev crit">심각 ${crit}</span><span class="sev warn">주의 ${warn}</span>`, ""],
    ["activity", "시간당 검사", hN >= 0 ? `${RN.n[hN].toLocaleString()}<small>건</small>` : "—", hN >= 0 ? `${rowLab(RN)} ${hN}시${hN > 0 && RN.n[hN - 1] != null ? ` · 앞 시간 ${RN.n[hN - 1].toLocaleString()}건` : ""}` : "검사 기록 없음", RN ? spark(RN.n) : ""],
    ["gauge", "최저 일시 지연 내성", tolV != null ? `${tolV.toFixed(1).replace(/\.0$/, "")}<small>%</small>` : "—", tolSub + tolState, tolSp],
    ["check-circle", "판단 일치", J.llm ? `${J.agree}<small>/ ${J.incidents}</small>` : "룰", J.llm ? `룰 + ${esc(J.model)} 교차 확인 · 오늘` : "룰 진단만 쓰는 중", ""]];
  const tips = ["오늘 실시간 감시가 연 사건", "한 검사가 모든 Seq 그룹을 지나므로 그룹 중 가장 큰 값", "한계(스레드 수 × 투입 간격) − 점유 p99 를 한계 대비 %로 — 20% 아래면 잠깐의 지연으로도 거부될 수 있음", "룰 판단과 LLM 판단이 같은 원인을 짚은 사건 수"];
  $("#opsKpis").innerHTML = tiles.map(([i, l, v, s, sp], k) => `<div class="kpi" title="${tips[k]}"><span class="kl">${ic(i, "sm")}${l}</span><span class="kv">${v}</span><span class="ks">${s}</span>${sp}</div>`).join(""); }
function renderFlow(st) {
  const C = st.collect, D = st.detect, J = st.judge, N = st.notify;
  const stages = [
    ["s-collect", 1, "수집", !st.running ? "" : C.exists ? "ok" : "warn", C.exists ? `${C.files}개 파일` : "오늘 폴더 없음", `${esc(C.tracking)} · 마지막 ${esc(C.last_read || "-")}`],
    ["s-detect", 2, "탐지", D.crit ? "crit" : D.warn ? "warn" : st.running ? "ok" : "", `심각 ${D.crit} · 주의 ${D.warn}`, `규칙 ${D.rules}개`],
    ["s-judge", 3, "판단", J.llm && J.alive === false ? "warn" : st.running ? "ok" : "", `사건 ${J.incidents}`, J.llm ? `룰 + ${esc(J.model)}${J.alive === false ? " · 서버 없음" : ""}` : "룰 진단만"],
    ["s-notify", 4, "알림", N.provider === "off" ? "warn" : N.failed ? "crit" : "ok", N.provider === "off" ? "메일 꺼짐" : `발송 ${N.sent}`, N.provider === "off" ? (N.toast ? "화면 토스트만" : "알림 없음") : `${esc(N.label)} · ${N.to}명`]];
  $("#flow").innerHTML = stages.map(([sec, n, name, s2, big, l]) => `<button type="button" class="stage" data-sec="${sec}" title="설정 → ${name}"><span class="st-h"><span class="st-n">${n}</span><span class="st-t">${name}</span><span class="badge ${s2}">${STK[s2]}</span></span><span class="st-big">${big}</span><span class="st-l">${l}</span></button>`).join("");
  $$("#flow .stage").forEach(s => s.onclick = () => goSec(s.dataset.sec)); }
function renderOpsLists(st) {
  $("#opsIncMeta").textContent = st.incidents.length ? `${st.incidents.length}건` : "";
  $("#opsInc").innerHTML = st.incidents.length ? st.incidents.map(r => `<tr class="click" tabindex="0" data-id="${esc(r.id)}"><td><div class="li">${sevTag(r.severity)}<b>${esc(r.title)}</b><span class="when">${esc(r.time)}</span><div class="li-sub">${esc(r.cause || "")}</div><div class="li-meta">${gateTag(r.gate, r.mode)}<span>메일</span>${mailTag(r.mail)}</div></div></td></tr>`).join("")
    : emptyRow(1, "오늘 사건 없음", st.running ? "감시 중입니다 — 경보가 사건으로 묶이면 여기에 나옵니다. 지난 기록은 '사건'에서 봅니다." : "감시가 정지되어 있습니다.", "check-circle");
  $$("#opsInc tr.click").forEach(tr => tr.onclick = () => { go("inc", {focus: false}); openInc(tr.dataset.id, false); });
  $("#opsAlertMeta").textContent = st.alerts.length ? `최근 ${st.alerts.length}건` : "";
  $("#opsAlerts").innerHTML = st.alerts.length ? st.alerts.slice(0, 14).map(a => `<li class="${esc(a.severity)}"><span class="tt">${esc((a.time || "").slice(6) || a.time)}</span><span class="dot"></span><div><div class="row" style="gap:6px">${sevTag(a.severity)}<b>${esc(a.title)}</b></div><div class="sub">${esc(a.evidence)}</div></div></li>`).join("")
    : `<li style="display:block">${emptyBox(st.running ? "경보 없음" : "감시 정지", st.running ? "정상 감시 중입니다." : "위 막대의 '감시 시작'으로 다시 켭니다.", st.running ? "check-circle" : "square")}</li>`; }
function renderGpu(st) { const gf = st.gpu_free || [];
  $("#gpuBox").innerHTML = gf.length ? gf.map(g => { const used = g.total - g.free, pct = 100 * used / g.total; const t = (st.gpu.find(x => x.gpu === g.gpu) || {}).temp;
    return `<div class="gpu-row"><b>GPU${g.gpu}</b><div class="bar" role="img" aria-label="VRAM ${pct.toFixed(0)}% 사용"><i class="${pct > 85 ? "hi" : ""}" style="width:${pct.toFixed(0)}%"></i></div><span class="mono">${(used / 1024).toFixed(1)}/${(g.total / 1024).toFixed(0)} GB · ${g.util}%${t != null ? " · " + t + "°C" : ""}</span></div>`; }).join("")
    : emptyBox("GPU 정보 없음", "nvidia-smi 를 찾지 못했습니다.", "cpu"); }
async function loadTimeline() { try { S.tl = await api("/api/timeline?days=7"); } catch (e) { return; } renderStrip(S.tl); if (S.st) renderKpis(S.st); }
function renderStrip(T) {
  const rows = T.days || [];
  $("#opsStrip").innerHTML = rows.map(r => {
    const isToday = r.day === T.today && !r.replay; let hc = 0, wc = 0, nd = 0;
    const cells = r.alerts.map((a, h) => { const has = r.n[h] != null; const fut = isToday && h > T.hour;
      const cls = a[0] ? "crit" : a[1] ? "warn" : a[2] ? "info" : fut ? "fut" : has ? "ok" : "";
      if (a[0]) hc++; if (a[1]) wc++; if (has) nd++;
      const tip = `${mmdd(r.day)} ${h}시 — 심각 ${a[0]} · 주의 ${a[1]} · 정보 ${a[2]}${has ? ` · 검사 ${r.n[h].toLocaleString()}` : ""}${r.rejects[h] ? ` · 거부 ${r.rejects[h]}` : ""}${r.tol_pct[h] != null ? ` · 최저 내성 ${r.tol_pct[h]}%` : ""}`;
      return `<div class="cell ${cls}${isToday && h === T.hour ? " now" : ""}" title="${tip}"></div>`; }).join("");
    const lab = `${mmdd(r.day)} ${isToday ? '<span class="tag b">오늘</span>' : r.replay ? '<span class="tag">재현</span>' : ""}`;
    return `<div class="strip-row"><div class="strip-lab">${lab}</div><div class="cells" role="img" aria-label="${mmdd(r.day)}${r.replay ? " 재현" : ""}: 심각 ${hc}시간, 주의 ${wc}시간, 검사 기록 ${nd}시간">${cells}</div></div>`; }).join("")
    + `<div class="axis" aria-hidden="true"><span></span><div class="ticks"><span>0시</span><span>6시</span><span>12시</span><span>18시</span><span>23시</span></div></div>`;
  $("#opsStripTbl").innerHTML = rows.map(r => { const c = r.alerts.reduce((a, x) => [a[0] + x[0], a[1] + x[1]], [0, 0]); const n = r.n.reduce((a, x) => a + (x || 0), 0); const rj = r.rejects.reduce((a, x) => a + (x || 0), 0); const tol = r.tol_pct.filter(x => x != null);
    return `<tr><td>${mmdd(r.day)}${r.replay ? " (재현)" : ""}</td><td class="num">${c[0]}</td><td class="num">${c[1]}</td><td class="num">${n.toLocaleString()}</td><td class="num">${rj}</td><td class="num">${tol.length ? Math.min(...tol) + "%" : "-"}</td></tr>`; }).join("");
  const pick = lastRowWith("tol_pct");
  $("#tolNote").textContent = pick ? rowLab(pick) : "";
  const w = Math.round($("#opsTol").clientWidth) || 640;
  $("#opsTol").innerHTML = pick ? tolChart(pick.tol_pct, `${rowLab(pick)} 시간별 최저 일시 지연 내성`, Math.max(300, w)) : emptyBox("아직 추세 없음", "검사가 들어오면 시간마다 그룹별 처리 여유를 기록합니다.", "activity");
  renderCapacity(); }
let rsT = null; window.addEventListener("resize", () => { clearTimeout(rsT); rsT = setTimeout(() => { if (S.view === "ops" && S.tl) renderStrip(S.tl); }, 150); });
function renderCapacity() {
  const lab = {ok: '<span class="badge ok">정상</span>', warn: '<span class="badge warn">주의</span>', crit: '<span class="badge crit">심각</span>', learning: '<span class="badge info">기준 수집 중</span>', idle: '<span class="badge">투입 없음</span>'};
  const f = (v, d = 2) => v == null ? "-" : (+v).toFixed(d).replace(/\.0+$/, "");
  let rows = (S.st && S.st.capacity) || [], src = "지금 · 점유 p99 / 한계";
  if (!rows.some(r => r.limit_s && r.hold_p99 != null)) { const R = ((S.tl && S.tl.days) || []).find(r => r.last && r.last.groups.length);
    if (R) { src = `${rowLab(R)} ${R.last.hour}시 기록 · 점유 p99 / 한계`;
      rows = R.last.groups.map(g => { const lim = (g.pool || 0) * (g.interval_s || 0), tol = lim && g.hold_p99 != null ? lim - g.hold_p99 : null;
        return {group: g.group, status: tol == null ? "" : tol / lim < .2 ? "crit" : tol / lim < .4 ? "warn" : "ok", idle_median: g.idle_median, hold_p50: g.hold_p50, hold_p99: g.hold_p99, limit_s: lim || null, tolerance_s: tol, interval_s: g.interval_s}; }); } }
  $("#capSrc").textContent = src;
  const bul = rows.filter(r => r.limit_s && r.hold_p99 != null).sort((a, b) => b.hold_p99 / b.limit_s - a.hold_p99 / a.limit_s).slice(0, 6);
  $("#capBullets").innerHTML = bul.length ? bul.map(r => { const u = Math.min(100, 100 * r.hold_p99 / r.limit_s), tol = r.tolerance_s != null ? 100 * r.tolerance_s / r.limit_s : null;
    return `<div class="bul"><span class="g">그룹 ${esc(String(r.group))}</span><div class="bul-bar" role="img" aria-label="그룹 ${esc(String(r.group))} 점유 p99 ${f(r.hold_p99)}초, 한계 ${f(r.limit_s)}초"><i style="width:${u.toFixed(1)}%;background:${u >= 80 ? "var(--crit)" : u >= 60 ? "var(--warn)" : "var(--ink-2)"}"></i></div><span class="v">${f(r.hold_p99)}초 / ${f(r.limit_s)}초 · 내성 ${tol != null ? tol.toFixed(1).replace(/\.0$/, "") + "%" : "-"}</span></div>`; }).join("")
    : emptyBox("아직 데이터 없음", "감시가 검사를 읽기 시작하면 그룹마다 첫 1시간으로 기준선을 잡습니다. 지난 날은 '분석 → 경보 재현'으로 기록을 만듭니다.", "gauge");
  $("#capRows").innerHTML = rows.length ? rows.map(r => `<tr><td class="mono">${esc(String(r.group || "단일"))}</td><td>${lab[r.status] || esc(r.status || "")}</td><td class="mono">${f(r.idle_median, 1)} / ${f(r.idle_baseline, 1)}</td><td class="mono">${f(r.hold_p50)}초 ${r.hold_ratio != null ? `(×${f(r.hold_ratio)})` : ""}</td><td class="mono">${f(r.hold_p99)}초 / ${r.limit_s != null ? f(r.limit_s) + "초" : "-"}</td><td class="mono">${r.tolerance_s != null ? f(r.tolerance_s) + "초" : "-"}</td><td class="mono">${f(r.interval_s, 3)}초</td></tr>`).join("")
    : emptyRow(7, "아직 데이터 없음", "검사가 들어오면 그룹별 기준선을 잡습니다.", "gauge");
  const b = rows.find(r => r.baseline_from); $("#capNote").textContent = b ? `기준선 ${b.baseline_from}부터` : bul.length && rows.length > bul.length ? `가장 빠듯한 ${bul.length}개 / ${rows.length}개 그룹` : ""; }
$("#btnRun").onclick = async () => { const on = S.st && S.st.running;
  if (on && !(await confirmBox({title: "감시를 멈출까요?", body: "멈춘 동안에는 새 경보·사건·메일이 나오지 않습니다.", ok: "감시 정지", danger: true}))) return;
  await busy($("#btnRun"), async () => { const r = await api("/api/control", {action: on ? "stop" : "start"}); toast(r.running ? "감시를 시작했습니다" : "감시를 멈췄습니다", "ok"); });
  loadStatus(); };
$("#btnInject").onclick = async () => {
  const mailOn = S.st && S.st.notify && S.st.notify.provider !== "off";
  if (mailOn && !(await confirmBox({title: "시험 경보를 넣을까요?", body: `메일이 켜져 있어 받는 사람 ${S.st.notify.to}명에게 시험 메일이 실제로 발송됩니다.`, ok: "넣고 발송"}))) return;
  await busy($("#btnInject"), async () => { const r = await api("/api/test/alert", {kind: $("#injKind").value}); toast(r.ok ? r.message : r.error, r.ok ? "ok" : "err"); });
  setTimeout(loadStatus, 1500); };
async function loadLog() { const r = await api("/api/log"); const b = $("#logBox"); const atEnd = b.scrollTop + b.clientHeight >= b.scrollHeight - 20; b.textContent = r.lines.join("\n") || "(아직 로그 없음)"; if (atEnd) b.scrollTop = b.scrollHeight; }
$("#logDetails").addEventListener("toggle", () => { if ($("#logDetails").open) loadLog(); });

