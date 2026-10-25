"""과거 일자 재생(리플레이)과 사후 처리 여유 검토 — 실시간과 같은 룰 엔진을 이벤트 시각 기준으로 돌린다."""

from __future__ import annotations

import json
import os

from ..events import Event, Extractor
from ..fileclass import classify
from ..tracker import resolve_targets
from .alerts import Notifier
from .config import _DEFAULT_CFG
from .rules import RuleEngine
from .wiring import _ENGINE_KINDS, _attach_capacity_state, _make_agent


def _replay_file(fi, ex: Extractor, pat, keep) -> list[Event]:
    """리플레이용 1회 읽기: 룰 이벤트 + 사용자 패턴에 걸린 줄(kind=LINE)."""
    from ..lineparser import iter_batchrun, iter_records
    fname = os.path.basename(fi.path)
    out: list[Event] = []
    if fi.category == "batchrun":
        for ts, tt, script in iter_batchrun(fi.path):
            out.append(Event(ts=ts, ts_text=tt, kind="BATCH", name=script))
            if pat.active and pat.prefilter(fname, "", script):
                out.append(Event(ts=ts, ts_text=tt, kind="LINE", name=fname,
                                 extra=script))
        return out
    rules = ex.rules.get(fi.category, [])
    for rec in iter_records(fi.path):
        if pat.active:
            line = f"{rec.ts_text}\t[{rec.level}][{rec.header}][{rec.obj_id}]\t{rec.msg}"
            if pat.prefilter(fname, rec.level, line):
                out.append(Event(ts=rec.ts, ts_text=rec.ts_text, kind="LINE",
                                 name=fname, level=rec.level, extra=line[:600]))
        ev = ex._match(rec, rules) if rules else None
        if ev is None and rec.level == "Error":
            ev = Event(ts=rec.ts, ts_text=rec.ts_text, kind="ERROR", extra=rec.msg[:500])
        if ev is None or (keep is not None and ev.kind not in keep):
            continue
        ev.obj_id = rec.obj_id
        ev.alg_idx = fi.alg_idx
        if fi.category == "comm" and ev.kind == "COMM_MSG":
            Extractor._enrich_comm(ev)
        out.append(ev)
    return out


_REVIEW_KINDS = frozenset(("INSP_RECV", "INSP_START", "INSP_REJECT", "COMM_MSG", "SEQ_START",
                           "PROC_TIME", "SEND_RESULT", "SEQ_FINISH", "POOL_CREATE", "USAGE"))


def capacity_review(events: list, state: dict | None = None) -> dict:
    """사후 리포트용: 하루치 이벤트를 시간순으로 다시 돌려 그룹별 처리 여유·추세·경보를 낸다.

    실시간 감시와 같은 RuleEngine·CapacityModel 을 쓰므로 리포트와 경보의 판단이 같다.
    state({'cap': ...})를 주면 앞 날짜의 기준선·시간별 추세를 이어받고 끝난 뒤 갱신한다
    (여러 날을 한 번에 분석할 때 기준선이 날짜를 넘어 이어진다)."""
    import contextlib
    import io
    import tempfile
    cfg = json.loads(json.dumps(_DEFAULT_CFG))
    cfg["alert_dir"] = tempfile.gettempdir()
    cfg["notify"].update(jsonl=False, toast=False, webhook="")
    with contextlib.redirect_stdout(io.StringIO()):
        n = Notifier(cfg, replay=True)
        eng = RuleEngine(cfg, n)
        if state and state.get("cap"):
            eng.cap.load(state["cap"])
        nxt, last, last_insp = None, None, None
        for e in events:
            if e.kind not in _REVIEW_KINDS:
                continue
            eng.feed(e)
            if nxt is None or e.ts >= nxt:
                eng.evaluate(e.ts)
                nxt = e.ts + 20
            last = e.ts
            if e.kind == "INSP_START":
                last_insp = e.ts
        if last is None:
            return {}
        eng.evaluate(last)
        eng.cap.hourly_rows(last + 3600)
        # 요약은 마지막 검사 시각 기준 (ProcessUsage 는 생산이 멈춘 뒤에도 계속 찍힌다)
        last = last_insp or last
    if state is not None:
        state["cap"] = eng.cap.state()
    groups = {g: eng.cap.stats(g, last) for g in sorted(eng.cap.g)}
    pg = eng.cap.prev_gen
    if pg and pg["end"] >= last - 1 and not any(s["n_arrivals"] for s in groups.values()):
        groups = pg["groups"]                     # 마지막 검사 뒤 재시작 — 재시작 직전 세대로 요약
    return {"alerts": [a for a in n.sent if a.rule in ("capacity_drift", "tact_drift")],
            "groups": groups,
            "hourly": {g: list(x.hourly) for g, x in sorted(eng.cap.g.items())},
            "evidence": eng.cap.evidence(last, None), "log": list(eng.cap.events),
            "end": last}


