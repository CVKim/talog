"""근거 수치를 사람이 읽는 해석 문장으로 — 스레드·처리 여유·GPU 등 (규칙·LLM·알림이 같은 문장을 쓴다).

완료 신호만 끊긴 모양 판정(completion_gap)도 여기 있다 — 해석과 룰 진단이 함께 쓴다."""

from __future__ import annotations

import statistics


def interpret(ev: dict, p: dict, family: str = "") -> list[str]:
    """근거 수치를 '무엇을 뜻하는지' 문장으로 옮긴다 — 원인 이름은 쓰지 않는다.

    소형 LLM 은 JSON 속 숫자를 잘못 읽는다(Tenneco 0730 11:21: gpu_log.fatal_near 38 을
    보고도 'GPU 오류 없음'). Robot_Sim 벤치에서 도구 출력에 해석 문장을 붙이자 qwen3.5 조치
    정답 49→61%, 해로운 조치 34→22%. 문턱은 룰과 같은 런북 params 를 쓰고, 문장을 묶어
    원인을 정하는 일은 LLM 에게 남긴다. family 를 주면 그 계열 원인을 가르는 문장만 싣는다
    (결함 사건에 GPU·재시작 문장이 섞이면 qwen2.5:7b 가 '원인 미상'으로 물러섬, 9/29 실측)."""
    facts = _interpret_all(ev, p)
    if family == "defect":
        keep = ("치명 결함", "연속 NG", "NG 비율", "타임아웃", "그랩", "조명", "모델 로드",
                "기종", "사용자 패턴")
    elif family == "reject":
        keep = ("GPU", "인퍼런스", "SEH", "크래시", "진행 중", "소요", "커널", "타임아웃",
                "재시작", "모델 로드", "사용자 패턴", "비상 정지", "유휴", "점유", "투입",
                "그룹", "CPU", "메모리", "단계", "지연", "한계")
    else:
        return facts
    return [f for f in facts if any(k in f for k in keep)]


