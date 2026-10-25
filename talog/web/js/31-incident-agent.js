/* ── 사건 — 에이전트 처리 과정 · 비슷한 과거 사례 · 사람의 마무리 ────────────────────────────
   처리 과정: 사건 기록(경보·근거·룰 진단·과거 사례·로컬 AI·수치 대조·합의·메일·사람의 확인)으로 에이전트가 거친 단계를
   그대로 보여 준다 — 새로 계산하거나 지어내지 않는다. 마무리: 해결 — 이대로(한 번) / 고쳐서 기록(원인·한 조치·메모,
   대화상자) / 오경보 / 확인만 / 다시 열기 (console/feedback.py). 해결 기록은 이 설비의 사례 KB 에 남아 다음 비슷한 사건에 '비슷한 과거 사례'로 붙는다. */
const ISTATE = {open: ["새 사건", "info", "siren"], ack: ["확인 중", "warn", "user"], resolved: ["해결", "ok", "check-circle"], false_alarm: ["오경보", "", "x-circle"]};
const stateTag = s => { const [l, c, i] = ISTATE[s] || ISTATE.open; return `<span class="st-chip ${c}">${ic(i, "sm")}${l}</span>`; };

/** 사건 상태 줄 — 지금 상태 + 다음 표시. '해결 — 이대로'는 한 번 누르면 끝(되돌리기 알림), 고쳐 쓸 때만 대화상자 */
function stateBar(d) { const s = d.state || "open";
  const b = (k, icon, label, cls = "", title = "") => `<button class="btn sm ${cls}" type="button" data-st="${k}"${title ? ` title="${esc(title)}"` : ""}>${ic(icon)}${label}</button>`;
  const acts = s === "open" || s === "ack"
    ? b("quick", "check-circle", "해결 — 이대로", "primary", "talog 가 고른 원인과 첫 권고 조치를 그대로 해결 기록으로 남기고 사례로 저장합니다")
      + b("resolved", "pen", "고쳐서 기록") + b("false_alarm", "x-circle", "오경보") + (s === "open" ? b("ack", "user", "확인만", "ghost") : "")
    : b("open", "undo", "다시 열기");
  return `<div class="dstate"><span class="k">사건 상태${term("resolve")}</span>${stateTag(s)}<span class="spacer"></span>${acts}</div>`; }
/** talog 판단 그대로의 해결 기록 — 대화상자의 미리 채운 값과 같다 */
function quickResolution(d) { const v = d.verdict || {}, acts = v.actions || [];
  return {cause: (v.cause_name || "").split(" — ")[0], action: acts.length ? ((d.action_text || {})[acts[0]] || acts[0]) : "", save_case: true}; }

const EV_NAMES = {inspections: "검사 흐름", capacity: "처리 여유", trend: "추세", phases: "단계 시간", resources: "자원", rejects: "거부",
  timeouts: "타임아웃", restarts: "재시작", model_load: "모델 로드", errors: "에러", gpu_log: "GPU 로그", gpu_now: "GPU 지금", memory: "메모리", defects: "결함"};
