"""룰 기반 자동 진단 엔진.

PC3 0727 RCA 등에서 검증된 진단 플레이북을 코드로 내장하여, LLM 없이도
리포트 최상단에 사람이 읽는 소견(findings)을 자동 서술한다.

각 소견: (심각도, 제목, 근거 문장, 권고) — 심각도 crit > warn > info.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field

from .assemble import ChannelRun, Inspection, ModelLoad, ProcessGen
from .events import Event


@dataclass(slots=True)
class Finding:
    severity: str          # crit / warn / info / ok
    title: str
    evidence: list[str] = field(default_factory=list)
    advice: str = ""


def _fmt_t(text: str) -> str:
    return text[:8] if text else "?"


# ---------------------------------------------------------------------------
def diagnose(inspections: list[Inspection], runs: list[ChannelRun],
             gens: list[ProcessGen], events: list[Event],
             model_loads: list[ModelLoad], log_start: float, log_end: float,
             usage_result=None, recipe=None) -> list[Finding]:
    out: list[Finding] = []
    prod = [i for i in inspections if not i.status.startswith("sim")]
    bad = [i for i in prod if i.status in
           ("rejected", "incomplete_lost", "incomplete", "unknown")]

    # 1) 미완료 검사와 원인 체인 ------------------------------------------------
    if bad:
        ev: list[str] = []
        kills = [g for g in gens if g.end_cause in ("kill", "crash")]
        gpu_fatal_ts = [e.ts for e in events if e.kind == "GPU_FATAL"]
        for it in bad[:8]:
            # 검사 시작 직후~15분 내의 종료만 소실 원인 후보로 본다
            # (시작 이전의 재시작은 이 검사와 무관)
            near = next((g for g in kills if g.end_ts and
                         -60 < g.end_ts - (it.start_ts or 0) < 900), None)
            # 거부 ±2분 내 GPU 컨텍스트 치명 오류 → 스레드 고갈의 근원 특정
            near_gpu = any(abs(t - (it.start_ts or 0)) <= 120
                           for t in gpu_fatal_ts)
            if it.status == "rejected":
                zone = f", 존{it.reject_zone} 투입 시점" if it.reject_zone else ""
                cause = (" — GPU 컨텍스트 사망(enqueueV3 오류)으로 Seq 스레드 "
                         "전원 블로킹" if near_gpu
                         else " — 직전 검사들이 스레드를 점유한 상태")
                ev.append(f"{_fmt_t(it.start_text)} {it.inner_id}: 설비 회신 "
                          f"{it.ack_status} (가용 Seq 스레드 "
                          f"{it.wait_threads}{zone}){cause}")
            elif it.status == "incomplete_lost":
                cause = (f", {_fmt_t(near.end_text)} 프로세스 "
                         + ("kill과 시간 일치" if near.end_cause == "kill"
                            else "크래시와 시간 일치") if near else "")
                if near_gpu:
                    cause += " (동시간 GPU 컨텍스트 오류 동반)"
                ev.append(f"{_fmt_t(it.start_text)} {it.inner_id}: "
                          f"{', '.join(it.lost_channels[:4])} 실행 중 소실{cause}")
            elif it.n_zones and it.n_zones_done < it.n_zones:
                cause = (f" — {_fmt_t(near.end_text)} "
                         + ("kill 재시작으로 잔여 존 유실"
                            if near.end_cause == "kill"
                            else "크래시로 잔여 존 유실") if near else "")
                if near_gpu:
                    cause += " (동시간 GPU 컨텍스트 오류 동반)"
                ev.append(f"{_fmt_t(it.start_text)} {it.inner_id}: "
                          f"존 {it.n_zones_done}/{it.n_zones}만 완료{cause}")
            else:
                ev.append(f"{_fmt_t(it.start_text)} {it.inner_id}: {it.n_fed}채널 "
                          f"투입 후 완료 신호 없음")
        if len(bad) > 8:
            ev.append(f"… 외 {len(bad) - 8}건")
        advice = ("실행 중 재시작(kill)이 원인인 건은 재시작 전 큐 드레인 대기 정책을, "
                  "NoInspThread 거부는 병목 채널 개선 또는 Seq 스레드 정책 협의를 "
                  "권장합니다.")
        out.append(Finding("crit", f"검사 미완료 {len(bad)}건", ev, advice))

    # 2) 재시작/크래시 패턴 ----------------------------------------------------
    restarts = max(0, len(gens) - 1)
    crashes = sum(1 for e in events if e.kind == "CRASH")
    if restarts >= 3 or crashes:
        ev = []
        for g in gens:
            if g.end_cause in ("kill", "crash"):
                ev.append(f"{_fmt_t(g.end_text)} 세대{g.gen_id} 종료"
                          f"({'kill 스크립트' if g.end_cause == 'kill' else '크래시'}), "
                          f"가동 {((g.end_ts - g.start_ts) / 60):.0f}분")
        # 재시작 사이 간격이 3분 미만인 연속 재시작(불안정 구간) 탐지
        rapid = 0
        for a, b in zip(gens, gens[1:]):
            if a.end_ts and (b.start_ts - a.end_ts) >= 0 \
                    and a.end_ts - a.start_ts < 180:
                rapid += 1
        if rapid:
            ev.append(f"3분 미만 가동 후 재종료된 세대 {rapid}개 — 반복 재시작 구간 존재")
        sev = "crit" if crashes else "warn"
        out.append(Finding(sev, f"프로세스 재시작 {restarts}회"
                           + (f", 크래시 {crashes}회" if crashes else ""),
                           ev[:8],
                           "재시작이 검사 진행 중에 실행되면 해당 검사가 소실됩니다. "
                           "재시작 전 신규 투입 차단 + 진행분 완료 대기 절차를 권장합니다."))

    # 3) 처리량 병목 채널 -------------------------------------------------------
    by_ch: dict[tuple[int, str], list[float]] = {}
    for r in runs:
        if r.status == "done" and r.infer_ms > 0:
            by_ch.setdefault((r.alg_idx, r.channel), []).append(r.infer_ms)
    if by_ch and prod:
        starts = sorted(i.start_ts for i in prod if i.start_ts)
        # 하한 0.5초: 고케이던스(초 단위 takt) 사이트에서 정상 간격이
        # 걸러져 유휴 간격만 남는 오산출을 방지한다
        gaps = [b - a for a, b in zip(starts, starts[1:]) if 0.5 < b - a < 1800]
        takt = statistics.median(gaps) if gaps else 0
        slow = [(idx, ch, statistics.mean(v), max(v))
                for (idx, ch), v in by_ch.items()
                if takt and statistics.mean(v) / 1000 > takt * 0.7]
        slow.sort(key=lambda s: -s[2])
        if slow:
            ev = [f"투입 주기(중앙값) {takt:.1f}초 대비:"]
            for idx, ch, avg, mx in slow[:6]:
                ev.append(f"  alg{idx}({ch}) 평균 {avg / 1000:.2f}s / "
                          f"최대 {mx / 1000:.2f}s")
            out.append(Finding("warn", f"투입 주기 대비 느린 채널 {len(slow)}개",
                               ev,
                               "해당 채널이 파이프라인 슬롯을 장기 점유하면 연속 투입 시 "
                               "스레드 고갈로 이어질 수 있습니다. 모델 경량화 또는 GPU "
                               "분산 배치를 검토하십시오."))
        elif takt:
            out.append(Finding("ok", "처리량 여유",
                               [f"투입 주기(중앙값) {takt:.1f}초, 최다 소요 채널 평균 "
                                f"{max(statistics.mean(v) for v in by_ch.values()) / 1000:.2f}s"],
                               ""))

    # 4) GPU 경합 (executeV2 열화) ---------------------------------------------
    execs: dict[str, list[tuple[float, float]]] = {}
    for e in events:
        if e.kind == "DLINFER_EXEC":
            execs.setdefault(e.model.rsplit(".", 1)[0], []).append((e.ts, e.value))
    contended = []
    for m, pairs in execs.items():
        vals = sorted(v for _t, v in pairs)
        if len(vals) < 30:
            continue
        p10 = vals[int(len(vals) * 0.10)]
        p95 = vals[min(len(vals) - 1, int(len(vals) * 0.95))]
        if p10 > 0 and p95 / p10 >= 4 and p95 >= 200:
            contended.append((m, p10, p95))
    if contended:
        contended.sort(key=lambda c: -c[2] / max(c[1], 0.1))
        ev = [f"{m}: 한산 시 {p10:.0f}ms → 혼잡 시 p95 {p95:.0f}ms "
              f"({p95 / max(p10, 0.1):.1f}배)" for m, p10, p95 in contended[:6]]
        out.append(Finding("warn", f"GPU 경합 열화 의심 모델 {len(contended)}개",
                           ev,
                           "동시 실행이 겹치는 시간대에 순수 GPU 실행시간이 수 배로 "
                           "늘어나는 패턴입니다. 모델 GPU 재배치(dev index) 또는 동시 "
                           "실행 수 제한을 검토하십시오."))

    # 5) 모델 로드 문제 ---------------------------------------------------------
    fails = [e for e in events if e.kind == "MODEL_FAIL"]
    if fails:
        models = sorted({e.model for e in fails})
        out.append(Finding("crit",
                           f"모델 로드 실패 {len(fails)}건 ({', '.join(models)})",
                           [f"{_fmt_t(fails[0].ts_text)} 최초 발생 — 해당 채널은 "
                            f"검사 불가 상태였습니다"],
                           "모델 파일 배포 상태와 TRT 엔진/드라이버 호환성을 "
                           "점검하십시오 (exception.log 의 CTRTLoader::Build 콜스택 "
                           "동반 여부 확인)."))
    slow_loads = [m for m in model_loads
                  if m.kind == "recipe_load" and m.dur_s > 60]
    if slow_loads:
        ev = [f"{_fmt_t(m.ts_text)} {m.name}: {m.dur_s:.0f}초 ({m.status})"
              for m in slow_loads[:5]]
        out.append(Finding("info", f"레시피/모델 로드 60초 초과 {len(slow_loads)}건",
                           ev, "로드 시간 동안 라인이 대기하므로 모델 수/엔진 캐시를 "
                               "점검할 가치가 있습니다."))

    # 6) 메모리 추세 ------------------------------------------------------------
    if usage_result is not None:
        _series, slope, rise, span, suspicious = usage_result
        if suspicious:
            out.append(Finding("crit", "메모리 증가 추세 의심 (릭 가능성)",
                               [f"{span:.1f}시간 동안 상한 기준 +{rise:,.0f}MB "
                                f"(≈ {slope:,.0f}MB/h)"],
                               "장시간 가동 시 OOM/성능 저하 위험이 있습니다. 해당 "
                               "구간에 로드/해제가 반복된 모듈을 우선 점검하십시오."))
        elif span >= 2:
            out.append(Finding("ok", "메모리 추세 안정",
                               [f"{span:.1f}시간 구간 추세 {slope:+,.0f}MB/h"], ""))

    # 6.5) 타임아웃 / 그랩 실패 / 인퍼런스 오류 (코드 검증 룰) --------------------
    n_ato = sum(1 for e in events if e.kind == "ALG_TIMEOUT")
    n_ito = sum(1 for e in events if e.kind == "IMG_TIMEOUT")
    if n_ato or n_ito:
        ev = []
        if n_ato:
            imgs = sorted({e.roi_idx for e in events
                           if e.kind == "ALG_TIMEOUT" and e.roi_idx})[:8]
            ev.append(f"알고리즘 타임아웃 {n_ato}건 (TIME_OUT NG 강제) — "
                      f"이미지 인덱스: {', '.join(map(str, imgs))}")
        if n_ito:
            ev.append(f"판정 미송신(조기 리턴) {n_ito}건 — 설비 측은 미검사로 처리")
        out.append(Finding("crit" if n_ito else "warn",
                           f"검사 타임아웃 {n_ato + n_ito}건", ev,
                           "타임아웃이 발생한 이미지(FOV)의 알고리즘 처리시간을 "
                           "확인하고, GPU 경합/모델 부하를 점검하십시오."))
    n_grab = sum(1 for e in events if e.kind == "GRAB_FAIL")
    if n_grab:
        out.append(Finding("crit", f"그랩 실패 {n_grab}건",
                           ["카메라/트리거 계통 실패 — 조명 소등 및 설비 정지로 "
                            "이어지는 경로입니다"],
                           "카메라 연결·트리거 신호·프레임 그래버 상태를 "
                           "점검하십시오 (알고리즘 문제가 아님)."))
    n_ie = sum(1 for e in events if e.kind == "INFER_ERROR")
    if n_ie:
        out.append(Finding("crit", f"GPU 인퍼런스 실행 오류 {n_ie}건",
                           ["해당 모델 결과가 통째로 누락됩니다 — VRAM 부족/"
                            "드라이버 장애 신호"],
                           "동일 GPU·모델 조합에서 반복되는지 확인하고 VRAM/"
                           "드라이버를 점검하십시오."))
    for kind, title, advice in (
            ("STORAGE_LOW", "이미지 저장 공간 부족",
             "저장소 정리 또는 보존 주기 축소가 필요합니다 (StorageNotEnough "
             "정지로 이어짐)."),
            ("LIGHT_UNSTABLE", "조명 컨트롤러 불안정",
             "조명 컨트롤러 연결/전원을 점검하십시오 (LightDisconnected 정지 "
             "경로).")):
        n = sum(1 for e in events if e.kind == kind)
        if n:
            out.append(Finding("crit", f"{title} {n}건", [], advice))

    # 6.3) GPU 컨텍스트 치명 오류 (감사 D: 스레드 전원 블로킹 → NoInspThread 근원)
    n_gf = sum(1 for e in events if e.kind == "GPU_FATAL")
    if n_gf:
        first_gf = next(e for e in events if e.kind == "GPU_FATAL")
        out.append(Finding("crit", f"GPU 컨텍스트 치명 오류 {n_gf}건 (enqueueV3)",
                           [f"{_fmt_t(first_gf.ts_text)} 최초 — CUDA 컨텍스트가 "
                            f"죽으면 인퍼런스 스레드가 전원 블로킹되어 "
                            f"NoInspThread 거부·검사 소실로 이어집니다"],
                           "GPU 드라이버/VRAM 상태를 점검하고, 재발 시 해당 "
                           "시간대 전력/온도 이력을 함께 확인하십시오."))
    # 6.4) SEH 예외 (SafeCall 포착 — 액세스 위반 등)
    seh = [e for e in events if e.kind == "EXC_SAFE"]
    if seh:
        codes = Counter((e.status, e.block) for e in seh)
        ev = [f"{code} @ {loc} — {n}건" for (code, loc), n in
              codes.most_common(4)]
        out.append(Finding("crit", f"SEH 예외 {len(seh)}건 (SafeCall 포착)",
                           ev, "0xc0000005(액세스 위반)가 반복되면 해당 위치의 "
                           "방어 코드 점검을 플랫폼 팀과 협의하십시오."))
    # 6.45) 스레드 정지 시점 진행 중 검사 강제 중단 (StopThread)
    stops = [e for e in events if e.kind == "STOP_ALL" and e.value > 0]
    if stops:
        out.append(Finding("crit",
                           f"스레드 정지 시점 진행 중 검사 강제 중단 {len(stops)}회",
                           [f"{_fmt_t(e.ts_text)} 정지 시 실행 중 스레드 "
                            f"{e.value:.0f}개" for e in stops[:5]],
                           "재기동/모델 교체 전 진행 중 검사 완료 대기 절차를 "
                           "권장합니다."))
    # 6.47) 이미지 저장 경로 지연 (100ms 초과만 로그되는 자체 필터 신호)
    slow_fs = [e.value for e in events if e.kind == "SYMLINK_SLOW"]
    if slow_fs and max(slow_fs) >= 500:
        out.append(Finding("warn",
                           f"이미지 저장 경로 지연 {len(slow_fs)}건 "
                           f"(최대 {max(slow_fs):.0f}ms)",
                           ["저장 디스크/NAS 지연은 스레드 점유 시간을 늘려 "
                            "NoInspThread 계열 장애의 선행지표가 됩니다"],
                           "저장 경로 디스크 상태(조각화/네트워크)를 점검하십시오."))
    # 6.48) 기종(레시피) 교체 후 특정 결함 급증 — 티칭/임계 부적합 신호.
    # 같은 레시피의 연속 재로드는 경계가 아니다 — 이름이 바뀌는 지점만 경계로
    # 삼고, before/after 를 인접 경계로 닫아 1회만 발화한다.
    rl = sorted((m for m in model_loads if m.kind == "recipe_load"
                 and m.name), key=lambda m: m.ts)
    bounds = [m for i, m in enumerate(rl)
              if i == 0 or m.name != rl[i - 1].name]
    fired: set[tuple[str, str]] = set()
    for bi, m in enumerate(bounds):
        prev_ts = bounds[bi - 1].ts if bi > 0 else 0.0
        next_ts = bounds[bi + 1].ts if bi + 1 < len(bounds) else float("inf")
        before = [i for i in prod if i.end_result in ("OK", "NG")
                  and i.start_ts and prev_ts <= i.start_ts < m.ts - 600]
        after = [i for i in prod if i.end_result in ("OK", "NG")
                 and i.start_ts and m.ts + 600 < i.start_ts < next_ts]
        if len(before) < 100 or len(after) < 100:
            continue

        def _share(items, d):
            return sum(1 for i in items if d in i.defects) / max(1, len(items))

        after_defs = Counter(d for i in after for d in i.defects)
        for d, _n in after_defs.most_common(3):
            if (m.name, d) in fired:
                continue
            sb, sa = _share(before, d), _share(after, d)
            if sb < 0.01 and sa > 0.20:
                fired.add((m.name, d))
                out.append(Finding(
                    "warn", f"기종 교체 후 '{d}' 판정 급증",
                    [f"{_fmt_t(m.ts_text)} '{m.name}' 구간 — 직전 기종 "
                     f"{sb * 100:.1f}% → 이 기종 {sa * 100:.1f}% "
                     f"({sum(1 for i in after if d in i.defects):,}"
                     f"/{len(after):,}건)"],
                    "실제 불량률로 보기 어려운 계단 점프입니다 — 해당 기종 "
                    "레시피의 측정 임계/티칭 적합성을 점검하십시오."))
                break

    # 6.5) GPU 리소스 신호 (v1.4) ------------------------------------------------
    # 0°C 는 NVML 읽기 실패(무효 리딩) — 통계에서 제외
    temps = [float(e.name) for e in events
             if e.kind == "GPU_STATUS" and e.name and float(e.name) > 0]
    if temps and max(temps) >= 85:
        out.append(Finding("crit", f"GPU 온도 임계 초과 (최고 {max(temps):.0f}°C)",
                           ["NVML 스냅샷([GPU STATUS]) 기준 85°C 이상 —  "
                            "스로틀링으로 인퍼런스 Tact 열화 가능"],
                           "GPU 냉각(팬/먼지/함체 통풍)을 점검하십시오."))
    gw = sorted(e.value for e in events if e.kind == "GPU_WAIT" and e.value > 0)
    if gw and len(gw) >= 20:
        p95 = gw[min(len(gw) - 1, int(len(gw) * 0.95))]
        if p95 >= 1000:
            out.append(Finding(
                "warn", f"GPU 전역 락 대기 p95 {p95 / 1000:.1f}s ({len(gw):,}회)",
                ["비상주(on memory infer=0) 모델 경로에서 여러 채널이 같은 "
                 "GPU 를 직렬로 대기했습니다"],
                "해당 모델의 on memory infer=1 전환(VRAM 여유 필요) 또는 "
                "GPU 부하 분산을 검토하십시오."))
    if recipe is not None and getattr(recipe, "models", None):
        offmem = [m for m in recipe.models.values() if not m.on_memory]
        if offmem:
            names = ", ".join(m.name for m in offmem[:6])
            out.append(Finding(
                "info", f"비상주(on memory infer=0) 모델 {len(offmem)}개",
                [f"{names} — 매 요청마다 GPU 락→로드→추론→언로드를 반복하며 "
                 f"인스턴스 수가 1로 강제됩니다"],
                "요청이 잦은 모델이면 on memory infer=1 전환을 검토하십시오 "
                "(VRAM 사용량 증가와 맞바꿈)."))

    # 7) 통신/기타 에러 클러스터 -------------------------------------------------
    # [WARNING] 접두 라인은 플랫폼이 스스로 '경고'로 분류한 것 — 에러 집계 제외
    err_n = sum(1 for e in events
                if e.kind in ("ERROR", "COMM_FAIL", "RECIPE_FAIL", "EXC_REDIRECT")
                and not e.extra.startswith("[WARNING]"))
    if err_n >= 20:
        out.append(Finding("info", f"에러/예외 로그 {err_n}건",
                           ["대부분 재시작 구간의 소켓 단절/재연결이면 무해하나, "
                            "에러 전체 탭에서 그룹별 분포를 확인하십시오."], ""))

    # 8) 로그 절단(수집 시점) 안내 -----------------------------------------------
    eof_n = sum(1 for i in prod if i.status == "in_progress_eof")
    if eof_n:
        out.append(Finding("info", f"로그 절단 구간 검사 {eof_n}건 (판정 불가)",
                           ["로그가 가동 중에 복사되어 완료 신호(comm) 커버리지 "
                            "밖에서 시작된 검사입니다. 실제 미완료가 아니라 판정 "
                            "불가이며, 해당 일자 로그를 다시 수집하면 완료로 "
                            "확정됩니다 (익일 로그가 있으면 자동 스티칭)."], ""))

    if prod and not bad and not crashes and restarts < 3 and not eof_n:
        out.insert(0, Finding("ok", "이 날은 특기할 이상이 없습니다",
                              [f"양산 검사 {len(prod)}건 전수 완료 신호 확인"], ""))
    return out
