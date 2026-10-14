"""검사 단위 구역 — 채널 Tact, 미완료/이상 검사, NG 판정, 프로세스 세대(재시작 이력)."""

from __future__ import annotations

import statistics
from collections import Counter

from .charts import _dist_svg
from .context import _BAD_STATUSES, _STATUS_KO, ReportContext
from .util import _esc, _fmt_ts, _translate_alglist


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
                     f'<polyline points="{poly}" fill="none" '
                     f'style="stroke:var(--serious)" stroke-width="1"/></svg>')
        else:
            spark = ""
        bar = int(avg / max_avg * 220)
        warn = ' style="background:var(--tint-warn)"' if avg >= 30000 else ""
        rows.append(
            f"<tr{warn}><td class='r'>{idx}</td><td>{_esc(ch)}</td><td>{_esc(gpu)}</td>"
            f"<td class='r'>{len(vals)}</td><td class='r'>{vals[0] / 1000:.2f}</td>"
            f"<td class='r'><b>{avg / 1000:.2f}</b></td><td class='r'>{p95 / 1000:.2f}</td>"
            f"<td class='r'>{vals[-1] / 1000:.2f}</td>"
            f"<td><div style='width:{bar}px;height:10px;background:var(--blue);"
            f"border-radius:5px'></div></td><td>{spark}</td></tr>")
    # 소요 상위 채널의 분포 히스토그램 (백분위 마커)
    dist_cards = []
    for idx, ch, pairs, vals, avg in stats[:8]:
        chart = _dist_svg([v for _t, v in pairs], "ms", w=352, h=140)
        if chart:
            dist_cards.append(
                f"<div style='width:352px'><div style='font-size:12px;"
                f"font-weight:600;margin-bottom:3px'>{idx}({_esc(ch)})</div>"
                f"{chart}</div>")
    dist_html = ""
    if dist_cards:
        dist_html = ("<h2>채널별 인퍼런스 분포 <span class='hint'>— 소요 상위 "
                     f"{len(dist_cards)}개 채널 · 주황=상위 5% 꼬리, 굵은 선="
                     "p95</span></h2>"
                     "<div style='display:flex;flex-wrap:wrap;gap:14px'>"
                     + "".join(dist_cards) + "</div>")
    # 사이클타임: 시퀀스 시작 → 결과 송신 완료 (SendResultToOthers, 존 단위)
    sr = [e.value for e in ctx.events if e.kind == "SEND_RESULT"]
    if len(sr) >= 8:
        dist_html += ("<h2>사이클타임 분포 <span class='hint'>— 시퀀스 시작→"
                      "결과 송신 완료(SendResultToOthers, 존 단위). 채널 "
                      "인퍼런스보다 큰 이 값이 설비가 체감하는 소요입니다"
                      "</span></h2>" + _dist_svg(sr, "ms", w=760, h=165))
    return ("<p class='legend'>N = 해당 채널의 인퍼런스 실행 횟수 · "
            "min/avg/p95/max = 채널 인퍼런스 소요(초)</p>"
            "<table class='sortable'><thead><tr><th>alg</th><th>채널</th><th>GPU</th>"
            "<th class='r'>N</th><th class='r'>min(s)</th><th class='r'>avg(s)</th>"
            "<th class='r'>p95(s)</th><th class='r'>max(s)</th>"
            "<th>avg 막대</th><th>시계열(일자 전체)</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table>" + dist_html)


