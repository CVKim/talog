# -*- coding: utf-8 -*-
"""Microsoft Graph 발송기 테스트 — 가짜 토큰(Entra) / Graph 서버로 확인한다."""

from __future__ import annotations

import base64
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from talog import watch
from talog.mailer import Mailer, sample_incident


class _FakeMS(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, code, obj=None):
        data = json.dumps(obj or {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        srv = self.server
        if self.path.endswith("/oauth2/v2.0/token"):
            form = urllib.parse.parse_qs(body.decode())
            srv.token_calls.append(form)
            if form.get("client_secret") != ["good-secret"]:
                return self._reply(401, {"error": "invalid_client",
                                         "error_description": "AADSTS7000215: Invalid client secret"})
            return self._reply(200, {"access_token": "tok-1", "expires_in": 3599})
        if self.path.startswith("/v1.0/users/") and self.path.endswith("/sendMail"):
            srv.sends.append({"path": self.path, "auth": self.headers.get("Authorization"),
                              "json": json.loads(body)})
            return self._reply(202)
        return self._reply(404)


@pytest.fixture()
def fake_ms():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeMS)
    srv.token_calls, srv.sends = [], []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _mailer(tmp_path, fake_ms, secret_env="TALOG_TEST_GRAPH"):
    base = f"http://127.0.0.1:{fake_ms.server_address[1]}"
    cfg = watch.load_config("")
    cfg["site"] = "PC3"
    cfg["email"].update(enabled=True, transport="graph", to=["lead@contoso.com"],
                        roles={"quality": ["q@contoso.com"]})
    cfg["email"]["graph"].update(tenant_id="contoso.com", client_id="app-123",
                                 sender="talog-alert@contoso.com", secret_env=secret_env,
                                 token_url=base + "/{tenant}/oauth2/v2.0/token",
                                 api_url=base + "/v1.0")
    return Mailer(cfg, str(tmp_path))


def test_graph_send_payload_and_token_cache(tmp_path, fake_ms, monkeypatch):
    m = _mailer(tmp_path, fake_ms)
    assert not m.check()[0]                          # 암호 없음
    monkeypatch.setenv("TALOG_TEST_GRAPH", "good-secret")
    ok, detail = m.check()
    assert ok and "토큰 발급 OK" in detail
    inc = sample_incident("PC3")
    inc["verdict"]["notify"] = ["quality"]
    r1 = m.send_incident(inc)
    r2 = m.send_incident(inc, reply_to=r1["message_id"], tag="[LLM 2차 의견]", suffix="_llm")
    assert r1["sent"] and r2["sent"], (r1, r2)
    assert len(fake_ms.token_calls) == 1               # 토큰 재사용
    assert fake_ms.token_calls[0]["scope"] == ["https://graph.microsoft.com/.default"]
    s = fake_ms.sends[0]
    assert s["auth"] == "Bearer tok-1"
    assert s["path"] == "/v1.0/users/talog-alert%40contoso.com/sendMail"
    msg = s["json"]["message"]
    assert [x["emailAddress"]["address"] for x in msg["toRecipients"]] == \
        ["lead@contoso.com", "q@contoso.com"]
    assert msg["subject"].startswith("[talog][CRIT][PC3]") and msg["body"]["contentType"] == "HTML"
    att = msg["attachments"][0]
    assert att["name"].startswith("incident_") and json.loads(base64.b64decode(att["contentBytes"]))
    assert msg["internetMessageHeaders"][0]["name"] == "X-Talog-Incident"
    assert fake_ms.sends[1]["json"]["message"]["subject"].startswith("Re: [talog]")


def test_graph_bad_secret_reports_aad_error(tmp_path, fake_ms, monkeypatch):
    monkeypatch.setenv("TALOG_TEST_GRAPH", "wrong")
    m = _mailer(tmp_path, fake_ms)
    res = m.send_incident(sample_incident("PC3"))
    assert not res["sent"] and "AADSTS7000215" in res["error"]
    assert res["eml"]                                  # 실패해도 outbox 사본은 남는다
