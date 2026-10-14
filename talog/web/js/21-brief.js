/* ── 에이전트 브리핑 (운영 현황 맨 위) ─────────────────────────────────
   talog 에이전트가 '지난번 확인 뒤 무슨 일이 있었나 · 오늘 한 일 · 처리 여유 전망 · 마무리 안 된 사건'을 문장으로 정리한다.
   글은 모두 상태·기록 데이터로 만든다 — LLM 을 쓰지 않으므로 지어낸 숫자가 없다. 지난번 확인 시각은 이 브라우저에 남긴다
   (localStorage, 실패해도 동작). 버튼은 '지금 할 일'과 같은 이동(runNext)을 쓴다. */
const SEEN_KEY = "talog.seen", FOLD_KEY = "talog.brief";
let seenPrev = null, seenSaved = 0, briefHtml = "";
try { seenPrev = +localStorage.getItem(SEEN_KEY) || null; } catch (e) { seenPrev = null; }
/** 화면을 보고 있는 동안 '마지막 확인'을 30초마다 남긴다 — 다음에 열 때 그 뒤에 생긴 일을 먼저 알린다 */
function saveSeen() { if (document.visibilityState !== "visible" || Date.now() - seenSaved < 30000) return;
  seenSaved = Date.now(); try { localStorage.setItem(SEEN_KEY, String(seenSaved)); } catch (e) { /* 사생활 보호 모드 */ } }
const OPEN = r => !["resolved", "false_alarm"].includes(r.state || "open");
const incDate = r => { const d = String(r.day || ""), t = String(r.time || "00:00:00");
  return d.length === 8 ? new Date(+d.slice(0, 4), +d.slice(4, 6) - 1, +d.slice(6, 8), +t.slice(0, 2) || 0, +t.slice(3, 5) || 0, +t.slice(6, 8) || 0) : null; };
const fmtSeen = ms => { const d = new Date(ms), same = d.toDateString() === new Date().toDateString();
  return `${same ? "오늘" : `${d.getMonth() + 1}/${d.getDate()}`} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`; };

async function loadBrief() { try { const r = await api("/api/incidents?days=7&replay=0"); S.briefInc = r.rows || []; } catch (e) { return; }
  if (S.st) renderBrief(S.st); }
