/* ── 설정: 바인딩 ─────────────────────────────────────── */
function isDirty() { return !!S.set && (JSON.stringify(S.set) !== S.orig || !!S.secret || S.clearSecret); }
function updateSaveBar() { $("#saveBar").classList.toggle("hidden", !isDirty()); $("#btnSaveGo").classList.toggle("hidden", S.view === "set"); }
function markDirty() { updateSaveBar(); }
$("#btnSaveGo").onclick = () => go("set", {focus: true});
function bind(root) {
  $$("[data-path]", root).forEach(el => {
    if (el.classList.contains("chips")) return chips(el);
    if (el.dataset.bound) return; el.dataset.bound = "1";
    const p = el.dataset.path, t = el.dataset.type || "";
    const load = () => { const v = getp(S.set, p); if (el.type === "checkbox") el.checked = !!v; else el.value = v ?? ""; };
    load(); el._load = load;
    el.addEventListener(el.type === "checkbox" || el.tagName === "SELECT" ? "change" : "input", () => {
      let v = el.type === "checkbox" ? el.checked : t === "number" ? (el.value === "" ? 0 : Number(el.value)) : el.value;
      setp(S.set, p, v); markDirty(); onChange(p); });
  });
}
function reloadBound() { $$("[data-path]").forEach(el => { if (el._load) el._load(); if (el._render) el._render(); }); }
function chips(el) {
  const p = el.dataset.path;
  const render = () => { let arr = getp(S.set, p); if (!Array.isArray(arr)) arr = [];
    el.innerHTML = arr.map((x, i) => `<span class="chipx">${esc(x)}<button type="button" data-i="${i}" aria-label="${esc(x)} 빼기">${ic("x", "sm")}</button></span>`).join("") + `<input type="text" aria-label="${esc(el.dataset.ph || "")}" placeholder="${esc(el.dataset.ph || "")}">`;
    const inp = $("input", el);
    inp.addEventListener("keydown", ev => { if (ev.isComposing) return; if ((ev.key === "Enter" || ev.key === ",") && inp.value.trim()) { ev.preventDefault(); addTo(p, inp.value.trim()); render(); $("input", el).focus(); }
      else if (ev.key === "Backspace" && !inp.value && arr.length) { arr.pop(); setp(S.set, p, arr); markDirty(); onChange(p); render(); $("input", el).focus(); } });
    inp.addEventListener("blur", () => { if (inp.value.trim()) { addTo(p, inp.value.trim()); render(); } });
    $$("button[data-i]", el).forEach(b => b.onclick = () => { arr.splice(+b.dataset.i, 1); setp(S.set, p, arr); markDirty(); onChange(p); render(); $("input", el).focus(); }); };
  el._render = render; render();
}
function addTo(p, v) { let arr = getp(S.set, p); if (!Array.isArray(arr)) arr = []; v.split(",").map(s => s.trim()).filter(Boolean).forEach(x => { if (!arr.includes(x)) arr.push(x); }); setp(S.set, p, arr); markDirty(); onChange(p); }
function onChange(p) { if (p === "tracking") renderFiles(); }

/* ── 설정: 수집 ───────────────────────────────────────── */
function trMode() { const t = S.set.tracking; return Array.isArray(t) ? "list" : t === "all" ? "all" : "core"; }
function renderTracking() { const m = trMode(); $$("#trSeg button").forEach(b => { b.classList.toggle("on", b.dataset.v === m); b.setAttribute("aria-pressed", b.dataset.v === m); });
  $("#trListBox").classList.toggle("hidden", m !== "list");
  $("#trHelp").innerHTML = ic("info") + "<span>" + {core: "<b>핵심 로그</b> — InspStarter·Comm·seq_N 등 소형 로그만 읽습니다(초당 수 KB, 검사 PC 부하 최소). 기본 규칙이 모두 동작하고, 로그 문구 규칙이 다른 파일을 가리키면 그 파일도 같이 읽습니다.",
    all: "<b>폴더 전체</b> — 일자 폴더의 *.log / *.txt 전체(DLInfer.log 같은 대용량 포함)를 읽습니다. 로그 문구 규칙을 폴더 전체에 걸 때 씁니다.",
    list: "<b>직접 선택</b> — 목록의 파일·와일드카드만 읽습니다. 기본 규칙은 고른 파일 범위에서만 동작합니다 (예: Comm.log 를 빼면 결함명 감시가 멈춤)."}[m] + "</span>";
  renderFiles(); }
