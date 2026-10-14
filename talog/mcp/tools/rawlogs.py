"""원본 로그 도구 — log_days · log_files · read_log · search_logs · trace_inspection.

읽기는 logread.py 가 한다(줄머리 시각 이분 탐색, 한 번 300줄·384MB·20초). 여기서는 인자를 풀고, 마스킹하고, 글로 만든다."""

from __future__ import annotations

import os
import re
import time
from typing import TYPE_CHECKING

from ... import logread
from ..fmt import cut, table
from ..registry import READ_ONLY, SOURCE, schema, tool

if TYPE_CHECKING:
    from ..server import TalogMCP


def strip_ts(line: str) -> str:
    return re.sub(r"^\d{4}/\d{2}/\d{2}-\d{2}:\d{2}:\d{2}\.\d{3}\t?", "", line)


@tool({"name": "log_days", "title": "원본 로그 폴더",
       "description": "원본 talos 로그를 직접 읽을 수 있는 일자 폴더(설정의 로그 루트, 콘솔에서 고른 폴더)와 날짜·파일 수·크기. "
                      "원본 로그를 보기 전에 먼저 부른다.",
       "inputSchema": schema({"source": SOURCE}), "annotations": READ_ONLY})
def log_days(srv: TalogMCP, a: dict) -> str:
    roots = srv.log_roots(a.get("source", ""))
    sel = srv.log_cfg().get("selected") or ""
    sel = os.path.normcase(os.path.abspath(sel)) if sel else "?"
    rows = []
    for d in logread.day_dirs(roots, 30):
        files = logread._files(d)
        size = sum(os.path.getsize(p) for p in files if os.path.exists(p))
        dd = logread.day_date(d)
        name = next((os.path.relpath(d, r) for r in roots if logread.inside(d, r)), d)
        picked = os.path.normcase(os.path.abspath(d)) == sel
        rows.append(["선택" if picked else (name if name != "." else d), dd.isoformat() if dd else "-", len(files),
                     f"{size / 2**30:.1f} GB", "콘솔에서 고른 폴더" if picked else ""])
    return srv.mask.text("# 원본 로그 일자 폴더\n" + table(["day (이 이름을 쓴다)", "날짜", "파일", "크기", "비고"], rows)
                          + "\n\n다음: log_files(day=…) 로 파일과 시각 범위를 본다. 하루가 수십 GB 이므로 search_logs 는 "
                            "time_from/time_to 로 범위를 좁힌다.")


@tool({"name": "log_files", "title": "원본 로그 파일 목록",
       "description": "일자 폴더의 로그 파일: 분류(핵심/대용량)·크기·첫/마지막 시각. 어느 파일을 볼지 고를 때 쓴다. "
                      "InspResult·alg·cam 같은 GB 단위 파일은 시각을 주고 읽는다.",
       "inputSchema": schema({"day": {"type": "string", "description": "일자 폴더 (log_days 의 이름, 예: 12, 2026_10\\12, 20261012). 비우면 가장 최근"},
                               "source": SOURCE}),
       "annotations": READ_ONLY})
def log_files(srv: TalogMCP, a: dict) -> str:
    d = srv.day_dir(a)
    rows = [[r["name"], "핵심" if r["core"] else "대용량" if r["size"] > logread.FULL_SCAN_MAX else "기타",
             f"{r['size'] / 2**20:,.1f} MB", logread.fmt_ts(r["first"], True), logread.fmt_ts(r["last"], True)]
            for r in logread.list_files(d)]
    return srv.mask.text(f"# 로그 파일 — {d} ({len(rows)}개)\n"
                          + table(["파일", "분류", "크기", "첫 줄", "마지막 줄"], rows, width=60)
                          + "\n\n160MB 넘는 파일은 search_logs 에 time_from/time_to 가 필요하다. read_log 는 크기와 상관없이 "
                            "at(시각)으로 바로 간다.")


