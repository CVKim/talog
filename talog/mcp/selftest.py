"""설치 점검 — 실제 클라이언트처럼 초기화·목록·도구 호출을 해 보고 결과를 구조화한다.
콘솔 '연결 시험'은 `talog mcp --self-test --json` 의 JSON 을 체크리스트·표로 그린다."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from .. import __version__, logread
from .prompts import PROMPTS
from .registry import TOOLS

if TYPE_CHECKING:
    from .server import TalogMCP


def self_test_report(server: TalogMCP) -> dict:
    """설치 점검 — 실제 클라이언트처럼 초기화·목록·도구 호출을 해 보고 결과를 구조화해 돌려준다.
    콘솔 '연결 시험'은 이 JSON 을 체크리스트·표로 그린다 (마크다운 원문을 그대로 보이지 않게)."""
    seq = [0]

    def call(method, params=None):
        seq[0] += 1
        t0 = time.perf_counter()
        r = server.handle({"jsonrpc": "2.0", "id": seq[0], "method": method, "params": params or {}}) or {}
        return r, round((time.perf_counter() - t0) * 1000)

    checks = []

    def check(key, title, ok, detail, ms=None):
        checks.append({"key": key, "title": title, "ok": bool(ok), "detail": detail, "ms": ms})

    r, ms = call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "talog-self-test", "version": __version__}})
    info = r.get("result") or {}
    si = info.get("serverInfo") or {}
    check("server", "서버 실행", bool(info), f"{si.get('name', '?')} {si.get('version', '')} · MCP {info.get('protocolVersion', '?')}", ms)
    r, _ = call("tools/list")
    tools = (r.get("result") or {}).get("tools") or []
    r, _ = call("prompts/list")
    prompts = (r.get("result") or {}).get("prompts") or []
    check("tools", "도구·프롬프트", len(tools) == len(TOOLS) and len(prompts) == len(PROMPTS),
          f"도구 {len(tools)}개 · 프롬프트 {len(prompts)}개")
    srcs = []
    extra = server.extra_log_roots()
    for s in server.sources:
        incs = s.incidents()
        st, _p = s.capacity_state()
        days = logread.day_dirs([r for r in [s.log_root] + extra if r], 50)
        srcs.append({"name": s.name, "kind": s.kind, "incidents": len(incs),
                     "crit": sum(1 for i in incs if (i.get("verdict") or {}).get("severity") == "crit"),
                     "reports": len(s.reports()), "baseline_groups": len((st or {}).get("groups") or {}),
                     "logs": bool(days), "log_days": len(days)})
    # 원본 로그 폴더만 있어도 Claude 가 읽을 것이 있다 (막 설치한 개발자 PC — 사건·리포트는 분석 뒤에 생긴다)
    has_data = any(x["incidents"] or x["reports"] or x["log_days"] for x in srcs)
    detail = " · ".join(f"{x['name']} — 사건 {x['incidents']}(심각 {x['crit']}) · 리포트 {x['reports']} · 원본 로그 폴더 "
                        f"{x['log_days']}개 · 기준선 {x['baseline_groups']}그룹" for x in srcs) or "출처 없음"
    if srcs and not has_data:
        detail += " — 볼 데이터가 아직 없습니다. 설정의 로그 루트를 로그 폴더로 맞추거나 분석을 한 번 돌리십시오"
    check("data", "데이터 출처", bool(srcs) and has_data, detail)
    check("mask", "마스킹", True, ("검사 id → ID-xxxxxx · 경로 → <로그>·<데이터> · 원본 로그 줄도 가려서 보냄"
                                   if server.mask.on else "꺼짐 — 원본 값이 그대로 Claude 에 갑니다"))
    calls = []
    ok_calls = True
    for name, args in (("overview", {}), ("list_incidents", {"limit": 5})):
        r, ms = call("tools/call", {"name": name, "arguments": args})
        res = r.get("result") or {}
        bad = bool(res.get("isError")) or "error" in r
        ok_calls &= not bad
        calls.append(f"{name} {ms}ms" + (" 실패" if bad else ""))
    check("calls", "도구 호출", ok_calls, " · ".join(calls))
    rows = []
    for s in server.sources:
        for inc in s.incidents():
            v = inc.get("verdict") or {}
            al = inc.get("alerts") or [{}]
            iid = inc.get("id") or ""
            rows.append({"id": iid, "day": f"{iid[4:6]}/{iid[6:8]}", "time": inc.get("opened", ""),
                         "severity": v.get("severity", ""), "title": server.mask.text(al[0].get("title", "")),
                         "more": max(0, len(al) - 1), "cause": (v.get("cause_name") or "").split(" — ")[0],
                         "verdict": v.get("label", ""), "gate": v.get("mode", ""), "source": s.name,
                         "replay": bool(inc.get("_replay"))})
    rows.sort(key=lambda x: x["id"], reverse=True)
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "sources": srcs, "incidents": rows[:8],
            "n_incidents": len(rows), "tools": [t["name"] for t in tools], "prompts": [p["name"] for p in prompts],
            "work_dir": server.work_dir, "masked": server.mask.on}


def self_test(server: TalogMCP, as_json: bool = False) -> int:
    rep = self_test_report(server)
    if as_json:
        print(json.dumps(rep, ensure_ascii=False))
        return 0 if rep["ok"] else 1
    for c in rep["checks"]:
        print(f"[talog mcp] {'✓' if c['ok'] else '✗'} {c['title']}: {c['detail']}")
    print(f"[talog mcp] 도구: {', '.join(rep['tools'])}")
    print(f"[talog mcp] 프롬프트: {', '.join(rep['prompts'])} · 작업 폴더 {rep['work_dir']}")
    for x in rep["incidents"][:5]:
        print(f"    {x['day']} {x['time']}  {x['severity']:<4} {x['title']}  →  {x['cause'] or '-'} · {x['verdict'] or '-'}")
    print(f"[talog mcp] 자가 점검 {'통과' if rep['ok'] else '실패'}")
    return 0 if rep["ok"] else 1
