# -*- coding: utf-8 -*-
"""처리 여유 모델(talog/capacity.py)과 그룹 단위 검사 추적(watch.RuleEngine) 테스트.

특정 설비 값에 기대지 않도록 가상 설비를 만들어 넣는다: 그룹 수·스레드 수·투입 간격·점유 시간
추세·수신 줄(groupId) 유무를 바꿔 가며 같은 규칙이 맞게 동작하는지 본다. 근거가 된 실제 사례는
풍산 2호기 10/12 (12그룹×6스레드, 수신 줄 있음, 5일에 걸친 점유 시간 증가)다.
"""

from __future__ import annotations

import datetime as dt
import json
import os

from talog import watch
from talog.agent import RuleDiagnoser, Runbook, gate, interpret
from talog.capacity import CapacityModel
from talog.events import Event
from talog.watch import Alert, Notifier, RuleEngine

_BASE = dt.datetime(2026, 10, 11, 0, 0, 0).timestamp()


def _ev(ts, kind, **kw) -> Event:
    tt = dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]
    return Event(ts=ts, ts_text=tt, kind=kind, **kw)


def _engine(tmp_path, **cap):
    cfg = watch.load_config("")
    cfg["alert_dir"] = str(tmp_path)
    cfg["rules"]["capacity"].update(cap)
    n = Notifier(cfg, replay=True)
    return cfg, n, RuleEngine(cfg, n)


def _site(groups=(1,), pool=6, hours=12.0, interval=lambda h: 6.0, hold=lambda h: 14.0,
          recv=True, ack=True, seq=True, stagger=0.0, seq_index=None, start=_BASE):
    """가상 설비의 이벤트 열. 제품 하나(inner)가 groups 를 stagger 초 간격으로 차례로 지난다.

    도착 순간 유휴 = 풀 − (그 그룹에서 아직 끝나지 않은 검사 수). 유휴 0 이면 거부."""
    out = []
    free = {g: [0.0] * pool for g in groups}          # 스레드별 반환 시각
    t, k = start, 0
    end = start + hours * 3600
    while t < end:
        h = (t - start) / 3600
        inner = f"2000026101100{k:06d}"
        for gi, g in enumerate(groups):
            tt = t + gi * stagger
            slots = free[g]
            idle = sum(1 for x in slots if x <= tt)
            if recv:
                out.append(_ev(tt, "INSP_RECV", inner_id=inner, value=float(g)))
            out.append(_ev(tt + 0.001, "INSP_START", inner_id=inner, value=float(idle)))
            if idle == 0:
                out.append(_ev(tt + 0.002, "INSP_REJECT"))
                if ack:
                    out.append(_ev(tt + 0.008, "COMM_MSG", inner_id=inner, value=float(g),
                                   name="V2M_INSPECT_START_ACK", status="NoInspThread"))
                continue
            slot = next(i for i, x in enumerate(slots) if x <= tt)
            d = hold(h)
            slots[slot] = tt + d
            n = seq_index(g) if seq_index else g
            if ack:
                out.append(_ev(tt + 0.008, "COMM_MSG", inner_id=inner, value=float(g),
                               name="V2M_INSPECT_START_ACK", status="OK"))
            if seq:
                out.append(_ev(tt + 0.004, "SEQ_START", inner_id=inner, alg_idx=n,
                               obj_id=f"o{g}-{slot}"))
                out.append(_ev(tt + d * 0.80, "PROC_TIME", alg_idx=n, obj_id=f"o{g}-{slot}",
                               value=d * 800))
                out.append(_ev(tt + d * 0.85, "SEND_RESULT", alg_idx=n, obj_id=f"o{g}-{slot}",
                               value=d * 850))
                out.append(_ev(tt + d, "SEQ_FINISH", alg_idx=n, obj_id=f"o{g}-{slot}",
                               value=d * 1000))
            out.append(_ev(tt + d * 0.85, "COMM_MSG", inner_id=inner, value=float(g),
                           name="V2M_INSPECT_END", status="OK"))
        t += interval(h)
        k += 1
    out.sort(key=lambda e: e.ts)
    return out


def _run(engine, events, until=None):
    nxt = events[0].ts
    for e in events:
        if until and e.ts > until:
            break
        engine.feed(e)
        if e.ts >= nxt:
            engine.evaluate(e.ts)
            nxt = e.ts + 20
    engine.evaluate(events[-1].ts if not until else until)


def _first(notifier, rule, sev=None):
    for a in notifier.sent:
        if a.rule == rule and (sev is None or a.severity == sev):
            return a
    return None


