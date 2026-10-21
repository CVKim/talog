/* ── 운영 현황 ─────────────────────────────────────────────────────────────
   위에서 아래로 세 덩어리 — 지금(상태 한 줄 · 할 일 · 에이전트 브리핑) → 오늘(사건 · 경보) → 자세히(탭: 처리 여유 ·
   상태 기록 · 장비 · 시험). 같은 사실을 여러 곳에 되풀이하지 않는다: 상태와 할 일은 맨 위 카드 한 곳에서만 말한다. */
async function loadStatus() {
  let st; try { st = await api("/api/status"); } catch (e) { $("#runText").textContent = "연결 끊김"; $("#runPill").className = "live off"; return; }
  S.st = st; applyAccess(st.access);
  $("#ver").textContent = "v" + st.version; $("#siteName").textContent = st.site || "설비 이름 미지정"; $("#siteData").textContent = st.data_dir || "";
  $("#runText").textContent = st.running ? "감시 중" : "정지"; $("#runPill").className = "live " + (st.running ? "on" : "");
  syncRunBtn();
  renderHero(st); renderBrief(st); renderOpsLists(st); renderCapacity(); renderGpu(st); setTitle();
  const n = st.incidents.filter(r => r.severity === "crit").length; $("#incCnt").textContent = n; $("#incCnt").classList.toggle("hidden", !n); $("#incCnt").setAttribute("aria-label", `오늘 심각 사건 ${n}건`);
  if (st.detect.pattern_errors && st.detect.pattern_errors.length && !S.patWarned) { S.patWarned = true; toast("규칙 설정 오류: " + st.detect.pattern_errors.join(" / "), "err"); }
  if (S.view === "set") renderGpuFree();
  if (st.job && st.job.state === "running" && S.view === "ana") pollJob();
}
/* ── 처리 여유 공용 — 실시간 기록만 쓴다(재현 기록으로 '할 일'을 만들지 않는다) ── */
const pctNum = v => (+v).toFixed(1).replace(/\.0$/, "");
const fmtPct = v => `${pctNum(v)}%`;
/** 버틸 여유 % → [상태 클래스, 낱말] (색만으로 알리지 않는다 — ux #37) */
const tolWord = v => v == null ? ["", "기록 없음"] : v < 20 ? ["crit", "위험"] : v < 40 ? ["warn", "빠듯"] : ["ok", "여유"];
/** 실시간 처리 여유 행 + 버틸 여유 % (pct) */
function liveCapacity(st) {
  return ((st && st.capacity) || []).filter(r => r.limit_s && r.tolerance_s != null)
    .map(r => ({...r, pct: 100 * r.tolerance_s / r.limit_s}));
}

/** 감시 정지는 운영자 모드 일이라 현장 모드에서는 버튼을 숨긴다(멈춰 있을 때의 '감시 시작'은 누구나) */
function syncRunBtn() { const st = S.st, rb = $("#btnRun"); if (!st || rb.getAttribute("aria-busy") === "true") return;
  rb.hidden = st.running && !atLeast("operator"); rb.className = "btn " + (st.running ? "" : "primary");
  rb.innerHTML = `${ic(st.running ? "square" : "play")}<span class="lbl">${st.running ? "감시 정지" : "감시 시작"}</span>`; rb.setAttribute("aria-label", st.running ? "감시 정지" : "감시 시작"); }

/** '할 일' — 맨 위 제목이 말하지 않은 것만, 급한 순서로 최대 3개 (ux #80: 오류·상태에는 다음 단계를).
    심각 사건·경보·감시 정지는 제목과 그 옆 버튼이 말한다. 현장 모드는 고칠 수 없는 안내(메일 꺼짐)를 빼고,
    설정으로 가는 버튼은 '설정 보기'로 말한다. */
