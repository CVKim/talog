/* ── 작업 스킬 카드: 누르면 필요한 값만 묻고 '/스킬 인자'로 새 대화를 연다 ── */
const skillBtn = (k, i, side) => `<button type="button" class="act${side ? " skc" : ""}" data-sk="${i}"${side ? ` title="${esc(k.desc)}"` : ""}><span class="ai-ic">${ic(k.icon)}</span><div><b>${esc(k.title)}</b><span class="d">${esc(k.desc)}</span><span class="skn">${esc(k.command)}</span></div></button>`;
let skCur = null;
function skField(f, v) { const id = "sk_" + f.name, val = v != null && v !== "" ? v : (f.default || "");
  const lab = `<label for="${id}">${esc(f.label)}${f.hint && f.kind !== "confirm" ? ` <span class="opt">(${esc(f.hint)})</span>` : ""}</label>`;
  if (f.kind === "incident") return `<div class="field">${lab}<select id="${id}" data-f="${f.name}"><option value="">가장 최근 심각 사건</option>${(f.choices || []).map(o => `<option value="${esc(o.v)}"${o.v === val ? " selected" : ""}>${esc((SEVK[o.severity] ? `[${SEVK[o.severity]}] ` : "") + o.label)}</option>`).join("")}</select></div>`;
  if (f.kind === "day") return `<div class="field">${lab}<select id="${id}" data-f="${f.name}"><option value="">사건이 있는 가장 최근 날</option>${(f.choices || []).map(o => `<option value="${esc(o.v)}"${o.v === val ? " selected" : ""}>${esc(o.label)}</option>`).join("")}</select></div>`;
  if (f.kind === "logday") { const ch = f.choices || [];
    return `<div class="field">${lab}<input type="text" id="${id}" data-f="${f.name}" list="${id}_l" value="${esc(val)}" placeholder="${ch.length ? "일자 폴더 경로" : "일자 폴더 경로 — 설정의 로그 루트에 폴더가 없습니다"}" spellcheck="false"><datalist id="${id}_l">${ch.map(o => `<option value="${esc(o.v)}">${esc(o.label)}</option>`).join("")}</datalist><p class="hint">${f.root_only ? "재현하려면 설정의 로그 루트 아래 일자 폴더여야 합니다. " : ""}Claude 에게는 경로 대신 '선택'으로만 알립니다.</p></div>`; }
  if (f.kind === "confirm") return `<div class="confirm" id="${id}_box"><label class="switch"><input type="checkbox" id="${id}" data-f="${f.name}"${val === true ? " checked" : ""}><span>${esc(f.label)}</span></label><p class="hint">${esc(f.hint || "")}</p></div>`;
  const pat = f.kind === "time" ? ` pattern="\\d{1,2}:\\d{2}(:\\d{2})?" inputmode="numeric" title="HH:MM 또는 HH:MM:SS"` : "";
  return `<div class="field">${lab}<input type="text" id="${id}" data-f="${f.name}" value="${esc(val)}" placeholder="${esc(f.placeholder || "")}"${pat} maxlength="160"></div>`; }
function skRunLine() { const sel = s => { const o = $(s).selectedOptions[0]; return o ? o.textContent.split(" — ")[0] : "기본"; };
  const rp = $("#sk_replay"), on = !!(rp && rp.checked);
  if (rp) { $("#sk_replay_box").classList.toggle("on", on); $("#skOk span").textContent = on ? "재현 포함 실행" : "실행"; }
  $("#skRun").innerHTML = `<span class="mono">${esc(skCur.command)}</span> 스킬을 새 대화로 실행합니다 · 모델 ${esc(sel("#caModel"))} · effort ${esc(sel("#caEffort"))}${on ? " · <b>이 질문에서만 경보 재현 허용</b>" : ""}`; }
