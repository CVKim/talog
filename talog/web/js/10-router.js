/* ── 화면 전환 (주소 #보기/세부 — 뒤로 가기·직접 링크) ─────────────── */
function show(v) { if (v === "ai" && !isDev()) v = "ops";            // 현장 모드에는 Claude 화면이 없다
  S.view = v;
  $$("#nav a").forEach(a => { if (a.dataset.v === v) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
  $$("section.view").forEach(s => s.classList.toggle("active", s.id === "v-" + v));
  $("#pageTitle").textContent = VIEWS[v][0]; $("#pageSub").textContent = VIEWS[v][1];
  document.title = `${VIEWS[v][0]} · talog`;
  if (v === "ops") loadTimeline(); if (v === "inc") loadInc(); if (v === "ana") { loadReports(); loadDays(); pollJob(); }
  if (v === "set") { renderGpuFree(); if (!S.folder) loadFolder(); } if (v === "ai") loadClaude();
  updateSaveBar(); }
function go(v, opt = {}) { const same = S.view === v; if (!same || opt.force) show(v);
  if (opt.push !== false) { const h = "#" + v + (opt.sub ? "/" + opt.sub : ""); if (location.hash !== h) history[same ? "replaceState" : "pushState"](null, "", h); }
  if (!same && !opt.keepScroll) window.scrollTo(0, 0);
  if (opt.focus && !same) $("#pageTitle").focus({preventScroll: true}); }
function scrollSec(id) { setTimeout(() => { const el = $("#" + id); if (el) el.scrollIntoView({behavior: reduceMotion() ? "auto" : "smooth", block: "start"}); }, 40); }
function goSec(id) { go("set", {sub: id, keepScroll: true}); scrollSec(id); }
function applyHash() { const [v0, a, b] = decodeURIComponent(location.hash.slice(1)).split("/"); const v = VIEWS[v0] && (v0 !== "ai" || isDev()) ? v0 : "ops";
  if (v !== S.view) show(v);
  if (v === "inc" && a && (!S.inc || S.inc.id !== a)) openInc(a, b === "replay");
  if (v === "set" && a) scrollSec(a);
  if (v === "ai" && a === "setup") openSetup(true); }
document.addEventListener("click", ev => {
  const a = ev.target.closest('a[href^="#"]'); if (!a || ev.ctrlKey || ev.metaKey || ev.shiftKey || a.target) return;
  const [v, sub] = a.getAttribute("href").slice(1).split("/"); if (!VIEWS[v]) return;
  ev.preventDefault(); go(v, {focus: true, sub, keepScroll: !!sub}); if (sub && v === "set") scrollSec(sub); });
window.addEventListener("popstate", applyHash);
document.addEventListener("keydown", ev => { if ((ev.key === "Enter" || ev.key === " ") && ev.target.matches && ev.target.matches("tr.click")) { ev.preventDefault(); ev.target.click(); } });
/* 다시 그려도 사용자가 펼치거나 접은 상태는 그대로 (data-k 로 기억) */
document.addEventListener("click", ev => { const s = ev.target.closest("details[data-k] > summary"); if (s) S.openK[s.parentElement.dataset.k] = !s.parentElement.open; });
const openAttr = (k, def) => (S.openK[k] ?? def) ? "open" : "";

