"""사건 근거 수집 — 룰 엔진 상태 스냅샷(EvidenceBuilder)과 GPU 로그 조사(GpuLogProbe)."""

from __future__ import annotations

import datetime as dt
import os
import re
import statistics
from array import array
from collections import Counter
from fnmatch import fnmatchcase

from ..lineparser import _TS_RE
from .common import _ERROR_KINDS, _alert_dict, _hms, _r
from .runbook import Runbook


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


class EvidenceBuilder:
    """룰 엔진·GPU 감시기의 현재 상태를 LLM·룰이 같이 보는 근거 JSON 으로 만든다."""

    def __init__(self, cfg: dict, engine, gpu=None, runbook: Runbook | None = None):
        self.cfg = cfg
        self.engine = engine
        self.gpu = gpu
        self.rb = runbook or Runbook()

    def capture(self, alert=None) -> dict:
        """경보 시점의 검사 상태 사본 — 마감(+batch_seconds) 전에 재시작이 끼면
        진행 중 목록이 비워지므로, 경보가 들어온 순간의 상태를 근거로 쓴다.
        경보에 그룹이 실려 있으면(meta.group) 그 그룹 기준으로 처리 여유 근거도 떠 둔다."""
        eng = self.engine
        g = (getattr(alert, "meta", None) or {}).get("group") if alert is not None else None
        st = {"pending": dict(eng.pending), "pgroup": dict(getattr(eng, "_pgroup", {}) or {}),
              "done": list(eng.done), "durations": list(eng.durations), "group": g}
        cap = getattr(eng, "cap", None)
        if cap is not None and alert is not None:
            try:
                st["cap"] = cap.evidence(alert.ts, g)
            except Exception as e:                # 근거 보강 실패가 사건 분석을 막지 않게
                print(f"  ! 처리 여유 근거 수집 실패(계속): {type(e).__name__}: {e}")
                st["cap"] = {}
        return st

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

        # 검사 흐름 (경보 시점 상태) — 그룹을 알면 그 그룹만 -----------------------
        capev = st.get("cap") or {}
        g = st.get("group")
        if g is None:
            g = (capev.get("capacity") or {}).get("group")
        pg = st.get("pgroup") or {}

        def same_group(key):
            return not g or pg.get(key, 0) in (g, 0)

        done = [x for x in st["done"] if x[0] <= t_trig and
                (not g or len(x) < 4 or x[3] in (g, 0))]
        base_l = [x[2] for x in done if x[0] < t_trig - 1800]    # 30분 이전 = 평소
        recent_d = [x[2] for x in done if x[0] >= t_trig - 1800][-3:]
        if len(base_l) < 5:
            base_l = [x[2] for x in done][:-3] or list(st["durations"])
        base = statistics.median(base_l) if len(base_l) >= 5 else None
        starts = within({"INSP_START"}, t0 - 1800, t_trig)
        gaps = [b.ts - a.ts for a, b in zip(starts, starts[1:]) if 0.5 < b.ts - a.ts < 1800]
        ends = [e for e in within({"COMM_MSG"}, t0 - 600) if "INSPECT_END" in e.name]
        last_end = max((e.ts for e in ends if e.ts <= t_trig), default=None)
        # 1시간 넘게 남은 항목은 소실된 검사(END 미송신)라 진행 중으로 세지 않는다.
        # 거부된 검사도 뺀다 — InspStarter 의 도착 줄이 설비 회신(ACK)보다 먼저 읽히면
        # 경보 순간 사본에 '시작된 검사'로 남아 있기 때문 (실시간 종단 시험 9/29: 3건 → 2건)
        rejected = {(e.inner_id, int(e.value or 0)) for e in within({"COMM_MSG"}, t0 - 1800)
                    if "INSPECT_START_ACK" in e.name and e.status and e.status != "OK"}

        def was_rejected(key):
            inner, gg = key.split("@", 1)[0], pg.get(key, 0)
            return (inner, gg) in rejected or (gg == 0 and any(r[0] == inner for r in rejected))

        pend = {k: s for k, s in st["pending"].items()
                if 0 <= t_trig - s < 3600 and same_group(k) and not was_rejected(k)}
        cap_c = capev.get("capacity") or {}
        interval = cap_c.get("interval_s") or (statistics.median(gaps) if gaps else None)
        pool = cap_c.get("pool")
        # '지금 한 건에 걸리는 시간' = 최근 완료 중앙값과 경보 순간 진행 중 최장 경과 중 큰 값
        cands = []
        if recent_d:
            cands.append(statistics.median(recent_d))
        if pend:
            cands.append(max(t_trig - s for s in pend.values()))
        cur = max(cands) if cands else None
        ev["inspections"] = {
            "group": g or None,
            "baseline_duration_s": _r(base),
            "recent_durations_s": [_r(d) for d in recent_d],
            "slowdown_ratio": (_r(statistics.median(recent_d) / base, 2)
                               if base and recent_d else None),
            "inflight": len(pend),
            "inflight_ids": [k.split("@", 1)[0] for k in sorted(pend, key=pend.get)[:4]],
            "oldest_inflight_s": _r(max(t_trig - s for s in pend.values())) if pend else None,
            "recent_interval_s": _r(interval, 3),
            "duration_over_interval": _r(cur / interval, 2) if cur and interval else None,
            "pool": pool,
            "threads_needed_over_pool": (_r(cur / interval / pool, 2)
                                         if cur and interval and pool else None),
            "wait_threads_recent": [int(e.value) for e in starts[-4:]],
            "starts_10min": sum(1 for e in starts if e.ts >= t_trig - 600),
            "ends_10min": len({(e.inner_id, int(e.value or 0)) for e in ends if e.ts <= t_trig}),
            # 완료 신호가 끊겼는가 — 마지막 완료(INSPECT_END) 뒤 경과와 그 뒤 들어온 검사 시작 수
            "last_end_age_s": _r(t_trig - last_end) if last_end is not None else None,
            "starts_since_last_end": sum(1 for e in starts if last_end is None or e.ts > last_end),
        }
        # 처리 여유·추세·단계·일시 지연·자원 (talog/capacity.py) — 기준선 대비
        for k in ("capacity", "trend", "phases", "transient", "resources"):
            if capev.get(k):
                ev[k] = capev[k]

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
