"""콘솔 HTTP — 127.0.0.1 전용. 경로 → Console 메서드 표.

보안: Host 헤더가 127.0.0.1·localhost 일 때만 받고(DNS 리바인딩 차단), POST 는 페이지에 심은 세션 토큰
(X-Talog-Token)이 있어야 받는다(다른 사이트의 위조 요청 차단). 응답은 늘 no-store·nosniff. 접근 로그는 남기지 않는다.

새 API 는 GET_ROUTES / POST_ROUTES 에 한 줄을 더한다 — 함수는 (con, 인자) 를 받는다(GET 은 쿼리 dict, POST 는 JSON 본문):
    "/api/foo": lambda con, q: con.foo(q.get("x", "")),
돌려주는 값: dict·list → 200 JSON · (상태, 본문) · (상태, 본문, Content-Type). 없는 대상은 FileNotFoundError → 404.

모드(modes.py): 운영자 모드 이상에서 여는 경로는 oper(…), 개발자 모드에서만 여는 경로는 dev(…) 로 감싼다.
감싸지 않은 경로는 현장 모드에서도 쓴다 — 보기·리포트 만들기·사건 질문 같은 것.
"""

from __future__ import annotations

import json
import os
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from .. import __version__
from .app import Console
from .page import build_page
from .util import ASSETS, _Tee

JSON = "application/json; charset=utf-8"
HTML = "text/html; charset=utf-8"
Route = Callable[[Console, dict], Any]

NEED_OPER = "현장 모드에서는 할 수 없습니다 — 왼쪽 위에서 운영자 모드로 바꾸십시오 (PIN)"
NEED_DEV = "Claude 연동은 개발자 모드에서만 씁니다 — 왼쪽 위에서 개발자 모드로 바꾸십시오 (PIN)"


# ── 모드 ────────────────────────────────────────────────────────────
def _needs(mode: str, message: str) -> Callable[[Route], Route]:
    def wrap(fn: Route) -> Route:
        def run(con: Console, arg: dict):
            if not con.at_least(mode):
                return 403, {"ok": False, "error": message, "need": mode, "mode": con.mode}
            return fn(con, arg)
        return run
    return wrap


oper = _needs("operator", NEED_OPER)     # 운영자 모드 이상: 설정 저장·시험·감시 정지·경보 재현
dev = _needs("developer", NEED_DEV)      # 개발자 모드: Claude 연동


# ── GET ─────────────────────────────────────────────────────────────
def _incident(con: Console, q: dict):
    d = con.incident_detail(q.get("id", ""), q.get("replay") == "1")
    return (200, d) if d else (404, {"error": "없음"})


def _folder(con: Console, q: dict):
    tr = q.get("tracking")
    return con.folder(q.get("dir", ""), json.loads(tr) if tr else None)


def _job(con: Console, _q: dict):
    j = {k: v for k, v in con.job.items() if k != "log"}
    j["log"] = list(con.job.get("log") or [])[-250:]
    return j


def _claude_file(con: Console, q: dict):
    data, ctype = con.claude_file(q.get("path", ""))
    return 200, data, ctype


GET_ROUTES: dict[str, Route] = {
    # 운영·설정
    "/api/status": lambda con, q: con.status(),
    "/api/access": lambda con, q: con.access_info(),
    "/api/settings": lambda con, q: {"settings": con.public_settings(), "meta": con.meta()},
    "/api/folder": _folder,
    "/api/log": lambda con, q: {"lines": list(con.log)[-300:]},
    "/api/llm/models": lambda con, q: con.llm_models(q.get("url", "")),
    # 사건
    "/api/incidents": lambda con, q: {"rows": con.incidents(int(q.get("days", 3)), q.get("replay", "1") == "1")},
    "/api/incident": _incident,
    "/api/mail": lambda con, q: (200, con.mail_html(q.get("id", ""), q.get("replay") == "1",
                                                    q.get("which", "initial")), HTML),
    "/api/timeline": lambda con, q: con.timeline(int(q.get("days", 7))),
    "/api/rules/stats": lambda con, q: con.rule_stats(int(q.get("days", 7))),
    "/api/setup/detect": lambda con, q: con.setup_detect(),
    "/api/health": lambda con, q: con.health(),
    "/api/daily": lambda con, q: (200, con.daily_html(q.get("day", ""), q.get("replay") == "1"), HTML),
    # 분석
    "/api/reports": lambda con, q: {"rows": con.reports()},
    "/api/job": _job,
    # Claude (개발자 모드)
    "/api/claude": dev(lambda con, q: con.claude_info()),
    "/api/claude/ask": dev(lambda con, q: con.claude_ask_status()),
    "/api/claude/file": dev(_claude_file),
}


