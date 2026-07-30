"""talog ask — 로그 DB 대화형 질의 (로컬 Ollama / Claude API 겸용 tool-use 루프).

사용:
    python -m talog ask <out 폴더 | .sqlite> [--backend auto|ollama|claude]
                        [--model 이름] [--q "단발 질문"]

도구:
    sql(query)                — talog SQLite 조회 (SELECT 전용, LIMIT 강제)
    log_lines(file_id, line)  — 원본 로그 파일의 해당 라인 주변 원문
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import sys
import urllib.request

from .guide import AI_GUIDE

_OLLAMA = "http://localhost:11434"
_MAX_ROUNDS = 8
_SQL_CHAR_LIMIT = 3800

_TOOLS_SPEC = [
    {
        "name": "sql",
        "description": "talog SQLite 를 조회한다. SELECT 문만 허용된다.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "SELECT 문"}},
            "required": ["query"],
        },
    },
    {
        "name": "log_lines",
        "description": "원본 로그 파일의 특정 라인 주변 원문을 본다. "
                       "events.file_id 와 events.line_no 를 사용한다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "integer"},
                "line_no": {"type": "integer"},
                "around": {"type": "integer", "description": "앞뒤 라인 수(기본 8)"},
            },
            "required": ["file_id", "line_no"],
        },
    },
]


# ---------------------------------------------------------------------------
class ToolBox:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    def sql(self, query: str) -> str:
        q = (query or "").strip().rstrip(";")
        if not re.match(r"^\s*(select|with)\b", q, re.I):
            return "오류: SELECT 문만 허용됩니다."
        if not re.search(r"\blimit\b", q, re.I):
            q += " LIMIT 200"
        try:
            cur = self.con.execute(q)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        except sqlite3.Error as e:
            return f"SQL 오류: {e}"
        out = [" | ".join(cols)]
        for r in rows:
            out.append(" | ".join(str(v) for v in r))
            if sum(len(x) for x in out) > _SQL_CHAR_LIMIT:
                out.append(f"... (표시 잘림, 총 {len(rows)}행)")
                break
        return "\n".join(out) if rows else "(0행)"

    def log_lines(self, file_id: int, line_no: int, around: int = 8) -> str:
        row = self.con.execute("SELECT path FROM files WHERE id=?",
                               (file_id,)).fetchone()
        if not row or not os.path.exists(row[0]):
            return "오류: 파일을 찾을 수 없습니다."
        lo, hi = max(1, line_no - around), line_no + around
        out = []
        try:
            with open(row[0], "r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    if i > hi:
                        break
                    if i >= lo:
                        mark = ">>" if i == line_no else "  "
                        out.append(f"{mark}{i}: {line.rstrip()}"[:300])
        except OSError as e:
            return f"파일 읽기 오류: {e}"
        return "\n".join(out) or "(빈 범위)"

    def run(self, name: str, args: dict) -> str:
        if name == "sql":
            return self.sql(args.get("query", ""))
        if name == "log_lines":
            return self.log_lines(int(args.get("file_id", 0)),
                                  int(args.get("line_no", 0)),
                                  int(args.get("around", 8) or 8))
        return f"알 수 없는 도구: {name}"


# ---------------------------------------------------------------------------
_SQL_BLOCK_RE = re.compile(r"```sql\s*(.+?)```", re.S | re.I)
_BARE_SQL_RE = re.compile(r"^\s*(SELECT|WITH)\b.+", re.S | re.I)


def _extract_sql(text: str) -> str:
    """응답 텍스트에서 실행 가능한 SELECT 문을 추출한다 (소형 모델 폴백)."""
    m = _SQL_BLOCK_RE.search(text or "")
    if m:
        return m.group(1).strip()
    m = _BARE_SQL_RE.match(text or "")
    return m.group(0).strip() if m else ""


def _http_json(url: str, payload: dict, headers: dict, timeout: int = 600) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class OllamaBackend:
    """Ollama /api/chat (tool calling 지원 모델: qwen2.5 등)."""

    def __init__(self, model: str):
        self.model = model

    @staticmethod
    def alive() -> bool:
        try:
            with urllib.request.urlopen(f"{_OLLAMA}/api/tags", timeout=3):
                return True
        except OSError:
            return False

    def chat(self, system: str, question: str, tools: ToolBox,
             verbose: bool = True) -> str:
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": question}]
        ollama_tools = [{"type": "function",
                         "function": {"name": t["name"],
                                      "description": t["description"],
                                      "parameters": t["input_schema"]}}
                        for t in _TOOLS_SPEC]
        for rnd in range(_MAX_ROUNDS):
            r = _http_json(f"{_OLLAMA}/api/chat",
                           {"model": self.model, "messages": msgs,
                            "tools": ollama_tools, "stream": False,
                            "options": {"temperature": 0.1, "num_ctx": 16384}}, {})
            msg = r.get("message", {})
            calls = msg.get("tool_calls") or []
            if not calls:
                content = msg.get("content", "")
                # 소형 모델 폴백: 도구 호출 대신 SQL 을 텍스트로 내놓으면
                # 추출해 실행하고 결과를 되먹인다
                sql = _extract_sql(content)
                if sql and rnd < _MAX_ROUNDS - 2:
                    if verbose:
                        print(f"   [자동실행] {sql[:120]}")
                    result = tools.run("sql", {"query": sql})
                    msgs.append({"role": "assistant", "content": content})
                    msgs.append({"role": "user",
                                 "content": f"[sql 실행 결과]\n{result}\n\n"
                                            f"이 결과를 바탕으로 계속 분석하거나, "
                                            f"충분하면 최종 답을 한국어로 작성하라."})
                    continue
                return content or "(응답 없음)"
            msgs.append(msg)
            for c in calls:
                fn = c.get("function", {})
                name = fn.get("name", "")
                args = fn.get("arguments", {}) or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                if verbose:
                    print(f"   [도구] {name} {json.dumps(args, ensure_ascii=False)[:140]}")
                result = tools.run(name, args)
                msgs.append({"role": "tool", "content": result, "name": name})
        return "(도구 호출 한도 초과 — 질문을 더 좁혀 주십시오)"


class ClaudeBackend:
    """Anthropic Messages API (도구 사용). ANTHROPIC_API_KEY 필요."""

    def __init__(self, model: str):
        self.model = model or "claude-sonnet-5"
        self.key = os.environ.get("ANTHROPIC_API_KEY", "")

    def chat(self, system: str, question: str, tools: ToolBox,
             verbose: bool = True) -> str:
        headers = {"x-api-key": self.key, "anthropic-version": "2023-06-01"}
        msgs = [{"role": "user", "content": question}]
        for _ in range(_MAX_ROUNDS):
            r = _http_json("https://api.anthropic.com/v1/messages",
                           {"model": self.model, "max_tokens": 2000,
                            "system": system, "messages": msgs,
                            "tools": _TOOLS_SPEC}, headers)
            content = r.get("content", [])
            uses = [b for b in content if b.get("type") == "tool_use"]
            if not uses:
                return "".join(b.get("text", "") for b in content) or "(응답 없음)"
            msgs.append({"role": "assistant", "content": content})
            results = []
            for u in uses:
                if verbose:
                    print(f"   [도구] {u['name']} "
                          f"{json.dumps(u.get('input', {}), ensure_ascii=False)[:140]}")
                results.append({"type": "tool_result", "tool_use_id": u["id"],
                                "content": tools.run(u["name"], u.get("input", {}))})
            msgs.append({"role": "user", "content": results})
        return "(도구 호출 한도 초과 — 질문을 더 좁혀 주십시오)"


# ---------------------------------------------------------------------------
def _pick_db(target: str) -> str:
    if target.lower().endswith(".sqlite"):
        return target
    dbs = sorted(glob.glob(os.path.join(target, "*.sqlite")))
    if not dbs:
        raise FileNotFoundError(f"{target} 에 .sqlite 가 없습니다. 먼저 talog 로 "
                                f"로그를 분석하십시오.")
    if len(dbs) == 1:
        return dbs[0]
    print("분석 대상 DB 를 선택하십시오:")
    for i, d in enumerate(dbs, 1):
        print(f"  {i}. {os.path.basename(d)}")
    sel = input("번호: ").strip()
    return dbs[int(sel) - 1] if sel.isdigit() and 1 <= int(sel) <= len(dbs) else dbs[-1]


def _build_system(db_path: str) -> str:
    parts = [AI_GUIDE]
    diag = os.path.splitext(db_path)[0] + "_diagnosis.md"
    if os.path.exists(diag):
        with open(diag, "r", encoding="utf-8") as f:
            parts.append("\n## 사전 자동 진단 소견 (룰 엔진 결과 — 출발점으로 활용)\n"
                         + f.read()[:4000])
    return "\n".join(parts)


def pick_backend(name: str, model: str):
    if name == "claude" or (name == "auto" and os.environ.get("ANTHROPIC_API_KEY")):
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("ANTHROPIC_API_KEY 가 없습니다."); sys.exit(2)
        return ClaudeBackend(model), f"Claude ({model or 'claude-sonnet-5'})"
    if name in ("ollama", "auto"):
        if OllamaBackend.alive():
            m = model or "qwen2.5:7b"
            return OllamaBackend(m), f"Ollama ({m}, 로컬 GPU/CPU)"
        if name == "ollama":
            print("Ollama 서버가 실행 중이 아닙니다. `ollama serve` 후 다시 "
                  "시도하십시오."); sys.exit(2)
    print("사용 가능한 LLM 백엔드가 없습니다.\n"
          " - 로컬: https://ollama.com 설치 후 `ollama pull qwen2.5:7b`\n"
          " - API : 환경변수 ANTHROPIC_API_KEY 설정\n"
          "LLM 없이도 리포트의 '자동 진단 소견'과 SQLite 직접 조회는 가능합니다.")
    sys.exit(2)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="talog ask",
                                 description="talog DB 대화형 질의 (로컬 LLM/Claude)")
    ap.add_argument("target", help="out 폴더 또는 .sqlite 파일")
    ap.add_argument("--backend", default="auto", choices=["auto", "ollama", "claude"])
    ap.add_argument("--model", default="", help="모델 이름 (기본: qwen2.5:7b / claude-sonnet-5)")
    ap.add_argument("--q", default="", help="단발 질문 (REPL 대신 1회 실행)")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    db = _pick_db(args.target)
    tools = ToolBox(db)
    system = _build_system(db)
    backend, label = pick_backend(args.backend, args.model)
    print(f"[talog ask] DB: {os.path.basename(db)} / 백엔드: {label}")

    def one(q: str):
        try:
            ans = backend.chat(system, q, tools)
        except OSError as e:
            ans = f"백엔드 통신 오류: {e}"
        print("\n" + ans + "\n")

    if args.q:
        one(args.q)
        return 0
    print("질문을 입력하십시오. 종료: /exit")
    while True:
        try:
            q = input("질문> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q:
            continue
        if q.lower() in ("/exit", "exit", "quit"):
            break
        one(q)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
