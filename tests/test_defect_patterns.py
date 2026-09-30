# -*- coding: utf-8 -*-
"""watch 신규 룰(결함명 감시·NoInspThread 회신·룰 조정)과 추적 모드·사용자 패턴 테스트."""

from __future__ import annotations

import datetime as dt
import os

from talog import watch
from talog.events import Event
from talog.tracker import PatternEngine, compile_patterns, resolve_targets
from talog.watch import Alert, Notifier, RuleEngine, TailReader

_BASE = dt.datetime(2026, 7, 29, 9, 0, 0).timestamp()


def _make(tmp_path, **rules):
    cfg = watch.load_config("")
    cfg["alert_dir"] = str(tmp_path)
    for k, v in rules.items():
        if isinstance(v, dict) and isinstance(cfg["rules"].get(k), dict):
            cfg["rules"][k].update(v)
        else:
            cfg["rules"][k] = v
    n = Notifier(cfg, replay=True)
    return cfg, n, RuleEngine(cfg, n)


def _end(ts, inner, result="OK", defects=(), zone=1):
    payload = ["V3.0", "TALOS1", result]
    if defects:
        payload += [str(len(defects)), *defects]
    payload += [inner, "ProductID", str(zone)]
    e = Event(ts=ts, ts_text="", kind="COMM_MSG", name="V2M_INSPECT_END",
              extra=",".join(payload))
    watch.Extractor._enrich_comm(e)
    return e


# ---------------------------------------------------------------------------
def test_critical_defect_wildcard_and_cooldown(tmp_path):
    _c, n, eng = _make(tmp_path, defect_watch={"critical": ["BOOT_*"],
                                               "critical_cooldown_min": 10})
    eng.feed(_end(_BASE, "2000326072907195133", "NG", ["DOT_GREASE_MISS"]))
    assert n.sent == []                              # 치명 목록 밖
    eng.feed(_end(_BASE + 10, "2000326072907195134", "NG", ["BOOT_DAMAGED"]))
    assert [a.rule for a in n.sent] == ["defect_critical"]
    a = n.sent[0]
    assert a.severity == "crit" and "BOOT_DAMAGED" in a.title
    assert "2000326072907195134" in a.evidence
    eng.feed(_end(_BASE + 60, "2000326072907195135", "NG", ["BOOT_DAMAGED"]))
    assert len(n.sent) == 1                          # 10분 쿨다운
    eng.feed(_end(_BASE + 700, "2000326072907195136", "NG", ["BOOT_DAMAGED"]))
    assert len(n.sent) == 2 and "3건" in n.sent[1].evidence


def test_repeat_streak_and_multizone_sticky(tmp_path):
    _c, n, eng = _make(tmp_path, defect_watch={"repeat_count": 3, "repeat_window_min": 30,
                                               "ng_streak": 3})
    i1, i2, i3, i4 = (f"20003260729090000{k}" for k in "1234")
    # 다존 설비: 같은 inner 의 두 존 END 는 검사 1건 (존 하나라도 NG 면 NG)
    eng.feed(_end(_BASE, i1, "NG", ["DOT_GREASE_MISS"], zone=1))
    eng.feed(_end(_BASE + 1, i1, "OK", zone=2))
    assert eng.ng_streak_len() == 1 and eng.results[i1]["result"] == "NG"
    eng.feed(_end(_BASE + 60, i2, "NG", ["DOT_GREASE_MISS"]))
    assert n.sent == []
    eng.feed(_end(_BASE + 120, i3, "NG", ["DOT_GREASE_MISS"]))
    rules = sorted(a.rule for a in n.sent)
    assert rules == ["defect_repeat", "ng_streak"]
    st = next(a for a in n.sent if a.rule == "ng_streak")
    assert st.severity == "crit" and "연속 NG 3건" in st.title and "DOT_GREASE_MISS 3" in st.title
    eng.feed(_end(_BASE + 180, i4, "OK"))
    assert eng.ng_streak_len() == 0


def test_ng_rate(tmp_path):
    _c, n, eng = _make(tmp_path, defect_watch={"ng_rate_window": 10, "ng_rate_percent": 50})
    for i in range(10):
        eng.feed(_end(_BASE + i, f"200032607290900{i:02d}", "NG" if i % 2 == 0 else "OK",
                      ["X"] if i % 2 == 0 else ()))
    assert [a.rule for a in n.sent] == ["ng_rate"]
    assert "50%" in n.sent[0].title


def test_noinsp_ack_path_dedups_with_reject_line(tmp_path):
    _c, n, eng = _make(tmp_path)
    ack = Event(ts=_BASE, ts_text="", kind="COMM_MSG", name="V2M_INSPECT_START_ACK",
                extra="V3.0,TALOS3,NoInspThread,2000026072709262821,1641694923,1")
    watch.Extractor._enrich_comm(ack)
    eng.feed(ack)
    eng.feed(Event(ts=_BASE, ts_text="", kind="INSP_REJECT"))
    assert [a.rule for a in n.sent] == ["no_insp_thread"]
    assert "2000026072709262821" in n.sent[0].evidence


