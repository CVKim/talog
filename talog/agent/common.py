"""에이전트 공통 — 심각도·사건 계열 순위, 기본 매개변수, 에러 종류, 시각·반올림·경보 직렬화 도구."""

from __future__ import annotations

import datetime as dt


_SEV_RANK = {"info": 0, "warn": 1, "crit": 2}


_FAMILY_RANK = {"reject": 0, "defect": 1, "system": 2}


_DEFAULT_PARAMS = {"gpu_fault_window_s": 300, "not_init_after_restart_s": 300,
                   "slowdown_ratio": 1.5, "exec_slowdown_ratio": 1.5,
                   "recipe_change_window_min": 60, "restart_merge_s": 30,
                   "drift_ratio": 1.2, "interval_tol_pct": 10}


_ERROR_KINDS = ("ERROR", "EXC_REDIRECT", "MODEL_FAIL", "CRASH", "COMM_FAIL",
                "RECIPE_FAIL", "EXC_SAFE")


def sev_rank(s: str) -> int:
    return _SEV_RANK.get(str(s or "").lower(), 0)


def _hms(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else None


def _r(x, nd=1):
    return None if x is None else round(float(x), nd)


def _alert_dict(a) -> dict:
    return {"time": _hms(a.ts), "severity": a.severity, "rule": a.rule,
            "title": a.title, "evidence": a.evidence}