$$("#trSeg button").forEach(b => b.onclick = () => { const v = b.dataset.v;
  S.set.tracking = v === "list" ? (Array.isArray(S.set.tracking) ? S.set.tracking : ["InspStarter.log", "Comm.log", "WorkerThreadPoolMng.log", "exception.log", "ProcessUsage.log", "BatchRunLog.txt", "seq_*.log"]) : v;
  markDirty(); reloadBound(); renderTracking(); loadFolder(); });
async function loadFolder() { if (!S.set) return; const dir = $("#fileDir").value.trim();
  S.folder = await api("/api/folder?dir=" + encodeURIComponent(dir) + "&tracking=" + encodeURIComponent(JSON.stringify(S.set.tracking)));
  if (!dir) $("#fileDir").placeholder = S.folder.folder || "폴더 없음"; renderFiles(); renderSugg(); }
$("#btnFiles").onclick = () => busy($("#btnFiles"), loadFolder);
function renderFiles() { const F = S.folder; if (!F || !S.set) return; const list = trMode() === "list";
  const sel = new Set((Array.isArray(S.set.tracking) ? S.set.tracking : []).map(x => x.toLowerCase()));
  $("#fileRows").innerHTML = F.files.map(f => `<tr><td>${list ? `<input type="checkbox" data-f="${esc(f.name)}" aria-label="${esc(f.name)} 읽기" ${sel.has(f.name.toLowerCase()) ? "checked" : ""}>` : ""}</td><td class="mono">${esc(f.name)}</td><td class="mono">${fmtSize(f.size)}</td><td class="small">${esc(f.category)}</td><td class="mono small">${esc(f.mtime)}</td><td>${f.tracked ? '<span class="tag will">읽음</span>' : ""}</td></tr>`).join("") || emptyRow(6, "파일 없음", "다른 폴더를 불러와 보십시오.", "folder-open");
  $$("#fileRows input[data-f]").forEach(cb => cb.onchange = () => { const arr = S.set.tracking.slice(); const n = cb.dataset.f; const i = arr.findIndex(x => x.toLowerCase() === n.toLowerCase());
    if (cb.checked && i < 0) arr.push(n); if (!cb.checked && i >= 0) arr.splice(i, 1); S.set.tracking = arr; markDirty(); reloadBound(); loadFolder(); });
  const w = F.files.filter(f => f.tracked); $("#fileNote").textContent = `${F.folder || "-"} · 파일 ${F.files.length}개 · 읽는 파일 ${w.length}개 (${fmtSize(w.reduce((a, f) => a + f.size, 0))}) — 저장 전 설정 기준 미리보기`; }

/* ── 설정: 탐지 ───────────────────────────────────────── */
function biVal(b) { const u = (S.set.rules.builtin || {})[b.name] || {}; return Object.assign({enabled: true, severity: b.severity, count: 1, window_min: 10}, b.default, u); }
function biSet(name, k, v) { const bi = S.set.rules.builtin = S.set.rules.builtin || {}; const b = S.meta.builtin.find(x => x.name === name);
  const cur = Object.assign({}, bi[name] || {}); cur[k] = v;
  const def = Object.assign({enabled: true, severity: b.severity, count: 1, window_min: 10}, b.default);
  Object.keys(cur).forEach(x => { if (JSON.stringify(cur[x]) === JSON.stringify(def[x])) delete cur[x]; });
  if (Object.keys(cur).length) bi[name] = cur; else delete bi[name]; markDirty(); }