def test_overrides_enabled_severity_and_count(tmp_path):
    _c, n, eng = _make(tmp_path, overrides={
        "grab_fail": {"enabled": False},
        "alg_timeout": {"severity": "crit"},
        "img_timeout": {"count": 3, "window_min": 10}})
    eng.feed(Event(ts=_BASE, ts_text="", kind="GRAB_FAIL"))
    assert n.sent == []                              # 끈 룰
    eng.feed(Event(ts=_BASE, ts_text="", kind="ALG_TIMEOUT", roi_idx=2, value=5000))
    assert n.sent[-1].rule == "alg_timeout" and n.sent[-1].severity == "crit"
    for i in range(2):
        eng.feed(Event(ts=_BASE + 60 * i, ts_text="", kind="IMG_TIMEOUT", inner_id="x"))
    assert all(a.rule != "img_timeout" for a in n.sent)   # 3회 미만
    eng.feed(Event(ts=_BASE + 130, ts_text="", kind="IMG_TIMEOUT", inner_id="x"))
    a = n.sent[-1]
    assert a.rule == "img_timeout" and "최근 10분 3회" in a.evidence


# ---------------------------------------------------------------------------
def test_restart_burst_counts_pool_bundle_as_one_restart(tmp_path):
    _c, n, eng = _make(tmp_path)
    pools = ("eFunction", "eDraw", "eLongRunning", "eSaver", "eFovProc")
    for p in pools:                                  # 기동 1회 = 풀 5종 로그 5줄
        eng.feed(Event(ts=_BASE, ts_text="", kind="POOL_CREATE", name=p))
    eng.evaluate(_BASE + 10)
    assert len(eng.restarts) == 1 and n.sent == []   # 예전: 5회로 세어 즉시 경보
    for k in (1, 2):                                  # 5분 간격 재기동 2회 더 = 3회
        for p in pools:
            eng.feed(Event(ts=_BASE + 300 * k, ts_text="", kind="POOL_CREATE", name=p))
    eng.evaluate(_BASE + 610)
    assert [a.rule for a in n.sent] == ["restart_burst"] and "재시작 3회" in n.sent[0].title


def test_insp_stall_aggregates_into_one_alert(tmp_path):
    _c, n, eng = _make(tmp_path)
    for i in range(5):                                # 평소 소요 120초
        inner = f"20000260729090{i:05d}"
        eng.feed(Event(ts=_BASE + i * 400, ts_text="", kind="INSP_START", inner_id=inner))
        e = Event(ts=_BASE + i * 400 + 120, ts_text="", kind="COMM_MSG",
                  name="V2M_INSPECT_END", extra=f"V3.0,TALOS1,OK,{inner},P,1")
        watch.Extractor._enrich_comm(e)
        eng.feed(e)
    t = _BASE + 3000
    for i in range(50):                               # 장애·로그 절단: 진행 중 50건 정체
        eng.feed(Event(ts=t + i, ts_text="", kind="INSP_START",
                       inner_id=f"20000260729100{i:05d}"))
    eng.evaluate(t + 400)
    assert [a.rule for a in n.sent] == ["insp_stall"]  # 예전: 50건
    a = n.sent[0]
    assert "외 49건" in a.title and "20000260729100" + "00000" in a.evidence
    eng.evaluate(t + 400 + 5 * 60)
    assert len(n.sent) == 1                            # 10분 쿨다운
    eng.evaluate(t + 400 + 11 * 60)
    assert len(n.sent) == 2


def test_reject_removes_unstarted_arrival_from_pending(tmp_path):
    _c, n, eng = _make(tmp_path)
    eng.feed(Event(ts=_BASE, ts_text="", kind="INSP_START", inner_id="2000026072709245100",
                   value=1))
    eng.feed(Event(ts=_BASE + 90, ts_text="", kind="INSP_START",
                   inner_id="2000026072709262821", value=0))     # 대기 스레드 0 으로 도착
    eng.feed(Event(ts=_BASE + 90, ts_text="", kind="INSP_REJECT"))
    assert list(eng.pending) == ["2000026072709245100"]   # 거부된 검사는 진행 중 아님
    # 거부된 검사의 도착 라인이 없는 사이트: 직전 도착(대기 1)은 진짜 진행 중 → 유지
    eng.feed(Event(ts=_BASE + 100, ts_text="", kind="INSP_START",
                   inner_id="2000026072709270000", value=1))
    eng.feed(Event(ts=_BASE + 101, ts_text="", kind="INSP_REJECT"))
    assert "2000026072709270000" in eng.pending


