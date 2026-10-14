"""사건 — 목록·시간별 상태 기록·사건 상세·메일 미리보기·사건에 묻기(로컬 LLM)."""

from __future__ import annotations

import datetime as dt
import glob
import html
import json
import os
import re


class IncidentsMixin:
    """Console 이 물려받는다 — 공유 상태는 app.Console.__init__ 에 있다."""

    # ── 사건 ───────────────────────────────────────────────────
    def incidents(self, days: int = 3, replay: bool = True, limit: int = 200) -> list[dict]:
        files = sorted(glob.glob(os.path.join(self.data_dir, "incidents_*.jsonl")),
                       reverse=True)
        cutoff = (dt.datetime.now() - dt.timedelta(days=days - 1)).strftime("%Y%m%d")
        rows = []
        for p in files:
            base = os.path.basename(p)
            is_rep = base.endswith("_replay.jsonl")
            if is_rep and not replay:
                continue
            m = re.search(r"incidents_(\d{8})", base)
            if not is_rep and m and m.group(1) < cutoff:
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    lines = f.readlines()
            except OSError:
                continue
            for ln in lines:
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                v = d.get("verdict") or {}
                llm = d.get("llm") or {}
                em = d.get("email") or {}
                rows.append({
                    "id": d.get("id"), "replay": is_rep,
                    "day": (d.get("id") or "")[:8] or (m.group(1) if m else ""),
                    "time": d.get("opened"), "site": d.get("site"),
                    "title": (d.get("alerts") or [{}])[0].get("title", ""),
                    "n_alerts": len(d.get("alerts") or []),
                    "severity": v.get("severity"), "cause": v.get("cause_name"),
                    "llm": (llm.get("root_cause") if llm.get("ok") else
                            ("실패" if llm else "")),
                    "mode": v.get("label"), "gate": v.get("mode"),
                    "mail": ("발송" if em.get("sent") else "보관" if em.get("eml") else
                             ("실패" if em.get("error") else "-"))})
        notes = self.incident_notes()                 # 사람이 남긴 상태 (feedback.py)
        for r in rows:
            n = notes.get((r["id"], r["replay"]))
            r["state"] = n[-1].get("state", "open") if n else "open"
        rows.sort(key=lambda r: (r["day"], r["time"] or ""), reverse=True)
        return rows[:limit]

    def timeline(self, days: int = 7) -> dict:
        """날짜별·시간별 경보(심각·주의·정보 수)와 처리량·거부·최저 일시 지연 내성 — 운영 현황의 상태 띠와 추세.
        실시간 감시 기록과 경보 재현 기록을 따로 줄로 낸다. 줄마다 마지막 기록 시간의 그룹별 값(last)도 붙인다
        — 실시간 처리 여유가 아직 없을 때 화면이 최근 기록으로 그룹별 여유를 보여 준다."""
        rows: dict[tuple[str, bool], dict] = {}
        hours: dict[tuple[str, bool], dict[int, list]] = {}

        def row(day: str, rep: bool) -> dict:
            return rows.setdefault((day, rep), {
                "day": day, "replay": rep, "alerts": [[0, 0, 0] for _ in range(24)],
                "n": [None] * 24, "groups": [0] * 24, "rejects": [None] * 24, "tol_pct": [None] * 24,
                "last": None})
        sev_i = {"crit": 0, "warn": 1, "info": 2}
        for p in glob.glob(os.path.join(self.data_dir, "alerts_*.jsonl")):
            m = re.search(r"alerts_(\d{8})(_replay)?\.jsonl$", os.path.basename(p))
            if not m:
                continue
            r = row(m.group(1), bool(m.group(2)))
            try:
                with open(p, encoding="utf-8") as f:
                    for ln in f:
                        try:
                            a = json.loads(ln)
                            h = int(str(a.get("ts", ""))[11:13])
                        except (ValueError, TypeError):
                            continue
                        if 0 <= h < 24 and a.get("severity") in sev_i:
                            r["alerts"][h][sev_i[a["severity"]]] += 1
            except OSError:
                continue
        for p in glob.glob(os.path.join(self.data_dir, "kpi_*.jsonl")):
            m = re.search(r"kpi_(\d{8})(_replay)?\.jsonl$", os.path.basename(p))
            if not m:
                continue
            r = row(m.group(1), bool(m.group(2)))
            try:
                with open(p, encoding="utf-8") as f:
                    for ln in f:
                        try:
                            k = json.loads(ln)
                            h = int(str(k.get("hour", ""))[11:13])
                        except (ValueError, TypeError):
                            continue
                        if not 0 <= h < 24 or not isinstance(k.get("group"), int):
                            continue
                        # 한 검사가 모든 Seq 그룹을 지나므로 검사 수는 그룹 합이 아니라 그룹 중 최대
                        r["n"][h] = max(r["n"][h] or 0, int(k.get("n") or 0))
                        r["groups"][h] += 1
                        r["rejects"][h] = (r["rejects"][h] or 0) + int(k.get("rejects") or 0)
                        lim = (k.get("pool") or 0) * (k.get("interval_s") or 0)
                        if lim > 0 and k.get("hold_p99") is not None:
                            pct = round(100 * (lim - k["hold_p99"]) / lim, 1)
                            r["tol_pct"][h] = pct if r["tol_pct"][h] is None else min(r["tol_pct"][h], pct)
                        hours.setdefault((r["day"], r["replay"]), {}).setdefault(h, []).append(
                            {x: k.get(x) for x in ("group", "n", "idle_median", "hold_p50", "hold_p99",
                                                   "pool", "interval_s", "rejects")})
            except OSError:
                continue
        for key, by_h in hours.items():
            h = max(by_h)
            rows[key]["last"] = {"hour": h, "groups": sorted(by_h[h], key=lambda g: g["group"])}
        today = dt.datetime.now().strftime("%Y%m%d")
        row(today, False)                                  # 오늘 줄은 기록이 없어도 보인다
        out = sorted(rows.values(), key=lambda r: (r["day"], not r["replay"]), reverse=True)
        return {"today": today, "hour": dt.datetime.now().hour, "days": out[:max(1, min(int(days or 7), 31))]}

    def incident(self, iid: str, replay: bool) -> dict | None:
        pat = "incidents_*_replay.jsonl" if replay else "incidents_[0-9]*[0-9].jsonl"
        for p in sorted(glob.glob(os.path.join(self.data_dir, pat)), reverse=True):
            try:
                with open(p, encoding="utf-8") as f:
                    for ln in f:
                        if f'"id": "{iid}"' in ln:
                            d = json.loads(ln)
                            day = (d.get("id") or "")[:8]
                            if day.isdigit():
                                d["day_dir"] = self._find_day_dir(day)
                            return d
            except (OSError, ValueError):
                continue
        return None

    def _find_day_dir(self, day: str) -> str:
        """사건 날짜(YYYYMMDD)의 원본 로그 폴더 — 설정의 로그 루트와 콘솔에서 고른 폴더에서 찾는다(재현 사건 포함). 없으면 ''."""
        from .. import logread
        roots = [self.cfg.get("watch_root", "")] + [r for r in self._log_cfg().get("roots", []) if isinstance(r, str)]
        try:
            return logread.resolve_day([r for r in roots if r], day)
        except ValueError:
            return ""

    def mail_html(self, iid: str, replay: bool, which: str = "initial") -> str:
        from ..mailer import Mailer
        inc = self.incident(iid, replay)
        if inc is None:
            return "<p>사건을 찾을 수 없습니다.</p>"
        em = inc.get("email_followup") if which == "followup" else inc.get("email")
        path = (em or {}).get("eml", "")
        if path and os.path.exists(path):
            import email
            from email import policy
            with open(path, "rb") as f:
                msg = email.message_from_binary_file(f, policy=policy.default)
            part = msg.get_body(preferencelist=("html",))
            if part is not None:
                subj = html.escape(str(msg["Subject"]))
                to = html.escape(str(msg["To"]))
                return (f"<div style='font:12px sans-serif;color:#555;margin-bottom:8px'>"
                        f"<b>제목</b> {subj}<br><b>받는 사람</b> {to}</div>"
                        + part.get_content())
        return Mailer(self.cfg, self.data_dir, replay=True).body_html(inc)

    def ask_incident(self, iid: str, replay: bool, question: str) -> dict:
        """사건 기록 안의 사실만으로 LLM 이 답한다 (도구 없음, 근거 밖은 모른다고)."""
        from ..llm import OllamaClient
        inc = self.incident(iid, replay)
        if inc is None:
            return {"ok": False, "error": "사건을 찾을 수 없습니다"}
        question = (question or "").strip()[:500]
        if not question:
            return {"ok": False, "error": "질문이 비어 있습니다"}
        ev = inc.get("evidence") or {}
        ctx = {"사건": inc.get("id"), "설비": inc.get("site"), "시각": inc.get("opened"),
               "계열": inc.get("family_title") or inc.get("family"),
               "판단": inc.get("verdict"), "룰 진단": inc.get("rule"),
               "LLM 2차 의견": {k: (inc.get("llm") or {}).get(k) for k in
                             ("root_cause", "evidence", "analysis") if inc.get("llm")},
               "경보": inc.get("alerts"), "핵심 사실": ev.get("key_facts"),
               "근거 로그": (ev.get("log_lines") or [])[-40:]}
        text = json.dumps(ctx, ensure_ascii=False, default=str)[:14000]
        msgs = [{"role": "system", "content":
                 "너는 비전 검사 설비의 사건 분석 보조다. 아래 사건 기록에 있는 사실만으로 "
                 "한국어로 짧게 답한다. 기록에 없는 수치·원인은 지어내지 말고 '기록에 없음'"
                 "이라고 답한다. 조치를 물으면 기록의 권고 조치를 우선한다."},
                {"role": "user", "content": f"[사건 기록]\n{text}\n\n[질문]\n{question}"}]
        r = OllamaClient(self.cfg["llm"]).chat(msgs, num_predict=600)
        if r["error"]:
            return {"ok": False, "error": r["error"], "device": r["device"]}
        return {"ok": True, "answer": r["content"].strip(), "device": r["device"],
                "wall_s": r["wall_s"], "model": self.cfg["llm"].get("model")}