# ── 그룹 단위 추적 ───────────────────────────────────────────
def test_shared_inner_across_zones_tracked_per_group(tmp_path):
    """같은 inner 가 그룹 1→5→9 를 1.5초 간격으로 지나는 설비: 그룹마다 자기 소요가 나온다."""
    _c, _n, eng = _engine(tmp_path)
    hold = {1: 2.0, 5: 0.8, 9: 4.0}
    t = _BASE
    for k in range(20):
        inner = f"x{k}"
        for gi, g in enumerate((1, 5, 9)):
            ts = t + gi * 1.5
            eng.feed(_ev(ts, "INSP_RECV", inner_id=inner, value=float(g)))
            eng.feed(_ev(ts + 0.001, "INSP_START", inner_id=inner, value=5.0))
        for gi, g in sorted(enumerate((1, 5, 9)), key=lambda x: x[0] * 1.5 + hold[x[1]]):
            eng.feed(_ev(t + gi * 1.5 + hold[g], "COMM_MSG", inner_id=inner, value=float(g),
                         name="V2M_INSPECT_END", status="OK"))
        t += 6
    assert eng.pending == {}
    by_g = {}
    for _ts, _i, d, g in eng.done:
        by_g.setdefault(g, []).append(round(d, 3))
    assert set(by_g) == {1, 5, 9}
    assert {round(sum(v) / len(v), 2) for v in by_g.values()} == {2.0, 0.8, 4.0}


def test_arrivals_without_recv_line_get_group_from_ack(tmp_path):
    """수신 줄(groupId)이 없는 옛 사이트: 4존이 같은 inner 로 거의 동시에 도착 → 회신이 그룹을 붙인다."""
    _c, _n, eng = _engine(tmp_path)
    t = _BASE
    for g in (1, 2, 3, 4):
        eng.feed(_ev(t + g * 0.01, "INSP_START", inner_id="y1", value=float(6 - g)))
    for g in (1, 2, 3, 4):
        eng.feed(_ev(t + 0.1 + g * 0.01, "COMM_MSG", inner_id="y1", value=float(g),
                     name="V2M_INSPECT_START_ACK", status="OK"))
    assert sorted(eng.pending) == ["y1@1", "y1@2", "y1@3", "y1@4"]
    assert {g: [a[1] for a in eng.cap.g[g].arr] for g in (1, 2, 3, 4)} == \
        {1: [5], 2: [4], 3: [3], 4: [2]}
    for g in (1, 2, 3, 4):
        eng.feed(_ev(t + 1 + g, "COMM_MSG", inner_id="y1", value=float(g),
                     name="V2M_INSPECT_END", status="OK"))
    assert eng.pending == {}


def test_legacy_single_group_site_unchanged(tmp_path):
    """그룹 정보가 전혀 없는 사이트(회신 토큰 0)는 예전처럼 inner 하나로 잡는다."""
    _c, _n, eng = _engine(tmp_path)
    eng.feed(_ev(_BASE, "INSP_START", inner_id="p1", value=2.0))
    assert "p1" in eng.pending
    eng.feed(_ev(_BASE + 0.1, "COMM_MSG", inner_id="p1", value=0.0,
                 name="V2M_INSPECT_START_ACK", status="OK"))
    eng.feed(_ev(_BASE + 9.0, "COMM_MSG", inner_id="p1", value=0.0,
                 name="V2M_INSPECT_END", status="OK"))
    assert eng.pending == {} and round(eng.durations[-1], 1) == 9.0
    eng.evaluate(_BASE + 120)                    # 그룹 0 으로 넘어간 도착
    assert [a[1] for a in eng.cap.g[0].arr] == [2]


def test_seq_file_index_mapped_to_group(tmp_path):
    """seq_N 의 N 이 그룹 번호와 달라도(단일 그룹 사이트의 seq_1 ↔ 회신 토큰 3) 짝을 배워 점유를 붙인다."""
    _c, _n, eng = _engine(tmp_path)
    evs = _site(groups=(3,), hours=0.2, recv=False, seq_index=lambda g: 1)
    _run(eng, evs)
    assert eng.seq_group(1) == 3
    assert len(eng.cap.g[3].holds) > 50 and eng.cap.g[3].holds[-1][2] == "seq"
    assert eng.cap.g[3].phases                   # 처리·송신·후처리 단계도 쌓인다


# ── 처리 여유 경보 ───────────────────────────────────────────
def test_gradual_slowdown_raises_capacity_and_drift_warnings(tmp_path):
    """투입은 그대로, 점유만 시간당 3%씩 늘어나는 설비 → 처음 몇 시간은 조용하다가 주의 경보."""
    _c, n, eng = _engine(tmp_path)
    evs = _site(groups=(1, 2), pool=6, hours=12, interval=lambda h: 6.0,
                hold=lambda h: 14.0 * (1 + 0.03 * h))
    _run(eng, evs)
    first = {(a.rule, a.severity): a.ts for a in reversed(n.sent)}
    assert all(t - _BASE > 6 * 3600 for t in first.values()), first
    drift = _first(n, "tact_drift", "warn")
    cap = _first(n, "capacity_drift", "warn")
    assert drift and cap
    assert 8 * 3600 < drift.ts - _BASE < 11 * 3600       # 1.25배 ≈ 8.3시간
    assert "투입은 그대로" in drift.evidence
    assert "유휴 스레드 중앙값 3개 (기준 4개)" in cap.evidence
    assert not any(a.severity == "crit" for a in n.sent)
    b = eng.cap.g[1].base
    assert b["idle_median"] == 4 and abs(b["hold_p50"] - 14.3) < 0.5 and b["pool"] == 6