function nextActions(st) {
  const out = [], oper = atLeast("operator"), setBtn = (lab, sec) => [oper ? lab : "설정 보기", "sec:" + sec];
  const low = liveCapacity(st).filter(r => r.pct < 20).sort((a, b) => a.pct - b.pct);
  if (low.length) out.push({cls: low[0].pct < 0 ? "crit" : "warn", icon: "gauge",
    text: `<b>그룹 ${esc(String(low[0].group))}${low.length > 1 ? ` 외 ${low.length - 1}개` : ""}의 버틸 여유 ${fmtPct(low[0].pct)}</b> — 잠깐의 지연으로도 검사 시작 거부가 날 수 있습니다. 다음 정지 때 재시작을 계획하십시오.`,
    btn: ["처리 여유 보기", "cap"]});
  if (st.notify.failed) out.push({cls: "crit", icon: "mail", text: `<b>메일 발송 실패 ${st.notify.failed}건</b> — 알림 설정의 '연결 확인'으로 계정을 점검하십시오.`, btn: setBtn("알림 설정", "s-notify")});
  if (st.running && !st.collect.exists) out.push({cls: "warn", icon: "folder-open", text: "<b>오늘 로그 폴더가 없습니다.</b> 설비가 쉬는 날이 아니라면 로그 폴더 설정을 확인하십시오.", btn: setBtn("수집 설정", "s-collect")});
  if (st.judge.llm && st.judge.alive === false) out.push({cls: "warn", icon: "cpu", text: "<b>로컬 AI(LLM)가 응답하지 않습니다.</b> 판단은 규칙 진단만으로 합니다.", btn: setBtn("판단 설정", "s-judge")});
  if (oper && st.notify.provider === "off") out.push({cls: "info", icon: "mail", text: "<b>알림 메일이 꺼져 있습니다.</b> 심각 사건이 나도 담당자에게 메일이 가지 않습니다.", btn: setBtn("알림 설정", "s-notify")});
  return out.slice(0, 3);
}
/** 사건으로 묶이지 않은 심각 경보만 있을 때(예: GPU 과열) — 사건 수 0 과 '심각' 이 엇갈려 보이지 않게 따로 말한다 */
const critAlertsOnly = st => st.detect.crit > 0 && !st.incidents.some(r => r.severity === "crit");
function runNext(act) {
  if (act === "start") $("#btnRun").click();
  else if (act === "cap") { moreTab("cap"); $("#opsMore").scrollIntoView({behavior: reduceMotion() ? "auto" : "smooth", block: "start"}); }
  else if (act === "alerts") { todayTab("al"); $("#todayAl").closest(".card").scrollIntoView({behavior: reduceMotion() ? "auto" : "smooth", block: "start"}); }
  else if (act === "open") { go("inc", {focus: false}); if (!$("#incOpen").checked) { $("#incOpen").checked = true; S.incOpen = true; if (S.incRows) renderIncList(); } }
  else if (act.startsWith("sec:")) goSec(act.slice(4));
  else if (act.startsWith("inc:")) { go("inc", {focus: false}); openInc(act.slice(4), false); }
}
/** 맨 위 상태 — 한 줄 제목 + 한 줄 설명 + (있으면) 할 일. 상태는 여기서만 말한다 */
function renderHero(st) {
  const D = st.detect, crit = st.incidents.filter(r => r.severity === "crit" && OPEN(r));
  const critDone = st.incidents.filter(r => r.severity === "crit").length - crit.length;   // 해결·오경보로 마무리한 심각 사건
  const up = st.uptime_s ? (st.uptime_s >= 3600 ? (st.uptime_s / 3600).toFixed(1) + "시간" : Math.round(st.uptime_s / 60) + "분") : "-";
  let cls, icon, title, sub, btn = null;
  if (!st.running) { cls = "idle"; icon = "square"; title = "감시가 멈춰 있습니다"; sub = "멈춘 동안에는 새 경보·사건·메일이 나오지 않습니다. 오른쪽 위 '감시 시작'으로 다시 켭니다."; }
  else if (crit.length) { cls = "crit"; icon = "alert"; title = `확인할 심각 사건 ${crit.length}건`; sub = `${crit[0].time} ${crit[0].title}${crit[0].cause ? " — " + crit[0].cause : ""}`; btn = [crit.length > 1 ? "가장 최근 사건 열기" : "사건 열기", "inc:" + crit[0].id, "siren"]; }
  else if (critAlertsOnly(st)) { cls = "crit"; icon = "alert"; title = `심각 경보 ${D.crit}건`; sub = D.last ? D.last.title : ""; btn = ["경보 보기", "alerts", "alert"]; }
  else if (!st.collect.exists) { cls = "warn"; icon = "folder-open"; title = "오늘 로그를 기다리는 중입니다"; sub = "로그 폴더에 오늘 날짜 폴더가 생기면 자동으로 읽기 시작합니다."; }
  else if (D.warn) { cls = "warn"; icon = "alert"; title = `주의 경보 ${D.warn}건`; sub = D.last ? `마지막: ${D.last.title}` : ""; btn = ["경보 보기", "alerts", "alert"]; }
  else { cls = "ok"; icon = "check-circle"; title = "정상 — 지켜보는 중입니다"; sub = critDone ? `오늘 심각 사건 ${critDone}건은 모두 마무리됐습니다.` : "오늘 경보·사건이 없습니다."; }
  const acts = nextActions(st);
  const list = acts.map((a, i) => `<li class="${a.cls}">${ic(a.icon)}<span class="nt">${a.text}</span><button class="btn sm${i === 0 && a.cls === "crit" ? " primary" : ""}" type="button" data-next="${esc(a.btn[1])}">${esc(a.btn[0])}${ic("chevron-right", "sm")}</button></li>`).join("");
  $("#opsHero").className = "hero " + cls;
  $("#opsHero").innerHTML = `<div class="hi">${ic(icon)}</div><div class="ht"><h2>${esc(title)}</h2><p>${esc(sub)}</p>${btn ? `<button class="btn${cls === "crit" || cls === "idle" ? " primary" : ""} hbtn" type="button" data-next="${esc(btn[1])}">${ic(btn[2])}${esc(btn[0])}</button>` : ""}</div>
    <div class="hmeta"><span>마지막 읽음 ${esc(st.collect.last_read || "-")}</span><span>가동 ${esc(up)}</span></div>
    ${acts.length ? `<div class="next"><h3>${ic("list-checks", "sm")}할 일</h3><ol>${list}</ol></div>` : ""}`;
  $$("#opsHero [data-next]").forEach(b => b.onclick = () => runNext(b.dataset.next)); }
