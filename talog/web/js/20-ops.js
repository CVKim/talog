/* ── 운영 현황 ─────────────────────────────────────────── */
async function loadStatus() {
  let st; try { st = await api("/api/status"); } catch (e) { $("#runText").textContent = "연결 끊김"; $("#runPill").className = "live off"; return; }
  S.st = st; applyAccess(st.access);
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
/* ── 처리 여유 공용 — 실시간 기록만 쓴다(재현 기록으로 '지금 할 일'을 만들지 않는다) ── */
const pctNum = v => (+v).toFixed(1).replace(/\.0$/, "");
const fmtPct = v => `${pctNum(v)}%`;
/** 버틸 여유 % → [상태 클래스, 낱말] (색만으로 알리지 않는다 — ux #37) */
const tolWord = v => v == null ? ["", "기록 없음"] : v < 20 ? ["crit", "위험"] : v < 40 ? ["warn", "빠듯"] : ["ok", "여유"];
/** 실시간 처리 여유 행 + 버틸 여유 % (pct) */
function liveCapacity(st) {
  return ((st && st.capacity) || []).filter(r => r.limit_s && r.tolerance_s != null)
    .map(r => ({...r, pct: 100 * r.tolerance_s / r.limit_s}));
}

/** '지금 할 일' — 상태에서 사람이 할 일을 급한 순서로 (ux #80: 오류·상태에는 다음 단계를) */
function nextActions(st) {
  const out = [];
  const crit = st.incidents.filter(r => r.severity === "crit");
  if (!st.running) out.push({cls: "warn", icon: "play", text: "<b>감시가 멈춰 있습니다.</b> 멈춘 동안에는 경보·사건·메일이 나오지 않습니다.", btn: ["감시 시작", "start"]});
  if (crit.length) out.push({cls: "crit", icon: "siren", text: `<b>심각 사건 ${crit.length}건</b> — 원인과 권고 조치를 확인하십시오. 가장 최근: ${esc(crit[0].time)} ${esc(crit[0].title)}`, btn: ["사건 열기", "inc:" + crit[0].id]});
  const low = liveCapacity(st).filter(r => r.pct < 20).sort((a, b) => a.pct - b.pct);
  if (low.length) out.push({cls: low[0].pct < 0 ? "crit" : "warn", icon: "gauge",
    text: `<b>그룹 ${esc(String(low[0].group))}${low.length > 1 ? ` 외 ${low.length - 1}개` : ""}의 버틸 여유 ${fmtPct(low[0].pct)}</b> — 잠깐의 지연으로도 검사 시작 거부가 날 수 있습니다. 다음 정지 때 재시작을 계획하십시오.`,
    btn: ["처리 여유 보기", "cap"]});
  if (st.running && !st.collect.exists) out.push({cls: "warn", icon: "folder-open", text: "<b>오늘 로그 폴더가 없습니다.</b> 설비가 쉬는 날이 아니라면 수집 설정의 로그 루트를 확인하십시오.", btn: ["수집 설정", "sec:s-collect"]});
  if (st.notify.failed) out.push({cls: "crit", icon: "mail", text: `<b>메일 발송 실패 ${st.notify.failed}건</b> — 알림 설정의 '연결 확인'으로 계정을 점검하십시오.`, btn: ["알림 설정", "sec:s-notify"]});
  if (st.judge.llm && st.judge.alive === false) out.push({cls: "warn", icon: "cpu", text: "<b>로컬 AI(LLM)가 응답하지 않습니다.</b> 판단은 규칙 진단만으로 합니다.", btn: ["판단 설정", "sec:s-judge"]});
  if (st.notify.provider === "off") out.push({cls: "info", icon: "mail", text: "<b>알림 메일이 꺼져 있습니다.</b> 심각 사건이 나도 담당자에게 메일이 가지 않습니다.", btn: ["알림 설정", "sec:s-notify"]});
  return out.slice(0, 4);
}
function runNext(act) {
  if (act === "start") $("#btnRun").click();
  else if (act === "cap") { const c = $("#capBullets").closest(".card"); c.scrollIntoView({behavior: reduceMotion() ? "auto" : "smooth", block: "start"}); }
  else if (act.startsWith("sec:")) goSec(act.slice(4));
  else if (act.startsWith("inc:")) { go("inc", {focus: false}); openInc(act.slice(4), false); }
}
function renderHero(st) {
  const D = st.detect, crit = st.incidents.filter(r => r.severity === "crit"), warnN = D.warn;
  const up = st.uptime_s ? (st.uptime_s >= 3600 ? (st.uptime_s / 3600).toFixed(1) + "시간" : Math.round(st.uptime_s / 60) + "분") : "-";
  let cls, icon, title, sub;
  if (!st.running) { cls = "idle"; icon = "square"; title = "감시가 멈춰 있습니다"; sub = "멈춘 동안에는 새 경보·사건·메일이 나오지 않습니다."; }
  else if (crit.length || D.crit) { cls = "crit"; icon = "alert"; title = `심각 ${crit.length || D.crit}건 — 확인이 필요합니다`; sub = crit[0] ? `${crit[0].time} ${crit[0].title} · ${crit[0].cause || ""}` : (D.last ? D.last.title : ""); }
  else if (!st.collect.exists) { cls = "warn"; icon = "folder-open"; title = "오늘 로그 폴더가 아직 없습니다"; sub = "로그 루트 아래에 오늘 날짜 폴더가 생기면 자동으로 읽기 시작합니다."; }
  else if (warnN) { cls = "warn"; icon = "alert"; title = `주의 ${warnN}건`; sub = D.last ? `마지막: ${D.last.title}` : ""; }
  else { cls = "ok"; icon = "check-circle"; title = "정상 감시 중"; sub = "오늘 경보·사건이 없습니다."; }
  const acts = nextActions(st);
  const list = acts.length
    ? acts.map((a, i) => `<li class="${a.cls}">${ic(a.icon)}<span class="nt">${a.text}</span><button class="btn sm${i === 0 && a.cls === "crit" ? " primary" : ""}" type="button" data-next="${esc(a.btn[1])}">${esc(a.btn[0])}${ic("chevron-right", "sm")}</button></li>`).join("")
    : `<li class="ok none">${ic("check-circle")}<span class="nt">지금 할 일이 없습니다 — 정상 감시 중입니다. 경보가 나면 여기에 할 일이 순서대로 나옵니다.</span></li>`;
  $("#opsHero").className = "hero " + cls;
  $("#opsHero").innerHTML = `<div class="hi">${ic(icon)}</div><div class="ht"><h2>${esc(title)}</h2><p>${esc(sub)}</p><div class="hmeta"><span>가동 ${esc(up)}</span><span>마지막 읽음 ${esc(st.collect.last_read || "-")}</span><span>규칙 ${st.detect.rules}개</span><span>${hhmmss()} 기준</span></div></div>
    <div class="next"><h3>${ic("list-checks", "sm")}지금 할 일</h3><ol>${list}</ol></div>`;
  $$("#opsHero [data-next]").forEach(b => b.onclick = () => runNext(b.dataset.next)); }
const rowLab = r => `${mmdd(r.day)}${r.replay ? " 재현" : S.tl && r.day === S.tl.today ? " 오늘" : ""}`;
const lastRowWith = key => ((S.tl && S.tl.days) || []).find(r => r[key].some(x => x != null));
const lastIdx = arr => { for (let h = arr.length - 1; h >= 0; h--) if (arr[h] != null) return h; return -1; };
function renderKpis(st) {
  const inc = st.incidents, crit = inc.filter(r => r.severity === "crit").length, warn = inc.filter(r => r.severity === "warn").length;
  const RN = lastRowWith("n"), hN = RN ? lastIdx(RN.n) : -1, prev = hN > 0 && RN.n[hN - 1] != null ? RN.n[hN - 1] : null;
  const live = liveCapacity(st), minCap = live.length ? live.reduce((a, b) => b.pct < a.pct ? b : a) : null;
  let tolV = null, tolWhere = "그룹별 기준선을 잡는 중", tolSp = "", tolLive = false;
  if (minCap) { tolV = minCap.pct; tolLive = true; tolWhere = `지금 · 그룹 ${esc(String(minCap.group))}`; }
  else { const RT = lastRowWith("tol_pct"); if (RT) { const h = lastIdx(RT.tol_pct); tolV = RT.tol_pct[h]; tolWhere = `${rowLab(RT)} ${h}시 기록`; tolSp = spark(RT.tol_pct, {warnBelow: 20}); } }
  // 지금 값이 아니면(지난 기록·재현) '지금'이라고 말하지 않는다
  const tolX = tolV == null ? "기동 뒤 약 70분이 지나면 그룹마다 평소 상태를 잡고 계산합니다."
    : !tolLive ? `지난 기록입니다 — 그때는 ${tolV < 0 ? "검사 시작 거부가 날 수 있는 상태였습니다" : tolV < 20 ? "잠깐의 지연으로도 거부가 날 수 있었습니다" : tolV < 40 ? "여유가 줄어 있었습니다" : "여유가 있었습니다"}.`
    : tolV < 0 ? "지금도 검사 시작 거부가 날 수 있는 상태입니다." : tolV < 20 ? "잠깐의 지연으로도 거부가 날 수 있습니다."
    : tolV < 40 ? "여유가 줄었습니다 — 추세를 지켜보십시오." : "잠깐 느려져도 거부 없이 버틸 수 있습니다.";
  const J = st.judge;
  const tiles = [
    {icon: "siren", label: "오늘 사건", tip: "incident", value: `${inc.length}<small>건</small>`,
      st: crit ? ["crit", "확인 필요"] : warn ? ["warn", "주의"] : ["ok", "없음"],
      line: `<span class="sev crit">심각 ${crit}</span><span class="sev warn">주의 ${warn}</span>`,
      x: crit ? "심각 사건부터 원인과 권고 조치를 확인하십시오." : "경보를 묶어 원인·조치를 붙인 기록입니다."},
    {icon: "activity", label: "시간당 검사", value: hN >= 0 ? `${RN.n[hN].toLocaleString()}<small>건</small>` : "—", st: null,
      line: hN >= 0 ? `${rowLab(RN)} ${hN}시${prev != null ? ` · 앞 시간 ${prev.toLocaleString()}건` : ""}` : "검사 기록 없음",
      x: "한 시간에 들어온 검사 수입니다. 그룹이 여럿이어도 한 번만 셉니다.", sp: RN ? spark(RN.n) : ""},
    {icon: "gauge", label: "버틸 여유 (가장 낮은 그룹)", tip: "tolerance", value: tolV != null ? `${pctNum(tolV)}<small>%</small>` : "—",
      st: tolV == null ? null : tolWord(tolV), line: tolWhere, x: tolX, sp: tolSp},
    {icon: "check-circle", label: "규칙·AI 판단 일치", tip: "gate", value: J.llm ? `${J.agree}<small>/ ${J.incidents}</small>` : "규칙만",
      st: !J.llm || !J.incidents ? null : J.agree < J.incidents ? ["warn", "다른 판단 있음"] : ["ok", "모두 일치"],
      line: J.llm ? `규칙 + ${esc(J.model)} · 오늘` : "로컬 AI 교차 확인 꺼짐",
      x: J.llm ? "규칙 진단과 로컬 AI 가 같은 원인을 고른 사건 수입니다." : "판단은 규칙 진단만 합니다 — 설정 → 판단에서 켭니다."}];
  $("#opsKpis").innerHTML = tiles.map(t => `<div class="kpi"><span class="kl">${ic(t.icon, "sm")}${esc(t.label)}${t.tip ? term(t.tip) : ""}</span><span class="kv">${t.value}</span><span class="ks">${t.st ? `<span class="kst ${t.st[0]}">${esc(t.st[1])}</span>` : ""}${t.line}</span><span class="kx">${esc(t.x)}</span>${t.sp || ""}</div>`).join(""); }
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

