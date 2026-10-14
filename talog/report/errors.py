"""에러·예외 구역 — 묶음 기준(_err_group_key)과 요약·전체 표."""

from __future__ import annotations

from collections import Counter

from .context import ReportContext
from .util import _esc, _fmt_ts


_ERR_KIND_DESC = {
    "COMM_FAIL": "소켓 수신 길이 오류 (RecvLength)",
    "CRASH": "프로세스 예외 종료 (exception_callback)",
    "EXC_SAFE": "SEH 예외 (SafeCall 포착)",
    "FOV_FAIL": "FOV 선별 실패 (결함명 미발견)",
    "GPU_FATAL": "GPU 컨텍스트 치명 오류 (enqueueV3)",
}


def _err_group_key(msg: str) -> str:
    """에러 그룹핑 키 정규화 — 검사 id/수치가 같은 메시지를 갈라놓지 않게."""
    import re as _re
    msg = _re.sub(r"\b\d{12,}\b", "<id>", msg)
    msg = _re.sub(r"\d+\(ms\)", "N(ms)", msg)
    msg = _re.sub(r"0x[0-9a-fA-F]{6,}", "0x<addr>", msg)
    return msg


def _errors_section(ctx: ReportContext) -> str:
    errs = [e for e in ctx.events
            if e.kind in ("ERROR", "MODEL_FAIL", "CRASH", "COMM_FAIL",
                          "RECIPE_FAIL", "EXC_REDIRECT", "EXC_SAFE",
                          "FOV_FAIL", "GPU_FATAL")]
    if not errs:
        return "<p class='ok'>에러가 없습니다.</p>"
    grouped = Counter()
    first: dict = {}
    for e in errs:
        if e.kind == "EXC_REDIRECT":
            key = (f"예외(@{e.name})", _err_group_key(e.extra[:120]))
        else:
            kd = ("WARN" if e.extra.startswith("[WARNING]") else e.kind)
            key = (kd, _err_group_key(e.model or e.extra[:150] or e.name
                                      or _ERR_KIND_DESC.get(e.kind, "")))
        grouped[key] += 1
        first.setdefault(key, e)
    rows = []
    for key, cnt in grouped.most_common():
        e = first[key]
        detail = (e.model or e.extra or e.name
                  or _ERR_KIND_DESC.get(e.kind, ""))
        if e.kind == "MODEL_FAIL" and ctx.recipe:
            mi = int(e.model.replace("DLMODEL", "")) if e.model.startswith("DLMODEL") else 0
            m = ctx.recipe.models.get(mi)
            if m:
                detail = f"{e.model} = {m.model_file} (GPU{m.dev_index}, {m.infer_dll})"
        ctxt = getattr(e, "context", "")
        acc = (f"<details class='ctx'><summary>원본 로그 전후 ±3줄</summary>"
               f"<pre>{_esc(ctxt)}</pre></details>") if ctxt else ""
        rows.append(f"<tr><td>{_fmt_ts(e.ts_text)}</td><td>{_esc(key[0])}</td>"
                    f"<td class='r'>{cnt}</td><td>{_esc(detail)}{acc}</td></tr>")
    return ("<p class='legend'>같은 메시지는 묶어 최초 발생 기준으로 표시합니다. "
            "행의 <b>원본 로그 전후 ±3줄</b>을 펼치면 해당 시점의 주변 기록을 "
            "바로 확인할 수 있습니다.</p>"
            "<table class='sortable'><thead><tr><th>최초 발생</th><th>종류</th>"
            "<th>건수</th><th>내용</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table>")


def _errors_summary(ctx: ReportContext) -> str:
    """종합 페이지용: 에러 상위 그룹 요약."""
    errs = [e for e in ctx.events
            if e.kind in ("ERROR", "MODEL_FAIL", "CRASH", "COMM_FAIL",
                          "RECIPE_FAIL", "EXC_REDIRECT", "EXC_SAFE",
                          "FOV_FAIL", "GPU_FATAL")]
    if not errs:
        return "<p class='ok'>에러가 없습니다.</p>"
    grouped = Counter()
    first: dict = {}
    for e in errs:
        kd = (f"예외(@{e.name})" if e.kind == "EXC_REDIRECT" else
              ("WARN" if e.extra.startswith("[WARNING]") else e.kind))
        key = (kd, _err_group_key(e.model or e.extra[:110] or e.name
                                  or _ERR_KIND_DESC.get(e.kind, "")))
        grouped[key] += 1
        first.setdefault(key, e)
    rows = []
    for key, cnt in grouped.most_common(6):
        e = first[key]
        rows.append(f"<tr onclick=\"gotoSection('sec-err')\" style='cursor:pointer'>"
                    f"<td>{_fmt_ts(e.ts_text)}</td><td>{_esc(key[0])}</td>"
                    f"<td class='r'>{cnt}</td><td>{_esc(key[1])}</td></tr>")
    more = (f"<div class='legend'>총 {len(errs)}건 / {len(grouped)}그룹 — 행을 "
            f"클릭하면 전체 목록으로 이동합니다</div>")
    return ("<table><thead><tr><th>최초</th><th>종류</th><th class='r'>건수</th>"
            "<th>내용</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table>" + more)
