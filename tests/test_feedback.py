# -*- coding: utf-8 -*-
"""사건 마무리·학습 — 상태 표시(확인·해결·오경보), 해결 기록 → 설비 사례 KB → 에이전트의 '비슷한 과거 사례', 규칙 피드백."""
import datetime as dt
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from talog import casekb, settings

DAY = dt.date.today().strftime("%Y%m%d")


def _incident(iid: str, rule: str, title: str) -> dict:
    return {"id": iid, "site": "시험 설비", "opened": "10:00:00", "closed": "10:01:00", "family": "reject",
            "alerts": [{"time": "10:00:00", "severity": "crit", "rule": rule, "title": title, "evidence": "e"}],
            "evidence": {"key_facts": ["그룹 3 유휴 스레드 0개"]},
            "verdict": {"mode": "rule_only", "label": "룰 판단", "cause": "thread_starvation",
                        "cause_name": "스레드 고갈 — 진행 중 검사가 스레드를 모두 잡음", "actions": ["planned_restart"],
                        "severity": "crit"},
            "action_text": {"planned_restart": "계획 정지 때 재시작"}}


@pytest.fixture
def console(tmp_path, monkeypatch):
    monkeypatch.setenv("TALOG_KB_DIR", str(tmp_path / "global_kb"))     # 기본 사례 KB 를 시험 폴더로
    from talog.console import Console, _handler_factory
    data = tmp_path / "data"
    data.mkdir()
    a = _incident(f"{DAY}-100000-no_insp_thread", "no_insp_thread", "검사 시작 거부 (NoInspThread) 그룹 3")
    b = _incident(f"{DAY}-110000-tact_drift", "tact_drift", "처리 시간 증가 — 그룹 5")
    (data / f"incidents_{DAY}.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in (a, b)) + "\n",
                                                encoding="utf-8")
    (data / f"alerts_{DAY}.jsonl").write_text("\n".join(json.dumps({"ts": f"{DAY[:4]}/{DAY[4:6]}/{DAY[6:]}-10:00:00",
                                                                     "rule": r, "severity": "warn"})
                                                         for r in ("tact_drift", "tact_drift", "no_insp_thread")) + "\n",
                                              encoding="utf-8")
    path = os.path.join(str(tmp_path), "talog.yaml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(settings.dump({"data_dir": str(data)}))
    con = Console(path)                                   # 현장 모드 — 사건 표시는 현장에서도 한다
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(con))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(p, body=None):
        req = urllib.request.Request(base + p, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "X-Talog-Token": con.token})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
    yield con, call, data, a, b
    srv.shutdown()
    srv.server_close()


def test_states_are_recorded_and_listed(console):
    con, call, data, a, b = console
    assert con.mode == "field"
    rows = {r["id"]: r for r in call("/api/incidents?days=1&replay=0")[1]["rows"]}
    assert rows[a["id"]]["state"] == "open"
    code, r = call("/api/incident/state", {"id": a["id"], "state": "ack"})
    assert code == 200 and r["state"] == "ack" and len(r["notes"]) == 1 and r["notes"][0]["mode"] == "field"
    assert call("/api/incident/state", {"id": a["id"], "state": "done"})[0] == 400
    assert call("/api/incident/state", {"id": "없는-사건", "state": "ack"})[0] == 404
    rows = {r["id"]: r for r in call("/api/incidents?days=1&replay=0")[1]["rows"]}
    assert rows[a["id"]]["state"] == "ack" and rows[b["id"]]["state"] == "open"
    d = call(f"/api/incident?id={a['id']}")[1]
    assert d["state"] == "ack" and d["notes"][-1]["state"] == "ack"


def test_resolution_becomes_a_case_the_agent_finds(console):
    con, call, data, a, _b = console
    code, r = call("/api/incident/state", {"id": a["id"], "state": "resolved", "cause": "알고리즘 타임아웃 누적",
                                            "action": "ALG 타임아웃 2초로", "note": "야간조 확인", "save_case": True})
    assert code == 200 and r["case"] == f"talog-{a['id']}"
    kb = casekb.CaseKB(casekb.site_kb_dir(str(data)))
    assert [c.id for c in kb.cases] == [f"talog-{a['id']}"]
    c = kb.cases[0]
    assert c.cause == "알고리즘 타임아웃 누적" and c.action == "ALG 타임아웃 2초로" and "NoInspThread" in c.title
    # 같은 사건을 다시 저장해도 사례가 늘지 않는다
    call("/api/incident/state", {"id": a["id"], "state": "resolved", "save_case": True})
    assert len(casekb.CaseKB(casekb.site_kb_dir(str(data))).cases) == 1
    # 에이전트는 기본 KB 와 설비 KB 를 함께 찾는다 → 다음 비슷한 사건에 이 기록이 붙는다
    from talog.agent.incident import IncidentAgent
    agent = SimpleNamespace(acfg={}, alert_dir=str(data))
    sim = IncidentAgent._similar(agent, [SimpleNamespace(title="검사 시작 거부 (NoInspThread) 그룹 3")],
                                 {"name": "스레드 고갈", "facts": ["그룹 3 유휴 스레드 0개"]})
    assert sim and sim[0]["cause"] == "알고리즘 타임아웃 누적"


def test_rule_feedback_counts_false_alarms(console):
    con, call, data, _a, b = console
    assert call("/api/incident/state", {"id": b["id"], "state": "false_alarm", "note": "투입 멈춤"})[0] == 200
    st = call("/api/rules/stats?days=7")[1]
    assert st["live"]["tact_drift"] == {"alerts": 2, "false": 1}
    assert st["live"]["no_insp_thread"] == {"alerts": 1, "false": 0}
    assert st["replay"] == {}


def test_search_all_merges_kbs(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    casekb.CaseKB(str(a)).add(casekb.Case(id="x1", title="GPU 컨텍스트 오류 반복", symptom="enqueueV3 실패", cause="드라이버"))
    casekb.CaseKB(str(b)).add(casekb.Case(id="y1", title="GPU 컨텍스트 오류 후 재시작", symptom="enqueueV3", cause="전원"))
    hits = casekb.search_all("GPU 컨텍스트 오류 enqueueV3", 5, [str(a), str(b), str(a)])
    assert sorted(c.id for _s, c in hits) == ["x1", "y1"]


def test_page_has_agent_pieces():
    from talog.console import build_page
    page = build_page()
    for i in ("opsBrief", "briefList", "btnBriefFold", "resDlg", "resForm", "resCase", "incOpen"):
        assert f'id="{i}"' in page, i
    for fn in ("function renderBrief", "function incTrace", "function stateBar", "function ruleFeedback"):
        assert fn in page, fn