function numIn(name, k, v, cls = "w-s", step = "", lab = "") { return `<input type="number" class="${cls}" data-bi="${name}" data-k="${k}" value="${esc(v)}" ${step ? `step="${step}"` : ""} min="0" aria-label="${esc(lab || k)}">`; }
function condOf(b, v) {
  if (b.kind === "event") return `${numIn(b.name, "count", v.count, "w-s", "", "횟수")}회 / ${numIn(b.name, "window_min", v.window_min, "w-s", "", "분")}분 <span class="meta">(1회 = 즉시)</span>`;
  if (b.kind === "repeat") return `${numIn(b.name, "count", v.count, "w-s", "", "횟수")}회 / ${numIn(b.name, "window_min", v.window_min, "w-s", "", "분")}분`;
  if (b.kind === "stall") return `평소 소요 중앙값의 ${numIn(b.name, "factor", v.factor, "w-s", "0.1", "배수")}배 넘게 끝나지 않으면`;
  if (b.kind === "memory") return `+${numIn(b.name, "mb_per_hour", v.mb_per_hour, "w-s", "", "MB/시간")} MB/시간 넘게 오르면`;
  if (b.kind === "capacity") return `유휴 스레드가 기준보다 ${numIn(b.name, "idle_drop", v.idle_drop, "w-s", "", "유휴 감소")}개 줄거나, 일시 지연 내성이 한계의 ${numIn(b.name, "tolerance_pct", v.tolerance_pct, "w-s", "", "내성 %")}% 아래 <span class="meta">(그룹별 자동 기준선 · 유휴 1개 이하·내성 20% 미만은 심각)</span>`;
  if (b.kind === "drift") return `스레드 점유 시간이 기준의 ${numIn(b.name, "ratio", v.ratio, "w-s", "0.05", "배수")}배 이상 <span class="meta">(투입 간격이 그대로일 때만 · 1.5배 이상은 심각)</span>`;
  if (b.kind === "gpu") return `${numIn(b.name, "celsius", v.celsius, "w-s", "", "온도")} °C 이상`;
  if (b.kind === "streak") return `${numIn(b.name, "count", v.count, "w-s", "", "연속 건수")}건 연속 NG`;
  if (b.kind === "rate") return `최근 ${numIn(b.name, "count", v.count, "w-s", "", "검사 수")}검사 중 ${numIn(b.name, "percent", v.percent, "w-s", "", "퍼센트")}% 이상 NG`;
  return ""; }
function renderBuiltin() {
  $("#biRows").innerHTML = S.meta.builtin.map(b => { const v = biVal(b);
    return `<tr><td><label class="switch"><input type="checkbox" data-bi="${b.name}" data-k="enabled" aria-label="${esc(b.label)} 사용" ${v.enabled !== false ? "checked" : ""}></label></td><td><b>${esc(b.label)}</b><div class="mono sub">${esc(b.name)}</div></td>
      <td><select data-bi="${b.name}" data-k="severity" aria-label="${esc(b.label)} 등급">${["crit", "warn", "info"].map(s => `<option value="${s}" ${v.severity === s ? "selected" : ""}>${SEVK[s]}</option>`).join("")}</select></td><td class="small">${condOf(b, v)}</td></tr>`; }).join("");
  $$("#biRows [data-bi]").forEach(el => el.addEventListener(el.type === "checkbox" || el.tagName === "SELECT" ? "change" : "input", () => {
    const k = el.dataset.k; biSet(el.dataset.bi, k, el.type === "checkbox" ? el.checked : el.tagName === "SELECT" ? el.value : Number(el.value || 0)); })); }
