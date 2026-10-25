"""설비 건강 (talog/health.py) 에 넣을 기록을 모은다 — 시간별 KPI·기준선·판정 기록·GPU·오늘 사건.

실시간 기록(kpi_YYYYMMDD.jsonl)이 최근 3시간 안에 있으면 '지금' 기준으로, 없으면(개발자 PC·데모) 가장 최근 재현 기록을
'지난 기록' 기준으로 계산한다 — 화면은 지난 기록을 '지금'이라 쓰지 않는다. 30초 동안 결과를 둔다.
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import os
import re
import time

from .. import health as H


def _rows(paths: list[str]) -> list[dict]:
    out = []
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                for ln in f:
                    try:
                        r = json.loads(ln)
                    except ValueError:
                        continue
                    if isinstance(r, dict) and r.get("hour"):
                        out.append(r)
        except OSError:
            continue
    return sorted(out, key=lambda r: r["hour"])


class HealthMixin:
    """Console 이 물려받는다 — 공유 상태는 app.Console.__init__ 에 있다."""

    def _kpi_files(self, replay: bool, days: int = 30) -> list[str]:
        cut = (dt.datetime.now() - dt.timedelta(days=days)).strftime("%Y%m%d")
        out = []
        for p in glob.glob(os.path.join(self.data_dir, "kpi_*.jsonl")):
            m = re.search(r"kpi_(\d{8})(_replay)?\.jsonl$", os.path.basename(p))
            if m and bool(m.group(2)) == replay and (replay or m.group(1) >= cut):
                out.append(p)
        return sorted(out)

    def _baseline(self, replay: bool) -> dict:
        try:
            with open(os.path.join(self.data_dir, f"capacity_state{'_replay' if replay else ''}.json"), encoding="utf-8") as f:
                st = json.load(f)
            return st if isinstance(st, dict) else {}
        except (OSError, ValueError):
            return {}

    def _rule_value(self, name: str, key: str, default: float) -> float:
        v = ((self.settings.get("rules") or {}).get("builtin") or {}).get(name) or {}
        try:
            return float(v.get(key, default))
        except (TypeError, ValueError):
            return default

    def health(self) -> dict:
        now = time.time()
        cache = getattr(self, "_health_cache", None)
        if cache and now - cache[0] < 30:
            return cache[1]
        live = _rows(self._kpi_files(False))
        recent = (dt.datetime.now() - dt.timedelta(hours=3)).strftime("%Y-%m-%d %H:00")
        basis, rows = "live", live
        if not any(r["hour"] >= recent for r in live):
            rep = _rows(self._kpi_files(True))
            if rep:
                basis, rows = "replay", rep
            elif not live:
                basis = "none"
        out = self._health_calc(rows, basis)
        self._health_cache = (now, out)
        return out

    def _health_calc(self, rows: list[dict], basis: str, current: bool = True) -> dict:
        """rows(시간순 KPI 행)로 설비 건강 — 지금 화면과 하루 정리(daily.py, 그날 끝까지의 행)가 함께 쓴다"""
        snap = self.watch.snapshot() if (self.watch and basis == "live" and current) else {}
        base = self._baseline(basis == "replay")
        gen_from = base.get("gen_start") or 0
        groups = sorted({r["group"] for r in rows if isinstance(r.get("group"), int)})
        live_cap = {c.get("group"): c for c in (snap.get("capacity") or []) if c.get("limit_s")}
        items = []
        for g in groups:
            gr = [r for r in rows if r.get("group") == g]
            b = (base.get("groups") or {}).get(str(g)) or {}
            since = dt.datetime.fromtimestamp(b["to"]).strftime("%Y-%m-%d %H:00") if b.get("to") else (
                dt.datetime.fromtimestamp(gen_from).strftime("%Y-%m-%d %H:00") if gen_from else "")
            ser = [(r["hour"], r.get("hold_p99")) for r in gr if not since or r["hour"] >= since]
            items.append(H.drift(g, ser, b.get("hold_p99")))
            c = live_cap.get(g)
            if c:
                p99, lim = c.get("hold_p99"), c.get("limit_s")
            else:
                last = gr[-1] if gr else {}
                p99 = last.get("hold_p99")
                lim = (last.get("pool") or 0) * (last.get("interval_s") or 0) or None
            items.append(H.spike(g, gr, p99, lim))
        items.append(H.quality([r for r in rows if r.get("group") == "quality"]))
        if basis == "live" and current:                 # 장비·오늘 사건은 지금 값 — 지난 날 정리에는 넣지 않는다
            items += H.device(snap.get("gpu") or [], self._rule_value("gpu_temp", "celsius", 85),
                              [r for r in rows if r.get("group") == "process"], self._rule_value("memory_trend", "mb_per_hour", 100))
            items.append(H.open_incidents(self.incidents(days=1, replay=False, limit=500)))
        out = H.summarize(items)
        last_hour = rows[-1]["hour"] if rows else ""
        out.update({"basis": basis, "last_hour": last_hour, "at": dt.datetime.now().strftime("%H:%M"),
                    "groups": len(groups), "quality_hours": sum(1 for r in rows if r.get("group") == "quality")})
        return out
