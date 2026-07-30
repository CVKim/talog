# -*- coding: utf-8 -*-
"""talog.watch 의 RuleEngine / Notifier 단위 테스트.

실제 로그 파일 없이 Event 객체를 직접 feed 하여 룰 판정과 경보 발송을
검증한다. Notifier 는 replay=True 로 생성하여 토스트/웹훅을 발송하지
않으며, alert_dir 은 tmp_path 로 교체한다.
"""

from __future__ import annotations

import datetime as dt
import json
import os

from talog import watch
from talog.events import Event
from talog.watch import Alert, Notifier, RuleEngine

# 기준 시각 (2026/07/29 09:00:00)
_BASE = dt.datetime(2026, 7, 29, 9, 0, 0).timestamp()


def _make(tmp_path):
    """기본 설정으로 (cfg, notifier, engine) 을 생성한다."""
    cfg = watch.load_config("")           # 기본값 사용 (파일 미존재 경로)
    cfg["alert_dir"] = str(tmp_path)      # 외부 드라이브 의존 제거
    notifier = Notifier(cfg, replay=True)
    engine = RuleEngine(cfg, notifier)
    return cfg, notifier, engine


def _ev(ts: float, kind: str, **kw) -> Event:
    """테스트용 Event 를 간단히 생성한다."""
    tt = dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]
    return Event(ts=ts, ts_text=tt, kind=kind, **kw)


# ---------------------------------------------------------------------------
# 1) error_repeat: 10분 창 내 동일 key ERROR 5건 → 경보 1건, 쿨다운 중복 억제
def test_error_repeat_alert_and_cooldown(tmp_path):
    _cfg, notifier, engine = _make(tmp_path)
    # 동일 key(모델명) 의 ERROR 를 1분 간격으로 5건 feed 한다 (창 10분 이내).
    for i in range(5):
        engine.feed(_ev(_BASE + i * 60, "ERROR", model="DLMODEL0005"))
    engine.evaluate(_BASE + 300)
    assert len(notifier.sent) == 1
    a = notifier.sent[0]
    assert a.rule == "error_repeat"
    assert a.severity == "warn"
    assert "DLMODEL0005" in a.evidence
    # 쿨다운(기본 30분) 내 재평가 시 중복 발송이 없어야 한다.
    engine.evaluate(_BASE + 320)
    assert len(notifier.sent) == 1


# ---------------------------------------------------------------------------
# 2) no_insp_thread: INSP_REJECT feed 즉시 crit 경보
def test_no_insp_thread_immediate_crit(tmp_path):
    _cfg, notifier, engine = _make(tmp_path)
    # evaluate 호출 없이 feed 만으로 즉시 발보되어야 한다.
    engine.feed(_ev(_BASE, "INSP_REJECT"))
    assert len(notifier.sent) == 1
    a = notifier.sent[0]
    assert a.rule == "no_insp_thread"
    assert a.severity == "crit"


# ---------------------------------------------------------------------------
# 3) insp_stall: 완료 5건으로 중앙값 형성 후, pending 이 2배 초과 시 경보.
#    INSPECT_END(COMM_MSG) 수신 시 pending 해제 확인.
def test_insp_stall_alert_and_pending_release(tmp_path):
    _cfg, notifier, engine = _make(tmp_path)
    # 정상 완료 5건 (각 120초 소요) → durations 중앙값 120초 형성.
    for i in range(5):
        st = _BASE + i * 400
        inner = f"job{i}"
        engine.feed(_ev(st, "INSP_START", inner_id=inner))
        engine.feed(_ev(st + 120, "COMM_MSG", inner_id=inner,
                        name="INSPECT_END"))
    assert len(engine.durations) == 5
    # 새 검사 시작 후 완료 신호 없이 방치한다.
    stall_start = _BASE + 3000
    engine.feed(_ev(stall_start, "INSP_START", inner_id="stall1"))
    # limit = max(min_seconds=180, 중앙값 120 * 2.0 = 240) = 240초.
    # 240초 이내에는 경보가 없어야 한다.
    engine.evaluate(stall_start + 200)
    assert len(notifier.sent) == 0
    # 2배(240초) 초과 시점에는 경보가 발생해야 한다.
    engine.evaluate(stall_start + 250)
    assert len(notifier.sent) == 1
    a = notifier.sent[0]
    assert a.rule == "insp_stall"
    assert a.severity == "warn"
    assert "stall1" in a.evidence
    # INSPECT_END 수신 시 pending 이 해제되고 duration 이 누적되어야 한다.
    engine.feed(_ev(stall_start + 260, "COMM_MSG", inner_id="stall1",
                    name="INSPECT_END"))
    assert "stall1" not in engine.pending
    assert len(engine.durations) == 6
    # pending 해제 후 재평가 시 추가 경보가 없어야 한다.
    engine.evaluate(stall_start + 300)
    assert len(notifier.sent) == 1