async function openSkill(k, preset = {}) { if (!k) return; await ensureClaude();
  if (S.ask && S.ask.state === "running") { toast("Claude 가 답하는 중입니다 — 끝난 뒤 실행하십시오", "err"); return; }
  skCur = k; $("#skIc").innerHTML = ic(k.icon); $("#skT").textContent = k.title; $("#skD").textContent = k.desc;
  $("#skFields").innerHTML = k.fields.map(f => skField(f, preset[f.name])).join(""); $("#skOk span").textContent = "실행";
  const rp = $("#sk_replay"); if (rp) rp.onchange = skRunLine; skRunLine();
  const d = $("#skDlg"); d.returnValue = ""; d.showModal(); const f1 = $("#skFields [data-f]"); if (f1) f1.focus(); }
$("#skNo").onclick = () => $("#skDlg").close("cancel");
$("#skDlg").onclose = async () => { const d = $("#skDlg"), k = skCur; if (d.returnValue !== "ok" || !k) return;
  const args = {}; let replay = false;
  $$("#skFields [data-f]").forEach(el => { if (el.type === "checkbox") { if (el.dataset.f === "replay") replay = el.checked; } else args[el.dataset.f] = el.value.trim(); });
  if (replay && !(await confirmBox({title: "그날 로그를 재현할까요?", body: "지금 규칙으로 하루치를 다시 돌려 경보·사건을 만듭니다. 작업 폴더에 결과 파일을 쓰고 수 분~수십 분 걸리며, 메일은 보내지 않습니다.\nClaude 는 이 질문에서만 재현 도구를 쓸 수 있습니다.", ok: "재현 포함 실행"}))) return;
  runSkill(k, args, replay); };
async function runSkill(k, args = {}, replay = false) { await ensureClaude();
  if (typeof k === "string") k = ((S.claude && S.claude.skills) || []).find(x => x.key === k); if (!k) return;
  if (!S.claudeCli) { copyText(`${k.command} ${Object.values(args).filter(Boolean).join(" ")}`.trim(), "이 PC 에 Claude Code 가 없어 스킬 명령을 복사했습니다 — 에이전트 폴더를 연 Claude 앱에 붙여 넣으세요"); return; }
  if (S.view !== "ai") go("ai", {focus: false});
  const r = await api("/api/claude/ask", {skill: k.key, args, allow_replay: !!replay, model: $("#caModel").value || "", effort: $("#caEffort").value || ""});
  if (!r || !r.ok) { toast((r && r.error) || "실행하지 못했습니다", "err"); return; }
  S.openK = {}; setAskRunning(true); pollAsk(true); }
async function askClaude(q, {fresh = false} = {}) { await ensureClaude();
  if (!S.claudeCli) { copyText(q, "이 PC 에 Claude Code 가 없어 질문을 복사했습니다 — Claude 앱에 붙여 넣으세요"); return; }
  if (S.view !== "ai") go("ai", {focus: false});
  const model = $("#caModel").value || "", effort = $("#caEffort").value || "";
  const r = await api("/api/claude/ask", {q, model, effort, new: fresh});
  if (!r || !r.ok) { toast((r && r.error) || "보내지 못했습니다", "err"); return; }
  $("#caQ").value = ""; autoGrow(); setAskRunning(true); pollAsk(true); }
async function pollAsk(scroll) { let j; try { j = await api("/api/claude/ask"); } catch (e) { clearTimeout(askTimer); askTimer = setTimeout(() => pollAsk(false), 3000); return; }
  const near = window.innerHeight + window.scrollY >= document.body.scrollHeight - 220;
  const was = S.ask && S.ask.state; renderThread(j); clearTimeout(askTimer);
  if (j.state === "running") askTimer = setTimeout(() => pollAsk(false), 900);
  else if (was === "running") { const t = (j.turns || []).slice(-1)[0] || {}; $("#caAnnounce").textContent = t.state === "done" ? "Claude 가 답을 마쳤습니다" : t.state === "cancelled" ? "분석을 멈췄습니다" : "Claude 분석이 실패했습니다"; if (S.view === "ai") loadClaude(); }
  if (S.view === "ai" && (scroll || near)) window.scrollTo({top: document.body.scrollHeight, behavior: scroll && !reduceMotion() ? "smooth" : "auto"}); }
