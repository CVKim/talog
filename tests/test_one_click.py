"""v2.5 '딸깍' — 처음 설정(이 PC 의 talos 로그 폴더 찾기 → 이 폴더로 시작)과 사건 한 번에 마무리(되돌리면 사례 KB 에서도 빠짐)."""
import json
import os

from talog.console.setup import find_log_roots


def _talos_root(base, name="Talos", day=("2026_10", "21"), files=("InspStarter.log", "comm.log")):
    d = os.path.join(base, name, *day)
    os.makedirs(d)
    for f in files:
        open(os.path.join(d, f), "w").close()
    return os.path.join(base, name)


def test_find_log_roots_picks_talos_layout(tmp_path):
    base = tmp_path / "AIV_LOG"
    good = _talos_root(str(base), "Talos")
    _talos_root(str(base), "Other", files=("readme.txt",))            # 일자 폴더는 있는데 talos 로그가 아님
    os.makedirs(base / "Empty")
    got = find_log_roots([str(base)])
    assert [g["root"] for g in got] == [good]
    assert got[0]["latest"] == os.path.join("2026_10", "21") and got[0]["files"] == 2
    # 일자 폴더를 바로 가진 폴더를 주면 그 폴더 자체가 후보
    assert [g["root"] for g in find_log_roots([good])] == [good]


def test_setup_root_saves_log_root_only_in_operator_mode(tmp_path):
    from talog.console import Console
    from talog.console.http import POST_ROUTES
    root = _talos_root(str(tmp_path / "logs"))
    con = Console(str(tmp_path / "talog.yaml"), mode="field")
    code, body = POST_ROUTES["/api/setup/root"](con, {"root": root})
    assert code == 403                                                  # 현장 모드는 PIN 으로 운영자 모드가 먼저
    con.mode = "operator"
    code, body = POST_ROUTES["/api/setup/root"](con, {"root": str(tmp_path / "nope")})
    assert code == 400 and not body["ok"]
    code, body = POST_ROUTES["/api/setup/root"](con, {"root": root})
    assert code == 200 and body["ok"] and con.settings["log_root"] == root
    assert con.cfg["watch_root"] == root and con.settings["mode"] == "field"   # 다른 값(모드)은 그대로
    con.stop_watch()


def test_undo_resolution_removes_case_from_site_kb(tmp_path):
    from talog.casekb import CaseKB, site_kb_dir
    from talog.console import Console
    con = Console(str(tmp_path / "talog.yaml"), mode="field")
    os.makedirs(con.data_dir, exist_ok=True)
    iid = "20261012-040101-capacity_drift"
    with open(os.path.join(con.data_dir, "incidents_20261012_replay.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"id": iid, "site": "설비A", "opened": "04:01:01", "closed": "04:02:01", "family": "capacity",
                            "alerts": [{"rule": "capacity_drift", "title": "처리 여유 감소", "severity": "warn", "time": "04:01:01"}],
                            "verdict": {"cause": "progressive", "cause_name": "시간 경과형 처리 열화", "actions": ["restart"]},
                            "action_text": {"restart": "휴지 시점에 재시작"}}, ensure_ascii=False) + "\n")
    code, r = con.set_incident_state(iid, True, "resolved", "시간 경과형 처리 열화", "휴지 시점에 재시작", "", True)
    assert code == 200 and r["case"] == f"talog-{iid}"
    assert [c.id for c in CaseKB(site_kb_dir(con.data_dir)).cases] == [f"talog-{iid}"]
    code, r = con.set_incident_state(iid, True, "open")                 # 알림의 '되돌리기' = 다시 열기
    assert code == 200 and r["notes"][-1]["case_removed"] == f"talog-{iid}"
    assert CaseKB(site_kb_dir(con.data_dir)).cases == []
    assert con.incident_detail(iid, True)["state"] == "open"
    con.set_incident_state(iid, True, "resolved", "", "", "", True)    # 해결 → 바로 오경보로 바꿔도 사례에서 빠진다
    code, r = con.set_incident_state(iid, True, "false_alarm")
    assert code == 200 and r["notes"][-1]["case_removed"] == f"talog-{iid}"
    assert CaseKB(site_kb_dir(con.data_dir)).cases == []


def test_page_has_one_click_controls():
    from talog.console import build_page
    page = build_page()
    for s in ('"/api/setup/root"', 'data-st="${k}"', '"해결 — 이대로"', "quickResolution(", '{label: "되돌리기"', "useFoundRoot()",
              'location.hash === "#inc" && window.innerWidth >= 1200'):
        assert s in page, s
