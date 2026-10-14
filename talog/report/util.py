"""리포트 HTML 조각에 공통으로 쓰는 작은 도구 — 이스케이프, 색 변수, 시각 표기, 선 아이콘."""

from __future__ import annotations

import html

from ..recipe import Recipe


    #   (ts_epoch, gpu_idx, temp_c, util_pct, mem_mb)


def _esc(v) -> str:
    return html.escape(str(v))


# 시리즈/중립 색을 테마 변수로 번역 (status 색은 라이트/다크 공통이라 hex 유지)
_CVAR = {
    "#2a78d6": "var(--blue)", "#eb6834": "var(--orange)",
    "#1baf7a": "var(--aqua)", "#86b6ef": "var(--blue-soft)",
    "#898781": "var(--muted)", "#4a3aa7": "var(--violet)",
    "#0ca30c": "var(--good)", "#ec835a": "var(--serious)",
    "#d03b3b": "var(--crit)",
}


def _c(color: str) -> str:
    return _CVAR.get(color, color)


def _fmt_ts(text: str) -> str:
    return text[:12] if text else "-"


def _translate_alglist(recipe: Recipe | None, lst: str) -> str:
    if not lst:
        return ""
    out = []
    for tok in lst.split(","):
        tok = tok.strip()
        if tok.isdigit() and recipe:
            out.append(f"{tok}({recipe.alg_name(int(tok))})")
        elif tok:
            out.append(tok)
    return ", ".join(out)


def _ico(*paths: str) -> str:
    """콘솔과 같은 선 아이콘 (24×24) — 글꼴에 따라 모양이 바뀌는 기호 문자 대신 쓴다."""
    return ("<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.6' "
            "stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'>"
            + "".join(f"<path d='{d}'/>" for d in paths) + "</svg>")
