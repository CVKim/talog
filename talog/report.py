"""단일 파일 인터랙티브 HTML 진단 리포트 (외부 의존 없음: 인라인 CSS/JS/SVG).

탭 구성: 대시보드 / 검사 조회(검색+간트) / Tact / 에러 / 시스템.
검사 요약은 전수를 JSON 으로 내장하고, 채널 단위 상세(간트용)는
이상 검사 + 검사시간 상위 N + 최근 N 건에 대해서만 내장해 파일 크기를 억제한다.
"""

from __future__ import annotations

import html
import json
import statistics
from collections import Counter
from dataclasses import dataclass, field

from .assemble import ChannelRun, Inspection, ModelLoad, ProcessGen
from .diagnose import Finding
from .events import Event
from .recipe import Recipe

_STATUS_KO = {
    "complete": "완료",
    "rejected": "시작 거부",
    "incomplete_lost": "실행 중 소실",
    "incomplete": "미완료",
    "in_progress_eof": "로그 절단(판정 불가)",
    "sim_complete": "시뮬레이션 완료",
    "sim_partial": "시뮬레이션 부분 실행",
    "unknown": "불명",
}
# 디자인 토큰 — dataviz 검증 팔레트(레퍼런스 인스턴스, 라이트 서피스 기준)
# 상태색은 status 팔레트, 크기(막대)는 시퀀셜 blue, 보조 시퀀셜은 orange.
_STATUS_COLOR = {
    "complete": "#0ca30c",            # status: good
    "rejected": "#d03b3b",            # status: critical
    "incomplete": "#d03b3b",
    "unknown": "#d03b3b",
    "incomplete_lost": "#ec835a",     # status: serious
    "in_progress_eof": "#898781",     # muted (판정 불가)
    "sim_complete": "#1baf7a",        # categorical slot3 (중립 계열)
    "sim_partial": "#898781",
}
_OK_STATUSES = ("complete", "in_progress_eof", "sim_complete")
_BAD_STATUSES = ("rejected", "incomplete_lost", "incomplete", "unknown")


@dataclass
class ReportContext:
    title: str
    day_dir: str
    recipe: Recipe | None
    inspections: list[Inspection]
    runs: list[ChannelRun]
    gens: list[ProcessGen]
    events: list[Event]
    dl_channels: dict[int, str]
    file_rows: list[tuple] = field(default_factory=list)
    log_start: float = 0.0
    log_end: float = 0.0
    stitched: bool = False       # 익일 첫 구간 스티칭 적용 여부
    detail_cap: int = 60         # 상세(간트) 내장 검사 수 상한
    model_loads: list[ModelLoad] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    llm_summary: str = ""        # 로컬 LLM 종합 소견 (--llm 옵션)
    similar_cases: list = field(default_factory=list)   # (유사도,제목,원인,조치,site,date)
    gpu_watch: list = field(default_factory=list)  # TalogWatch gpu_*.jsonl 샘플
    #   (ts_epoch, gpu_idx, temp_c, util_pct, mem_mb)


def _esc(v) -> str:
    return html.escape(str(v))


def _fmt_ts(text: str) -> str:
    return text[:12] if text else "-"


def _translate_alglist(recipe: Recipe | None, lst: str) -> str:
    if not lst:
        return ""
    out = []
    for tok in lst.split(","):
        tok = tok.strip()
        if tok.isdigit() and recipe:
            out.append(f"{tok}({recipe.alg_name(int(tok))})")
        elif tok:
            out.append(tok)
    return ", ".join(out)