def test_pattern_engine_count_window_scope_level(tmp_path):
    cfg = watch.load_config("")
    cfg["alert_dir"] = str(tmp_path)
    n = Notifier(cfg, replay=True)
    pe = PatternEngine([
        {"name": "비상정지", "match": "V2M_EMERGENCY_STOP", "files": ["comm.log"],
         "severity": "crit", "count": 1},
        {"name": "SEH", "match": r"re:Code: 0x[0-9a-f]+", "severity": "crit", "count": 2,
         "window_min": 5, "family": "reject"},
        {"name": "에러급증", "match": "re:.", "level": "Error", "severity": "warn",
         "count": 3, "window_min": 1},
        {"name": "잘못된", "match": "re:(", "severity": "warn"},
    ], n, Alert)
    assert len(pe.rules) == 3 and pe.errors and "정규식 오류" in pe.errors[0]
    pe.feed(_BASE, "seq_1.log", "Debug", "... V2M_EMERGENCY_STOP,V3.0 ...")
    assert n.sent == []                              # 파일 범위 밖
    pe.feed(_BASE, "Comm.log", "Debug", "... V2M_EMERGENCY_STOP,V3.0,TALOS3,Crash ...")
    assert n.sent[-1].rule == "pattern" and n.sent[-1].severity == "crit"
    pe.feed(_BASE + 1, "exception.log", "", "Code: 0xc0000005, Location: x")
    assert len(n.sent) == 1                          # 2회 필요
    pe.feed(_BASE + 100, "exception.log", "", "code: 0XC0000005")   # 대소문자 무시
    assert n.sent[-1].key == "SEH" and n.sent[-1].family == "reject"
    for i in range(3):
        pe.feed(_BASE + 200 + i, "DLInfer.log", "Debug" if i == 1 else "Error", "x")
    assert all(a.key != "에러급증" for a in n.sent)   # 레벨 조건: Error 2건뿐
    pe.feed(_BASE + 204, "DLInfer.log", "Error", "y")
    assert n.sent[-1].key == "에러급증" and "3회" in n.sent[-1].title
    assert [s[1] for s in pe.samples("SEH")] == ["exception.log", "exception.log"]


def test_compile_patterns_skips_disabled_and_empty():
    rules, errs = compile_patterns([{"name": "a", "match": "x", "enabled": False},
                                    {"name": "b", "match": ""}, "not-a-dict"])
    assert rules == [] and errs == ["b: match 가 비어 있음"]


def _touch(d, name, size=10):
    p = os.path.join(d, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"x" * size)
    return p


def test_resolve_targets_modes(tmp_path):
    d = str(tmp_path)
    for n in ("InspStarter.log", "comm.log", "seq_1.log", "seq_4.log", "DLInfer.log",
              "notes.bin", "InspCondRelationGraph_1.log"):
        _touch(d, n)
    _touch(d, os.path.join("alg", "alg_dl_0(x).log"))
    base = lambda ps: sorted(os.path.relpath(p, d) for p in ps)   # noqa: E731
    assert base(resolve_targets(d, {"mode": "default"})) == ["InspStarter.log",
                                                              "comm.log", "seq_1.log"]
    sel = base(resolve_targets(d, {"mode": "select", "files": ["SEQ_*.log", "alg\\alg_dl_*"]}))
    assert sel == sorted([os.path.join("alg", "alg_dl_0(x).log"), "seq_1.log", "seq_4.log"])
    auto = base(resolve_targets(d, {"mode": "auto",
                                    "exclude": ["InspCondRelationGraph*.log"]}))
    assert "notes.bin" not in auto and "DLInfer.log" in auto
    assert "InspCondRelationGraph_1.log" not in auto and not any("alg" in x for x in auto)
    auto2 = resolve_targets(d, {"mode": "auto", "include_alg": True})
    assert any(os.path.basename(p) == "alg_dl_0(x).log" for p in auto2)


def test_tailreader_line_hook_keep_kinds_and_initial_tail(tmp_path):
    p = os.path.join(str(tmp_path), "Comm.log")
    lines = [f"2026/07/29-09:00:{i:02d}.000\t[Debug][[S]LEN:0063][0]\t"
             f"V2M_INSPECT_END,V3.0,TALOS3,OK,20000260727090000{i:02d},1,1\n" for i in range(5)]
    lines.insert(2, "  call stack continuation line\n")
    with open(p, "w", encoding="utf-8") as f:
        f.writelines(lines)
    seen = []
    tr = TailReader(os.path.join(str(tmp_path), "state.json"))
    evs = tr.poll_file(p, line_hook=lambda ts, fn, lv, ln: seen.append((fn, lv, ln[:30])),
                       keep_kinds={"COMM_MSG"})
    assert len(evs) == 5 and all(e.kind == "COMM_MSG" and e.status == "OK" for e in evs)
    assert len(seen) == 6 and seen[2][1] == "" and "continuation" in seen[2][2]
    assert tr.poll_file(p) == []                     # 새 바이트 없음
    # 처음 보는 큰 파일은 끝부분부터 (과거분 재파싱 방지)
    big = os.path.join(str(tmp_path), "DLInfer.log")
    with open(big, "w", encoding="utf-8") as f:
        for i in range(12000):
            f.write(f"2026/07/29-09:00:00.000\t[Error][X][0]\tline {i:05d} " + "y" * 60 + "\n")
    tr2 = TailReader(os.path.join(str(tmp_path), "s2.json"), max_initial_mb=0.5)
    got = []
    tr2.poll_file(big, line_hook=lambda ts, fn, lv, ln: got.append(ln))
    assert 0 < len(got) < 12000 and got[-1].split("line ")[1].startswith("11999")