def _interpret_all(ev: dict, p: dict) -> list[str]:
    out: list[str] = []
    w = int(p["gpu_fault_window_s"])
    g, er = ev.get("gpu_log") or {}, ev.get("errors") or {}
    src = str(g.get("source") or "")
    nf, ne = g.get("fatal_near") or 0, g.get("infer_errors_near") or 0
    ns = er.get("seh_near") or 0
    # 조사 성공 여부: 신규 기록은 ok 필드, 예전 기록은 출처 문구로 가린다
    probed = g.get("ok") if "ok" in g else ("색인" in src or "꼬리" in src)
    if nf:
        out.append(f"GPU 치명 오류(enqueueV3 cudaGetLastError) {nf}건이 경보 ±{w}초 안에 있다"
                   f" (첫 오류 {g.get('first_fatal')}).")
    elif probed:
        out.append(f"경보 ±{w}초 안에 GPU 치명 오류가 없다.")
    elif src:
        out.append("GPU 로그(DLInfer.log)를 볼 수 없어 GPU 오류 여부는 알 수 없다.")
    if ne:
        out.append(f"인퍼런스 오류(INFER_ERROR) {ne}건이 경보 ±{w}초 안에 있다.")
    if ns:
        out.append(f"SEH 예외 {ns}건({', '.join(er.get('seh_codes') or [])})이 경보 ±{w}초 안에 "
                   f"있다.")
    if er.get("crash_near"):
        out.append("같은 구간에 프로세스 크래시가 있다.")
    i = ev.get("inspections") or {}
    if i.get("inflight"):
        out.append(f"경보 순간 진행 중인 검사가 {i['inflight']}건이다"
                   + (f" (가장 오래된 것 {i['oldest_inflight_s']}초 경과)."
                      if i.get("oldest_inflight_s") is not None else "."))
    else:
        out.append("경보 순간 진행 중인 검사가 없다.")
    doi = i.get("duration_over_interval")
    pool = i.get("pool")
    gap = completion_gap(i, ev.get("capacity") or {})
    if gap:
        w = i.get("wait_threads_recent") or []
        out.append(f"{_gap_text(i)} — 그런데 최근 도착 순간 유휴 스레드는 "
                   f"{', '.join(str(x) for x in w)}개(스레드 {pool or (ev.get('capacity') or {}).get('pool')}개)"
                   f"로 스레드는 놀고 있다. 처리가 막힌 것이 아니라 완료 신호만 안 보인다.")
    if doi is not None and gap:
        out.append(f"검사 한 건의 소요가 투입 간격의 {doi}배로 계산되지만, 완료 신호 없이 진행 중 "
                   f"경과만 늘어난 값이라 처리 시간으로 보기 어렵다.")
    elif doi is not None and pool:
        # 스레드가 여럿이면 소요가 투입 간격보다 긴 게 정상 — 스레드 수로 나눈 몫으로 말한다
        # ('투입이 처리보다 빠르다' 는 스레드 1개일 때의 해석이라 LLM 을 처리량 포화로 끈다)
        out.append(f"지금 검사 한 건의 소요는 투입 간격({i.get('recent_interval_s')}초)의 {doi}배로, "
                   f"스레드 {pool}개가 감당하는 한계의 {doi / pool * 100:.0f}%다"
                   + (" — 한계에 닿았다." if doi >= pool * 0.95 else "."))
    elif doi is not None:
        out.append(f"지금 검사 한 건의 소요가 투입 간격({i.get('recent_interval_s')}초)의 {doi}배다"
                   f" — 투입이 처리보다 {'빠르다' if doi >= 1 else '느리다'}.")
    sd, base = i.get("slowdown_ratio"), i.get("baseline_duration_s")
    if sd is not None and base:
        # 급변 기준(1.5배)에 못 미쳐도 점진 증가 기준(1.2배) 이상이면 '비슷하다'고 하지 않는다
        out.append(f"최근 완료 소요는 30분 이전 평소({base}초)의 {sd}배다 — "
                   + ("느려졌다." if sd >= p["slowdown_ratio"] else
                      "조금 늘었다." if sd >= p["drift_ratio"] else "평소와 비슷하다."))
    ed = g.get("exec_slowdown_ratio")
    if ed is not None:
        out.append(f"GPU 커널 시간(executeV2)은 직전 구간의 {ed}배다 — "
                   f"{'느려졌다' if ed >= p['exec_slowdown_ratio'] else '평소와 비슷하다'}.")
    t = ev.get("timeouts") or {}
    n_it, n_at = t.get("img_timeout_30min") or 0, t.get("alg_timeout_30min") or 0
    out.append(f"30분 안에 이미지 처리 타임아웃 {n_it}건, 알고리즘 타임아웃 {n_at}건이 있다."
               if (n_it or n_at) else "30분 안에 타임아웃이 없다.")
    r = ev.get("restarts") or {}
    s = r.get("since_restart_s")
    if s is not None:
        out.append(f"마지막 재시작은 {r.get('last_restart')}로 경보 {s:.0f}초 전이다 — "
                   f"{'기동 직후다' if s <= p['not_init_after_restart_s'] else '기동 직후는 아니다'}.")
    else:
        out.append("경보 전 60분 안에 재시작이 없다.")
    if r.get("pool_log_lines_60min"):
        out.append(f"60분 안의 실제 재시작은 {r.get('count_60min')}회다 (풀 생성 로그 "
                   f"{r['pool_log_lines_60min']}줄을 {p['restart_merge_s']}초 단위로 묶은 값).")
    m = ev.get("model_load") or {}
    if m.get("pending"):
        out.append("모델 로드 명령 뒤 완료 응답이 아직 없다.")
    elif m.get("last_cmd"):
        out.append(f"모델 로드는 {m['last_cmd']}에 끝났다 ({m.get('name')}, 경보 "
                   f"{m.get('minutes_since')}분 전{', 기종이 바뀌었다' if m.get('changed') else ''}).")
    d = ev.get("defects") or {}
    hits = d.get("critical_hits") or []
    if hits:
        out.append(f"치명 결함 {len(hits)}건: " + ", ".join(f"{h['name']}({h['time']})"
                                                         for h in hits[-3:]) + ".")
    if d.get("streak"):
        out.append(f"연속 NG 가 {d['streak']}건이다.")
    if d.get("ng_rate_recent") is not None and d.get("inspections_recent"):
        out.append(f"최근 {d['inspections_recent']}검사의 NG 비율은 {d['ng_rate_recent']}% "
                   f"(그 앞 기준선 {d.get('ng_rate_baseline')}%).")
    if d.get("ng_rate_before_change") is not None:
        out.append(f"기종 로드 전 NG 비율은 {d['ng_rate_before_change']}% 였다.")
    for k, lab in (("grab_fail_30min", "그랩 실패"), ("light_unstable_30min", "조명 불안정"),
                   ("emergency_stop_30min", "비상 정지 송신")):
        if er.get(k):
            out.append(f"30분 안에 {lab} {er[k]}건이 있다.")
    for pt in ev.get("patterns") or []:
        if pt.get("lines"):
            out.append(f"사용자 패턴 '{pt['name']}' 원문: {pt['lines'][-1][:160]}")
    out += _interpret_capacity(ev)
    return out


