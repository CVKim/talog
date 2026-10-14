# -*- coding: utf-8 -*-
"""talog MCP 서버(talog/mcp_server.py) 테스트 — Claude(구독)가 talog 산출물을 도구로 읽는 연동.

실제 산출물 모양을 본뜬 가상 데이터(사건·KPI·기준선·진단 리포트 DB)를 tmp 에 만들고, JSON-RPC 요청을 그대로
넣어 프로토콜·도구·마스킹·SQL 차단·재현 작업·stdio 통신·에이전트 폴더를 확인한다.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

import pytest

from talog import mcp_server as M

INNER = "2000026101205024639"
INNER2 = "2000026101205024640"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _incident(log_root: str) -> dict:
    return {
        "id": "20261012-050228-capacity_drift", "site": "시험 설비", "opened": "05:02:28", "closed": "05:03:28",
        "family": "reject", "t_trigger": dt.datetime(2026, 10, 12, 5, 2, 28).timestamp(),
        "alerts": [{"time": "05:02:28", "severity": "crit", "rule": "capacity_drift",
                    "title": "거부 직전 — 그룹 9 유휴 스레드 1개", "evidence": f"inner {INNER} 도착 순간 유휴 1개"},
                   {"time": "05:02:49", "severity": "crit", "rule": "no_insp_thread",
                    "title": "검사 시작 거부(NoInspThread) — 미검사 임박", "evidence": "가용 Seq 스레드 0 (그룹 10)"}],
        "rule": {"cause": "progressive_slowdown", "name": "시간 경과형 처리 열화",
                 "facts": ["그룹 9 점유 시간 4.043→5.37초 (1.33배)"], "confirmed": True,
                 "actions": ["planned_restart"], "notify": ["vision_engineer"]},
        "verdict": {"mode": "agree", "label": "룰·LLM 판단 일치", "severity": "crit",
                    "cause": "progressive_slowdown", "cause_name": "시간 경과형 처리 열화 — 기동 뒤 점유 시간이 누적 증가",
                    "actions": ["planned_restart", "collect_memory_snapshot"], "notify": ["vision_engineer"],
                    "message": "그룹 9 점유가 꾸준히 늘었다."},
        "evidence": {
            "capacity": {"group": 9, "pool": 6, "interval_s": 1.508, "limit_s": 9.05, "idle_median": 3,
                         "idle_baseline": 4, "idle_min": 1, "hold_p50": 5.37, "hold_p99": 6.04, "hold_max": 6.58,
                         "tolerance_s": 3.01, "tolerance_ratio": 0.332, "hold_source": "seq"},
            "trend": {"since": "10/11 00:09", "hours": 28.9, "hold_p50_baseline": 4.043, "hold_p50_now": 5.37,
                      "hold_ratio": 1.33, "interval_baseline_s": 1.508, "interval_now_s": 1.508,
                      "interval_change_pct": 0.0, "spearman_hourly": 0.99, "monotonic": True,
                      "groups_rising": [9, 10], "groups_with_baseline": 12},
            "inspections": {"inflight": 5, "inflight_ids": [INNER, INNER2], "oldest_inflight_s": 6.0,
                            "wait_threads_recent": [2, 4, 6, 1], "starts_10min": 4767, "ends_10min": 4768},
            "restarts": {"since_restart_s": None, "count_60min": 0},
            "key_facts": ["그룹 9의 처리 한계는 스레드 6개 × 투입 간격 1.508초 = 9.05초이다."],
            "log_lines": [f"2026/10/12-05:02:49.421 [Debug] V2M_INSPECT_START_ACK,V3.0,TALOS1,NoInspThread,{INNER},ProductID,10",
                          f"file {log_root}\\12\\InspStarter.log line 123"]},
        "llm": {"ok": True, "model": "qwen2.5:7b", "device": "cpu", "wall_s": 60.1, "root_cause": "progressive_slowdown",
                "confidence": 0.9, "evidence": ["점유 1.33배"]},
    }


@pytest.fixture()
def site(tmp_path):
    data, logs = tmp_path / "data", tmp_path / "logs"
    (data / "reports").mkdir(parents=True)
    logs.mkdir()
    cfg = tmp_path / "talog.yaml"
    cfg.write_text(f"site: 시험 설비\nlog_root: {logs}\ndata_dir: {data}\ntracking: core\nai: {{llm: false}}\n"
                   f"mail: {{provider: \"off\"}}\nnotify: {{toast: false, webhook: \"\"}}\n", encoding="utf-8")
    with open(data / "incidents_20261012_replay.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(_incident(str(logs)), ensure_ascii=False) + "\n")
    with open(data / "kpi_20261012_replay.jsonl", "w", encoding="utf-8") as f:
        for h, (hold, p99, rej) in enumerate([(4.6, 5.44, 0), (5.39, 6.0, 0), (5.54, 8.98, 1)]):
            for g in (9, 10):
                f.write(json.dumps({"hour": f"2026-10-12 0{h + 3}:00", "group": g, "n": 2387, "idle_median": 3,
                                    "hold_p50": hold, "hold_p99": p99, "hold_max": p99 + 0.5, "interval_s": 1.508,
                                    "pool": 6, "rejects": rej}) + "\n")
            f.write(json.dumps({"hour": f"2026-10-12 0{h + 3}:00", "group": "process", "mem_mb_max": 45000.0,
                                "cpu_pct": 45.3, "total_cpu_pct": 88.1, "threads": 2093}) + "\n")
    base = {"from": dt.datetime(2026, 10, 11, 0, 9).timestamp(), "to": dt.datetime(2026, 10, 11, 1, 10).timestamp(),
            "idle_median": 4, "hold_p50": 4.043, "hold_p99": 4.27, "interval_s": 1.508, "pool": 6, "n": 2399}
    with open(data / "capacity_state_replay.json", "w", encoding="utf-8") as f:
        json.dump({"version": 1, "gen_start": base["from"] - 600, "gen_reason": "", "hourly": {},
                   "groups": {"9": base, "10": dict(base, hold_p50=4.022)}}, f)
    (data / "reports" / "시험_12_diagnosis.md").write_text(
        f"# 시험_12 자동 진단 소견\n\n## [crit] 검사 시작 거부(NoInspThread) 1건 — 그룹 10\n- 05:02:49 inner {INNER} — 그룹 10\n",
        encoding="utf-8")
    con = sqlite3.connect(data / "reports" / "시험_12.sqlite")
    con.executescript("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
                      "CREATE TABLE events(id INTEGER PRIMARY KEY, ts REAL, ts_text TEXT, kind TEXT, inner_id TEXT,"
                      " value REAL, status TEXT, name TEXT);"
                      "CREATE TABLE inspections(inner_id TEXT PRIMARY KEY, start_text TEXT, duration_s REAL,"
                      " wait_threads INTEGER, status TEXT);")
    con.execute("INSERT INTO meta VALUES ('day_dir', ?)", (str(logs / "12"),))
    con.execute("INSERT INTO events VALUES (1, 1.0, '05:02:49.421', 'COMM_MSG', ?, 10, 'NoInspThread',"
                " 'V2M_INSPECT_START_ACK')", (INNER,))
    con.execute("INSERT INTO inspections VALUES (?, '05:02:46.412', 2.88, -1, 'complete')", (INNER,))
    con.commit()
    con.close()
    return {"cfg": str(cfg), "data": str(data), "logs": str(logs), "tmp": tmp_path}


def _srv(site, mask=True):
    return M.build([site["cfg"]], str(site["tmp"] / "work"), mask, str(site["tmp"] / "kb"))


def _call(srv, name, args=None):
    r = srv.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": name, "arguments": args or {}}})
    res = r["result"]
    return res["content"][0]["text"], res["isError"]


# ── 프로토콜 ──────────────────────────────────────────────────────────
def test_protocol_handshake_and_listing(site):
    srv = _srv(site)
    init = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "pytest", "version": "1"}}})["result"]
    assert init["protocolVersion"] == "2025-06-18"
    assert init["serverInfo"]["name"] == "talog" and "tools" in init["capabilities"]
    assert "overview" in init["instructions"]
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"overview", "get_incident", "capacity_trend", "query_report", "start_replay", "save_case"} <= names
    assert all(t["inputSchema"]["type"] == "object" for t in tools)
    writers = {t["name"] for t in tools if not t["annotations"]["readOnlyHint"]}
    assert writers == {"start_replay", "save_case"}
    prompts = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "prompts/list"})["result"]["prompts"]
    assert {p["name"] for p in prompts} == {"incident_rca", "daily_review", "site_health", "log_investigation"}
    msg = srv.handle({"jsonrpc": "2.0", "id": 4, "method": "prompts/get",
                      "params": {"name": "incident_rca", "arguments": {"incident_id": "X-1"}}})["result"]["messages"][0]
    assert "X-1" in msg["content"]["text"] and "Result" in msg["content"]["text"]
    res = srv.handle({"jsonrpc": "2.0", "id": 5, "method": "resources/read", "params": {"uri": "talog://runbook"}})
    assert "progressive_slowdown" in res["result"]["contents"][0]["text"]
    assert srv.handle({"jsonrpc": "2.0", "id": 6, "method": "ping"})["result"] == {}
    assert srv.handle({"jsonrpc": "2.0", "id": 8, "method": "nope"})["error"]["code"] == -32601
    assert srv.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                       "params": {"name": "nope", "arguments": {}}})["error"]["code"] == -32602
    assert srv.handle({"id": 10, "method": "ping"})["error"]["code"] == -32600


# ── 도구 ─────────────────────────────────────────────────────────────
def test_overview_and_incident_tools_mask_customer_values(site):
    srv = _srv(site)
    text, err = _call(srv, "overview")
    assert not err and "시험 설비" in text and "마스킹: 켜짐" in text and "2그룹" in text
    text, err = _call(srv, "list_incidents", {"day": "20261012", "severity": "crit"})
    assert not err and "20261012-050228-capacity_drift" in text and "시간 경과형 처리 열화" in text
    text, err = _call(srv, "list_incidents", {"severity": "warn"})
    assert "# 사건 0건" in text
    text, err = _call(srv, "get_incident", {"incident_id": "20261012-050228-capacity_drift"})
    assert not err
    for part in ("## 판단", "## 룰 근거", "## 핵심 사실", "## 경보 2건", "처리 여유: 그룹 9", "추세: 기준선 10/11 00:09부터",
                 "## 로컬 LLM 2차 의견", "근거 로그 2줄은 생략"):
        assert part in text, part
    assert INNER not in text and re.search(r"ID-[0-9a-f]{6}", text)
    # 같은 값은 같은 토큰 — 도구 사이에서 이어 볼 수 있다
    tok = srv.mask.token(INNER)
    text2, _ = _call(srv, "get_incident", {"incident_id": "20261012-050228-capacity_drift", "include_log_lines": True})
    assert tok in text2 and site["logs"] not in text2 and "<로그>" in text2
    text3, err = _call(srv, "get_incident", {"incident_id": "없는-사건"})
    assert err and "list_incidents" in text3


def test_no_mask_shows_raw_values(site):
    text, _ = _call(_srv(site, mask=False), "get_incident", {"incident_id": "20261012-050228-capacity_drift",
                                                            "include_log_lines": True})
    assert INNER in text and site["logs"] in text


def test_capacity_trend_shows_baseline_and_tolerance(site):
    text, err = _call(_srv(site), "capacity_trend", {"source": "시험"})
    assert not err
    assert "## 기준선" in text and "| 9 | 10/11 00:09~01:10 | 4 | 4.043" in text
    assert "## 그룹 9" in text and "## 그룹 10" in text and "## 프로세스" in text
    # 05시 행: 한계 9.05 − p99 8.98 = 내성 0.07, 거부 1
    assert re.search(r"\| 05:00 \| 2387 \| 3 \| 5\.54 \| 1\.37 \| 8\.98 \| 9\.48 \| 9\.05 \| 0\.07 \| 1\.508 \| 6 \| 1 \|", text), text


def test_report_tools_and_sql_guard(site):
    srv = _srv(site)
    text, err = _call(srv, "list_reports")
    assert not err and "시험_12" in text and "있음" in text
    text, err = _call(srv, "get_report", {"tag": "시험_12"})
    assert not err and "검사 시작 거부" in text and "| events | 1 |" in text and INNER not in text
    text, err = _call(srv, "query_report", {"tag": "시험_12", "sql": "SELECT ts_text, inner_id, value FROM events"})
    assert not err and "05:02:49.421" in text and srv.mask.token(INNER) in text and INNER not in text
    text, err = _call(srv, "query_report", {"tag": "시험_12", "sql": "SELECT replace(name, 'V2M_', '') FROM events"})
    assert not err and "INSPECT_START_ACK" in text
    for bad in ("DELETE FROM events", "SELECT 1; DROP TABLE events", "ATTACH DATABASE 'x.db' AS x",
                "WITH a AS (SELECT 1) SELECT * FROM a; PRAGMA writable_schema=1", "pragma table_info(events)"):
        text, err = _call(srv, "query_report", {"tag": "시험_12", "sql": bad})
        assert err, bad
    con = sqlite3.connect(os.path.join(site["data"], "reports", "시험_12.sqlite"))
    assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    con.close()
    text, err = _call(srv, "get_report", {"tag": "../x"})
    assert err


def test_runbook_rediagnose_and_cases(site):
    srv = _srv(site)
    text, err = _call(srv, "runbook")
    assert not err and "## reject" in text and "completion_signal_lost" in text and "drift_ratio=1.2" in text
    text, err = _call(srv, "runbook", {"family": "reject"})
    assert not err and "### isolated_stall" in text and "조치:" in text
    text, err = _call(srv, "rediagnose", {"source": "시험"})
    assert not err and "# 재진단 — 사건 1건" in text
    text, err = _call(srv, "save_case", {"title": "그룹 점유 누적", "symptom": f"inner {INNER} 거부",
                                         "cause": "열화", "action": "재시작"})
    assert err and "일반화" in text                     # 원본 id 는 사례에 못 넣는다
    text, err = _call(srv, "save_case", {"title": "그룹 점유 누적으로 NoInspThread", "symptom": "유휴 4→3, 거부 직전 경보",
                                         "cause": "시간 경과형 처리 열화", "action": "계획 재시작·메모리 덤프",
                                         "tags": ["NoInspThread"]})
    assert not err and "사례 저장" in text
    text, err = _call(srv, "search_cases", {"query": "NoInspThread 거부 직전 유휴", "k": 3})
    assert not err and "그룹 점유 누적으로 NoInspThread" in text
    text, err = _call(srv, "job_status")
    assert not err and "(없음)" in text


def _write_day(logs: str, day: str = "12"):
    """실시간 감시 테스트와 같은 형식의 작은 하루치 로그 (스레드 2개·투입 3초·소요 2초, 30건)."""
    d = os.path.join(logs, day)
    os.makedirs(d, exist_ok=True)
    t0 = dt.datetime.now().replace(microsecond=0) - dt.timedelta(minutes=10)

    def tt(sec):
        return (t0 + dt.timedelta(seconds=sec)).strftime("%Y/%m/%d-%H:%M:%S.%f")[:-3]

    ins, com = [], []
    for k in range(1, 31):
        inner, s = f"20000261013{k:08d}", 3.0 * k
        ins.append(f"{tt(s)}\t[Debug][CCommAiv::OnCommRecv][11]\tRecive M2V_INSPECT_START. innerId: {inner}, "
                   f"productId: ProductID, groupId: 1")
        ins.append(f"{tt(s + 0.001)}\t[Debug][CInspStarter::StartSeq][22]\teAIVProtocol_M2V_InspStart is arrived. "
                   f"Waiting thread count = 2, innerid: {inner}, productId: ProductID")
        com.append(f"{tt(s + 0.002)}\t[Debug][[S]LEN:0062][0]\tV2M_INSPECT_START_ACK,V3.0,TALOS1,OK,{inner},ProductID,1")
        com.append(f"{tt(s + 2.0)}\t[Debug][[S]LEN:0062][0]\tV2M_INSPECT_END,V3.0,TALOS1,OK,{inner},ProductID,1")
    for name, lines in (("InspStarter.log", ins), ("comm.log", com)):
        with open(os.path.join(d, name), "w", encoding="utf-8", newline="") as f:
            f.write("".join(x + "\r\n" for x in lines))


def test_raw_log_tools_read_search_trace_with_masking(site):
    _write_day(site["logs"])
    srv = _srv(site)
    text, err = _call(srv, "log_days")
    assert not err and "| 12 |" in text
    text, err = _call(srv, "log_files", {"day": "12"})
    assert not err and "InspStarter.log" in text and "comm.log" in text and "<로그>" in text
    text, err = _call(srv, "search_logs", {"day": "12", "query": "Waiting thread count = 2"})
    assert not err and "— 30건" in text and "InspStarter.log 30" in text
    assert not re.search(r"\d{12,}", text)                      # 검사 id 는 가려진다
    tok = re.search(r"innerid: (ID-[0-9a-f]{6})", text).group(1)
    # Claude 가 받은 토큰 그대로 검사를 추적한다 (토큰 → 원래 id 는 이 PC 의 작업 폴더에만)
    text, err = _call(srv, "trace_inspection", {"day": "12", "inspection_id": tok})
    assert not err and "InspStarter.log 2" in text and "comm.log 2" in text and "INSPECT_END" in text
    assert os.path.exists(site["tmp"] / "work" / ".mask_map.json")
    srv2 = _srv(site)                                             # 다음 대화(새 서버)에서도 같은 토큰을 안다
    assert not _call(srv2, "trace_inspection", {"day": "12", "inspection_id": tok})[1]
    text, err = _call(srv, "read_log", {"day": "12", "file": "comm.log", "tail": True, "lines": 3})
    assert not err and text.count("V2M_INSPECT_") == 3 and "(파일 끝)" in text
    t0 = re.search(r"(\d{2}:\d{2}:\d{2})", _call(srv, "read_log", {"day": "12", "file": "comm.log", "lines": 1})[0]).group(1)
    text, err = _call(srv, "read_log", {"day": "12", "file": "comm.log", "at": t0, "lines": 4, "before": 0})
    assert not err and "이어 읽기" in text and text.count("V2M_INSPECT_") == 4
    text, err = _call(srv, "search_logs", {"day": "12", "query": "INSPECT_END", "time_from": t0, "time_to": t0, "files": ["comm.log"]})
    assert not err and "— 0건" in text
    for bad in ({"day": "12", "file": "..\\..\\talog.yaml"}, {"day": "..", "file": "comm.log"},
                {"day": str(site["tmp"]), "file": "talog.yaml"}):
        assert _call(srv, "read_log", bad)[1], bad                # 로그 폴더 밖은 못 읽는다
    # 콘솔에서 고른 폴더(로그 루트 밖)는 'day=선택' 으로 읽고, 고르지 않은 다른 폴더는 막는다
    other = site["tmp"] / "copied" / "logs_1012"
    _write_day(str(other.parent), "logs_1012")
    outside = site["tmp"] / "elsewhere"
    _write_day(str(outside.parent), "elsewhere")
    with open(site["tmp"] / "work" / "log_roots.json", "w", encoding="utf-8") as f:
        json.dump({"selected": str(other), "roots": [str(other)]}, f)
    text, err = _call(srv, "log_files", {"day": "선택"})
    assert not err and "<로그 폴더>" in text and "copied" not in text
    assert "| 선택 |" in _call(srv, "log_days")[0]
    assert _call(srv, "log_files", {"day": str(outside)})[1]
    assert M.prompt_messages("log_investigation", {"day": "선택"})["messages"][0]["content"]["text"].startswith("talog 원본 로그")


def test_start_replay_runs_in_background_and_reports(site):
    _write_day(site["logs"])
    srv = _srv(site)
    text, err = _call(srv, "start_replay", {"source": "시험", "day": "..\\etc"})
    assert err
    text, err = _call(srv, "start_replay", {"source": "시험", "day": "99"})
    assert err and "12" in text                          # 있는 폴더를 알려 준다
    text, err = _call(srv, "start_replay", {"source": "시험", "day": "12", "carry_baseline": False})
    assert not err and "재현 작업 시작" in text
    jid = re.search(r"job_id='([^']+)'", text).group(1)
    deadline = time.time() + 120
    while time.time() < deadline:
        text, err = _call(srv, "job_status", {"job_id": jid})
        if "— 완료" in text or "— 실패" in text:
            break
        time.sleep(1)
    assert "— 완료" in text, text
    assert "리플레이 완료" in text and "## 경보 0건" in text
    # 끝난 작업은 출처로도 보인다
    assert f"재현:{jid}" in _call(srv, "overview")[0] or "재현 작업" in _call(srv, "overview")[0]


def test_stdio_transport_only_writes_protocol_lines(site):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    p = subprocess.Popen([sys.executable, "-m", "talog", "mcp", "--config", site["cfg"],
                          "--work-dir", str(site["tmp"] / "work")],
                         cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "overview", "arguments": {}}}]
    out, errs = p.communicate(("\n".join(json.dumps(r, ensure_ascii=False) for r in reqs) + "\n").encode("utf-8"),
                              timeout=60)
    lines = [ln for ln in out.decode("utf-8").splitlines() if ln.strip()]
    msgs = [json.loads(ln) for ln in lines]               # 모든 줄이 JSON — 다른 출력이 섞이지 않았다
    assert [m["id"] for m in msgs] == [1, 2, 3]
    assert msgs[0]["result"]["serverInfo"]["name"] == "talog"
    assert len(msgs[1]["result"]["tools"]) == len(M.TOOLS)
    assert "시험 설비" in msgs[2]["result"]["content"][0]["text"]
    assert "대기" in errs.decode("utf-8", "replace")      # 안내 문구는 stderr 로


def test_self_test_cli(site):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    r = subprocess.run([sys.executable, "-m", "talog", "mcp", "--config", site["cfg"], "--work-dir",
                        str(site["tmp"] / "work"), "--self-test"], cwd=ROOT, env=env, capture_output=True, timeout=60)
    out = r.stdout.decode("utf-8", "replace")
    assert r.returncode == 0 and "자가 점검 통과" in out and "도구 18개" in out


def test_self_test_passes_on_fresh_pc_with_only_raw_logs(tmp_path):
    """막 설치한 개발자 PC — 사건·리포트는 아직 없고 복사한 원본 로그만 있다. 연결 시험은 통과해야 한다."""
    logs, data = tmp_path / "AIV_LOG" / "설비A", tmp_path / "data"
    day = logs / "2026_10" / "12"
    day.mkdir(parents=True)
    (day / "InspStarter.log").write_text("2026/10/12-05:02:49.421 start\n", encoding="utf-8")
    cfg = tmp_path / "talog.yaml"
    cfg.write_text(f"site: 설비A\nlog_root: {logs}\ndata_dir: {data}\nai: {{llm: false}}\n", encoding="utf-8")
    rep = M.self_test_report(M.build([str(cfg)], str(tmp_path / "work"), True, str(tmp_path / "kb")))
    data_check = next(c for c in rep["checks"] if c["key"] == "data")
    assert rep["ok"] and data_check["ok"] and "원본 로그 폴더 1개" in data_check["detail"]
    assert rep["sources"][0]["log_days"] == 1 and rep["sources"][0]["incidents"] == 0
    # 아무것도 없으면(로그 루트도 없음) 실패하고 무엇을 하라는지 알린다
    cfg.write_text(f"site: 빈 설비\nlog_root: {tmp_path / 'nowhere'}\ndata_dir: {tmp_path / 'data2'}\n", encoding="utf-8")
    rep = M.self_test_report(M.build([str(cfg)], str(tmp_path / "work2"), True, str(tmp_path / "kb2")))
    data_check = next(c for c in rep["checks"] if c["key"] == "data")
    assert not rep["ok"] and not data_check["ok"] and "로그 루트" in data_check["detail"]


def test_init_workspace_keeps_user_lessons(site, tmp_path):
    ws = tmp_path / "agent"
    r = M.init_workspace(str(ws), [site["cfg"]])
    for rel in ("CLAUDE.md", "README.md", ".mcp.json", os.path.join(".claude", "skills", "talog-rca", "SKILL.md"),
                os.path.join(".claude", "skills", "talog-rca", "templates", "rca_report.html")):
        assert (ws / rel).exists(), rel
    assert (ws / "reports").is_dir() and (ws / "mcp_jobs").is_dir()
    mcp = json.loads((ws / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["talog"]
    assert mcp["type"] == "stdio" and "mcp" in mcp["args"] and os.path.abspath(site["cfg"]) in mcp["args"]
    assert mcp["args"][mcp["args"].index("--work-dir") + 1] == str(ws / "mcp_jobs")
    md = (ws / "CLAUDE.md").read_text(encoding="utf-8")
    assert "시험 설비" in md and "{{" not in md and "계획 먼저" in md and "모르면 묻는다" in md
    with open(ws / "CLAUDE.md", "a", encoding="utf-8") as f:
        f.write("- 현장 메모: 야간 2교대\n")
    r2 = M.init_workspace(str(ws), [site["cfg"]])
    assert "CLAUDE.md" in r2["kept"] and "야간 2교대" in (ws / "CLAUDE.md").read_text(encoding="utf-8")
    assert r["sources"] == ["시험 설비"]


def test_packaged_kit_matches_repo_skill():
    """저장소의 .claude/skills/talog-rca 와 패키지(claude_kit) 사본이 같아야 한다 (exe 로 만든 에이전트 폴더 = 개발판)."""
    repo = os.path.join(ROOT, ".claude", "skills", "talog-rca")
    kit = os.path.join(ROOT, "talog", "claude_kit", "skills", "talog-rca")
    if not os.path.isdir(repo):
        pytest.skip("저장소 스킬 사본 없음")
    for rel in ("SKILL.md", os.path.join("templates", "rca_report.html")):
        with open(os.path.join(repo, rel), encoding="utf-8") as a, open(os.path.join(kit, rel), encoding="utf-8") as b:
            assert a.read().replace("\r\n", "\n") == b.read().replace("\r\n", "\n"), rel


def test_server_command_points_at_entry_script(site):
    cmd = M.server_command([site["cfg"]], "W")
    assert cmd[0] == sys.executable and cmd[1].endswith("talog_main.py") and cmd[2] == "mcp"
    assert cmd[cmd.index("--config") + 1] == os.path.abspath(site["cfg"]) and cmd[-1] == os.path.abspath("W")