def test_input_rate_change_is_not_called_drift(tmp_path):
    """점유가 늘어도 투입 간격이 같이 30% 짧아졌으면(부하 변화) 처리 시간 증가 경보를 내지 않는다."""
    _c, n, eng = _engine(tmp_path)
    evs = _site(groups=(1,), pool=8, hours=6, interval=lambda h: 6.0 if h < 3 else 4.2,
                hold=lambda h: 14.0 if h < 3 else 19.0)
    _run(eng, evs)
    assert _first(n, "tact_drift") is None


def test_near_miss_is_immediate(tmp_path):
    """평소 유휴가 넉넉한 그룹에서 유휴 1개 도착 → 판정 주기를 기다리지 않고 즉시 심각."""
    _c, n, eng = _engine(tmp_path)
    evs = _site(groups=(9,), pool=6, hours=1.5, interval=lambda h: 1.5, hold=lambda h: 3.0)
    _run(eng, evs)
    assert eng.cap.g[9].base and eng.cap.g[9].base["idle_median"] >= 3
    t = evs[-1].ts + 0.5
    eng.feed(_ev(t, "INSP_RECV", inner_id="z1", value=9.0))
    eng.feed(_ev(t + 0.001, "INSP_START", inner_id="z1", value=1.0))
    a = n.sent[-1]
    assert a.rule == "capacity_drift" and a.severity == "crit" and a.title.startswith("거부 직전")
    assert a.meta == {"group": 9}


def test_restart_resets_baseline_and_state_persists(tmp_path):
    _c, _n, eng = _engine(tmp_path)
    sf = watch._attach_capacity_state(eng, str(tmp_path))
    evs = _site(groups=(1,), hours=2.5, interval=lambda h: 6.0, hold=lambda h: 14.0)
    _run(eng, evs)
    assert eng.cap.g[1].base
    sf.save(eng.cap)
    # 다른 엔진(= talog 재시작)이 기준선과 시간별 추세를 이어받는다
    _c2, _n2, eng2 = _engine(tmp_path)
    watch._attach_capacity_state(eng2, str(tmp_path))
    assert eng2.cap.g[1].base == eng.cap.g[1].base and eng2.cap.g[1].hourly
    kpi = [f for f in os.listdir(tmp_path) if f.startswith("kpi_")]
    assert kpi
    with open(os.path.join(tmp_path, kpi[0]), encoding="utf-8") as f:
        row = json.loads(f.readline())
    assert {"hour", "group", "idle_median", "hold_p50", "hold_p99", "interval_s"} <= set(row)
    # 재시작(풀 생성) → 기준선을 새로 잡는다
    eng2.feed(_ev(evs[-1].ts + 60, "POOL_CREATE", name="eFunction"))
    assert eng2.cap.g[1].base is None
    # 메모리 급감도 재시작으로 본다 (풀 생성 로그가 없는 사이트)
    eng3 = CapacityModel()
    eng3.g.setdefault(1, None)
    eng3 = CapacityModel()
    eng3.arrival(_BASE, 1, 4, 2)
    eng3._grp(1).base = {"from": _BASE, "idle_median": 4}
    for i in range(10):
        eng3.usage_sample(_BASE + i * 5, 45000.0, 44.0, 88.0, 2093)
    eng3.usage_sample(_BASE + 60, 3000.0, 5.0, 20.0, 400)
    assert eng3._grp(1).base is None and "재시작" in eng3.gen_reason


def test_old_baseline_not_used_for_earlier_data(tmp_path):
    """뒤 날짜에서 잡은 기준선을 앞 날짜 재현에 쓰지 않는다."""
    m = CapacityModel()
    m.load({"version": 1, "gen_start": _BASE + 86400,
            "groups": {"1": {"from": _BASE + 86400, "idle_median": 4, "hold_p50": 14}}})
    m.arrival(_BASE, 1, 4, 2)
    assert m._grp(1).base is None


