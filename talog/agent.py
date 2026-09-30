"""talog watch 사건 분석 에이전트 — 경보 → 근거 수집 → 룰 진단 + LLM 2차 의견 → 합의 → 알림.

Robot_Sim 운영 에이전트 벤치(2026-09-28, 트윈 고장 주입 403건)의 결론을 따른다.
  - 증상이 수치로 구조화된 경우 규칙이 9B 로컬 LLM 보다 정확하다(조치 정답 85% 대 55%).
  - LLM 단독은 해로운 조치가 36% 다. 그래서 규칙과 LLM 이 같은 원인을 말할 때만
    '판단 일치'로 내보내고, 다르면 두 의견을 붙여 사람에게 넘긴다(해로운 조치 6% → 3.5%).
  - 알림 문구 속 숫자·시각은 근거 데이터와 기계적으로 대조한다(지어낸 숫자 차단).
talog 는 설비를 직접 조작하지 않는다. '조치'는 담당자에게 권고하는 닫힌 어휘다.

흐름:
  Notifier.emit(경보) → IncidentAgent.submit → 묶음(batch_seconds) → tick() 이 마감
    → EvidenceBuilder.snapshot (메인 스레드: 룰 엔진 상태를 복사)
    → 작업 스레드: GPU 로그 조사 → RuleDiagnoser → LLMAnalyst → gate → Mailer / JSONL
리플레이는 같은 경로를 이벤트 시각 기준으로 동기 실행한다.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import queue
import re
import statistics
import threading
import time
from array import array
from collections import Counter
from fnmatch import fnmatchcase

import yaml

from .lineparser import _TS_RE
from .llm import OllamaClient, hanzi_count, parse_json

_RUNBOOK_PATH = os.path.join(os.path.dirname(__file__), "rules", "runbook.yaml")
_SEV_RANK = {"info": 0, "warn": 1, "crit": 2}
_FAMILY_RANK = {"reject": 0, "defect": 1, "system": 2}
_DEFAULT_PARAMS = {"gpu_fault_window_s": 300, "not_init_after_restart_s": 300,
                   "slowdown_ratio": 1.5, "exec_slowdown_ratio": 1.5,
                   "recipe_change_window_min": 60, "restart_merge_s": 30}
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


# ---------------------------------------------------------------------------
class Runbook:
    """rules/runbook.yaml — 원인·조치·담당 어휘와 LLM 이 읽는 원인 설명."""

    def __init__(self, path: str = ""):
        p = path if path and os.path.exists(path) else _RUNBOOK_PATH
        with open(p, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        self.path = p
        self.params = {**_DEFAULT_PARAMS, **(raw.get("params") or {})}
        self.roles: dict = raw.get("roles") or {}
        self.actions: dict = raw.get("actions") or {}
        self.alert_family: dict = raw.get("alert_family") or {}
        self.families: dict = raw.get("families") or {}

    def family_of(self, rule: str) -> tuple[str, str]:
        """경보 룰 → (계열, system 계열이면 원인 id). 모르는 룰은 system/unknown."""
        v = str(self.alert_family.get(rule, "system.unknown"))
        fam, _, cause = v.partition(".")
        return (fam if fam in self.families else "system"), cause

    def causes(self, family: str) -> dict:
        return (self.families.get(family) or {}).get("causes") or {}

    def cause_name(self, family: str, cause: str) -> str:
        if cause == "unknown":
            return "원인 미상"
        c = self.causes(family).get(cause) or {}
        return c.get("name", cause)

    def cause_spec(self, family: str, cause: str) -> dict:
        return self.causes(family).get(cause) or {}

    def action_ko(self, a: str) -> str:
        return str(self.actions.get(a, a))

    def role_ko(self, r: str) -> str:
        return str(self.roles.get(r, r))


# ---------------------------------------------------------------------------
class GpuLogProbe:
    """DLInfer.log 를 사건 시점에만 조사한다(상시 tail 하지 않음 — 저부하 원칙 유지).

    실시간: 오늘 폴더 DLInfer.log 의 꼬리 N MB 만 읽는다.
    리플레이: 파일 전체를 한 번 색인해 두고 시간 창으로 조회한다(미래 구간은 보지 않음).
    """

    _EXEC_RE = re.compile(r"Model: (\S+?), DeviceIdx:\d+, executeV2 Tact = ([\d.]+)")
    _ERR_RE = re.compile(r"INPUT_TENSOR_ERROR|EXECUTE_ERROR|VERIFY_OUTPUT_ERROR|"
                         r"OUTPUT_TENSOR_ERROR")

    def __init__(self, day_dir_fn, tail_mb: float = 16, replay: bool = False):
        self.day_dir_fn = day_dir_fn
        self.budget = int(max(1.0, float(tail_mb)) * 1048576)
        self.replay = replay
        self._index = None                   # 리플레이 색인 (fatal, err, exec)

    @staticmethod
    def _find(day_dir: str) -> str:
        try:
            for n in os.listdir(day_dir):
                if n.lower() == "dlinfer.log":
                    return os.path.join(day_dir, n)
        except OSError:
            pass
        return ""

    def _parse(self, lines, fatal: list, err: list, ex_ts, ex_ms, ex_model):
        for ln in lines:
            if "executeV2" in ln:
                kind = 0
            elif "enqueueV3" in ln:
                kind = 1
            elif "_ERROR" in ln:
                kind = 2
            else:
                continue
            m = _TS_RE.match(ln)
            if not m:
                continue
            yy, mo, dd, hh, mi, ss, ms = m.groups()
            try:
                ts = dt.datetime(int(yy), int(mo), int(dd), int(hh), int(mi),
                                 int(ss), int(ms) * 1000).timestamp()
            except (ValueError, OverflowError, OSError):
                continue
            if kind == 0:
                em = self._EXEC_RE.search(ln)
                if em:
                    ex_ts.append(ts)
                    ex_ms.append(float(em.group(2)))
                    ex_model.append(em.group(1))
            elif kind == 1 and "cudaGetLastError" in ln:
                fatal.append(ts)
            elif kind == 2 and self._ERR_RE.search(ln):
                err.append(ts)

    def _load(self):
        path = self._find(self.day_dir_fn() or "")
        fatal, err, ex_model = [], [], []
        ex_ts, ex_ms = array("d"), array("d")
        if not path:
            return path, (fatal, err, ex_ts, ex_ms, ex_model)
        if self.replay:
            if self._index is None:
                with open(path, "rb") as f:
                    lines = (raw.decode("utf-8", "replace") for raw in f
                             if b"executeV2" in raw or b"enqueueV3" in raw
                             or b"_ERROR" in raw)
                    self._parse(lines, fatal, err, ex_ts, ex_ms, ex_model)
                self._index = (fatal, err, ex_ts, ex_ms, ex_model)
            return path, self._index
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - self.budget))
            chunk = f.read(self.budget)
        text = chunk.decode("utf-8", "replace").split("\n")
        if size > self.budget and text:
            text = text[1:]                       # 잘린 첫 줄 버림
        self._parse((ln.rstrip("\r") for ln in text), fatal, err, ex_ts, ex_ms,
                    ex_model)
        return path, (fatal, err, ex_ts, ex_ms, ex_model)

    def scan(self, t_trigger: float, t_close: float, window_s: float) -> dict:
        try:
            path, (fatal, err, ex_ts, ex_ms, ex_model) = self._load()
        except OSError as e:
            return {"source": f"DLInfer.log 읽기 실패: {e}", "ok": False}
        if not path:
            return {"source": "DLInfer.log 없음", "ok": False}
        lo, hi = t_trigger - window_s, t_close
        near_f = [t for t in fatal if lo <= t <= hi]
        near_e = [t for t in err if lo <= t <= hi]
        out = {"source": os.path.basename(path) + (" (전체 색인)" if self.replay
                                                   else " (꼬리 조사)"), "ok": True,
               "fatal_near": len(near_f), "infer_errors_near": len(near_e),
               "first_fatal": _hms(min(near_f)) if near_f else None,
               "exec_samples": 0, "exec_slowdown_ratio": None,
               "exec_slowest_model": None}
        # 모델별 executeV2 중앙값: 경보 전 5분 vs 그 앞 25분 (모델 섞임 편향 방지)
        rec: dict[str, list] = {}
        bef: dict[str, list] = {}
        for t, v, mdl in zip(ex_ts, ex_ms, ex_model):
            if t_trigger - 300 <= t <= t_trigger:
                rec.setdefault(mdl, []).append(v)
            elif t_trigger - 1800 <= t < t_trigger - 300:
                bef.setdefault(mdl, []).append(v)
        out["exec_samples"] = sum(len(v) for v in rec.values())
        best = None
        for mdl, vs in rec.items():
            bs = bef.get(mdl) or []
            if len(vs) >= 20 and len(bs) >= 20:
                b = statistics.median(bs)
                if b > 0:
                    ratio = statistics.median(vs) / b
                    if best is None or ratio > best[0]:
                        best = (ratio, mdl, statistics.median(vs), b)
        if best:
            out["exec_slowdown_ratio"] = round(best[0], 2)
            out["exec_slowest_model"] = best[1]
            out["exec_p50_recent_ms"] = round(best[2], 1)
            out["exec_p50_before_ms"] = round(best[3], 1)
        return out


# ---------------------------------------------------------------------------
class EvidenceBuilder:
    """룰 엔진·GPU 감시기의 현재 상태를 LLM·룰이 같이 보는 근거 JSON 으로 만든다."""

    def __init__(self, cfg: dict, engine, gpu=None, runbook: Runbook | None = None):
        self.cfg = cfg
        self.engine = engine
        self.gpu = gpu
        self.rb = runbook or Runbook()

    def capture(self) -> dict:
        """경보 시점의 검사 상태 사본 — 마감(+batch_seconds) 전에 재시작이 끼면
        진행 중 목록이 비워지므로, 경보가 들어온 순간의 상태를 근거로 쓴다."""
        eng = self.engine
        return {"pending": dict(eng.pending), "done": list(eng.done),
                "durations": list(eng.durations)}

    def snapshot(self, alerts: list, t_close: float, state: dict | None = None) -> dict:
        eng = self.engine
        p = self.rb.params
        st = state or self.capture()
        t0 = min(a.ts for a in alerts)
        t_trig = alerts[0].ts
        rec = sorted((e for e in eng.recent if e.ts <= t_close), key=lambda e: e.ts)

        def within(kind_set, lo, hi=None):
            hi = t_close if hi is None else hi
            return [e for e in rec if e.kind in kind_set and lo <= e.ts <= hi]

        ev: dict = {"site": self.cfg.get("site", ""),
                    "window": {"from": _hms(t0 - 1800), "to": _hms(t_close)},
                    "trigger": _alert_dict(alerts[0]),
                    "n_alerts": len(alerts),
                    "alerts": [_alert_dict(a) for a in alerts[:12]]}
        # 사용자 패턴 경보: 걸린 원문 줄을 근거로 싣는다 (LLM 이 내용을 해석)
        pats = []
        for a in alerts:
            if a.rule == "pattern" and all(x["name"] != a.key for x in pats):
                smp = eng.patterns.samples(a.key) if getattr(eng, "patterns", None) else []
                pats.append({"name": a.key, "severity": a.severity,
                             "lines": [f"{_hms(t)} {f}: {x[:200]}" for t, f, x in smp
                                       if t <= t_close][-5:]})
        if pats:
            ev["patterns"] = pats[:4]

        # 검사 흐름 (경보 시점 상태) -------------------------------------------
        done = [x for x in st["done"] if x[0] <= t_trig]
        base_l = [d for t, _i, d in done if t < t_trig - 1800]    # 30분 이전 = 평소
        recent_d = [d for t, _i, d in done if t >= t_trig - 1800][-3:]
        if len(base_l) < 5:
            base_l = [d for _t, _i, d in done][:-3] or list(st["durations"])
        base = statistics.median(base_l) if len(base_l) >= 5 else None
        starts = within({"INSP_START"}, t0 - 1800, t_trig)
        gaps = [b.ts - a.ts for a, b in zip(starts, starts[1:]) if 0.5 < b.ts - a.ts < 1800]
        ends = [e for e in within({"COMM_MSG"}, t0 - 600) if "INSPECT_END" in e.name]
        # 1시간 넘게 남은 항목은 소실된 검사(END 미송신)라 진행 중으로 세지 않는다.
        # 거부된 검사도 뺀다 — InspStarter 의 도착 줄이 설비 회신(ACK)보다 먼저 읽히면
        # 경보 순간 사본에 '시작된 검사'로 남아 있기 때문 (실시간 종단 시험 9/29: 3건 → 2건)
        rejected = {e.inner_id for e in within({"COMM_MSG"}, t0 - 1800)
                    if "INSPECT_START_ACK" in e.name and e.status and e.status != "OK"}
        pend = {k: s for k, s in st["pending"].items()
                if 0 <= t_trig - s < 3600 and k not in rejected}
        interval = statistics.median(gaps) if gaps else None
        # '지금 한 건에 걸리는 시간' = 최근 완료 중앙값과 경보 순간 진행 중 최장 경과 중 큰 값
        cands = []
        if recent_d:
            cands.append(statistics.median(recent_d))
        if pend:
            cands.append(max(t_trig - s for s in pend.values()))
        cur = max(cands) if cands else None
        ev["inspections"] = {
            "baseline_duration_s": _r(base),
            "recent_durations_s": [_r(d) for d in recent_d],
            "slowdown_ratio": (_r(statistics.median(recent_d) / base, 2)
                               if base and recent_d else None),
            "inflight": len(pend),
            "inflight_ids": sorted(pend, key=pend.get)[:4],
            "oldest_inflight_s": _r(max(t_trig - s for s in pend.values())) if pend else None,
            "recent_interval_s": _r(interval),
            "duration_over_interval": _r(cur / interval, 2) if cur and interval else None,
            "wait_threads_recent": [int(e.value) for e in starts[-4:]],
            "starts_10min": sum(1 for e in starts if e.ts >= t_trig - 600),
            "ends_10min": len({e.inner_id for e in ends if e.ts <= t_trig}),
        }

        # 시작 거부 -------------------------------------------------------------
        rj = [e for e in within({"INSP_REJECT", "COMM_MSG"}, t0 - 1800)
              if e.kind == "INSP_REJECT" or ("INSPECT_START_ACK" in e.name
                                             and e.status == "NoInspThread")]
        rj_ts = []
        for e in rj:                               # 같은 거부의 두 흔적(1초 내) 병합
            if not rj_ts or e.ts - rj_ts[-1] > 1.0:
                rj_ts.append(e.ts)
        ev["rejects"] = {"count_30min": len(rj_ts), "first": _hms(rj_ts[0]) if rj_ts else None,
                         "last": _hms(rj_ts[-1]) if rj_ts else None,
                         "inner_ids": sorted({e.inner_id for e in rj if e.inner_id})[:5]}

        # 타임아웃 ---------------------------------------------------------------
        it = within({"IMG_TIMEOUT"}, t0 - 1800)
        at = within({"ALG_TIMEOUT"}, t0 - 1800)
        last_to = max([e.ts for e in it + at], default=0)
        ev["timeouts"] = {"img_timeout_30min": len(it), "alg_timeout_30min": len(at),
                          "last": _hms(last_to) if last_to else None}

        # 재시작 (풀 생성 5종 등 30초 안의 흔적은 1회) ---------------------------
        marks = [e.ts for e in within({"POOL_CREATE"}, t0 - 3600)]
        restarts: list[float] = []
        for t in marks:
            if not restarts or t - restarts[-1] > p["restart_merge_s"]:
                restarts.append(t)
        before = [t for t in restarts if t <= t0]
        kills = [e for e in within({"BATCH"}, t0 - 3600) if "kill" in e.name.lower()]
        ev["restarts"] = {"count_60min": len(restarts),
                          "count_30min": sum(1 for t in restarts if t >= t0 - 1800),
                          "last_restart": _hms(before[-1]) if before else None,
                          "since_restart_s": _r(t0 - before[-1], 0) if before else None,
                          "kill_60min": len(kills),
                          "pool_log_lines_60min": len(marks)}

        # 모델 로드 (기종 교체) ------------------------------------------------
        loads = [e for e in within({"COMM_MSG"}, t0 - 5400) if "MODEL_LOAD" in e.name]
        cmds = [e for e in loads if e.name.startswith("M2V_MODEL_LOAD")]
        acks = [e for e in loads if e.name.startswith("V2M_MODEL_LOAD_ACK")]
        last_cmd = cmds[-1] if cmds else None
        name = (last_cmd.extra.split(",")[-1].strip() if last_cmd else None)
        prev = (cmds[-2].extra.split(",")[-1].strip() if len(cmds) >= 2 else None)
        ev["model_load"] = {
            "last_cmd": _hms(last_cmd.ts) if last_cmd else None,
            "name": name, "prev_name": prev,
            "changed": (name != prev) if (name and prev) else None,
            "minutes_since": _r((t0 - last_cmd.ts) / 60) if last_cmd else None,
            "pending": bool(last_cmd and not any(a.ts >= last_cmd.ts for a in acks)
                            and t_close - last_cmd.ts < 900),
        }

        # 에러 ---------------------------------------------------------------
        errs = within(set(_ERROR_KINDS), t0 - 1800)
        keys = Counter((e.model or (e.extra or e.name or e.kind))[:60] for e in errs
                       if e.kind != "EXC_SAFE")
        w = p["gpu_fault_window_s"]
        ev["errors"] = {
            "count_30min": len(errs),
            "top": [[k, n] for k, n in keys.most_common(4)],
            "seh_near": sum(1 for e in errs if e.kind == "EXC_SAFE" and e.ts >= t0 - w),
            "seh_codes": sorted({e.status for e in errs if e.kind == "EXC_SAFE"})[:3],
            "crash_near": any(e.kind == "CRASH" and e.ts >= t0 - w for e in errs),
            "grab_fail_30min": len(within({"GRAB_FAIL"}, t0 - 1800)),
            "light_unstable_30min": len(within({"LIGHT_UNSTABLE"}, t0 - 1800)),
            "emergency_stop_30min": sum(1 for e in within({"COMM_MSG"}, t0 - 1800)
                                        if "EMERGENCY" in e.name),
        }

        ev["gpu_log"] = {"source": "미조사"}      # 작업 스레드가 GpuLogProbe 로 채운다

        # GPU 현재 상태 (nvidia-smi, 실시간만) --------------------------------
        gnow = []
        if self.gpu is not None and getattr(self.gpu, "hist", None):
            last = self.gpu.hist[-1][1]
            peak: dict[int, float] = {}
            for t, gs in self.gpu.hist:
                if t >= t0 - 1800:
                    for g in gs:
                        peak[g["gpu"]] = max(peak.get(g["gpu"], 0.0), g["temp"])
            gnow = [{"gpu": g["gpu"], "temp_c": g["temp"], "util_pct": g["util"],
                     "mem_mb": g["mem"], "temp_max_30min": peak.get(g["gpu"])}
                    for g in last]
        ev["gpu_now"] = gnow

        # 메모리 (ProcessUsage 는 여러 프로세스가 섞여 5분 버킷 상한 포락선으로 본다) --
        buckets: dict[int, float] = {}
        for t, v in eng.mem:
            if t0 - 3600 <= t <= t_close:
                b = int(t // 300)
                buckets[b] = max(buckets.get(b, 0.0), v)
        ks = sorted(buckets)
        ev["memory"] = {"ram_mb": _r(buckets[ks[-1]], 0) if ks else None,
                        "rise_mb_60min": _r(buckets[ks[-1]] - buckets[ks[0]], 0)
                        if len(ks) >= 2 else None}

        # 결함 판정 -------------------------------------------------------------
        ev["defects"] = self._defects(t0, t_close, last_cmd.ts if last_cmd else None)

        # 트리거 전후 핵심 로그 (요약 문장) --------------------------------------
        ev["log_lines"] = self._log_lines(rec, t0)
        return ev

    def _defects(self, t0: float, t_close: float, change_ts) -> dict:
        eng = self.engine
        dw = self.cfg["rules"].get("defect_watch", {})
        items = [(k, v) for k, v in eng.results.items() if v["ts"] <= t_close]
        recent = items[-50:]
        base = items[:-50][-500:]

        def rate(lst):
            return (round(100.0 * sum(1 for _k, v in lst if v["result"] in ("NG", "REWORK"))
                          / len(lst), 1) if lst else None)

        hist = [(t, d, i) for t, d, i in eng.defect_hist if t0 - 1800 <= t <= t_close]
        pats = [str(x) for x in (dw.get("critical") or [])]
        crit = [{"name": d, "inner_id": i, "time": _hms(t)} for t, d, i in hist
                if any(fnmatchcase(d.upper(), q.upper()) for q in pats)]
        last_ng = [{"time": _hms(v["ts"]), "inner_id": k, "defects": v["defects"][:4]}
                   for k, v in items if v["result"] in ("NG", "REWORK")][-5:]
        before = [(k, v) for k, v in items if change_ts and v["ts"] < change_ts][-200:]
        return {"inspections_recent": len(recent),
                "ng_recent": sum(1 for _k, v in recent if v["result"] in ("NG", "REWORK")),
                "ng_rate_recent": rate(recent), "ng_rate_baseline": rate(base),
                "ng_rate_before_change": rate(before) if len(before) >= 20 else None,
                "streak": eng.ng_streak_len(),
                "top": [[d, n] for d, n in Counter(d for _t, d, _i in hist).most_common(5)],
                "critical_hits": crit[-10:], "last_ng": last_ng}

    @staticmethod
    def _log_lines(rec: list, t0: float, cap: int = 14) -> list[str]:
        out: list[tuple[float, str]] = []
        last_pool = 0.0
        for e in rec:
            if not (t0 - 180 <= e.ts <= t0 + 120):
                continue
            t = _hms(e.ts)
            k = e.kind
            if k == "INSP_START":
                s = f"{t} 검사 시작 inner={e.inner_id} 대기스레드={int(e.value)}"
            elif k == "INSP_REJECT":
                s = f"{t} 시작 거부: All Seq thread is running or not initialized"
            elif k == "COMM_MSG":
                s = f"{t} {e.name},{e.extra[:110]}"
            elif k == "IMG_TIMEOUT":
                s = f"{t} 이미지 처리 타임아웃 inner={e.inner_id}"
            elif k == "ALG_TIMEOUT":
                s = f"{t} 알고리즘 타임아웃 {e.value:.0f}ms (이미지 {e.roi_idx})"
            elif k == "POOL_CREATE":
                if e.ts - last_pool < 30:
                    continue
                last_pool = e.ts
                s = f"{t} 프로세스 기동 (WorkerThreadPool 생성)"
            elif k == "POOL_DESTROY":
                s = f"{t} 스레드 풀 정지 ({e.name})"
            elif k == "BATCH":
                s = f"{t} 배치 스크립트 {e.name}"
            elif k == "CRASH":
                s = f"{t} unhandled exception (크래시)"
            elif k == "EXC_SAFE":
                s = f"{t} SEH 예외 {e.status} @ {e.block}"
            elif k in _ERROR_KINDS:
                s = f"{t} 에러 {(e.model or e.extra or e.name)[:100]}"
            else:
                s = f"{t} {k} {e.name or e.extra[:80]}".strip()
            out.append((abs(e.ts - t0), s))
        keep = sorted(sorted(out)[:cap], key=lambda x: x[1][:8])
        return [s for _d, s in keep]


# ---------------------------------------------------------------------------
def interpret(ev: dict, p: dict, family: str = "") -> list[str]:
    """근거 수치를 '무엇을 뜻하는지' 문장으로 옮긴다 — 원인 이름은 쓰지 않는다.

    소형 LLM 은 JSON 속 숫자를 잘못 읽는다(Tenneco 0730 11:21: gpu_log.fatal_near 38 을
    보고도 'GPU 오류 없음'). Robot_Sim 벤치에서 도구 출력에 해석 문장을 붙이자 qwen3.5 조치
    정답 49→61%, 해로운 조치 34→22%. 문턱은 룰과 같은 런북 params 를 쓰고, 문장을 묶어
    원인을 정하는 일은 LLM 에게 남긴다. family 를 주면 그 계열 원인을 가르는 문장만 싣는다
    (결함 사건에 GPU·재시작 문장이 섞이면 qwen2.5:7b 가 '원인 미상'으로 물러섬, 9/29 실측)."""
    facts = _interpret_all(ev, p)
    if family == "defect":
        keep = ("치명 결함", "연속 NG", "NG 비율", "타임아웃", "그랩", "조명", "모델 로드",
                "기종", "사용자 패턴")
    elif family == "reject":
        keep = ("GPU", "인퍼런스", "SEH", "크래시", "진행 중", "소요", "커널", "타임아웃",
                "재시작", "모델 로드", "사용자 패턴", "비상 정지")
    else:
        return facts
    return [f for f in facts if any(k in f for k in keep)]


def _interpret_all(ev: dict, p: dict) -> list[str]:
    out: list[str] = []
    w = int(p["gpu_fault_window_s"])
    g, er = ev.get("gpu_log") or {}, ev.get("errors") or {}
    src = str(g.get("source") or "")
    nf, ne = g.get("fatal_near") or 0, g.get("infer_errors_near") or 0
    ns = er.get("seh_near") or 0
    # 조사 성공 여부: 신규 기록은 ok 필드, 예전 기록은 출처 문구로 가린다
    probed = g.get("ok") if "ok" in g else ("색인" in src or "꼬리" in src)
    if nf:
        out.append(f"GPU 치명 오류(enqueueV3 cudaGetLastError) {nf}건이 경보 ±{w}초 안에 있다"
                   f" (첫 오류 {g.get('first_fatal')}).")
    elif probed:
        out.append(f"경보 ±{w}초 안에 GPU 치명 오류가 없다.")
    elif src:
        out.append("GPU 로그(DLInfer.log)를 볼 수 없어 GPU 오류 여부는 알 수 없다.")
    if ne:
        out.append(f"인퍼런스 오류(INFER_ERROR) {ne}건이 경보 ±{w}초 안에 있다.")
    if ns:
        out.append(f"SEH 예외 {ns}건({', '.join(er.get('seh_codes') or [])})이 경보 ±{w}초 안에 "
                   f"있다.")
    if er.get("crash_near"):
        out.append("같은 구간에 프로세스 크래시가 있다.")
    i = ev.get("inspections") or {}
    if i.get("inflight"):
        out.append(f"경보 순간 진행 중인 검사가 {i['inflight']}건이다 (가장 오래된 것 "
                   f"{i.get('oldest_inflight_s')}초 경과).")
    else:
        out.append("경보 순간 진행 중인 검사가 없다.")
    doi = i.get("duration_over_interval")
    if doi is not None:
        out.append(f"지금 검사 한 건의 소요가 투입 간격({i.get('recent_interval_s')}초)의 {doi}배다"
                   f" — 투입이 처리보다 {'빠르다' if doi >= 1 else '느리다'}.")
    sd, base = i.get("slowdown_ratio"), i.get("baseline_duration_s")
    if sd is not None and base:
        out.append(f"최근 완료 소요는 30분 이전 평소({base}초)의 {sd}배다 — "
                   f"{'느려졌다' if sd >= p['slowdown_ratio'] else '평소와 비슷하다'}.")
    ed = g.get("exec_slowdown_ratio")
    if ed is not None:
        out.append(f"GPU 커널 시간(executeV2)은 직전 구간의 {ed}배다 — "
                   f"{'느려졌다' if ed >= p['exec_slowdown_ratio'] else '평소와 비슷하다'}.")
    t = ev.get("timeouts") or {}
    n_it, n_at = t.get("img_timeout_30min") or 0, t.get("alg_timeout_30min") or 0
    out.append(f"30분 안에 이미지 처리 타임아웃 {n_it}건, 알고리즘 타임아웃 {n_at}건이 있다."
               if (n_it or n_at) else "30분 안에 타임아웃이 없다.")
    r = ev.get("restarts") or {}
    s = r.get("since_restart_s")
    if s is not None:
        out.append(f"마지막 재시작은 {r.get('last_restart')}로 경보 {s:.0f}초 전이다 — "
                   f"{'기동 직후다' if s <= p['not_init_after_restart_s'] else '기동 직후는 아니다'}.")
    else:
        out.append("경보 전 60분 안에 재시작이 없다.")
    if r.get("pool_log_lines_60min"):
        out.append(f"60분 안의 실제 재시작은 {r.get('count_60min')}회다 (풀 생성 로그 "
                   f"{r['pool_log_lines_60min']}줄을 {p['restart_merge_s']}초 단위로 묶은 값).")
    m = ev.get("model_load") or {}
    if m.get("pending"):
        out.append("모델 로드 명령 뒤 완료 응답이 아직 없다.")
    elif m.get("last_cmd"):
        out.append(f"모델 로드는 {m['last_cmd']}에 끝났다 ({m.get('name')}, 경보 "
                   f"{m.get('minutes_since')}분 전{', 기종이 바뀌었다' if m.get('changed') else ''}).")
    d = ev.get("defects") or {}
    hits = d.get("critical_hits") or []
    if hits:
        out.append(f"치명 결함 {len(hits)}건: " + ", ".join(f"{h['name']}({h['time']})"
                                                         for h in hits[-3:]) + ".")
    if d.get("streak"):
        out.append(f"연속 NG 가 {d['streak']}건이다.")
    if d.get("ng_rate_recent") is not None and d.get("inspections_recent"):
        out.append(f"최근 {d['inspections_recent']}검사의 NG 비율은 {d['ng_rate_recent']}% "
                   f"(그 앞 기준선 {d.get('ng_rate_baseline')}%).")
    if d.get("ng_rate_before_change") is not None:
        out.append(f"기종 로드 전 NG 비율은 {d['ng_rate_before_change']}% 였다.")
    for k, lab in (("grab_fail_30min", "그랩 실패"), ("light_unstable_30min", "조명 불안정"),
                   ("emergency_stop_30min", "비상 정지 송신")):
        if er.get(k):
            out.append(f"30분 안에 {lab} {er[k]}건이 있다.")
    for pt in ev.get("patterns") or []:
        if pt.get("lines"):
            out.append(f"사용자 패턴 '{pt['name']}' 원문: {pt['lines'][-1][:160]}")
    return out


class RuleDiagnoser:
    """런북 원인을 근거 수치로 판정한다 (결정적, 즉시). LLM 과 교차 확인하는 기준 의견."""

    def __init__(self, rb: Runbook, cfg: dict):
        self.rb = rb
        self.cfg = cfg

    def _out(self, family, cause, facts, confirmed=True) -> dict:
        spec = self.rb.cause_spec(family, cause)
        actions = list(spec.get("actions") or ["escalate_to_human"])
        return {"cause": cause, "name": self.rb.cause_name(family, cause),
                "facts": facts[:5], "confirmed": confirmed,
                "actions": actions, "notify": list(spec.get("notify") or []),
                "strength": len(facts)}

    def diagnose(self, family: str, ev: dict, alerts: list, sys_cause: str = "") -> dict:
        if family == "reject":
            return self._reject(ev)
        if family == "defect":
            return self._defect(ev, alerts)
        return self._system(ev, sys_cause, alerts)

    def _reject(self, ev: dict) -> dict:
        p = self.rb.params
        g, r, t, i = ev["gpu_log"], ev["restarts"], ev["timeouts"], ev["inspections"]
        m, er = ev["model_load"], ev["errors"]
        w = int(p["gpu_fault_window_s"])
        nf, ne, ns = (g.get("fatal_near") or 0), (g.get("infer_errors_near") or 0), \
            (er.get("seh_near") or 0)
        if nf or ne or ns:
            f = [f"경보 ±{w}초 GPU 치명 오류(enqueueV3) {nf}건·인퍼런스 오류 {ne}건·"
                 f"SEH 예외 {ns}건"]
            if g.get("first_fatal"):
                f.append(f"첫 GPU 치명 오류 {g['first_fatal']}")
            if er.get("crash_near"):
                f.append("같은 구간 크래시 동반")
            return self._out("reject", "gpu_context_fault", f)
        since = r.get("since_restart_s")
        if (since is not None and since <= p["not_init_after_restart_s"]) or m.get("pending"):
            f = []
            if since is not None:
                f.append(f"마지막 재시작 {r.get('last_restart')} 후 {since:.0f}초 만의 거부")
            if m.get("pending"):
                f.append(f"모델 로드 명령 {m.get('last_cmd')} 뒤 완료 응답 없음")
            return self._out("reject", "not_initialized", f)
        inflight = i.get("inflight") or 0
        if (t.get("img_timeout_30min") or 0) >= 1 and inflight <= 1:
            return self._out("reject", "slot_leak_timeout", [
                f"30분 내 이미지 처리 타임아웃 {t['img_timeout_30min']}건(마지막 "
                f"{t.get('last')}) 뒤 거부", f"진행 중 검사 {inflight}건뿐인데 스레드 없음"])
        f = []
        sd, ed = i.get("slowdown_ratio"), g.get("exec_slowdown_ratio")
        doi = i.get("duration_over_interval")
        if doi and doi >= 1.0:
            f.append(f"검사 소요가 투입 간격({i.get('recent_interval_s')}초)의 {doi}배 — "
                     f"투입이 처리보다 빠름")
        if sd and sd >= p["slowdown_ratio"]:
            f.append(f"최근 완료 소요 {i.get('recent_durations_s')}초 — 평소 중앙값 "
                     f"{i.get('baseline_duration_s')}초의 {sd}배")
        if ed and ed >= p["exec_slowdown_ratio"]:
            f.append(f"executeV2 {g.get('exec_slowest_model')} 중앙값 "
                     f"{g.get('exec_p50_before_ms')}→{g.get('exec_p50_recent_ms')}ms "
                     f"({ed}배)")
        if inflight >= 2:
            f.append(f"진행 중 검사 {inflight}건 (가장 오래된 것 "
                     f"{i.get('oldest_inflight_s')}초 경과)")
        if f:
            if i.get("recent_interval_s"):
                f.append(f"최근 투입 간격 중앙값 {i['recent_interval_s']}초")
            return self._out("reject", "throughput_saturation", f)
        return self._out("reject", "unknown", ["거부 전후로 느려짐·GPU 오류·타임아웃·"
                                               "재시작 근거가 없음"])

    def _defect(self, ev: dict, alerts: list) -> dict:
        p = self.rb.params
        d, t, er, m = ev["defects"], ev["timeouts"], ev["errors"], ev["model_load"]
        rules = {a.rule for a in alerts}
        top = d.get("top") or []
        forced_n = ((t.get("alg_timeout_30min") or 0) + (er.get("grab_fail_30min") or 0)
                    + (er.get("light_unstable_30min") or 0))
        tname = [n for n, _c in top if "TIME_OUT" in n.upper() or "TIMEOUT" in n.upper()]
        if forced_n or tname:
            f = [f"30분 내 알고리즘 타임아웃 {t.get('alg_timeout_30min', 0)}건·그랩 실패 "
                 f"{er.get('grab_fail_30min', 0)}건·조명 불안정 "
                 f"{er.get('light_unstable_30min', 0)}건"]
            if tname:
                f.append(f"결함명 {', '.join(tname)} (시스템 강제 NG)")
            return self._out("defect", "system_forced_ng", f)
        ms = m.get("minutes_since")
        rec, bef = d.get("ng_rate_recent"), d.get("ng_rate_before_change")
        if (ms is not None and ms <= p["recipe_change_window_min"] and rec is not None
                and bef is not None and rec >= max(2 * bef, bef + 20)
                and m.get("changed") is not False):
            return self._out("defect", "recipe_mismatch", [
                f"기종 로드 {m.get('last_cmd')}({m.get('name')}) 후 {ms}분",
                f"NG 비율 {bef}% → {rec}%"])
        f = []
        if top:
            f.append("30분 결함 분포 " + ", ".join(f"{n} {c}건" for n, c in top[:3]))
        if d.get("streak"):
            f.append(f"연속 NG {d['streak']}건")
        if d.get("ng_rate_recent") is not None:
            f.append(f"최근 {d.get('inspections_recent')}검사 NG {d['ng_rate_recent']}% "
                     f"(기준선 {d.get('ng_rate_baseline')}%)")
        if rules & {"ng_streak", "defect_repeat", "ng_rate"}:
            return self._out("defect", "defect_burst", f)
        if "defect_critical" in rules:
            hits = d.get("critical_hits") or []
            if hits:
                f.insert(0, "치명 결함 " + ", ".join(
                    f"{h['name']}(inner {h['inner_id']}, {h['time']})" for h in hits[-3:]))
            return self._out("defect", "critical_defect", f)
        return self._out("defect", "unknown", f or ["결함 경보의 근거 부족"])

    def _system(self, ev: dict, cause: str, alerts: list) -> dict:
        cause = cause if cause in self.rb.causes("system") else "unknown"
        facts = [f"{a.title} — {a.evidence}"[:160] for a in alerts[:2]]
        confirmed = True
        if cause == "restart_loop":
            r = ev["restarts"]
            need = int(self.cfg["rules"].get("restart_burst", {}).get("count", 3))
            facts = [f"실제 재시작 {r['count_60min']}회(30초 안의 흔적 병합, 풀 생성 로그 "
                     f"{r['pool_log_lines_60min']}줄)", f"kill 스크립트 {r['kill_60min']}회"]
            if r["count_60min"] < need:
                confirmed = False
                facts.append(f"기준 {need}회 미만 — 풀 생성 로그 중복 계수로 인한 오경보 의심")
        elif cause == "process_crash":
            er, g = ev["errors"], ev["gpu_log"]
            if er.get("seh_near"):
                facts.append(f"SEH 예외 {er['seh_near']}건 {er.get('seh_codes')}")
            if g.get("fatal_near"):
                facts.append(f"GPU 치명 오류 {g['fatal_near']}건 동반")
        elif cause == "custom_pattern":
            facts = []
            for p in ev.get("patterns") or []:
                facts.append(f"패턴 '{p['name']}' — " + (p["lines"][-1] if p["lines"]
                                                          else "원문 없음"))
            facts = facts or [f"{a.title} — {a.evidence}"[:160] for a in alerts[:2]]
        return self._out("system", cause, facts, confirmed)


# ---------------------------------------------------------------------------
_TIME_TOK = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?(?!\d)")
# '9시 28분 3초' 식 한국어 시각 표기 (초는 선택)
_KTIME_TOK = re.compile(r"(?<!\d)(\d{1,2})\s*시\s*(\d{1,2})\s*분(?:\s*(\d{1,2})\s*초)?")
_NUM_TOK = re.compile(r"(?<![\w.])-?\d+(?:,\d{3})*(?:\.\d+)?")


def _walk(obj, nums: set, times: set, ids: set):
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        nums.add(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _walk(v, nums, times, ids)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _walk(v, nums, times, ids)
    elif isinstance(obj, str):
        for m in _TIME_TOK.finditer(obj):
            times.add(f"{int(m.group(1)):02d}:{m.group(2)}")
        rest = _TIME_TOK.sub(" ", obj)
        for tok in _NUM_TOK.findall(rest):
            digits = tok.replace(",", "").lstrip("-")
            if len(digits.split(".")[0]) >= 10:
                ids.add(digits)
            else:
                try:
                    nums.add(float(tok.replace(",", "")))
                except ValueError:
                    pass


def fact_check(message: str, evidence: dict, extra: tuple = ()) -> dict:
    """알림 문구의 숫자·시각이 근거 데이터에 있는가 (Robot_Sim report.py 규칙 확장).

    - 시각(HH:MM[:SS])은 근거에 있는 시각과 HH:MM 이 같아야 한다
    - 10자리 이상 숫자(inner id 등)는 문자열로 정확히 같아야 한다
    - 그 밖의 숫자는 적힌 자릿수로 반올림해 근거 값(또는 ×100 백분율)과 같으면 통과
    - 0~3 의 작은 정수는 개수 표기가 흔해 통과시킨다
    """
    nums, times, ids = set(), set(), set()
    _walk(evidence, nums, times, ids)
    for x in extra:
        _walk(x, nums, times, ids)
    bad = []
    msg = message or ""
    for m in _TIME_TOK.finditer(msg):
        if f"{int(m.group(1)):02d}:{m.group(2)}" not in times:
            bad.append(m.group(0))
    for m in _KTIME_TOK.finditer(msg):
        if f"{int(m.group(1)):02d}:{int(m.group(2)):02d}" not in times:
            bad.append(m.group(0))
    rest = _KTIME_TOK.sub(" ", _TIME_TOK.sub(" ", msg))
    n_tok = 0
    for tok in _NUM_TOK.findall(rest):
        n_tok += 1
        digits = tok.replace(",", "").lstrip("-")
        if len(digits.split(".")[0]) >= 10:
            if digits not in ids:
                bad.append(tok)
            continue
        x = float(tok.replace(",", ""))
        dec = len(digits.split(".")[1]) if "." in digits else 0
        if dec == 0 and 0 <= x <= 3:
            continue
        ok = False
        for y in nums:
            for cand in (y, -y, y * 100.0):
                if round(cand, dec) == round(x, dec) or (dec == 0 and abs(cand - x) < 0.5):
                    ok = True
                    break
            if ok:
                break
        if not ok:
            bad.append(tok)
    return {"ok": not bad, "unsupported": bad[:8], "n_numbers": n_tok,
            "hanzi": hanzi_count(msg)}


# ---------------------------------------------------------------------------
_ANALYSIS_PROMPT = ("판단하기 전에 분석을 먼저 짧게 써라(6문장 이내): 핵심 관찰과 그 수치, "
                    "그것이 어느 원인을 지지하거나 배제하는지. 아직 JSON 은 쓰지 마라.")
_FINAL_PROMPT = ("이제 최종 판단을 JSON 으로만 답하라. evidence 에는 수치를 넣은 짧은 근거 "
                 "문장(최대 4개), root_cause 는 원인 id, confidence 는 0~1, actions 는 조치 id "
                 "1~3개, notify 는 역할 id, severity 는 info|warn|crit, message_ko 는 담당자에게 "
                 "보낼 한국어 3문장 이내(한글만, 한자 금지, 근거 JSON 에 있는 숫자와 시각만 인용).")


class LLMAnalyst:
    """로컬 LLM 2차 의견 — 런북 문단을 읽고 닫힌 어휘로 답한다(서술 → JSON 2단계)."""

    def __init__(self, llm_cfg: dict, rb: Runbook, reason_first: bool = True):
        self.client = OllamaClient(llm_cfg)
        self.rb = rb
        self.reason_first = reason_first

    def _system(self, family: str) -> str:
        fam = self.rb.families.get(family) or {}
        causes = "\n".join(f"- {cid}: {c.get('name', cid)}\n  {c.get('description', '')}"
                           for cid, c in self.rb.causes(family).items())
        acts = "\n".join(f"- {a}: {t}" for a, t in self.rb.actions.items())
        roles = ", ".join(f"{r}({t})" for r, t in self.rb.roles.items())
        return (
            "당신은 talos 비전 검사 설비의 운영 에이전트다. 실시간 감시(talog watch)가 경보를 "
            "올렸다. 아래 런북에서 근거에 맞는 원인 하나를 고르고, 담당자에게 권고할 조치와 알릴 "
            "역할을 정한다. talog 는 설비를 직접 조작하지 않으며 조치는 사람에게 권고하는 것이다.\n\n"
            f"[원인 — {fam.get('title', family)}]\n{causes}\n- unknown: 어느 원인도 근거와 맞지 "
            f"않음\n\n[조치]\n{acts}\n\n[역할] {roles}\n\n"
            "원칙:\n1. 근거에 없는 수치나 사실을 지어내지 않는다. 숫자와 시각은 근거 JSON 에 있는 "
            "값만 인용한다. key_facts 는 근거 수치를 문장으로 옮긴 것이므로, 수치를 직접 읽은 "
            "결과가 key_facts 와 다르면 key_facts 를 따른다.\n2. 근거가 한 원인을 뚜렷이 지지하지 "
            "않으면 unknown 과 "
            "escalate_to_human 을 고른다.\n3. 진행 중 검사(inflight)가 있으면 즉시 재시작을 권하지 "
            "않는다.\n4. 근거 없이 정비 출동이나 제품 격리를 권하지 않는다.\n"
            "5. 모든 서술은 한국어로 쓰고 중국어 한자를 쓰지 않는다.")

    def _schema(self, family: str) -> dict:
        causes = list(self.rb.causes(family)) + ["unknown"]
        return {"type": "object", "properties": {
            "evidence": {"type": "array", "items": {"type": "string", "maxLength": 200},
                         "maxItems": 4},
            "root_cause": {"type": "string", "enum": causes},
            "confidence": {"type": "number"},
            "actions": {"type": "array", "items": {"type": "string",
                                                   "enum": list(self.rb.actions)},
                        "minItems": 1, "maxItems": 3},
            "notify": {"type": "string", "enum": list(self.rb.roles)},
            "severity": {"type": "string", "enum": ["info", "warn", "crit"]},
            "message_ko": {"type": "string", "maxLength": 400}},
            "required": ["evidence", "root_cause", "confidence", "actions", "notify",
                         "severity", "message_ko"]}

    def analyze(self, family: str, ev: dict) -> dict:
        c = self.client
        out = {"ok": False, "error": "", "model": c.model, "url": c.url, "device": "",
               "device_reason": "", "wall_s": 0.0, "prompt_tokens": 0, "gen_tokens": 0,
               "calls": 0, "analysis": ""}
        # 해석 문장(key_facts)을 맨 앞에 둔다 — 숫자를 직접 읽다 틀리는 것을 줄인다
        ordered = {"key_facts": ev.get("key_facts") or interpret(ev, self.rb.params, family),
                   **{k: v for k, v in ev.items() if k != "key_facts"}}
        user = (f"[사건] {ev.get('site') or '설비'} {ev['trigger']['time']} — 경보 "
                f"{ev.get('n_alerts')}건: " + " / ".join(a["title"] for a in ev["alerts"][:4])
                + "\n\n[근거 JSON]\n" + json.dumps(ordered, ensure_ascii=False))
        msgs = [{"role": "system", "content": self._system(family)},
                {"role": "user", "content": user}]
        device = None

        def call(m, fmt=None, n=None):
            nonlocal device
            r = c.chat(m, fmt=fmt, num_predict=n, device=device)
            device = device or r["device"]           # 한 사건 안에서는 장치 고정(재로드 방지)
            out["calls"] += 1
            out["wall_s"] = round(out["wall_s"] + r["wall_s"], 2)
            out["prompt_tokens"] += r["prompt_tokens"]
            out["gen_tokens"] += r["gen_tokens"]
            out["device"], out["device_reason"] = r["device"], out["device_reason"] or \
                r["device_reason"]
            return r

        if self.reason_first:
            r = call(msgs + [{"role": "user", "content": _ANALYSIS_PROMPT}], n=450)
            if r["error"]:
                out["error"] = r["error"]
                return out
            out["analysis"] = r["content"].strip()[:1500]
            msgs = msgs + [{"role": "user", "content": _ANALYSIS_PROMPT},
                           {"role": "assistant", "content": out["analysis"]}]
        msgs = msgs + [{"role": "user", "content": _FINAL_PROMPT}]
        schema = self._schema(family)
        r = call(msgs, fmt=schema, n=700)
        if r["error"]:
            out["error"] = r["error"]
            return out
        d = parse_json(r["content"]) or {}
        if d and hanzi_count(str(d.get("message_ko", ""))):
            # 한자 누출 시 1회 재요청 (Qwen 계열의 알려진 이탈)
            r2 = call(msgs + [{"role": "assistant", "content": r["content"]},
                              {"role": "user", "content": "message_ko 에 한자가 섞였다. 같은 "
                               "판단을 한글로만 다시 써서 같은 JSON 형식으로 답하라."}],
                      fmt=schema, n=700)
            d = parse_json(r2["content"]) or d
        if not d:
            out["error"] = "JSON 파싱 실패"
            out["raw"] = r["content"][:300]
            return out
        causes = list(self.rb.causes(family)) + ["unknown"]
        rc = d.get("root_cause")
        out.update(
            ok=True,
            root_cause=rc if rc in causes else "unknown",
            actions=[a for a in (d.get("actions") or []) if a in self.rb.actions][:3]
            or ["escalate_to_human"],
            notify=d.get("notify") if d.get("notify") in self.rb.roles else "",
            severity=d.get("severity") if d.get("severity") in _SEV_RANK else "",
            confidence=d.get("confidence") if isinstance(d.get("confidence"),
                                                         (int, float)) else None,
            evidence=[str(x)[:200] for x in (d.get("evidence") or [])][:4],
            message_ko=str(d.get("message_ko", ""))[:400])
        out["fact"] = fact_check(out["message_ko"], ev, extra=(self.rb.params,))
        return out


# ---------------------------------------------------------------------------
def rule_message(family: str, rule: dict, alerts: list, rb: Runbook) -> str:
    a0 = alerts[0]
    facts = "; ".join(rule["facts"][:2]) if rule.get("facts") else "근거 부족"
    acts = ", ".join(rb.action_ko(a).split(" — ")[0].split(" (")[0]
                     for a in rule["actions"][:2])
    return f"{a0.title}. 룰 진단: {rule['name']}. 근거: {facts}. 권고: {acts}."


def gate(family: str, rule: dict, llm: dict | None, alerts: list, rb: Runbook) -> dict:
    """합의 관문: 규칙과 LLM 이 같은 원인을 말할 때만 '판단 일치'."""
    sev = max((a.severity for a in alerts), key=sev_rank)
    notify = list(rule["notify"])
    actions = list(rule["actions"])
    msg = rule_message(family, rule, alerts, rb)
    llm_ok = bool(llm and llm.get("ok"))
    if not llm:
        mode, label = "rule_only", "룰 판단 (LLM 미사용)"
    elif not llm_ok:
        mode, label = "rule_only", f"룰 판단 (LLM 응답 실패: {llm.get('error', '')[:60]})"
    elif llm["root_cause"] == rule["cause"] and rule["cause"] != "unknown":
        mode, label = "agree", "룰·LLM 판단 일치"
    elif rule["cause"] == "unknown" and llm["root_cause"] != "unknown":
        mode, label = "llm_only", "룰 판단 불가 — LLM 의견만 있음, 담당자 확인 필요"
    else:
        mode, label = "disagree", "판단 불일치 — 담당자 확인 필요"
    if llm_ok and llm.get("notify") and llm["notify"] not in notify and mode != "rule_only":
        notify.append(llm["notify"])
    if mode in ("disagree", "llm_only") or rule["cause"] == "unknown":
        if "escalate_to_human" not in actions:
            actions.append("escalate_to_human")
    msg_src = "rule"
    if mode == "agree":
        f = llm.get("fact") or {}
        if llm.get("message_ko") and f.get("ok") and not f.get("hanzi"):
            msg, msg_src = llm["message_ko"], "llm"
    elif mode in ("disagree", "llm_only"):
        msg += (f" LLM 의견: {rb.cause_name(family, llm['root_cause'])}"
                f" — 두 의견을 함께 확인하십시오.")
    if not rule.get("confirmed", True):
        sev = "warn"
        label = "오경보 의심 — " + label
    return {"mode": mode, "label": label, "cause": rule["cause"],
            "cause_name": rule["name"],
            "llm_cause": llm.get("root_cause") if llm_ok else None,
            "llm_cause_name": rb.cause_name(family, llm["root_cause"]) if llm_ok else None,
            "actions": actions, "notify": notify, "severity": sev,
            "message": msg, "message_source": msg_src}


# ---------------------------------------------------------------------------
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
            self._states[id(a)] = self.builder.capture()   # 경보 순간의 검사 상태
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
            from .casekb import CaseKB
            kb = CaseKB()
            if not kb.cases:
                return []
            q = alerts[0].title + " " + rule.get("name", "") + " " + " ".join(
                rule.get("facts", [])[:2])
            return [{"score": round(s, 2), "title": c.title, "cause": c.cause,
                     "action": c.action, "site": c.site, "date": c.date}
                    for s, c in kb.search(q, 2)]
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
