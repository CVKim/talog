"""SVG 차트와 그 요약 문장 — 타임라인, 시간대별, 분포, 시계열."""

from __future__ import annotations

import statistics

from .context import _BAD_STATUSES, _STATUS_COLOR, _STATUS_KO, ReportContext
from .util import _c, _esc, _fmt_ts


def _timeline_svg(ctx: ReportContext) -> str:
    if not ctx.log_end or ctx.log_end <= ctx.log_start:
        return "<p>타임라인 데이터 없음</p>"
    w, h = 1120, 104
    span = ctx.log_end - ctx.log_start

    def x(ts: float) -> float:
        return 40 + (ts - ctx.log_start) / span * (w - 80)

    parts = [f'<svg viewBox="0 0 {w} {h}" style="width:100%;'
             f'background:var(--surface);border:1px solid var(--line);'
             f'border-radius:12px;box-shadow:var(--shadow)">']
    import datetime as _dt
    t0 = _dt.datetime.fromtimestamp(ctx.log_start)
    tick = _dt.datetime(t0.year, t0.month, t0.day, t0.hour).timestamp()
    while tick <= ctx.log_end:
        if tick >= ctx.log_start:
            hh = _dt.datetime.fromtimestamp(tick).strftime("%H시")
            parts.append(f'<line x1="{x(tick):.1f}" y1="20" x2="{x(tick):.1f}" y2="76" '
                         f'style="stroke:var(--grid)"/>'
                         f'<text x="{x(tick):.1f}" y="94" font-size="11" '
                         f'text-anchor="middle" class="mut">{hh}</text>')
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
                         f'height="{hh2:.0f}" style="fill:var(--blue-soft)">'
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
                         f'{x(g.start_ts) + 5:.1f},5" style="fill:var(--blue)">'
                         f'<title>프로세스 시작 {g.start_text}</title></polygon>')
        if g.end_cause in ("crash", "kill", "destroy") and g.end_ts:
            parts.append(f'<text x="{x(g.end_ts):.1f}" y="16" font-size="12" '
                         f'text-anchor="middle" fill="#d03b3b">✖<title>종료({g.end_cause}) '
                         f'{g.end_text}</title></text>')
    # 모델 로드 이벤트 마커: 레시피 로드 명령(보라 ▲), 로드 실패(빨간 ▲)
    for m in ctx.model_loads:
        if m.kind == "recipe_load":
            col = "var(--violet)" if m.status == "OK" else "#d03b3b"
            parts.append(f'<text x="{x(m.ts):.1f}" y="{h - 20}" font-size="12" '
                         f'text-anchor="middle" style="fill:{col}">▲<title>모델 로드 '
                         f'{_esc(m.name)} {m.ts_text[:12]} ({_esc(m.status)}, '
                         f'{m.dur_s:.0f}s)</title></text>')
    for e in ctx.events:
        if e.kind == "MODEL_FAIL":
            parts.append(f'<text x="{x(e.ts):.1f}" y="{h - 20}" font-size="12" '
                         f'text-anchor="middle" fill="#d03b3b">▲<title>모델 로드 실패 '
                         f'{_esc(e.model)} {e.ts_text[:12]}</title></text>')
    parts.append("</svg>")
    legend = ('<div class="legend">범례: '
              '<span style="color:var(--good-text)">│완료</span> '
              '<span style="color:var(--serious-text)">┃실행 중 소실</span> '
              '<span style="color:var(--crit-text)">┃시작 거부 · ✖프로세스 종료'
              '</span> <span style="color:var(--blue)">▼프로세스 시작</span> · '
              '굵은 선을 클릭하면 해당 검사 상세로 이동합니다</div>')
    return "".join(parts) + legend