def _gname(g) -> str:
    return f"그룹 {g}" if g not in (None, 0, "0") else "검사 그룹"


def _interpret_capacity(ev: dict) -> list[str]:
    """처리 여유(talog/capacity.py) 근거 → 문장. 원인 이름은 쓰지 않는다.

    풍산 2호기 시험에서 qwen2.5:7b 가 '증가분의 65%는 알고리즘 단계'를 '특정 알고리즘이 늘었다'로 읽어
    결론을 바꿨다. 단계 이름은 '처리(시작~판정)'처럼 쓰고 '여러 처리의 합'임을 밝힌다."""
    out: list[str] = []
    c, t = ev.get("capacity") or {}, ev.get("trend") or {}
    g = _gname(c.get("group"))
    if c.get("idle_median") is not None and c.get("idle_baseline") is not None:
        im, ib = c["idle_median"], c["idle_baseline"]
        out.append(f"{g}의 도착 순간 유휴 스레드는 최근 30분 중앙값 {im:g}개로, 기준선 {ib:g}개"
                   + ("보다 줄었다" if im < ib else "와 같다" if im == ib else "보다 많다")
                   + (f" (최소 {c['idle_min']}개)." if c.get("idle_min") is not None else "."))
    if c.get("limit_s") and c.get("hold_p99") is not None:
        out.append(f"{g}의 처리 한계는 스레드 {c['pool']}개 × 투입 간격 {c['interval_s']}초 = "
                   f"{c['limit_s']}초이고, 최근 60분 점유 p99 는 {c['hold_p99']}초다 — 일시 지연을 "
                   f"{c['tolerance_s']}초까지만 견딘다.")
    if t.get("hold_ratio") is not None:
        mono = t.get("monotonic")
        tail = (f" — {t.get('hours')}시간 동안 꾸준히 늘었다" if mono else
                " — 꾸준히 늘어난 것은 아니다" if mono is False else "")
        out.append(f"{g}의 스레드 점유 시간(중앙값)은 기준선({t.get('since')}부터) "
                   f"{t.get('hold_p50_baseline')}초에서 지금 {t.get('hold_p50_now')}초로 "
                   f"{t['hold_ratio']}배다{tail}.")
    chg = t.get("interval_change_pct")
    if chg is not None:
        out.append(f"같은 기간 투입 간격은 {t.get('interval_baseline_s')}초에서 {t.get('interval_now_s')}초"
                   + (" 그대로다 — 투입(부하)이 늘어난 것이 아니다." if abs(chg) <= 10 else
                      f"로 {abs(chg):.0f}% 짧아졌다 — 투입(부하)이 늘었다." if chg < 0 else
                      f"로 {chg:.0f}% 길어졌다 — 투입이 줄었다."))
    nb = t.get("groups_with_baseline") or 0
    if nb >= 2:
        nr = len(t.get("groups_rising") or [])
        out.append(f"기준선이 있는 {nb}개 그룹 중 {nr}개 그룹의 점유 시간이 10% 이상 늘었다"
                   + (" — 여러 그룹이 함께 늘어 특정 알고리즘 하나보다 프로세스 공통 원인을 가리킨다."
                      if nr >= 2 and nr * 2 >= nb else " — 일부 그룹에 국한된다." if nr else "."))
    ph = ev.get("phases") or {}
    if ph.get("share_of_increase_pct") and any(ph["share_of_increase_pct"]):
        parts = ", ".join(f"{n} {v:g}%" for n, v in zip(ph.get("names") or [], ph["share_of_increase_pct"])
                          if v)
        out.append(f"{_gname(ph.get('group'))}에서 늘어난 점유 시간은 단계별로 {parts} 로 나뉜다 "
                   f"(각 단계는 여러 처리의 합이다).")
    tr = ev.get("transient") or {}
    if tr.get("groups"):
        det = tr.get("detail") or {}
        txt = ", ".join(f"{_gname(k)} 최대 {v['max_last_60s']}초(그 앞 60분 p99 {v['p99_prev_60min']}초)"
                        for k, v in list(det.items())[:3])
        out.append(f"경보 직전 60초 동안 {tr.get('n_groups')}개 그룹의 점유가 평소 최대치를 넘었다: {txt}"
                   + (" — 여러 그룹이 동시에 느려진 일시 지연이 겹쳤다." if (tr.get("n_groups") or 0) >= 2
                      else " — 일시 지연이 겹쳤다."))
    r = ev.get("resources") or {}
    if r.get("total_cpu_now_pct") is not None:
        out.append(f"장비 전체 CPU 는 최근 1시간 평균 {r['total_cpu_now_pct']}%다"
                   + (" — CPU 여유가 적어 일시 지연에 취약하다." if r["total_cpu_now_pct"] >= 85 else "."))
    if r.get("mem_slope_mb_per_h") is not None:
        m = r["mem_slope_mb_per_h"]
        out.append(f"프로세스 메모리는 시간당 {m}MB 변한다"
                   + (" — 누수 경보 기준(시간당 100MB)보다 작다." if abs(m) < 100 else
                      " — 누수 경보 기준(시간당 100MB) 이상이다."))
    return out


