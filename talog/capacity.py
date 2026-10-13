"""처리 여유 모델 — Seq 그룹별 유휴 스레드·점유 시간·투입 간격을 배우고 기준선과 비교한다.

talos 는 검사 요청을 Seq 그룹(InspStarter 의 groupId, comm 회신의 마지막 토큰)마다 정해진 수의
Seq 스레드로 처리한다. 검사 하나가 스레드를 잡고 있는 시간(점유)이 길어지면 새 검사가 도착한
순간의 유휴 스레드(InspStarter 'Waiting thread count')가 줄고, 0 이 되면 NoInspThread 로 거부된다.
사이트마다 그룹 수·스레드 수·투입 간격이 다르다(풍산 2호기 12그룹×6, Tenneco 4존, PC3 1그룹).
그래서 상수를 두지 않고 로그에서 배운다.

  유휴 스레드   새 검사가 도착한 순간의 값 (모든 사이트의 InspStarter 에 있다)
  점유 시간     seq_N 의 'CheckFinish done' — 결과 송신 뒤 저장·DB 까지 끝나 스레드가 돌아오는 시점.
                seq 를 읽지 않으면 comm 회신 기준 소요(INSPECT_END − 도착)로 대신한다
  투입 간격     같은 그룹 도착 간격의 중앙값
  풀 크기       도착 순간 (유휴 + 그 그룹 진행 중 수)의 95 분위와 seq 스레드 인스턴스 수 중 큰 값 (추정)
  한계          풀 × 투입 간격 — 점유가 이 값을 넘으면 다음 투입을 받을 스레드가 없다

기준선은 기동(재시작)·기종 교체 뒤 워밍업을 지난 첫 구간에서 그룹마다 잡는다. 상태를 data_dir 에
저장해 talog 가 다시 뜨거나 플랫폼 로그가 지워져도 이어진다(풍산 2호기 10/09~10/10 로그 삭제 사례).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import statistics
from bisect import bisect_left
from collections import Counter, deque

DEFAULTS = {
    "enabled": True,
    "idle_window_min": 30,      # 유휴 스레드 판정 창
    "hold_window_min": 60,      # 점유 시간 판정 창
    "warmup_min": 10,           # 기동·기종 교체 직후 제외
    "baseline_min": 60,         # 기준선 수집 최소 시간 (워밍업 뒤)
    "min_samples": 100,         # 기준선에 필요한 도착 수 (느린 설비는 시간이 더 걸린다)
    "eval_samples": 30,         # 판정에 필요한 최소 표본
    "idle_drop": 1,             # 유휴 중앙값이 기준보다 이만큼 줄면 주의
    "idle_crit": 1,             # 기준 유휴 중앙값이 3 이상인 그룹에서 중앙값이 이 값 이하면 심각
    "near_miss_idle": 1,        # 기준 유휴 중앙값이 3 이상인 그룹에서 유휴가 이 값 이하인 도착 = 거부 직전
    "tolerance_warn": 0.40,     # (한계 − p99 점유) / 한계 가 이보다 작으면 주의
    "tolerance_crit": 0.20,
    "drift_warn": 1.25,         # 점유 p50 / 기준 이 이 이상이면 주의
    "drift_crit": 1.5,
    "interval_tol": 0.10,       # 투입 간격이 기준 ±10% 안일 때만 '열화'로 본다 (밖이면 부하 변화)
    "heal_ratio": 0.75,         # 점유가 기준의 이 배 아래로 내려오면 재시작된 것으로 보고 기준을 다시 잡는다
    "max_age_days": 14,         # 이보다 오래된 저장 기준선은 버린다
    "eval_seconds": 60,         # 판정 주기
    "cooldown_min": 60,         # 심각 경보 재발보 간격
    "warn_cooldown_min": 240,   # 주의 경보 재발보 간격 (추세는 몇 시간 이어지므로 자주 울리지 않게)
}

_KEEP_S = 3 * 3600              # 그룹별 원자료 보관 (3시간)


def q(vals, p):
    """정렬해 p 분위값 (표본이 없으면 None)."""
    if not vals:
        return None
    v = sorted(vals)
    return v[min(len(v) - 1, int(len(v) * p))]


def _r(x, nd=2):
    return None if x is None else round(float(x), nd)


def spearman(xs) -> float | None:
    """시간 순서와 값의 순위상관 (1 = 꾸준히 증가). 표본 4개 미만이면 None."""
    n = len(xs)
    if n < 4:
        return None
    order = sorted(range(n), key=lambda i: xs[i])
    rank = [0] * n
    for r, i in enumerate(order):
        rank[i] = r
    d2 = sum((rank[i] - i) ** 2 for i in range(n))
    return 1 - 6 * d2 / (n * (n * n - 1))


def _hms(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else None


def _mdhm(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%m/%d %H:%M") if ts else None


class _Group:
    __slots__ = ("arr", "holds", "src_seq_ts", "votes", "rejects", "phases", "hourly", "base")

    def __init__(self):
        self.arr: deque = deque()          # (ts, 유휴)
        self.holds: deque = deque()        # (ts, 점유 초, 'seq'|'cycle')
        self.src_seq_ts = 0.0              # 마지막 seq 점유 시각 (있으면 cycle 은 쓰지 않는다)
        self.votes: deque = deque(maxlen=2000)   # 유휴 + 진행 중 (풀 크기 추정)
        self.rejects: deque = deque()
        self.phases: deque = deque()       # (ts, 처리, 송신, 후처리)
        self.hourly: deque = deque(maxlen=96)
        self.base: dict | None = None


class CapacityModel:
    """그룹별 처리 여유. RuleEngine 이 이벤트를 넣고 evaluate() 가 경보 후보를 돌려준다."""

    def __init__(self, cfg: dict | None = None):
        self.c = {**DEFAULTS, **(cfg or {})}
        self.g: dict[int, _Group] = {}
        self.gen_start = 0.0               # 이번 세대(기동·기종 교체) 시작 시각
        self.gen_reason = ""
        self.first_ts = 0.0
        self.last_eval = 0.0
        self.last_hour = 0                 # KPI 를 낸 마지막 시간 (epoch // 3600)
        self.usage: deque = deque()        # (ts, MB, cpu%, 장비 cpu%, 스레드 수)
        self.changed = False               # 저장할 상태가 바뀌었는가
        self.summary: list[dict] = []      # 그룹별 요약 (콘솔 표시용, 판정 때마다 새 목록으로 교체)
        self.seq_threads: dict[int, int] = {}  # 그룹 -> seq 인스턴스(스레드) 수 (이번 세대)
        self.gen_id = 0                    # 세대 번호 (재시작·기종 교체마다 +1)
        self.events: list[str] = []        # 기준선 확정·재설정 기록 (콘솔·로그용)
        self.prev_gen: dict | None = None  # 직전 세대의 마지막 그룹 요약 (재시작 직전, 사후 리포트용)

    # ── 입력 ───────────────────────────────────────────────────
    def _grp(self, g: int) -> _Group:
        x = self.g.get(g)
        if x is None:
            x = self.g[g] = _Group()
        return x

    def _touch(self, ts: float):
        if not self.first_ts:
            self.first_ts = ts
            if not self.gen_start:
                self.gen_start = ts
            # 저장된 기준선이 이 데이터보다 뒤에서 잡힌 것이면(과거 일자 재현 등) 쓰지 않는다
            for x in self.g.values():
                if x.base and x.base.get("from", 0) > ts + 60:
                    x.base = None
                    self.changed = True

    @staticmethod
    def _trim(dq: deque, now: float, keep: float = _KEEP_S):
        while dq and now - dq[0][0] > keep:
            dq.popleft()

    def arrival(self, ts: float, g: int, idle: int, inflight: int | None = None) -> dict | None:
        """도착 1건. 평소 유휴가 넉넉한(기준 중앙값 3 이상) 그룹에서 유휴가 1 이하로 떨어지면
        '거부 직전' 경보 후보를 바로 돌려준다 (판정 주기를 기다리지 않는다)."""
        self._touch(ts)
        x = self._grp(g)
        x.arr.append((ts, int(idle)))
        self._trim(x.arr, ts)
        if inflight is not None and inflight >= 0:
            x.votes.append(int(idle) + int(inflight))
        b = x.base
        c = self.c
        # 유휴 0 은 거부 그 자체라 NoInspThread 경보가 맡는다 — 여기서는 1 이상만
        if (c.get("enabled", True) and b and (b.get("idle_median") or 0) >= 3
                and 1 <= idle <= c["near_miss_idle"]):
            return {"rule": "capacity_drift", "severity": "crit",
                    "title": f"거부 직전 — 그룹 {g} 유휴 스레드 {idle}개",
                    "evidence": f"{_hms(ts)} 도착 순간 유휴 {idle}개 (기준 중앙값 "
                                f"{b.get('idle_median'):g}개) — 일시 지연 한 번에 NoInspThread 가 "
                                f"날 수 있습니다", "key": "near", "groups": [g], "meta": {"group": g}}
        return None

    def hold(self, ts: float, g: int, sec: float, src: str = "seq"):
        if sec is None or sec < 0 or sec > 3600:
            return
        self._touch(ts)
        x = self._grp(g)
        if src == "seq":
            x.src_seq_ts = ts
        elif ts - x.src_seq_ts < 600:
            return                          # seq 점유가 들어오는 그룹은 회신 기준 소요를 쓰지 않는다
        x.holds.append((ts, float(sec), src))
        self._trim(x.holds, ts)

    def phase(self, ts: float, g: int, proc: float, send: float, hold: float):
        if not (0 <= proc <= send <= hold <= 3600):
            return
        x = self._grp(g)
        x.phases.append((ts, proc, send - proc, hold - send))
        self._trim(x.phases, ts)

    def reject(self, ts: float, g: int):
        self._touch(ts)
        x = self._grp(g)
        if x.rejects and ts - x.rejects[-1][0] < 2.0:
            return                          # 같은 거부의 두 흔적(InspStarter·comm 회신)
        x.rejects.append((ts,))
        self._trim(x.rejects, ts)

    def usage_sample(self, ts: float, mb: float, cpu: float | None, total: float | None,
                     threads: int | None):
        """ProcessUsage. 메모리가 절반 아래로 떨어지면 재시작으로 본다(풀 생성 로그가 없는 사이트)."""
        if self.usage:
            peak = max(u[1] for u in list(self.usage)[-60:])
            if peak > 2000 and mb < 0.5 * peak:
                self.reset(ts, "재시작 추정 (프로세스 메모리 급감)")
        self.usage.append((ts, mb, cpu, total, threads))
        while self.usage and ts - self.usage[0][0] > 24 * 3600:
            self.usage.popleft()

    def reset(self, ts: float, reason: str):
        """재시작·기종 교체 — 기준선과 풀 추정을 새로 잡는다(원자료도 비운다)."""
        had = any(x.base for x in self.g.values())
        last = max((x.arr[-1][0] for x in self.g.values() if x.arr), default=None)
        if last is not None:
            # 생산이 끝난 뒤 재시작하면 그날 마지막 검사 시점의 요약이 비므로 비우기 전에 남긴다
            self.prev_gen = {"end": last, "reason": reason,
                             "groups": {g: self.stats(g, last) for g, x in sorted(self.g.items()) if x.arr}}
        for x in self.g.values():
            x.arr.clear()
            x.holds.clear()
            x.votes.clear()
            x.phases.clear()
            x.base = None
            x.src_seq_ts = 0.0
        self.seq_threads.clear()
        self.gen_id += 1
        self.gen_start = ts
        self.gen_reason = reason
        self.changed = True
        if had:
            self.events.append(f"{_mdhm(ts)} 기준선 초기화 — {reason}")

    # ── 통계 ───────────────────────────────────────────────────
    @staticmethod
    def _since(dq: deque, lo: float, hi: float | None = None):
        """시각순 deque 에서 [lo, hi] 구간 (deque 는 시각순이라 이분 탐색)."""
        i = bisect_left(dq, (lo,))
        out = []
        for k in range(i, len(dq)):
            item = dq[k]
            if hi is not None and item[0] > hi:
                break
            out.append(item)
        return out

    def interval(self, g: int, lo: float, hi: float) -> float | None:
        ts = [a[0] for a in self._since(self._grp(g).arr, lo, hi)]
        gaps = [b - a for a, b in zip(ts, ts[1:]) if 0 < b - a <= 600]
        return statistics.median(gaps) if len(gaps) >= 5 else None

    def pool(self, g: int) -> int | None:
        """풀(스레드 수) 추정. (유휴 + 진행 중) 은 저장·DB 단계 검사가 진행 중에서 빠져 풀보다 작게
        나올 때가 있어 최빈값이 아니라 95 분위를 쓰고, seq 인스턴스 수가 믿을 만하면 같이 본다."""
        x = self._grp(g)
        mx = max((a[1] for a in x.arr), default=0)
        cands = []
        if len(x.votes) >= 30:
            cands.append(q(list(x.votes), 0.95))
        st = self.seq_threads.get(g)
        if st and mx <= st <= 64:
            cands.append(st)
        p = max(cands) if cands else None
        return max(p, mx) if p else None

    def stats(self, g: int, now: float) -> dict:
        c = self.c
        x = self._grp(g)
        idle = [a[1] for a in self._since(x.arr, now - c["idle_window_min"] * 60, now)]
        holds = [h[1] for h in self._since(x.holds, now - c["hold_window_min"] * 60, now)]
        iv = self.interval(g, now - 3600, now)
        pool = self.pool(g)
        lim = pool * iv if (pool and iv) else None
        p99 = q(holds, 0.99) if len(holds) >= c["eval_samples"] else None
        out = {"group": g, "n_arrivals": len(idle), "n_holds": len(holds),
               "idle_median": statistics.median(idle) if idle else None,
               "idle_mean": _r(statistics.mean(idle)) if idle else None,
               "idle_min": min(idle) if idle else None,
               "idle_p01": q(idle, 0.01),
               "hold_p50": _r(statistics.median(holds)) if holds else None,
               "hold_p95": _r(q(holds, 0.95)), "hold_p99": _r(p99),
               "hold_max": _r(max(holds)) if holds else None,
               "hold_source": (x.holds[-1][2] if x.holds else None),
               "interval_s": _r(iv, 3), "pool": pool, "limit_s": _r(lim),
               "tolerance_s": _r(lim - p99) if (lim and p99 is not None) else None,
               "tolerance_ratio": _r((lim - p99) / lim, 3) if (lim and p99 is not None) else None,
               "rejects_60min": sum(1 for r in x.rejects if now - r[0] <= 3600)}
        b = x.base
        if b:
            out["baseline"] = b
            if out["hold_p50"] and b.get("hold_p50"):
                out["hold_ratio"] = _r(out["hold_p50"] / b["hold_p50"])
            if out["idle_median"] is not None and b.get("idle_median") is not None:
                out["idle_drop"] = _r(b["idle_median"] - out["idle_median"], 1)
            if iv and b.get("interval_s"):
                out["interval_change_pct"] = _r(100 * (iv / b["interval_s"] - 1), 1)
        return out

    # ── 기준선 ─────────────────────────────────────────────────
    def _capture(self, now: float):
        c = self.c
        lo = self.gen_start + c["warmup_min"] * 60
        if now - lo < c["baseline_min"] * 60:
            return
        for g, x in self.g.items():
            if x.base:
                continue
            arr = self._since(x.arr, lo, now)
            holds = [h[1] for h in self._since(x.holds, lo, now)]
            if len(arr) < c["min_samples"] or len(holds) < c["eval_samples"]:
                continue
            idle = [a[1] for a in arr]
            iv = self.interval(g, lo, now)
            x.base = {"from": round(lo, 3), "to": round(now, 3),
                      "idle_median": statistics.median(idle), "idle_p01": q(idle, 0.01),
                      "hold_p50": _r(statistics.median(holds), 3), "hold_p99": _r(q(holds, 0.99), 3),
                      "interval_s": _r(iv, 4), "pool": self.pool(g), "n": len(arr),
                      "reason": self.gen_reason or "감시 시작"}
            ph = self._since(x.phases, lo, now)
            if len(ph) >= c["eval_samples"]:
                x.base["phases"] = [_r(statistics.mean(p[i] for p in ph), 3) for i in (1, 2, 3)]
            self.changed = True
            self.events.append(f"{_mdhm(now)} 그룹 {g} 기준선 확정 — 유휴 {x.base['idle_median']:g}, "
                               f"점유 p50 {x.base['hold_p50']}초, 투입 {x.base['interval_s']}초")

    def _heal(self, now: float):
        """점유가 기준보다 한참 짧아졌으면 그 사이 재시작된 것 — 그 그룹 기준선을 다시 잡는다."""
        for g, x in self.g.items():
            b = x.base
            if not b:
                continue
            holds = [h[1] for h in self._since(x.holds, now - 1800, now)]
            if len(holds) >= self.c["eval_samples"] and b.get("hold_p50") and \
                    statistics.median(holds) < b["hold_p50"] * self.c["heal_ratio"]:
                x.base = None
                self.gen_start = now               # 이 그룹은 지금부터 워밍업·기준 수집
                self.changed = True
                self.events.append(f"{_mdhm(now)} 그룹 {g} 기준선 재설정 — 점유가 기준보다 짧아짐(재시작 추정)")
            elif now - b.get("to", now) > self.c["max_age_days"] * 86400:
                x.base = None
                self.changed = True

    # ── 판정 ───────────────────────────────────────────────────
    def evaluate(self, now: float) -> list[dict]:
        """경보 후보 [{rule, severity, title, evidence, key, groups, meta}]. 주기는 eval_seconds."""
        c = self.c
        if not c.get("enabled", True) or now - self.last_eval < c["eval_seconds"]:
            return []
        self.last_eval = now
        self._capture(now)
        self._heal(now)
        cap, drift, summ = [], [], []
        for g, x in self.g.items():
            b = x.base
            s = self.stats(g, now)
            active = bool(x.arr) and now - x.arr[-1][0] <= 300   # 5분 안에 도착이 있어야 판정
            summ.append({k: s.get(k) for k in ("group", "idle_median", "idle_min", "hold_p50",
                                               "hold_p99", "hold_ratio", "interval_s", "pool",
                                               "limit_s", "tolerance_s", "tolerance_ratio",
                                               "interval_change_pct", "rejects_60min")}
                        | {"idle_baseline": (b or {}).get("idle_median"),
                           "hold_baseline": (b or {}).get("hold_p50"),
                           "baseline_from": _mdhm((b or {}).get("from")),
                           "status": "ok" if active else "idle"})
            if b is None or s["n_arrivals"] < c["eval_samples"] or not active:
                continue                            # 라인이 멈춘 뒤 남은 창으로 경보하지 않는다
            sev, why = None, []
            bi, im = b.get("idle_median"), s["idle_median"]
            if bi is not None and im is not None and bi >= 1:
                if bi >= 3 and im <= c["idle_crit"]:
                    sev = "crit"
                    why.append(f"유휴 스레드 중앙값 {im:g}개 (기준 {bi:g}개)")
                elif im <= bi - c["idle_drop"]:
                    sev = "warn"
                    why.append(f"유휴 스레드 중앙값 {im:g}개 (기준 {bi:g}개)")
            tr = s.get("tolerance_ratio")
            if tr is not None:
                if tr < c["tolerance_crit"]:
                    sev = "crit"
                    why.append(f"일시 지연 내성 {s['tolerance_s']}초 (한계 {s['limit_s']}초의 {tr * 100:.0f}%)")
                elif tr < c["tolerance_warn"]:
                    sev = sev or "warn"
                    why.append(f"일시 지연 내성 {s['tolerance_s']}초 (한계 {s['limit_s']}초의 {tr * 100:.0f}%)")
            if sev:
                cap.append((sev, g, why, s))
            hr = s.get("hold_ratio")
            chg = s.get("interval_change_pct")
            if hr is not None and s["n_holds"] >= c["eval_samples"] and \
                    (chg is None or abs(chg) <= c["interval_tol"] * 100):
                if hr >= c["drift_crit"]:
                    drift.append(("crit", g, s))
                elif hr >= c["drift_warn"]:
                    drift.append(("warn", g, s))
        state = {}
        for sev, g, _why, _s in cap:
            state[g] = "crit" if sev == "crit" or state.get(g) == "crit" else "warn"
        for sev, g, _s in drift:
            state[g] = "crit" if sev == "crit" or state.get(g) == "crit" else state.get(g, "warn")
        for r in summ:
            if r["group"] in state:
                r["status"] = state[r["group"]]
            elif r["idle_baseline"] is None:
                r["status"] = "learning"
        self.summary = sorted(summ, key=lambda r: r["group"])
        out = []
        for sev in ("crit", "warn"):
            items = [i for i in cap if i[0] == sev]
            if items:
                items.sort(key=lambda i: (i[3].get("idle_median") or 0, i[3].get("tolerance_ratio") or 1))
                sev_, g, why, s = items[0]
                more = f" 외 {len(items) - 1}개 그룹" if len(items) > 1 else ""
                out.append({"rule": "capacity_drift", "severity": sev,
                            "title": f"처리 여유 감소 — 그룹 {g}{more}",
                            "evidence": " · ".join(why) + (
                                f" · 점유 p50 {s['hold_p50']}초(기준 {s['baseline'].get('hold_p50')}초)"
                                if s.get("hold_p50") else ""),
                            "key": sev, "groups": [i[1] for i in items], "meta": {"group": g}})
        for sev in ("crit", "warn"):
            items = [i for i in drift if i[0] == sev]
            if items:
                items.sort(key=lambda i: -(i[2].get("hold_ratio") or 0))
                _sv, g, s = items[0]
                more = f" 외 {len(items) - 1}개 그룹" if len(items) > 1 else ""
                b = s["baseline"]
                chg = s.get("interval_change_pct")
                out.append({"rule": "tact_drift", "severity": sev,
                            "title": f"처리 시간 증가 — 그룹 {g}{more} 기준의 {s['hold_ratio']}배",
                            "evidence": f"점유 p50 {b.get('hold_p50')}→{s['hold_p50']}초, 투입 간격 "
                                        f"{b.get('interval_s')}→{s['interval_s']}초"
                                        + (" (투입은 그대로 — 부하 변화 아님)" if chg is not None else "")
                                        + f" · 기준 {_mdhm(b.get('from'))}부터",
                            "key": sev, "groups": [i[1] for i in items], "meta": {"group": g}})
        return out

    # ── 시간별 KPI ─────────────────────────────────────────────
    def hourly_rows(self, now: float) -> list[dict]:
        """끝난 시간대마다 그룹별 KPI 행 (한 번만). 추세 판단용으로 그룹에도 남긴다."""
        h_now = int(now // 3600)
        if not self.last_hour:
            self.last_hour = h_now
            return []
        rows = []
        for h in range(self.last_hour, h_now):
            lo, hi = h * 3600, (h + 1) * 3600
            for g, x in sorted(self.g.items()):
                arr = self._since(x.arr, lo, hi - 1e-6)
                holds = [v[1] for v in self._since(x.holds, lo, hi - 1e-6)]
                if not arr and not holds:
                    continue
                idle = [a[1] for a in arr]
                ph = self._since(x.phases, lo, hi - 1e-6)
                row = {"hour": dt.datetime.fromtimestamp(lo).strftime("%Y-%m-%d %H:00"),
                       "group": g, "n": len(arr),
                       "idle_median": statistics.median(idle) if idle else None,
                       "idle_hist": dict(sorted(Counter(idle).items())),
                       "hold_p50": _r(statistics.median(holds), 3) if holds else None,
                       "hold_p95": _r(q(holds, 0.95), 3), "hold_p99": _r(q(holds, 0.99), 3),
                       "hold_max": _r(max(holds), 3) if holds else None,
                       "interval_s": _r(self.interval(g, lo, hi), 4), "pool": self.pool(g),
                       "rejects": sum(1 for r in x.rejects if lo <= r[0] < hi)}
                if ph:
                    row["phases"] = [_r(statistics.mean(p[i] for p in ph), 3) for i in (1, 2, 3)]
                x.hourly.append(row)
                rows.append(row)
            us = [u for u in self.usage if lo <= u[0] < hi]
            if us:
                rows.append({"hour": dt.datetime.fromtimestamp(lo).strftime("%Y-%m-%d %H:00"),
                             "group": "process", "mem_mb_max": _r(max(u[1] for u in us), 0),
                             "cpu_pct": _r(statistics.mean(u[2] for u in us if u[2] is not None), 1)
                             if any(u[2] is not None for u in us) else None,
                             "total_cpu_pct": _r(statistics.mean(u[3] for u in us if u[3] is not None), 1)
                             if any(u[3] is not None for u in us) else None,
                             "threads": max((u[4] or 0) for u in us) or None})
        self.last_hour = h_now
        if rows:
            self.changed = True                     # 추세(시간별 행)도 재시작 뒤 이어지게 저장
        return rows

    # ── 근거 (사건 분석용) ──────────────────────────────────────
    def resources(self, now: float) -> dict:
        us = list(self.usage)
        if not us:
            return {}
        hour = [u for u in us if now - 3600 <= u[0] <= now]
        start = [u for u in us if u[0] <= us[0][0] + 3600]
        out = {"talos_cpu_now_pct": None, "total_cpu_now_pct": None}
        if hour and any(u[2] is not None for u in hour):
            out["talos_cpu_now_pct"] = _r(statistics.mean(u[2] for u in hour if u[2] is not None), 1)
        if hour and any(u[3] is not None for u in hour):
            out["total_cpu_now_pct"] = _r(statistics.mean(u[3] for u in hour if u[3] is not None), 1)
        if start and any(u[2] is not None for u in start):
            out["talos_cpu_start_pct"] = _r(statistics.mean(u[2] for u in start if u[2] is not None), 1)
        out["since"] = _mdhm(us[0][0])
        out["threads_start"], out["threads_now"] = start[0][4], us[-1][4]
        if len(us) >= 10 and us[-1][0] - us[0][0] >= 1800:
            xs = [u[0] for u in us]
            ys = [u[1] for u in us]
            mx, my = statistics.mean(xs), statistics.mean(ys)
            den = sum((t - mx) ** 2 for t in xs) or 1e-9
            out["mem_slope_mb_per_h"] = _r(sum((t - mx) * (v - my) for t, v in zip(xs, ys)) / den * 3600, 1)
        return out

    def evidence(self, now: float, focus: int | None) -> dict:
        """사건 근거: capacity(관심 그룹) · trend(기준선 대비, 여러 그룹) · phases · transient."""
        if not self.g:
            return {}
        gs = sorted(self.g)
        if focus not in self.g:
            # 관심 그룹을 모르면 유휴 중앙값이 가장 낮은 그룹
            cand = [(self.stats(g, now), g) for g in gs]
            cand = [(s.get("idle_median") if s.get("idle_median") is not None else 99, g)
                    for s, g in cand if s["n_arrivals"]]
            focus = min(cand)[1] if cand else gs[0]
        s = self.stats(focus, now)
        base = s.get("baseline") or {}
        cap = {k: s.get(k) for k in ("group", "pool", "interval_s", "limit_s", "idle_median",
                                     "idle_min", "idle_p01", "hold_p50", "hold_p99", "hold_max",
                                     "tolerance_s", "tolerance_ratio", "hold_source",
                                     "rejects_60min")}
        cap["idle_baseline"] = base.get("idle_median")
        cap["groups_total"] = len(gs)
        groups = []
        for g in gs:
            t = self.stats(g, now)
            if not t["n_arrivals"] and not t["n_holds"]:
                continue
            series = [r["hold_p50"] for r in self._grp(g).hourly if r.get("hold_p50")][-24:]
            groups.append({"group": g, "hold_ratio": t.get("hold_ratio"),
                           "idle_median": t.get("idle_median"),
                           "idle_baseline": (t.get("baseline") or {}).get("idle_median"),
                           "interval_change_pct": t.get("interval_change_pct"),
                           "spearman_hourly": _r(spearman(series)), "hours": len(series)})
        rising = [x for x in groups if (x["hold_ratio"] or 0) >= 1.1]
        fx = next((x for x in groups if x["group"] == focus), {})
        trend = {"since": _mdhm(base.get("from")) if base else None,
                 "baseline_reason": base.get("reason"),
                 "hours": _r((now - base["from"]) / 3600, 1) if base else None,
                 "hold_p50_baseline": base.get("hold_p50"), "hold_p50_now": s.get("hold_p50"),
                 "hold_ratio": s.get("hold_ratio"),
                 "interval_baseline_s": base.get("interval_s"), "interval_now_s": s.get("interval_s"),
                 "interval_change_pct": s.get("interval_change_pct"),
                 "spearman_hourly": fx.get("spearman_hourly"),
                 "monotonic": (fx.get("spearman_hourly") or 0) >= 0.8 if fx.get("hours", 0) >= 4 else None,
                 "groups_rising": [x["group"] for x in rising], "groups_with_baseline":
                     sum(1 for x in groups if x["hold_ratio"] is not None),
                 "groups": groups[:16]}
        ev = {"capacity": cap, "trend": trend}
        ph = self._since(self._grp(focus).phases, now - 3600, now)
        if ph and base.get("phases"):
            now_ph = [_r(statistics.mean(p[i] for p in ph), 3) for i in (1, 2, 3)]
            inc = [max(0.0, n - b) for n, b in zip(now_ph, base["phases"])]
            tot = sum(inc) or 1.0
            ev["phases"] = {"group": focus, "names": ["처리(시작~판정)", "결과 송신", "후처리(저장·DB)"],
                            "baseline_s": base["phases"], "now_s": now_ph,
                            "share_of_increase_pct": [_r(100 * v / tot, 0) for v in inc]}
        tr = {}
        for g in gs:
            x = self._grp(g)
            last = [h[1] for h in self._since(x.holds, now - 60, now)]
            prev = [h[1] for h in self._since(x.holds, now - 3660, now - 60)]
            if last and len(prev) >= self.c["eval_samples"]:
                p = q(prev, 0.99)
                if p:
                    tr[g] = {"max_last_60s": _r(max(last)), "p99_prev_60min": _r(p),
                             "ratio": _r(max(last) / p)}
        hot = sorted((g for g, v in tr.items() if v["ratio"] >= 1.3), key=lambda g: -tr[g]["ratio"])
        if hot:
            ev["transient"] = {"groups": hot[:8], "n_groups": len(hot), "detail": {g: tr[g] for g in hot[:4]}}
        res = self.resources(now)
        if res:
            ev["resources"] = res
        return ev

    # ── 저장 ───────────────────────────────────────────────────
    def state(self) -> dict:
        return {"version": 1, "gen_start": self.gen_start, "gen_reason": self.gen_reason,
                "groups": {str(g): x.base for g, x in self.g.items() if x.base},
                "hourly": {str(g): list(x.hourly)[-48:] for g, x in self.g.items() if x.hourly}}

    def load(self, st: dict):
        if not isinstance(st, dict) or st.get("version") != 1:
            return
        self.gen_start = float(st.get("gen_start") or 0)
        self.gen_reason = str(st.get("gen_reason") or "")
        for g, b in (st.get("groups") or {}).items():
            try:
                self._grp(int(g)).base = b
            except (TypeError, ValueError):
                continue
        for g, rows in (st.get("hourly") or {}).items():
            try:
                self._grp(int(g)).hourly.extend(rows or [])
            except (TypeError, ValueError):
                continue


class StateFile:
    """capacity 상태(기준선)와 시간별 KPI 를 data_dir 에 쓴다. 리플레이는 _replay 접미사."""

    def __init__(self, data_dir: str, replay: bool = False):
        self.dir = data_dir
        self.sfx = "_replay" if replay else ""
        self.path = os.path.join(data_dir, f"capacity_state{self.sfx}.json")

    def load(self, model: CapacityModel):
        try:
            with open(self.path, encoding="utf-8") as f:
                model.load(json.load(f))
        except (OSError, ValueError):
            pass

    def save(self, model: CapacityModel):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(model.state(), f, ensure_ascii=False)
            os.replace(tmp, self.path)
            model.changed = False
        except OSError as e:
            print(f"  ! 처리 여유 상태 저장 실패(계속): {e}")

    def write_kpi(self, rows: list[dict]):
        if not rows:
            return
        by_day: dict[str, list] = {}
        for r in rows:
            by_day.setdefault(r["hour"][:10].replace("-", ""), []).append(r)
        for day, items in by_day.items():
            try:
                with open(os.path.join(self.dir, f"kpi_{day}{self.sfx}.jsonl"), "a",
                          encoding="utf-8") as f:
                    for r in items:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
            except OSError as e:
                print(f"  ! KPI 기록 실패(계속): {e}")