const hasVal = v => v != null && v !== "" && !(Array.isArray(v) && !v.length) && !(typeof v === "object" && !Array.isArray(v) && !Object.keys(v).length);
/** 에이전트 처리 과정 — [상태, 아이콘, 단계, 설명] */
function traceSteps(d) {
  const al = d.alerts || [], ev = d.evidence || {}, rule = d.rule || {}, llm = d.llm || {}, v = d.verdict || {}, em = d.email || {}, sim = d.similar || [];
  const secs = d.t_close && d.t_trigger ? Math.round(d.t_close - d.t_trigger) : null, evk = Object.keys(EV_NAMES).filter(k => hasVal(ev[k])), out = [];
  out.push(["ok", "siren", "경보 받음", `경보 ${al.length}개 — 첫 경보 ${al[0] ? `${al[0].time} ${al[0].title}` : "-"}${d.dropped ? ` · 비슷한 경보 ${d.dropped}개는 묶음 한도로 줄임` : ""}`]);
  out.push(["ok", "list", "사건으로 묶음", `${d.opened || "-"} ~ ${d.closed || "-"}${secs != null ? ` (${secs}초 동안 들어온 경보를 하나로)` : ""}${d.immediate ? " · 심각 등급이라 바로 분석" : ""}`]);
  out.push([evk.length ? "ok" : "tr-skip", "database", "근거 모음", evk.length ? `${evk.map(k => EV_NAMES[k]).join("·")} — 핵심 사실 ${(ev.key_facts || []).length}개, 근거 로그 ${(ev.log_lines || []).length}줄` : "모은 근거가 없습니다"]);
  const known = rule.cause && rule.cause !== "unknown";
  out.push([known ? (rule.confirmed === false ? "warn" : "ok") : "warn", "check", "룰 진단", `${rule.name || "원인 미상"}${rule.strength != null ? ` · 근거 강도 ${rule.strength}` : ""}${rule.confirmed === false ? " · 오경보 의심" : ""}${(rule.facts || []).length ? ` · 확인한 사실 ${rule.facts.length}개` : ""}`]);
  out.push([sim.length ? "ok" : "tr-skip", "book", "비슷한 과거 사례", sim.length ? `${sim.length}건 — 가장 비슷한 사례: ${sim[0].title} (${Math.round((sim[0].score || 0) * 100)}%)` : "사례 KB 에 비슷한 사례가 없었습니다 — 이 사건의 해결 기록을 남기면 다음부터 찾습니다"]);
  if (!d.llm) out.push(["tr-skip", "cpu", "로컬 AI 2차 의견", "쓰지 않음 — 설정 → 3 판단에서 켭니다 (그때는 규칙 진단만으로 판단)"]);
  else if (!llm.ok) out.push(["crit", "cpu", "로컬 AI 2차 의견", `응답 실패 — ${llm.error || "원인 모름"} (규칙 진단으로 판단)`]);
  else {
    out.push(["ok", "cpu", "로컬 AI 2차 의견", `${llm.model || ""} · ${llm.device || ""} · ${llm.wall_s}초 — 원인: ${v.llm_cause_name || llm.root_cause || "-"}${llm.confidence != null ? ` (확신도 ${Math.round(llm.confidence * 100)}%)` : ""}`]);
    const f = llm.fact || {}, un = f.unsupported || [], dr = llm.message_dropped || [];
    out.push([un.length || f.hanzi ? "warn" : "ok", "flask", "수치 대조", `문구의 숫자·시각 ${f.n_numbers ?? "-"}개를 근거와 맞춤 — ${un.length ? `근거에 없는 값 ${un.length}개(${un.slice(0, 4).join(", ")}) → 메일에는 룰 문구를 씀` : "모두 근거에 있음"}${dr.length ? ` · 조치 문장 ${dr.length}개는 런북 문장으로 바꿈` : ""}${f.hanzi ? " · 한자가 섞여 다시 받음" : ""}`]);
  }
  out.push([gateCls(v.mode, v.label) === "agree" ? "ok" : "warn", "shield", "합의 관문", `${v.label || "-"} — ${gateExplain(v.mode, v.label)}`]);
  const ef = d.email_followup || {};
  out.push(em.sent ? ["ok", "mail", "알림", `메일 발송 → ${(em.to || []).length}명${ef.sent ? " · 로컬 AI 의견은 같은 스레드의 후속 메일로" : ""}`]
    : em.error ? ["crit", "mail", "알림", `메일 실패 — ${em.error}`]
    : em.eml ? ["tr-skip", "mail", "알림", "메일은 만들어 보관만 했습니다 (재현이거나 발송 꺼짐)"]
    : ["tr-skip", "mail", "알림", "메일 보내지 않음 (등급 기준 미만이거나 메일 꺼짐)"]);
  const n = (d.notes || []).slice(-1)[0];
  out.push(n ? [n.state === "resolved" ? "ok" : n.state === "false_alarm" ? "warn" : "", "user", "사람의 확인", `${(ISTATE[n.state] || ISTATE.open)[0]} · ${n.time}${n.cause ? ` — 원인: ${n.cause}` : ""}${n.case ? " · 사례 KB 에 저장" : ""}`]
    : ["tr-skip", "user", "사람의 확인", "아직 없음 — 위 '해결 — 이대로'로 마무리하면 다음 판단의 근거가 됩니다"]);
  return out; }
function incTrace(d) { const st = traceSteps(d);
  return `<p class="hint" style="margin:0 0 12px">talog 에이전트가 이 사건을 처리한 순서입니다. 모두 사건 기록에 남은 값입니다.</p>
    <ol class="trace">${st.map(([c, i, t, x], k) => `<li class="${c}"><span class="tn">${ic(i, "sm")}</span><div><b>${k + 1}. ${esc(t)}<span class="vh"> — ${c === "ok" ? "완료" : c === "warn" ? "확인 필요" : c === "crit" ? "실패" : c === "tr-skip" ? "건너뜀" : ""}</span></b><span>${esc(x)}</span></div></li>`).join("")}</ol>`; }

/** 비슷한 과거 사례 — 한 줄 요약으로 접어 두고(가장 비슷한 사례 이름·점수), 펼치면 원인·조치 */
function incSimilar(d) { const sim = d.similar || []; if (!sim.length) return "";
  return `<details class="simcases disclosure" data-k="sim" ${openAttr("sim", false)}><summary>${ic("book", "sm")}<span>비슷한 과거 사례 <b>${sim.length}건</b> — 가장 비슷한 것: ${esc(sim[0].title)} (${Math.round((sim[0].score || 0) * 100)}%)</span></summary><p class="meta" style="margin:0 0 8px">사례 KB · 이 설비의 해결 기록 포함 — 판단에는 쓰지 않고 참고로만 보입니다</p><ul>${sim.map(s => `<li><div class="sc-h"><b>${esc(s.title)}</b><span class="tag">${Math.round((s.score || 0) * 100)}%</span></div>
    <div class="sub">원인: ${esc(s.cause || "-")}</div><div class="sub">조치: ${esc(s.action || "-")}</div>${s.site || s.date ? `<div class="meta">${esc([s.site, s.date].filter(Boolean).join(" · "))}</div>` : ""}</li>`).join("")}</ul></details>`; }