function renderCustom() { const C = S.set.rules.custom = S.set.rules.custom || [];
  $("#cuRows").innerHTML = C.length ? C.map((r, i) => { const def = r.type === "defect"; const mv = Array.isArray(r.match) ? r.match.join(", ") : (r.match || "");
    return `<tr data-i="${i}"><td><label class="switch"><input type="checkbox" data-k="enabled" aria-label="규칙 사용" ${r.enabled !== false ? "checked" : ""}></label></td>
    <td><input type="text" data-k="name" aria-label="규칙 이름" value="${esc(r.name || "")}" style="width:130px"></td>
    <td><select data-k="type" aria-label="규칙 종류"><option value="defect" ${def ? "selected" : ""}>결함명</option><option value="log" ${!def ? "selected" : ""}>로그 문구</option></select></td>
    <td><input type="text" data-k="match" aria-label="찾을 것" value="${esc(mv)}" class="mono" style="width:230px" placeholder="${def ? "BOOT_DAMAGED, *CRACK*" : "문구 또는 re:정규식"}"></td>
    <td>${def ? '<span class="meta">Comm.log 판정</span>' : `<input type="text" data-k="files" aria-label="대상 파일" value="${esc((r.files || []).join(", "))}" style="width:130px" placeholder="추적 파일 전체">`}</td>
    <td><select data-k="severity" aria-label="등급">${["crit", "warn", "info"].map(s => `<option value="${s}" ${(r.severity || "warn") === s ? "selected" : ""}>${SEVK[s]}</option>`).join("")}</select></td>
    <td class="small" style="white-space:nowrap"><input type="number" data-k="count" aria-label="횟수" value="${r.count || 1}" class="w-s" min="1">회 / <input type="number" data-k="window_min" aria-label="분" value="${r.window_min || 10}" class="w-s">분</td>
    <td><button class="btn sm icon danger" type="button" data-del="${i}" aria-label="${esc(r.name || "규칙")} 삭제" title="삭제">${ic("trash")}</button></td></tr>`; }).join("") : emptyRow(8, "사용자 규칙 없음", "치명 결함명이나 꼭 잡을 로그 문구를 추가하십시오.", "list");
  $$("#cuRows tr[data-i]").forEach(tr => { const r = C[+tr.dataset.i];
    $$("[data-k]", tr).forEach(el => el.addEventListener(el.type === "checkbox" || el.tagName === "SELECT" ? "change" : "input", () => { const k = el.dataset.k;
      if (k === "enabled") r.enabled = el.checked; else if (k === "files") r.files = el.value.split(",").map(s => s.trim()).filter(Boolean);
      else if (k === "match") r.match = r.type === "defect" ? el.value.split(",").map(s => s.trim()).filter(Boolean) : el.value;
      else if (k === "count" || k === "window_min") r[k] = Number(el.value || 1);
      else if (k === "type") { r.type = el.value; r.match = el.value === "defect" ? String(Array.isArray(r.match) ? r.match.join(",") : r.match || "").split(",").map(s => s.trim()).filter(Boolean) : (Array.isArray(r.match) ? r.match.join(", ") : r.match || ""); if (el.value === "defect") delete r.files; renderCustom(); }
      else r[k] = el.value;
      markDirty(); if (k === "match") renderSugg(); })); });
  $$("#cuRows [data-del]").forEach(b => b.onclick = () => { C.splice(+b.dataset.del, 1); markDirty(); renderCustom(); renderSugg(); toast("규칙을 뺐습니다 — 저장해야 적용되고, '되돌리기'로 살릴 수 있습니다"); }); }