# ── POST ────────────────────────────────────────────────────────────
def _save_settings(con: Console, b: dict):
    errs = con.save(b.get("settings") or {}, b.get("secret") or "", bool(b.get("clear_secret")))
    return (400 if errs else 200), {"ok": not errs, "errors": errs, "settings": con.public_settings()}


def _stop_watch(con: Console, _b: dict):
    con.stop_watch()
    return {"ok": True, "running": con.running()}


def _control(con: Console, b: dict):
    if b.get("action") == "stop":
        return oper(_stop_watch)(con, b)           # 감시 정지는 운영자 모드 이상
    if b.get("action") == "start":
        con.start_watch()                          # 시작은 현장 모드에서도 — 현장 PC 가 감시를 멈춘 채 남지 않게
    return {"ok": True, "running": con.running()}


def _start_job(con: Console, b: dict):
    return con.start_job(b.get("kind", ""), b.get("dir", ""), b.get("recipe", ""))


def _job_post(con: Console, b: dict):
    """진단 리포트는 현장 모드에서도, 경보 재현(규칙을 고칠 때 쓰는 시험)은 운영자 모드 이상."""
    return (oper(_start_job) if b.get("kind") == "replay" else _start_job)(con, b)


def _claude_ask(con: Console, b: dict):
    return con.claude_ask(b.get("q", ""), b.get("model", ""), b.get("effort", ""), bool(b.get("new")),
                          str(b.get("skill") or ""), b.get("args") or {}, b.get("allow_replay") is True)


POST_ROUTES: dict[str, Route] = {
    # 모드 (위로 올릴 때만 PIN)
    "/api/access/mode": lambda con, b: con.set_mode(str(b.get("mode") or ""), str(b.get("pin") or "")),
    # 운영·설정
    "/api/settings": oper(_save_settings),
    "/api/control": _control,
    "/api/setup/root": oper(lambda con, b: con.setup_root(b.get("root", ""))),
    "/api/test/llm": oper(lambda con, b: con.test_llm(b)),
    "/api/test/mail-check": oper(lambda con, b: con.test_mail_check(b)),
    "/api/test/mail": oper(lambda con, b: con.test_mail(b)),
    "/api/test/rule": oper(lambda con, b: con.test_rule(b)),
    "/api/test/alert": oper(lambda con, b: con.inject(b.get("kind", ""))),
    # 사건·분석
    "/api/job": _job_post,
    "/api/incident/state": lambda con, b: con.set_incident_state(
        str(b.get("id") or ""), bool(b.get("replay")), str(b.get("state") or ""), str(b.get("cause") or ""),
        str(b.get("action") or ""), str(b.get("note") or ""), b.get("save_case") is True),
    "/api/ask/incident": lambda con, b: con.ask_incident(b.get("id", ""), bool(b.get("replay")), b.get("q", "")),
    "/api/ask/report": lambda con, b: con.ask_report(b.get("tag", ""), b.get("q", "")),
    # Claude (개발자 모드)
    "/api/claude/test": dev(lambda con, b: con.claude_test()),
    "/api/claude/ask": dev(_claude_ask),
    "/api/claude/ask/cancel": dev(lambda con, b: con.claude_ask_cancel()),
    "/api/claude/prompt": dev(lambda con, b: con.claude_prompt(b.get("name", ""), b.get("args") or {})),
    "/api/claude/thread/new": dev(lambda con, b: con.claude_new_thread()),
    "/api/claude/log": dev(lambda con, b: con.claude_select_log(b.get("path", ""))),
    "/api/claude/workspace": dev(lambda con, b: con.claude_workspace(b.get("dir", ""))),
    "/api/claude/open": dev(lambda con, b: con.open_folder(b.get("dir", ""))),
}