function incNotes(d) { const ns = d.notes || []; if (!ns.length) return "";
  return `<section class="notes"><h3>${ic("user")}사람의 기록</h3><ol class="nl">${ns.slice().reverse().slice(0, 5).map(n => `<li><div class="sc-h">${stateTag(n.state)}<span class="meta">${esc(n.time)}${n.mode && typeof MODE_INFO !== "undefined" && MODE_INFO[n.mode] ? ` · ${esc(MODE_INFO[n.mode].label)}` : ""}</span></div>
    ${n.cause ? `<div class="sub">원인: ${esc(n.cause)}</div>` : ""}${n.action ? `<div class="sub">조치: ${esc(n.action)}</div>` : ""}${n.note ? `<div class="sub">메모: ${esc(n.note)}</div>` : ""}${n.case ? `<div class="meta">${ic("book", "sm")}사례 KB 에 저장 — 다음 비슷한 사건에 함께 보입니다</div>` : ""}</li>`).join("")}</ol></section>`; }

/* 마무리 표시 — 확인·다시 열기·'해결 — 이대로'·오경보는 바로(해결·오경보는 알림의 '되돌리기'로 다시 연다),
   고쳐서 기록만 대화상자에서. 되돌리면 그 해결 기록은 사례 KB 에서도 빠진다(console/feedback.py). */
async function setIncState(state, extra = {}) { const c = S.inc; if (!c) return false;
  const r = await api("/api/incident/state", {id: c.id, replay: !!c.rp, state, ...extra});
  if (!r || !r.ok) { toast((r && r.error) || "저장하지 못했습니다", "err"); return false; }
  const undo = state === "resolved" || state === "false_alarm" ? {label: "되돌리기", fn: () => setIncStateFor(c, "open")} : null;
  toast(state === "resolved" ? (r.case ? "해결로 기록했습니다 — 다음 비슷한 사건에 이 기록을 함께 보여 줍니다" : "해결로 기록했습니다")
    : state === "false_alarm" ? "오경보로 표시했습니다 — 설정의 규칙 피드백에 반영됩니다" : state === "ack" ? "확인 중으로 표시했습니다" : "다시 열었습니다", "ok", undo);
  await afterState(c); return true; }
/** 되돌리기 — 알림이 뜬 뒤 다른 사건을 열었어도 그 사건을 다시 연다 */
async function setIncStateFor(c, state) { const r = await api("/api/incident/state", {id: c.id, replay: !!c.rp, state});
  if (!r || !r.ok) { toast((r && r.error) || "되돌리지 못했습니다", "err"); return; }
  toast("다시 열었습니다" + (r.notes && r.notes.slice(-1)[0] && r.notes.slice(-1)[0].case_removed ? " — 사례 KB 에서도 뺐습니다" : ""), "ok"); await afterState(c); }
async function afterState(c) { if (S.inc && S.inc.id === c.id) await openInc(c.id, c.rp); if (S.incRows) loadInc(); loadBrief(); loadStatus(); }
function stateClick(k) { if (k === "quick") setIncState("resolved", quickResolution(S.inc.d)); else if (k === "resolved") resDlg(k); else setIncState(k); }
function resDlg(kind) { const d = S.inc && S.inc.d; if (!d) return; const v = d.verdict || {}, acts = v.actions || [], res = kind === "resolved";
  $("#resT").textContent = res ? "해결 기록" : "오경보로 표시";
  $("#resDlg").classList.toggle("fa", !res); $("#resDlg .dlg-h use").setAttribute("href", res ? "#i-check-circle" : "#i-x-circle");
  $("#resD").textContent = res ? "실제 원인과 한 조치를 남깁니다. 비슷한 사건이 다시 나면 talog 에이전트가 이 기록을 함께 보여 줍니다."
    : "경보가 실제 문제가 아니었다고 표시합니다. 규칙마다 오경보 수를 세어 설정 화면에서 조건 조정을 권합니다.";
  ["#resCauseBox", "#resActBox", "#resCaseBox"].forEach(s => $(s).classList.toggle("hidden", !res));
  $("#resCause").value = res ? (v.cause_name || "").split(" — ")[0] : ""; $("#resAct").value = res && acts.length ? ((d.action_text || {})[acts[0]] || acts[0]) : "";
  $("#resNote").value = ""; $("#resCase").checked = true; S.resKind = kind;
  const g = $("#resDlg"); g.returnValue = ""; g.showModal(); (res ? $("#resCause") : $("#resNote")).focus(); }
$("#resForm").addEventListener("submit", async ev => { ev.preventDefault(); const res = S.resKind === "resolved";
  const ok = await busy($("#resOk"), () => setIncState(S.resKind, {cause: res ? $("#resCause").value : "", action: res ? $("#resAct").value : "",
    note: $("#resNote").value, save_case: res && $("#resCase").checked}));
  if (ok) $("#resDlg").close("ok"); });
$("#resNo").onclick = () => $("#resDlg").close("cancel");
