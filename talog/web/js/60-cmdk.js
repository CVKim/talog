/* ── 빠른 이동 (Ctrl+K) ─────────────────────────────────── */
let cmdItems = [], cmdSel = 0;
function cmdList() { const items = [];
  Object.entries(VIEWS).filter(([k]) => k !== "ai" || isDev()).forEach(([k, v]) => items.push({g: "화면", icon: {ops: "dashboard", inc: "siren", ana: "file-search", ai: "sparkles", set: "sliders"}[k], label: v[0], k: v[1], run: () => go(k, {focus: true})}));
  ((S.claude && S.claude.skills) || []).forEach(k => items.push({g: "Claude 스킬", icon: k.icon, label: k.title, k: `${k.command} · ${k.desc}`, run: () => { go("ai"); openSkill(k); }}));
  (S.incRows || (S.st && S.st.incidents) || []).slice(0, 30).forEach(r => items.push({g: "사건", icon: "siren", label: r.title, k: `${mmdd(r.day || "")} ${r.time || ""} ${r.cause || ""}`, run: () => { go("inc", {focus: false}); openInc(r.id, !!r.replay); }}));
  if (isDev()) { items.push({g: "명령", icon: "plus", label: "Claude 새 대화", k: "앞 대화를 잊고 새로 시작", run: () => { go("ai"); newThread(); }});
    items.push({g: "명령", icon: "plug", label: "Claude 연결 시험", k: "MCP 서버 점검", run: () => { go("ai"); openSetup(true); $("#btnAiTest").click(); }}); }
  Object.entries(MODE_INFO).filter(([k]) => k !== curMode()).forEach(([k, x]) => items.push({g: "모드", icon: x.icon,
    label: `${x.label}로 바꾸기`, k: x.d + (MODE_RANK[k] > MODE_RANK[curMode()] ? " (PIN)" : ""), run: () => switchMode(k)}));
  items.push({g: "명령", icon: "monitor", label: "화면 테마 바꾸기", k: THEME_LAB[theme], run: cycleTheme});
  items.push({g: "도움말", icon: "help", label: "이 화면 읽는 법", k: "? 키", run: () => openHelp()});
  items.push({g: "도움말", icon: "book", label: "용어 사전", k: "버틸 여유 · 기준선 · 판단 일치 …", run: () => openHelp({tab: "terms"})});
  items.push({g: "도움말", icon: "route", label: "처음 안내 다시 보기", k: "화면 30초 둘러보기", run: startTour});
  Object.entries(GLOSSARY).filter(([, g]) => !g.dev || isDev()).forEach(([k, g]) => items.push({g: "용어", icon: "book", label: g.t, k: g.d.slice(0, 40) + "…", run: () => openHelp({tab: "terms", term: k})}));
  items.push({g: "명령", icon: S.st && S.st.running ? "square" : "play", label: S.st && S.st.running ? "감시 정지" : "감시 시작", k: "", run: () => $("#btnRun").click()});
  return items; }
function renderCmd() { const q = $("#cmdkQ").value.trim().toLowerCase();
  cmdItems = cmdList().filter(x => !q || `${x.label} ${x.k} ${x.g}`.toLowerCase().includes(q)).slice(0, 40); cmdSel = Math.min(cmdSel, Math.max(0, cmdItems.length - 1));
  let g = ""; $("#cmdkList").innerHTML = cmdItems.length ? cmdItems.map((x, i) => (x.g !== g ? `<div class="cmdk-g">${esc(g = x.g)}</div>` : "") + `<button type="button" class="cmdk-item" role="option" id="cmd-${i}" aria-selected="${i === cmdSel}" data-i="${i}" tabindex="-1">${ic(x.icon)}<span>${esc(x.label)}</span><span class="k">${esc(x.k)}</span></button>`).join("") : `<div class="empty" style="padding:20px">${ic("search", "xl")}<b>찾지 못했습니다</b></div>`;
  $("#cmdkQ").setAttribute("aria-activedescendant", cmdItems.length ? "cmd-" + cmdSel : "");
  $$("#cmdkList .cmdk-item").forEach(b => b.onclick = () => runCmd(+b.dataset.i)); const s = $("#cmd-" + cmdSel); if (s) s.scrollIntoView({block: "nearest"}); }
function runCmd(i) { const x = cmdItems[i]; $("#cmdk").close(); if (x) x.run(); }
function openCmd() { const d = $("#cmdk"); if (d.open) return; $("#cmdkQ").value = ""; cmdSel = 0; renderCmd(); d.showModal(); $("#cmdkQ").focus(); }
$("#btnCmd").onclick = openCmd;
$("#cmdkQ").addEventListener("input", () => { cmdSel = 0; renderCmd(); });
$("#cmdkQ").addEventListener("keydown", ev => { if (ev.key === "ArrowDown" || ev.key === "ArrowUp") { ev.preventDefault(); cmdSel = (cmdSel + (ev.key === "ArrowDown" ? 1 : -1) + cmdItems.length) % Math.max(1, cmdItems.length); renderCmd(); }
  else if (ev.key === "Enter" && !ev.isComposing) { ev.preventDefault(); runCmd(cmdSel); } });
$("#cmdk").addEventListener("click", ev => { if (ev.target === $("#cmdk")) $("#cmdk").close(); });
document.addEventListener("keydown", ev => { if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "k") { ev.preventDefault(); openCmd(); } });

