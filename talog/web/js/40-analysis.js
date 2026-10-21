/* ── 분석 ──────────────────────────────────────────────── */
async function loadDays() { const f = S.folder || await api("/api/folder"); S.folder = f;
  $("#dayList").innerHTML = (f.days || []).map(d => `<option value="${esc(d)}">`).join("");
  $("#dayChips").innerHTML = (f.days || []).slice(0, 8).map(d => `<button type="button" data-d="${esc(d)}" title="${esc(d)}">${esc(tail2(d))}</button>`).join("");
  $$("#dayChips button").forEach(b => b.onclick = () => { $("#anaDir").value = b.dataset.d; anaStep(); });
  if (!$("#anaDir").value && f.days && f.days[0]) $("#anaDir").placeholder = "예: " + f.days[0]; }
/** 단계 표시 (ux #81) — 폴더가 있으면 ①을 마치고 ②, 작업이 돌거나 끝나면 ③ */
function anaStep(state = S.jobWas) {
  const has = !!$("#anaDir").value.trim(), job = ["running", "done", "error"].includes(state);
  $$("#anaSteps li[data-step]").forEach(li => {
    const n = +li.dataset.step, on = job ? n === 3 : has ? n === 2 : n === 1;
    li.classList.toggle("on", on);
    li.classList.toggle("done", job ? n < 3 : has && n === 1);
    if (on) li.setAttribute("aria-current", "step"); else li.removeAttribute("aria-current");
  });
}
$("#anaDir").addEventListener("input", () => anaStep());
const anaDir = () => $("#anaDir").value.trim() || (S.folder && S.folder.days && S.folder.days[0]) || "";
async function startJob(kind, btn) { const dir = anaDir();
  if (!dir) { toast("로그 폴더를 넣으십시오", "err"); $("#anaDir").focus(); return; }
  if (kind === "replay" && isDirty()) toast("저장하지 않은 규칙 변경은 재현에 반영되지 않습니다 — 저장 후 다시 돌리십시오", "err");
  $("#anaDir").value = dir; const r = await busy(btn, () => api("/api/job", {kind, dir, recipe: $("#anaRecipe").value.trim()}));
  if (!r || !r.ok) { toast((r && r.error) || "시작하지 못했습니다", "err"); return; } pollJob(); }
$("#btnReport").onclick = () => startJob("report", $("#btnReport")); $("#btnReplay").onclick = async () => { if (await needMode("operator", "경보 재현은 운영자 모드에서 돌립니다")) startJob("replay", $("#btnReplay")); };
$("#btnAnaClaude").onclick = () => busy($("#btnAnaClaude"), async () => { const dir = anaDir();
  if (!dir) { toast("로그 폴더를 넣으십시오", "err"); return; }
  await ensureClaude(); openSkill(((S.claude && S.claude.skills) || []).find(k => k.key === "logs"), {day: dir}); });
let jobTimer = null;
async function pollJob() { const j = await api("/api/job"); if (j.state === "idle") return; $("#jobBox").classList.remove("hidden");
  const kind = j.kind === "replay" ? "경보 재현" : "진단 리포트", run = j.state === "running";
  $("#jobState").innerHTML = run ? `${ic("loader", "spin")}<b>${kind} 진행 중</b><span class="mono">${esc(j.target)}</span>`
    : j.state === "done" ? `${ic("check-circle", "ok")}<b class="ok">${kind} 완료</b><span class="meta">${j.elapsed_s}초</span>${j.kind === "replay" ? `<a class="btn sm" href="#inc">사건 화면에서 '재현' 보기${ic("arrow-right", "sm")}</a>` : ""}`
    : `${ic("x-circle", "crit")}<b class="crit">${kind} 실패</b>`;
  $("#btnReport").disabled = $("#btnReplay").disabled = run;
  $("#jobLog").textContent = (j.log || []).join("\n"); $("#jobLog").scrollTop = 1e9;
  clearTimeout(jobTimer); if (run) jobTimer = setTimeout(pollJob, 1500); else if (S.jobWas === "running") { loadReports(); loadTimeline(); if (j.kind === "report" && j.reports && j.reports[0]) selectReport(j.reports[0]); }
  S.jobWas = j.state; anaStep(j.state); }