function usageBadge(u) { const el = $("#aiUsage");
  if (!u || u.utilization == null) { $("#aiUsageTxt").textContent = "사용량: 첫 질문 뒤 표시"; $("#aiUsageBar").style.width = "0"; el.className = "usage"; return; }
  const pct = Math.round(u.utilization * 100), typ = {five_hour: "5시간", seven_day: "7일", seven_day_opus: "7일·Opus", seven_day_sonnet: "7일·Sonnet"}[u.type] || u.type || "";
  const rs = u.resets_at ? new Date(u.resets_at * 1000) : null;
  $("#aiUsageTxt").textContent = `구독 사용량 ${typ} ${pct}%${rs ? ` · ${rs.getMonth() + 1}/${rs.getDate()} ${String(rs.getHours()).padStart(2, "0")}:${String(rs.getMinutes()).padStart(2, "0")} 초기화` : ""}${u.overage ? " · 초과 사용 중" : ""}`;
  $("#aiUsageBar").style.width = Math.min(100, pct) + "%"; el.className = "usage " + (pct >= 90 ? "c" : pct >= 70 ? "w" : "");
  el.title = `구독 사용량 — 마지막 질문 때 Claude Code 가 알려 준 값 (${hhmmss(new Date((u.at || Date.now() / 1000) * 1000))})${pct >= 90 ? ". 거의 다 썼습니다 — Sonnet·낮은 effort 를 권합니다" : ""}`; }
const fmtMs = s => s.ms != null ? (s.ms >= 1000 ? (s.ms / 1000).toFixed(1) + "초" : s.ms + "ms") : `${s.t}초`;
function stepRow(s, running, k) {
  if (s.kind === "note") return `<div class="step memo"><span class="si">${ic("message", "sm")}</span><div class="sl">${mdInline(s.label)}</div></div>`;
  const live = !s.done && running, cls = s.error ? "err" : live ? "run" : "";
  const icon = live ? ic("loader", "spin sm") : ic(s.error ? "x-circle" : TOOL_IC[s.tool] || "wrench", "sm");
  return `<div class="step ${cls}"><span class="si">${icon}</span><div class="sl"><b>${esc(s.label)}</b>${s.what ? `<span class="sw">${esc(s.what)}</span>` : ""}${s.preview ? `<details data-k="${k}" ${openAttr(k, false)}><summary>${ic("chevron-right", "sm")}talog 가 돌려준 결과</summary><pre>${esc(s.preview)}</pre></details>` : ""}</div><span class="sms">${live ? "읽는 중" : fmtMs(s)}</span></div>`; }
