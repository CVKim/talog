"""모델 로드·평균 검사시간 구역과 모델별 GPU 구역."""

from __future__ import annotations

import statistics

from ..events import Event
from .charts import _dist_svg, _series_svg
from .context import ReportContext
from .util import _esc, _fmt_ts


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
        # 스테이지별 Tact 집계 (클릭 시 파이프라인 분해 상세)
        stages: dict[str, dict[str, list[float]]] = {}
        for e in ctx.events:
            if e.kind == "DLINFER_STAGE":
                m = e.model.rsplit(".", 1)[0]
                stages.setdefault(m, {}).setdefault(e.name, []).append(e.value)
        out.append("<h2>모델별 GPU 실행시간(executeV2) 추이</h2>"
                   "<p class='legend'>순수 GPU 커널 실행시간(ms)입니다. 동시 실행이 "
                   "겹치는 시간대에 값이 상승하면 GPU 경합을 의미합니다. "
                   "N = 인퍼런스 실행 횟수. <b>카드를 클릭하면 스테이지 분해</b>"
                   "(전처리→복사→실행→검증)를 보여줍니다.</p>")
        out.append("<div style='display:flex;flex-wrap:wrap;gap:14px'>")
        sd_id = 0
        for m, pairs in sorted(execs.items(),
                               key=lambda kv: -statistics.mean(v for _t, v in kv[1])):
            vals = sorted(v for _t, v in pairs)
            avg = statistics.mean(vals)
            p95 = vals[min(len(vals) - 1, int(len(vals) * 0.95))]
            chart = _series_svg(pairs, ctx.log_start, ctx.log_end)
            sd = ""
            st = stages.get(m, {})
            if st or vals:
                sd_id += 1
                # 파이프라인 순서: 전처리 → H2D 복사 → 커널 실행 → 검증 → 합계
                order = [("ProcessInput", "전처리(ProcessInput)"),
                         ("copyInputToDevice", "입력 복사(H2D)"),
                         ("executeV2", "커널 실행(executeV2)"),
                         ("VerifyOutput", "출력 검증(VerifyOutput)"),
                         ("Infer", "합계(Infer)")]
                data = dict(st)
                data["executeV2"] = vals
                rows_s = []
                mx_avg = max((statistics.mean(v) for k, v in data.items()
                              if v), default=1) or 1
                for key, label in order:
                    sv = sorted(data.get(key, []))
                    if not sv:
                        continue
                    savg = statistics.mean(sv)
                    sp95 = sv[min(len(sv) - 1, int(len(sv) * 0.95))]
                    bw_px = max(2, int(savg / mx_avg * 78))
                    rows_s.append(
                        f"<tr><td>{label}</td>"
                        f"<td class='r' style='white-space:nowrap'>"
                        f"<span style='display:inline-block;width:{bw_px}px;"
                        f"height:7px;background:var(--blue);border-radius:3px;"
                        f"margin-right:6px;vertical-align:1px'></span>"
                        f"{savg:,.1f}</td>"
                        f"<td class='r'>{sp95:,.1f}</td>"
                        f"<td class='r'>{sv[-1]:,.1f}</td></tr>")
                if rows_s:
                    sd = (f"<div class='sdetail' id='sd{sd_id}'>"
                          f"<div class='legend' style='margin:6px 0 3px'>"
                          f"executeV2 분포 (백분위 마커)</div>"
                          + _dist_svg(vals, "ms", w=352, h=140)
                          + "<table style='margin-top:8px'><thead><tr>"
                          "<th>스테이지</th>"
                          "<th class='r'>avg(ms)</th><th class='r'>p95(ms)</th>"
                          "<th class='r'>max(ms)</th>"
                          "</tr></thead><tbody>" + "".join(rows_s)
                          + "</tbody></table>"
                          f"<div class='legend'>스테이지별 N={len(vals):,} "
                          f"(인퍼런스 실행 횟수와 동일)</div></div>")
            click = (f" onclick=\"document.getElementById('sd{sd_id}')"
                     f".classList.toggle('show')\" style='cursor:pointer'"
                     if sd else "")
            out.append(
                f"<div{click}><div style='width:352px'>"
                f"<div style='font-size:12px;font-weight:600;"
                f"margin-bottom:2px'>{_esc(m)}"
                + (" <span class='hint'>▾ 스테이지</span>" if sd else "")
                + f"</div>"
                f"<div style='font-size:11px;color:var(--muted)'>"
                f"N={len(vals):,}회 실행 · avg {avg:.1f}ms · p95 {p95:.1f}ms · "
                f"max {vals[-1]:,.1f}ms</div>"
                f"{chart}</div>{sd}</div>")
        out.append("</div>")
    return "".join(out)


