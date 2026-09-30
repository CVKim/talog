# -*- coding: utf-8 -*-
"""플랫폼 층 테스트: 사용자 설정(talog.yaml) ↔ 엔진 설정 변환, 결함 규칙, 콘솔 API, CLI."""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import yaml

from talog import settings, watch
from talog.events import Event
from talog.watch import Notifier, RuleEngine

_BASE = dt.datetime(2026, 7, 29, 9, 0, 0).timestamp()


def _end(ts, inner, result="NG", defects=("X",)):
    payload = ["V3.0", "TALOS1", result, str(len(defects)), *defects, inner, "P", "1"]
    e = Event(ts=ts, ts_text="", kind="COMM_MSG", name="V2M_INSPECT_END",
              extra=",".join(payload))
    watch.Extractor._enrich_comm(e)
    return e


# ── 설정 변환 ────────────────────────────────────────────────
def test_compile_defaults_and_mail_providers():
    cfg = settings.compile({})
    assert cfg["watch_root"] == r"D:\AIV_LOG\Talos" and cfg["agent"]["enabled"]
    assert cfg["agent"]["use_llm"] is False and not cfg["email"]["enabled"]
    assert cfg["tracking"]["mode"] == "default"
    g = settings.compile({"mail": {"provider": "gmail", "account": "a@gmail.com",
                                   "to": ["b@x.com"], "secret": "CIPHER"}})["email"]
    assert g["enabled"] and g["smtp_host"] == "smtp.gmail.com" and g["username"] == "a@gmail.com"
    assert g["password_dpapi"] == "CIPHER" and g["attach_json"] is False
    assert g["min_severity"] == "crit" and g["immediate"] == ["crit"]
    m = settings.compile({"mail": {"provider": "m365", "tenant_id": "t", "client_id": "c",
                                   "sender": "s@x.com", "secret": "C2", "digest": True,
                                   "to": "b@x.com, c@x.com"}})["email"]
    assert m["transport"] == "graph" and m["graph"]["client_secret_dpapi"] == "C2"
    assert m["min_severity"] == "warn" and m["to"] == ["b@x.com", "c@x.com"]


def test_compile_rules_builtin_and_custom():
    cfg = settings.compile({"rules": {
        "builtin": {"grab_fail": {"enabled": False}, "alg_timeout": {"severity": "crit"},
                    "img_timeout": {"count": 3, "window_min": 5},
                    "restart_burst": {"count": 4}, "gpu_temp": {"celsius": 80},
                    "ng_streak": {"enabled": True, "count": 7}},
        "custom": [{"type": "defect", "name": "치명", "match": "BOOT_*, *CRACK*",
                    "severity": "crit", "count": 1},
                   {"type": "log", "name": "GPU", "match": "enqueueV3", "files": ["DLInfer.log"],
                    "severity": "crit", "count": 1}]}})
    r = cfg["rules"]
    assert r["overrides"]["grab_fail"] == {"enabled": False}
    assert r["overrides"]["alg_timeout"] == {"severity": "crit"}
    assert r["overrides"]["img_timeout"] == {"count": 3, "window_min": 5.0}
    assert r["restart_burst"]["count"] == 4 and r["gpu_temp"]["celsius"] == 80
    assert r["defect_watch"]["ng_streak"] == 7 and r["defect_watch"]["ng_rate_window"] == 0
    assert r["defect_watch"]["rules"][0]["match"] == ["BOOT_*", "*CRACK*"]
    assert r["patterns"][0]["name"] == "GPU" and "type" not in r["patterns"][0]
    # 핵심 로그 밖 파일을 보는 로그 규칙 → 그 파일도 같이 읽도록 선택 모드로
    assert cfg["tracking"]["mode"] == "select" and "DLInfer.log" in cfg["tracking"]["files"]
    assert "Comm.log" in cfg["tracking"]["files"]