# ---------------------------------------------------------------------------
# 4) restart_burst: 60분 내 POOL_CREATE 3건 → 경보, pending 초기화 확인
def test_restart_burst_alert_and_pending_clear(tmp_path):
    _cfg, notifier, engine = _make(tmp_path)
    # 진행 중 검사 1건을 걸어 두고 재시작을 반복시킨다.
    engine.feed(_ev(_BASE, "INSP_START", inner_id="p1"))
    assert "p1" in engine.pending
    for i in range(3):
        engine.feed(_ev(_BASE + 10 + i * 60, "POOL_CREATE"))
    # 재시작(POOL_CREATE) 시 진행분(pending)이 초기화되어야 한다.
    assert engine.pending == {}
    engine.evaluate(_BASE + 200)
    assert len(notifier.sent) == 1
    a = notifier.sent[0]
    assert a.rule == "restart_burst"
    assert a.severity == "crit"


# ---------------------------------------------------------------------------
# 5-1) memory_trend: 2시간 이상 + 기울기 시퀀스 → crit 경보
def test_memory_trend_rising_crit_alert(tmp_path):
    _cfg, notifier, engine = _make(tmp_path)
    # 2.5시간 동안 2분 간격 76개 샘플, 시간당 약 +400MB 상승
    # (5분 간격으로 보면 약 +33MB 씩 꾸준히 증가 → 총 +1,000MB).
    for i in range(76):
        t = _BASE + i * 120
        mb = 1000.0 + (i * 120 / 3600.0) * 400.0
        engine.feed(_ev(t, "USAGE", value=mb))
    engine.evaluate(_BASE + 76 * 120)
    assert len(notifier.sent) == 1
    a = notifier.sent[0]
    assert a.rule == "memory_trend"
    assert a.severity == "crit"


# 5-2) memory_trend: 안정(평탄) 시퀀스는 미발보
def test_memory_trend_stable_no_alert(tmp_path):
    _cfg, notifier, engine = _make(tmp_path)
    # 동일한 샘플 수/기간이지만 사용량이 평탄하면 경보가 없어야 한다.
    for i in range(76):
        engine.feed(_ev(_BASE + i * 120, "USAGE", value=1200.0))
    engine.evaluate(_BASE + 76 * 120)
    assert len(notifier.sent) == 0


# ---------------------------------------------------------------------------
# 6) 경보가 alerts_*.jsonl 에 기록되는지 (replay 는 _replay 접미사)
def test_alert_written_to_replay_jsonl(tmp_path):
    cfg, notifier, _engine = _make(tmp_path)
    ts = dt.datetime(2026, 7, 29, 9, 21, 14).timestamp()
    notifier.emit(Alert(ts, "no_insp_thread", "crit",
                        "검사 시작 거부(NoInspThread) — 미검사 임박",
                        "가용 Seq 스레드 0"))
    path = os.path.join(str(tmp_path), "alerts_20260729_replay.jsonl")
    assert os.path.exists(path)
    with open(path, "r", encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["rule"] == "no_insp_thread"
    assert rec["severity"] == "crit"
    assert rec["ts"].startswith("2026/07/29-09:21:14")
