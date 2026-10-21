/* ── 모드 — 현장 · 운영자 · 개발자 (talog/access.py · console/modes.py) ────────────────
   왼쪽 위 전환 탭(#modeSw)과 설정 맨 위 '모드' 카드에서 바꾼다. 위로 올릴 때만 PIN 을 묻고, 고른 모드는 talog.yaml 에
   남아 다음에 열 때도 같다. 현장 모드는 설정 칸을 잠그고(fieldset), 감시 정지·경보 재현·시험 경보 앞에서 needMode() 가
   운영자 모드로 바꿀지 묻는다. Claude 화면·버튼(.dev-only)은 개발자 모드에서만. 서버(http.py 의 oper·dev)가 한 번 더 지킨다. */
const MODE_RANK = {field: 0, operator: 1, developer: 2};
const MODE_INFO = {
  field: {icon: "factory", label: "현장 모드", d: "운영 현황·사건·리포트를 보고 만듭니다. 설정은 보기만 합니다."},
  operator: {icon: "wrench", label: "운영자 모드", d: "현장 모드에 더해 설정 저장·시험·감시 정지·경보 재현을 합니다."},
  developer: {icon: "code", label: "개발자 모드", d: "운영자 모드에 더해 Claude(구독)로 사건과 원본 로그를 분석합니다."}};
const curMode = () => (S.acc && S.acc.mode) || "field";
const isDev = () => curMode() === "developer";
const atLeast = m => MODE_RANK[curMode()] >= MODE_RANK[m];

function applyAccess(a) {
  if (!a) return;
  const was = S.acc && S.acc.mode; S.acc = a;
  document.documentElement.dataset.mode = a.mode;
  if (!isDev() && S.view === "ai") go("ops", {push: false});
  renderAccess();
  if (was && was !== a.mode) modeChanged(); }
const refreshAccess = () => api("/api/access").then(applyAccess).catch(() => {});
/* 모드에 따라 그리는 것이 다른 곳(Claude 버튼이 붙는 사건 상세·리포트 목록)을 다시 그린다 */
function modeChanged() {
  if (S.inc && S.inc.id) openInc(S.inc.id, S.inc.rp);
  if (S.reports) loadReports();
  if (isDev()) ensureClaude(); }

let accKey = "";
function renderAccess() { const m = curMode();
  if (m === accKey) return;                       // 5초마다 오는 상태로 다시 그리지 않는다 (누르던 버튼이 사라지지 않게)
  accKey = m;
  $$("#modeSw [data-mode]").forEach(b => { const k = b.dataset.mode, on = k === m; b.setAttribute("aria-checked", on); b.tabIndex = on ? 0 : -1;
    b.title = on ? `지금 ${MODE_INFO[k].label}` : `${MODE_INFO[k].label}로 바꾸기${MODE_RANK[k] > MODE_RANK[m] ? " (PIN)" : ""}`; });
  $("#accT").textContent = `모드 — 지금 ${MODE_INFO[m].label}`;
  $("#accIc").className = "acc-ic " + m; $("#accIc").innerHTML = ic(MODE_INFO[m].icon, "lg");
  $("#modeOpts").innerHTML = Object.entries(MODE_INFO).map(([k, x]) => `<button type="button" class="mode-opt" role="radio" data-mode="${k}" aria-checked="${k === m}">
    <b>${ic(x.icon)}${esc(x.label)}${k === m ? '<span class="tag on">지금</span>' : MODE_RANK[k] > MODE_RANK[m] ? '<span class="tag">PIN</span>' : ""}</b><span>${esc(x.d)}</span></button>`).join("");
  $("#setFs").disabled = !atLeast("operator"); $("#v-set").classList.toggle("locked", !atLeast("operator"));
  updateSaveBar(); }

/* 모드 바꾸기 — 위로 올릴 때만 PIN 창. 바꾸면 true */
async function switchMode(target, why = "") {
  if (!MODE_INFO[target] || target === curMode()) return target === curMode();
  if (MODE_RANK[target] > MODE_RANK[curMode()]) return pinDlg(target, why);
  if (target === "field" && isDirty()) { toast("저장하지 않은 설정 변경이 있습니다 — 저장하거나 되돌린 뒤 바꾸십시오", "err"); return false; }
  const r = await api("/api/access/mode", {mode: target});
  if (!r || !r.ok) { toast((r && r.error) || "바꾸지 못했습니다", "err"); return false; }
  applyAccess(r.access); toast(`${MODE_INFO[target].label}로 바꿨습니다`, "ok"); return true; }
/* 운영자·개발자 모드가 있어야 하는 동작 앞에서 부른다 — 이미 그 모드 이상이면 바로 true */
async function needMode(m, why) { return atLeast(m) || switchMode(m, why); }

function modeClick(ev) { const b = ev.target.closest("[data-mode]"); if (b) switchMode(b.dataset.mode); }
$("#modeSw").addEventListener("click", modeClick);
$("#modeOpts").addEventListener("click", modeClick);
/* 라디오 묶음 — 화살표로 옮겨 가며 고른다(Enter·Space 로 바꾼다) */
$("#modeSw").addEventListener("keydown", ev => { const k = ev.key === "ArrowDown" || ev.key === "ArrowRight" ? 1 : ev.key === "ArrowUp" || ev.key === "ArrowLeft" ? -1 : 0;
  if (!k) return; ev.preventDefault(); const bs = $$("#modeSw [data-mode]"), i = bs.indexOf(document.activeElement);
  const n = bs[Math.max(0, Math.min(bs.length - 1, (i < 0 ? 0 : i) + k))]; bs.forEach(b => { b.tabIndex = b === n ? 0 : -1; }); n.focus(); });

let pinTarget = "field", pinResolve = null;
function pinDlg(target, why = "") { pinTarget = target;
  $("#pinT").textContent = `${MODE_INFO[target].label}로 바꾸기`;
  $("#pinD").textContent = (why ? why + ". " : "") + `${MODE_INFO[target].d} PIN 을 넣으십시오 (담당자에게 문의).`;
  $("#pinIn").value = ""; $("#pinErr").textContent = "";
  return new Promise(res => { pinResolve = res; const d = $("#pinDlg"); d.returnValue = ""; d.showModal(); $("#pinIn").focus(); }); }
$("#pinForm").addEventListener("submit", async ev => { ev.preventDefault();
  const pin = $("#pinIn").value, err = m => { $("#pinErr").textContent = m; $("#pinIn").select(); };
  if (!pin) return err("PIN 을 넣으십시오");
  const r = await busy($("#pinOk"), () => api("/api/access/mode", {mode: pinTarget, pin}));
  if (!r || !r.ok) return err((r && r.error) || "바꾸지 못했습니다");
  applyAccess(r.access); toast(`${MODE_INFO[pinTarget].label}로 바꿨습니다`, "ok");
  $("#pinDlg").close("ok"); });
$("#pinNo").onclick = () => $("#pinDlg").close("cancel");
$("#pinDlg").addEventListener("close", () => { if (pinResolve) { pinResolve($("#pinDlg").returnValue === "ok"); pinResolve = null; } });