@tool({"name": "read_log", "title": "원본 로그 읽기",
       "description": "파일 하나의 구간을 원문 그대로 읽는다(마스킹 적용). at(시각 HH:MM[:SS]) 앞뒤 · offset(앞 결과의 '다음 위치') · "
                      "tail(끝) 중 하나. 줄 앞 시각으로 이분 탐색해 4GB 파일도 그 시각으로 바로 간다. 한 번 최대 300줄.",
       "inputSchema": schema({"file": {"type": "string", "description": "파일 이름 (log_files 의 이름, 예: seq_10.log, alg\\alg_dl_2(2_SIDE1).log)"},
                               "day": {"type": "string", "description": "일자 폴더 (비우면 가장 최근)"},
                               "at": {"type": "string", "description": "이 시각 앞뒤를 읽는다 (예: 05:02:28)"},
                               "offset": {"type": "integer", "minimum": 0, "description": "앞 결과의 '다음 위치'부터 이어 읽기"},
                               "tail": {"type": "boolean", "description": "파일 끝을 읽기"},
                               "lines": {"type": "integer", "minimum": 1, "maximum": 300, "description": "읽을 줄 수 (기본 80)"},
                               "before": {"type": "integer", "minimum": 0, "maximum": 200, "description": "at 앞 줄 수 (기본 20)"},
                               "source": SOURCE}, ["file"]),
       "annotations": READ_ONLY})
def read_log(srv: TalogMCP, a: dict) -> str:
    d = srv.day_dir(a)
    files = logread.match_files(d, [a.get("file", "")])
    if not files:
        raise ValueError(f"파일이 없습니다: {a.get('file')} — log_files 로 이름을 확인하십시오")
    p = files[0]
    at = logread.parse_when(a["at"], logread.day_date(d)) if a.get("at") else None
    w = logread.read_window(p, at=at, offset=a.get("offset"), tail=bool(a.get("tail")),
                            lines=a.get("lines") or 80, before=a.get("before", 20))
    ts = [logread.ts_of(x.encode("utf-8", "replace")) for x in w["lines"]]
    ts = [t for t in ts if t is not None]
    head = (f"# {logread.rel(d, p)} — {logread.fmt_ts(ts[0]) if ts else '-'} ~ {logread.fmt_ts(ts[-1]) if ts else '-'}"
            f" · {len(w['lines'])}줄 · 위치 {w['start']:,} / {w['size']:,} 바이트")
    tail = ("\n(파일 끝)" if w["eof"] else
            f"\n이어 읽기: read_log(file='{logread.rel(d, p)}', offset={w['next']})")
    return cut(srv.mask.text(head + "\n```\n" + "\n".join(w["lines"]) + "\n```" + tail))


@tool({"name": "search_logs", "title": "원본 로그 찾기",
       "description": "일자 폴더의 로그에서 문구(대소문자 무시)나 정규식을 찾아 시간순으로 보여 준다. files 를 비우면 핵심 로그"
                      "(InspStarter·comm·seq_N·exception·WorkerThreadPoolMng 등). 160MB 넘는 파일은 time_from/time_to 를 줘야 찾는다. "
                      "ID-xxxxxx 토큰은 원래 검사 id 로 바꿔 찾는다. 한 번 최대 300건·20초.",
       "inputSchema": schema({"query": {"type": "string", "description": "찾을 문구 (예: NoInspThread, Waiting thread count = 0, ID-1a2b3c)"},
                               "day": {"type": "string", "description": "일자 폴더 (비우면 가장 최근)"},
                               "files": {"type": "array", "items": {"type": "string"}, "description": "파일 이름·와일드카드 (예: [\"seq_*.log\", \"alg\\\\*.log\"])"},
                               "regex": {"type": "boolean", "description": "query 를 정규식으로 (기본 false)"},
                               "time_from": {"type": "string", "description": "이 시각부터 (예: 04:55)"},
                               "time_to": {"type": "string", "description": "이 시각까지 (예: 05:05)"},
                               "max_hits": {"type": "integer", "minimum": 1, "maximum": 300, "description": "최대 건수 (기본 100)"},
                               "source": SOURCE}, ["query"]),
       "annotations": READ_ONLY})
