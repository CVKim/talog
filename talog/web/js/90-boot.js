/* ── 시작 ───────────────────────────────────────────── */
(async function init() {
  $("#opsInc").innerHTML = skRows(4); $("#capRows").innerHTML = skRows(3, 7);
  const r = await api("/api/settings"); S.meta = r.meta;
  $("#cfgPath").textContent = r.meta.config;
  $("#legacyTag").innerHTML = r.meta.legacy ? '<span class="tag w">옛 watch.yaml 형식 — 저장하면 새 형식(값 그대로)으로 바뀝니다</span>' : "";
  $("#presetSel").innerHTML = '<option value="">예시에서 추가…</option>' + r.meta.presets.map((p, i) => `<option value="${i}">${esc(p.name)} (${SEVK[p.severity]}, ${p.count}회/${p.window_min}분)</option>`).join("");
  setSettings(r.settings); applyAccess(r.meta.access); initMore(); $("#pageSub").textContent = VIEWS[S.view][1]; applyHash(); await loadStatus(); loadTimeline(); if (S.set.ai.llm) loadModels();
  api("/api/folder").then(f => { S.folder = S.folder || f; renderFiles(); renderSugg(); });
  ensureClaude();
  maybeTour();                                        // 처음 여는 브라우저에서 한 번 (건너뛸 수 있음)
  setInterval(() => { loadStatus(); if ($("#logDetails").open) loadLog(); }, 5000);
  setInterval(() => { if (S.view === "ops") loadTimeline(); }, 60000);
})();