$("#btnAddDef").onclick = () => { S.set.rules.custom.push({type: "defect", name: "치명 결함", match: [], severity: "crit", count: 1, window_min: 10}); markDirty(); renderCustom(); };
$("#btnAddLog").onclick = () => { S.set.rules.custom.push({type: "log", name: "새 로그 규칙", match: "", files: [], severity: "warn", count: 1, window_min: 10}); markDirty(); renderCustom(); };
$("#presetSel").onchange = ev => { const i = ev.target.value; if (i === "") return; const p = JSON.parse(JSON.stringify(S.meta.presets[+i]));
  S.set.rules.custom.push(Object.assign({type: "log"}, p)); ev.target.value = ""; markDirty(); renderCustom(); toast(`'${p.name}' 추가 — 저장해야 적용됩니다`); };
function renderSugg() { const F = S.folder; if (!F || !S.set) return;
  const have = new Set(S.set.rules.custom.filter(r => r.type === "defect").flatMap(r => r.match || []).map(x => String(x).toUpperCase()));
  const seen = {}; (F.seen_defects || []).forEach(([n, c]) => seen[n] = c);
  const names = Array.from(new Set([...(F.seen_defects || []).map(x => x[0]), ...(F.recipe_defects || [])]));
  $("#defSugg").innerHTML = names.length ? names.slice(0, 120).map(n => `<button type="button" class="${have.has(n.toUpperCase()) ? "have" : ""}" data-n="${esc(n)}" ${have.has(n.toUpperCase()) ? 'aria-disabled="true" title="이미 규칙에 있음"' : ""}>${esc(n)}${seen[n] ? `<b>${seen[n]}</b>` : ""}</button>`).join("") : '<span class="meta">오늘 폴더의 Comm.log 에서 결함명을 찾지 못했습니다 (수집 → 오늘 폴더 파일 보기에서 다른 폴더를 불러올 수 있음)</span>';
  $$("#defSugg button[data-n]").forEach(s => s.onclick = () => { if (s.classList.contains("have")) return;
    let r = S.set.rules.custom.find(x => x.type === "defect" && x.severity === "crit" && (x.count || 1) === 1);
    if (!r) { r = {type: "defect", name: "치명 결함", match: [], severity: "crit", count: 1, window_min: 10}; S.set.rules.custom.push(r); }
    r.match = (r.match || []).concat([s.dataset.n]); markDirty(); renderCustom(); renderSugg(); }); }
$("#btnRt").onclick = async () => { const text = $("#rtText").value; if (!text) { $("#rtText").focus(); return; } const C = S.set.rules.custom;
  if (!C.length) { $("#rtRes").innerHTML = '<span class="meta">사용자 규칙이 없습니다.</span>'; return; }
  const r = await busy($("#btnRt"), () => api("/api/test/rule", {rules: C, text, file: $("#rtFile").value.trim()}));
  $("#rtRes").innerHTML = r.results.map(x => `<div class="row" style="gap:6px;margin:3px 0">${x.ok ? (x.match ? `<span class="tag on">${ic("check", "sm")}걸림</span>` : '<span class="tag">안 걸림</span>') : '<span class="tag c">오류</span>'}<span>${esc(x.name)}</span>${x.ok ? (x.file_ok ? "" : '<span class="warn small">(대상 파일 밖)</span>') : `<span class="crit small">${esc(x.error)}</span>`}</div>`).join(""); };

/* ── 설정: 판단 ───────────────────────────────────────── */
function renderAi() { const on = !!S.set.ai.llm; $$("#aiSeg button").forEach(b => { const o = (b.dataset.v === "1") === on; b.classList.toggle("on", o); b.setAttribute("aria-pressed", o); }); $("#aiBox").classList.toggle("hidden", !on);
  const d = S.set.ai.device || "cpu"; $$("#devSeg button").forEach(b => { b.classList.toggle("on", b.dataset.v === d); b.setAttribute("aria-pressed", b.dataset.v === d); });
  $("#devHelp").textContent = {cpu: "검사 GPU 를 전혀 쓰지 않습니다. 사건당 1~2분, 가장 안전.", gpu: "사건당 3~6초. 검사와 VRAM 을 나눠 씁니다.", auto: "요청마다 여유 VRAM(6GB↑)·사용률(40%↓)을 보고 GPU/CPU 를 고릅니다."}[d]; }