def run_replay(cfg: dict, day_dir: str) -> int:
    """과거 일자 폴더를 시간순으로 재생하여 룰 경보를 검증한다."""
    if not os.path.isdir(day_dir):
        print(f"리플레이 폴더가 없습니다: {day_dir}")
        return 1
    print(f"[talog watch] 리플레이: {day_dir}")
    notifier = Notifier(cfg, replay=True)
    engine = RuleEngine(cfg, notifier)
    # 같은 data_dir 로 여러 날을 이어 재생하면 앞 날의 기준선·추세가 이어진다 (_replay 파일)
    cap_state = _attach_capacity_state(engine, notifier.alert_dir, replay=True)
    ex = Extractor()
    events: list[Event] = []
    tc = cfg.get("tracking") or {}
    mode = str(tc.get("mode", "default") or "default").lower()
    pat = engine.patterns
    # 같은 시각 이벤트의 순서를 예전(폴더 나열 순)과 같게 둔다
    for path in sorted(resolve_targets(day_dir, tc),
                       key=lambda p: os.path.basename(p).upper()):
        fi = classify(path)
        if fi is None:
            continue
        try:
            if mode == "default" and not pat.active:
                evs, _n = ex.extract_file(fi, 0)       # 기존 경로 그대로
            else:
                evs = _replay_file(fi, ex, pat, None if mode == "default"
                                   else _ENGINE_KINDS)
            events.extend(evs)
        except (OSError, ValueError, UnicodeError):
            continue
    events.sort(key=lambda e: e.ts)
    if not events:
        print("이벤트 없음")
        return 1
    # 사건 분석·메일도 이벤트 시각 기준으로 재생한다 (메일은 SMTP 없이 outbox_replay 에만)
    agent = _make_agent(cfg, engine, notifier, replay=True, day_dir=day_dir)
    next_eval = events[0].ts
    for e in events:
        if agent is not None:
            # 묶음 마감 시각이 지났으면 그 시각의 상태로 마감한다 (다음 이벤트가
            # 한참 뒤여도 '미래' 로그가 근거에 섞이지 않게)
            due = agent.due()
            if due is not None and due <= e.ts:
                agent.tick(due)
        if e.kind == "LINE":                      # 사용자 패턴에 걸린 원문 줄
            pat.feed(e.ts, e.name, e.level, e.extra)
        else:
            engine.feed(e)
        if e.ts >= next_eval:                     # 20초 간격 판정 시뮬레이션
            engine.evaluate(e.ts)
            next_eval = e.ts + 20
    engine.evaluate(events[-1].ts)
    cap_state.write_kpi(engine.hourly_rows(events[-1].ts + 3600))   # 마지막 시간대까지 (처리 여유 + 판정)
    cap_state.save(engine.cap)
    if agent is not None:
        agent.flush(agent.due() or events[-1].ts)
    print(f"[talog watch] 리플레이 완료 — 이벤트 {len(events):,}개, "
          f"경보 {len(notifier.sent)}건"
          + (f", 사건 분석 {agent.processed}건 → {notifier.alert_dir}\\"
             f"incidents_*_replay.jsonl" if agent is not None else ""))
    return 0
