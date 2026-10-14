"""재현·재진단 도구 — start_replay(파일 씀) · job_status · rediagnose.

재현은 Jobs(sources.py)가 talog run --replay 를 하위 프로세스로 띄운다. 콘솔 바로 묻기는 사용자가 확인한 질문에만 start_replay 를 연다."""

from __future__ import annotations

import glob
import json
import os
import time
from typing import TYPE_CHECKING

from ... import logread
from ..fmt import SEV_KO, cut, table
from ..registry import DAY, READ_ONLY, SOURCE, WRITES_LOCAL, schema, tool

if TYPE_CHECKING:
    from ..server import TalogMCP


@tool({"name": "start_replay", "title": "경보 재현 시작",
       "description": "현장 로그 폴더 하루치를 지금 talog 규칙으로 시간순 재생한다(백그라운드, 수 분~수십 분). 메일은 보내지 "
                      "않고 작업 폴더에만 쓴다. 규칙을 고친 뒤 결과가 어떻게 바뀌는지, 경보가 언제 났을지 확인할 때 쓴다. "
                      "job_status 로 진행·결과를 본다.",
       "inputSchema": schema({"source": {"type": "string", "description": "로그 루트가 있는 출처"},
                               "day": {"type": "string", "description": "로그 루트 아래 일자 폴더 이름 (예: 12 또는 2026_10\\12)"},
                               "carry_baseline": {"type": "boolean", "description": "출처에 저장된 처리 여유 기준선을 이어받을지 (기본 true)"},
                               "llm": {"type": "boolean", "description": "사건마다 로컬 LLM 교차 확인도 돌릴지 (기본 false — 느림)"}},
                              ["source", "day"]),
       "annotations": WRITES_LOCAL})
def start_replay(srv: TalogMCP, a: dict) -> str:
    s = srv.one(a.get("source", ""), need_logs=True)
    day = str(a.get("day") or "").strip().strip("\\/")
    if not day or ".." in day.replace("\\", "/").split("/"):
        raise ValueError("day 는 로그 루트 아래 일자 폴더 이름입니다 (예: 12, 2026_10\\12)")
    day_dir = day if os.path.isabs(day) else os.path.join(s.log_root, day)
    if not logread.inside(day_dir, s.log_root):         # 문자열 앞부분 비교는 D:\logs_old 를 D:\logs 안으로 본다
        raise ValueError("로그 루트 밖의 폴더는 재현할 수 없습니다")
    if not os.path.isdir(day_dir):
        subs = sorted(os.listdir(s.log_root))[-12:] if os.path.isdir(s.log_root) else []
        raise ValueError(f"일자 폴더가 없습니다: {day} — 로그 루트의 폴더: {', '.join(subs) or '(읽을 수 없음)'}")
    m = srv.jobs.start(s, day_dir, a.get("carry_baseline", True) is not False, bool(a.get("llm")))
    return srv.mask.text(
        f"# 재현 작업 시작 — {m['id']}\n- 출처 {s.name} · 일자 폴더 {day_dir}\n"
        f"- 기준선 이어받기: {'예 (' + m['seeded_from'] + ')' if m['carry_baseline'] else '아니오 (그날 첫 1시간으로 새로 잡음)'}"
        f" · 로컬 LLM: {'켬' if m['llm'] else '끔'}\n"
        f"- 하루치에 수 분~수십 분 걸린다. job_status(job_id='{m['id']}') 로 진행을 본다.")


@tool({"name": "job_status", "title": "재현 작업 상태",
       "description": "재현 작업의 진행·결과. 끝나면 경보·사건 목록이 나오고, 그 사건은 source='재현:<job id>' 로 "
                      "get_incident·capacity_trend 에 쓸 수 있다. job_id 를 비우면 작업 목록.",
       "inputSchema": schema({"job_id": {"type": "string"}}),
       "annotations": READ_ONLY})
