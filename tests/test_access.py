# -*- coding: utf-8 -*-
"""모드(현장·운영자·개발자) — talog/access.py · console/modes.py · console/http.py 의 oper·dev."""
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
import yaml

from talog import access, settings

TEST_PIN = "2468"


@pytest.fixture(autouse=True)
def _test_pin(monkeypatch):
    """실제 고정 PIN 대신 시험용 PIN 의 해시로 바꿔 끼운다 (저장소에 PIN 원문을 두지 않는다)."""
    monkeypatch.setattr(access, "PIN_HASH", access.hash_pin(TEST_PIN))


# ── 모드·PIN 기본 ─────────────────────────────────────────────
def test_resolve_mode_prefers_cli_then_file_then_field():
    assert access.resolve_mode(None) == "field"
    assert access.resolve_mode("operator") == "operator"
    assert access.resolve_mode("developer", "field") == "field"
    assert access.resolve_mode("field", "developer") == "developer"
    assert access.resolve_mode("DEV-PC?") == "field"              # 모르는 값은 안전한 쪽


def test_pin_only_when_going_up():
    assert access.needs_pin("field", "operator") and access.needs_pin("operator", "developer")
    assert not access.needs_pin("developer", "field") and not access.needs_pin("operator", "operator")
    assert access.check_pin(TEST_PIN) and not access.check_pin("2469") and not access.check_pin("")
    real = "pbkdf2_sha256$200000$0f5d4c130956a151cca71c0db451fbcd$" \
           "4a04463c18c5976053d92e289ca786abf2b1863fc01b13c425fe58d4dc6d939f"
    assert not access.check_pin(TEST_PIN, real)                   # 고정 PIN 해시 형식은 그대로 읽힌다


def test_settings_carry_mode():
    assert settings.normalize({})["mode"] == "field"
    assert any("mode 값 오류" in e for e in settings.validate({"mode": "lab"}))
    assert settings.validate({"mode": "operator"}) == [] and settings.validate({"mode": "developer"}) == []
    text = settings.dump({"mode": "operator"})
    assert "mode: operator  # field = 현장" in text and yaml.safe_load(text)["mode"] == "operator"
    assert "mode" not in settings.compile({"mode": "developer"})   # 엔진 설정에는 들어가지 않는다


# ── 콘솔 HTTP ────────────────────────────────────────────────
def _serve(con):
    from talog.console import _handler_factory
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
    return srv, call


@pytest.fixture
def console(tmp_path):
    from talog.console import Console
    path = os.path.join(str(tmp_path), "talog.yaml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(settings.dump({"data_dir": str(tmp_path / "data")}))
    con = Console(path)
    srv, call = _serve(con)
    yield con, call, path
    srv.shutdown()
    srv.server_close()


def test_field_mode_views_but_cannot_change(console, tmp_path):
    con, call, _path = console
    assert con.mode == "field" and call("/api/access")[1]["mode"] == "field"
    code, r = call("/api/settings")
    assert code == 200 and r["meta"]["access"]["mode"] == "field"
    new = r["settings"]
    for p, body in (("/api/settings", {"settings": new}), ("/api/test/rule", {"rules": [], "text": "x"}),
                    ("/api/test/alert", {"kind": "noinsp"}), ("/api/control", {"action": "stop"}),
                    ("/api/job", {"kind": "replay", "dir": str(tmp_path)})):
        code, r = call(p, body)
        assert code == 403 and r["need"] == "operator" and "운영자 모드" in r["error"], p
    code, r = call("/api/claude/ask")
    assert code == 403 and r["need"] == "developer"
    # 보기·리포트 만들기는 현장 모드에서도
    assert call("/api/status")[0] == 200
    code, r = call("/api/job", {"kind": "report", "dir": str(tmp_path / "없는 폴더")})
    assert code == 200 and "폴더가 없습니다" in r["error"]


def test_switching_up_needs_pin_and_is_saved(console):
    con, call, path = console
    code, r = call("/api/access/mode", {"mode": "operator"})
    assert code == 403 and r["left"] == 4
    for left in (3, 2, 1, 0):
        assert call("/api/access/mode", {"mode": "operator", "pin": "0000"})[1]["left"] == left
    assert call("/api/access/mode", {"mode": "operator", "pin": TEST_PIN})[0] == 429   # 5번 틀리면 잠시 거절
    con._pin_fail = []
    code, r = call("/api/access/mode", {"mode": "operator", "pin": TEST_PIN})
    assert code == 200 and r["access"]["mode"] == "operator"
    assert yaml.safe_load(open(path, encoding="utf-8"))["mode"] == "operator"         # 다음에 열 때도 같다
    s = call("/api/settings")[1]["settings"]
    s.update(site="T9", mode="field")                    # 설정 저장으로는 모드를 바꾸지 못한다
    code, r = call("/api/settings", {"settings": s})
    assert code == 200 and r["ok"], r
    doc = yaml.safe_load(open(path, encoding="utf-8"))
    assert doc["site"] == "T9" and doc["mode"] == "operator"
    assert call("/api/claude/ask")[0] == 403                            # Claude 는 개발자 모드에서만
    assert call("/api/access/mode", {"mode": "developer", "pin": "9999"})[0] == 403
    code, r = call("/api/access/mode", {"mode": "developer", "pin": TEST_PIN})
    assert code == 200 and r["access"]["mode"] == "developer"
    code, r = call("/api/claude/ask")
    assert code == 200 and "turns" in r
    # 내릴 때는 PIN 을 묻지 않는다
    code, r = call("/api/access/mode", {"mode": "field"})
    assert code == 200 and r["access"]["mode"] == "field"
    assert yaml.safe_load(open(path, encoding="utf-8"))["mode"] == "field"
    assert call("/api/access/mode", {"mode": "lab"})[0] == 400


def test_cli_mode_override_is_not_saved(tmp_path):
    from talog.console import Console
    path = os.path.join(str(tmp_path), "talog.yaml")
    con = Console(path, mode="developer")
    assert con.mode == "developer" and con.is_dev() and con.at_least("operator")
    assert not os.path.exists(path)                       # 실행 인자는 파일에 쓰지 않는다
    from talog import cli
    assert "--mode field|operator|developer" in cli.USAGE and "admin-pin" not in cli.USAGE


def test_page_has_mode_switch_and_hides_claude_outside_developer():
    from talog.console import build_page
    page = build_page()
    for i in ("modeSw", "modeOpts", "pinDlg", "pinForm", "pinIn", "setFs", "s-mode"):
        assert f'id="{i}"' in page, i
    assert page.count('<button type="button" role="radio" data-mode=') == 3        # 왼쪽 위 세 칸
    assert '<a href="#ai" data-v="ai" class="dev-only">' in page
    assert 'class="ana-opt dev-only" id="btnAnaClaude"' in page
    assert 'html:not([data-mode="developer"]) .dev-only{display:none!important}' in page