# ── 사건 분석 (룰 진단·해석 문장·합의 관문) ─────────────────────
def _ev_dict(**over):
    ev = {"site": "X", "trigger": {"time": "05:02:49", "rule": "no_insp_thread", "severity": "crit",
                                   "title": "거부", "evidence": ""},
          "n_alerts": 1, "alerts": [],
          "inspections": {"inflight": 6, "recent_interval_s": 1.5, "duration_over_interval": 5.9,
                          "pool": 6, "slowdown_ratio": 1.3, "baseline_duration_s": 4.0,
                          "recent_durations_s": [8.7]},
          "rejects": {"count_30min": 1},
          "timeouts": {"img_timeout_30min": 0, "alg_timeout_30min": 0, "last": None},
          "restarts": {"since_restart_s": None, "count_60min": 0, "pool_log_lines_60min": 0},
          "model_load": {"pending": False},
          "errors": {"seh_near": 0},
          "gpu_log": {"fatal_near": 0, "infer_errors_near": 0, "exec_slowdown_ratio": 1.1},
          "capacity": {"group": 11, "pool": 6, "interval_s": 1.508, "limit_s": 9.05,
                       "idle_median": 3, "idle_baseline": 4, "idle_min": 0, "hold_p50": 5.38,
                       "hold_p99": 6.15, "tolerance_s": 2.9, "tolerance_ratio": 0.32},
          "trend": {"since": "10/11 00:10", "hours": 29.0, "hold_p50_baseline": 4.03,
                    "hold_p50_now": 5.38, "hold_ratio": 1.33, "interval_baseline_s": 1.508,
                    "interval_now_s": 1.508, "interval_change_pct": 0.0, "spearman_hourly": 0.99,
                    "monotonic": True, "groups_rising": [1, 2, 9, 10, 11, 12],
                    "groups_with_baseline": 12},
          "phases": {"group": 11, "names": ["처리(시작~판정)", "결과 송신", "후처리(저장·DB)"],
                     "baseline_s": [3.4, 0.2, 0.25], "now_s": [4.5, 0.15, 0.6],
                     "share_of_increase_pct": [76.0, 0.0, 24.0]},
          "transient": {"groups": [9, 10, 11], "n_groups": 3,
                        "detail": {9: {"max_last_60s": 9.6, "p99_prev_60min": 6.03, "ratio": 1.59}}},
          "resources": {"total_cpu_now_pct": 88.5, "mem_slope_mb_per_h": 17.4}}
    for k, v in over.items():
        ev[k] = {**ev[k], **v} if isinstance(v, dict) and isinstance(ev.get(k), dict) else v
    return ev


def test_rule_diagnoser_progressive_vs_load_vs_gpu():
    d = RuleDiagnoser(Runbook(), watch.load_config(""))
    r = d.diagnose("reject", _ev_dict(), [])
    assert r["cause"] == "progressive_slowdown"
    assert r["actions"] == ["planned_restart", "collect_memory_snapshot", "report_dev_team"]
    assert any("1.33배" in f for f in r["facts"])
    # 투입이 30% 빨라졌다 → 부하 변화 (처리량 포화)
    assert d.diagnose("reject", _ev_dict(trend={"interval_change_pct": -30.0}),
                      [])["cause"] == "throughput_saturation"
    # GPU 커널이 느려졌다 → 경합 (처리량 포화)
    assert d.diagnose("reject", _ev_dict(gpu_log={"exec_slowdown_ratio": 2.0}),
                      [])["cause"] == "throughput_saturation"
    # 오르내림 → 시간 경과형 아님
    assert d.diagnose("reject", _ev_dict(trend={"monotonic": False}),
                      [])["cause"] == "throughput_saturation"
    # 기준선이 없으면(추세 근거 없음) 예전 판정
    ev = _ev_dict()
    ev.pop("trend")
    sat = d.diagnose("reject", ev, [])
    assert sat["cause"] == "throughput_saturation"
    assert any("스레드 6개로 감당할 수 있는 한계의 98%" in f for f in sat["facts"])


def test_interpret_capacity_sentences():
    rb = Runbook()
    facts = interpret(_ev_dict(), rb.params, "reject")
    text = "\n".join(facts)
    assert "그룹 11의 도착 순간 유휴 스레드는 최근 30분 중앙값 3개로, 기준선 4개보다 줄었다" in text
    assert "스레드 6개 × 투입 간격 1.508초 = 9.05초" in text and "2.9초까지만 견딘다" in text
    assert "투입(부하)이 늘어난 것이 아니다" in text
    assert "12개 그룹 중 6개 그룹" in text and "프로세스 공통 원인" in text
    assert "(각 단계는 여러 처리의 합이다)" in text and "알고리즘 단계" not in text
    assert "스레드 6개가 감당하는 한계의 98%다 — 한계에 닿았다" in text
    assert "투입이 처리보다" not in text                # 스레드 1개짜리 해석을 쓰지 않는다
    assert "3개 그룹의 점유가 평소 최대치를 넘었다" in text
    for cid in list(rb.causes("reject")) + ["시간 경과형", "처리량 포화"]:
        assert cid not in text                       # 원인 이름은 말하지 않는다


