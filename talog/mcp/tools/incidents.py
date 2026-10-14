"""사건·처리 여유 도구 — overview · list_incidents · get_incident · capacity_trend.

사건 = 묶인 경보 + 룰 진단 + 로컬 LLM 2차 의견. 처리 여유 = Seq 그룹별 유휴 스레드·점유·한계·내성(capacity.py)."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from ... import __version__
from ..fmt import SEV_KO, cut, hm, num, table
from ..registry import DAY, READ_ONLY, SOURCE, schema, tool

if TYPE_CHECKING:
    from ..server import TalogMCP


def find_incident(srv: TalogMCP, iid: str, source: str = "") -> dict:
    hits = [i for s in srv.pick(source) for i in s.incidents() if i.get("id") == iid]
    if not hits:
        raise ValueError(f"사건을 찾지 못했습니다: {iid} — list_incidents 로 id 를 확인하십시오")
    live = [h for h in hits if not h.get("_replay")]
    return (live or hits)[-1]


def evidence_lines(ev: dict) -> list[str]:
    out = []
    def v(x, unit=""):
        return "없음" if x is None else f"{num(x)}{unit}"

    c = ev.get("capacity") or {}
    if c:
        out.append(f"- 처리 여유: 그룹 {c.get('group')} · 풀 {v(c.get('pool'))} · 투입 {v(c.get('interval_s'), '초')} · "
                   f"한계 {v(c.get('limit_s'), '초')} · 유휴 중앙값 {v(c.get('idle_median'))} (기준 "
                   f"{v(c.get('idle_baseline'))}, 최소 {v(c.get('idle_min'))}) · 점유 p50/p99/최대 "
                   f"{v(c.get('hold_p50'))}/{v(c.get('hold_p99'))}/{v(c.get('hold_max'))}초 · 내성 "
                   f"{v(c.get('tolerance_s'), '초')} ({v(c.get('tolerance_ratio'))}) · 점유 출처 {c.get('hold_source') or '-'}")
    t = ev.get("trend") or {}
    if t and t.get("hold_p50_baseline") is not None:
        out.append(f"- 추세: 기준선 {t.get('since')}부터 {v(t.get('hours'), '시간')} · 점유 {v(t.get('hold_p50_baseline'))}→"
                   f"{v(t.get('hold_p50_now'))}초 ({v(t.get('hold_ratio'), '배')}) · 투입 {v(t.get('interval_baseline_s'))}→"
                   f"{v(t.get('interval_now_s'))}초 ({v(t.get('interval_change_pct'), '%')}) · 순위상관 "
                   f"{v(t.get('spearman_hourly'))} · 단조 증가 {'예' if t.get('monotonic') else '아니오'} · 함께 는 그룹 "
                   f"{t.get('groups_rising') or []} / 기준선 있는 그룹 {t.get('groups_with_baseline') or 0}개")
    elif t:
        out.append(f"- 추세: 기준선 없음 (재시작이 잦거나 기동 뒤 1시간이 안 됨) · 지금 점유 {v(t.get('hold_p50_now'), '초')} · "
                   f"투입 {v(t.get('interval_now_s'), '초')}")
    p = ev.get("phases") or {}
    if p:
        out.append(f"- 단계: {p.get('names')} 기준 {p.get('baseline_s')} → 지금 {p.get('now_s')} · 증가 몫 "
                   f"{p.get('share_of_increase_pct')}%")
    tr = ev.get("transient") or {}
    if tr:
        out.append(f"- 일시 지연(직전 60초): {tr.get('n_groups')}개 그룹 {tr.get('groups')} · " +
                   "; ".join(f"g{g}: 최대 {d.get('max_last_60s')}초 (앞 60분 p99 {d.get('p99_prev_60min')})"
                             for g, d in list((tr.get("detail") or {}).items())[:4]))
    r = ev.get("resources") or {}
    if r:
        parts = []
        if r.get("talos_cpu_now_pct") is not None:
            parts.append(f"talos CPU {r['talos_cpu_now_pct']}% (구간 시작 {r.get('talos_cpu_start_pct', '-')}%)")
        if r.get("total_cpu_now_pct") is not None:
            parts.append(f"장비 CPU {r['total_cpu_now_pct']}%")
        if r.get("threads_now") is not None:
            parts.append(f"스레드 {r.get('threads_start', '-')}→{r['threads_now']}")
        if r.get("mem_slope_mb_per_h") is not None:
            parts.append(f"메모리 기울기 {r['mem_slope_mb_per_h']}MB/h")
        known = {"talos_cpu_now_pct", "talos_cpu_start_pct", "total_cpu_now_pct", "threads_start",
                 "threads_now", "mem_slope_mb_per_h", "since"}
        parts += [f"{k} {v}" for k, v in r.items() if k not in known and v not in (None, "", [], {})]
        out.append("- 자원" + (f"({r['since']}부터)" if r.get("since") else "") + ": " + " · ".join(parts))
    i = ev.get("inspections") or {}
    if i:
        out.append(f"- 검사 흐름: 진행 중 {i.get('inflight')} (가장 오래된 것 {i.get('oldest_inflight_s')}초) · 최근 완료 "
                   f"{i.get('recent_durations_s')}초 (평소 {i.get('baseline_duration_s')}초, {i.get('slowdown_ratio')}배) · "
                   f"소요/투입 {i.get('duration_over_interval')} · 최근 도착 유휴 {i.get('wait_threads_recent')} · 10분 "
                   f"시작/완료 {i.get('starts_10min')}/{i.get('ends_10min')} · 마지막 완료 {i.get('last_end_age_s')}초 전")
    g = ev.get("gpu_log") or {}
    if g:
        out.append(f"- GPU 로그: 치명 {g.get('fatal_near')} · 인퍼런스 오류 {g.get('infer_errors_near')} · executeV2 "
                   f"{g.get('exec_slowdown_ratio')}배 ({g.get('exec_slowest_model', '')})")
    rs = ev.get("restarts") or {}
    if rs:
        last = (f"마지막 {rs['last_restart']} ({rs.get('since_restart_s'):.0f}초 전)"
                if rs.get("last_restart") and rs.get("since_restart_s") is not None else "최근 재시작 없음")
        out.append(f"- 재시작: {last} · 60분 {rs.get('count_60min') or 0}회")
    t2 = ev.get("timeouts") or {}
    if t2:
        n_it, n_at = t2.get("img_timeout_30min") or 0, t2.get("alg_timeout_30min") or 0
        out.append(f"- 타임아웃(30분): 이미지 {n_it} · 알고리즘 {n_at}" + (f" · 마지막 {t2['last']}" if t2.get("last") else ""))
    e = ev.get("errors") or {}
    if e:
        extra = [f"{k} {v}" for k, v in e.items()
                 if k not in ("seh_near", "crash_near") and v not in (None, 0, "", [], {}, False)]
        out.append(f"- 에러: SEH {e.get('seh_near') or 0} · 크래시 {'있음' if e.get('crash_near') else '없음'}"
                   + (" · " + ", ".join(extra)[:240] if extra else ""))
    m = ev.get("model_load") or {}
    if m:
        if m.get("last_cmd") or m.get("pending"):
            out.append(f"- 모델 로드: {m.get('name') or '-'} · {m.get('last_cmd') or '-'}"
                       + (" · 완료 응답 대기 중" if m.get("pending") else "")
                       + (f" · 기종 교체({m.get('prev_name')}→{m.get('name')})" if m.get("changed") else "")
                       + (f" · {m['minutes_since']}분 전" if m.get("minutes_since") is not None else ""))
        else:
            out.append("- 모델 로드: 최근 없음")
    return out or ["(근거 데이터 없음)"]


@tool({"name": "overview", "title": "데이터 개요",
       "description": "처음 부르는 도구. 연결된 talog 데이터 출처(현장)별 사건 수·날짜·처리 여유 기준선·진단 리포트·재현 작업·"
                      "사례 KB 와 마스킹 상태를 보여 준다.",
       "inputSchema": schema({}), "annotations": READ_ONLY})
def overview(srv: TalogMCP, _a: dict) -> str:
    lines = [f"# talog 데이터 개요 — talog {__version__}",
             f"마스킹: {'켜짐 (검사 id → ID-xxxxxx, 경로 → <로그>·<데이터>, 원본 로그 줄도 가림)' if srv.mask.on else '꺼짐 (원본 값 그대로)'}",
             ""]
    rows = []
    for s in srv.all_sources():
        incs = s.incidents()
        by_day: dict[str, int] = {}
        n_crit = sum(1 for i in incs if (i.get("verdict") or {}).get("severity") == "crit")
        for i in incs:
            by_day[(i.get("id") or "")[:8]] = by_day.get((i.get("id") or "")[:8], 0) + 1
        latest = max(incs, key=lambda i: i.get("id") or "", default=None)
        st, _p = s.capacity_state()
        base = ""
        if st and st.get("groups"):
            frm = min((g.get("from") or 0) for g in st["groups"].values())
            base = f"{len(st['groups'])}그룹 ({hm(frm)}~)"
        reps = s.reports()
        rows.append([s.name, s.kind, ", ".join(f"{d[4:6]}/{d[6:]}({n})" for d, n in sorted(by_day.items())[-5:]) or "-",
                     f"{len(incs)} (심각 {n_crit})",
                     (f"{(latest.get('id') or '')[:8]} {latest.get('opened', '')} "
                      f"{((latest.get('alerts') or [{}])[0]).get('title', '')}") if latest else "-",
                     base or "-", ", ".join(r["tag"] for r in reps[-3:]) or "-",
                     "있음" if s.log_root else "-"])
    lines.append(table(["출처", "종류", "날짜(사건 수)", "사건", "최근 사건", "처리 여유 기준선", "리포트", "로그 루트"], rows, 70))
    jobs = srv.jobs.all()
    if jobs:
        lines += ["", f"재현 작업 {len(jobs)}건 (최근 5건):",
                  table(["job id", "상태", "출처", "시작"],
                         [[j["id"], j.get("state"), j.get("source"), j.get("started")] for j in jobs[:5]])]
    try:
        from ...casekb import CaseKB
        kb = CaseKB(srv.kb_dir)
        lines += ["", f"사례 KB: {len(kb.cases)}건"]
    except Exception:
        pass
    lines += ["", "다음: list_incidents(day=…) → get_incident(incident_id=…) · capacity_trend(source=…) · "
                  "list_reports → get_report(tag=…) · runbook()"]
    return srv.mask.text("\n".join(lines))


@tool({"name": "list_incidents", "title": "사건 목록",
       "description": "talog 실시간 감시(또는 경보 재현)가 만든 사건 목록. 사건 = 묶인 경보 + 룰 진단 + LLM 2차 의견. "
                      "id 를 get_incident 에 넘긴다.",
       "inputSchema": schema({"source": SOURCE, "day": DAY,
                               "severity": {"type": "string", "enum": ["all", "crit", "warn"],
                                            "description": "심각도 거르기 (기본 all)"},
                               "limit": {"type": "integer", "minimum": 1, "maximum": 200,
                                         "description": "최대 행 수 (기본 30)"}}),
       "annotations": READ_ONLY})
def list_incidents(srv: TalogMCP, a: dict) -> str:
    day = srv.day_arg(a.get("day", ""))
    sev = (a.get("severity") or "all").lower()
    limit = int(a.get("limit") or 30)
    rows = []
    for s in srv.pick(a.get("source", "")):
        for i in s.incidents():
            iid = i.get("id") or ""
            if day and not iid.startswith(day):
                continue
            v = i.get("verdict") or {}
            if sev in ("crit", "warn") and v.get("severity") != sev:
                continue
            al = i.get("alerts") or [{}]
            rows.append((iid, [iid, f"{iid[4:6]}/{iid[6:8]} {i.get('opened', '')}",
                               SEV_KO.get(v.get("severity"), v.get("severity") or "-"),
                               al[0].get("title", "") + (f" (+{len(al) - 1})" if len(al) > 1 else ""),
                               (v.get("cause_name") or "-").split(" — ")[0], v.get("label") or "-",
                               s.name + (" ·재현" if i.get("_replay") else "")]))
    rows.sort(key=lambda r: r[0], reverse=True)
    total = len(rows)
    body = table(["사건 id", "시각", "심각도", "제목", "원인(룰)", "판단", "출처"], [r[1] for r in rows[:limit]], 60)
    head = f"# 사건 {total}건" + (f" — {day}" if day else "") + (f" (최근 {limit}건 표시)" if total > limit else "")
    return srv.mask.text(head + "\n" + body + "\n\n다음: get_incident(incident_id=…)")


@tool({"name": "get_incident", "title": "사건 상세",
       "description": "사건 하나의 판단(룰·LLM 일치 여부, 원인, 권고 조치), 룰 근거, 해석 문장, 경보, 근거 데이터"
                      "(처리 여유·추세·단계·일시 지연·자원·검사 흐름·GPU 로그·재시작), 비슷한 과거 사례.",
       "inputSchema": schema({"incident_id": {"type": "string", "description": "사건 id (예: 20261012-050228-capacity_drift)"},
                               "source": SOURCE,
                               "include_log_lines": {"type": "boolean", "description": "근거 로그 원문 줄을 넣을지 (기본 false, 마스킹 적용)"},
                               "raw_evidence": {"type": "boolean", "description": "근거 JSON 전체를 덧붙일지 (기본 false)"}},
                              ["incident_id"]),
       "annotations": READ_ONLY})
def get_incident(srv: TalogMCP, a: dict) -> str:
    from ...agent import Runbook
    inc = find_incident(srv, str(a.get("incident_id", "")).strip(), a.get("source", ""))
    rb = Runbook()
    v, rule, ev = inc.get("verdict") or {}, inc.get("rule") or {}, inc.get("evidence") or {}
    alerts = inc.get("alerts") or []
    out = [f"# 사건 {inc.get('id')} — {(alerts[0] if alerts else {}).get('title', '')}",
           f"- 현장 {inc.get('site') or '-'} · {inc.get('opened', '')}~{inc.get('closed', '')} · 계열 "
           f"{(rb.families.get(inc.get('family') or '') or {}).get('title', inc.get('family'))} · 출처 {inc.get('_source')}"
           + (" (경보 재현)" if inc.get("_replay") else ""), "",
           "## 판단",
           f"- {v.get('label', '-')} · {SEV_KO.get(v.get('severity'), v.get('severity', '-'))} · 원인: {v.get('cause_name', '-')}",
           f"- 담당자 메시지: {v.get('message', '-')}",
           "- 권고 조치:"]
    out += [f"  {k}. {rb.action_ko(x)}" for k, x in enumerate(v.get("actions") or [], 1)]
    if v.get("notify"):
        out.append("- 담당: " + ", ".join(rb.role_ko(r) for r in v["notify"]))
    if rule.get("facts"):
        out += ["", "## 룰 근거"] + [f"- {f}" for f in rule["facts"]]
    if ev.get("key_facts"):
        out += ["", "## 핵심 사실 (해석 문장 — 로컬 LLM 입력)"] + [f"- {f}" for f in ev["key_facts"]]
    if alerts:
        out += ["", f"## 경보 {len(alerts)}건",
                table(["시각", "심각도", "규칙", "제목", "근거"],
                       [[x.get("time"), SEV_KO.get(x.get("severity"), x.get("severity")), x.get("rule"),
                         x.get("title"), x.get("evidence")] for x in alerts[:12]], 90)]
    out += ["", "## 근거 데이터 (요약)"] + evidence_lines(ev)
    llm = inc.get("llm") or {}
    if llm:
        out += ["", "## 로컬 LLM 2차 의견",
                (f"- {llm.get('model', '')} · {llm.get('device', '')} · {llm.get('wall_s', '')}초 → 원인 "
                 f"{llm.get('root_cause', '-')} (신뢰도 {llm.get('confidence', '-')})" if llm.get("ok")
                 else f"- 실패: {llm.get('error', '')}")]
        out += [f"  · {e}" for e in (llm.get("evidence") or [])[:4]]
        if llm.get("message_dropped"):
            out.append("  · 문구 다듬기로 뺀 문장: " + " / ".join(llm["message_dropped"]))
    if inc.get("similar"):
        out += ["", "## 비슷한 과거 사례 (사례 KB)"] + [
            f"- [{s.get('score')}] {s.get('title')} — 원인: {s.get('cause')} / 조치: {s.get('action')}"
            for s in inc["similar"]]
    logs = ev.get("log_lines") or []
    if logs:
        if a.get("include_log_lines"):
            out += ["", f"## 근거 로그 {len(logs)}줄"] + [f"    {ln}" for ln in logs[:60]]
        else:
            out += ["", f"(근거 로그 {len(logs)}줄은 생략 — include_log_lines=true 로 요청)"]
    if a.get("raw_evidence"):
        ev2 = {k: v2 for k, v2 in ev.items() if k not in ("log_lines", "key_facts")}
        out += ["", "## 근거 JSON", "```json", json.dumps(ev2, ensure_ascii=False, indent=1, default=str)[:12000], "```"]
    return cut(srv.mask.text("\n".join(out)))


@tool({"name": "capacity_trend", "title": "처리 여유 추세",
       "description": "Seq 그룹별 처리 여유: 기준선(기동 뒤 첫 1시간의 유휴 스레드·점유 시간·투입 간격·풀)과 그날 시간별 KPI"
                      "(유휴 중앙값, 점유 p50/p99, 한계=풀×투입 간격, 일시 지연 내성, 거부), 프로세스 CPU·메모리.",
       "inputSchema": schema({"source": SOURCE, "day": DAY,
                               "groups": {"type": "array", "items": {"type": "integer"},
                                          "description": "볼 그룹 번호 (비우면 점유가 가장 많이 는 그룹 4개)"}}),
       "annotations": READ_ONLY})
def capacity_trend(srv: TalogMCP, a: dict) -> str:
    s = srv.one(a.get("source", ""))
    st, st_path = s.capacity_state()
    days = s.kpi_days()
    day = srv.day_arg(a.get("day", "")) or (days[-1] if days else "")
    rows = s.kpi_rows(day) if day else []
    grp_rows = [r for r in rows if isinstance(r.get("group"), int)]
    proc = [r for r in rows if r.get("group") == "process"]
    base = (st or {}).get("groups") or {}
    want = [int(g) for g in (a.get("groups") or [])]
    if not want:
        ratio = {}
        for r in grp_rows:
            b = base.get(str(r["group"])) or {}
            if b.get("hold_p50") and r.get("hold_p50"):
                ratio[r["group"]] = max(ratio.get(r["group"], 0), r["hold_p50"] / b["hold_p50"])
        want = [g for g, _ in sorted(ratio.items(), key=lambda kv: -kv[1])[:4]] or \
            sorted({r["group"] for r in grp_rows})[:4]
    out = [f"# 처리 여유 — {s.name} · {day[:4]}-{day[4:6]}-{day[6:]}" if day else f"# 처리 여유 — {s.name}"]
    if base:
        out += ["", f"## 기준선 ({os.path.basename(st_path)} · 세대 시작 {hm((st or {}).get('gen_start'))})",
                table(["그룹", "기준 구간", "유휴 중앙값", "점유 p50", "점유 p99", "투입 간격", "풀", "도착 수"],
                       [[g, f"{hm(b.get('from'))}~{hm(b.get('to'))[6:]}", b.get("idle_median"), num(b.get("hold_p50")),
                         num(b.get("hold_p99")), num(b.get("interval_s")), b.get("pool"), b.get("n")]
                        for g, b in sorted(base.items(), key=lambda kv: int(kv[0]))])]
    else:
        out += ["", "기준선 없음 — 기동 뒤 안정 구간(워밍업 10분 + 1시간, 도착 100건 이상)이 아직 없거나 재시작이 잦다."]
    if not rows:
        out += ["", f"시간별 KPI 없음 (kpi 파일 날짜: {', '.join(days) or '-'})"]
        return srv.mask.text("\n".join(out))
    for g in want:
        gr = sorted((r for r in grp_rows if r["group"] == g), key=lambda r: r["hour"])
        if not gr:
            continue
        b = base.get(str(g)) or {}
        trs = []
        for r in gr:
            lim = (r.get("pool") or 0) * (r.get("interval_s") or 0)
            tol = (lim - r["hold_p99"]) if (lim and r.get("hold_p99") is not None) else None
            ratio = (r["hold_p50"] / b["hold_p50"]) if (b.get("hold_p50") and r.get("hold_p50")) else None
            trs.append([r["hour"][-5:], r.get("n"), num(r.get("idle_median")), num(r.get("hold_p50")),
                        num(ratio, 2) if ratio else "-", num(r.get("hold_p99")), num(r.get("hold_max")),
                        num(lim, 2) if lim else "-", num(tol, 2) if tol is not None else "-",
                        num(r.get("interval_s")), r.get("pool"), r.get("rejects") or 0])
        out += ["", f"## 그룹 {g}" + (f" (기준: 유휴 {b.get('idle_median')}, 점유 {b.get('hold_p50')}초)" if b else ""),
                table(["시", "도착", "유휴", "점유 p50", "기준 대비", "p99", "최대", "한계", "내성", "투입", "풀", "거부"], trs)]
    if proc:
        out += ["", "## 프로세스", table(["시", "메모리 최대 MB", "talos CPU %", "장비 CPU %", "스레드"],
                                        [[r["hour"][-5:], r.get("mem_mb_max"), r.get("cpu_pct"),
                                          r.get("total_cpu_pct"), r.get("threads")]
                                         for r in sorted(proc, key=lambda r: r["hour"])])]
    others = sorted({r["group"] for r in grp_rows} - set(want))
    if others:
        out += ["", f"다른 그룹 {others} 는 groups=[…] 로 지정해 본다."]
    return cut(srv.mask.text("\n".join(out)))