# 설비 응답이 없는 로드 명령 행 표기 (색-단독 신호 방지)
_NO_ACK = ("<span style='color:var(--crit-text);font-weight:600'>"
           "응답 없음</span>")


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
        warn = "#ec835a" if avg >= 30000 else "var(--blue)"
        rows.append(
            f"<div class='mbar' onclick=\"gotoSection('sec-model')\">"
            f"<div class='mname'>{_esc(m)}</div>"
            f"<div class='mtrack'><div style='width:{w}px;background:{warn}' "
            f"class='mfill'></div><span>{avg / 1000:.2f}s · N={n}</span></div></div>")
    more = "" if len(by_model) <= 12 else \
        f"<div class='legend'>외 {len(by_model) - 12}개 모델 — 상세 분석에서 확인</div>"
    return "<div class='mbars'>" + "".join(rows) + "</div>" + more


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
            out.append(f"<p style='background:var(--tint-warn);border:1px solid var(--tint-warn-bd);"
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
            f"<tr{' style=background:var(--tint-crit)' if m.status not in ('OK',) else ''}>"
            f"<td>{_fmt_ts(m.ts_text)}</td><td>{_esc(m.name)}</td>"
            f"<td class='r'>{m.dur_s:.1f}s</td>"
            f"<td>{_esc(m.status) if m.status else _NO_ACK}</td></tr>"
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
        mx_dur = slow[0].dur_s or 1
        rows = "".join(
            f"<tr{' style=background:var(--tint-crit)' if m.status != 'OK' else ''}>"
            f"<td>{_fmt_ts(m.ts_text)}</td><td class='r'>{m.alg_idx}</td>"
            f"<td>{_esc(m.name)}</td>"
            f"<td class='r'><span class='dbar' "
            f"style='--p:{m.dur_s / mx_dur:.2f}'></span>{m.dur_s:.1f}s</td>"
            f"<td>{_esc(m.status)}</td></tr>" for m in slow[:200])
        note = f" (전체 {len(ci)}건 중 소요 상위 200건)" if len(ci) > 200 else ""
        out.append(f"<p class='legend'>재시작/레시피 전환 시마다 채널이 모델을 다시 "
                   f"로드합니다{note}.</p>")
        out.append("<div class='scrollwrap'>"
                   "<table class='sortable'><thead><tr><th>시작</th><th>alg</th>"
                   "<th>채널</th><th class='r'>소요</th><th>상태</th></tr></thead>"
                   "<tbody>" + rows + "</tbody></table></div>")
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
        out.append("<p class='legend'>N = 해당 모델(가중치)의 인퍼런스 실행 횟수 "
                   "(InspectMC 파이프라인 단위)</p>"
                   "<table class='sortable'><thead><tr><th>모델</th><th>GPU</th>"
                   "<th>사용 채널(alg)</th><th class='r'>N</th>"
                   "<th class='r'>min(s)</th><th class='r'>avg(s)</th>"
                   "<th class='r'>p95(s)</th><th class='r'>max(s)</th>"
                   "</tr></thead><tbody>"
                   + "".join(rows) + "</tbody></table>")
    else:
        out.append("<p>모델별 Tact 데이터 없음</p>")
    out.append(_gpu_model_section(ctx))
    return "".join(out)