def job_status(srv: TalogMCP, a: dict) -> str:
    jid = (a.get("job_id") or "").strip()
    if not jid:
        jobs = srv.jobs.all()
        for j in jobs:
            if j.get("state") == "running":
                srv.jobs.state(j["id"])
        jobs = srv.jobs.all()
        return srv.mask.text("# 재현 작업\n" + table(["job id", "상태", "출처", "일자 폴더", "시작", "끝"],
                                                   [[j["id"], j.get("state"), j.get("source"), j.get("day_dir"),
                                                     j.get("started"), j.get("ended", "-")] for j in jobs[:20]], 70))
    m = srv.jobs.state(jid)
    if m is None:
        raise ValueError(f"작업을 찾지 못했습니다: {jid}")
    log = srv.jobs.log_text(jid)
    el = time.time() - m.get("t0", time.time())
    out = [f"# 재현 작업 {jid} — {'진행 중' if m['state'] == 'running' else '완료' if m['state'] == 'done' else '실패'}",
           f"- 출처 {m.get('source')} · 일자 폴더 {m.get('day_dir')} · 경과 {el / 60:.1f}분"
           + (f" · 끝 {m.get('ended')}" if m.get("ended") else "")]
    tail = [ln for ln in log.splitlines() if ln.strip()][-12:]
    if m["state"] == "running":
        out += ["", "## 최근 로그", "```", *tail, "```", "", "아직 진행 중 — 잠시 뒤 다시 job_status 를 부른다."]
        return srv.mask.text("\n".join(out))
    js = srv.jobs.source(jid)
    alerts = []
    for p in sorted(glob.glob(os.path.join(m["dir"], "alerts_*.jsonl"))):
        with open(p, encoding="utf-8") as f:
            alerts += [json.loads(x) for x in f if x.strip()]
    done = [ln for ln in log.splitlines() if "리플레이 완료" in ln]
    out += [f"- {done[-1].strip() if done else '완료 줄 없음'}", "",
            f"## 경보 {len(alerts)}건",
            table(["시각", "심각도", "규칙", "제목"], [[x.get("ts"), SEV_KO.get(x.get("severity"), x.get("severity")),
                                                     x.get("rule"), x.get("title")] for x in alerts[:40]], 80)]
    incs = js.incidents() if js else []
    if incs:
        out += ["", f"## 사건 {len(incs)}건 (source='재현:{jid}' 로 get_incident)",
                table(["사건 id", "원인(룰)", "판단"],
                       [[i.get("id"), ((i.get("verdict") or {}).get("cause_name") or "-").split(" — ")[0],
                         (i.get("verdict") or {}).get("label")] for i in incs[:40]])]
    if m["state"] == "failed":
        out += ["", "## 로그 끝부분", "```", *tail, "```"]
    return cut(srv.mask.text("\n".join(out)))


@tool({"name": "rediagnose", "title": "저장된 사건 재진단",
       "description": "저장된 사건 근거에 지금 코드의 룰 진단을 다시 돌려 원인이 바뀌는 사건을 보여 준다 (규칙 회귀 확인).",
       "inputSchema": schema({"source": SOURCE, "day": DAY}),
       "annotations": READ_ONLY})
def rediagnose(srv: TalogMCP, a: dict) -> str:
    from ... import watch
    from ...agent import RuleDiagnoser, Runbook
    from ...watch import Alert
    day = srv.day_arg(a.get("day", ""))
    d = RuleDiagnoser(Runbook(), watch.load_config(""))
    rows, n, ch = [], 0, 0
    for s in srv.pick(a.get("source", "")):
        for inc in s.incidents():
            if day and not (inc.get("id") or "").startswith(day):
                continue
            if not inc.get("evidence") or not inc.get("rule"):
                continue
            alerts = [Alert(0, x.get("rule", ""), x.get("severity", "warn"), x.get("title", ""), x.get("evidence", ""))
                      for x in inc.get("alerts") or []]
            # 옛 판의 사건은 근거 블록이 빠졌을 수 있다 — 빈 블록으로 채워 판정한다
            ev = {k: {} for k in ("gpu_log", "restarts", "timeouts", "inspections", "model_load", "errors",
                                  "defects", "memory")}
            ev.update(inc["evidence"])
            try:
                new = d.diagnose(inc.get("family", ""), ev, alerts, inc.get("sys_cause", ""))
            except Exception as e:      # 옛 근거 형식 — 건너뛰고 알린다
                rows.append([inc.get("id"), inc["rule"].get("cause"), f"오류 {type(e).__name__}", "", s.name])
                continue
            n += 1
            changed = new["cause"] != inc["rule"].get("cause")
            ch += changed
            rows.append([inc.get("id"), inc["rule"].get("cause"), new["cause"], "바뀜" if changed else "",
                         s.name + (" ·재현" if inc.get("_replay") else "")])
    rows.sort(key=lambda r: (r[3] != "바뀜", r[0] or ""))
    return cut(srv.mask.text(f"# 재진단 — 사건 {n}건 중 원인이 바뀐 사건 {ch}건\n" +
                               table(["사건 id", "저장된 원인", "지금 규칙", "", "출처"], rows[:120])))
