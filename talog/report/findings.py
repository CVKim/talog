"""자동 진단 소견 카드 (심각·주의·참고·정상)."""

from __future__ import annotations

from .context import ReportContext
from .util import _esc, _ico


# severity -> (액센트 색, 아이콘 배경, 라벨, 아이콘)
_SEV_STYLE = {
    "crit": ("var(--crit-text)", "var(--crit)", "심각", _ico("M18 6 6 18", "m6 6 12 12")),
    "warn": ("var(--serious-text)", "var(--serious)", "주의", _ico("M12 6v8", "M12 18h.01")),
    "info": ("var(--muted)", "var(--muted)", "참고", _ico("M12 11v6", "M12 7h.01")),
    "ok":   ("var(--good-text)", "var(--good)", "정상", _ico("M20 6 9 17l-5-5")),
}


_SEV_ORDER = {"crit": 0, "warn": 1, "info": 2, "ok": 3}


def _findings_section(ctx: ReportContext) -> str:
    """종합 최상단: 룰 기반 자동 진단 소견 (+선택적 LLM 종합 소견)."""
    if not ctx.findings:
        return ""
    out = []
    if ctx.llm_summary:
        out.append(
            "<h2>AI 종합 소견 <span class='hint'>— 로컬 LLM이 아래 룰 진단을 "
            "바탕으로 작성</span></h2>"
            f"<div style='border:1px solid var(--line);background:var(--surface);"
            f"border-radius:12px;box-shadow:var(--shadow);padding:12px 16px;"
            f"white-space:pre-wrap;font-size:13.5px'>{_esc(ctx.llm_summary)}</div>")
    out.append("<h2>자동 진단 소견 <span class='hint'>— 룰 엔진이 로그 전수에서 "
               "도출한 결과입니다</span></h2>")
    ordered = sorted(ctx.findings,
                     key=lambda f: _SEV_ORDER.get(f.severity, 2))
    for f in ordered:
        color, ico_bg, label, icon = _SEV_STYLE.get(f.severity,
                                                    _SEV_STYLE["info"])
        if f.severity in ("ok", "info"):
            # 낮은 심각도는 한 줄 컴팩트 행으로 (스캔 리듬 유지)
            ev1 = f.evidence[0] if f.evidence else ""
            out.append(
                f"<div class='finding slim' style='border-left-color:{ico_bg}'>"
                f"<div class='fico' style='background:{ico_bg}'>{icon}</div>"
                f"<div><span class='badge' style='color:{color}'>{label}"
                f"</span><b>{_esc(f.title)}</b>"
                f"<span class='hint'> — {_esc(ev1)}</span></div></div>")
            continue
        ev = "".join(f"<li>{_esc(x)}</li>" for x in f.evidence)
        adv = (f"<div class='fadv'>→ {_esc(f.advice)}</div>"
               if f.advice else "")
        out.append(
            f"<div class='finding' style='border-left-color:{ico_bg}'>"
            f"<div class='fico' style='background:{ico_bg}'>{icon}</div>"
            f"<div style='min-width:0'><div class='ftitle'>"
            f"<span class='badge' style='color:{color}'>{label}</span>"
            f"{_esc(f.title)}</div><ul>{ev}</ul>{adv}</div></div>")
    if ctx.similar_cases:
        rows = "".join(
            f"<div style='border:1px solid var(--line);border-radius:12px;"
            f"background:var(--surface);box-shadow:var(--shadow);"
            f"padding:9px 13px;margin:7px 0;font-size:13px'>"
            f"<b>{_esc(t)}</b> <span class='hint'>({_esc(site)} {_esc(dt)}, "
            f"유사도 {s})</span><br>원인: {_esc(cz)}<br>조치: {_esc(ac)}</div>"
            for s, t, cz, ac, site, dt in ctx.similar_cases)
        out.append("<h2>유사 과거 사례 <span class='hint'>— 사례 지식베이스(kb) "
                   "검색 결과</span></h2>" + rows)
    return "".join(out)