def test_gate_uses_runbook_actions_with_llm_message():
    rb = Runbook()
    rule = {"cause": "progressive_slowdown", "name": "시간 경과형 처리 열화", "facts": ["x"],
            "confirmed": True, "actions": ["planned_restart", "collect_memory_snapshot"],
            "notify": ["vision_engineer"]}
    llm = {"ok": True, "root_cause": "progressive_slowdown", "notify": "vision_engineer",
           "actions": ["check_gpu_load", "adjust_cadence"],
           "message_ko": "점유 시간이 기준의 1.33배로 꾸준히 늘었습니다.",
           "fact": {"ok": True, "hanzi": 0}}
    v = gate("reject", rule, llm, [Alert(_BASE, "no_insp_thread", "crit", "t", "e")], rb)
    assert v["mode"] == "agree" and v["message_source"] == "llm"
    assert v["actions"] == ["planned_restart", "collect_memory_snapshot"]
    assert "권고: 투입이 멈춘 휴지 시점" in v["message"] and "GPU" not in v["message"]


def test_usage_line_total_cpu_parsed():
    from talog.events import Extractor
    from talog.lineparser import LogRecord
    ex = Extractor()
    rec = LogRecord(ts=0, ts_text="", level="Debug", header="USAGE", obj_id="0", line_no=0,
                    msg="CPU Usage : 44.8%\tMemory Usage : 45006.6Mb\tthread count : 2093 | "
                        "Total CPU Usage : 85.9%\tTotal Memory Usage : 29.1%\tTotal thread count : 6960")
    e = ex._match(rec, ex.rules["processusage"])
    assert (e.value, e.status, e.name, e.extra) == (45006.6, "44.8", "2093", "85.9")


# ── 사후 리포트 (analyze) ────────────────────────────────────
def test_capacity_review_feeds_report_findings():
    """리포트도 감시와 같은 규칙으로 처리 여유 소견을 내고, 조립과 별개로 거부를 센다."""
    from talog.diagnose import diagnose
    from talog.watch import capacity_review
    evs = _site(groups=(1, 2), pool=6, hours=12, interval=lambda h: 6.0,
                hold=lambda h: 14.0 * (1 + 0.03 * h))
    # 마지막에 거부 1건 (그룹 2) — 조립 결과(inspections)는 비워 둔다
    t = evs[-1].ts + 1
    evs += [_ev(t, "INSP_RECV", inner_id="r1", value=2.0),
            _ev(t + 0.001, "INSP_START", inner_id="r1", value=0.0),
            _ev(t + 0.002, "INSP_REJECT"),
            _ev(t + 0.01, "COMM_MSG", inner_id="r1", value=2.0, name="V2M_INSPECT_START_ACK",
                status="NoInspThread")]
    carry = {}
    cap = capacity_review(evs, carry)
    assert cap["alerts"] and carry["cap"]["groups"]
    fs = diagnose([], [], [], evs, [], evs[0].ts, evs[-1].ts, capacity=cap)
    titles = [f.title for f in fs]
    assert any(t.startswith("검사 시작 거부(NoInspThread) 1건 — 그룹 2") for t in titles), titles
    cf = next(f for f in fs if f.title.startswith("처리 여유 감소"))
    assert cf.severity == "warn" and "계획 재시작" in cf.advice
    assert any("유휴 스레드 중앙값 4→3" in x for x in cf.evidence)
    assert not any(f.title.startswith("이 날은 특기할 이상이 없습니다") for f in fs)
    # 다음 날을 이어 분석하면 기준선이 그대로 이어진다
    cap2 = capacity_review(_site(groups=(1, 2), hours=1, hold=lambda h: 18.0,
                                 start=_BASE + 86400), carry)
    assert cap2["groups"][1]["baseline"]["from"] == cap["groups"][1]["baseline"]["from"]


def test_single_group_site_without_group_tokens(tmp_path):
    """수신 줄도 그룹 토큰도 없는 옛 단일 그룹 사이트(CTR 계열): 그룹 0 으로 기준선·점유(회신 기준)를 잡는다."""
    _c, n, eng = _engine(tmp_path)
    evs = _site(groups=(0,), pool=2, hours=3, interval=lambda h: 10.0, hold=lambda h: 6.0,
                recv=False, seq=False)
    _run(eng, evs)
    x = eng.cap.g[0]
    assert x.base and x.base["idle_median"] == 2 and abs(x.base["hold_p50"] - 5.1) < 0.2
    assert x.holds[-1][2] == "cycle" and eng.cap.pool(0) == 2
    assert not n.sent                                   # 평소 그대로면 경보 없음


def test_multi_group_site_does_not_invent_group_zero(tmp_path):
    """여러 그룹 사이트에서 그룹을 끝내 모르는 도착은 가짜 그룹 0 으로 만들지 않는다."""
    _c, _n, eng = _engine(tmp_path)
    _run(eng, _site(groups=(1, 2), hours=0.5, recv=True))
    eng.feed(_ev(_BASE + 2000, "INSP_START", inner_id="lost1", value=3.0))
    eng.evaluate(_BASE + 2100)
    assert 0 not in eng.cap.g