const rowLab = r => `${mmdd(r.day)}${r.replay ? " 재현" : S.tl && r.day === S.tl.today ? " 오늘" : ""}`;
const lastRowWith = key => ((S.tl && S.tl.days) || []).find(r => r[key].some(x => x != null));

/* 탭 묶음 — 누르기·화살표로 고른다. sel(키) 를 돌려준다 */
function tabset(list, attr, onSel) {
  const tabs = () => $$("[role=tab]", list);
  const sel = k => tabs().forEach(b => { const on = b.dataset[attr] === k; b.setAttribute("aria-selected", on); b.tabIndex = on ? 0 : -1;
    $("#" + b.getAttribute("aria-controls")).hidden = !on; if (on) onSel(k); });
  tabs().forEach(b => { b.onclick = () => sel(b.dataset[attr]); b.onkeydown = e => { const k = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0; if (!k) return;
    e.preventDefault(); const vis = tabs().filter(t => t.offsetParent !== null), i = vis.indexOf(b), n = vis[(i + k + vis.length) % vis.length]; sel(n.dataset[attr]); n.focus(); }; });
  return sel; }
/* 오늘 — 사건 · 경보. 사람이 고르기 전에는 사건이 없고 경보만 있으면 경보를 먼저 보인다 */
$("#tt-inc").dataset.t = "inc"; $("#tt-al").dataset.t = "al";
const todayTab = tabset($("#todaySeg"), "t", k => { S.todayTab = k; });
/* 자세히 — 처리 여유 · 상태 기록 · 장비 · 시험(운영자). 고른 탭은 이 브라우저에 남는다 */
const MORE_KEY = "talog.more";
const moreTab = tabset($("#moreTabs"), "more", k => { try { localStorage.setItem(MORE_KEY, k); } catch (e) { /* 사생활 보호 모드 */ }
  if ((k === "cap" || k === "strip") && S.tl) renderStrip(S.tl); });
function initMore() { let k = "cap"; try { k = localStorage.getItem(MORE_KEY) || "cap"; } catch (e) { k = "cap"; }
  if (!$(`#moreTabs [data-more="${k}"]`) || (k === "test" && !atLeast("operator"))) k = "cap"; moreTab(k); }
