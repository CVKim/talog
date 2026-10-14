/* ── 화면 테마 (시스템·밝게·어둡게) ───────────────────────────── */
const THEMES = ["auto", "light", "dark"], THEME_LAB = {auto: "시스템 설정", light: "밝게", dark: "어둡게"}, THEME_IC = {auto: "monitor", light: "sun", dark: "moon"};
let theme = "auto"; try { theme = localStorage.getItem("talog.theme") || "auto"; } catch (e) {}
function applyTheme() { const r = document.documentElement; if (theme === "auto") r.removeAttribute("data-theme"); else r.setAttribute("data-theme", theme);
  const b = $("#btnTheme"); b.innerHTML = ic(THEME_IC[theme], "lg"); b.setAttribute("aria-label", `화면 테마: ${THEME_LAB[theme]} — 눌러서 바꾸기`); b.title = `화면 테마: ${THEME_LAB[theme]}`; }
function cycleTheme() { theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length]; try { localStorage.setItem("talog.theme", theme); } catch (e) {} applyTheme(); toast(`화면 테마: ${THEME_LAB[theme]}`); }
$("#btnTheme").onclick = cycleTheme; applyTheme();

