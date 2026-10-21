"use strict";
const TOKEN = "__TALOG_TOKEN__";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const ic = (n, c = "") => `<svg class="ic ${c}" aria-hidden="true"><use href="#i-${n}"/></svg>`;
const SEVK = {crit: "심각", warn: "주의", info: "정보"};
const STK = {ok: "정상", warn: "주의", crit: "심각", "": "대기"};
const VIEWS = {ops: ["운영 현황", "지금 상태 · 할 일 · 오늘 기록"], inc: ["사건", "경보를 묶어 원인과 할 일을 붙인 기록 — 보고 나면 마무리합니다"],
  ana: ["분석", "로그 폴더로 리포트를 만들거나 지난날을 다시 돌려 봅니다"], ai: ["Claude", "구독 Claude 가 talog 데이터와 원본 로그를 직접 읽고 답합니다"], set: ["설정", "로그 폴더 · 경보 규칙 · 판단 · 알림"]};
const S = {set: null, orig: "", meta: null, st: null, folder: null, secret: "", clearSecret: false, view: "ops", inc: null, rep: null, tests: [], incSev: "", incQ: "", openK: {}};
const reduceMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
/** 브라우저 탭 제목 — 마무리 안 된 오늘 심각 사건이 있으면 앞에 (건수) — 다른 창을 보고 있어도 알 수 있게 */
function setTitle() { const n = ((S.st && S.st.incidents) || []).filter(r => r.severity === "crit" && !["resolved", "false_alarm"].includes(r.state || "open")).length;
  document.title = `${n ? `(${n}) ` : ""}${VIEWS[S.view][0]} · talog`; }
const WD = ["일", "월", "화", "수", "목", "금", "토"];
const dayLabel = d => { if (!d || d.length < 8) return d || ""; const dt = new Date(+d.slice(0, 4), +d.slice(4, 6) - 1, +d.slice(6, 8)); return `${+d.slice(4, 6)}월 ${+d.slice(6, 8)}일 (${WD[dt.getDay()]})`; };
const mmdd = d => d && d.length >= 8 ? `${d.slice(4, 6)}/${d.slice(6, 8)}` : (d || "");

async function api(path, body) {
  const opt = body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json", "X-Talog-Token": TOKEN}, body: JSON.stringify(body)};
  const r = await fetch(path, opt);
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok && typeof data === "object") { data._status = r.status; if (data.need) refreshAccess(); }   // 다른 창에서 모드를 내렸다
  return data;
}
function toast(msg, kind = "") {
  const d = document.createElement("div"); d.className = "toast " + kind; d.setAttribute("role", kind === "err" ? "alert" : "status");
  d.innerHTML = `${ic(kind === "ok" ? "check-circle" : kind === "err" ? "alert" : "info", "lg")}<div class="msg"></div><button class="x" type="button" aria-label="알림 닫기">${ic("x")}</button>`;
  $(".msg", d).textContent = msg; $(".x", d).onclick = () => d.remove();
  $("#toast").appendChild(d); setTimeout(() => d.remove(), kind === "err" ? 9000 : 4500); }
function confirmBox({title, body = "", ok = "확인", danger = false}) {
  return new Promise(res => { const d = $("#dlg"); $("#dlgT").textContent = title; $("#dlgB").textContent = body;
    const b = $("#dlgOk"); b.textContent = ok; b.className = "btn " + (danger ? "danger-solid" : "primary");
    d.returnValue = ""; d.onclose = () => res(d.returnValue === "ok"); d.showModal(); (danger ? $("#dlgNo") : b).focus(); }); }
async function busy(btn, fn) { if (!btn || btn.getAttribute("aria-busy") === "true") return;
  btn.setAttribute("aria-busy", "true"); btn.disabled = true;
  try { return await fn(); } finally { btn.removeAttribute("aria-busy"); btn.disabled = false; } }
const getp = (o, p) => p.split(".").reduce((a, k) => (a == null ? undefined : a[k]), o);
function setp(o, p, v) { const ks = p.split("."); let a = o; ks.slice(0, -1).forEach(k => { if (a[k] == null || typeof a[k] !== "object") a[k] = {}; a = a[k]; }); a[ks[ks.length - 1]] = v; }
const sevTag = s => `<span class="sev ${esc(s)}">${esc(SEVK[s] || s || "-")}</span>`;
function fmtSize(n) { if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB"; if (n >= 1024) return (n / 1024).toFixed(0) + " KB"; return n + " B"; }
function gateCls(mode, label) { return mode === "agree" ? "agree" : (mode === "disagree" || mode === "llm_only" || (label || "").startsWith("오경보")) ? "warn" : ""; }
function gateTag(g, label) { const lab = label || (g === "agree" ? "판단 일치" : g === "disagree" ? "불일치" : g === "llm_only" ? "LLM 만" : (g === "rule_only" || g === "rule") ? "룰" : (g || "-"));
  return `<span class="tag ${g === "agree" ? "on" : (g === "disagree" || g === "llm_only") ? "w" : ""}">${esc(lab)}</span>`; }
const emptyRow = (cols, title, hint = "", icon = "inbox") => `<tr class="emptyrow"><td colspan="${cols}"><div class="empty">${ic(icon, "xl")}<b>${esc(title)}</b>${hint ? `<span>${hint}</span>` : ""}</div></td></tr>`;
const emptyBox = (title, hint = "", icon = "inbox") => `<div class="empty">${ic(icon, "xl")}<b>${esc(title)}</b>${hint ? `<span>${hint}</span>` : ""}</div>`;
const skRows = (n, cols = 1) => Array.from({length: n}, (_, i) => `<tr class="skrow"><td colspan="${cols}"><span class="sk" style="width:${88 - (i % 3) * 14}%"></span><span class="sk sm" style="width:${46 - (i % 2) * 12}%"></span></td></tr>`).join("");
const hhmmss = (d = new Date()) => d.toTimeString().slice(0, 8);
const dur = s => s >= 3600 ? (s / 3600).toFixed(1) + "시간" : s >= 60 ? `${Math.floor(s / 60)}분 ${s % 60}초` : `${s}초`;
const tail2 = p => String(p || "").split(/[\\/]/).filter(Boolean).slice(-2).join("\\");