function turnHtml(t, i, last) {
  const running = t.state === "running", tools = (t.steps || []).filter(s => s.kind !== "note").length;
  const auth = t.auth === "none" ? '<span class="badge ok">구독</span>' : t.auth ? `<span class="badge warn">API 과금 계정 (${esc(t.auth)})</span>` : "";
  const head = `<b>Claude</b>${t.model ? `<span class="badge plain">${esc(t.model)}</span>` : t.model_req ? `<span class="badge plain">${esc(t.model_req)}</span>` : ""}${t.effort ? `<span class="badge plain">effort ${esc(t.effort)}</span>` : ""}${t.resumed ? '<span class="badge brand plain">이어서</span>' : ""}${auth}<span class="num">${dur(t.elapsed_s || 0)}</span>${t.turns ? `<span>· ${t.turns}턴</span>` : ""}${t.denied && t.denied.length ? `<span class="badge warn" title="${esc(t.denied.join(", "))}">막힌 동작 ${t.denied.length}</span>` : ""}`;
  const sk = `s${i}`, st = t.steps || [];
  const steps = st.length ? `<details class="ai-steps" data-k="${sk}" ${openAttr(sk, running)}><summary>${ic(running ? "loader" : "wrench", running ? "spin sm" : "sm")}${running ? "도구로 확인하는 중" : "확인한 과정"} · 도구 ${tools}번${st.length > tools ? ` · 메모 ${st.length - tools}` : ""}<span class="chev">${ic("chevron-right", "sm")}</span></summary>${st.map((s, j) => stepRow(s, running, `p${i}-${j}`)).join("")}</details>` : "";
  let body = "";
  if (running) body = t.live ? `<div class="answer streaming">${mdHtml(t.live)}</div>` : `<div class="typing" role="img" aria-label="생각하는 중"><i></i><i></i><i></i></div>`;
  else if (t.state === "done") { const reps = (t.reports || []).map(p => { const h = p.toLowerCase().endsWith(".html"); return `<a class="btn sm" href="/api/claude/file?path=${encodeURIComponent(p)}" target="_blank" rel="noopener">${ic(h ? "file-text" : "list", "sm")}${h ? "보고서" : "근거"} · ${esc(p.split(/[\\/]/).pop())}</a>`; }).join("");
    body = `<div class="answer">${mdHtml(t.answer)}</div><div class="ans-bar"><button class="btn sm ghost" type="button" data-copyans="${i}">${ic("copy", "sm")}복사</button>${last ? `<button class="btn sm ghost" type="button" data-again="${i}">${ic("repeat", "sm")}다시 ${t.card ? "실행" : "묻기"}</button>` : ""}${reps}${factBadge(t.fact)}<span class="spacer"></span><span class="ai-note">${ic("sparkles", "sm")}AI 가 만든 답 — 근거를 확인하십시오</span></div>`; }
  else body = `<div class="note ${t.state === "failed" ? "crit" : ""}">${ic(t.state === "failed" ? "x-circle" : "info")}<span style="white-space:pre-wrap">${esc(t.error || (t.state === "cancelled" ? "사용자가 멈췄습니다" : "실패"))}</span></div>${last ? `<div class="ans-bar"><button class="btn sm" type="button" data-again="${i}">${ic("repeat", "sm")}다시 묻기</button></div>` : ""}`;
  const q = t.q.length > 700 ? t.q.slice(0, 700) + " …" : t.q;
  const tags = (t.skill ? `<span class="tag">${ic("bolt", "sm")}${esc(t.skill)}</span>` : "") + (t.replay ? `<span class="tag w">${ic("history", "sm")}재현 허용</span>` : "") + (t.log_selected && !t.skill ? `<span class="tag">${ic("folder-open", "sm")}원본 로그 폴더 함께</span>` : "");
  return `<div class="msg-u">${tags ? `<div class="mu-h">${tags}</div>` : ""}${esc(q)}</div><div class="msg-ai"><div class="avatar">${ic("sparkles")}</div><div class="ai-body"><div class="ai-head">${head}</div>${steps}${body}</div></div>`; }
function renderThread(st) { S.ask = st = st || {}; usageBadge(st.usage); renderCtx(st);
  const turns = st.turns || [], run = st.state === "running", box = $("#caThread"); setAskRunning(run);
  $("#ctxSkills").classList.toggle("hidden", !turns.length);         // 빈 대화에서는 가운데 카드로 보인다
  const la = [...turns].reverse().find(t => t.auth != null && t.auth !== "");
  $("#aiStAuth").className = "badge " + (la ? (la.auth === "none" ? "ok" : "warn") : "plain");
  $("#aiStAuth").textContent = la ? (la.auth === "none" ? "구독 로그인 확인됨" : `API 과금 계정 (${la.auth})`) : "구독 로그인으로 실행";
  if (!turns.length) { const K = (S.claude && S.claude.skills) || [], Q = (S.claude && S.claude.quick) || [];
    box.innerHTML = `<div class="ai-empty"><div class="orb">${ic("sparkles")}</div><h2>무엇을 분석할까요?</h2><p>Claude 가 talog 의 사건·처리 여유·진단 리포트와 <b>원본 로그</b>를 도구로 직접 읽고 답합니다. 작업을 고르면 필요한 값만 묻고 그 작업의 스킬로 시작합니다.</p><div class="ai-acts">${K.map((k, i) => skillBtn(k, i, false)).join("")}</div>${Q.length ? `<div class="ai-quick"><span class="lab">가벼운 질문</span>${Q.map((x, i) => `<button type="button" class="btn sm" data-quick="${i}">${ic(x.icon, "sm")}${esc(x.title)}</button>`).join("")}</div>` : ""}</div>`;
    $$("#caThread [data-sk]").forEach(b => b.onclick = () => openSkill(K[+b.dataset.sk]));
    $$("#caThread [data-quick]").forEach(b => b.onclick = () => askClaude(Q[+b.dataset.quick].text, {fresh: true})); return; }
  if (box.querySelector(".ai-empty")) box.innerHTML = "";
  while (box.children.length > turns.length) box.lastElementChild.remove();
  turns.forEach((t, i) => { const h = turnHtml(t, i, i === turns.length - 1); let el = box.children[i];
    if (!el) { el = document.createElement("div"); el.className = "turn"; box.appendChild(el); }
    if (el._h === h) return; el._h = h; el.innerHTML = h;
    $$("[data-copyans]", el).forEach(b => b.onclick = () => copyText(t.answer, "답을 복사했습니다"));
    $$("[data-again]", el).forEach(b => b.onclick = () => t.card ? openSkill(((S.claude && S.claude.skills) || []).find(k => k.key === t.card), {...t.args, replay: !!t.replay}) : askClaude(t.q, {fresh: !(S.ask && S.ask.can_continue) || turns.length === 1})); }); }