def _timeline_summary(ctx: ReportContext) -> str:
    """타임라인 캡션 — 그림과 같은 데이터로 만든 한 줄 요약 (몇 건·언제부터 언제까지·이상 검사의 종류와 첫 시각)."""
    started = [it for it in ctx.inspections if it.start_ts and not it.status.startswith("sim")]
    if not started:
        return ""
    started.sort(key=lambda it: it.start_ts)
    bad = [it for it in started if it.status in _BAD_STATUSES]
    kinds: dict[str, list] = {}
    for it in bad:
        kinds.setdefault(it.status, []).append(it)
    parts = [f"검사 {len(started):,}건 ({_fmt_ts(started[0].start_text)[:8]} ~ {_fmt_ts(started[-1].start_text)[:8]})"]
    if bad:
        parts.append("<b>이상 " + f"{len(bad)}건</b> — " + ", ".join(
            f"{_STATUS_KO.get(k, k)} {len(v)}건 (처음 {_fmt_ts(v[0].start_text)[:8]})" for k, v in kinds.items()))
    else:
        parts.append("이상 검사 없음")
    n_restart = max(0, len(ctx.gens) - 1)
    crashes = sum(1 for g in ctx.gens if g.end_cause in ("crash", "kill", "destroy"))
    parts.append(f"재시작 {n_restart}회" + (f" · 비정상 종료 {crashes}회" if crashes else ""))
    loads = sum(1 for m in ctx.model_loads if m.kind == "recipe_load")
    if loads:
        parts.append(f"모델 로드 {loads}회")
    return f"<p class='sumline'><b>요약</b> · {' · '.join(parts)}</p>"


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
                     f'height="{bh:.0f}" rx="3" style="fill:var(--blue)">'
                     f'<title>{hh}시 검사 {n}건</title></rect>')
        dh = durs[hh] / maxd * 90
        parts.append(f'<rect x="{xx + bw * 0.58:.0f}" y="{110 - dh:.0f}" '
                     f'width="{bw * 0.28:.0f}" height="{dh:.0f}" rx="2" '
                     f'style="fill:var(--orange)">'
                     f'<title>{hh}시 평균 검사시간 {durs[hh]:.2f}초</title></rect>')
        parts.append(f'<text x="{xx + bw * 0.4:.0f}" y="126" font-size="10" '
                     f'text-anchor="middle" class="mut">{hh}시</text>')
    parts.append(f'<text x="50" y="145" font-size="11" style="fill:var(--blue)">'
                 f'■ 검사 수 (최대 {maxn:,}건)</text>'
                 f'<text x="220" y="145" font-size="11" '
                 f'style="fill:var(--orange)">■ 평균 검사시간(s) '
                 f'(최대 {maxd:.2f}s)</text>')
    parts.append("</svg>")
    # 캡션 — 가장 바쁜 시간과 가장 느린 시간 (그림과 같은 값)
    busy = max(hours, key=lambda hh: len(per_hour[hh]))
    slow = max(hours, key=lambda hh: durs[hh])
    allv = [d for v in per_hour.values() for d in v if d > 0]
    base = statistics.median(allv) if allv else 0
    parts.append(f"<p class='sumline'><b>요약</b> · 가장 바쁜 시간 {busy}시 ({len(per_hour[busy]):,}건) · "
                 f"평균 검사시간이 가장 긴 시간 {slow}시 ({durs[slow]:.2f}초"
                 + (f", 하루 가운데 값 {base:.2f}초의 {durs[slow] / base:.2f}배" if base else "") + ")</p>")
    return "".join(parts)


