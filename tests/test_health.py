"""v2.5 설비 건강(talog/health.py)·판정 기록(시간별 KPI 의 quality 줄)·하루 정리(/api/daily)."""
import datetime as dt
import json
import os

from talog import health as H
from talog import watch
from talog.events import Event
from talog.watch import Notifier, RuleEngine


def _hours(n, start="2026-10-12 00:00"):
    t0 = dt.datetime.strptime(start, "%Y-%m-%d %H:%M")
    return [(t0 + dt.timedelta(hours=i)).strftime("%Y-%m-%d %H:00") for i in range(n)]


# ── drift ────────────────────────────────────────────────────────────
def test_drift_warns_then_risks_and_names_when_it_started():
    hs = _hours(10)
    vals = [4.0] * 6 + [4.4, 4.6, 4.8, 5.0]
    warn = H.drift(3, list(zip(hs[:9], vals[:9])), 4.0)
    assert warn["state"] == "warn" and "10/12 06시부터" in warn["reason"] and warn["pct"] == 20
    risk = H.drift(3, list(zip(hs, vals)), 4.0)
    assert risk["state"] == "risk" and risk["score"] < 50 and risk["todo"]


def test_drift_ignores_small_steady_offset_and_waits_for_baseline():
    # 기준보다 8% 높은 상태가 하루 넘게 이어져도(누적합은 쌓인다) 증가 폭이 10% 미만이면 좋음
    steady = H.drift(1, [(h, 4.32) for h in _hours(30)], 4.0)
    assert steady["state"] == "ok" and steady["cusum"] >= H.H_RISK
    assert H.drift(1, [(h, 4.0) for h in _hours(3)], None)["state"] == "none"


# ── spike ────────────────────────────────────────────────────────────
def _spikes(n=20):
    rows = [{"hold_p50": 4.0, "hold_max": 5.0, "n": 100} for _ in range(n - 2)]
    return rows + [{"hold_p50": 4.0, "hold_max": 6.0, "n": 100}, {"hold_p50": 4.0, "hold_max": 7.0, "n": 100}]


def test_spike_compares_room_with_past_jumps():
    hist = _spikes()                                   # 튐: 1초 18번 · 2초 · 3초 → 가장 큼 3, 상위 5% 2
    assert H.spike(1, hist, 6.5, 10.0)["state"] == "ok"        # 여유 3.5
    assert H.spike(1, hist, 7.5, 10.0)["state"] == "warn"      # 여유 2.5 < 3
    assert H.spike(1, hist, 8.5, 10.0)["state"] == "risk"      # 여유 1.5 < 2
    over = H.spike(1, hist[:3], 10.2, 10.0)                    # 한계를 넘으면 기록이 적어도 바로 점검
    assert over["state"] == "risk" and over["rank"] > 1000
    assert H.spike(1, hist[:5], 6.0, 10.0)["state"] == "none"  # 12시간 전에는 판단하지 않는다


# ── quality ──────────────────────────────────────────────────────────
def _q(ng_base, ng_cur, d_base=None, d_cur=None):
    return ([{"n": 1000, "ng": ng_base, "defects": d_base or {}} for _ in range(10)]
            + [{"n": 1000, "ng": ng_cur, "defects": d_cur or {}} for _ in range(2)])


def test_quality_p_chart_and_defect_mix():
    assert H.quality(_q(20, 25))["state"] == "ok"               # 2.0% → 2.5% (1%p 미만)
    up = H.quality(_q(20, 60))                                  # 2% → 6%
    assert up["state"] == "risk" and "6.0%로 늘었습니다" in up["reason"]
    mix = H.quality(_q(20, 20, {"A": 10, "B": 10}, {"A": 2, "B": 18}))   # NG 비율은 같고 결함 종류만 바뀜
    assert mix["state"] == "risk" and "'B' 50% → 90%" in mix["reason"] and mix["psi"] >= 0.25   # 늘어난 결함을 가리킨다
    assert H.quality([{"n": 50, "ng": 1}] * 5)["state"] == "none"


# ── summarize ────────────────────────────────────────────────────────
def test_summarize_takes_lowest_and_breaks_ties_by_urgency():
    hs = _hours(12)
    drifts = [H.drift(g, list(zip(hs, [4.0] * 3 + [6.0] * 9)), 4.0) for g in (1, 2)]
    over = H.spike(3, [], 10.5, 10.0)
    assert drifts[0]["score"] == over["score"] == 20            # 같은 점수 — 한계를 넘은 쪽이 먼저
    s = H.summarize(drifts + [over, H.quality([])])
    assert s["score"] == 20 and s["state"] == "risk" and s["top"]["key"] == "spike:3"
    assert [t["key"] for t in s["todo"]] == ["spike:3", "drift:1"]   # 같은 할 일은 한 번만
    s2 = H.summarize(drifts)
    assert s2["more"] == 1                                       # '같은 흐름 1곳 더'
    assert H.summarize([H.quality([])])["state"] == "none"


