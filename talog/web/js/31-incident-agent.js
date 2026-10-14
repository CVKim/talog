/* ── 사건 — 에이전트 처리 과정 · 비슷한 과거 사례 · 사람의 마무리 ────────────────────────────
   처리 과정: 사건 기록(경보·근거·룰 진단·과거 사례·로컬 AI·수치 대조·합의·메일·사람의 확인)으로 에이전트가 거친 단계를
   그대로 보여 준다 — 새로 계산하거나 지어내지 않는다. 마무리: 확인 / 해결 기록(원인·한 조치·메모, 사례 KB 저장) /
   오경보 / 다시 열기 (console/feedback.py). 해결 기록은 이 설비의 사례 KB 에 남아 다음 비슷한 사건에 '비슷한 과거 사례'로 붙는다. */
const ISTATE = {open: ["새 사건", "info", "siren"], ack: ["확인 중", "warn", "user"], resolved: ["해결", "ok", "check-circle"], false_alarm: ["오경보", "", "x-circle"]};
const stateTag = s => { const [l, c, i] = ISTATE[s] || ISTATE.open; return `<span class="st-chip ${c}">${ic(i, "sm")}${l}</span>`; };

/** 사건 상태 줄 — 지금 상태 + 할 수 있는 다음 표시 */
function stateBar(d) { const s = d.state || "open";
  const b = (k, icon, label, primary) => `<button class="btn sm${primary ? " primary" : ""}" type="button" data-st="${k}">${ic(icon)}${label}</button>`;
  const acts = s === "open" ? b("ack", "user", "확인") + b("resolved", "check-circle", "해결 기록", true) + b("false_alarm", "x-circle", "오경보")
    : s === "ack" ? b("resolved", "check-circle", "해결 기록", true) + b("false_alarm", "x-circle", "오경보") : b("open", "undo", "다시 열기");
  return `<div class="dstate"><span class="k">사건 상태${term("resolve")}</span>${stateTag(s)}<span class="spacer"></span>${acts}</div>`; }

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
    : ["tr-skip", "user", "사람의 확인", "아직 없음 — 위 '해결 기록'으로 마무리하면 다음 판단의 근거가 됩니다"]);
  return out; }
function incTrace(d) { const st = traceSteps(d);
  return `<p class="hint" style="margin:0 0 12px">talog 에이전트가 이 사건을 처리한 순서입니다. 모두 사건 기록에 남은 값입니다.</p>
    <ol class="trace">${st.map(([c, i, t, x], k) => `<li class="${c}"><span class="tn">${ic(i, "sm")}</span><div><b>${k + 1}. ${esc(t)}<span class="vh"> — ${c === "ok" ? "완료" : c === "warn" ? "확인 필요" : c === "crit" ? "실패" : c === "tr-skip" ? "건너뜀" : ""}</span></b><span>${esc(x)}</span></div></li>`).join("")}</ol>`; }
/** 판단 탭 맨 아래 — 처리 과정 한 줄 요약(탭으로 이동) */
function traceLine(d) { const st = traceSteps(d), warn = st.filter(s => s[0] === "warn" || s[0] === "crit").length;
  return `<button class="trace-line" type="button" data-gotab="trace">${ic("route", "sm")}<span>에이전트가 이 사건을 <b>${st.length}단계</b>로 처리했습니다${warn ? ` — 확인할 단계 ${warn}개` : ""}</span><span class="go">처리 과정 보기${ic("chevron-right", "sm")}</span></button>`; }

function incSimilar(d) { const sim = d.similar || []; if (!sim.length) return "";
  return `<section class="simcases"><h3>${ic("book")}비슷한 과거 사례<span data-tip="casekb"></span><span class="meta">사례 KB · 이 설비의 해결 기록 포함</span></h3><ul>${sim.map(s => `<li><div class="sc-h"><b>${esc(s.title)}</b><span class="tag">${Math.round((s.score || 0) * 100)}%</span></div>
    <div class="sub">원인: ${esc(s.cause || "-")}</div><div class="sub">조치: ${esc(s.action || "-")}</div>${s.site || s.date ? `<div class="meta">${esc([s.site, s.date].filter(Boolean).join(" · "))}</div>` : ""}</li>`).join("")}</ul></section>`; }
function incNotes(d) { const ns = d.notes || []; if (!ns.length) return "";
  return `<section class="notes"><h3>${ic("user")}사람의 기록</h3><ol class="nl">${ns.slice().reverse().slice(0, 5).map(n => `<li><div class="sc-h">${stateTag(n.state)}<span class="meta">${esc(n.time)}${n.mode && typeof MODE_INFO !== "undefined" && MODE_INFO[n.mode] ? ` · ${esc(MODE_INFO[n.mode].label)}` : ""}</span></div>
    ${n.cause ? `<div class="sub">원인: ${esc(n.cause)}</div>` : ""}${n.action ? `<div class="sub">조치: ${esc(n.action)}</div>` : ""}${n.note ? `<div class="sub">메모: ${esc(n.note)}</div>` : ""}${n.case ? `<div class="meta">${ic("book", "sm")}사례 KB 에 저장 — 다음 비슷한 사건에 함께 보입니다</div>` : ""}</li>`).join("")}</ol></section>`; }

/* 마무리 표시 — 확인·다시 열기는 바로, 해결 기록·오경보는 대화상자에서 */
async function setIncState(state, extra = {}) { const c = S.inc; if (!c) return false;
  const r = await api("/api/incident/state", {id: c.id, replay: !!c.rp, state, ...extra});
  if (!r || !r.ok) { toast((r && r.error) || "저장하지 못했습니다", "err"); return false; }
  toast(state === "resolved" ? (r.case ? "해결 기록을 남겼습니다 — 사례 KB 에 저장해 다음 비슷한 사건에 함께 보여 줍니다" : "해결 기록을 남겼습니다")
    : state === "false_alarm" ? "오경보로 표시했습니다 — 설정의 규칙 피드백에 반영됩니다" : state === "ack" ? "확인 중으로 표시했습니다" : "다시 열었습니다", "ok");
  await openInc(c.id, c.rp); if (S.incRows) loadInc(); loadBrief(); return true; }
function stateClick(k) { if (k === "resolved" || k === "false_alarm") resDlg(k); else setIncState(k); }
function resDlg(kind) { const d = S.inc && S.inc.d; if (!d) return; const v = d.verdict || {}, acts = v.actions || [], res = kind === "resolved";
  $("#resT").textContent = res ? "해결 기록" : "오경보로 표시";
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