async function loadReports() { if (!S.reports) $("#repRows").innerHTML = skRows(4);
  const r = await api("/api/reports"); S.reports = r.rows; $("#repCount").textContent = r.rows.length ? `${r.rows.length}개` : "";
  $("#repRows").innerHTML = r.rows.length ? r.rows.map(x => `<tr class="${S.rep === x.tag ? "sel" : ""}" data-tag="${esc(x.tag)}"><td><div class="rep-t"><b>${esc(x.tag)}</b><span class="meta">${esc(x.mtime)} · 소견 ${x.n_findings}건</span></div>${x.findings.slice(0, 2).map(f => `<div class="sub">${esc(f)}</div>`).join("")}
      <div class="rowact"><a class="btn sm" href="/r/${encodeURIComponent(x.tag)}/" target="_blank" rel="noopener">${ic("external")}열기</a>${x.db ? `<button class="btn sm" type="button" data-ask="${esc(x.tag)}">${ic("message")}질문</button>` : ""}${isDev() ? `<button class="btn sm" type="button" data-claude-rep="${esc(x.tag)}">${ic("sparkles", "brand")}Claude 로 확인</button>` : ""}</div></td></tr>`).join("")
    : emptyRow(1, "아직 만든 리포트가 없습니다", '위에서 폴더를 넣고 "진단 리포트 만들기"를 누르십시오.', "file-text");
  $$("#repRows [data-ask]").forEach(b => b.onclick = () => selectReport(b.dataset.ask));
  $$("#repRows [data-claude-rep]").forEach(b => b.onclick = () => askClaude(`talog 리포트 ${b.dataset.claudeRep} 의 자동 진단 소견을 리포트 DB 로 교차 확인하고, 원인과 할 일을 정리해 줘 (talog-rca 절차, 보고서는 reports 폴더에 HTML)`, {fresh: true})); }
/* 리포트에 질문 — 목록에서 '질문'을 눌러 대상을 고른 뒤에만 옆에 연다(처음 화면에는 리포트 목록만) */
function selectReport(tag) { S.rep = tag; S.repQa = []; $("#askCard").classList.remove("hidden"); $("#repWrap").classList.remove("one"); $("#askTag").textContent = tag; $("#askTag").classList.remove("hidden"); $("#askList").innerHTML = ""; $$("#repRows tr").forEach(tr => tr.classList.toggle("sel", tr.dataset.tag === tag)); $("#askQ").focus(); }
async function askReport() { const q = $("#askQ").value.trim(); if (!q) { $("#askQ").focus(); return; } if (!S.rep) { toast("리포트 목록에서 '질문'을 눌러 대상을 고르십시오", "err"); return; }
  $("#askBusy").textContent = "LLM 이 DB 를 조회하는 중… (CPU 는 몇 분 걸릴 수 있음)";
  const r = await busy($("#btnAsk"), () => api("/api/ask/report", {tag: S.rep, q})); $("#askBusy").textContent = "";
  if (!r || !r.ok) { toast("질문 실패: " + ((r && r.error) || ""), "err"); return; } $("#askQ").value = "";
  S.repQa.unshift({q, ...r});
  $("#askList").innerHTML = S.repQa.map(x => `<div class="qa"><div class="q">${ic("message", "sm")}<span>${esc(x.q)}</span></div><div class="a">${esc(x.answer)}</div>${factNote(x.fact, "조회 결과")}<div class="sub">${esc(x.model)} · ${esc(x.device)} · ${x.wall_s}초</div>${x.trace && x.trace.length ? `<details class="disclosure"><summary>조회한 근거 ${x.trace.length}건</summary><pre class="log">${esc(x.trace.map(t => "▶ " + t.tool + ": " + (t.args.query || JSON.stringify(t.args)) + "\n" + (t.result || "")).join("\n\n"))}</pre></details>` : ""}</div>`).join(""); }
function factNote(f, src) { if (!f) return ""; return f.ok ? `<div class="sub ok">${ic("check", "sm")} 답의 숫자·시각이 모두 ${src}에 있습니다</div>`
  : `<div class="note warn">${ic("alert")}<span>${src}에 없는 값: <b>${esc(f.unsupported.join(", "))}</b> — LLM 이 지어냈을 수 있습니다. 근거로 확인하십시오.</span></div>`; }
$("#btnAsk").onclick = askReport; $("#askQ").onkeydown = ev => { if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) askReport(); };