# ── 판정 기록: 규칙 엔진의 quality 줄 ─────────────────────────────────────
def _end(ts, inner, result="OK", defects=(), zone=1):
    payload = ["V3.0", "TALOS1", result]
    if defects:
        payload += [str(len(defects)), *defects]
    payload += [inner, "ProductID", str(zone)]
    e = Event(ts=ts, ts_text="", kind="COMM_MSG", name="V2M_INSPECT_END", extra=",".join(payload))
    watch.Extractor._enrich_comm(e)
    return e


def test_rule_engine_writes_hourly_quality_rows(tmp_path):
    cfg = watch.load_config("")
    cfg["alert_dir"] = str(tmp_path)
    eng = RuleEngine(cfg, Notifier(cfg, replay=True))
    t0 = dt.datetime(2026, 10, 12, 9, 10).timestamp()
    eng.feed(_end(t0, "20003261012091000", "NG", ["SCRATCH"], zone=1))
    eng.feed(_end(t0 + 1, "20003261012091000", "NG", ["SCRATCH", "DENT"], zone=2))   # 다존: 검사 1건·결함명 한 번씩
    eng.feed(_end(t0 + 60, "20003261012091001", "OK"))
    assert eng.quality_rows(t0 + 600) == []                     # 아직 끝나지 않은 시간
    rows = eng.quality_rows(t0 + 3600)
    assert rows == [{"hour": "2026-10-12 09:00", "group": "quality", "n": 2, "ng": 1, "defects": {"SCRATCH": 1, "DENT": 1}}]
    eng.feed(_end(t0 + 120, "20003261012091002", "NG", ["X"]))  # 이미 내보낸 시간의 늦은 줄은 버린다
    assert eng.quality_rows(t0 + 7200) == []


# ── 콘솔: 기준(지금·지난 기록)과 하루 정리 ──────────────────────────────────
def _kpi(path, hours, p99, quality=None):
    with open(path, "w", encoding="utf-8") as f:
        for i, h in enumerate(hours):
            f.write(json.dumps({"hour": h, "group": 1, "n": 2400, "hold_p50": 3.0, "hold_p99": p99[i], "hold_max": p99[i] + 1,
                                "pool": 2, "interval_s": 4.5, "rejects": 0}) + "\n")
            if quality:
                f.write(json.dumps({"hour": h, "group": "quality", **quality[i]}) + "\n")


def test_health_uses_replay_only_when_no_recent_live_rows(tmp_path):
    from talog.console import Console
    con = Console(str(tmp_path / "talog.yaml"), mode="field")
    os.makedirs(con.data_dir, exist_ok=True)
    hs = _hours(14)
    _kpi(os.path.join(con.data_dir, "kpi_20261012_replay.jsonl"), hs, [4.0] * 4 + [4.0 + 0.4 * i for i in range(1, 11)])
    h = con.health()
    assert h["basis"] == "replay" and h["state"] == "risk" and h["top"]["kind"] == "drift"
    now = dt.datetime.now().replace(minute=0, second=0, microsecond=0)
    live = [(now - dt.timedelta(hours=k)).strftime("%Y-%m-%d %H:00") for k in range(6, 0, -1)]
    _kpi(os.path.join(con.data_dir, f"kpi_{now:%Y%m%d}.jsonl"), live, [4.0] * 6)
    con._health_cache = None
    h = con.health()
    assert h["basis"] == "live" and h["last_hour"] == live[-1]


def test_daily_page_is_one_offline_printable_sheet(tmp_path):
    from talog.console import Console, build_page
    from talog.console.http import GET_ROUTES
    con = Console(str(tmp_path / "talog.yaml"), mode="field")
    os.makedirs(con.data_dir, exist_ok=True)
    hs = _hours(14)
    _kpi(os.path.join(con.data_dir, "kpi_20261012_replay.jsonl"), hs, [4.0] * 14,
         [{"n": 1000, "ng": 20 if i < 12 else 60, "defects": {"A": 5}} for i in range(14)])
    with open(os.path.join(con.data_dir, "alerts_20261012_replay.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": "2026-10-12 13:05:00", "severity": "warn", "rule": "ng_rate", "title": "NG 비율"}) + "\n")
    with open(os.path.join(con.data_dir, "incidents_20261012_replay.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"id": "20261012-130500-ng_rate", "opened": "13:05:00", "alerts": [{"title": "NG 비율 <급증>"}],
                            "verdict": {"severity": "warn", "cause_name": "촬상 조건"}}, ensure_ascii=False) + "\n")
    code, page, ctype = GET_ROUTES["/api/daily"](con, {"day": "20261012", "replay": "1"})
    assert code == 200 and ctype.startswith("text/html") and page.startswith("<!doctype html>")
    for s in ("하루 정리", "2026-10-12", "지난 기록(경보 재현)", "판정 분포", "33,600", "2.6%", "0 · 1", "1건 · 마무리 0",
              "NG 비율 &lt;급증&gt;", "13시", "인쇄 · PDF"):
        assert s in page, s
    assert "http://" not in page and "https://" not in page and "<script" not in page
    assert "/api/daily?day=" in build_page() and "dailyLinks(H)" in build_page()
