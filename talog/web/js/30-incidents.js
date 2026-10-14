/* ── 사건 ──────────────────────────────────────────────── */
async function loadInc() { if (!S.incRows) $("#incRows").innerHTML = `<div style="padding:8px">${Array.from({length: 5}, () => '<span class="sk"></span><span class="sk sm" style="width:60%;margin-bottom:16px"></span>').join("")}</div>`;
  const r = await api(`/api/incidents?days=${$("#incDays").value}&replay=${$("#incReplay").checked ? 1 : 0}`);
  S.incRows = r.rows; renderIncList(); }
function renderIncList() {
  const rows = S.incRows || [], q = S.incQ.toLowerCase();
  const cnt = {"": rows.length, crit: 0, warn: 0, info: 0}; rows.forEach(r => { if (cnt[r.severity] != null) cnt[r.severity]++; });
  $$("#incSev button").forEach(b => { $("b", b).textContent = cnt[b.dataset.sev] ?? 0; b.setAttribute("aria-pressed", b.dataset.sev === S.incSev); });
  const vis = rows.filter(r => (!S.incSev || r.severity === S.incSev) && (!S.incOpen || OPEN(r)) && (!q || `${r.title} ${r.cause} ${r.id}`.toLowerCase().includes(q)));
  $("#incCount").textContent = `${vis.length}건`;
  if (!vis.length) { $("#incRows").innerHTML = emptyBox(rows.length ? "맞는 사건이 없습니다" : "기록 없음", rows.length ? "찾는 말·심각도·'마무리 안 된 것만'을 바꿔 보십시오." : "기간을 넓히거나 '재현 포함'을 켜 보십시오.", "search"); return; }
  const groups = []; vis.forEach(r => { const k = r.day + (r.replay ? "r" : ""); let g = groups.find(x => x.k === k); if (!g) groups.push(g = {k, day: r.day, rep: r.replay, items: []}); g.items.push(r); });
  $("#incRows").innerHTML = groups.map(g => `<div class="day-h">${esc(dayLabel(g.day))}${g.rep ? '<span class="tag">재현</span>' : ""}<span class="spacer"></span>${g.items.length}건</div>`
    + g.items.map(x => `<button type="button" class="li-item" data-id="${esc(x.id)}" data-rp="${x.replay ? 1 : 0}" ${S.inc && S.inc.id === x.id ? 'aria-current="true"' : ""}><div class="li">${sevTag(x.severity)}<b>${esc(x.title)}${x.n_alerts > 1 ? ` <span class="meta">+${x.n_alerts - 1}</span>` : ""}</b><span class="when">${esc(x.time)}</span><div class="li-sub">${esc(x.cause || "-")}</div><div class="li-meta">${stateTag(x.state)}${gateTag(x.gate, x.mode)}<span>메일 ${esc(x.mail)}</span></div></div></button>`).join("")).join("");
  $$("#incRows .li-item").forEach(b => b.onclick = () => openInc(b.dataset.id, b.dataset.rp === "1")); }
