"""시스템 자원 구역 — 메모리(누수 추세 판정 포함)와 GPU 자원."""

from __future__ import annotations

import statistics

from .charts import _series_svg
from .context import ReportContext
from .util import _esc


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
        out.append(f"<p style='background:var(--tint-crit);border:1px solid var(--tint-crit-bd);"
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


def _gpu_mini(ctx: ReportContext) -> str:
    """종합 페이지용: GPU VRAM·온도 미니 요약 (클릭 시 시스템·GPU 상세로).

    [GPU STATUS] 데이터가 있을 때만 표시한다.
    """
    status = [e for e in ctx.events if e.kind == "GPU_STATUS"]
    if not status:
        return ("<h2>GPU</h2><p class='legend'>이 로그에는 GPU 계측"
                "([GPU STATUS])이 없습니다 — 신규 talos-vision 빌드부터 "
                "DLInfer.log 에 기록됩니다. 상세 분석 › 시스템·GPU 에 확보 "
                "방법을 안내합니다.</p>")
    by_gpu: dict[str, list] = {}
    for e in status:
        by_gpu.setdefault(e.status or "0", []).append(e)
    out = ["<h2>GPU <span class='hint'>— 클릭 시 상세(시스템·GPU)</span></h2>"]
    for g in sorted(by_gpu):
        evs = by_gpu[g]
        mem = [(e.ts, e.value) for e in evs]
        temps = [float(e.name) for e in evs if e.name and float(e.name) > 0]
        tmax = max(temps, default=0.0)
        twarn = (" <span style='color:var(--crit-text);font-weight:700'>⚠"
                 "</span>" if tmax >= 85 else "")
        out.append(
            f"<div onclick=\"gotoSection('sec-sys')\" style='cursor:pointer'>"
            f"<div style='font-size:13px;margin-bottom:4px'>GPU {_esc(g)} — "
            f"VRAM(MB), 온도 최고 <b>{tmax:.0f}°C</b>{twarn}</div>"
            + _series_svg(mem, ctx.log_start, ctx.log_end,
                          w=520, h=80, color="#2a78d6", unit="MB") + "</div>")
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
        out.append("<h3>GPU별 VRAM·온도 (NVML 스냅샷, 인퍼런스 전후)</h3>")
        for g in sorted(by_gpu):
            evs = by_gpu[g]
            mem = [(e.ts, e.value) for e in evs]
            mvals = sorted(v for _t, v in mem)
            # 온도 0°C 는 NVML 읽기 실패(무효 리딩) — 추세에서 제외하고 따로 집계
            temp_all = [float(e.name or 0) for e in evs if e.name]
            n_zero = sum(1 for v in temp_all if v == 0)
            temp = [(e.ts, float(e.name)) for e in evs
                    if e.name and float(e.name) > 0]
            tvals = sorted(v for _t, v in temp)
            tmax = tvals[-1] if tvals else 0.0
            tavg = statistics.mean(tvals) if tvals else 0.0
            tcol = "#d03b3b" if tmax >= 85 else "#eb6834"
            twarn = " ⚠" if tmax >= 85 else ""
            zero_chip = (f"<span class='chip'>온도 무효 리딩(0°C) "
                         f"<b>{n_zero:,}</b>회 — NVML 읽기 실패, 추세 제외"
                         f"</span>" if n_zero else "")
            out.append(
                f"<div class='chips'>"
                f"<span class='chip'>GPU {_esc(g)} 샘플 <b>{len(evs):,}</b>개"
                f"</span>"
                f"<span class='chip'>VRAM <b>{mvals[0]:,.0f} ~ "
                f"{mvals[-1]:,.0f}MB</b> (평균 "
                f"{statistics.mean(mvals):,.0f})</span>"
                f"<span class='chip'>온도 평균 <b>{tavg:.0f}°C</b> · 최고 "
                f"<b>{tmax:.0f}°C</b>{twarn}</span>{zero_chip}</div>")
            out.append(
                "<div style='display:flex;flex-wrap:wrap;gap:16px'>"
                f"<div><div style='font-size:12px;font-weight:600'>GPU {_esc(g)}"
                f" — VRAM 사용량(MB)</div>"
                + _series_svg(mem, ctx.log_start, ctx.log_end,
                              w=430, h=100, color="#2a78d6", unit="MB")
                + f"</div><div><div style='font-size:12px;font-weight:600'>"
                f"GPU {_esc(g)} — 온도(°C)</div>"
                + _series_svg(temp, ctx.log_start, ctx.log_end,
                              w=430, h=100, color=tcol, unit="C")
                + "</div></div>")

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