function briefItems(st) {
  const rows = S.briefInc || st.incidents || [], out = [];
  // 1) 지난번 확인 뒤
  if (seenPrev) {
    const fresh = rows.filter(r => { const d = incDate(r); return d && d.getTime() > seenPrev; }), c = fresh.filter(r => r.severity === "crit");
    out.push(fresh.length
      ? {icon: "siren", cls: c.length ? "crit" : "warn", text: `지난번 확인(${fmtSeen(seenPrev)}) 뒤 새 사건 <b>${fresh.length}건</b>${c.length ? ` — 심각 ${c.length}건, 가장 최근 ${esc(c[0].time)} ${esc(c[0].title)}` : ""}.`, btn: ["사건 보기", "inc:" + (c[0] || fresh[0]).id]}
      : {icon: "check-circle", cls: "ok", text: `지난번 확인(${fmtSeen(seenPrev)}) 뒤 새 사건은 없습니다.`});
  } else out.push({icon: "sparkles", cls: "", text: "처음 여셨습니다 — 이 설비의 오늘 기록을 정리했습니다. 다음부터는 마지막으로 본 뒤 생긴 일을 먼저 알려 드립니다."});
  // 2) 오늘 한 일
  const T = S.tl, today = T && (T.days || []).find(r => r.day === T.today && !r.replay);
  const nToday = today ? today.n.reduce((a, b) => a + (b || 0), 0) : 0, inc = st.incidents, D = st.detect, nAl = D.crit + D.warn + D.info;
  // (감시가 멈춘 것은 맨 위 상태 카드가 말한다 — 여기서는 되풀이하지 않고 오늘 한 일만)
  if (!nToday && !nAl) out.push({icon: "activity", cls: "", text: "오늘은 아직 읽은 검사가 없습니다."});
  else out.push({icon: "activity", cls: "", text: `오늘 검사 <b>${nToday.toLocaleString()}건</b>을 지켜보며 `
    + (nAl ? `경보 ${nAl}건을 사건 ${inc.length}건으로 묶어 분석했습니다${inc.length ? ` — 규칙·로컬 AI 판단 일치 ${inc.filter(r => r.gate === "agree").length}건` : ""}${st.notify.sent ? `, 메일 ${st.notify.sent}통` : ""}.`
      : "경보 없이 정상으로 봤습니다.")});
  // 3) 처리 여유 전망 (실시간 값만 — 재현·지난 기록을 '지금'으로 말하지 않는다)
  const live = liveCapacity(st);
  if (live.length) { const m = live.reduce((a, b) => b.pct < a.pct ? b : a), [wc, w] = tolWord(m.pct);
    let trend = ""; const v = today ? today.tol_pct.filter(x => x != null) : [];
    if (v.length >= 2) { const d = v[v.length - 1] - v[0]; trend = Math.abs(d) < 3 ? " 오늘 들어 비슷합니다." : ` 오늘 들어 ${pctNum(Math.abs(d))}%p ${d < 0 ? "줄었습니다" : "늘었습니다"}.`; }
    out.push({icon: "gauge", cls: wc, text: `가장 빠듯한 그룹은 <b>${esc(String(m.group))}</b> — 버틸 여유 ${fmtPct(m.pct)}(${w}).${trend}`, btn: ["처리 여유 보기", "cap"]}); }
  else if (st.running) out.push({icon: "gauge", cls: "", text: "그룹별 평소 상태(기준선)를 잡는 중입니다 — 기동 뒤 약 70분이 지나면 처리 여유를 알려 드립니다."});
  // 4) 마무리 안 된 사건 — 사람의 해결 기록이 다음 판단의 근거가 된다
  const open = rows.filter(OPEN);
  if (open.length) out.push({icon: "list-checks", cls: open.some(r => r.severity === "crit") ? "warn" : "",
    text: `마무리 안 된 사건 <b>${open.length}건</b> — 원인과 한 조치를 '해결 기록'으로 남기면 다음에 비슷한 사건이 날 때 talog 가 함께 보여 드립니다.`, btn: ["사건 보기", "inc:" + open[0].id]});
  else if (rows.length) out.push({icon: "check-circle", cls: "ok", text: "최근 7일 사건은 모두 마무리됐습니다."});
  return out; }
function renderBrief(st) {
  saveSeen();
  $("#briefWhen").textContent = `${hhmmss().slice(0, 5)} 정리 · 상태 기록으로 만든 글`;
  const html = briefItems(st).map(x => `<li class="${x.cls}">${ic(x.icon)}<span class="bt">${x.text}</span>${x.btn ? `<button class="btn sm" type="button" data-next="${esc(x.btn[1])}">${esc(x.btn[0])}${ic("chevron-right", "sm")}</button>` : ""}</li>`).join("");
  if (html === briefHtml) return;                     // 5초마다 같은 글이면 다시 그리지 않는다 (누르던 버튼 유지)
  briefHtml = html; $("#briefList").innerHTML = html;
  $$("#briefList [data-next]").forEach(b => b.onclick = () => runNext(b.dataset.next)); }
function foldBrief(fold) { $("#opsBrief").classList.toggle("folded", fold); $("#btnBriefFold").setAttribute("aria-expanded", !fold);
  $("#btnBriefFold").textContent = fold ? "펼치기" : "접기"; try { localStorage.setItem(FOLD_KEY, fold ? "1" : "0"); } catch (e) { /* 무시 */ } }
$("#btnBriefFold").onclick = () => foldBrief(!$("#opsBrief").classList.contains("folded"));
try { if (localStorage.getItem(FOLD_KEY) === "1") foldBrief(true); } catch (e) { /* 무시 */ }
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") { seenSaved = 0; try { localStorage.setItem(SEEN_KEY, String(Date.now())); } catch (e) { /* 무시 */ } } });