def test_no_alert_after_line_stops(tmp_path):
    """라인이 멈춘 뒤에는 남은 창의 옛 값으로 경보하지 않는다."""
    _c, n, eng = _engine(tmp_path)
    evs = _site(groups=(1,), pool=6, hours=12, interval=lambda h: 6.0,
                hold=lambda h: 14.0 * (1 + 0.03 * h))
    _run(eng, evs)
    k = len(n.sent)
    for m in range(1, 40):                      # 40분 동안 ProcessUsage 만 들어온다
        eng.feed(_ev(evs[-1].ts + m * 60, "USAGE", value=1000.0))
        eng.evaluate(evs[-1].ts + m * 60)
    assert len(n.sent) == k
    assert eng.cap.summary and eng.cap.summary[0]["status"] == "idle"


def test_pool_not_inflated_when_completion_signal_stops(tmp_path):
    """comm 로그가 먼저 끊기면(회전·복사 지연·미추적) 회신 없이 도착만 쌓인다 — 풀 추정이 부풀지 않는다.
    Tenneco 0730: comm 13:55 끊김, InspStarter·seq 는 14:05 까지 → 투표를 막기 전에는 풀 3 → 356."""
    _c, _n, eng = _engine(tmp_path)
    evs = _site(groups=(1, 2, 3, 4), pool=3, hours=2, interval=lambda h: 0.92, hold=lambda h: 1.4,
                recv=False, stagger=0.3)
    cut = evs[-1].ts - 600
    evs = [e for e in evs if not (e.kind == "COMM_MSG" and e.ts > cut)]
    _run(eng, evs)
    assert len(eng.pending) > 1000                      # 회신이 끊긴 뒤 진행 중이 쌓였다
    assert {g: eng.cap.pool(g) for g in (1, 2, 3, 4)} == {1: 3, 2: 3, 3: 3, 4: 3}


def test_report_summary_survives_restart_after_last_inspection():
    """생산이 끝난 뒤 재시작해도(MR 0801: 마지막 검사 07:06, 재시작 07:08) 사후 요약은 그 세대 값을 쓴다."""
    from talog.watch import capacity_review
    evs = _site(groups=(1,), pool=2, hours=3, interval=lambda h: 3.2, hold=lambda h: 2.0)
    t = evs[-1].ts + 150
    evs += [_ev(t, "POOL_CREATE")] + [_ev(t + 5 * k, "USAGE", value=1000.0) for k in range(1, 50)]
    s = capacity_review(evs, {})["groups"][1]
    assert s["n_arrivals"] > 0 and s["pool"] == 2
    assert s["baseline"]["idle_median"] == 2 and s["hold_ratio"] == 1.0


def test_llm_message_cleaned_of_actions_and_contradicted_claims():
    """7B 담당자 문구: 원인 id → 한국어 이름, 조치·계획 문장과 근거에 어긋난 용량 주장은 뺀다."""
    from talog.agent import clean_message
    rb = Runbook()
    ev = {"capacity": {"tolerance_s": 3.01, "limit_s": 9.05}, "trend": {"interval_change_pct": 0.0}}
    msg, drop = clean_message(
        "처리 한계를 초과한 점유 시간이 12.9시간 동안 늘었습니다. 그룹 9의 점유 시간이 기준선의 "
        "1.33배로 progressive_slowdown 쪽입니다. 투입이 늘어난 것은 아닙니다. 처리량이 꾸준히 "
        "증가했습니다. 즉시 재시작하지 않고 감시를 지속합니다. GPU 로그 확인 필요. 개발팀에 보고해야 한다. "
        "알고리즘 개발팀에 보고합니다. 여러 그룹이 함께 늘었습니다(root_cause: progressive_slowdown).",
        ev, rb, "reject")
    assert msg == ("그룹 9의 점유 시간이 기준선의 1.33배로 시간 경과형 처리 열화 쪽입니다. "
                   "투입이 늘어난 것은 아닙니다. 여러 그룹이 함께 늘었습니다.")
    assert len(drop) == 6
    # 내성이 바닥났으면 '한계 초과' 는 사실이라 남긴다
    assert clean_message("처리 한계를 초과했습니다.", {"capacity": {"tolerance_s": -0.5}}, rb,
                         "reject")[0] == "처리 한계를 초과했습니다."
    # 다 빠지면 gate 가 룰 문구로 대신한다
    rule = {"cause": "progressive_slowdown", "name": "시간 경과형 처리 열화", "facts": ["x"],
            "confirmed": True, "actions": ["planned_restart"], "notify": ["vision_engineer"]}
    llm = {"ok": True, "root_cause": "progressive_slowdown", "notify": "vision_engineer",
           "actions": ["monitor"], "message_ko": "", "fact": {"ok": True, "hanzi": 0}}
    v = gate("reject", rule, llm, [Alert(_BASE, "capacity_drift", "warn", "t", "e")], rb)
    assert v["mode"] == "agree" and v["message_source"] == "rule"