def _incomplete_section(ctx: ReportContext) -> str:
    bad = [i for i in ctx.inspections if i.status in _BAD_STATUSES]
    if not bad:
        return "<p class='ok'>미완료/이상 검사가 없습니다.</p>"
    note = ""
    if len(bad) > 300:
        note = f"<p class='legend'>총 {len(bad)}건 중 앞 300건만 표시합니다.</p>"
        bad = bad[:300]
    n_all_ch = len(ctx.dl_channels) or 0
    rows = []
    for it in bad:
        reason = []
        # 가장 정보량 큰 사유(존 부분 완료)를 첫 줄로
        if it.n_zones and it.n_zones_done < it.n_zones:
            reason.append(f"<b>존 부분 완료</b>: {it.n_zones_done}/{it.n_zones}존만 "
                          f"END 수신")
        if it.ack_status and it.ack_status != "OK":
            zone = f", 존{it.reject_zone} 투입 시점" if it.reject_zone else ""
            reason.append(f"설비 회신 <b>{_esc(it.ack_status)}</b>"
                          f" (가용 스레드 {it.wait_threads}{zone})")
        if it.lost_channels:
            reason.append("실행 중 소실: " + _esc(", ".join(it.lost_channels)))
        if it.n_nofeed and n_all_ch and it.n_nofeed >= n_all_ch:
            # 전 채널 미투입 = 채널 로그 미파싱/절단 의심 — 장황한 나열 대신 축약
            reason.append(f"전 채널({it.n_nofeed}) 실행기록 없음 — 채널 로그 "
                          f"절단/미파싱 의심")
        elif it.nofeed_channels and len(it.nofeed_channels) <= 15:
            reason.append("<b>이상 미투입</b>: " + _esc(", ".join(it.nofeed_channels)))
        elif it.n_nofeed:
            reason.append(f"<b>이상 미투입</b> {it.n_nofeed}채널")
        if it.n_skipped:
            reason.append(f"정상 스킵(레시피 조건 비활성) {it.n_skipped}채널")
        if it.timed_out:
            reason.append("<b>타임아웃</b>: 판정 결과가 설비로 미송신 "
                          "(플랫폼 [TIMEOUT] 조기 리턴)")
        if it.remain_list:
            reason.append("플랫폼 REMAIN 덤프: "
                          + _esc(_translate_alglist(ctx.recipe, it.remain_list)))
        rows.append(f"<tr><td>{_fmt_ts(it.start_text)}</td>"
                    f"<td><a href='#' class='ilink mono' "
                    f"data-id='{_esc(it.inner_id)}'>"
                    f"{_esc(it.inner_id)}</a></td>"
                    f"<td>{_esc(it.product_id)}</td>"
                    f"<td><span class='pill p-{_esc(it.status)}'>"
                    f"{_STATUS_KO.get(it.status, it.status)}</span></td>"
                    f"<td class='r'>{it.n_done}/{it.n_fed}</td>"
                    f"<td>{'<br>'.join(reason) or '-'}</td></tr>")
    return (note + "<table><thead><tr><th>시작</th><th>inner id</th><th>product id</th>"
            "<th>상태</th><th>완료/투입</th><th>사유 (레시피 조인)</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table>")


def _ng_recipe_segments(ctx: ReportContext) -> str:
    """기종(레시피) 교체 경계로 NG% 를 분리 — 혼합 지표의 해석 왜곡 방지.

    감사 C 실증: Tenneco 30 혼합 NG 56% = NUPE 6.3% + THETA3 68.0% 의 착시.
    """
    rl = sorted((m for m in ctx.model_loads
                 if m.kind == "recipe_load" and m.name), key=lambda m: m.ts)
    if not rl:
        return ""
    # 경계 시각들 (같은 레시피 연속 로드는 병합)
    bounds: list[tuple[float, str]] = []
    for m in rl:
        if not bounds or bounds[-1][1] != m.name:
            bounds.append((m.ts, m.name))
    if len(bounds) < 2:
        return ""
    segs = []
    for i, (ts, name) in enumerate(bounds):
        end = bounds[i + 1][0] if i + 1 < len(bounds) else float("inf")
        items = [x for x in ctx.inspections
                 if x.end_result in ("OK", "NG") and x.start_ts
                 and ts <= x.start_ts < end]
        if len(items) < 30:
            continue
        n_ng = sum(1 for x in items if x.end_result == "NG")
        top = Counter(d for x in items for d in x.defects).most_common(3)
        segs.append((name, ts, items, n_ng, top))
    if len(segs) < 2:
        return ""
    rows = []
    import datetime as _dt
    for name, ts, items, n_ng, top in segs:
        rate = n_ng / len(items) * 100
        col = ("var(--crit-text)" if rate >= 30 else
               "var(--serious-text)" if rate >= 10 else "var(--good-text)")
        tops = ", ".join(f"{d} {n:,}" for d, n in top) or "-"
        rows.append(
            f"<tr><td>{_esc(name)}</td>"
            f"<td>{_dt.datetime.fromtimestamp(ts).strftime('%H:%M')}~</td>"
            f"<td class='r'>{len(items):,}</td><td class='r'>{n_ng:,}</td>"
            f"<td class='r' style='color:{col};font-weight:700'>{rate:.1f}%</td>"
            f"<td>{_esc(tops)}</td></tr>")
    return ("<p class='legend'><b>기종(레시피) 구간별 분리</b> — 혼합 NG% 는 "
            "기종별 편차를 가립니다</p>"
            "<table><thead><tr><th>기종</th><th>구간</th><th class='r'>판정</th>"
            "<th class='r'>NG</th><th class='r'>NG%</th><th>상위 결함</th>"
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>")


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
    out.append(_ng_recipe_segments(ctx))
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
