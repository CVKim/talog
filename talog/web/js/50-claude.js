/* ── Claude (AI 작업 공간) ──────────────────────────────── */
const TOOL_IC = {overview: "dashboard", list_incidents: "siren", get_incident: "siren", capacity_trend: "activity", list_reports: "file-text", get_report: "file-text",
  query_report: "database", runbook: "list", search_cases: "history", rediagnose: "repeat", job_status: "history", start_replay: "history", save_case: "save",
  log_days: "folder-open", log_files: "folder-open", read_log: "file-text", search_logs: "search", trace_inspection: "route", Skill: "sparkles", Read: "file-text", Glob: "search", Write: "pen", Edit: "pen"};
const TOOL_GROUPS = [["원본 로그 (직접 읽기)", ["log_days", "log_files", "read_log", "search_logs", "trace_inspection"]],
  ["사건·처리 여유", ["overview", "list_incidents", "get_incident", "capacity_trend", "rediagnose", "runbook", "search_cases"]],
  ["진단 리포트", ["list_reports", "get_report", "query_report"]], ["재현·사례", ["start_replay", "job_status", "save_case"]]];
async function copyText(t, msg = "복사했습니다") {
  try { await navigator.clipboard.writeText(t); }
  catch (e) { const ta = document.createElement("textarea"); ta.value = t; ta.style.position = "fixed"; ta.style.opacity = "0"; document.body.appendChild(ta); ta.select(); try { document.execCommand("copy"); } catch (e2) {} ta.remove(); }
  toast(msg, "ok"); }
document.addEventListener("click", ev => { const c = ev.target.closest("[data-copy]"); if (c) copyText($("#" + c.dataset.copy).textContent); });
async function ensureClaude() { if (S.claude) return S.claude; try { const c = await api("/api/claude"); if (c && !c.error) { S.claude = c; S.claudeCli = !!c.cli; S.logs = S.logs || c.logs; } } catch (e) {} return S.claude; }
async function loadClaude() {
  let c; try { c = await api("/api/claude"); } catch (e) { toast("Claude 연동 정보를 읽지 못했습니다", "err"); return; }
  if (!c || c.error) { toast("Claude 연동 정보 오류: " + (c && c.error), "err"); return; }
  S.claude = c; S.claudeCli = !!c.cli;
  $("#aiStCli").textContent = c.cli ? `Claude Code ${c.cli_version || ""}`.trim() + (c.cli_login === false ? " · 로그인 필요" : "") : "Claude Code 없음 — 질문 복사만";
  $("#aiStCli").className = "badge " + (c.cli && c.cli_login !== false ? "ok" : "warn");
  $("#btnCa span").textContent = c.cli ? "보내기" : "질문 복사";
  if (!$("#caModel").options.length) { $("#caModel").innerHTML = c.models.map(m => `<option value="${m.v}">${esc(m.label)}</option>`).join(""); $("#caEffort").innerHTML = c.efforts.map(m => `<option value="${m.v}">${esc(m.label)}</option>`).join("");
    try { const pm = localStorage.getItem("talog.claude.model"), pe = localStorage.getItem("talog.claude.effort");
      if (pm != null && c.models.some(m => m.v === pm)) $("#caModel").value = pm; if (pe != null && c.efforts.some(m => m.v === pe)) $("#caEffort").value = pe; } catch (e) {} }
  $("#aiSkills").innerHTML = c.skills.map((k, i) => skillBtn(k, i, true)).join("");
  $$("#aiSkills [data-sk]").forEach(b => b.onclick = () => openSkill(c.skills[+b.dataset.sk]));
  const byName = Object.fromEntries(c.tools.map(t => [t.name, t]));
  $("#aiToolCnt").textContent = `${c.tools.length}개 · 바로 묻기 ${c.tools.filter(t => t.ask).length}개`;
  $("#aiTools").innerHTML = TOOL_GROUPS.map(([g, names]) => `<div class="toolgrp"><b>${g}</b><ul>${names.filter(n => byName[n]).map(n => `<li title="${esc(byName[n].description)}">${ic(TOOL_IC[n] || "wrench", "sm")}<span><span class="mono">${n}</span> <span>${esc(byName[n].title)}${byName[n].ask ? "" : byName[n].card ? " · 비교 카드에서 확인 후" : " · Claude 앱에서"}</span></span></li>`).join("")}</ul></div>`).join("");
  renderLogSel(c.logs);
  $("#cmdCode").textContent = c.claude_code; $("#cmdMcp").textContent = c.mcp_json; $("#cmdDesk").textContent = c.desktop_json;
  $("#deskPath").textContent = c.desktop_path + (c.desktop_exists ? "  (파일 있음 — talog 항목만 붙이기)" : "  (없으면 새로 만듭니다)");
  $("#aiSkillHint").innerHTML = c.skill ? `작업 스킬 ${c.skills.length}개 — 에이전트 폴더의 <span class="mono">.claude\\skills\\talog-*</span>: ${c.skills.map(k => `<span class="mono">${esc(k.command)}</span>`).join(" · ")}` : "";
  if (!$("#wsDir").value) $("#wsDir").value = c.workspace_default;
  $("#wsResult").innerHTML = c.workspace_exists ? `<span class="ok">${ic("check-circle", "sm")} 이미 있습니다</span> <span class="meta">— 다시 만들면 연결·스킬만 새로 하고 CLAUDE.md 는 그대로 둡니다</span>` : "";
  renderThread(c.ask); if (c.ask.state === "running") { clearTimeout(askTimer); askTimer = setTimeout(() => pollAsk(false), 900); } }