$$("#aiSeg button").forEach(b => b.onclick = () => { S.set.ai.llm = b.dataset.v === "1"; markDirty(); renderAi(); if (S.set.ai.llm) loadModels(); });
$$("#devSeg button").forEach(b => b.onclick = () => { S.set.ai.device = b.dataset.v; markDirty(); renderAi(); });
$("#modelSel").onchange = ev => { S.set.ai.model = ev.target.value; markDirty(); };
async function loadModels() { $("#llmAlive").innerHTML = `<span class="meta">${ic("loader", "spin sm")} 확인 중…</span>`; const r = await api("/api/llm/models?url=" + encodeURIComponent(S.set.ai.url || ""));
  $("#llmAlive").innerHTML = r.alive ? `<span class="badge ok">연결됨 · 모델 ${r.models.length}개</span>` : '<span class="badge crit">응답 없음</span> <span class="meta">Ollama 를 켜거나 주소를 확인하십시오</span>';
  const cur = S.set.ai.model; const ms = Array.from(new Set([cur, ...r.models.filter(m => !/embed/i.test(m))].filter(Boolean)));
  $("#modelSel").innerHTML = ms.map(m => `<option ${m === cur ? "selected" : ""}>${esc(m)}</option>`).join(""); }
$("#btnModels").onclick = () => busy($("#btnModels"), loadModels);
function renderGpuFree() { const g = (S.st && S.st.gpu_free) || [];
  $("#gpuFree").innerHTML = g.length ? `<table><thead><tr><th scope="col">GPU</th><th scope="col">여유 VRAM</th><th scope="col">사용률</th><th scope="col">자동 모드라면</th></tr></thead><tbody>${g.map(x => { const ok = x.free >= 6000 && x.util <= 40;
    return `<tr><td>GPU${x.gpu}</td><td class="mono">${(x.free / 1024).toFixed(1)} / ${(x.total / 1024).toFixed(0)} GB</td><td class="num">${x.util}%</td><td>${ok ? '<span class="tag on">GPU 사용</span>' : '<span class="tag">CPU 로</span>'}</td></tr>`; }).join("")}</tbody></table>` : '<span class="meta">nvidia-smi 없음 — CPU 만 쓸 수 있습니다</span>'; }
$$("[data-llmtest]").forEach(b => b.onclick = async () => { $$("[data-llmtest]").forEach(x => { if (x !== b) x.disabled = true; }); $("#llmTestBusy").textContent = `${b.dataset.llmtest.toUpperCase()} 로 생성 중…`;
  const r = await busy(b, () => api("/api/test/llm", {device: b.dataset.llmtest, url: S.set.ai.url, model: S.set.ai.model}));
  $$("[data-llmtest]").forEach(x => x.disabled = false); $("#llmTestBusy").textContent = "";
  if (!r || !r.ok) { toast("LLM 시험 실패: " + ((r && r.error) || ""), "err"); return; }
  S.tests.unshift({t: hhmmss(), ...r});
  $("#llmTestRows").innerHTML = S.tests.map(x => `<tr><td class="mono">${esc(x.t)}</td><td class="mono">${esc(x.model)}</td><td><b>${esc(x.device.toUpperCase())}</b></td><td class="num">${x.wall_s}초 (로드 ${x.load_s})</td><td class="num">${x.tok_s} 토큰/초</td><td class="small">${esc(x.reply)}</td></tr>`).join(""); });