def test_seq_before_ack_without_recv_keeps_group_mapping(tmp_path):
    """수신 줄이 없고 seq 시작이 회신보다 먼저 찍히는 사이트: 같은 inner 가 존을 0.3초 간격으로 지나도
    seq↔그룹이 한 칸씩 밀리지 않는다 (밀리면 앞 존 도착이 두 배로 잡히고 마지막 존은 비었다)."""
    _c, n, eng = _engine(tmp_path)
    _run(eng, _site(groups=(1, 2, 3, 4), pool=3, hours=1.5, interval=lambda h: 0.92,
                    hold=lambda h: 1.4, recv=False, stagger=0.3))
    assert [eng.seq_group(k) for k in (1, 2, 3, 4)] == [1, 2, 3, 4]
    n_arr = [len(eng.cap.g[g].arr) for g in (1, 2, 3, 4)]
    assert max(n_arr) - min(n_arr) <= 2
    assert not [a for a in n.sent if a.rule == "capacity_drift"]


def test_completion_signal_lost_is_not_saturation():
    """완료 신호(회신 END)만 끊기면 진행 중·소요 배율이 부푼다 — 스레드가 놀고 있으면 처리량 포화가 아니다.
    Tenneco 0730 13:59: comm.log 가 10분 먼저 끊겨 진행 중 197건, 최근 도착 유휴 3/3, seq 점유 0.61초."""
    d = RuleDiagnoser(Runbook(), watch.load_config(""))
    lost = dict(inspections={"inflight": 197, "oldest_inflight_s": 181.8, "recent_interval_s": 0.922,
                             "duration_over_interval": 197.19, "pool": 3, "slowdown_ratio": 1.02,
                             "baseline_duration_s": 0.6, "wait_threads_recent": [3, 3, 3, 2],
                             "starts_10min": 2196, "ends_10min": 1408, "last_end_age_s": 238.9,
                             "starts_since_last_end": 1031},
                capacity={"group": 4, "pool": 3, "idle_median": 3, "idle_baseline": None,
                          "hold_source": "seq", "hold_p50": 0.61, "hold_p99": 0.65,
                          "tolerance_s": 2.12, "tolerance_ratio": 0.77})
    ev = _ev_dict(**lost)
    ev.pop("trend")
    r = d.diagnose("reject", ev, [])
    assert r["cause"] == "completion_signal_lost"
    assert r["actions"] == ["check_result_delivery", "check_comm_log"]
    text = "\n".join(interpret(ev, Runbook().params, "reject"))
    assert "완료 신호만 안 보인다" in text and "한계에 닿았다" not in text
    # 같은 수치라도 최근 도착 순간 유휴가 0 쪽이면(처리가 막힘) 끊김이 아니다
    ev2 = _ev_dict(**lost)
    ev2.pop("trend")
    ev2["inspections"]["wait_threads_recent"] = [1, 0, 0, 0]
    assert d.diagnose("reject", ev2, [])["cause"] == "throughput_saturation"
    # 완료가 시작만큼 들어오면 끊김이 아니다
    assert d.diagnose("reject", _ev_dict(inspections={"wait_threads_recent": [6, 6, 5, 6],
                                                      "starts_10min": 400, "ends_10min": 398,
                                                      "last_end_age_s": 0.4, "starts_since_last_end": 1}),
                      [])["cause"] == "progressive_slowdown"
    assert "마지막 완료 신호(INSPECT_END)는 경보 239초 전이고 그 뒤로 검사 시작만 1031건" in text


def test_live_poll_feeds_files_in_time_order(tmp_path):
    """실시간 감시는 한 주기(기본 20초)에 여러 파일의 새 줄을 읽는다. 파일 순서대로 넣으면 도착
    (InspStarter)을 다 넣은 뒤에야 회신(comm)이 들어가, 그사이 끝난 검사까지 진행 중으로 세어 풀
    투표가 부푼다(스레드 2개·투입 3초·소요 2초인데 투표 3·4). 시각순으로 넣으면 투표는 늘 2다."""
    root = tmp_path / "logs"
    now = dt.datetime.now()
    day = root / f"{now.year:04d}_{now.month:02d}" / f"{now.day:02d}"
    day.mkdir(parents=True)
    t0 = now - dt.timedelta(minutes=5)

    def tt(sec):
        return (t0 + dt.timedelta(seconds=sec)).strftime("%Y/%m/%d-%H:%M:%S.%f")[:-3]

    def write(ks, mode):
        ins, com = [], []
        for k in ks:
            inner, s = f"20000261013{k:08d}", 3.0 * k
            ins.append(f"{tt(s)}\t[Debug][CCommAiv::OnCommRecv][11]\tRecive M2V_INSPECT_START. "
                       f"innerId: {inner}, productId: ProductID, groupId: 1")
            ins.append(f"{tt(s + 0.001)}\t[Debug][CInspStarter::StartSeq][22]\teAIVProtocol_M2V_InspStart "
                       f"is arrived. Waiting thread count = 2, innerid: {inner}, productId: ProductID")
            com.append(f"{tt(s + 0.002)}\t[Debug][[S]LEN:0062][0]\tV2M_INSPECT_START_ACK,V3.0,TALOS1,OK,"
                       f"{inner},ProductID,1")
            com.append(f"{tt(s + 2.0)}\t[Debug][[S]LEN:0062][0]\tV2M_INSPECT_END,V3.0,TALOS1,OK,"
                       f"{inner},ProductID,1")
        for name, lines in (("InspStarter.log", ins), ("comm.log", com)):
            with open(day / name, mode, encoding="utf-8", newline="") as f:
                f.write("".join(x + "\r\n" for x in lines))

    cfg = watch.load_config("")
    cfg.update(watch_root=str(root), alert_dir=str(tmp_path / "alerts"), low_priority=False)
    cfg["notify"].update(toast=False, webhook="")
    lw = watch.LiveWatch(cfg)
    write(range(1, 4), "w")                     # 1주기: 완료 신호가 들어오기 시작
    lw.step()
    write(range(4, 10), "a")                    # 2주기: 도착 6건 (각 2초 뒤 완료)
    lw.step()
    assert not lw.engine.pending
    assert set(lw.engine.cap.g[1].votes) == {2}