def test_legacy_watch_yaml_migrates_losslessly(tmp_path):
    legacy = {"watch_root": r"E:\logs", "site": "PC3", "poll_seconds": 5,
              "tracking": {"mode": "auto", "exclude": ["*.dmp"]},
              "rules": {"insp_stall": {"cooldown_min": 20},
                        "defect_watch": {"critical": ["BOOT_*"], "repeat_count": 10,
                                         "ng_rate_window": 50},
                        "patterns": [{"name": "비상정지", "match": "re:EMERGENCY",
                                      "level": "Error", "severity": "crit", "count": 1}],
                        "overrides": {"grab_fail": {"severity": "warn"}}},
              "llm": {"enabled": True, "script": "x.txt", "device": "auto", "keep_alive": "2m"},
              "agent": {"enabled": True, "use_llm": True, "batch_seconds": 30},
              "email": {"enabled": True, "smtp_host": "relay.local", "smtp_port": 25,
                        "security": "none", "to": ["a@x.com"], "min_severity": "warn"}}
    p = tmp_path / "watch.yaml"
    p.write_text(yaml.safe_dump(legacy, allow_unicode=True), encoding="utf-8")
    s, was_legacy = settings.load(str(p))
    assert was_legacy and s["tracking"] == "all" and s["mail"]["provider"] == "smtp"
    assert s["mail"]["digest"] and s["ai"]["llm"] and s["ai"]["device"] == "auto"
    types = [(c["type"], c.get("name")) for c in s["rules"]["custom"]]
    assert ("log", "비상정지") in types and ("defect", "치명 결함") in types
    assert s["rules"]["builtin"]["ng_rate"]["enabled"]
    assert s["advanced"] == {"poll_seconds": 5, "tracking": {"exclude": ["*.dmp"]},
                             "rules": {"insp_stall": {"cooldown_min": 20}},
                             "llm": {"keep_alive": "2m"}, "agent": {"batch_seconds": 30}}
    eng = watch.load_config(str(p))
    back = settings.compile(s)
    assert settings._diff(settings._norm_engine(eng), settings._norm_engine(back)) \
        is settings._SAME
    # 저장한 새 형식을 엔진이 그대로 읽는다
    p2 = tmp_path / "talog.yaml"
    p2.write_text(settings.dump(s), encoding="utf-8")
    assert watch.load_config(str(p2))["rules"]["insp_stall"]["cooldown_min"] == 20
    assert not settings.load(str(p2))[1]


def test_validate_messages():
    errs = settings.validate({"mail": {"provider": "gmail"},
                              "rules": {"custom": [{"type": "log", "name": "x",
                                                    "match": "re:("}]}})
    assert any("정규식 오류" in e for e in errs)
    assert any("받는 사람" in e for e in errs) and any("Gmail 계정" in e for e in errs)
    assert settings.validate({}) == []


# ── 결함 규칙 (엔진) ─────────────────────────────────────────
def test_defect_rule_count_window_per_name(tmp_path):
    cfg = settings.compile({"data_dir": str(tmp_path), "rules": {"custom": [
        {"type": "defect", "name": "크랙 반복", "match": ["*CRACK*"], "severity": "warn",
         "count": 3, "window_min": 10}]}})
    n = Notifier(cfg, replay=True)
    eng = RuleEngine(cfg, n)
    eng.feed(_end(_BASE, "2000326072907195131", defects=("SIDE_CRACK",)))
    eng.feed(_end(_BASE + 60, "2000326072907195132", defects=("TOP_CRACK",)))
    eng.feed(_end(_BASE + 120, "2000326072907195133", defects=("SIDE_CRACK",)))
    assert n.sent == []                             # 같은 결함명끼리 센다 (SIDE 2건)
    eng.feed(_end(_BASE + 180, "2000326072907195134", defects=("SIDE_CRACK",)))
    assert [(a.rule, a.severity) for a in n.sent] == [("defect_repeat", "warn")]
    assert "SIDE_CRACK 10분 내 3건" in n.sent[0].title
    eng.feed(_end(_BASE + 900, "2000326072907195135", defects=("SIDE_CRACK",)))
    assert len(n.sent) == 1                         # 10분 창 밖 — 다시 1건부터


def test_defect_rule_crit_immediate(tmp_path):
    cfg = settings.compile({"data_dir": str(tmp_path), "rules": {"custom": [
        {"type": "defect", "name": "치명", "match": ["BOOT_DAMAGED"], "severity": "crit"}]}})
    n = Notifier(cfg, replay=True)
    eng = RuleEngine(cfg, n)
    eng.feed(_end(_BASE, "2000326072907195131", defects=("BOOT_DAMAGED",)))
    assert [a.rule for a in n.sent] == ["defect_critical"]
    assert n.sent[0].title == "치명 결함 검출: BOOT_DAMAGED"