/* 답의 수치·시각을 이번 대화의 도구 결과와 대조한 결과 (지어낸 값 찾기) */
function factBadge(f) { if (!f || !f.checked) return "";
  if (f.ok) return `<span class="badge ok plain" title="답에 적힌 수치·시각 ${f.checked}개가 모두 이번 대화의 도구 결과에 있습니다">${ic("check-circle", "sm")}수치 근거 ${f.checked}개 확인</span>`;
  const U = f.unsupported || [], u = U.join(", ");
  return `<span class="badge warn plain" title="도구 결과에서 찾지 못한 값: ${esc(u)} — 계산한 값이거나 지어낸 값일 수 있습니다. 보고서에서 확인하십시오">${ic("alert", "sm")}도구 결과에 없는 값 ${U.length}개: ${esc(U.slice(0, 3).join(", ").slice(0, 28))}${U.length > 3 ? " …" : ""}</span>`; }
async function newThread() { const r = await api("/api/claude/thread/new", {}); if (!r.ok) { toast(r.error, "err"); return; } S.openK = {}; $("#caThread").innerHTML = ""; renderThread({state: "idle", turns: [], usage: (S.ask || {}).usage}); toast("새 대화를 시작합니다"); $("#caQ").focus(); }
$("#btnNewThread").onclick = newThread;
$("#btnCa").onclick = () => { const q = $("#caQ").value.trim(); if (!q) { toast("질문을 넣으십시오", "err"); $("#caQ").focus(); return; } askClaude(q, {fresh: !(S.ask && S.ask.can_continue)}); };
function autoGrow() { const t = $("#caQ"); t.style.height = "auto"; t.style.height = Math.min(240, t.scrollHeight + 2) + "px"; }
$("#caQ").addEventListener("input", autoGrow);
$("#caQ").onkeydown = ev => { if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); $("#btnCa").click(); } };
$("#caModel").onchange = () => { try { localStorage.setItem("talog.claude.model", $("#caModel").value); } catch (e) {} };
$("#caEffort").onchange = () => { try { localStorage.setItem("talog.claude.effort", $("#caEffort").value); } catch (e) {} };
$("#btnCaStop").onclick = async () => {
  if (!(await confirmBox({title: "분석을 멈출까요?", body: "Claude Code 를 끝냅니다. 그때까지 쓴 보고서 파일은 남습니다.", ok: "멈추기", danger: true}))) return;
  const r = await api("/api/claude/ask/cancel", {}); if (!r.ok) toast(r.error, "err"); pollAsk(false); };
function openSetup(on) { const s = $("#aiSetup"); const show2 = on ?? s.classList.contains("hidden"); s.classList.toggle("hidden", !show2); $("#btnAiSetup").setAttribute("aria-expanded", show2);
  if (show2) setTimeout(() => s.scrollIntoView({behavior: reduceMotion() ? "auto" : "smooth", block: "start"}), 40); }