/* ── 설정: 알림 ───────────────────────────────────────── */
const PF = {
  gmail: [["mail.account", "Gmail 주소 (보내는 계정)", "you@gmail.com"], ["#secret", "앱 비밀번호 (16자리)", ""], ["mail.sender", "보내는 이름 (선택)", "talog <you@gmail.com>"]],
  m365: [["mail.tenant_id", "테넌트 ID 또는 회사 도메인", "company.com"], ["mail.client_id", "앱(클라이언트) ID", "00000000-0000-…"], ["mail.sender", "보낼 사서함", "talog-alert@company.com"], ["#secret", "클라이언트 암호 (값)", ""]],
  smtp: [["mail.host", "SMTP 서버", "smtp.company.com"], ["mail.port", "포트", "587", "number"], ["mail.security", "보안", "", "sec"], ["mail.account", "계정 (비우면 인증 없이)", ""], ["#secret", "비밀번호", ""], ["mail.sender", "보내는 사람", "talog <talog@company.com>"]]};
function renderProv() { const M = S.set.mail, p = M.provider || "off";
  $("#provBox").innerHTML = Object.entries(S.meta.providers).map(([k, v]) => `<label class="${k === p ? "on" : ""}"><input type="radio" name="prov" value="${k}" ${k === p ? "checked" : ""}>${esc(v.label)}</label>`).join("");
  $$("#provBox input").forEach(r => r.onchange = () => { M.provider = r.value; markDirty(); renderProv(); });
  const help = (S.meta.providers[p] || {}).help || ""; $("#provHelp").innerHTML = help ? ic("info") + `<span>${esc(help)}</span>` : ""; $("#provHelp").classList.toggle("hidden", !help);
  $("#mailBox").classList.toggle("hidden", p === "off");
  const saved = (S.origSet && S.origSet.mail.provider === p && S.origSet.mail.account === M.account && S.origSet.mail.has_secret);
  $("#provFields").innerHTML = (PF[p] || []).map(([path, label, ph, t], i) => { const id = `pf-${p}-${i}`; return path === "#secret"
    ? `<div class="field"><label for="secretIn">${esc(label)}</label><input type="password" id="secretIn" autocomplete="new-password" placeholder="${saved ? "저장됨 — 바꿀 때만 입력" : "입력하면 DPAPI 로 암호화해 저장"}"><label class="switch" style="font-weight:500"><input type="checkbox" id="secretClear"><span>저장된 값 지우기</span></label>${S.meta.secret_env ? `<span class="hint" style="margin:0">${esc(S.meta.secret_env)} 가 있으면 그 값을 먼저 씁니다</span>` : ""}</div>`
    : t === "sec" ? `<div class="field"><label for="${id}">${esc(label)}</label><select id="${id}" data-path="${path}"><option value="starttls">STARTTLS (587)</option><option value="ssl">SSL/TLS (465)</option><option value="none">없음 (사내 릴레이)</option></select></div>`
    : `<div class="field"><label for="${id}">${esc(label)}</label><input type="${t === "number" ? "number" : "text"}" id="${id}" data-path="${path}" ${t === "number" ? 'data-type="number"' : ""} placeholder="${esc(ph)}"></div>`; }).join("");
  bind($("#provFields"));
  const si = $("#secretIn"); if (si) { si.value = S.secret; si.oninput = () => { S.secret = si.value; markDirty(); }; $("#secretClear").checked = S.clearSecret; $("#secretClear").onchange = ev => { S.clearSecret = ev.target.checked; markDirty(); }; } }
function renderRoles() { const R = S.set.mail.roles = S.set.mail.roles || {};
  Object.keys(S.meta.roles).forEach(r => { if (!Array.isArray(R[r])) R[r] = R[r] ? [].concat(R[r]) : []; });
  $("#roleBox").innerHTML = Object.entries(S.meta.roles).map(([r, lab]) => `<div class="field"><label>${esc(lab)} <span class="mono opt">(${esc(r)})</span></label><div class="chips" data-path="mail.roles.${r}" data-ph="이메일 입력 후 Enter"></div></div>`).join("");
  bind($("#roleBox")); }
