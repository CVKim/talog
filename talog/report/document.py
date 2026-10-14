"""리포트 한 장을 조립한다 — 타일·구역 HTML 과 assets/ 의 스타일·스크립트를 합친 독립 실행 HTML."""

from __future__ import annotations

import json
import os
import statistics
from collections import Counter

from .. import __version__ as _VER
from .charts import _duration_hist_svg, _hourly_svg, _timeline_summary, _timeline_svg
from .context import _BAD_STATUSES, _STATUS_COLOR, _STATUS_KO, ReportContext
from .errors import _errors_section, _errors_summary
from .findings import _findings_section
from .inspections import _gens_section, _incomplete_section, _ng_section, _tact_section
from .models import _model_bar_summary, _models_section
from .payload import _build_graph, _build_payload
from .resources import _gpu_mini, _gpu_resource_section, _usage_analysis, _usage_mini, _usage_section
from .util import _esc


HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")                                         # report.css · report.js
WORDMARK = os.path.join(os.path.dirname(HERE), "assets", "aivex_wordmark.svg")   # 콘솔과 같은 로고


def _asset(name: str) -> str:
    """assets/ 의 스타일·스크립트 원문. 줄바꿈은 LF 로 읽는다 — Windows 사본(CRLF)에서도 같은 리포트가 나오게."""
    with open(os.path.join(ASSETS, name), encoding="utf-8") as f:
        return f.read()


def _wordmark() -> str:
    """AIVEX 워드마크 (assets 의 SVG 를 그대로 싣는다 — 리포트는 파일 하나로 열린다)."""
    try:
        with open(WORDMARK, encoding="utf-8") as f:
            svg = f.read().strip()
        return svg.replace("<svg ", "<svg class='wm' ", 1)
    except OSError:
        return "<b class='wm-txt'>AIVEX</b>"


# 페이지 스크립트·스타일 — 리포트는 파일 하나로 열리므로 본문에 그대로 싣는다.
# 스크립트의 __SUMMARY__ 같은 자리표시는 render 가 자료(JSON)로 바꾼다.
_JS = _asset("report.js")
_CSS = _asset("report.css")