def search_logs(srv: TalogMCP, a: dict) -> str:
    d = srv.day_dir(a)
    ref = logread.day_date(d)
    t_from = logread.parse_when(a["time_from"], ref) if a.get("time_from") else None
    t_to = logread.parse_when(a["time_to"], ref) if a.get("time_to") else None
    if t_to is not None and t_from is not None and t_to < t_from:
        raise ValueError("time_to 가 time_from 보다 앞입니다")
    files = logread.match_files(d, a.get("files") or list(logread.CORE_DEFAULT))
    if not files:
        raise ValueError("찾을 파일이 없습니다 — log_files 로 이름을 확인하십시오")
    query = srv.mask.unmask(a.get("query", ""))
    t0 = time.time()
    r = logread.search(files, query, regex=bool(a.get("regex")), t_from=t_from, t_to=t_to,
                       max_hits=a.get("max_hits") or 100, day_dir=d)
    lines = [f"{logread.fmt_ts(t)}  {name}  {strip_ts(text)}" for t, name, text in r["hits"]]
    rng = (f" · {a.get('time_from') or '처음'}~{a.get('time_to') or '끝'}" if (t_from or t_to) else "")
    head = (f"# 찾기 '{a.get('query', '')}' — {len(lines)}건{rng} · 파일 {len(files)}개 · "
            f"{r['scanned'] / 2**20:,.0f}MB 훑음 · {time.time() - t0:.1f}초"
            + (" · 한도에서 멈춤 (범위를 좁히거나 max_hits 를 늘리십시오)" if r["partial"] else ""))
    per = ", ".join(f"{k} {v}" for k, v in sorted(r["per_file"].items(), key=lambda kv: -kv[1]))
    body = ("\n파일별: " + per if per else "") + ("\n```\n" + "\n".join(lines) + "\n```" if lines else "\n(없음)")
    if r["skipped"]:
        body += "\n건너뛴 큰 파일: " + ", ".join(r["skipped"])
    return cut(srv.mask.text(head + body))


@tool({"name": "trace_inspection", "title": "검사 하나 추적",
       "description": "검사 id(원래 값 또는 ID-xxxxxx) 하나를 InspStarter·comm·seq_N·alg 로그에서 시간순으로 모은다 — 도착·유휴 스레드·"
                      "Seq 시작·알고리즘·판정·결과 송신·거부까지. 시작 시각은 id 안의 시각이나 첫 등장에서 찾는다.",
       "inputSchema": schema({"inspection_id": {"type": "string", "description": "검사(inner) id 또는 ID-xxxxxx"},
                               "day": {"type": "string", "description": "일자 폴더 (비우면 가장 최근)"},
                               "at": {"type": "string", "description": "id 에서 시각을 못 찾을 때 시작 시각 (예: 05:02:40)"},
                               "window_s": {"type": "integer", "minimum": 10, "maximum": 1800, "description": "시작 뒤 몇 초까지 (기본 180)"},
                               "source": SOURCE}, ["inspection_id"]),
       "annotations": READ_ONLY})
def trace_inspection(srv: TalogMCP, a: dict) -> str:
    d = srv.day_dir(a)
    raw = srv.mask.unmask((a.get("inspection_id") or "").strip())
    if raw.startswith("ID-"):
        raise ValueError(f"{raw} 의 원래 값을 모릅니다 — 그 id 가 나온 도구(get_incident·search_logs)를 이 대화에서 먼저 부르십시오")
    hint = logread.parse_when(a["at"], logread.day_date(d)) if a.get("at") else None
    r = logread.trace(d, raw, a.get("window_s") or 180, hint=hint)
    lines = [f"{logread.fmt_ts(t)}  {name}  {strip_ts(text)}" for t, name, text in r["hits"]]
    per = ", ".join(f"{k} {v}" for k, v in sorted(r["per_file"].items(), key=lambda kv: -kv[1]))
    head = (f"# 검사 {raw} 추적 — {logread.fmt_ts(r['t0'])} 부터 {r['window_s']}초 · {len(lines)}줄 "
            f"(시작 시각: {r['found_by'] or 'at'})" + (" · 한도에서 멈춤" if r["partial"] else ""))
    return cut(srv.mask.text(head + ("\n파일별: " + per if per else "")
                               + ("\n```\n" + "\n".join(lines) + "\n```" if lines else "\n(이 범위에 없음 — at 이나 window_s 를 바꿔 보십시오)")))