# ---------------------------------------------------------------------------
# 데이터 직렬화 (클라이언트 JS 용)
# ---------------------------------------------------------------------------
def _build_payload(ctx: ReportContext) -> tuple[str, str]:
    """(요약 JSON, 상세 JSON) 을 만든다. 고케이던스 사이트는 요약을 상한한다."""
    src = ctx.inspections
    if len(src) > 9000:
        bad = [i for i in src if i.status in _BAD_STATUSES]
        src = bad + [i for i in src if i.status not in _BAD_STATUSES][-9000:]
        src.sort(key=lambda i: i.start_ts or 0)
    summary = []
    for it in src:
        summary.append([
            it.inner_id, it.product_id, round(it.start_ts, 3), it.start_text[:12],
            it.status, round((it.end_ts - it.start_ts), 2) if it.end_ts and it.start_ts else 0,
            it.n_done, it.n_fed, it.ack_status, it.end_result,
        ])

    # 상세 대상 선정: 이상 전건 + 검사시간 상위 + 최근
    by_inner: dict[str, list[ChannelRun]] = {}
    for r in ctx.runs:
        by_inner.setdefault(r.inner_id, []).append(r)

    picked: list[Inspection] = [i for i in ctx.inspections if i.status in _BAD_STATUSES]
    done = [i for i in ctx.inspections if i.status == "complete" and i.end_ts]
    done.sort(key=lambda i: i.end_ts - i.start_ts, reverse=True)
    for i in done[: max(0, ctx.detail_cap - len(picked)) // 2]:
        picked.append(i)
    rest = max(0, ctx.detail_cap - len(picked))
    for i in sorted(done, key=lambda i: i.start_ts, reverse=True)[:rest]:
        if i not in picked:
            picked.append(i)

    detail: dict[str, dict] = {}
    for it in picked:
        rs = sorted(by_inner.get(it.inner_id, []), key=lambda r: (r.infer_start_ts, r.alg_idx))
        base = it.start_ts or (rs[0].infer_start_ts if rs else 0)
        detail[it.inner_id] = {
            "st": it.start_text[:12], "status": it.status,
            "remain": _translate_alglist(ctx.recipe, it.remain_list),
            "lost": it.lost_channels, "nofeed": it.nofeed_channels[:30],
            "lostIdx": it.lost_idx, "nofeedIdx": it.nofeed_idx,
            "skipIdx": it.skipped_idx,
            "runs": [[r.alg_idx, r.channel[:28], r.exec_no,
                      round(r.infer_start_ts - base, 2),
                      round(r.infer_ms / 1000, 2) if r.infer_ms else 0,
                      r.status, r.model[:40], round(r.pre_ms, 1)] for r in rs],
        }

    def dumps(o) -> str:
        return json.dumps(o, ensure_ascii=False, separators=(",", ":")) \
                   .replace("</", "<\\/")

    return dumps(summary), dumps(detail)


def _build_graph(ctx: ReportContext) -> str:
    """종속성 그래프 데이터(JSON): IMG → ROI → ALG 계층 + 일자 요약 통계.

    레시피가 있으면 recipe 의 requireroiidx/baseimgidx 로, 없으면 관측된
    FEED(roi)→ALG 관계로 축약 구성한다.
    """
    day_stats: dict[int, dict] = {}
    for r in ctx.runs:
        s = day_stats.setdefault(r.alg_idx, {"done": 0, "lost": 0})
        if r.status == "done":
            s["done"] += 1
        elif r.status == "lost":
            s["lost"] += 1

    algs = []
    rois: dict[int, dict] = {}
    imgs: dict[int, dict] = {}
    if ctx.recipe and ctx.recipe.algs:
        for idx, a in sorted(ctx.recipe.algs.items()):
            if idx not in ctx.dl_channels and idx not in day_stats:
                # 로그에 없는 alg 도 레시피 구조는 보여준다 (rb/dummy 포함)
                pass
            st = day_stats.get(idx, {"done": 0, "lost": 0})
            algs.append({"id": idx, "label": (a.name or a.alg_id)[:30],
                         "roi": a.roi_idx, "done": st["done"], "lost": st["lost"],
                         "dl": idx in ctx.dl_channels})
            for ri in a.roi_idx:
                r = ctx.recipe.rois.get(ri)
                rois.setdefault(ri, {"id": ri,
                                     "label": (r.name if r else f"ROI{ri}")[:24],
                                     "img": r.base_img if r else 0})
                if r and r.base_img:
                    imgs.setdefault(r.base_img, {"id": r.base_img,
                                                 "label": f"IMG {r.base_img}"})
        has_recipe = True
    else:
        # 관측 기반 축약: 채널 런의 roi_idx → alg
        roi_of_alg: dict[int, set] = {}
        for r in ctx.runs:
            if r.roi_idx:
                roi_of_alg.setdefault(r.alg_idx, set()).add(r.roi_idx)
        for idx, ch in sorted(ctx.dl_channels.items()):
            st = day_stats.get(idx, {"done": 0, "lost": 0})
            rlist = sorted(roi_of_alg.get(idx, []))
            algs.append({"id": idx, "label": ch[:30], "roi": rlist,
                         "done": st["done"], "lost": st["lost"], "dl": True})
            for ri in rlist:
                rois.setdefault(ri, {"id": ri, "label": f"ROI{ri}", "img": 0})
        has_recipe = False

    graph = {"imgs": sorted(imgs.values(), key=lambda x: x["id"]),
             "rois": sorted(rois.values(), key=lambda x: x["id"]),
             "algs": algs, "hasRecipe": has_recipe}
    return json.dumps(graph, ensure_ascii=False, separators=(",", ":")) \
               .replace("</", "<\\/")


# ---------------------------------------------------------------------------
# SVG 조각들 (서버 측 렌더)
# ---------------------------------------------------------------------------
def _timeline_svg(ctx: ReportContext) -> str:
    if not ctx.log_end or ctx.log_end <= ctx.log_start:
        return "<p>타임라인 데이터 없음</p>"
    w, h = 1120, 104
    span = ctx.log_end - ctx.log_start

    def x(ts: float) -> float:
        return 40 + (ts - ctx.log_start) / span * (w - 80)

    parts = [f'<svg viewBox="0 0 {w} {h}" style="width:100%;background:#fcfcfb;'
             f'border:1px solid #e1e0d9;border-radius:6px">']
    import datetime as _dt
    t0 = _dt.datetime.fromtimestamp(ctx.log_start)
    tick = _dt.datetime(t0.year, t0.month, t0.day, t0.hour).timestamp()
    while tick <= ctx.log_end:
        if tick >= ctx.log_start:
            hh = _dt.datetime.fromtimestamp(tick).strftime("%H시")
            parts.append(f'<line x1="{x(tick):.1f}" y1="20" x2="{x(tick):.1f}" y2="76" '
                         f'stroke="#e1e0d9"/><text x="{x(tick):.1f}" y="94" font-size="11" '
                         f'text-anchor="middle" fill="#898781">{hh}</text>')
        tick += 3600
    # 검사 틱 — 대량(3천 건 초과) 사이트는 분 단위 밀도 스트립으로 전환
    if len(ctx.inspections) > 3000:
        per_min: dict[int, int] = {}
        for it in ctx.inspections:
            if it.start_ts:
                b = int(it.start_ts // 60)
                per_min[b] = per_min.get(b, 0) + 1
        mx = max(per_min.values()) if per_min else 1
        for b, cnt in per_min.items():
            hh2 = 8 + cnt / mx * 32
            parts.append(f'<rect x="{x(b * 60):.1f}" y="{68 - hh2:.0f}" width="1.6" '
                         f'height="{hh2:.0f}" fill="#86b6ef">'
                         f'<title>{cnt}건/분</title></rect>')
        for it in ctx.inspections:
            if it.start_ts and it.status in _BAD_STATUSES:
                c = _STATUS_COLOR.get(it.status, "#d03b3b")
                parts.append(
                    f'<line x1="{x(it.start_ts):.1f}" y1="26" '
                    f'x2="{x(it.start_ts):.1f}" y2="72" stroke="{c}" '
                    f'stroke-width="3" class="tl" data-id="{_esc(it.inner_id)}" '
                    f'style="cursor:pointer"><title>{_esc(it.inner_id)} '
                    f'{_STATUS_KO.get(it.status, it.status)} '
                    f'{_fmt_ts(it.start_text)}</title></line>')
    else:
        for it in ctx.inspections:
            if not it.start_ts:
                continue
            c = _STATUS_COLOR.get(it.status, "#898781")
            bad = it.status in _BAD_STATUSES
            yy, hh2, sw = (44, 24, 1.5) if not bad else (30, 46, 3)
            parts.append(
                f'<line x1="{x(it.start_ts):.1f}" y1="{yy}" x2="{x(it.start_ts):.1f}" '
                f'y2="{yy + hh2}" stroke="{c}" stroke-width="{sw}" class="tl" '
                f'data-id="{_esc(it.inner_id)}" style="cursor:pointer">'
                f'<title>{_esc(it.inner_id)} {_STATUS_KO.get(it.status, it.status)} '
                f'{_fmt_ts(it.start_text)}</title></line>')
    for g in ctx.gens:
        if g.start_text != "(전일부터 가동)":
            parts.append(f'<polygon points="{x(g.start_ts):.1f},16 {x(g.start_ts) - 5:.1f},5 '
                         f'{x(g.start_ts) + 5:.1f},5" fill="#2a78d6">'
                         f'<title>프로세스 시작 {g.start_text}</title></polygon>')
        if g.end_cause in ("crash", "kill", "destroy") and g.end_ts:
            parts.append(f'<text x="{x(g.end_ts):.1f}" y="16" font-size="12" '
                         f'text-anchor="middle" fill="#d03b3b">✖<title>종료({g.end_cause}) '
                         f'{g.end_text}</title></text>')
    # 모델 로드 이벤트 마커: 레시피 로드 명령(보라 ▲), 로드 실패(빨간 ▲)
    for m in ctx.model_loads:
        if m.kind == "recipe_load":
            col = "#4a3aa7" if m.status == "OK" else "#d03b3b"
            parts.append(f'<text x="{x(m.ts):.1f}" y="{h - 20}" font-size="12" '
                         f'text-anchor="middle" fill="{col}">▲<title>모델 로드 '
                         f'{_esc(m.name)} {m.ts_text[:12]} ({_esc(m.status)}, '
                         f'{m.dur_s:.0f}s)</title></text>')
    for e in ctx.events:
        if e.kind == "MODEL_FAIL":
            parts.append(f'<text x="{x(e.ts):.1f}" y="{h - 20}" font-size="12" '
                         f'text-anchor="middle" fill="#d03b3b">▲<title>모델 로드 실패 '
                         f'{_esc(e.model)} {e.ts_text[:12]}</title></text>')
    parts.append("</svg>")
    legend = ('<div class="legend">범례: <span style="color:#0ca30c">│완료</span> '
              '<span style="color:#ec835a">┃실행 중 소실</span> '
              '<span style="color:#d03b3b">┃시작 거부 · ✖프로세스 종료</span> '
              '<span style="color:#2a78d6">▼프로세스 시작</span> · 굵은 선을 클릭하면 '
              '해당 검사 상세로 이동합니다</div>')
    return "".join(parts) + legend


def _hourly_svg(ctx: ReportContext) -> str:
    """시간대별 검사 수 + 평균 검사시간 추이."""
    import datetime as _dt
    per_hour: dict[int, list[float]] = {}
    for it in ctx.inspections:
        if not it.start_ts or it.status.startswith("sim"):
            continue
        hh = _dt.datetime.fromtimestamp(it.start_ts).hour
        d = (it.end_ts - it.start_ts) if it.end_ts else 0
        per_hour.setdefault(hh, []).append(d)
    if not per_hour:
        return ""
    hours = sorted(per_hour)
    maxn = max(len(v) for v in per_hour.values())
    durs = {h: statistics.mean([d for d in v if d > 0] or [0]) for h, v in per_hour.items()}
    maxd = max(durs.values()) or 1
    # 버킷이 적을 때(짧은 가동일) 막대가 화면 폭을 가득 채우지 않도록 폭 상한
    bw = min(64.0, 1030 / max(len(hours), 1))
    w, h = max(260, int(90 + bw * len(hours) + 10)), 150
    parts = [f'<svg viewBox="0 0 {w} {h}" '
             f'style="width:100%;max-width:{w}px;display:block">']
    for i, hh in enumerate(hours):
        n = len(per_hour[hh])
        bh = n / maxn * 90
        xx = 50 + i * bw
        parts.append(f'<rect x="{xx:.0f}" y="{110 - bh:.0f}" width="{bw * 0.55:.0f}" '
                     f'height="{bh:.0f}" fill="#2a78d6"><title>{hh}시 검사 {n}건</title></rect>')
        dh = durs[hh] / maxd * 90
        parts.append(f'<rect x="{xx + bw * 0.58:.0f}" y="{110 - dh:.0f}" '
                     f'width="{bw * 0.28:.0f}" height="{dh:.0f}" fill="#eb6834">'
                     f'<title>{hh}시 평균 검사시간 {durs[hh]:.2f}초</title></rect>')
        parts.append(f'<text x="{xx + bw * 0.4:.0f}" y="126" font-size="10" '
                     f'text-anchor="middle" fill="#898781">{hh}시</text>')
    parts.append(f'<text x="50" y="145" font-size="11" fill="#2a78d6">■ 검사 수</text>'
                 f'<text x="120" y="145" font-size="11" fill="#eb6834">■ 평균 검사시간(s)</text>')
    parts.append("</svg>")
    return "".join(parts)


def _duration_hist_svg(ctx: ReportContext) -> str:
    durs = [(i.end_ts - i.start_ts) for i in ctx.inspections
            if i.end_ts and i.start_ts and i.status == "complete"]
    if len(durs) < 3:
        return ""
    lo, hi = min(durs), max(durs)
    nb = 24
    step = (hi - lo) / nb or 1
    bins = [0] * nb
    for d in durs:
        bins[min(nb - 1, int((d - lo) / step))] += 1
    mx = max(bins) or 1
    w, h = 1120, 130
    bw = (w - 90) / nb
    parts = [f'<svg viewBox="0 0 {w} {h}" style="width:100%">']
    for i, n in enumerate(bins):
        bh = n / mx * 85
        parts.append(f'<rect x="{50 + i * bw:.0f}" y="{100 - bh:.0f}" '
                     f'width="{bw * 0.85:.0f}" height="{bh:.0f}" fill="#86b6ef">'
                     f'<title>{lo + i * step:.0f}~{lo + (i + 1) * step:.0f}초: {n}건</title></rect>')
    for frac in (0, 0.5, 1.0):
        xx = 50 + frac * (nb * bw)
        parts.append(f'<text x="{xx:.0f}" y="118" font-size="10" text-anchor="middle" '
                     f'fill="#898781">{lo + frac * (hi - lo):.0f}s</text>')
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# 표 섹션들
# ---------------------------------------------------------------------------
def _tact_section(ctx: ReportContext) -> str:
    by_ch: dict[tuple[int, str], list[tuple[float, float]]] = {}
    for r in ctx.runs:
        if r.status == "done" and r.infer_ms > 0:
            by_ch.setdefault((r.alg_idx, r.channel), []).append((r.infer_start_ts, r.infer_ms))
    if not by_ch:
        return "<p>인퍼런스 Tact 데이터 없음</p>"
    stats = []
    for (idx, ch), pairs in by_ch.items():
        vals = sorted(v for _t, v in pairs)
        stats.append((idx, ch, pairs, vals, statistics.mean(vals)))
    stats.sort(key=lambda s: -s[4])
    max_avg = stats[0][4] or 1
    rows = []
    for idx, ch, pairs, vals, avg in stats:
        p95 = vals[min(len(vals) - 1, int(len(vals) * 0.95))]
        gpu = ""
        if ctx.recipe:
            ms = ctx.recipe.alg_models(idx)
            gpu = ", ".join(sorted({f"GPU{m.dev_index}" for m in ms}))
        # 시계열 스파크라인 (최대 200pt 샘플)
        pts = sorted(pairs)[:: max(1, len(pairs) // 200)]
        if len(pts) >= 2 and ctx.log_end > ctx.log_start:
            mx = vals[-1] or 1
            poly = " ".join(
                f"{(t - ctx.log_start) / (ctx.log_end - ctx.log_start) * 180:.0f},"
                f"{18 - v / mx * 16:.0f}" for t, v in pts)
            spark = (f'<svg viewBox="0 0 180 20" style="width:180px;height:20px">'
                     f'<polyline points="{poly}" fill="none" stroke="#ec835a" '
                     f'stroke-width="1"/></svg>')
        else:
            spark = ""
        bar = int(avg / max_avg * 220)
        warn = ' style="background:#fbeee7"' if avg >= 30000 else ""
        rows.append(
            f"<tr{warn}><td class='r'>{idx}</td><td>{_esc(ch)}</td><td>{_esc(gpu)}</td>"
            f"<td class='r'>{len(vals)}</td><td class='r'>{vals[0] / 1000:.2f}</td>"
            f"<td class='r'><b>{avg / 1000:.2f}</b></td><td class='r'>{p95 / 1000:.2f}</td>"
            f"<td class='r'>{vals[-1] / 1000:.2f}</td>"
            f"<td><div style='width:{bar}px;height:10px;background:#2a78d6;"
            f"border-radius:2px'></div></td><td>{spark}</td></tr>")
    return ("<table class='sortable'><thead><tr><th>alg</th><th>채널</th><th>GPU</th>"
            "<th>N</th><th>min(s)</th><th>avg(s)</th><th>p95(s)</th><th>max(s)</th>"
            "<th>avg 막대</th><th>시계열(일자 전체)</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table>")


def _incomplete_section(ctx: ReportContext) -> str:
    bad = [i for i in ctx.inspections if i.status in _BAD_STATUSES]
    if not bad:
        return "<p class='ok'>미완료/이상 검사가 없습니다.</p>"
    note = ""
    if len(bad) > 300:
        note = f"<p class='legend'>총 {len(bad)}건 중 앞 300건만 표시합니다.</p>"
        bad = bad[:300]
    rows = []
    for it in bad:
        reason = []
        if it.ack_status and it.ack_status != "OK":
            reason.append(f"설비 회신 <b>{_esc(it.ack_status)}</b>"
                          f" (가용 스레드 {it.wait_threads})")
        if it.lost_channels:
            reason.append("실행 중 소실: " + _esc(", ".join(it.lost_channels)))
        if it.nofeed_channels and len(it.nofeed_channels) <= 15:
            reason.append("<b>이상 미투입</b>: " + _esc(", ".join(it.nofeed_channels)))
        elif it.n_nofeed:
            reason.append(f"<b>이상 미투입</b> {it.n_nofeed}채널")
        if it.n_skipped:
            reason.append(f"정상 스킵(레시피 조건 비활성) {it.n_skipped}채널")
        if it.timed_out:
            reason.append("<b>타임아웃</b>: 판정 결과가 설비로 미송신 "
                          "(플랫폼 [TIMEOUT] 조기 리턴)")
        if it.n_zones > 1 and it.n_zones_done < it.n_zones:
            reason.append(f"<b>존 부분 완료</b>: {it.n_zones_done}/{it.n_zones}존만 "
                          f"END 수신")
        if it.remain_list:
            reason.append("플랫폼 REMAIN 덤프: "
                          + _esc(_translate_alglist(ctx.recipe, it.remain_list)))
        c = _STATUS_COLOR.get(it.status, "#898781")
        rows.append(f"<tr><td>{_fmt_ts(it.start_text)}</td>"
                    f"<td><a href='#' class='ilink' data-id='{_esc(it.inner_id)}'>"
                    f"{_esc(it.inner_id)}</a></td>"
                    f"<td>{_esc(it.product_id)}</td>"
                    f"<td><span style='color:{c};font-weight:600'>"
                    f"{_STATUS_KO.get(it.status, it.status)}</span></td>"
                    f"<td class='r'>{it.n_done}/{it.n_fed}</td>"
                    f"<td>{'<br>'.join(reason) or '-'}</td></tr>")
    return (note + "<table><thead><tr><th>시작</th><th>inner id</th><th>product id</th>"
            "<th>상태</th><th>완료/투입</th><th>사유 (레시피 조인)</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table>")


def _errors_section(ctx: ReportContext) -> str:
    errs = [e for e in ctx.events
            if e.kind in ("ERROR", "MODEL_FAIL", "CRASH", "COMM_FAIL",
                          "RECIPE_FAIL", "EXC_REDIRECT")]
    if not errs:
        return "<p class='ok'>에러가 없습니다.</p>"
    grouped = Counter()
    first: dict = {}
    for e in errs:
        if e.kind == "EXC_REDIRECT":
            key = (f"예외(@{e.name})", e.extra[:80])
        else:
            key = (e.kind, e.model or e.extra[:120] or e.name)
        grouped[key] += 1
        first.setdefault(key, e)
    rows = []
    for key, cnt in grouped.most_common():
        e = first[key]
        detail = e.model or e.extra or e.name
        if e.kind == "MODEL_FAIL" and ctx.recipe:
            mi = int(e.model.replace("DLMODEL", "")) if e.model.startswith("DLMODEL") else 0
            m = ctx.recipe.models.get(mi)
            if m:
                detail = f"{e.model} = {m.model_file} (GPU{m.dev_index}, {m.infer_dll})"
        ctxt = getattr(e, "context", "")
        acc = (f"<details class='ctx'><summary>원본 로그 전후 ±3줄</summary>"
               f"<pre>{_esc(ctxt)}</pre></details>") if ctxt else ""
        rows.append(f"<tr><td>{_fmt_ts(e.ts_text)}</td><td>{_esc(key[0])}</td>"
                    f"<td class='r'>{cnt}</td><td>{_esc(detail)}{acc}</td></tr>")
    return ("<p class='legend'>같은 메시지는 묶어 최초 발생 기준으로 표시합니다. "
            "행의 <b>원본 로그 전후 ±3줄</b>을 펼치면 해당 시점의 주변 기록을 "
            "바로 확인할 수 있습니다.</p>"
            "<table class='sortable'><thead><tr><th>최초 발생</th><th>종류</th>"
            "<th>건수</th><th>내용</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table>")


def _series_svg(pairs: list[tuple[float, float]], t0: float, t1: float,
                w: int = 340, h: int = 74, color: str = "#ec835a",
                unit: str = "ms") -> str:
    """(ts, value) 시계열 미니 차트. 최대 240 포인트로 샘플링."""
    if len(pairs) < 2 or t1 <= t0:
        return ""
    pts = sorted(pairs)[:: max(1, len(pairs) // 240)]
    vmax = max(v for _t, v in pts) or 1
    poly = " ".join(f"{(t - t0) / (t1 - t0) * (w - 8) + 4:.0f},"
                    f"{h - 16 - v / vmax * (h - 26):.1f}" for t, v in pts)
    return (f'<svg viewBox="0 0 {w} {h}" style="width:{w}px;height:{h}px;'
            f'background:#fcfcfb;border:1px solid #e1e0d9;border-radius:4px">'
            f'<polyline points="{poly}" fill="none" stroke="{color}" stroke-width="1.2"/>'
            f'<text x="4" y="{h - 4}" font-size="9" fill="#898781">'
            f'max {vmax:,.1f}{unit}</text></svg>')


def _gpu_model_section(ctx: ReportContext) -> str:
    """DLInfer.log 기반: GPU-모델 매핑 + 모델별 GPU 실행시간(executeV2) 그래프."""
    loads: dict[str, list[Event]] = {}
    execs: dict[str, list[tuple[float, float]]] = {}
    gpu_of: dict[str, set[str]] = {}
    for e in ctx.events:
        if e.kind == "MODEL_GPU_LOAD":
            m = e.model.rsplit(".", 1)[0]
            loads.setdefault(m, []).append(e)
            gpu_of.setdefault(m, set()).add(e.status)
        elif e.kind == "DLINFER_EXEC":
            m = e.model.rsplit(".", 1)[0]
            execs.setdefault(m, []).append((e.ts, e.value))
            gpu_of.setdefault(m, set()).add(e.status)
    if not loads and not execs:
        return ("<h2>GPU-모델 매핑 (DLInfer)</h2>"
                "<p>DLInfer.log 가 없거나 계측 라인이 없습니다.</p>")
    out = ["<h2>GPU-모델 매핑 및 로드 (DLInfer 실측)</h2>"]
    rows = []
    for m in sorted(set(loads) | set(execs)):
        ls = loads.get(m, [])
        gpus = ", ".join(f"GPU{g}" for g in sorted(gpu_of.get(m, set())))
        avg_load = statistics.mean([e.value for e in ls]) if ls else 0
        rows.append(f"<tr><td>{_esc(m)}</td><td>{gpus}</td>"
                    f"<td class='r'>{len(ls)}</td>"
                    f"<td class='r'>{avg_load / 1000:.2f}s</td>"
                    f"<td class='r'>{len(execs.get(m, []))}</td></tr>")
    out.append("<table class='sortable'><thead><tr><th>모델</th><th>GPU</th>"
               "<th class='r'>로드 횟수</th><th class='r'>평균 로드시간</th>"
               "<th class='r'>GPU 실행 수</th></tr></thead><tbody>"
               + "".join(rows) + "</tbody></table>")

    if execs:
        out.append("<h2>모델별 GPU 실행시간(executeV2) 추이</h2>"
                   "<p class='legend'>순수 GPU 커널 실행시간(ms)입니다. 동시 실행이 "
                   "겹치는 시간대에 값이 상승하면 GPU 경합을 의미합니다.</p>")
        out.append("<div style='display:flex;flex-wrap:wrap;gap:14px'>")
        for m, pairs in sorted(execs.items(),
                               key=lambda kv: -statistics.mean(v for _t, v in kv[1])):
            vals = sorted(v for _t, v in pairs)
            avg = statistics.mean(vals)
            p95 = vals[min(len(vals) - 1, int(len(vals) * 0.95))]
            chart = _series_svg(pairs, ctx.log_start, ctx.log_end)
            out.append(
                f"<div style='width:352px'><div style='font-size:12px;font-weight:600;"
                f"margin-bottom:2px'>{_esc(m)}</div>"
                f"<div style='font-size:11px;color:#898781'>N={len(vals):,} · "
                f"avg {avg:.1f}ms · p95 {p95:.1f}ms · max {vals[-1]:,.1f}ms</div>"
                f"{chart}</div>")
        out.append("</div>")
    return "".join(out)


_SEV_STYLE = {
    "crit": ("#d03b3b", "#f9e6e6", "심각"),
    "warn": ("#ec835a", "#fbeee7", "주의"),
    "info": ("#898781", "#f0efec", "참고"),
    "ok":   ("#006300", "#e8f5e9", "정상"),
}


def _findings_section(ctx: ReportContext) -> str:
    """종합 최상단: 룰 기반 자동 진단 소견 (+선택적 LLM 종합 소견)."""
    if not ctx.findings:
        return ""
    out = []
    if ctx.llm_summary:
        out.append(
            "<h2>AI 종합 소견 <span class='hint'>— 로컬 LLM이 아래 룰 진단을 "
            "바탕으로 작성</span></h2>"
            f"<div style='border:1px solid #c3c2b7;background:#f0efec;"
            f"border-radius:6px;padding:12px 16px;white-space:pre-wrap;"
            f"font-size:13.5px'>{_esc(ctx.llm_summary)}</div>")
    out.append("<h2>자동 진단 소견 <span class='hint'>— 룰 엔진이 로그 전수에서 "
               "도출한 결과입니다</span></h2>")
    for f in ctx.findings:
        color, bg, label = _SEV_STYLE.get(f.severity, _SEV_STYLE["info"])
        ev = "".join(f"<li>{_esc(x)}</li>" for x in f.evidence)
        adv = (f"<div style='margin-top:4px;color:#0b0b0b'>→ {_esc(f.advice)}</div>"
               if f.advice else "")
        out.append(
            f"<div style='border-left:5px solid {color};background:{bg};"
            f"border-radius:4px;padding:9px 14px;margin:8px 0'>"
            f"<div style='font-weight:700;color:{color}'>[{label}] {_esc(f.title)}"
            f"</div><ul style='margin:5px 0 0 18px;padding:0;font-size:13px'>{ev}</ul>"
            f"{adv}</div>")
    if ctx.similar_cases:
        rows = "".join(
            f"<div style='border:1px solid #e1e0d9;border-radius:6px;"
            f"padding:8px 12px;margin:6px 0;font-size:13px'>"
            f"<b>{_esc(t)}</b> <span class='hint'>({_esc(site)} {_esc(dt)}, "
            f"유사도 {s})</span><br>원인: {_esc(cz)}<br>조치: {_esc(ac)}</div>"
            for s, t, cz, ac, site, dt in ctx.similar_cases)
        out.append("<h2>유사 과거 사례 <span class='hint'>— 사례 지식베이스(kb) "
                   "검색 결과</span></h2>" + rows)
    return "".join(out)


def _ng_section(ctx: ReportContext) -> str:
    """NG 판정 분포 — 결함명별 발생 건수 (품질 관점)."""
    ng = [i for i in ctx.inspections if i.end_result == "NG"]
    if not ng:
        return ""
    counter = Counter()
    for it in ng:
        for d in it.defects:
            counter[d] += 1
    total = len([i for i in ctx.inspections
                 if i.status == "complete" or i.end_result])
    rate = len(ng) / total * 100 if total else 0
    out = [f"<h2>NG 판정 분포 <span class='hint'>— NG {len(ng)}건 / "
           f"판정 {total}건 ({rate:.2f}%)</span></h2>"]
    if counter:
        mx = counter.most_common(1)[0][1]
        rows = []
        for name, cnt in counter.most_common(12):
            w = max(3, int(cnt / mx * 420))
            rows.append(
                f"<div class='mbar'><div class='mname'>{_esc(name)}</div>"
                f"<div class='mtrack'><div style='width:{w}px;background:#ec835a' "
                f"class='mfill'></div><span>{cnt}건</span></div></div>")
        out.append("<div class='mbars'>" + "".join(rows) + "</div>")
        if len(counter) > 12:
            out.append(f"<div class='legend'>외 {len(counter) - 12}개 결함 유형 — "
                       f"SQLite inspections.defects 에서 전체 조회 가능</div>")
    else:
        out.append("<p class='legend'>NG 건은 있으나 결함명 페이로드가 없습니다 "
                   "(설비 프로토콜 버전에 따라 미포함될 수 있음).</p>")
    return "".join(out)


def _model_bar_summary(ctx: ReportContext) -> str:
    """종합 페이지용: 모델별 평균 검사시간(InspectMC) 가로 막대 요약."""
    by_model: dict[str, list[float]] = {}
    for r in ctx.runs:
        if r.status == "done" and r.infer_ms > 0 and r.model:
            by_model.setdefault(r.model, []).append(r.infer_ms)
    if not by_model:
        return "<p>모델별 데이터 없음</p>"
    stats = sorted(((m, statistics.mean(v), len(v)) for m, v in by_model.items()),
                   key=lambda s: -s[1])[:12]
    mx = stats[0][1] or 1
    rows = []
    for m, avg, n in stats:
        w = int(avg / mx * 420)
        warn = "#ec835a" if avg >= 30000 else "#2a78d6"
        rows.append(
            f"<div class='mbar' onclick=\"gotoSection('sec-model')\">"
            f"<div class='mname'>{_esc(m)}</div>"
            f"<div class='mtrack'><div style='width:{w}px;background:{warn}' "
            f"class='mfill'></div><span>{avg / 1000:.2f}s · N={n}</span></div></div>")
    more = "" if len(by_model) <= 12 else \
        f"<div class='legend'>외 {len(by_model) - 12}개 모델 — 상세 분석에서 확인</div>"
    return "<div class='mbars'>" + "".join(rows) + "</div>" + more


def _errors_summary(ctx: ReportContext) -> str:
    """종합 페이지용: 에러 상위 그룹 요약."""
    errs = [e for e in ctx.events
            if e.kind in ("ERROR", "MODEL_FAIL", "CRASH", "COMM_FAIL",
                          "RECIPE_FAIL", "EXC_REDIRECT")]
    if not errs:
        return "<p class='ok'>에러가 없습니다.</p>"
    grouped = Counter()
    first: dict = {}
    for e in errs:
        key = (f"예외(@{e.name})" if e.kind == "EXC_REDIRECT" else e.kind,
               (e.model or e.extra[:90] or e.name))
        grouped[key] += 1
        first.setdefault(key, e)
    rows = []
    for key, cnt in grouped.most_common(6):
        e = first[key]
        rows.append(f"<tr onclick=\"gotoSection('sec-err')\" style='cursor:pointer'>"
                    f"<td>{_fmt_ts(e.ts_text)}</td><td>{_esc(key[0])}</td>"
                    f"<td class='r'>{cnt}</td><td>{_esc(key[1])}</td></tr>")
    more = (f"<div class='legend'>총 {len(errs)}건 / {len(grouped)}그룹 — 행을 "
            f"클릭하면 전체 목록으로 이동합니다</div>")
    return ("<table><thead><tr><th>최초</th><th>종류</th><th>건수</th><th>내용</th>"
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>" + more)


def _usage_mini(ctx: ReportContext) -> str:
    """종합 페이지용: RAM 추이 미니 차트 + 추세."""
    ua = _usage_analysis(ctx)
    if ua is None:
        return ("<p class='legend'>ProcessUsage.log 없음 — 설비 LogConfig.ini 에서 "
                "<code>Process Usage Log=1</code> 활성화 시 CPU/RAM 추이가 "
                "표시됩니다.</p>")
    series, slope, rise, span, suspicious = ua
    mem_pairs = [(t, m) for t, m, _c, _th in series]
    badge = (f"<span style='color:#d03b3b;font-weight:700'>⚠ 증가 추세 의심 "
             f"+{rise:,.0f}MB/{span:.1f}h</span>" if suspicious
             else f"<span style='color:#0ca30c'>추세 {slope:+,.0f}MB/h — 안정</span>")
    line = "#d03b3b" if suspicious else "#2a78d6"
    return (f"<div onclick=\"gotoSection('sec-sys')\" style='cursor:pointer'>"
            f"<div style='font-size:13px;margin-bottom:4px'>RAM(MB) — {badge}</div>"
            + _series_svg(mem_pairs, ctx.log_start, ctx.log_end,
                          w=520, h=96, color=line, unit="MB") + "</div>")


def _models_section(ctx: ReportContext) -> str:
    """상세: 레시피 모델 구성 + 로드 명령 이력 + 채널별 로드 + 모델별 Tact."""
    out = []
    # 0) 레시피 모델 구성 (DLMODEL.ini — 인퍼런스 동작 플래그 포함)
    if ctx.recipe and ctx.recipe.models:
        n_offmem = sum(1 for m in ctx.recipe.models.values() if not m.on_memory)
        out.append(
            "<h2>레시피 모델 구성 (DLMODEL.ini)</h2>"
            "<p class='legend'><b>상주</b> = on memory infer — 1이면 기동 시 "
            "모델을 VRAM 에 올려두고 매 요청은 즉시 실행, 0이면 매 요청마다 "
            "GPU 락 → 로드 → 추론 → 언로드를 반복하며 인스턴스 수가 1로 "
            "강제됩니다(느리지만 VRAM 절약).</p>")
        if n_offmem:
            out.append(f"<p style='background:#fbeee7;border:1px solid #ecb0a0;"
                       f"border-radius:6px;padding:9px 13px'><b>⚠ 비상주 모델 "
                       f"{n_offmem}개</b> — 해당 모델은 매 요청 로드/언로드로 "
                       f"GPU 직렬화 대기가 발생할 수 있습니다.</p>")
        mrows = []
        for m in sorted(ctx.recipe.models.values(), key=lambda x: x.idx):
            onmem = "1" if m.on_memory else \
                "<span style='color:#ec835a;font-weight:700'>0 (비상주)</span>"
            dev = "CPU" if m.dev_type == 0 else f"GPU{m.dev_index}"
            mrows.append(
                f"<tr><td class='r'>{m.idx}</td><td>{_esc(m.name)}</td>"
                f"<td>{_esc(m.model_file)}</td><td>{dev}</td>"
                f"<td>{_esc(m.task or '-')}</td><td class='r'>{onmem}</td>"
                f"<td class='r'>{m.instance_count}</td>"
                f"<td class='r'>{'1' if m.patch_infer else '0'}</td>"
                f"<td>{_esc(m.infer_dll)}</td></tr>")
        out.append("<table class='sortable'><thead><tr><th class='r'>#</th>"
                   "<th>모델</th><th>파일</th><th>장치</th><th>타입</th>"
                   "<th class='r'>상주</th><th class='r'>인스턴스</th>"
                   "<th class='r'>패치</th><th>백엔드 DLL</th></tr></thead>"
                   "<tbody>" + "".join(mrows) + "</tbody></table>")
    # 1) 설비 레시피/모델 로드 명령 (comm)
    rl = [m for m in ctx.model_loads if m.kind == "recipe_load"]
    out.append("<h2>설비 모델 로드 명령 (comm)</h2>")
    if rl:
        rows = "".join(
            f"<tr{' style=background:#f9e6e6' if m.status not in ('OK',) else ''}>"
            f"<td>{_fmt_ts(m.ts_text)}</td><td>{_esc(m.name)}</td>"
            f"<td class='r'>{m.dur_s:.1f}s</td><td>{_esc(m.status)}</td></tr>"
            for m in rl)
        out.append("<table><thead><tr><th>명령 수신</th><th>레시피/모델</th>"
                   "<th class='r'>소요</th><th>결과</th></tr></thead><tbody>"
                   + rows + "</tbody></table>")
    else:
        out.append("<p>당일 설비發 모델 로드 명령이 없습니다 (기동 시 자동 로드만 수행).</p>")

    # 2) 채널별 모델 로드 (DeepLearningInspector::Initialize 쌍)
    ci = [m for m in ctx.model_loads if m.kind == "channel_init"]
    out.append("<h2>채널별 모델 로드 (Initialize 소요)</h2>")
    if ci:
        slow = sorted(ci, key=lambda m: -m.dur_s)
        rows = "".join(
            f"<tr{' style=background:#f9e6e6' if m.status != 'OK' else ''}>"
            f"<td>{_fmt_ts(m.ts_text)}</td><td class='r'>{m.alg_idx}</td>"
            f"<td>{_esc(m.name)}</td><td class='r'>{m.dur_s:.1f}s</td>"
            f"<td>{_esc(m.status)}</td></tr>" for m in slow[:200])
        note = f" (전체 {len(ci)}건 중 소요 상위 200건)" if len(ci) > 200 else ""
        out.append(f"<p class='legend'>재시작/레시피 전환 시마다 채널이 모델을 다시 "
                   f"로드합니다{note}.</p>")
        out.append("<table class='sortable'><thead><tr><th>시작</th><th>alg</th>"
                   "<th>채널</th><th class='r'>소요</th><th>상태</th></tr></thead><tbody>"
                   + rows + "</tbody></table>")
    else:
        out.append("<p>채널 Initialize 기록 없음</p>")

    # 3) 모델(가중치)별 인퍼런스 Tact
    by_model: dict[str, list[float]] = {}
    ch_of_model: dict[str, set[int]] = {}
    for r in ctx.runs:
        if r.status == "done" and r.infer_ms > 0 and r.model:
            by_model.setdefault(r.model, []).append(r.infer_ms)
            ch_of_model.setdefault(r.model, set()).add(r.alg_idx)
    out.append("<h2>모델별 인퍼런스 Tact (InspectMC = 파이프라인 단위)</h2>")
    if by_model:
        rows = []
        for model, vals in sorted(by_model.items(),
                                  key=lambda kv: -statistics.mean(kv[1])):
            vs = sorted(vals)
            avg = statistics.mean(vs)
            p95 = vs[min(len(vs) - 1, int(len(vs) * 0.95))]
            gpu = ""
            if ctx.recipe:
                devs = set()
                for a in ch_of_model[model]:
                    for m in ctx.recipe.alg_models(a):
                        devs.add(m.dev_index)
                gpu = ", ".join(f"GPU{d}" for d in sorted(devs))
            chs = ", ".join(str(a) for a in sorted(ch_of_model[model])[:12])
            warn = ' style="background:#fbeee7"' if avg >= 30000 else ""
            rows.append(f"<tr{warn}><td>{_esc(model)}</td><td>{_esc(gpu)}</td>"
                        f"<td>{chs}</td><td class='r'>{len(vs)}</td>"
                        f"<td class='r'>{vs[0] / 1000:.2f}</td>"
                        f"<td class='r'><b>{avg / 1000:.2f}</b></td>"
                        f"<td class='r'>{p95 / 1000:.2f}</td>"
                        f"<td class='r'>{vs[-1] / 1000:.2f}</td></tr>")
        out.append("<table class='sortable'><thead><tr><th>모델</th><th>GPU</th>"
                   "<th>사용 채널(alg)</th><th>N</th><th>min(s)</th><th>avg(s)</th>"
                   "<th>p95(s)</th><th>max(s)</th></tr></thead><tbody>"
                   + "".join(rows) + "</tbody></table>")
    else:
        out.append("<p>모델별 Tact 데이터 없음</p>")
    out.append(_gpu_model_section(ctx))
    return "".join(out)


# ---------------------------------------------------------------------------
def _usage_analysis(ctx: ReportContext):
    """ProcessUsage 시계열과 메모리 증가 추세를 계산한다.

    반환: (series, slope_mb_h, rise_mb, span_h, suspicious) 또는 None
    """
    series = [(e.ts, e.value, float(e.status or 0), int(e.name or 0))
              for e in ctx.events if e.kind == "USAGE"]
    if len(series) < 20:
        return None
    series.sort()
    # 5분 버킷 최대 RAM 으로 추세 계산 (여러 프로세스가 한 파일에 섞이므로
    # 상한 포락선을 주 엔진 프로세스의 추세로 본다)
    buckets: dict[int, float] = {}
    for ts, mem, _c, _t in series:
        b = int(ts // 300)
        buckets[b] = max(buckets.get(b, 0.0), mem)
    xs = sorted(buckets)
    if len(xs) < 6:
        return series, 0.0, 0.0, 0.0, False
    ys = [buckets[b] for b in xs]
    hx = [(b - xs[0]) * 300 / 3600 for b in xs]      # 시간(h)
    mx = statistics.mean(hx)
    my = statistics.mean(ys)
    var = sum((x - mx) ** 2 for x in hx) or 1e-9
    slope = sum((x - mx) * (y - my) for x, y in zip(hx, ys)) / var   # MB/h
    rise = ys[-1] - ys[0]
    span = hx[-1]
    suspicious = slope > 100 and rise > 1000 and span >= 2
    return series, slope, rise, span, suspicious


def _usage_section(ctx: ReportContext) -> str:
    ua = _usage_analysis(ctx)
    out = ["<h2>프로세스 리소스 사용량 (ProcessUsage)</h2>"]
    if ua is None:
        out.append("<p>ProcessUsage.log 가 없거나 데이터가 부족합니다. "
                   "설비의 <code>LogConfig.ini</code> 에서 "
                   "<code>Process Usage Log=1</code> 로 활성화할 수 있습니다.</p>")
        return "".join(out)
    series, slope, rise, span, suspicious = ua
    if suspicious:
        out.append(f"<p style='background:#f9e6e6;border:1px solid #ecb0a0;"
                   f"border-radius:6px;padding:10px'><b>⚠ 메모리 증가 추세 의심</b> — "
                   f"{span:.1f}시간 동안 상한 기준 +{rise:,.0f}MB "
                   f"(≈ {slope:,.0f}MB/h). 메모리 릭 여부 점검을 권장합니다.</p>")
    else:
        out.append(f"<p class='legend'>메모리 추세: {slope:+,.0f}MB/h "
                   f"({span:.1f}h 구간, 상한 포락선 기준) — 지속 증가 패턴 없음</p>")
    mem_pairs = [(t, m) for t, m, _c, _th in series]
    cpu_pairs = [(t, c) for t, _m, c, _th in series]
    thr_pairs = [(t, float(th)) for t, _m, _c, th in series]
    out.append("<div style='display:flex;flex-wrap:wrap;gap:16px'>")
    ram_color = "#d03b3b" if suspicious else "#2a78d6"
    for label, pairs, color, unit in (
            ("RAM (MB)", mem_pairs, ram_color, "MB"),
            ("CPU (%)", cpu_pairs, "#eb6834", "%"),
            ("스레드 수", thr_pairs, "#1baf7a", "")):
        out.append(f"<div><div style='font-size:12px;font-weight:600'>{label}</div>"
                   + _series_svg(pairs, ctx.log_start, ctx.log_end,
                                 w=520, h=110, color=color, unit=unit) + "</div>")
    out.append("</div>")
    out.append("<p class='legend'>주의: 이 파일에는 같은 PC 의 talos 프로세스들이 "
               "함께 기록되므로 선이 여러 밴드로 보일 수 있습니다. 추세 판정은 "
               "5분 버킷 상한(주 엔진 프로세스) 기준입니다.</p>")
    return "".join(out)


def _gpu_resource_section(ctx: ReportContext) -> str:
    """GPU 리소스 (v1.4): [GPU STATUS] VRAM·온도, 모델 로드 VRAM 델타,
    CUDA 메모리풀, TalogWatch 상주 수집 — GPU0/GPU1 개별 표시."""
    status = [e for e in ctx.events if e.kind == "GPU_STATUS"]
    memload = [e for e in ctx.events if e.kind == "GPU_MEMLOAD"]
    mempool = [e for e in ctx.events if e.kind == "CUDA_MEMPOOL"]
    waits = [e for e in ctx.events if e.kind == "GPU_WAIT" and e.value > 0]
    console_map = {e.model: int(e.value) for e in ctx.events
                   if e.kind == "GPU_MODEL_LOAD"}
    out = ["<h2>GPU 리소스</h2>"]
    any_data = False

    # 1) NVML [GPU STATUS] — GPU별 VRAM/온도 시계열 (신규 빌드 로그)
    if status:
        any_data = True
        by_gpu: dict[str, list] = {}
        for e in status:
            by_gpu.setdefault(e.status or "0", []).append(e)
        out.append("<h3>GPU별 VRAM·온도 (NVML 스냅샷, 인퍼런스 전후)</h3>"
                   "<div style='display:flex;flex-wrap:wrap;gap:16px'>")
        for g in sorted(by_gpu):
            evs = by_gpu[g]
            mem = [(e.ts, e.value) for e in evs]
            temp = [(e.ts, float(e.name or 0)) for e in evs if e.name]
            tmax = max((v for _t, v in temp), default=0.0)
            tcol = "#d03b3b" if tmax >= 85 else "#eb6834"
            twarn = " ⚠" if tmax >= 85 else ""
            out.append(
                f"<div><div style='font-size:12px;font-weight:600'>GPU {_esc(g)}"
                f" — VRAM 사용량(MB)</div>"
                + _series_svg(mem, ctx.log_start, ctx.log_end,
                              w=430, h=100, color="#2a78d6", unit="MB")
                + f"<div style='font-size:12px;font-weight:600;margin-top:6px'>"
                f"GPU {_esc(g)} — 온도(°C), 최고 {tmax:.0f}°C{twarn}</div>"
                + _series_svg(temp, ctx.log_start, ctx.log_end,
                              w=430, h=74, color=tcol, unit="C") + "</div>")
        out.append("</div>")

    # 2) 모델 로드 시 VRAM (cudaMemGetInfo 전후 델타)
    if memload:
        any_data = True
        series: dict[str, list] = {}
        per_model: dict[str, dict] = {}
        for e in memload:
            phys = console_map.get(e.model)
            gl = f"GPU #{phys}" if phys is not None else f"GPU 로컬{e.status}"
            series.setdefault(gl, []).append((e.ts, e.value))
            pm = per_model.setdefault(
                e.model, {"gpu": gl, "n": 0, "deltas": [], "last": 0.0})
            pm["n"] += 1
            pm["deltas"].append(float(e.name or 0))
            pm["last"] = e.value
        out.append(
            "<h3>모델 로드 시점 VRAM (cudaMemGetInfo)</h3>"
            "<p class='legend'>모델을 GPU 에 올릴 때마다 측정된 전체 VRAM "
            "사용량입니다. 상주(on memory infer=1) 모델은 기동/레시피 전환 시 "
            "1회, 비상주(=0) 모델은 매 요청마다 기록됩니다."
            + (" 물리 GPU 번호는 console.log 의 로드 이벤트로 보정했습니다."
               if console_map else
               " 이 로그의 GPU 번호는 프로세스 로컬 인덱스입니다 (물리 구분은 "
               "console.log 필요).") + "</p>"
            "<div style='display:flex;flex-wrap:wrap;gap:16px'>")
        for gl in sorted(series):
            out.append(f"<div><div style='font-size:12px;font-weight:600'>"
                       f"{_esc(gl)} — 로드 후 VRAM(MB)</div>"
                       + _series_svg(series[gl], ctx.log_start, ctx.log_end,
                                     w=430, h=100, color="#2a78d6", unit="MB")
                       + "</div>")
        out.append("</div>")
        rows = "".join(
            f"<tr><td>{_esc(m)}</td><td>{_esc(d['gpu'])}</td>"
            f"<td class='r'>{d['n']}</td>"
            f"<td class='r'>{statistics.mean(d['deltas']):,.0f}</td>"
            f"<td class='r'>{d['last']:,.0f}</td></tr>"
            for m, d in sorted(per_model.items(),
                               key=lambda kv: -kv[1]["n"])[:60])
        out.append("<table class='sortable'><thead><tr><th>모델</th><th>GPU</th>"
                   "<th class='r'>로드 횟수</th><th class='r'>평균 증분(MB)</th>"
                   "<th class='r'>마지막 로드 후(MB)</th></tr></thead><tbody>"
                   + rows + "</tbody></table>")

    # 3) CUDA 메모리풀 (WDDM, Tenneco 계열 빌드)
    if mempool:
        any_data = True
        by_dev: dict[str, list] = {}
        for e in mempool:
            by_dev.setdefault(e.status or "0", []).append((e.ts, e.value))
        out.append("<h3>CUDA 메모리풀 (usedCur)</h3>"
                   "<div style='display:flex;flex-wrap:wrap;gap:16px'>")
        for g in sorted(by_dev):
            out.append(f"<div><div style='font-size:12px;font-weight:600'>"
                       f"dev {_esc(g)} — usedCur(MB)</div>"
                       + _series_svg(by_dev[g], ctx.log_start, ctx.log_end,
                                     w=430, h=90, color="#2a78d6", unit="MB")
                       + "</div>")
        out.append("</div>")

    # 4) 비상주 경로의 GPU 전역 락 대기
    if waits:
        any_data = True
        vs = sorted(e.value for e in waits)
        p95 = vs[min(len(vs) - 1, int(len(vs) * 0.95))]
        out.append(f"<p><b>GPU 전역 락 대기</b> (WaitForCriticalSection, "
                   f"비상주 모델 경로): {len(vs):,}회 · 평균 "
                   f"{statistics.mean(vs):,.0f}ms · p95 {p95:,.0f}ms — "
                   f"값이 크면 여러 채널이 같은 GPU 를 직렬로 대기한 것입니다.</p>")

    # 5) TalogWatch 상주 수집 (gpu_*.jsonl — 사용률 포함 유일 소스)
    if ctx.gpu_watch:
        any_data = True
        by_gpu2: dict[int, list] = {}
        for ts, g, temp, util, mem in ctx.gpu_watch:
            by_gpu2.setdefault(g, []).append((ts, temp, util, mem))
        out.append("<h3>TalogWatch 상주 수집 (nvidia-smi, 5분 간격)</h3>"
                   "<div style='display:flex;flex-wrap:wrap;gap:16px'>")
        for g in sorted(by_gpu2):
            rows_g = by_gpu2[g]
            for label, idx, color, unit in (
                    ("사용률(%)", 2, "#eb6834", "%"),
                    ("VRAM(MB)", 3, "#2a78d6", "MB"),
                    ("온도(°C)", 1, "#1baf7a", "C")):
                pairs = [(r[0], r[idx]) for r in rows_g]
                out.append(f"<div><div style='font-size:12px;font-weight:600'>"
                           f"GPU {g} — {label}</div>"
                           + _series_svg(pairs, ctx.log_start, ctx.log_end,
                                         w=340, h=84, color=color, unit=unit)
                           + "</div>")
        out.append("</div>")

    if not any_data:
        out.append(
            "<p>이 로그 묶음에는 GPU 리소스 기록이 없습니다.</p>"
            "<p class='legend'>확보 방법 3가지 — ① 신규 talos-vision 빌드는 "
            "DLInfer.log 에 NVML 스냅샷([GPU STATUS] — VRAM·온도)을 남깁니다. "
            "② 모델 로드(레시피 전환)가 있는 날짜에는 cudaMemGetInfo 라인으로 "
            "VRAM 재구성이 가능합니다. ③ GPU 사용률(%)·클럭은 어떤 로그에도 "
            "없으므로 <code>talog watch</code>(TalogWatch)를 상주시키면 "
            "nvidia-smi 로 5분 간격 수집됩니다(gpu_*.jsonl). 클럭·스로틀링 "
            "사유까지 필요하면 플랫폼 로깅 추가를 권장합니다.</p>")
    return "".join(out)


def _gens_section(ctx: ReportContext) -> str:
    if not ctx.gens:
        return "<p>프로세스 세대 정보 없음</p>"
    cause_ko = {"crash": "크래시", "kill": "kill 스크립트", "destroy": "정상 종료",
                "eof": "로그 종료 시점까지 가동", "unknown": "불명"}
    rows = []
    for g in ctx.gens:
        dur = (g.end_ts - g.start_ts) / 60 if g.end_ts else 0
        rows.append(f"<tr><td class='r'>{g.gen_id}</td><td>{_fmt_ts(g.start_text)}</td>"
                    f"<td>{_fmt_ts(g.end_text)}</td><td class='r'>{dur:.1f}분</td>"
                    f"<td>{cause_ko.get(g.end_cause, g.end_cause)}</td></tr>")
    return ("<table><thead><tr><th>세대</th><th>시작</th><th>종료</th><th>가동</th>"
            "<th>종료 원인</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>")


# ---------------------------------------------------------------------------
_JS = r"""
const SUMMARY = __SUMMARY__;
const DETAIL = __DETAIL__;
const GRAPH = __GRAPH__;
const STATUS_KO = __STATUS_KO__;
const STATUS_COLOR = __STATUS_COLOR__;

function showTab(name) {
  document.querySelectorAll('.tabpane').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.tabbtn').forEach(b => b.classList.remove('on'));
  document.getElementById('pane-' + name).style.display = 'block';
  document.getElementById('btn-' + name).classList.add('on');
}

function fmtDur(s) { return s ? s.toFixed(2) + 's' : '-'; }

let STATUS_FILTER = 'all';
const BAD_SET = new Set(['rejected', 'incomplete_lost', 'incomplete', 'unknown']);

function applyFilter(rows) {
  if (STATUS_FILTER === 'bad') return rows.filter(r => BAD_SET.has(r[4]));
  if (STATUS_FILTER === 'ng') return rows.filter(r => r[9] === 'NG');
  if (STATUS_FILTER === 'eof') return rows.filter(r => r[4] === 'in_progress_eof');
  if (STATUS_FILTER === 'ok') return rows.filter(r => r[4] === 'complete');
  return rows;
}

function filteredRows() {
  const f = (document.getElementById('insp-search').value || '').trim();
  let rows = SUMMARY;
  if (f) rows = rows.filter(r => r[0].includes(f) || (r[1] && r[1].includes(f)));
  return applyFilter(rows);
}

function setFilter(name) {
  STATUS_FILTER = name;
  document.querySelectorAll('.fbtn').forEach(b =>
    b.classList.toggle('on', b.dataset.f === name));
  renderList();
}

function renderList() {
  const tb = document.getElementById('insp-body');
  const rows = filteredRows();
  const frag = [];
  const show = rows.slice(-400).reverse();   // 최근 400건 표시
  for (const r of show) {
    const [iid, pid, ts, st, status, dur, ndone, nfed, ack, res] = r;
    const c = STATUS_COLOR[status] || '#898781';
    const has = DETAIL[iid] ? ' class="ilink" style="cursor:pointer;color:#2a78d6"' : '';
    const resTxt = res === 'NG' ? '<span style="color:#ec835a;font-weight:700">NG</span>'
      : (res || '-');
    frag.push(`<tr><td>${st}</td><td${has} data-id="${iid}">${iid}</td><td>${pid || '-'}</td>` +
      `<td><span style="color:${c};font-weight:600">${STATUS_KO[status] || status}</span></td>` +
      `<td>${resTxt}</td>` +
      `<td class="r">${fmtDur(dur)}</td><td class="r">${ndone}/${nfed}</td></tr>`);
  }
  tb.innerHTML = frag.join('');
  document.getElementById('insp-count').textContent =
    `${rows.length}건 매칭 (표시 ${show.length}건, 상세 보유 ${Object.keys(DETAIL).length}건)`;
  bindLinks();
}

function exportCsv() {
  const head = ['inner_id', 'product_id', 'start', 'status', 'result',
                'duration_s', 'done', 'fed', 'ack'];
  const lines = [head.join(',')];
  for (const r of filteredRows()) {
    lines.push([r[0], r[1], r[3], STATUS_KO[r[4]] || r[4], r[9] || '',
                r[5], r[6], r[7], r[8] || ''].map(v =>
      `"${String(v).replace(/"/g, '""')}"`).join(','));
  }
  const blob = new Blob(['﻿' + lines.join('\r\n')],
                        {type: 'text/csv;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'talog_inspections.csv';
  a.click();
  URL.revokeObjectURL(a.href);
}

function renderGantt(iid) {
  const d = DETAIL[iid];
  const box = document.getElementById('gantt-box');
  if (!d) {
    box.innerHTML = `<p>이 검사(${iid})의 채널 상세는 리포트에 내장되지 않았습니다. ` +
      `동봉된 .sqlite 에서 channel_runs 테이블을 조회하면 전체를 볼 수 있습니다.</p>`;
    return;
  }
  const runs = d.runs;
  if (!runs.length) {
    box.innerHTML = `<h3>${iid} — ${STATUS_KO[d.status] || d.status} (${d.st})</h3>` +
      `<p>채널 실행 기록이 없습니다 (투입 전 거부).</p>`;
    return;
  }
  let maxT = 1;
  for (const r of runs) maxT = Math.max(maxT, r[3] + Math.max(r[4], 0.4));
  const W = 1060, LB = 300, rowH = 18;
  const H = runs.length * rowH + 40;
  const sx = t => LB + t / maxT * (W - LB - 20);
  const parts = [`<svg viewBox="0 0 ${W} ${H}" style="width:100%;background:#fff;border:1px solid #e1e0d9">`];
  const gstep = maxT > 120 ? 60 : (maxT > 30 ? 10 : 5);
  for (let t = 0; t <= maxT; t += gstep) {
    parts.push(`<line x1="${sx(t)}" y1="14" x2="${sx(t)}" y2="${H - 22}" stroke="#f0efec"/>` +
      `<text x="${sx(t)}" y="${H - 8}" font-size="10" text-anchor="middle" fill="#898781">+${t}s</text>`);
  }
  runs.forEach((r, i) => {
    const [alg, ch, ex, rel, dur, status, model, pre] = r;
    const y = 18 + i * rowH;
    const color = status === 'done' ? '#2a78d6' : '#ec835a';
    const wpx = Math.max(3, sx(rel + Math.max(dur, 0.05)) - sx(rel));
    parts.push(`<text x="${LB - 6}" y="${y + 11}" font-size="11" text-anchor="end" fill="#0b0b0b">` +
      `${alg}(${ch})${ex > 1 ? ' #' + ex : ''}</text>`);
    parts.push(`<rect x="${sx(rel)}" y="${y + 2}" width="${wpx}" height="12" rx="2" fill="${color}">` +
      `<title>${ch} exec${ex}\n시작 +${rel}s, 인퍼런스 ${dur ? dur + 's' : '(미완료)'}\n` +
      `전처리 ${pre}ms\n${model}\n상태: ${status}</title></rect>`);
    if (status !== 'done')
      parts.push(`<text x="${sx(rel) + wpx + 4}" y="${y + 12}" font-size="10" fill="#d03b3b">소실</text>`);
  });
  parts.push('</svg>');
  let head = `<h3>${iid} — <span style="color:${STATUS_COLOR[d.status] || '#0b0b0b'}">` +
    `${STATUS_KO[d.status] || d.status}</span> (시작 ${d.st})</h3>`;
  if (d.lost && d.lost.length) head += `<p>소실 채널: <b>${d.lost.join(', ')}</b></p>`;
  if (d.nofeed && d.nofeed.length) head += `<p>미투입: ${d.nofeed.join(', ')}</p>`;
  if (d.remain) head += `<p>플랫폼 REMAIN 덤프: ${d.remain}</p>`;
  box.innerHTML = head + parts.join('');
}

function renderGraph(iid) {
  const box = document.getElementById('graph-box');
  if (!box || !GRAPH.algs.length) { if (box) box.innerHTML = '<p>그래프 데이터 없음</p>'; return; }
  const d = iid ? DETAIL[iid] : null;
  const doneSet = new Set(), execCnt = {};
  if (d) for (const r of d.runs) { if (r[5] === 'done') doneSet.add(r[0]); execCnt[r[0]] = (execCnt[r[0]] || 0) + 1; }
  const lostSet = new Set(d ? d.lostIdx : []), nofeedSet = new Set(d ? d.nofeedIdx : []),
        skipSet = new Set(d ? d.skipIdx : []);
  const RH = 19, top = 34;
  const nA = GRAPH.algs.length, nR = GRAPH.rois.length, nI = GRAPH.imgs.length;
  const H = top + Math.max(nA, nR, nI) * RH + 20;
  const yFor = (i, n) => top + i * RH + (Math.max(nA, nR, nI) - n) * RH / 2;
  const imgY = {}, roiY = {}, algY = {};
  GRAPH.imgs.forEach((m, i) => imgY[m.id] = yFor(i, nI));
  GRAPH.rois.forEach((r, i) => roiY[r.id] = yFor(i, nR));
  GRAPH.algs.forEach((a, i) => algY[a.id] = yFor(i, nA));
  const P = [`<svg viewBox="0 0 1100 ${H}" style="width:100%;background:#fff">`];
  P.push(`<text x="90" y="18" font-size="12" fill="#898781" text-anchor="middle">이미지</text>` +
         `<text x="345" y="18" font-size="12" fill="#898781" text-anchor="middle">ROI</text>` +
         `<text x="770" y="18" font-size="12" fill="#898781" text-anchor="middle">알고리즘 (검사 채널)</text>`);
  for (const r of GRAPH.rois) if (r.img && imgY[r.img] !== undefined)
    P.push(`<line x1="165" y1="${imgY[r.img] + 7}" x2="285" y2="${roiY[r.id] + 7}" stroke="#e1e0d9" stroke-width="1"/>`);
  for (const a of GRAPH.algs) for (const ri of a.roi) if (roiY[ri] !== undefined) {
    let ec = '#e1e0d9';
    if (d) { if (lostSet.has(a.id)) ec = '#ec835a'; else if (nofeedSet.has(a.id)) ec = '#d03b3b';
             else if (doneSet.has(a.id)) ec = '#9ec5f4'; }
    P.push(`<line x1="435" y1="${roiY[ri] + 7}" x2="565" y2="${algY[a.id] + 7}" stroke="${ec}" stroke-width="1"/>`);
  }
  for (const m of GRAPH.imgs)
    P.push(`<rect x="20" y="${imgY[m.id]}" width="145" height="15" rx="3" fill="#f0efec" stroke="#e1e0d9"/>` +
           `<text x="26" y="${imgY[m.id] + 11.5}" font-size="10.5">${m.label}</text>`);
  for (const r of GRAPH.rois)
    P.push(`<rect x="285" y="${roiY[r.id]}" width="150" height="15" rx="3" fill="#f0efec" stroke="#e1e0d9"/>` +
           `<text x="291" y="${roiY[r.id] + 11.5}" font-size="10.5">${r.id} ${r.label}</text>`);
  for (const a of GRAPH.algs) {
    let fill = '#fff', stroke = '#c3c2b7', tip;
    if (d) {
      if (lostSet.has(a.id)) { fill = '#fbe3d9'; stroke = '#ec835a'; tip = '실행 중 소실'; }
      else if (nofeedSet.has(a.id)) { fill = '#f8dcdc'; stroke = '#d03b3b'; tip = '이상 미투입'; }
      else if (skipSet.has(a.id)) { fill = '#f0efec'; stroke = '#c3c2b7'; tip = '정상 스킵(조건 비활성)'; }
      else if (doneSet.has(a.id)) { fill = '#d7efd7'; stroke = '#0ca30c'; tip = `완료 (실행 ${execCnt[a.id]})`; }
      else { tip = a.dl ? '기록 없음' : '비 DL 채널'; }
    } else {
      if (a.lost) { fill = '#fbe3d9'; stroke = '#ec835a'; }
      else if (a.done) { fill = '#cde2fb'; stroke = '#2a78d6'; }
      tip = `당일 완료 ${a.done}회` + (a.lost ? `, 소실 ${a.lost}회` : '');
    }
    P.push(`<rect x="565" y="${algY[a.id]}" width="510" height="15" rx="3" fill="${fill}" stroke="${stroke}">` +
           `<title>alg${a.id} ${a.label} — ${tip}</title></rect>` +
           `<text x="571" y="${algY[a.id] + 11.5}" font-size="10.5">${a.id} ${a.label}</text>`);
  }
  P.push('</svg>');
  const mode = d ? `검사 <b>${iid}</b> 상태` : '일자 요약 (파랑=당일 실행, 주황=소실 발생 채널)';
  const legend = d ? ' · <span style="color:#0ca30c">■완료</span> <span style="color:#ec835a">■소실</span>' +
    ' <span style="color:#d03b3b">■이상 미투입</span> <span style="color:#898781">■정상 스킵</span>' : '';
  document.getElementById('graph-mode').innerHTML = mode + legend +
    (GRAPH.hasRecipe ? '' : ' · <span style="color:#898781">(레시피 없음 — 관측 기반 축약 그래프)</span>');
  box.innerHTML = `<div style="max-height:680px;overflow:auto;border:1px solid #e1e0d9;border-radius:6px">${P.join('')}</div>`;
}

function gotoSection(id) {
  showTab('detail');
  const el = document.getElementById(id);
  if (el) setTimeout(() => el.scrollIntoView({behavior: 'smooth'}), 30);
}

function scrollMain(id) {
  showTab('main');
  const el = document.getElementById(id);
  if (el) setTimeout(() => el.scrollIntoView({behavior: 'smooth'}), 30);
}

function openInspection(iid) {
  gotoSection('sec-insp');
  document.getElementById('insp-search').value = iid;
  renderList(iid);
  renderGantt(iid);
  renderGraph(DETAIL[iid] ? iid : null);
}

function bindLinks() {
  document.querySelectorAll('.ilink, .tl').forEach(el => {
    el.onclick = ev => { ev.preventDefault(); openInspection(el.dataset.id); };
  });
}

function makeSortable() {
  document.querySelectorAll('table.sortable th').forEach((th, idx) => {
    th.style.cursor = 'pointer';
    th.title = '클릭하여 정렬';
    th.onclick = () => {
      const tb = th.closest('table').querySelector('tbody');
      const rows = [...tb.querySelectorAll('tr')];
      const asc = th.dataset.asc !== '1';
      th.dataset.asc = asc ? '1' : '0';
      rows.sort((a, b) => {
        const x = a.children[idx].innerText, y = b.children[idx].innerText;
        const nx = parseFloat(x.replace(/[^\d.-]/g, '')), ny = parseFloat(y.replace(/[^\d.-]/g, ''));
        const cmp = (!isNaN(nx) && !isNaN(ny)) ? nx - ny : x.localeCompare(y);
        return asc ? cmp : -cmp;
      });
      rows.forEach(r => tb.appendChild(r));
    };
  });
}

window.addEventListener('DOMContentLoaded', () => {
  showTab('main');
  renderList('');
  renderGraph(null);
  document.getElementById('insp-search').addEventListener('input',
    ev => renderList(ev.target.value));
  makeSortable();
  bindLinks();
});
"""


def render(ctx: ReportContext) -> str:
    n = Counter(i.status for i in ctx.inspections)
    total = len(ctx.inspections)
    durations = [(i.end_ts - i.start_ts) for i in ctx.inspections
                 if i.end_ts and i.start_ts and i.status == "complete"]
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
    n_ng = sum(1 for i in ctx.inspections if i.end_result == "NG")
    n_err = sum(1 for e in ctx.events
                if e.kind in ("ERROR", "MODEL_FAIL", "CRASH", "COMM_FAIL",
                              "RECIPE_FAIL", "EXC_REDIRECT"))
    # (라벨, 값, 색, 클릭 시 이동) — 이동이 'main-bad' 면 종합 페이지 내 스크롤
    cards = [
        ("검사 수", str(total), "#0b0b0b", "sec-insp"),
        ("완료", str(n.get("complete", 0)), "#0ca30c", "sec-insp"),
        ("이상", str(n_bad), "#d03b3b" if n_bad else "#0ca30c", "main-bad"),
        ("NG 판정", str(n_ng), "#ec835a" if n_ng else "#0ca30c", "main-bad"),
        ("에러", str(n_err), "#d03b3b" if n_err else "#0ca30c", "sec-err"),
        ("평균 검사시간", f"{avg_dur:.2f}s" if avg_dur else "-", "#0b0b0b", "sec-tact"),
        ("최대 검사시간", f"{max_dur:.2f}s" if max_dur else "-", "#0b0b0b", "sec-tact"),
        ("재시작", str(max(0, len(ctx.gens) - 1)), "#2a78d6", "sec-sys"),
        ("시뮬레이션", str(n_sim), "#1baf7a", "sec-insp"),
    ]
    card_html = "".join(
        f"<div class='card' onclick=\"{'gotoSection' if t != 'main-bad' else 'scrollMain'}"
        f"('{t}')\"><div class='num' style='color:{c}'>{v}</div>"
        f"<div class='lbl'>{k}</div></div>" for k, v, c, t in cards)

    files_html = "".join(
        f"<tr><td>{_esc(nm)}</td><td>{_esc(cat)}</td><td class='r'>{sz / 1048576:.1f}</td>"
        f"<td class='r'>{rec:,}</td><td class='r'>{ev:,}</td></tr>"
        for nm, cat, sz, rec, ev in ctx.file_rows)

    ua = _usage_analysis(ctx)
    leak_banner = ""
    if ua is not None and ua[4]:
        leak_banner = (f"<p style='background:#f9e6e6;border:1px solid #ecb0a0;"
                       f"border-radius:6px;padding:10px;margin-top:14px'>"
                       f"<b>⚠ 메모리 증가 추세 의심</b> — {ua[3]:.1f}시간 동안 "
                       f"+{ua[2]:,.0f}MB (≈ {ua[1]:,.0f}MB/h). 시스템 탭에서 "
                       f"RAM 추이를 확인하세요.</p>")

    summary_json, detail_json = _build_payload(ctx)
    js = (_JS.replace("__SUMMARY__", summary_json)
             .replace("__DETAIL__", detail_json)
             .replace("__GRAPH__", _build_graph(ctx))
             .replace("__STATUS_KO__", json.dumps(_STATUS_KO, ensure_ascii=False))
             .replace("__STATUS_COLOR__", json.dumps(_STATUS_COLOR)))

    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<title>talog — {_esc(ctx.title)}</title>
<style>
 :root {{
   --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink2: #52514e;
   --muted: #898781; --grid: #e1e0d9; --line: rgba(11,11,11,.10);
   --blue: #2a78d6; --blue-100: #cde2fb; --orange: #eb6834;
   --good: #0ca30c; --good-text: #006300; --warn: #fab219;
   --serious: #ec835a; --crit: #d03b3b; --track: #f0efec;
 }}
 * {{ box-sizing: border-box; }}
 body {{ font-family: "Pretendard", system-ui, -apple-system, "Segoe UI",
         "Malgun Gothic", sans-serif; margin: 0; color: var(--ink);
         background: var(--page); font-size: 13px; line-height: 1.45; }}
 header {{ background: var(--surface); border-bottom: 1px solid var(--line);
           padding: 14px 28px 0; }}
 header h1 {{ font-size: 17px; margin: 0; letter-spacing: -.2px; }}
 header h1::before {{ content: ""; display: inline-block; width: 9px; height: 9px;
   border-radius: 50%; background: var(--blue); margin-right: 8px; }}
 header .meta {{ color: var(--muted); font-size: 11.5px; margin: 5px 0 10px; }}
 header .meta b {{ color: var(--ink2); font-weight: 600; }}
 nav {{ display: flex; gap: 2px; }}
 .tabbtn {{ background: none; border: none; color: var(--ink2); padding: 9px 14px;
            font-size: 13px; cursor: pointer; font-family: inherit;
            border-bottom: 2px solid transparent; }}
 .tabbtn:hover {{ color: var(--ink); }}
 .tabbtn.on {{ color: var(--ink); border-bottom-color: var(--blue);
               font-weight: 700; }}
 main {{ padding: 18px 28px 44px; max-width: 1560px; margin: 0 auto; }}
 .tabpane {{ display: none; }}
 h2 {{ font-size: 11.5px; margin: 30px 0 8px; color: var(--muted);
       font-weight: 700; text-transform: uppercase; letter-spacing: .6px; }}
 h2 .hint {{ text-transform: none; letter-spacing: 0; }}
 table {{ border-collapse: collapse; width: 100%; font-size: 12.5px;
          background: var(--surface); border: 1px solid var(--line);
          border-radius: 8px; overflow: hidden; }}
 th, td {{ border: 0; border-bottom: 1px solid var(--grid); padding: 6px 10px;
           text-align: left; }}
 tbody tr:last-child td {{ border-bottom: 0; }}
 tbody tr:hover td {{ background: rgba(42,120,214,.045); }}
 th {{ background: var(--surface); position: sticky; top: 0; color: var(--muted);
       font-size: 10.5px; text-transform: uppercase; letter-spacing: .5px;
       border-bottom: 1px solid var(--line); }}
 td.r, th.r {{ text-align: right; font-variant-numeric: tabular-nums; }}
 .cards {{ display: grid; gap: 10px; margin-top: 12px;
           grid-template-columns: repeat(auto-fit, minmax(118px, 1fr)); }}
 .card {{ border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px;
          background: var(--surface); cursor: pointer; }}
 .card:hover {{ border-color: var(--blue); }}
 .card .num {{ font-size: 22px; font-weight: 700; letter-spacing: -.4px; }}
 .card .lbl {{ font-size: 10.5px; color: var(--muted); margin-top: 3px;
               text-transform: uppercase; letter-spacing: .5px; }}
 .hint {{ font-size: 11.5px; color: var(--muted); font-weight: 400; }}
 .cols {{ display: flex; gap: 26px; flex-wrap: wrap; align-items: flex-start; }}
 .col {{ flex: 1 1 460px; min-width: 420px; }}
 .mbars {{ margin-top: 6px; background: var(--surface); border: 1px solid var(--line);
           border-radius: 8px; padding: 10px 14px; }}
 .mbar {{ display: flex; align-items: center; margin: 4px 0; cursor: pointer; }}
 .mbar:hover .mname {{ color: var(--blue); }}
 .mname {{ width: 235px; font-size: 12px; text-align: right; padding-right: 10px;
           overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
           color: var(--ink2); }}
 .mtrack {{ flex: 1; display: flex; align-items: center; gap: 7px; }}
 .mfill {{ height: 8px; border-radius: 4px; min-width: 3px; }}
 .mtrack span {{ font-size: 11px; color: var(--muted); white-space: nowrap;
                 font-variant-numeric: tabular-nums; }}
 .fbtn {{ border: 1px solid var(--line); background: var(--surface);
          border-radius: 15px; padding: 4px 13px; margin-left: 6px;
          cursor: pointer; font-size: 12px; font-family: inherit;
          color: var(--ink2); }}
 .fbtn:hover {{ border-color: var(--blue); color: var(--ink); }}
 .fbtn.on {{ background: var(--ink); color: #fff; border-color: var(--ink); }}
 .subnav {{ position: sticky; top: 0; background: var(--page);
            border-bottom: 1px solid var(--line); padding: 9px 0; z-index: 5; }}
 .subnav a {{ color: var(--ink2); margin-right: 20px; cursor: pointer;
              font-size: 12.5px; font-weight: 600; }}
 .subnav a:hover {{ color: var(--blue); }}
 section {{ scroll-margin-top: 46px; }}
 .legend {{ font-size: 11.5px; color: var(--muted); margin-top: 5px; }}
 .ok {{ color: var(--good-text); font-weight: 600; }}
 input[type=text] {{ font-size: 13px; padding: 7px 12px; width: 320px;
   border: 1px solid var(--line); border-radius: 8px; background: var(--surface);
   font-family: inherit; }}
 input[type=text]:focus {{ outline: 2px solid var(--blue-100);
   border-color: var(--blue); }}
 details summary {{ cursor: pointer; color: var(--ink2); margin-top: 12px;
                    font-size: 12.5px; }}
 details.ctx summary {{ margin-top: 4px; font-size: 11.5px; color: var(--blue);
                        font-weight: 600; }}
 details.ctx pre {{ background: var(--track); border-radius: 6px;
   padding: 8px 10px; margin: 5px 0 2px; font-size: 11px; line-height: 1.55;
   white-space: pre-wrap; word-break: break-all; overflow-x: auto;
   font-family: Consolas, monospace; color: var(--ink2); }}
 a.ilink {{ color: var(--blue); }}
 svg {{ display: block; }}
</style></head><body>
<header><h1>talog 진단 리포트 — {_esc(ctx.title)}</h1>
<div class="meta">{_esc(ctx.day_dir)}{stitch_note}<br>{recipe_html}</div></header>
<nav>
 <button class="tabbtn" id="btn-main" onclick="showTab('main')">종합</button>
 <button class="tabbtn" id="btn-detail" onclick="showTab('detail')">상세 분석</button>
</nav>
<main>
<div class="tabpane" id="pane-main">
 <div class="cards">{card_html}</div>
 {leak_banner}
 {_findings_section(ctx)}
 <h2>타임라인 <span class="hint">— 굵은 선(이상 검사)·▼재시작·✖종료·▲모델로드를
 클릭/호버해 보세요</span></h2>{_timeline_svg(ctx)}
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
  <a onclick="gotoSection('sec-sys')">시스템</a>
 </div>
 <section id="sec-insp">
  <h2>검사 조회</h2>
  <p><input type="text" id="insp-search" placeholder="inner id 또는 product id 검색">
     <button class="fbtn on" data-f="all" onclick="setFilter('all')">전체</button>
     <button class="fbtn" data-f="bad" onclick="setFilter('bad')">이상만</button>
     <button class="fbtn" data-f="ng" onclick="setFilter('ng')">NG</button>
     <button class="fbtn" data-f="ok" onclick="setFilter('ok')">완료</button>
     <button class="fbtn" data-f="eof" onclick="setFilter('eof')">절단</button>
     <button class="fbtn" onclick="exportCsv()" style="margin-left:14px">⬇ CSV 내보내기</button>
     <span id="insp-count" style="color:#898781;font-size:13px;margin-left:10px"></span></p>
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
  <h2>검사시간 분포 (완료 건)</h2>{_duration_hist_svg(ctx)}
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
<script>{js}</script>
</body></html>"""