# ── 콘솔 API ─────────────────────────────────────────────────
def _serve(con):
    from talog.console import _handler_factory
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(con))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(p, body=None, token=True, host=None):
        hdr = {"Content-Type": "application/json"}
        if token:
            hdr["X-Talog-Token"] = con.token
        if host:
            hdr["Host"] = host
        req = urllib.request.Request(base + p, data=None if body is None else
                                     json.dumps(body).encode(), headers=hdr)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
    return srv, call


def test_console_settings_save_and_security(tmp_path):
    from talog.console import Console
    path = os.path.join(str(tmp_path), "talog.yaml")
    con = Console(path)
    con.settings["data_dir"] = str(tmp_path / "data")
    con.cfg = settings.compile(con.settings)
    srv, call = _serve(con)
    try:
        code, r = call("/api/settings")
        assert code == 200 and r["settings"]["tracking"] == "core"
        assert len(r["meta"]["builtin"]) >= 15 and r["meta"]["providers"]["gmail"]
        new = r["settings"]
        new["site"] = "T1"
        new["rules"]["custom"] = [{"type": "log", "name": "x", "match": "re:("}]
        assert call("/api/settings", {"settings": new}, token=False)[0] == 403
        assert call("/api/settings", {"settings": new}, host="evil.example")[0] == 403
        code, res = call("/api/settings", {"settings": new})
        assert code == 400 and "정규식 오류" in res["errors"][0]
        new["rules"]["custom"] = [{"type": "defect", "name": "치명", "match": ["BOOT_*"],
                                   "severity": "crit", "count": 1}]
        new["mail"].update(provider="gmail", account="me@gmail.com", to=["me@gmail.com"])
        pw = "app-pw-secret-987" if sys.platform == "win32" else ""
        code, res = call("/api/settings", {"settings": new, "secret": pw})
        assert code == 200 and res["ok"], res
        saved = open(path, encoding="utf-8").read()
        doc = yaml.safe_load(saved)
        assert doc["site"] == "T1" and "app-pw-secret-987" not in saved
        assert "# off | gmail | m365 | smtp" in saved
        assert watch.load_config(path)["email"]["smtp_host"] == "smtp.gmail.com"
        if pw:
            assert res["settings"]["mail"]["has_secret"] and not res["settings"]["mail"]["secret"]
            # 발송 방식을 바꾸면 옛 비밀은 버린다
            new2 = res["settings"]
            new2["mail"].update(provider="smtp", host="relay.local", account="")
            code, res2 = call("/api/settings", {"settings": new2})
            assert code == 200 and not res2["settings"]["mail"]["has_secret"]
        assert os.path.exists(path + ".bak")
        code, r = call("/api/test/rule", {"rules": [
            {"type": "log", "name": "n", "match": "NoInspThread", "files": ["comm.log"]},
            {"type": "defect", "name": "d", "match": ["BOOT_*"]}],
            "text": "...ACK,V3.0,TALOS3,NoInspThread,1", "file": "Comm.log"})
        assert [x["match"] for x in r["results"]] == [True, False]
        code, st = call("/api/status")
        assert code == 200 and set(st) >= {"collect", "detect", "judge", "notify"}
        assert call("/r/..%5Cx/")[0] == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_console_reports_served_with_api_prefix(tmp_path):
    from talog.console import Console
    con = Console(os.path.join(str(tmp_path), "talog.yaml"))
    con.settings["data_dir"] = str(tmp_path)
    con.cfg = settings.compile(con.settings)
    rep = tmp_path / "reports"
    rep.mkdir()
    (rep / "2026_09_28.html").write_text("<html><head></head><body>r</body></html>",
                                         encoding="utf-8")
    (rep / "2026_09_28_diagnosis.md").write_text("# x\n\n## [crit] 스레드 고갈\n",
                                                 encoding="utf-8")
    srv, call = _serve(con)
    try:
        code, r = call("/api/reports")
        assert r["rows"][0]["tag"] == "2026_09_28" and r["rows"][0]["n_findings"] == 1
        code, page = call("/r/2026_09_28/")
        assert code == 200 and b'window.TALOG_API="/r/2026_09_28"' in page
    finally:
        srv.shutdown()
        srv.server_close()


# ── CLI ─────────────────────────────────────────────────────
def test_cli_help_and_routing(capsys, tmp_path):
    from talog import cli
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "talog run" in out and "talog analyze" in out
    assert cli.main(["analyze", str(tmp_path)]) == 2          # 로그 폴더 아님
    from talog.console import _report_tag
    assert _report_tag(r"D:\AIV_LOG\Talos\2026_09\28") == "2026_09_28"