def page_html(con: Console) -> str:
    """콘솔 페이지 — talog/web 소스를 합친 파일 하나(외부 리소스 없음). 세션 토큰을 심는다."""
    return build_page().replace("__TALOG_TOKEN__", con.token)


def _handler_factory(con: Console):
    class H(BaseHTTPRequestHandler):
        server_version = "talog-console"

        def log_message(self, fmt, *args):     # 접근 로그는 남기지 않는다
            pass

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _send(self, code: int, body, ctype=JSON):
            data = body if isinstance(body, bytes) else (
                json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
                if not isinstance(body, str) else body.encode("utf-8"))
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _reply(self, r):
            if isinstance(r, tuple):
                return self._send(*r)
            return self._send(200, r)

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {"error": "host"})
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/":
                    return self._send(200, page_html(con), HTML)
                if u.path.startswith("/r/"):          # 리포트 (뷰어 API 포함)
                    tag, _, sub = unquote(u.path[3:]).partition("/")
                    if sub in ("", "index.html"):
                        return self._send(200, con.report_page(tag), HTML)
                    if sub in ("api/insp", "api/detail", "api/meta"):
                        return self._send(200, con.report_api(tag, sub[4:], q))
                    return self._send(404, {"error": "not found"})
                if u.path in ("/favicon.ico", "/favicon.svg"):
                    name = "talog.ico" if u.path.endswith(".ico") else "talog_icon.svg"
                    with open(os.path.join(ASSETS, name), "rb") as f:
                        return self._send(200, f.read(), "image/x-icon" if name.endswith(".ico") else "image/svg+xml")
                route = GET_ROUTES.get(u.path)
                if route is not None:
                    return self._reply(route(con, q))
            except FileNotFoundError as e:
                return self._send(404, {"error": f"없음: {e}"})
            except Exception as e:
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._host_ok() or self.headers.get("X-Talog-Token") != con.token:
                return self._send(403, {"error": "토큰 불일치 — 콘솔 페이지를 새로고침하십시오"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
            except (ValueError, UnicodeError):
                return self._send(400, {"error": "JSON 형식 오류"})
            if not isinstance(body, dict):
                return self._send(400, {"error": "JSON 객체가 아닙니다"})
            route = POST_ROUTES.get(urlparse(self.path).path)
            if route is None:
                return self._send(404, {"error": "not found"})
            try:
                return self._reply(route(con, body))
            except FileNotFoundError as e:
                return self._send(404, {"ok": False, "error": f"없음: {e}"})
            except Exception as e:
                return self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

    return H


def serve(config_path: str, port: int = 8778, open_browser: bool = True, autostart: bool = True,
          mode: str = "") -> int:
    """콘솔을 띄우고 감시를 시작한다(autostart=False 면 '감시 시작'을 누를 때까지 멈춤). 모드와 상관없이 같다 —
    모드는 화면에서 바뀌므로, 현장 PC 를 개발자 모드로 둔 채 다시 켜도 감시가 멈춘 채 남지 않게."""
    con = Console(config_path, port, mode=mode)
    sys.stdout = _Tee(sys.stdout, con.log)        # 콘솔 창 + 화면의 실행 로그 (분석 작업은 별도 프로세스)
    srv = ThreadingHTTPServer(("127.0.0.1", port), _handler_factory(con))
    url = f"http://127.0.0.1:{port}/"
    print(f"[talog] AI 비전 로그 운영 플랫폼 {__version__} — {url}")
    print(f"        설정 {con.config_path}"
          + (" (옛 watch.yaml 형식 — 콘솔에서 저장하면 새 형식으로 바뀝니다)" if con.legacy else "")
          + " · 이 창을 닫으면 감시도 멈춥니다")
    print(f"        {con.access_info()['mode_label']} — 모드는 콘솔 왼쪽 위에서 바꿉니다 (위로 올릴 때 PIN)")
    if autostart:
        con.start_watch()
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        con.stop_watch()
        srv.server_close()
    return 0
