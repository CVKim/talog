"""런북·사례 KB 도구 — runbook · search_cases · save_case(파일 씀)."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from ..fmt import cut, table
from ..masking import LONG_DIGITS
from ..registry import READ_ONLY, STR, WRITES_LOCAL, schema, tool

if TYPE_CHECKING:
    from ..server import TalogMCP


@tool({"name": "runbook", "title": "원인·조치 사전",
       "description": "talog 런북: 경보 → 분석 계열, 계열별 원인(이름·판단 근거 설명)과 조치·담당, 판정 문턱값. "
                      "룰 진단과 같은 어휘로 말하려면 먼저 본다.",
       "inputSchema": schema({"family": {"type": "string", "enum": ["", "reject", "defect", "system"],
                                          "description": "계열 (비우면 전체 요약)"}}),
       "annotations": READ_ONLY})
def runbook(srv: TalogMCP, a: dict) -> str:
    from ...agent import Runbook
    rb = Runbook()
    fam = (a.get("family") or "").strip()
    out = ["# talog 런북 (원인·조치 사전)"]
    if not fam:
        amap: dict[str, list[str]] = {}
        for rule, f in rb.alert_family.items():
            amap.setdefault(str(f).split(".")[0], []).append(rule)
        for k, f in rb.families.items():
            causes = rb.causes(k)
            out += ["", f"## {k} — {f.get('title', '')}",
                    "- 원인: " + ", ".join(f"{cid}({(c.get('name') or cid).split(' — ')[0]})" for cid, c in causes.items())]
            if amap.get(k):
                out.append("- 경보 규칙: " + ", ".join(amap[k]))
        out += ["", "## 판정 문턱값 (params)", ", ".join(f"{k}={v}" for k, v in rb.params.items()),
                "", "계열을 지정하면 원인별 판단 근거 설명과 조치를 보여 준다: runbook(family='reject')"]
        return "\n".join(out)
    f = rb.families.get(fam)
    if not f:
        raise ValueError(f"계열이 없습니다: {fam}")
    out.append(f"## {fam} — {f.get('title', '')}")
    for cid, c in rb.causes(fam).items():
        out += ["", f"### {cid} — {c.get('name', cid)}", (c.get("description") or "").strip(),
                "- 조치: " + " / ".join(rb.action_ko(x) for x in c.get("actions") or []),
                "- 담당: " + ", ".join(rb.role_ko(r) for r in c.get("notify") or [])]
    return cut("\n".join(out))


@tool({"name": "search_cases", "title": "비슷한 과거 사례",
       "description": "사례 KB(과거 RCA: 증상·원인·조치)에서 비슷한 사례를 찾는다 (로컬 임베딩, 없으면 키워드).",
       "inputSchema": schema({"query": {"type": "string"}, "k": {"type": "integer", "minimum": 1, "maximum": 10}}, ["query"]),
       "annotations": READ_ONLY})
def search_cases(srv: TalogMCP, a: dict) -> str:
    from ...casekb import CaseKB
    kb = CaseKB(srv.kb_dir)
    k = max(1, min(10, int(a.get("k") or 5)))
    hits = kb.search(str(a.get("query") or ""), k)
    if not hits:
        return f"비슷한 사례 없음 (사례 KB {len(kb.cases)}건)"
    rows = [[round(sc, 2), c.id, c.site, c.date, c.title, c.cause, c.action] for sc, c in hits]
    return srv.mask.text(f"# 비슷한 사례 {len(rows)}건 (사례 KB {len(kb.cases)}건)\n" +
                          table(["점수", "id", "현장", "날짜", "제목", "원인", "조치"], rows, 90))


@tool({"name": "save_case", "title": "사례 저장",
       "description": "확정된 RCA 를 사례 KB 에 한 건 추가한다. 다음 배포 때 현장 talog 가 비슷한 사건에 이 사례를 붙인다. "
                      "고객명·검사 id 같은 원본 값은 넣지 말고 현상·원인·조치를 일반화해 쓴다.",
       "inputSchema": schema({"title": STR, "symptom": STR, "cause": STR, "action": STR,
                               "site": {"type": "string", "description": "현장 (가명 가능)"},
                               "tags": {"type": "array", "items": {"type": "string"}}},
                              ["title", "symptom", "cause", "action"]),
       "annotations": WRITES_LOCAL})
def save_case(srv: TalogMCP, a: dict) -> str:
    from ...casekb import Case, CaseKB
    kb = CaseKB(srv.kb_dir)
    fields = {k: str(a.get(k) or "").strip() for k in ("title", "symptom", "cause", "action", "site")}
    if not all(fields[k] for k in ("title", "symptom", "cause", "action")):
        raise ValueError("title·symptom·cause·action 은 비울 수 없습니다")
    if srv.mask.on and LONG_DIGITS.search(" ".join(fields.values())):
        raise ValueError("사례에 검사 id 같은 원본 값이 있습니다 — 일반화해서 다시 쓰십시오")
    case = Case(id="", date=dt.date.today().isoformat(), tags=[str(t) for t in a.get("tags") or []], **fields)
    kb.add(case)
    return f"사례 저장: {case.id} — {case.title} (사례 KB {len(kb.cases) + 1}건, {kb.path})"