const lastIdx = arr => { for (let h = arr.length - 1; h >= 0; h--) if (arr[h] != null) return h; return -1; };
/** 오늘 — 사건(상태 칩 · 원인 한 줄)과 경보. 사건 줄을 누르면 사건 화면으로 */
function renderOpsLists(st) {
  const inc = st.incidents, al = st.alerts;
  $("#todayIncN").textContent = inc.length; $("#todayAlN").textContent = al.length;
  if (!S.todayTab) todayTab(!inc.length && al.length ? "al" : "inc");
  $("#opsInc").innerHTML = inc.length ? inc.map(r => `<tr class="click" tabindex="0" data-id="${esc(r.id)}"><td><div class="li">${sevTag(r.severity)}<b>${esc(r.title)}</b><span class="when">${esc(r.time)}</span><div class="li-sub">${stateTag(r.state)}<span>${esc(r.cause || "")}</span></div></div></td></tr>`).join("")
    : emptyRow(1, "오늘 사건 없음", st.running ? "경보가 사건으로 묶이면 여기에 나옵니다. 지난 기록은 '지난 사건까지 보기' 에서 봅니다." : "감시가 정지되어 있습니다.", "check-circle");
  $$("#opsInc tr.click").forEach(tr => tr.onclick = () => { go("inc", {focus: false}); openInc(tr.dataset.id, false); });
  $("#opsAlerts").innerHTML = al.length ? al.slice(0, 14).map(a => `<li class="${esc(a.severity)}"><span class="tt">${esc((a.time || "").slice(6) || a.time)}</span><span class="dot"></span><div><div class="row" style="gap:6px">${sevTag(a.severity)}<b>${esc(a.title)}</b></div><div class="sub">${esc(a.evidence)}</div></div></li>`).join("")
    : `<li style="display:block">${emptyBox(st.running ? "오늘 경보 없음" : "감시 정지", st.running ? "정상 감시 중입니다." : "위 막대의 '감시 시작'으로 다시 켭니다.", st.running ? "check-circle" : "square")}</li>`; }
function renderGpu(st) { const gf = st.gpu_free || [];
  $("#gpuBox").innerHTML = gf.length ? gf.map(g => { const used = g.total - g.free, pct = 100 * used / g.total; const t = (st.gpu.find(x => x.gpu === g.gpu) || {}).temp;
    return `<div class="gpu-row"><b>GPU${g.gpu}</b><div class="bar" role="img" aria-label="VRAM ${pct.toFixed(0)}% 사용"><i class="${pct > 85 ? "hi" : ""}" style="width:${pct.toFixed(0)}%"></i></div><span class="mono">${(used / 1024).toFixed(1)}/${(g.total / 1024).toFixed(0)} GB · ${g.util}%${t != null ? " · " + t + "°C" : ""}</span></div>`; }).join("")
    : emptyBox("GPU 정보 없음", "nvidia-smi 를 찾지 못했습니다.", "cpu"); }
