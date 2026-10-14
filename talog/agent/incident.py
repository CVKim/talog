"""사건 에이전트 — 경보를 사건으로 묶고 근거 수집 → 진단 → 합의 → 메일·JSONL 기록까지 처리한다."""

from __future__ import annotations

import datetime as dt
import json
import os
import queue
import threading
import time

from .analyst import LLMAnalyst
from .common import _FAMILY_RANK, _alert_dict, _hms, sev_rank
from .diagnoser import RuleDiagnoser
from .evidence import EvidenceBuilder, GpuLogProbe
from .gate import gate
from .interpret import interpret
from .runbook import Runbook


class IncidentAgent:
    """경보를 사건으로 묶고 분석·알림까지 처리한다 (실시간: 작업 스레드 / 리플레이: 동기)."""

    def __init__(self, cfg: dict, engine, alert_dir: str, day_dir_fn, gpu=None,
                 mailer=None, replay: bool = False):
        self.cfg = cfg
        self.acfg = cfg.get("agent", {})
        self.alert_dir = alert_dir
        self.replay = replay
        self.mailer = mailer
        self.rb = Runbook(self.acfg.get("runbook", ""))
        self.builder = EvidenceBuilder(cfg, engine, gpu, self.rb)
        self.probe = GpuLogProbe(day_dir_fn, self.acfg.get("gpu_log_tail_mb", 16), replay)
        self.analyze_on = bool(self.acfg.get("enabled"))
        self.diag = RuleDiagnoser(self.rb, cfg)
        self.llm = (LLMAnalyst(cfg.get("llm", {}), self.rb,
                               bool(self.acfg.get("reason_first", True)))
                    if self.analyze_on and self.acfg.get("use_llm", True) else None)
        self.min_sev = sev_rank(self.acfg.get("min_severity", "crit"))
        if mailer is not None and mailer.enabled:
            # 메일 기준이 더 낮으면 그 심각도까지 사건으로 묶는다
            self.min_sev = min(self.min_sev, sev_rank(mailer.min_severity))
        self.batch_s = float(self.acfg.get("batch_seconds", 60))
        ecfg = cfg.get("email", {})
        self.imm_s = float(ecfg.get("immediate_seconds", 10))
        self.followup = bool(ecfg.get("llm_followup", True))
        self._batch: list = []
        self._states: dict = {}                 # id(경보) -> 경보 순간 검사 상태
        self._opened = 0.0
        self._imm_at = None                     # 즉시 등급 경보가 들어온 시각
        self._dropped = 0
        self._lock = threading.Lock()
        self.recent: list[dict] = []            # 상태 페이지용 최근 사건 요약
        self.processed = 0
        self._q: queue.Queue | None = None
        if not replay:
            self._q = queue.Queue()
            threading.Thread(target=self._worker, name="talog-agent",
                             daemon=True).start()

    # ── 메인 스레드 ─────────────────────────────────────────────
    def _mail_on(self) -> bool:
        return self.mailer is not None and self.mailer.enabled

    def submit(self, a):
        if sev_rank(a.severity) < self.min_sev:
            return
        if not self._batch:
            self._opened = a.ts
            self._dropped = 0
            self._states = {}
            self._imm_at = None
        if self._imm_at is None and self._mail_on() and self.mailer.is_immediate(a.severity):
            self._imm_at = a.ts                  # 심각 등급: 묶음을 기다리지 않는다
        if len(self._batch) < 60:
            self._batch.append(a)
            self._states[id(a)] = self.builder.capture(a)  # 경보 순간의 검사 상태
        else:
            self._dropped += 1

    def due(self):
        """열린 묶음을 마감할 시각 (없으면 None).

        즉시 등급: 들어온 뒤 immediate_seconds (같은 순간의 동반 경보만 모음, 시간당 상한만).
        그 밖: batch_seconds 뒤, 메일 간격이 막히면 최대 1시간 더 묶는다."""
        if not self._batch:
            return None
        if self._imm_at is not None:
            t = self._imm_at + self.imm_s
            return max(t, min(self.mailer.next_allowed(immediate=True), self._imm_at + 3600))
        t = self._opened + self.batch_s
        if self._mail_on():
            t = max(t, min(self.mailer.next_allowed(), self._opened + 3600))
        return t

    def tick(self, now: float, force: bool = False):
        if not self._batch:
            return
        if not force and now < self.due():
            return
        alerts, self._batch = self._batch, []
        inc = self._open(alerts, now)
        inc["immediate"] = self._imm_at is not None
        self._imm_at = None
        if self._mail_on():
            inc["_slot"] = self.mailer.reserve(now)
        if self._q is None:
            self._process(inc)
        else:
            self._q.put(inc)

    def flush(self, now: float, wait: float = 0.0):
        self.tick(now, force=True)
        if self._q is not None and wait > 0:
            t_end = time.time() + wait
            while self._q.unfinished_tasks and time.time() < t_end:
                time.sleep(0.2)

    def _family(self, a) -> tuple[str, str]:
        """경보 → (계열, system 원인). 패턴 경보는 설정의 family 를 따른다."""
        fam, cause = self.rb.family_of(a.rule)
        own = getattr(a, "family", "") or ""
        if own in self.rb.families and own != fam:
            return own, ("custom_pattern" if own == "system" else "")
        return fam, cause

    def _open(self, alerts: list, now: float) -> dict:
        top = max(sev_rank(a.severity) for a in alerts)
        cands = [a for a in alerts if sev_rank(a.severity) == top]
        primary = min(cands, key=lambda a: (_FAMILY_RANK.get(self._family(a)[0], 9), a.ts))
        ordered = [primary] + [a for a in alerts if a is not primary]
        family, sys_cause = self._family(primary)
        stamp = dt.datetime.fromtimestamp(primary.ts).strftime("%Y%m%d-%H%M%S")
        state = self._states.get(id(primary))
        self._states = {}
        return {"id": f"{stamp}-{primary.rule}", "site": self.cfg.get("site", ""),
                "opened": _hms(min(a.ts for a in alerts)), "closed": _hms(now),
                "t_trigger": primary.ts, "t_close": now, "family": family,
                "sys_cause": sys_cause, "alerts_obj": ordered,
                "alerts": [_alert_dict(a) for a in ordered], "dropped": self._dropped,
                "evidence": self.builder.snapshot(ordered, now, state),
                "replay": self.replay}

    # ── 작업 스레드 ─────────────────────────────────────────────
    def _worker(self):
        while True:
            inc = self._q.get()
            try:
                if inc is None:                     # close() — 남은 사건을 처리한 뒤 종료
                    return
                self._process(inc)
            finally:
                self._q.task_done()

    def close(self):
        if self._q is not None:
            self._q.put(None)

    def _set_verdict(self, inc: dict, fam: str, rule, llm, verdict: dict):
        inc.update(rule=rule, llm=llm, verdict=verdict)
        inc["family_title"] = (self.rb.families.get(fam) or {}).get("title", fam)
        inc["action_text"] = {a: self.rb.action_ko(a) for a in verdict["actions"]}
        inc["role_text"] = {r: self.rb.role_ko(r) for r in verdict["notify"]}

    def _mail_ok(self, verdict: dict) -> bool:
        return self._mail_on() and \
            sev_rank(verdict["severity"]) >= sev_rank(self.mailer.min_severity)

    def _process(self, inc: dict):
        try:
            alerts = inc.pop("alerts_obj")
            ev = inc["evidence"]
            fam = inc["family"]
            slot = inc.pop("_slot", None)
            mail = None
            if self.analyze_on:
                ev["gpu_log"] = self.probe.scan(inc["t_trigger"], inc["t_close"],
                                                self.rb.params["gpu_fault_window_s"])
                ev["key_facts"] = interpret(ev, self.rb.params, fam)
                rule = self.diag.diagnose(fam, ev, alerts, inc["sys_cause"])
                inc["similar"] = self._similar(alerts, rule)
                if inc.get("immediate") and self.llm is not None and self.followup:
                    # 심각 등급: 룰 판단으로 먼저 보내고, LLM 2차 의견은 후속 메일로
                    v0 = gate(fam, rule, None, alerts, self.rb)
                    v0["label"] = v0["label"].replace(
                        "룰 판단 (LLM 미사용)", "룰 판단 (즉시 발송) — LLM 2차 의견은 후속 메일")
                    self._set_verdict(inc, fam, rule, None, v0)
                    if self._mail_ok(v0):
                        mail = self.mailer.send_incident(inc)
                    inc["verdict_initial"], inc["email_initial"] = v0, mail
                    llm = self.llm.analyze(fam, ev)
                    verdict = gate(fam, rule, llm, alerts, self.rb)
                    self._set_verdict(inc, fam, rule, llm, verdict)
                    if mail is not None and llm.get("ok"):
                        tag = {"agree": "[LLM 2차 의견: 판단 일치]",
                               "disagree": "[LLM 2차 의견: 판단 불일치 — 확인 필요]",
                               "llm_only": "[LLM 2차 의견: 참고]"}.get(verdict["mode"],
                                                                     "[LLM 2차 의견]")
                        inc["email_followup"] = self.mailer.send_incident(
                            inc, reply_to=mail.get("message_id", ""), tag=tag,
                            suffix="_llm")
                else:
                    llm = self.llm.analyze(fam, ev) if self.llm else None
                    verdict = gate(fam, rule, llm, alerts, self.rb)
                    self._set_verdict(inc, fam, rule, llm, verdict)
                    if self._mail_ok(verdict):
                        mail = self.mailer.send_incident(inc)
            else:
                rule, llm = None, None
                verdict = {"mode": "none", "label": "분석 비활성 (경보 전달만)",
                           "cause": "", "cause_name": "",
                           "actions": [], "notify": [],
                           "severity": max((a.severity for a in alerts), key=sev_rank),
                           "message": alerts[0].title + " — " + alerts[0].evidence,
                           "message_source": "alert"}
                self._set_verdict(inc, fam, rule, llm, verdict)
                if self._mail_ok(verdict):
                    mail = self.mailer.send_incident(inc)
            if mail is None and slot is not None and self._mail_on():
                self.mailer.release(slot)           # 보내지 않은 사건은 발송 몫 반납
            inc["email"] = inc.get("email_initial") or mail
            self._persist(inc)
            with self._lock:
                self.processed += 1
                self.recent.append({
                    "id": inc["id"], "time": inc["opened"],
                    "title": inc["alerts"][0]["title"], "severity": verdict["severity"],
                    "cause": verdict.get("cause_name") or "-", "mode": verdict["label"],
                    "email": ("발송" if mail and mail.get("sent") else
                              "보관(outbox)" if mail and mail.get("eml") else
                              ("실패" if mail else "-"))})
                self.recent = self.recent[-20:]
            lm = ""
            if llm:
                lm = (f" | LLM {llm.get('root_cause', '실패')} "
                      f"({llm.get('device')}, {llm.get('wall_s')}s)")
            print(f"  [사건 분석] {inc['id']}: {verdict.get('cause_name') or '-'} — "
                  f"{verdict['label']}{lm}"
                  + (f" | 메일 {'발송' if mail.get('sent') else mail.get('error') or '보관'}"
                     if mail else ""))
        except Exception as e:      # 분석은 부가 기능 — 감시를 절대 죽이지 않는다
            print(f"  ! 사건 분석 실패(감시 계속): {type(e).__name__}: {e}")

    def _similar(self, alerts: list, rule: dict) -> list:
        if not self.acfg.get("casekb", True):
            return []
        try:
            from ..casekb import default_kb_dir, search_all, site_kb_dir
            q = alerts[0].title + " " + rule.get("name", "") + " " + " ".join(
                rule.get("facts", [])[:2])
            # 기본 사례 KB + 이 설비에서 사람이 남긴 해결 기록(콘솔 '해결 기록' → data_dir/kb)
            return [{"score": round(s, 2), "title": c.title, "cause": c.cause,
                     "action": c.action, "site": c.site, "date": c.date}
                    for s, c in search_all(q, 2, [default_kb_dir(), site_kb_dir(self.alert_dir)])]
        except Exception:
            return []

    def _persist(self, inc: dict):
        day = dt.datetime.fromtimestamp(inc["t_trigger"]).strftime("%Y%m%d")
        suffix = "_replay" if self.replay else ""
        path = os.path.join(self.alert_dir, f"incidents_{day}{suffix}.jsonl")
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(inc, ensure_ascii=False, default=str) + "\n")
        except OSError as e:
            print(f"  ! 사건 기록 실패(계속): {e}")

    def status_rows(self) -> list[dict]:
        with self._lock:
            return list(self.recent)
