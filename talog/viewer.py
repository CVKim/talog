"""talog view — SQLite 를 직접 서빙하는 로컬 뷰어 서버.

정적 HTML 은 공유·보고용(전 건 내장, 상한 6만)으로 유지하고, 이 모드는
방대한 로그(수십만~수백만 건)를 페이지 단위 쿼리로 커버한다:

    talog view <30.sqlite | talog_out 폴더 | 일자 폴더>

- /            : 동봉 리포트 HTML (리포트 JS 가 http 접속을 감지하면
                 검사 조회·간트 상세를 서버 API 로 전환한다)
- /api/insp    : 검사 페이지 쿼리 (filter/q/sort/offset/limit)
- /api/detail  : 특정 inner 의 채널 런 상세 (간트/그래프용)
- /api/meta    : 전체 건수/상태 분포
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_BAD = ("rejected", "incomplete_lost", "incomplete", "unknown")


def resolve_paths(target: str) -> tuple[str, str]:
    """대상 경로에서 (sqlite, html) 짝을 찾는다."""
    target = os.path.abspath(target)
    if target.lower().endswith(".sqlite"):
        db = target
        html = os.path.splitext(target)[0] + ".html"
    elif os.path.isdir(target):
        cand = target
        if not any(f.endswith(".sqlite") for f in os.listdir(cand)):
            sub = os.path.join(cand, "talog_out")
            if os.path.isdir(sub):
                cand = sub
        dbs = sorted(f for f in os.listdir(cand) if f.endswith(".sqlite"))
        if not dbs:
            raise FileNotFoundError(f".sqlite 를 찾을 수 없습니다: {target}")
        db = os.path.join(cand, dbs[0])
        html = os.path.splitext(db)[0] + ".html"
    else:
        raise FileNotFoundError(target)
    if not os.path.exists(html):
        raise FileNotFoundError(f"리포트 HTML 이 없습니다: {html} — "
                                f"먼저 talog 로 리포트를 생성하십시오.")
    return db, html


def _open_ro(db: str) -> sqlite3.Connection:
    uri = "file:" + urllib.parse.quote(db.replace("\\", "/")) + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, check_same_thread=False)
    return con


def query_insp(con: sqlite3.Connection, flt: str = "all", q: str = "",
               sort: str = "desc", offset: int = 0,
               limit: int = 400) -> dict:
    """검사 페이지 쿼리 — 리포트 JS 의 SUMMARY 행 포맷으로 반환한다."""
    where, args = [], []
    if flt == "bad":
        where.append("status IN (%s)" % ",".join("?" * len(_BAD)))
        args += list(_BAD)
    elif flt == "ng":
        where.append("end_result = 'NG'")
    elif flt == "ok":
        where.append("status = 'complete'")
    elif flt == "eof":
        where.append("status = 'in_progress_eof'")
    else:
        where.append("status != 'in_progress_eof'")
    if q:
        where.append("(inner_id LIKE ? OR product_id LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    w = ("WHERE " + " AND ".join(where)) if where else ""
    total = con.execute(
        f"SELECT COUNT(*) FROM inspections {w}", args).fetchone()[0]
    order = "DESC" if sort == "desc" else "ASC"
    rows = con.execute(
        f"SELECT inner_id, product_id, start_ts, substr(start_text,1,12), "
        f"status, ROUND(duration_s,2), n_done, n_fed, ack_status, end_result "
        f"FROM inspections {w} ORDER BY start_ts {order} "
        f"LIMIT ? OFFSET ?", args + [int(limit), int(offset)]).fetchall()
    return {"total": total, "offset": offset, "rows": [list(r) for r in rows]}


def query_detail(con: sqlite3.Connection, inner: str) -> dict | None:
    """특정 검사의 채널 런 상세 — 리포트 JS 의 DETAIL 포맷."""
    it = con.execute(
        "SELECT substr(start_text,1,12), status, remain_list, "
        "lost_channels, nofeed_channels, start_ts "
        "FROM inspections WHERE inner_id = ?", (inner,)).fetchone()
    if it is None:
        return None
    st_text, status, remain, lost_s, nofeed_s, start_ts = it
    runs = con.execute(
        "SELECT alg_idx, channel, exec_no, feed_ts, infer_start_ts, "
        "infer_ms, status, model, pre_ms FROM channel_runs "
        "WHERE inner_id = ? ORDER BY infer_start_ts, alg_idx",
        (inner,)).fetchall()
    base = start_ts or (runs[0][4] if runs else 0) or 0

    def _idx_list(s: str) -> list[int]:
        out = []
        for tok in (s or "").split(","):
            head = tok.strip().split("(")[0]
            if head.isdigit():
                out.append(int(head))
        return out

    return {
        "st": st_text, "status": status, "remain": remain or "",
        "lost": [t for t in (lost_s or "").split(",") if t],
        "nofeed": [t for t in (nofeed_s or "").split(",") if t][:30],
        "lostIdx": _idx_list(lost_s), "nofeedIdx": _idx_list(nofeed_s),
        "skipIdx": [],
        "runs": [[a, (ch or "")[:28], ex,
                  round((ist or fts or base) - base, 2),
                  round((ims or 0) / 1000, 2), stt, (mdl or "")[:40],
                  round(pre or 0, 1)]
                 for a, ch, ex, fts, ist, ims, stt, mdl, pre in runs],
    }


def query_meta(con: sqlite3.Connection) -> dict:
    st = dict(con.execute(
        "SELECT status, COUNT(*) FROM inspections GROUP BY status"))
    return {"total": sum(st.values()), "status": st}


def serve(target: str, port: int = 8777, open_browser: bool = True) -> None:
    db, html = resolve_paths(target)
    con = _open_ro(db)
    lock = threading.Lock()
    with open(html, "rb") as f:
        page = f.read()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):        # 콘솔 소음 억제
            pass

        def _json(self, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            try:
                u = urllib.parse.urlparse(self.path)
                qs = dict(urllib.parse.parse_qsl(u.query))
                if u.path == "/":
                    self.send_response(200)
                    self.send_header("Content-Type",
                                     "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(page)))
                    self.end_headers()
                    self.wfile.write(page)
                elif u.path == "/api/insp":
                    with lock:
                        self._json(query_insp(
                            con, qs.get("filter", "all"), qs.get("q", ""),
                            qs.get("sort", "desc"),
                            int(qs.get("offset", 0) or 0),
                            min(1000, int(qs.get("limit", 400) or 400))))
                elif u.path == "/api/detail":
                    with lock:
                        d = query_detail(con, qs.get("inner", ""))
                    self._json(d if d is not None else {"error": "not found"})
                elif u.path == "/api/meta":
                    with lock:
                        self._json(query_meta(con))
                else:
                    self.send_error(404)
            except (ConnectionError, BrokenPipeError):
                pass
            except Exception as e:          # 뷰어는 어떤 요청에도 죽지 않는다
                try:
                    self._json({"error": str(e)})
                except Exception:
                    pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    url = f"http://127.0.0.1:{port}/"
    print(f"[talog view] {os.path.basename(db)} — {url} (Ctrl+C 종료)")
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[talog view] 종료")
    finally:
        srv.server_close()
        con.close()