def render(ctx: ReportContext) -> str:
    n = Counter(i.status for i in ctx.inspections)
    total = len(ctx.inspections)
    durations = [(i.end_ts - i.start_ts) for i in ctx.inspections
                 if i.end_ts and i.start_ts and i.end_ts > i.start_ts
                 and i.status == "complete"]
    avg_dur = statistics.mean(durations) if durations else 0
    max_dur = max(durations) if durations else 0

    r = ctx.recipe
    recipe_html = ""
    if r:
        recipe_html = (f"레시피 <b>{_esc(r.root)}</b> / 버전 <b>{_esc(r.version)}</b> — "
                       f"alg {r.alg_count}개, Seq 스레드 <b>{r.thread_count}</b>개, "
                       f"모델 {len(r.models)}개")
    stitch_note = (" · 익일 첫 구간 스티칭 적용" if ctx.stitched else "")

    n_sim = n.get("sim_complete", 0) + n.get("sim_partial", 0)
    n_bad = sum(n.get(k, 0) for k in _BAD_STATUSES)
    # 여러 존이 inner 를 나눠 쓰는 설비는 뒤 존의 거부(NoInspThread)가 앞 존의 '완료' 검사에 묶인다 —
    # 소견(diagnose._reject_finding)과 같은 방식으로 세어 조립에 안 잡힌 거부만 더한다 (풍산 10/12: 이상 0 → 3)
    n_rej_ev = max(sum(1 for e in ctx.events if e.kind == "COMM_MSG" and "INSPECT_START_ACK" in e.name
                       and e.status == "NoInspThread"),
                   sum(1 for e in ctx.events if e.kind == "INSP_REJECT"))
    n_bad += max(0, n_rej_ev - n.get("rejected", 0))
    n_ng = sum(1 for i in ctx.inspections if i.end_result == "NG")
    n_err = sum(1 for e in ctx.events
                if e.kind in ("ERROR", "MODEL_FAIL", "CRASH", "COMM_FAIL",
                              "RECIPE_FAIL", "EXC_REDIRECT", "EXC_SAFE",
                              "FOV_FAIL", "GPU_FATAL")
                and not e.extra.startswith("[WARNING]"))
    # (라벨, 값, 색, 클릭 시 이동) — 이동이 'main-bad' 면 종합 페이지 내 스크롤
    # 히어로 카드: "이 설비가 지금 아픈가?" 에 즉답
    n_crit = sum(1 for f in ctx.findings if f.severity == "crit")
    n_warn = sum(1 for f in ctx.findings if f.severity == "warn")
    n_restart = max(0, len(ctx.gens) - 1)
    if n_crit:
        h_cls, h_col = "h-crit", "var(--crit-text)"
        h_val, h_lbl = f"심각 {n_crit}건", "설비 상태 — 조치 필요"
    elif n_warn:
        h_cls, h_col = "h-warn", "var(--serious-text)"
        h_val, h_lbl = f"주의 {n_warn}건", "설비 상태 — 관찰 필요"
    else:
        h_cls, h_col = "h-ok", "var(--good-text)"
        h_val, h_lbl = "정상", "설비 상태"
    h_sub = (f"미완료 {n_bad} · NG {n_ng} · 에러 {n_err} · 재시작 {n_restart}")
    hero = (f"<div class='card hero {h_cls}'>"
            f"<div class='num' style='color:{h_col}'>{h_val}</div>"
            f"<div class='lbl'>{h_lbl}</div>"
            f"<div class='sub'>{h_sub}</div></div>")

    cards = [
        ("검사 수", str(total), "var(--ink)", "sec-insp"),
        ("완료", str(n.get("complete", 0)), "var(--good-text)", "sec-insp"),
        ("이상", str(n_bad),
         "var(--serious-text)" if n_bad else "var(--good-text)", "main-bad"),
        ("NG 판정", str(n_ng),
         "var(--serious-text)" if n_ng else "var(--good-text)", "main-bad"),
        ("에러", str(n_err),
         "var(--crit-text)" if n_err else "var(--good-text)", "sec-err"),
        ("평균 검사시간", f"{avg_dur:.2f}s" if avg_dur else "-", "var(--ink)",
         "sec-tact"),
        ("최대 검사시간", f"{max_dur:.2f}s" if max_dur else "-", "var(--ink)",
         "sec-tact"),
        ("재시작", str(n_restart), "var(--blue)", "sec-sys"),
        ("시뮬레이션", str(n_sim), "var(--aqua)", "sec-insp"),
    ]

    def _numfmt(v: str) -> str:
        return (f"{v[:-1]}<span class='unit'>s</span>"
                if v.endswith("s") and v != "-" else v)

    # 타일 캡션 — 숫자만 두지 않고 그 숫자가 무엇인지 한 줄로
    desc = {"검사 수": "이 폴더에서 시작된 검사", "완료": "결과 송신까지 끝난 검사",
            "이상": "거부·실행 중 소실 등 끝나지 못한 검사", "NG 판정": "제품 결함으로 판정 (설비 이상 아님)",
            "에러": "오류·크래시·통신 실패 로그", "평균 검사시간": "시작 ~ 결과 송신 평균",
            "최대 검사시간": "가장 오래 걸린 한 건", "재시작": "talos 프로세스가 다시 뜬 횟수",
            "시뮬레이션": "설비 없이 돌린 시험 검사"}
    card_html = hero + "".join(
        f"<div class='card' onclick=\"{'gotoSection' if t != 'main-bad' else 'scrollMain'}"
        f"('{t}')\"><div class='num' style='color:{c}'>{_numfmt(v)}</div>"
        f"<div class='lbl'>{k}</div><div class='desc'>{desc.get(k, '')}</div></div>" for k, v, c, t in cards)

    files_html = "".join(
        f"<tr><td>{_esc(nm)}</td><td>{_esc(cat)}</td><td class='r'>{sz / 1048576:.1f}</td>"
        f"<td class='r'>{rec:,}</td><td class='r'>{ev:,}</td></tr>"
        for nm, cat, sz, rec, ev in ctx.file_rows)

    ua = _usage_analysis(ctx)
    leak_banner = ""
    if ua is not None and ua[4]:
        leak_banner = (f"<p style='background:var(--tint-crit);border:1px solid var(--tint-crit-bd);"
                       f"border-radius:6px;padding:10px;margin-top:14px'>"
                       f"<b>⚠ 메모리 증가 추세 의심</b> — {ua[3]:.1f}시간 동안 "
                       f"+{ua[2]:,.0f}MB (≈ {ua[1]:,.0f}MB/h). 시스템 탭에서 "
                       f"RAM 추이를 확인하세요.</p>")

    summary_json, detail_json = _build_payload(ctx)
    js = (_JS.replace("__SUMMARY__", summary_json)
             .replace("__TOTAL__", str(total))
             .replace("__DETAIL__", detail_json)
             .replace("__GRAPH__", _build_graph(ctx))
             .replace("__STATUS_KO__", json.dumps(_STATUS_KO, ensure_ascii=False))
             .replace("__STATUS_COLOR__", json.dumps(_STATUS_COLOR)))

    return f"""<!DOCTYPE html>
<html lang="ko" data-theme="dark"><head><meta charset="utf-8">
<title>talog — {_esc(ctx.title)}</title>
<script>try{{var t=localStorage.getItem('talog.theme')||localStorage.getItem('talog-theme');
if(t==='light'||t==='dark')document.documentElement.dataset.theme=t;
else if(window.matchMedia&&matchMedia('(prefers-color-scheme: light)').matches)document.documentElement.dataset.theme='light';
}}catch(e){{}}</script>
<style>
{_CSS}</style></head><body>
<header>
<div class="topbar">
 <div class="brand">{_wordmark()}<span class="bsep"></span><span class="word">talog</span>
  <span class="ver">v{_VER}</span></div>
 <div class="crumb">{_esc(ctx.title)} 진단 리포트
  <span class="meta">{_esc(ctx.day_dir)}{stitch_note}
  {('· ' + recipe_html) if recipe_html else ''}</span></div>
 <div class="tspacer"></div>
 <button class="themebtn" id="themeBtn" type="button" onclick="toggleTheme()">라이트</button>
</div>
<nav>
 <button class="tabbtn" id="btn-main" onclick="showTab('main')">종합</button>
 <button class="tabbtn" id="btn-detail" onclick="showTab('detail')">상세 분석</button>
</nav>
</header>
<main>
<div class="tabpane" id="pane-main">
 <div class="cards">{card_html}</div>
 {leak_banner}
 <h2>타임라인 <span class="hint">— 굵은 선(이상 검사)·▼재시작·✖종료·▲모델로드를
 클릭/호버해 보세요</span></h2>{_timeline_svg(ctx)}{_timeline_summary(ctx)}
 {_findings_section(ctx)}
 <div id="main-bad"></div>
 <h2>미완료/이상 검사 <span class="hint">— inner id 클릭 시 채널별 간트로
 이동</span></h2>{_incomplete_section(ctx)}
 {_ng_section(ctx)}
 <div class="cols">
  <div class="col">
   <h2>모델별 평균 검사시간 <span class="hint">— 클릭 시 상세</span></h2>
   {_model_bar_summary(ctx)}
  </div>
  <div class="col">
   <h2>에러 요약 <span class="hint">— 클릭 시 전체</span></h2>
   {_errors_summary(ctx)}
   <h2>메모리</h2>
   {_usage_mini(ctx)}
   {_gpu_mini(ctx)}
  </div>
 </div>
 <h2>시간대별 검사 수 / 평균 검사시간</h2>{_hourly_svg(ctx)}
</div>
<div class="tabpane" id="pane-detail">
 <div class="subnav">
  <a onclick="gotoSection('sec-insp')">검사 조회</a>
  <a onclick="gotoSection('sec-graph')">종속성 그래프</a>
  <a onclick="gotoSection('sec-tact')">채널 Tact</a>
  <a onclick="gotoSection('sec-model')">모델·GPU</a>
  <a onclick="gotoSection('sec-err')">에러 전체</a>
  <a onclick="gotoSection('sec-sys')">시스템·GPU</a>
 </div>
 <section id="sec-insp">
  <h2>검사 조회</h2>
  <p><input type="text" id="insp-search" placeholder="inner id 또는 product id 검색">
     <span style="color:var(--muted);font-size:12px;margin-left:10px">시간대</span>
     <input type="time" id="t-from" class="tin" onchange="PAGE_OFF=0;renderList()">
     <span style="color:var(--muted)">~</span>
     <input type="time" id="t-to" class="tin" onchange="PAGE_OFF=0;renderList()">
     <button class="fbtn on" data-f="all" onclick="setFilter('all')">전체</button>
     <button class="fbtn" data-f="bad" onclick="setFilter('bad')">이상만</button>
     <button class="fbtn" data-f="ng" onclick="setFilter('ng')">NG</button>
     <button class="fbtn" data-f="ok" onclick="setFilter('ok')">완료</button>
     <button class="fbtn" data-f="eof" onclick="setFilter('eof')">절단</button>
     <button class="fbtn" id="sortBtn" onclick="toggleSort()"
             style="margin-left:14px">정렬: 최신순 ▾</button>
     <span id="pageNav" style="display:none">
       <button class="fbtn" onclick="movePage(-400)">◀ 이전</button>
       <button class="fbtn" onclick="movePage(400)">다음 ▶</button>
     </span>
     <button class="fbtn" onclick="exportCsv()">⬇ CSV 내보내기</button>
     <span id="insp-count" style="color:var(--muted);font-size:13px;margin-left:10px"></span></p>
  <div id="gantt-box" style="margin:14px 0"></div>
  <table><thead><tr><th>시작</th><th>inner id</th><th>product id</th><th>상태</th>
  <th>판정</th><th class='r'>검사시간</th><th class='r'>완료/투입</th></tr></thead>
  <tbody id="insp-body"></tbody></table>
 </section>
 <section id="sec-graph">
  <h2>종속성 그래프 <span class="hint">— 레시피의 이미지→ROI→알고리즘 연결.
  검사 조회에서 inner id 를 선택하면 해당 검사의 상태로 색칠됩니다</span></h2>
  <p id="graph-mode" class="legend"></p>
  <div id="graph-box"></div>
 </section>
 <section id="sec-tact">
  <h2>채널별 인퍼런스 Tact</h2>{_tact_section(ctx)}
  <h2>검사시간 분포 (완료 건) <span class="hint">— 첫 START→마지막 END
  (다존 설비는 존 이송 시간 포함). 파랑=하위 95%, 주황=상위 5% 꼬리,
  굵은 선=p95. 축은 p99×1.25 에서 절단(max 는 주석)</span></h2>
  {_duration_hist_svg(ctx)}
 </section>
 <section id="sec-model">{_models_section(ctx)}</section>
 <section id="sec-err">
  <h2>에러/예외 전체</h2>{_errors_section(ctx)}
 </section>
 <section id="sec-sys">
  {_usage_section(ctx)}
  {_gpu_resource_section(ctx)}
  <h2>프로세스 세대 (재시작 이력)</h2>{_gens_section(ctx)}
  <h2>파싱된 파일</h2>
  <table class='sortable'><thead><tr><th>파일</th><th>분류</th><th class='r'>MB</th>
  <th class='r'>레코드</th><th class='r'>이벤트</th></tr></thead>
  <tbody>{files_html}</tbody></table>
 </section>
</div>
</main>
<script>
{js}</script>
</body></html>"""