/* ── 차트 캡션: 그림과 같은 데이터로 만든 한 줄 요약 (charts #5·#10·#18 — 글 요약·주석, 색만으로 알리지 않기) ── */
function setSum(el, cls, html) {
  const icon = cls === "crit" || cls === "warn" ? "alert" : cls === "ok" ? "check-circle" : "info";
  el.className = "sum" + (cls ? " " + cls : "");
  el.innerHTML = `${ic(icon)}<span><b>요약</b> · ${html}</span>`;
}
/** 최근 상태 기록(히트맵) 요약 — 실시간과 재현을 나눠 센다 */
function stripSummary(T) {
  const tally = rows => {
    let c = 0, w = 0, rj = 0, last = null;
    rows.forEach(r => {
      r.alerts.forEach((a, h) => {
        if (a[0]) { c++; if (!last || r.day > last.day || (r.day === last.day && h > last.h)) last = {day: r.day, h}; }
        if (a[1]) w++;
      });
      r.rejects.forEach(x => { rj += x || 0; });
    });
    return {c, w, rj, last};
  };
  const live = (T.days || []).filter(r => !r.replay), rep = (T.days || []).filter(r => r.replay);
  const L = tally(live);
  let html = !live.length ? "아직 실시간 기록이 없습니다."
    : L.c || L.w ? `최근 ${live.length}일 실시간 — <b>심각 ${L.c}시간</b> · 주의 ${L.w}시간${L.last ? ` · 가장 최근 심각 ${mmdd(L.last.day)} ${L.last.h}시` : ""}${L.rj ? ` · 검사 시작 거부 ${L.rj}건` : ""}.`
    : `최근 ${live.length}일 실시간 기록에 주의·심각 경보가 없습니다.`;
  if (rep.length) {
    const R = tally(rep);
    html += ` 재현 ${rep.map(r => mmdd(r.day)).join("·")}: 심각 ${R.c}시간 · 주의 ${R.w}시간${R.rj ? ` · 거부 ${R.rj}건` : ""} (지난 로그를 다시 돌린 기록).`;
  }
  return {cls: L.c ? "crit" : L.w ? "warn" : live.length ? "ok" : "", html};
}
/** 처리 여유 추세 요약 — 가장 빠듯했던 시각, 위험·주의 띠에 머문 시간 */
function tolSummary(row) {
  if (!row) return {cls: "", html: "아직 추세가 없습니다 — 검사가 들어오면 시간마다 그룹별 처리 여유를 기록합니다."};
  const pts = row.tol_pct.map((v, h) => [h, v]).filter(p => p[1] != null);
  const mn = pts.reduce((a, p) => p[1] < a[1] ? p : a), last = pts[pts.length - 1];
  const b20 = pts.filter(p => p[1] < 20).length, b40 = pts.filter(p => p[1] < 40).length;
  const [cls, word] = tolWord(mn[1]);
  return {cls, html: `<b>${word}</b> — ${rowLab(row)}에서 가장 빠듯했던 때는 <b>${mn[0]}시 ${fmtPct(mn[1])}</b>${mn[1] < 0 ? "(한계를 넘음 — 거부가 날 수 있던 시간)" : ""}. 20% 아래 ${b20}시간 · 40% 아래 ${b40}시간 · 마지막 기록 ${last[0]}시 ${fmtPct(last[1])}.`};
}
/** 그룹별 처리 여유 요약 — 한계를 넘은·위험·빠듯 그룹 */
function capSummary(rows) {
  const xs = rows.filter(r => r.limit_s && r.hold_p99 != null).map(r => ({g: r.group, u: 100 * r.hold_p99 / r.limit_s}));
  if (!xs.length) return null;
  const over = xs.filter(x => x.u >= 100), risk = xs.filter(x => x.u >= 80 && x.u < 100), tight = xs.filter(x => x.u >= 60 && x.u < 80);
  const names = a => a.slice(0, 4).map(x => x.g).join("·") + (a.length > 4 ? " 외" : "");
  const parts = [];
  if (over.length) parts.push(`<b>한계를 넘은 그룹 ${over.length}개</b>(${names(over)})`);
  if (risk.length) parts.push(`<b>위험 ${risk.length}개</b>(${names(risk)})`);
  if (tight.length) parts.push(`빠듯 ${tight.length}개(${names(tight)})`);
  return {cls: over.length || risk.length ? "crit" : tight.length ? "warn" : "ok",
    html: parts.length ? `${xs.length}개 그룹 중 ${parts.join(" · ")}. 막대가 오른쪽 끝(한계)에 닿으면 그 그룹에 검사 시작 거부가 납니다.`
      : `${xs.length}개 그룹 모두 여유가 있습니다 — 느린 검사도 한계의 60% 안에서 끝납니다.`};
}
async function loadTimeline() { try { S.tl = await api("/api/timeline?days=7"); } catch (e) { return; } renderStrip(S.tl); loadBrief(); }
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
  const ss = stripSummary(T); setSum($("#stripSum"), ss.cls, ss.html);
  const pick = lastRowWith("tol_pct");
  $("#tolNote").textContent = pick ? rowLab(pick) : "";
  const w = Math.round($("#opsTol").clientWidth) || 640;
  $("#opsTol").innerHTML = pick ? tolChart(pick.tol_pct, `${rowLab(pick)} 시간별 최저 일시 지연 내성`, Math.max(300, w)) : emptyBox("아직 추세 없음", "검사가 들어오면 시간마다 그룹별 처리 여유를 기록합니다.", "activity");
  const ts = tolSummary(pick); setSum($("#tolSum"), ts.cls, ts.html);
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
    return `<div class="bul"><span class="g">그룹 ${esc(String(r.group))}</span><div class="bul-bar" role="img" aria-label="그룹 ${esc(String(r.group))}: 느린 검사 점유 ${f(r.hold_p99)}초, 한계 ${f(r.limit_s)}초, 버틸 여유 ${tol != null ? fmtPct(tol) : "모름"} (${tolWord(tol)[1]})"><i style="width:${u.toFixed(1)}%;background:${u >= 80 ? "var(--crit)" : u >= 60 ? "var(--warn)" : "var(--ink-2)"}"></i></div><span class="v">${f(r.hold_p99)} / ${f(r.limit_s)}초 · 여유 <b class="${tolWord(tol)[0]}">${tol != null ? fmtPct(tol) : "-"}</b></span></div>`; }).join("")
    : emptyBox("아직 데이터 없음", "감시가 검사를 읽기 시작하면 그룹마다 첫 1시간으로 기준선을 잡습니다. 지난 날은 '분석 → 경보 재현'으로 기록을 만듭니다.", "gauge");
  $("#capRows").innerHTML = rows.length ? rows.map(r => `<tr><td class="mono">${esc(String(r.group || "단일"))}</td><td>${lab[r.status] || esc(r.status || "")}</td><td class="mono">${f(r.idle_median, 1)} / ${f(r.idle_baseline, 1)}</td><td class="mono">${f(r.hold_p50)}초 ${r.hold_ratio != null ? `(×${f(r.hold_ratio)})` : ""}</td><td class="mono">${f(r.hold_p99)}초 / ${r.limit_s != null ? f(r.limit_s) + "초" : "-"}</td><td class="mono">${r.tolerance_s != null ? f(r.tolerance_s) + "초" : "-"}</td><td class="mono">${f(r.interval_s, 3)}초</td></tr>`).join("")
    : emptyRow(7, "아직 데이터 없음", "검사가 들어오면 그룹별 기준선을 잡습니다.", "gauge");
  const cs = capSummary(rows); $("#capSum").hidden = !cs; if (cs) setSum($("#capSum"), cs.cls, cs.html);
  const b = rows.find(r => r.baseline_from); $("#capNote").textContent = b ? `기준선 ${b.baseline_from}부터` : bul.length && rows.length > bul.length ? `가장 빠듯한 ${bul.length}개 / ${rows.length}개 그룹` : ""; }