$("#btnMailCheck").onclick = () => busy($("#btnMailCheck"), async () => { $("#mailRes").innerHTML = '<span class="meta">접속 중…</span>'; const r = await api("/api/test/mail-check", {settings: S.set, secret: S.secret});
  $("#mailRes").innerHTML = r.ok ? `<div class="note ok">${ic("check-circle")}<span><b>${r.transport === "graph" ? "토큰 발급 성공" : "접속·인증 성공"}</b> — ${esc(r.detail)} (비밀: ${esc(r.secret_source)})</span></div>` : `<div class="note crit">${ic("x-circle")}<span><b>실패</b> — ${esc(r.detail || r.error)}</span></div>`; });
$("#btnMailTest").onclick = async () => { const n = (S.set.mail.to || []).length;
  if (!(await confirmBox({title: "테스트 메일을 보낼까요?", body: `받는 사람 ${n}명에게 실제로 발송됩니다.`, ok: "보내기"}))) return;
  await busy($("#btnMailTest"), async () => { $("#mailRes").innerHTML = '<span class="meta">보내는 중…</span>'; const r = await api("/api/test/mail", {settings: S.set, secret: S.secret});
    $("#mailRes").innerHTML = r.dry_run ? `<div class="note warn">${ic("info")}<span><b>보관만</b> — <span class="mono">${esc(r.eml)}</span></span></div>` : r.sent ? `<div class="note ok">${ic("check-circle")}<span><b>발송 성공</b> → ${esc((r.to || []).join(", "))}<br><span class="meta">${esc(r.subject)}</span></span></div>` : `<div class="note crit">${ic("x-circle")}<span><b>발송 실패</b> — ${esc(r.error)}</span></div>`; }); };

/* ── 저장 ───────────────────────────────────────────── */
$("#btnSave").onclick = () => busy($("#btnSave"), async () => {
  const r = await api("/api/settings", {settings: S.set, secret: S.secret, clear_secret: S.clearSecret});
  if (!r.ok) { toast("저장 실패\n- " + (r.errors || [r.error]).join("\n- "), "err"); return; }
  S.secret = ""; S.clearSecret = false; setSettings(r.settings); toast("저장했습니다 — 감시를 새 설정으로 다시 시작합니다", "ok"); setTimeout(loadStatus, 900); });
$("#btnRevert").onclick = async () => { if (!(await confirmBox({title: "변경을 되돌릴까요?", body: "저장하지 않은 설정 변경을 모두 버립니다.", ok: "되돌리기", danger: true}))) return;
  S.secret = ""; S.clearSecret = false; setSettings(JSON.parse(S.orig)); toast("변경을 되돌렸습니다"); };
window.addEventListener("beforeunload", ev => { if (isDirty()) { ev.preventDefault(); ev.returnValue = ""; } });
function setSettings(s) {
  S.set = s; S.set.rules.custom = S.set.rules.custom || []; S.set.rules.builtin = S.set.rules.builtin || {};
  S.origSet = JSON.parse(JSON.stringify(s)); renderRoles(); S.orig = JSON.stringify(S.set);
  if (!S.boundOnce) { bind(document); S.boundOnce = true; } else reloadBound();
  renderTracking(); renderBuiltin(); renderCustom(); renderSugg(); renderAi(); renderProv();
  const adv = S.set.advanced || {}; $("#advCard").classList.toggle("hidden", !Object.keys(adv).length); $("#advBox").textContent = JSON.stringify(adv, null, 2);
  markDirty(); }
if ("IntersectionObserver" in window) { const spy = new IntersectionObserver(es => es.forEach(e => { if (e.isIntersecting) $$(".subnav a").forEach(a => { const on = a.dataset.sec === e.target.id; a.classList.toggle("on", on); if (on) a.setAttribute("aria-current", "step"); else a.removeAttribute("aria-current"); }); }), {rootMargin: "-130px 0px -60% 0px"});
  ["s-collect", "s-detect", "s-judge", "s-notify"].forEach(id => spy.observe($("#" + id))); }