function renderLogSel(L) { S.logs = L = L || {};
  $("#logDays").innerHTML = (L.days || []).map(d => `<option value="${esc(d)}">`).join("");
  $("#logSelPath").textContent = L.selected || ""; $("#logSelPath").classList.toggle("hidden", !L.selected);
  $("#btnLogClr").classList.toggle("hidden", !L.selected);
  $("#logSelState").innerHTML = L.selected ? '<span class="badge ok">선택됨</span>' : '<span class="badge plain">고르지 않음</span>';
  if (L.days && L.days[0]) $("#logSel").placeholder = L.days[0];
  renderCtx(); }
$("#btnLogSel").onclick = () => busy($("#btnLogSel"), async () => { const p = $("#logSel").value.trim() || (S.logs && S.logs.days && S.logs.days[0]) || "";
  if (!p) { toast("로그 폴더를 넣으십시오", "err"); return; }
  const r = await api("/api/claude/log", {path: p}); if (!r.ok) { toast(r.error, "err"); return; } $("#logSel").value = ""; renderLogSel(r); toast("Claude 가 읽을 로그 폴더를 골랐습니다", "ok"); });
$("#btnLogClr").onclick = async () => { const r = await api("/api/claude/log", {path: ""}); if (r.ok) renderLogSel(r); };
function renderCtx(st) { st = st || S.ask || {}; const L = S.logs || {};
  const chips = [];
  if (st.can_continue) chips.push(`<span class="ctxchip brand">${ic("message", "sm")}<span>이어서 묻기 — 앞 대화를 기억합니다</span><button type="button" data-ctx="new" aria-label="새 대화로 시작" title="새 대화로">${ic("x", "sm")}</button></span>`);
  if (L.selected) chips.push(`<span class="ctxchip" title="${esc(L.selected)}">${ic("folder-open", "sm")}<span>원본 로그 ${esc(tail2(L.selected))}</span><button type="button" data-ctx="log" aria-label="로그 폴더 해제" title="로그 폴더 해제">${ic("x", "sm")}</button></span>`);
  $("#caCtx").innerHTML = chips.join(""); $("#caCtx").classList.toggle("hidden", !chips.length);
  const nb = $("#caCtx [data-ctx=new]"); if (nb) nb.onclick = newThread; const lb = $("#caCtx [data-ctx=log]"); if (lb) lb.onclick = () => $("#btnLogClr").click(); }
/* 짧은 마크다운 → HTML (제목·목록·굵게·코드·코드 블록·표·구분선). 먼저 이스케이프한 뒤 꾸민다 */
const mdInline = x => esc(x).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/(^|[\s(])\*([^*\s][^*]*?)\*(?=[\s).,:;!?]|$)/g, "$1<em>$2</em>");
function mdHtml(t) { const inline = mdInline;
  const out = []; let list = null, tbl = [], code = null;
  const fl = () => { if (list) { out.push(`<${list.t}>${list.items.map(i => `<li>${inline(i)}</li>`).join("")}</${list.t}>`); list = null; } };
  const ft = () => { if (tbl.length) { const rows = tbl.filter(r => !/^\s*\|?\s*:?-{2,}/.test(r)).map(r => r.trim().replace(/^\||\|$/g, "").split("|"));
    out.push(`<div class="tbl" style="max-height:none"><table>${rows.map((cs, i) => `<tr>${cs.map(x => i ? `<td>${inline(x.trim())}</td>` : `<th>${inline(x.trim())}</th>`).join("")}</tr>`).join("")}</table></div>`); tbl = []; } };
  for (const ln of String(t || "").replace(/\r/g, "").split("\n")) { let m;
    if (code !== null) { if (/^\s*```/.test(ln)) { out.push(`<pre>${esc(code.join("\n"))}</pre>`); code = null; } else code.push(ln); continue; }
    if (/^\s*```/.test(ln)) { fl(); ft(); code = []; continue; }
    if (/^\s*\|.*\|\s*$/.test(ln)) { fl(); tbl.push(ln); continue; } ft();
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(ln)) { fl(); out.push("<hr>"); }
    else if ((m = ln.match(/^\s*#{1,6}\s+(.*)/))) { fl(); out.push(`<h4>${inline(m[1])}</h4>`); }
    else if ((m = ln.match(/^\s*[-*]\s+(.*)/))) { if (!list || list.t !== "ul") { fl(); list = {t: "ul", items: []}; } list.items.push(m[1]); }
    else if ((m = ln.match(/^\s*\d+[.)]\s+(.*)/))) { if (!list || list.t !== "ol") { fl(); list = {t: "ol", items: []}; } list.items.push(m[1]); }
    else if (!ln.trim()) fl(); else { fl(); out.push(`<p>${inline(ln)}</p>`); } }
  if (code !== null) out.push(`<pre>${esc(code.join("\n"))}</pre>`);
  fl(); ft(); return out.join(""); }
let askTimer = null;
function setAskRunning(on) { $(".comp").classList.toggle("running", on); $("#btnCa").disabled = on; $("#btnCaStop").classList.toggle("hidden", !on); $("#btnNewThread").disabled = on; $$("#aiSkills [data-sk], #caThread .ai-empty [data-sk], #caThread .ai-empty [data-quick]").forEach(b => b.disabled = on); }
