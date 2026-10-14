"""사건 마무리·학습 — 사람이 남긴 사건 상태(확인·해결·오경보)와 해결 기록, 규칙별 경보·오경보 통계.

    data_dir/incident_notes.jsonl   사건 표시 기록 — 한 줄 = 한 번의 표시, 같은 사건은 마지막 줄이 지금 상태
    data_dir/kb/cases.jsonl         '해결 기록'을 사례로 — 에이전트가 다음 사건에 '비슷한 과거 사례'로 붙인다
                                    (agent/incident.py 가 기본 사례 KB 와 함께 찾는다, casekb.search_all)

규칙 피드백(rule_stats)은 경보 기록과 오경보 표시로 규칙마다 최근 경보·오경보 수를 세어, 화면이 오경보가 많은 규칙에
조건 조정을 권하게 한다. 사건 표시는 모드와 상관없이 할 수 있다(현장 작업자의 일상 일이다).
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import os
import re

STATES = {"open": "새 사건", "ack": "확인 중", "resolved": "해결", "false_alarm": "오경보"}
NOTES = "incident_notes.jsonl"


class FeedbackMixin:
    """Console 이 물려받는다 — 공유 상태는 app.Console.__init__ 에 있다."""

    def incident_notes(self) -> dict[tuple[str, bool], list[dict]]:
        """(사건 id, 재현인가) → 그 사건의 표시 기록들(남긴 순서)."""
        out: dict[tuple[str, bool], list[dict]] = {}
        try:
            with open(os.path.join(self.data_dir, NOTES), encoding="utf-8") as f:
                for ln in f:
                    try:
                        n = json.loads(ln)
                    except ValueError:
                        continue
                    if isinstance(n, dict) and n.get("id"):
                        out.setdefault((str(n["id"]), bool(n.get("replay"))), []).append(n)
        except OSError:
            pass
        return out

    def incident_detail(self, iid: str, replay: bool) -> dict | None:
        """사건 기록 + 사람이 남긴 표시 기록(notes)과 지금 상태(state)."""
        d = self.incident(iid, replay)
        if d is None:
            return None
        notes = self.incident_notes().get((iid, bool(replay)), [])
        d["notes"] = notes
        d["state"] = notes[-1].get("state", "open") if notes else "open"
        return d

    def set_incident_state(self, iid: str, replay: bool, state: str, cause: str = "", action: str = "",
                           note: str = "", save_case: bool = False) -> tuple[int, dict]:
        if state not in STATES:
            return 400, {"ok": False, "error": f"모르는 상태: {state}"}
        inc = self.incident(iid, replay)
        if inc is None:
            return 404, {"ok": False, "error": "사건을 찾을 수 없습니다"}
        rec = {"id": iid, "replay": bool(replay), "state": state, "cause": str(cause or "").strip()[:300],
               "action": str(action or "").strip()[:500], "note": str(note or "").strip()[:500],
               "time": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "mode": self.mode}
        if state == "resolved" and save_case:
            rec["case"] = self._save_case(inc, rec)
        os.makedirs(self.data_dir, exist_ok=True)
        with open(os.path.join(self.data_dir, NOTES), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"[콘솔] 사건 {iid}{' (재현)' if replay else ''} → {STATES[state]}"
              + (f" · 사례 저장 {rec['case']}" if rec.get("case") else ""))
        notes = self.incident_notes().get((iid, bool(replay)), [])
        return 200, {"ok": True, "state": state, "case": rec.get("case", ""), "notes": notes}

    def _save_case(self, inc: dict, rec: dict) -> str:
        """해결 기록을 이 설비의 사례 KB 에 한 건으로 — 같은 사건을 다시 저장하면 새로 더하지 않는다."""
        from ..casekb import Case, CaseKB, site_kb_dir
        kb = CaseKB(site_kb_dir(self.data_dir))
        cid = f"talog-{inc.get('id')}"
        if any(c.id == cid for c in kb.cases):
            return cid
        v, ev = inc.get("verdict") or {}, inc.get("evidence") or {}
        alerts = inc.get("alerts") or [{}]
        acts = v.get("actions") or []
        first = (inc.get("action_text") or {}).get(acts[0], acts[0]) if acts else ""
        day = str(inc.get("id") or "")[:8]
        kb.add(Case(id=cid, title=alerts[0].get("title", "") or str(inc.get("id")),
                    symptom=" · ".join([a.get("title", "") for a in alerts[:3]] + list(ev.get("key_facts") or [])[:3])[:600],
                    cause=rec["cause"] or v.get("cause_name", ""), action=rec["action"] or first,
                    site=str(inc.get("site") or ""),
                    date=f"{day[:4]}-{day[4:6]}-{day[6:]}" if day.isdigit() else "",
                    tags=[t for t in (inc.get("family"), v.get("cause"), "talog 해결 기록") if t]))
        return cid

    def rule_stats(self, days: int = 7) -> dict:
        """규칙별 경보 수와 그중 오경보로 표시된 사건의 경보 수 — 실시간(최근 days 일)과 재현(전부)을 따로."""
        days = max(1, min(int(days or 7), 31))
        cutoff = (dt.datetime.now() - dt.timedelta(days=days - 1)).strftime("%Y%m%d")
        out: dict = {"days": days, "live": {}, "replay": {}}

        def bump(kind: str, rule: str, key: str) -> None:
            r = out[kind].setdefault(rule, {"alerts": 0, "false": 0})
            r[key] += 1
        for p in glob.glob(os.path.join(self.data_dir, "alerts_*.jsonl")):
            m = re.search(r"alerts_(\d{8})(_replay)?\.jsonl$", os.path.basename(p))
            if not m or (not m.group(2) and m.group(1) < cutoff):
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    for ln in f:
                        try:
                            a = json.loads(ln)
                        except ValueError:
                            continue
                        if a.get("rule"):
                            bump("replay" if m.group(2) else "live", str(a["rule"]), "alerts")
            except OSError:
                continue
        for (iid, rep), recs in self.incident_notes().items():
            if recs[-1].get("state") != "false_alarm" or (not rep and iid[:8] < cutoff):
                continue
            inc = self.incident(iid, rep)
            for a in (inc or {}).get("alerts") or []:            # 오경보 사건에 묶인 경보 수 (경보 수와 같은 단위)
                if a.get("rule"):
                    bump("replay" if rep else "live", str(a["rule"]), "false")
        return out
