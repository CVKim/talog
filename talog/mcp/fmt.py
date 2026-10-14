"""MCP 도구 결과 글 만들기 — 마크다운 표·숫자·시각, 길이 상한. 도구 모듈이 함께 쓴다."""

from __future__ import annotations

import datetime as dt

MAX_TEXT = 22000                  # 도구 결과 한 번의 글자 수 상한 (Claude Code 출력 한도 안)
SEV_KO = {"crit": "심각", "warn": "주의", "info": "정보"}


def now_iso() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def hm(ts) -> str:
    try:
        return dt.datetime.fromtimestamp(float(ts)).strftime("%m/%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "-"


def cut(text: str, limit: int = MAX_TEXT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n… (결과가 길어 {len(text) - limit:,}자를 잘랐습니다 — 범위를 좁혀 다시 호출하십시오)"


def cell(v, width: int = 80) -> str:
    s = "" if v is None else str(v)
    s = s.replace("|", "／").replace("\r", " ").replace("\n", " ")
    return s if len(s) <= width else s[: width - 1] + "…"


def table(headers: list[str], rows: list[list], width: int = 80) -> str:
    if not rows:
        return "(없음)"
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(cell(v, width) for v in r) + " |")
    return "\n".join(out)


def num(v, nd: int = 3):
    if v is None:
        return "-"
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
        return str(int(v))
    if isinstance(v, float):
        s = f"{v:.{nd}f}".rstrip("0").rstrip(".")
        return s or "0"
    return v