def _dist_svg(values: list[float], unit: str = "ms", w: int = 520,
              h: int = 158) -> str:
    """지연시간 분포 히스토그램 + 백분위 마커 (p50·avg·p95·p99, 꼬리 강조).

    x축은 p99×1.25 로 클립해 본체가 뭉개지지 않게 하고, 넘는 max 는 주석으로
    표기한다. p95 초과 구간(상위 5%)은 주황으로 음영·착색된다.
    """
    # 음수/비유한 값 방어 (존 병합·스티칭 경계에서 종료<시작 레코드 가능)
    vs = sorted(v for v in values if v >= 0)
    if len(vs) < 8:
        return ""
    n = len(vs)

    def pct(p: float) -> float:
        return vs[min(n - 1, int(n * p))]

    p50, p95, p99 = pct(.5), pct(.95), pct(.99)
    avg = statistics.mean(vs)
    vmax = vs[-1]
    hi = max(p99 * 1.25, p95 * 1.1, avg * 1.2, 1e-9)
    clipped = vmax > hi * 1.02
    fmt = (lambda v: f"{v:,.0f}") if hi >= 20 else (lambda v: f"{v:.2f}")
    nb = max(18, min(30, w // 18))
    step = hi / nb
    bins = [0] * nb
    for v in vs:
        bins[min(nb - 1, max(0, int(v / step)))] += 1
    mxb = max(bins) or 1
    x0, x1 = 8, w - 8
    base, top = h - 26, 34
    bw = (x1 - x0) / nb

    def sx(v: float) -> float:
        return x0 + min(v, hi) / hi * (x1 - x0)

    parts = [f'<svg viewBox="0 0 {w} {h}" style="width:100%;max-width:{w}px;'
             f'background:var(--surface);border:1px solid var(--line);'
             f'border-radius:10px;box-shadow:var(--shadow)">']
    # 상위 5% 꼬리 음영
    parts.append(f'<rect x="{sx(p95):.1f}" y="{top - 4}" '
                 f'width="{max(0.0, x1 - sx(p95)):.1f}" '
                 f'height="{base - top + 4}" '
                 f'style="fill:var(--serious)" fill-opacity=".08"/>')
    for i, cnt in enumerate(bins):
        if not cnt:
            continue
        bh = max(2.0, cnt / mxb * (base - top - 4))
        color = ("var(--serious)" if (i + 0.5) * step > p95
                 else "var(--blue)")
        parts.append(
            f'<rect x="{x0 + i * bw + 1:.1f}" y="{base - bh:.1f}" '
            f'width="{max(1.0, bw - 2):.1f}" height="{bh:.1f}" rx="2" '
            f'style="fill:{color}"><title>{fmt(i * step)}~'
            f'{fmt((i + 1) * step)}{unit}: {cnt:,}건</title></rect>')
    parts.append(f'<line x1="{x0}" y1="{base}" x2="{x1}" y2="{base}" '
                 f'style="stroke:var(--grid)"/>')

    placed: dict[int, float] = {}    # 라벨 행(y) -> 마지막 라벨 x (겹침 방지)

    def marker(v: float, label: str, ly: int, solid: bool = False):
        xx = sx(v)
        # 같은 행의 직전 라벨과 46px 미만이면 다른 행으로 회피
        if ly in placed and abs(xx - placed[ly]) < 46:
            ly = 29 if ly == 13 else 13
            if ly in placed and abs(xx - placed[ly]) < 46:
                return               # 양쪽 다 겹치면 라벨 생략 (선만 유지 아래)
        placed[ly] = xx
        col = "var(--serious-text)" if solid else "var(--muted)"
        dash = "" if solid else ' stroke-dasharray="4 4"'
        wgt = ' font-weight="700"' if solid else ""
        anchor = ("start" if xx < x0 + 30 else
                  "end" if xx > x1 - 30 else "middle")
        parts.append(f'<line x1="{xx:.1f}" y1="{ly + 4}" x2="{xx:.1f}" '
                     f'y2="{base}" style="stroke:{col}"{dash} '
                     f'stroke-width="{1.6 if solid else 1}"/>')
        parts.append(f'<text x="{xx:.1f}" y="{ly}" font-size="9.5" '
                     f'text-anchor="{anchor}" style="fill:{col}"{wgt}>'
                     f'{label}</text>')

    marker(p95, f"p95 {fmt(p95)}", 13, solid=True)   # 강조 마커 우선 배치
    marker(p50, f"p50 {fmt(p50)}", 13)
    marker(avg, f"avg {fmt(avg)}", 29)
    if p99 > p95 * 1.03:
        marker(p99, f"p99 {fmt(p99)}", 29)
    tail_n = n - int(n * 0.95)
    narrow = w < 480       # 좁은 카드는 하단 캡션 겹침 방지를 위해 축약
    parts.append(f'<text x="{x0}" y="{h - 6}" font-size="9.5" class="mut">'
                 f'0{unit}</text>')
    right = f"{fmt(hi)}{unit}"
    if clipped:
        right += "+" if narrow else f" · max {fmt(vmax)}{unit} (축 밖)"
    parts.append(f'<text x="{x1}" y="{h - 6}" font-size="9.5" class="mut" '
                 f'text-anchor="end">{right}'
                 + (f'<title>max {fmt(vmax)}{unit} — 축은 p99×1.25 에서 절단'
                    f'</title>' if clipped and narrow else '') + '</text>')
    if not narrow:
        parts.append(f'<text x="{(x0 + x1) / 2:.0f}" y="{h - 6}" '
                     f'font-size="9.5" class="mut" text-anchor="middle">'
                     f'N={n:,} · 상위 5% {tail_n:,}건</text>')
    parts.append("</svg>")
    return "".join(parts)


def _duration_hist_svg(ctx: ReportContext) -> str:
    durs = [(i.end_ts - i.start_ts) for i in ctx.inspections
            if i.end_ts and i.start_ts and i.status == "complete"]
    return _dist_svg(durs, unit="s", w=1120, h=176)


def _series_svg(pairs: list[tuple[float, float]], t0: float, t1: float,
                w: int = 340, h: int = 74, color: str = "#ec835a",
                unit: str = "ms") -> str:
    """(ts, value) 시계열 미니 차트 (라인 + 영역 채움 + 축 라벨).

    y축: 0 ~ 최대값(좌상단 표기), 중간 그리드 1줄. x축: 시작~끝 시각(HH:MM).
    최대 240pt 샘플링.
    """
    if len(pairs) < 2 or t1 <= t0:
        return ""
    import datetime as _dt
    c = _c(color)
    pts = sorted(pairs)[:: max(1, len(pairs) // 240)]
    vmax = max(v for _t, v in pts) or 1
    base = h - 15                      # y=0 기준선
    top = 13                           # 최대값 위치
    xy = [((t - t0) / (t1 - t0) * (w - 10) + 5,
           base - v / vmax * (base - top)) for t, v in pts]
    poly = " ".join(f"{x:.0f},{y:.1f}" for x, y in xy)
    area = f"{xy[0][0]:.0f},{base} " + poly + f" {xy[-1][0]:.0f},{base}"
    mid = (base + top) / 2
    hhmm0 = _dt.datetime.fromtimestamp(t0).strftime("%H:%M")
    hhmm1 = _dt.datetime.fromtimestamp(t1).strftime("%H:%M")
    return (f'<svg viewBox="0 0 {w} {h}" style="width:{w}px;height:{h}px;'
            f'background:var(--surface);border:1px solid var(--line);'
            f'border-radius:10px;box-shadow:var(--shadow)">'
            f'<line x1="5" y1="{mid:.0f}" x2="{w - 5}" y2="{mid:.0f}" '
            f'style="stroke:var(--grid)" stroke-dasharray="3 4"/>'
            f'<line x1="5" y1="{base}" x2="{w - 5}" y2="{base}" '
            f'style="stroke:var(--grid)"/>'
            f'<polygon points="{area}" style="fill:{c}" fill-opacity=".12"/>'
            f'<polyline points="{poly}" fill="none" style="stroke:{c}" '
            f'stroke-width="1.6" stroke-linejoin="round"/>'
            f'<text x="6" y="10" font-size="8.5" class="mut">'
            f'{vmax:,.1f}{unit}</text>'
            f'<text x="6" y="{base - 2}" font-size="8.5" class="mut">0</text>'
            f'<text x="6" y="{h - 3}" font-size="8.5" class="mut">{hhmm0}</text>'
            f'<text x="{w - 6}" y="{h - 3}" font-size="8.5" class="mut" '
            f'text-anchor="end">{hhmm1}</text></svg>')