$("#btnAiSetup").onclick = () => openSetup();
$("#btnWs").onclick = () => busy($("#btnWs"), async () => {
  let r; try { r = await api("/api/claude/workspace", {dir: $("#wsDir").value.trim()}); } catch (e) { r = {ok: false, error: String(e)}; }
  if (!r.ok) { toast("폴더를 만들지 못했습니다: " + (r.error || ""), "err"); return; }
  $("#wsDir").value = r.dir;
  $("#wsResult").innerHTML = `<span class="ok">${ic("check-circle", "sm")} 만들었습니다</span> <span class="mono">${esc(r.dir)}</span><div class="sub">데이터 출처: ${esc(r.sources.join(", "))} · 만든 파일 ${r.made.length}개${r.kept.length ? ` · 그대로 둔 파일: ${esc(r.kept.join(", "))}` : ""}</div>`;
  toast("에이전트 폴더를 만들었습니다 — Claude 에서 이 폴더를 여십시오", "ok"); });
$("#btnWsOpen").onclick = async () => { const r = await api("/api/claude/open", {dir: $("#wsDir").value.trim()}); if (!r.ok) toast(r.error, "err"); };
$("#btnAiTest").onclick = () => busy($("#btnAiTest"), async () => { $("#aiTest").classList.remove("hidden");
  $("#aiTestState").innerHTML = `<b>${ic("loader", "spin")}연결 시험 중</b><span class="meta">Claude 가 띄울 명령 그대로 talog MCP 서버를 띄워 도구 목록·개요를 불러 봅니다</span>`; $("#aiTestLog").textContent = "";
  let r; try { r = await api("/api/claude/test", {}); } catch (e) { r = {ok: false, output: String(e)}; }
  $("#aiTestState").innerHTML = r.ok ? `<b class="ok">${ic("check-circle")}연결 시험 통과</b><span class="badge plain num">${r.elapsed_s}초</span><span class="meta">Claude 가 이 명령으로 talog 를 띄우면 아래처럼 보입니다</span>` : `<b class="crit">${ic("x-circle")}연결 시험 실패</b><span class="meta">아래 원본 출력을 확인하십시오</span>`;
  const R = r.report; let h = "";
  if (R) { h += `<ul class="checks">${R.checks.map(c => `<li class="${c.ok ? "" : "bad"}">${ic(c.ok ? "check-circle" : "x-circle")}<b>${esc(c.title)}</b><span>${esc(c.detail)}</span><em>${c.ms != null ? c.ms + " ms" : ""}</em></li>`).join("")}</ul>`;
    if (R.incidents.length) h += `<h3 class="subhead">Claude 가 보게 될 사건 <span class="meta">최근 ${R.incidents.length}건 / 전체 ${R.n_incidents}건</span></h3><div class="tbl" style="max-height:300px"><table><caption class="vh">Claude 가 보게 될 사건</caption><thead><tr><th scope="col">시각</th><th scope="col">심각도</th><th scope="col">제목</th><th scope="col">원인 (룰)</th><th scope="col">판단</th><th scope="col">출처</th></tr></thead><tbody>${R.incidents.map(x => `<tr><td class="mono" style="white-space:nowrap">${esc(x.day)} ${esc(x.time)}</td><td>${sevTag(x.severity)}</td><td><b>${esc(x.title)}</b>${x.more ? ` <span class="meta">+${x.more}</span>` : ""}</td><td>${esc(x.cause || "-")}</td><td>${gateTag(x.gate, x.verdict)}</td><td class="small">${esc(x.source)}${x.replay ? ' <span class="tag">재현</span>' : ""}</td></tr>`).join("")}</tbody></table></div>`; }
  h += `<details class="disclosure"><summary>원본 출력</summary><pre class="log" style="max-height:240px">${esc((r.command ? "$ " + r.command + "\n\n" : "") + (r.output || ""))}</pre></details>`;
  $("#aiTestLog").innerHTML = h; toast(r.ok ? "연결 시험 통과" : "연결 시험 실패", r.ok ? "ok" : "err"); });