$("#btnRun").onclick = async () => { const on = S.st && S.st.running;
  if (on && !(await needMode("operator", "감시 정지는 운영자 모드에서 합니다"))) return;
  if (on && !(await confirmBox({title: "감시를 멈출까요?", body: "멈춘 동안에는 새 경보·사건·메일이 나오지 않습니다.", ok: "감시 정지", danger: true}))) return;
  await busy($("#btnRun"), async () => { const r = await api("/api/control", {action: on ? "stop" : "start"}); toast(r.running ? "감시를 시작했습니다" : "감시를 멈췄습니다", "ok"); });
  loadStatus(); };
$("#btnInject").onclick = async () => {
  if (!(await needMode("operator", "시험 경보는 운영자 모드에서 넣습니다"))) return;
  const mailOn = S.st && S.st.notify && S.st.notify.provider !== "off";
  if (mailOn && !(await confirmBox({title: "시험 경보를 넣을까요?", body: `메일이 켜져 있어 받는 사람 ${S.st.notify.to}명에게 시험 메일이 실제로 발송됩니다.`, ok: "넣고 발송"}))) return;
  await busy($("#btnInject"), async () => { const r = await api("/api/test/alert", {kind: $("#injKind").value}); toast(r.ok ? r.message : r.error, r.ok ? "ok" : "err"); });
  setTimeout(loadStatus, 1500); };
async function loadLog() { const r = await api("/api/log"); const b = $("#logBox"); const atEnd = b.scrollTop + b.clientHeight >= b.scrollHeight - 20; b.textContent = r.lines.join("\n") || "(아직 로그 없음)"; if (atEnd) b.scrollTop = b.scrollHeight; }
$("#logDetails").addEventListener("toggle", () => { if ($("#logDetails").open) loadLog(); });

