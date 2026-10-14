"""진단 리포트 도구 — list_reports · get_report · query_report(읽기 전용 SQL).

리포트 SQLite 는 mode=ro·query_only 로 열고, SELECT/WITH 한 문장·행 수·20초 제한을 둔다."""

from __future__ import annotations

import os
import re
import sqlite3
import time
from urllib.request import pathname2url
from typing import TYPE_CHECKING

from ..fmt import cut, hm, num, table
from ..registry import READ_ONLY, SOURCE, schema, tool
from ..sources import Source

TAG_RE = re.compile(r"^[\w\-. ]+$")
SQL_TIMEOUT_S = 20.0

if TYPE_CHECKING:
    from ..server import TalogMCP


def find_report(srv: TalogMCP, tag: str, source: str = "") -> tuple[Source, dict]:
    if not TAG_RE.match(tag or ""):
        raise ValueError("tag 형식이 올바르지 않습니다")
    for s in srv.pick(source):
        for r in s.reports():
            if r["tag"] == tag:
                return s, r
    raise ValueError(f"리포트를 찾지 못했습니다: {tag} — list_reports 로 확인하십시오")


def open_ro(db: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{pathname2url(os.path.abspath(db))}?mode=ro", uri=True, timeout=5)
    con.execute("PRAGMA query_only=ON")
    return con


@tool({"name": "list_reports", "title": "진단 리포트 목록",
       "description": "talog analyze(콘솔 '진단 리포트 만들기')가 만든 일자별 리포트와 자동 진단 소견 제목.",
       "inputSchema": schema({"source": SOURCE}), "annotations": READ_ONLY})
def list_reports(srv: TalogMCP, a: dict) -> str:
    rows = []
    for s in srv.pick(a.get("source", "")):
        for r in s.reports():
            rows.append([r["tag"], s.name, hm(r["mtime"]), "있음" if r["db"] else "-",
                         " / ".join(r["findings"][:4])])
    return srv.mask.text("# 진단 리포트\n" + table(["tag", "출처", "만든 때", "DB", "소견"], rows, 120)
                          + "\n\n다음: get_report(tag=…) → query_report(tag=…, sql=…)")


@tool({"name": "get_report", "title": "진단 리포트 소견",
       "description": "리포트 하나의 자동 진단 소견(심각·주의·정상과 근거·권고)과 리포트 DB 의 표·행 수·열 이름"
                      "(query_report 로 SQL 을 쓰기 전에 본다).",
       "inputSchema": schema({"tag": {"type": "string", "description": "리포트 태그 (list_reports 의 tag)"},
                               "source": SOURCE}, ["tag"]),
       "annotations": READ_ONLY})
def get_report(srv: TalogMCP, a: dict) -> str:
    s, r = find_report(srv, a.get("tag", ""), a.get("source", ""))
    with open(r["diag"], encoding="utf-8") as f:
        diag = f.read()
    out = [f"# 진단 리포트 {r['tag']} — {s.name}", "", diag.strip()]
    if r["db"]:
        con = open_ro(r["db"])
        try:
            meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
            tabs = [x[0] for x in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            info = []
            for t in tabs:
                cols = [c[1] for c in con.execute(f"PRAGMA table_info({t})")]
                n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                info.append([t, f"{n:,}", ", ".join(cols)])
        finally:
            con.close()
        out += ["", "## 리포트 DB", f"- 원본 폴더: {meta.get('day_dir', '-')} · talog {meta.get('version', '-')}",
                table(["표", "행", "열"], info, 400),
                "- *_ts 는 epoch 초, 사람이 읽는 시각은 *_text 열. inspections.status: complete/rejected/incomplete_lost/…",
                "- events.kind 예: INSP_START(value=유휴 스레드), INSP_REJECT, COMM_MSG(name=V2M_INSPECT_END 등, value=그룹), "
                "SEQ_FINISH(value=점유 ms), USAGE(value=메모리 MB), POOL_CREATE(재시작)"]
    return cut(srv.mask.text("\n".join(out)))


@tool({"name": "query_report", "title": "리포트 DB 조회 (SQL)",
       "description": "리포트 SQLite 를 읽기 전용으로 조회한다. SELECT/WITH 한 문장만, 행 수 제한, 20초 제한. "
                      "표: inspections(검사 1건=1행: start_text, status, duration_s, wait_threads, end_result …), "
                      "events(파싱 이벤트: ts_text, kind, inner_id, value, status, name, extra …), channel_runs, "
                      "process_gens, model_loads, files.",
       "inputSchema": schema({"tag": {"type": "string"}, "sql": {"type": "string", "description": "SELECT … (LIMIT 이 없으면 붙인다)"},
                               "max_rows": {"type": "integer", "minimum": 1, "maximum": 500, "description": "기본 50"},
                               "source": SOURCE}, ["tag", "sql"]),
       "annotations": READ_ONLY})
def query_report(srv: TalogMCP, a: dict) -> str:
    s, r = find_report(srv, a.get("tag", ""), a.get("source", ""))
    if not r["db"]:
        raise ValueError("이 리포트에는 DB 가 없습니다")
    q = (a.get("sql") or "").strip().rstrip(";").strip()
    q_nc = re.sub(r"--[^\n]*|/\*.*?\*/", " ", q, flags=re.S).strip()
    if not re.match(r"^(select|with)\b", q_nc, re.I):
        raise ValueError("SELECT(또는 WITH … SELECT) 한 문장만 허용됩니다")
    if ";" in q_nc:
        raise ValueError("한 문장만 허용됩니다 (세미콜론 금지)")
    # DB 는 읽기 전용(mode=ro·query_only)으로 열리지만, 파일을 붙이거나 설정을 바꾸는 문은 미리 막는다
    if re.search(r"\b(attach|detach|pragma|insert|update|delete|create|drop|alter|vacuum|reindex)\b", q_nc, re.I):
        raise ValueError("읽기 전용 조회만 허용됩니다")
    max_rows = max(1, min(500, int(a.get("max_rows") or 50)))
    con = open_ro(r["db"])
    deadline = time.time() + SQL_TIMEOUT_S
    con.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 20000)
    try:
        cur = con.execute(q)
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(max_rows + 1)
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e):
            raise ValueError(f"조회가 {SQL_TIMEOUT_S:.0f}초를 넘었습니다 — 조건·인덱스 열(kind, inner_id, ts)로 좁히십시오")
        raise ValueError(f"SQL 오류: {e}")
    finally:
        con.close()
    more = len(rows) > max_rows
    rows = rows[:max_rows]
    body = table(cols, [[srv.mask.text(x) if isinstance(x, str) else num(x, 4) for x in row] for row in rows], 100)
    return cut(f"# {r['tag']} 조회 결과 {len(rows)}행" + (" (더 있음 — max_rows 를 늘리거나 조건을 좁히십시오)" if more else "")
                + "\n```sql\n" + q + "\n```\n" + body)