$("#incDays").onchange = loadInc; $("#incReplay").onchange = loadInc;
$("#incOpen").onchange = ev => { S.incOpen = ev.target.checked; renderIncList(); };
$("#btnIncRefresh").onclick = () => busy($("#btnIncRefresh"), loadInc);
$("#incSearch").oninput = ev => { S.incQ = ev.target.value.trim(); renderIncList(); };
$$("#incSev button").forEach(b => b.onclick = () => { S.incSev = b.dataset.sev; renderIncList(); });
/** 판단 확신 — 규칙·로컬 AI 판단 관계를 쉬운 말로 (ux #113: 핵심 뜻은 줄이지 않는다) */
function gateExplain(mode, label) {
  if ((label || "").startsWith("오경보")) return "오경보로 보입니다 — 근거 탭에서 수치를 확인하십시오.";
  if (mode === "agree") return "규칙과 로컬 AI 가 같은 원인을 골랐습니다.";
  if (mode === "disagree") return "규칙과 로컬 AI 의 원인이 다릅니다 — 근거 탭에서 두 의견을 비교하십시오.";
  if (mode === "llm_only") return "규칙이 원인을 정하지 못해 로컬 AI 의견만 있습니다 — 근거를 직접 확인하십시오.";
  return "규칙 진단만 했습니다 (로컬 AI 교차 확인 없음).";
}
/** 사건 요약 네 칸 — 무슨 일 · 왜 · 지금 할 일 · 판단 확신 */
function incSummary(d, title) {
  const v = d.verdict || {}, acts = v.actions || [];
  const [causeShort, causeLong] = (v.cause_name || "-").split(" — ");
  const first = acts.length ? ((d.action_text || {})[acts[0]] || acts[0]) : "권고 조치 없음 — 근거를 보고 판단하십시오";
  const roles = (v.notify || []).map(r => (d.role_text || {})[r] || r).join(", ");
  const cell = (cls, icon, k, b, sub, tip = "") => `<div class="isum-i ${cls}"><span class="k">${ic(icon, "sm")}${k}${tip}</span><b>${esc(b)}</b>${sub ? `<span class="d">${esc(sub)}</span>` : ""}</div>`;
  return `<div class="isum">${cell("", "siren", "무슨 일", title, `${d.opened || ""}${d.closed ? " – " + d.closed : ""} · ${d.family_title || d.family || ""}`, term("incident"))}
    ${cell("", "search", "왜 (원인)", causeShort, causeLong || "", term("rule_diag"))}
    ${cell("now", "wrench", "지금 할 일", first, acts.length > 1 ? `권고 조치 ${acts.length}개 중 1번${roles ? " · 담당 " + roles : ""}` : (roles ? "담당 " + roles : ""))}
    ${cell("", gateCls(v.mode, v.label) === "agree" ? "check-circle" : "alert", "판단 확신", v.label || "-", gateExplain(v.mode, v.label), term("gate"))}</div>`;
}
async function openInc(id, rp) {
  const mark = () => $$("#incRows .li-item").forEach(b => { if (b.dataset.id === id) b.setAttribute("aria-current", "true"); else b.removeAttribute("aria-current"); });
  mark(); $("#incDetail").className = "card dcard";
  $("#incDetail").innerHTML = `<span class="sk lg"></span><span class="sk" style="width:40%;margin-top:12px"></span><span class="sk" style="width:100%;height:84px;margin-top:18px"></span><span class="sk" style="width:90%;margin-top:18px"></span><span class="sk" style="width:70%"></span>`;
  const d = await api(`/api/incident?id=${encodeURIComponent(id)}&replay=${rp ? 1 : 0}`);
  if (!d || d.error) { $("#incDetail").innerHTML = emptyBox("사건을 찾을 수 없습니다", esc(id), "alert"); return; }
  S.inc = {id, rp, d, qa: []}; mark();
  history.replaceState(null, "", `#inc/${encodeURIComponent(id)}${rp ? "/replay" : ""}`);
  const v = d.verdict || {}, rule = d.rule || {}, llm = d.llm || {}, ev = d.evidence || {};
  const acts = (v.actions || []).map(a => `<li>${esc((d.action_text || {})[a] || a)}</li>`).join("");
  const roles = (v.notify || []).map(r => (d.role_text || {})[r] || r).join(", ");
  const em = d.email || {}, fu = d.email_followup, gc = gateCls(v.mode, v.label), title = (d.alerts[0] || {}).title || id;
  $("#incDetail").className = "card dcard " + esc(v.severity || "");
  $("#incDetail").innerHTML = `<div class="dhead"><h2>${esc(title)}</h2><div class="dact">${isDev() ? `<button class="btn sm primary" type="button" id="btnIncClaude">${ic("sparkles")}Claude 로 분석</button>${d.day_dir ? `<button class="btn sm" type="button" id="btnIncLog" title="${esc(d.day_dir)}">${ic("file-search")}원본 로그로 확인</button>` : ""}` : ""}</div></div>
    <div class="dmeta">${sevTag(v.severity)}<span>${esc(d.site || "")}</span><span class="num">${esc(d.opened)} – ${esc(d.closed || "")}</span><span>${esc(d.family_title || d.family)}</span><span class="mono">${esc(d.id)}</span>${rp ? '<span class="tag">재현</span>' : ""}</div>
    ${stateBar(d)}
    <div class="tabs" role="tablist" aria-label="사건 내용">${[["sum", "판단"], ["trace", "처리 과정"], ["ev", "근거"], ["al", `경보 <span class="tag">${d.alerts.length}</span>`], ["mail", "메일"], ["qa", "질문"]].map(([k, l], i) => `<button type="button" role="tab" id="tab-${k}" aria-controls="pn-${k}" aria-selected="${i === 0}" tabindex="${i === 0 ? 0 : -1}" data-tab="${k}">${l}</button>`).join("")}</div>
    <div role="tabpanel" id="pn-sum" aria-labelledby="tab-sum">${incSummary(d, title)}<div class="verdict ${gc}"><div class="vrow"><span class="gate ${gc}">${ic(gc === "agree" ? "check-circle" : gc === "warn" ? "alert" : "info", "sm")}${esc(v.label || "")}</span><b>${esc(v.cause_name || "")}</b>${v.llm_cause_name && v.llm_cause !== v.cause ? `<span class="meta">/ LLM: ${esc(v.llm_cause_name)}</span>` : ""}</div><p>${esc(v.message || "")}</p></div>
      <div class="grid g2 dgrid"><section><h3>${ic("wrench")}권고 조치</h3><ol class="acts">${acts || "<li>-</li>"}</ol><div class="sub" style="margin-top:8px">${ic("user", "sm")} 담당: ${esc(roles || "-")}</div></section>
        <section><h3>${ic("list")}핵심 사실</h3><ul class="facts">${(ev.key_facts || []).slice(0, 8).map(f => `<li>${esc(f)}</li>`).join("") || "<li>-</li>"}</ul></section></div>${incSimilar(d)}${incNotes(d)}${traceLine(d)}</div>
    <div role="tabpanel" id="pn-trace" aria-labelledby="tab-trace" hidden>${incTrace(d)}</div>
    <div role="tabpanel" id="pn-ev" aria-labelledby="tab-ev" hidden><div class="grid g2 dgrid"><section><h3>${ic("check")}룰 진단 근거</h3><ul class="facts small">${(rule.facts || []).map(f => `<li>${esc(f)}</li>`).join("") || "<li>-</li>"}</ul></section>
      <section><h3>${ic("cpu")}LLM 2차 의견 ${llm.ok ? `<span class="meta">${esc(llm.model)} · ${esc(llm.device)} · ${llm.wall_s}초</span>` : ""}</h3>
      ${llm.ok ? `<div><b>${esc(llm.root_cause || "")}</b></div><ul class="facts small">${(llm.evidence || []).map(x => `<li>${esc(x)}</li>`).join("")}</ul>${(llm.fact && !llm.fact.ok) ? `<div class="note warn">${ic("alert")}<span>근거에 없는 수치 ${esc(llm.fact.unsupported.join(", "))} — 메일에는 룰 문구를 썼습니다</span></div>` : ""}<details class="disclosure"><summary>LLM 분석 서술</summary><div class="small" style="white-space:pre-wrap;line-height:1.6">${esc(llm.analysis || "")}</div></details>` : (d.llm ? `<div class="note crit">${ic("x-circle")}<span>응답 실패: ${esc(llm.error)}</span></div>` : '<div class="meta">LLM 미사용 (룰 진단만)</div>')}</section></div></div>
    <div role="tabpanel" id="pn-al" aria-labelledby="tab-al" hidden><ol class="tl">${d.alerts.map(a => `<li class="${esc(a.severity)}"><span class="tt">${esc(a.time)}</span><span class="dot"></span><div><div class="row" style="gap:6px">${sevTag(a.severity)}<b>${esc(a.title)}</b></div><div class="sub">${esc(a.evidence)}</div></div></li>`).join("")}</ol>
      <details class="disclosure"><summary>근거 로그 ${(ev.log_lines || []).length}줄</summary><pre class="log">${esc((ev.log_lines || []).join("\n"))}</pre></details></div>
    <div role="tabpanel" id="pn-mail" aria-labelledby="tab-mail" hidden><div class="row" style="margin-bottom:10px">${em.sent ? '<span class="tag on">발송</span>' : em.eml ? '<span class="tag">보관</span>' : '<span class="tag">없음</span>'}${fu ? `<button class="btn sm" type="button" id="mFu">후속 메일 보기</button>` : ""}</div>
      ${em.eml || em.sent ? `<iframe class="mail" id="mFrame" title="사건 메일 미리보기" src="/api/mail?id=${encodeURIComponent(id)}&replay=${rp ? 1 : 0}"></iframe>` : '<div class="meta">메일을 보내지 않은 사건입니다 (등급 기준 미만이거나 메일 꺼짐)</div>'}</div>
    <div role="tabpanel" id="pn-qa" aria-labelledby="tab-qa" hidden><p class="hint" style="margin:0 0 8px">사건 기록(근거·판단)만으로 로컬 LLM 이 답합니다.${isDev() ? " 원본 로그까지 보는 깊은 분석은 <b>Claude 로 분석</b>을 쓰십시오." : ""}</p>
      <div class="row"><label class="vh" for="iaQ">이 사건에 할 질문</label><input type="text" id="iaQ" style="flex:1;min-width:200px" placeholder="예: 왜 룰과 LLM 판단이 달랐나?"><button class="btn" type="button" id="btnIa">${ic("send")}질문</button><span id="iaBusy" class="meta" aria-live="polite"></span></div><div id="iaList"></div></div>`;
  const tabs = $$("#incDetail [role=tab]");
  const sel = t => { tabs.forEach(b => { const on = b === t; b.setAttribute("aria-selected", on); b.tabIndex = on ? 0 : -1; $("#" + b.getAttribute("aria-controls")).hidden = !on; }); };
  tabs.forEach((t, i) => { t.onclick = () => sel(t); t.onkeydown = e => { const k = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0; if (k) { e.preventDefault(); const n = tabs[(i + k + tabs.length) % tabs.length]; sel(n); n.focus(); } }; });
  $$("#incDetail [data-st]").forEach(b => b.onclick = () => stateClick(b.dataset.st));
  $$("#incDetail [data-gotab]").forEach(b => b.onclick = () => { const t = $("#tab-" + b.dataset.gotab); if (t) { t.click(); t.focus(); } });
  hydrateTerms($("#incDetail"));
  if (fu) $("#mFu").onclick = () => { $("#mFrame").src = `/api/mail?id=${encodeURIComponent(id)}&replay=${rp ? 1 : 0}&which=followup`; };
  if (isDev()) $("#btnIncClaude").onclick = () => busy($("#btnIncClaude"), () => runSkill("rca", {incident: id}));     // 사건 RCA 스킬 (읽기만)
  if (isDev() && d.day_dir) $("#btnIncLog").onclick = () => busy($("#btnIncLog"), () => runSkill("logs", {day: d.day_dir, time: ((String(d.opened || "").match(/\d{1,2}:\d{2}(:\d{2})?/) || [""])[0]), symptom: `${title} (talog 사건 ${id})`}));
  const ask = async () => { const q = $("#iaQ").value.trim(); if (!q) return; $("#iaBusy").textContent = "답을 만드는 중… (CPU 는 1~2분)";
    const r = await busy($("#btnIa"), () => api("/api/ask/incident", {id, replay: rp, q})); $("#iaBusy").textContent = "";
    if (!r || !r.ok) { toast("질문 실패: " + ((r && r.error) || ""), "err"); return; } $("#iaQ").value = "";
    S.inc.qa.unshift({q, ...r}); $("#iaList").innerHTML = S.inc.qa.map(x => `<div class="qa"><div class="q">${ic("message", "sm")}<span>${esc(x.q)}</span></div><div class="a">${esc(x.answer)}</div><div class="sub">${esc(x.model)} · ${esc(x.device)} · ${x.wall_s}초</div></div>`).join(""); };
  $("#btnIa").onclick = ask; $("#iaQ").onkeydown = ev => { if (ev.key === "Enter" && !ev.isComposing) ask(); };
  if (window.innerWidth < 1200) $("#incDetail").scrollIntoView({behavior: reduceMotion() ? "auto" : "smooth", block: "start"});
}