def test_isolated_stall_is_not_saturation():
    """정체 경보인데 남은 검사가 한 건이고 스레드는 남아 있다 — 남은 건의 경과로 소요 배율이 494배가
    돼도 처리량 포화가 아니다 (Tenneco 0728 15:53: 그룹 3 검사 1건 491초, 최근 도착 유휴 2·2·0·2/3)."""
    d = RuleDiagnoser(Runbook(), watch.load_config(""))
    stall = [Alert(_BASE, "insp_stall", "warn", "검사 정체 491초", "e")]
    ev = _ev_dict(inspections={"inflight": 1, "oldest_inflight_s": 490.6, "recent_durations_s": [1.9, 1.4, 1.5],
                               "baseline_duration_s": 0.6, "slowdown_ratio": 2.61, "recent_interval_s": 0.992,
                               "duration_over_interval": 494.57, "pool": 3, "wait_threads_recent": [2, 2, 0, 2],
                               "starts_10min": 224, "ends_10min": 220, "last_end_age_s": 489.6,
                               "starts_since_last_end": 0, "inflight_ids": ["2000026072815453938"]},
                  capacity={"group": 3, "pool": 3, "idle_median": 3.0, "idle_baseline": None})
    ev.pop("trend")
    r = d.diagnose("reject", ev, stall)
    assert r["cause"] == "isolated_stall"
    assert r["actions"] == ["check_stuck_inspection", "check_result_delivery"]
    assert any("남은 inner id 2000026072815453938" in f for f in r["facts"])
    # 같은 근거라도 거부(NoInspThread)가 함께면 스레드가 모자랐던 것 — 단건 정체가 아니다
    both = stall + [Alert(_BASE, "no_insp_thread", "crit", "거부", "e")]
    assert d.diagnose("reject", ev, both)["cause"] == "throughput_saturation"
    # 남은 검사가 많으면(스레드 수 넘음) 단건 정체가 아니다
    ev["inspections"]["inflight"] = 3
    assert d.diagnose("reject", ev, stall)["cause"] == "throughput_saturation"


def test_report_drops_channel_ok_when_capacity_finding_exists():
    """채널 평균만 본 '처리량 여유'(ok)는 그룹별 처리 여유 소견과 어긋난다 — 기준선이 있으면 내지 않는다
    (풍산 10/12 리포트: '처리 여유 감소' 심각 아래에 '처리량 여유' ok 가 같이 나왔다)."""
    from talog.diagnose import diagnose
    from talog.watch import capacity_review
    from talog.assemble import ChannelRun, Inspection
    evs = _site(groups=(1, 2), pool=6, hours=12, interval=lambda h: 6.0,
                hold=lambda h: 14.0 * (1 + 0.03 * h))
    cap = capacity_review(evs, {})
    prod = [Inspection(inner_id=f"i{k}", start_ts=_BASE + 6.0 * k, status="complete") for k in range(50)]
    runs = [ChannelRun(inner_id=f"i{k}", alg_idx=1, channel="c1", status="done", infer_ms=270.0)
            for k in range(50)]
    titles = [f.title for f in diagnose(prod, runs, [], evs, [], evs[0].ts, evs[-1].ts, capacity=cap)]
    assert any(t.startswith("처리 여유 감소") for t in titles) and "처리량 여유" not in titles
    # 처리 여유 근거가 없는 옛 사이트는 그대로 낸다
    titles2 = [f.title for f in diagnose(prod, runs, [], [], [], _BASE, _BASE + 300, capacity=None)]
    assert "처리량 여유" in titles2