def completion_gap(i: dict, c: dict) -> bool:
    """완료 신호만 끊긴 모양인가 — 마지막 완료(INSPECT_END) 뒤 60초 넘게 검사 시작만 20건 이상
    들어왔거나 10분 완료가 시작의 절반 미만인데, 최근 도착 순간 유휴 스레드는 스레드 수에 가깝다
    (처리가 막혔다면 유휴가 0 쪽으로 떨어진다). comm 로그 기록이 멈췄거나(회전·디스크·로거) 설비로
    결과 송신이 끊긴 경우다."""
    pool = c.get("pool") or i.get("pool")
    w = i.get("wait_threads_recent") or []
    if not pool or not w or min(w) < 1 or statistics.median(w) < max(1, pool - 1):
        return False
    age, since = i.get("last_end_age_s"), i.get("starts_since_last_end") or 0
    stopped = since >= 20 and (age is None or age >= 60)
    st, en = i.get("starts_10min") or 0, i.get("ends_10min")
    return stopped or (en is not None and st >= 20 and en < 0.5 * st)


def _gap_text(i: dict) -> str:
    age, since = i.get("last_end_age_s"), i.get("starts_since_last_end") or 0
    if since >= 20 and (age is None or age >= 60):
        return (f"마지막 완료 신호(INSPECT_END)는 경보 {age:.0f}초 전이고 그 뒤로 검사 시작만 {since}건이다"
                if age is not None else f"10분 넘게 완료 신호(INSPECT_END)가 없는데 검사 시작은 {since}건이다")
    return (f"최근 10분 검사 시작 {i.get('starts_10min')}건에 완료 신호(INSPECT_END)는 "
            f"{i.get('ends_10min')}건이다")
