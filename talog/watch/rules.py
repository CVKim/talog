"""룰 엔진 — 이벤트를 슬라이딩 윈도우로 보며 경보 조건(거부·정체·에러·재시작·결함·처리 여유)을 판정한다."""

from __future__ import annotations

import fnmatch
import statistics
from collections import Counter, OrderedDict, deque

from ..assemble import _parse_ng_defects
from ..events import Event
from ..capacity import CapacityModel
from ..tracker import PatternEngine
from .alerts import Alert, Notifier


def _defect_rules(dw: dict) -> list[dict]:
    """결함 규칙 목록 (rules.defect_watch.rules). 옛 키 critical(1건 즉시 심각)·
    repeat_count(같은 결함 N건 주의)도 같은 형태의 규칙으로 옮긴다."""
    win = float(dw.get("repeat_window_min", 30) or 30)
    items = []
    if dw.get("critical"):
        items.append(({"match": dw["critical"], "severity": "crit", "count": 1,
                       "window_min": win,
                       "cooldown_min": dw.get("critical_cooldown_min", 10)}, "crit"))
    if int(dw.get("repeat_count", 0) or 0):
        items.append(({"match": ["*"], "severity": "warn",
                       "count": int(dw["repeat_count"]), "window_min": win}, "rep"))
    for i, it in enumerate(dw.get("rules") or []):
        if isinstance(it, dict) and it.get("enabled", True) is not False:
            items.append((it, f"d{i}"))
    out = []
    for it, key in items:
        m = it.get("match") or []
        pats = [m] if isinstance(m, str) else list(m)
        pats = [str(p).strip().upper() for x in pats for p in str(x).split(",") if str(p).strip()]
        if not pats:
            continue
        sev = str(it.get("severity", "crit")).lower()
        n = max(1, int(it.get("count", 1) or 1))
        cd = it.get("cooldown_min")
        out.append({"name": str(it.get("name") or ""), "match": pats, "key": key,
                    "severity": sev if sev in ("info", "warn", "crit") else "crit",
                    "count": n, "window_s": float(it.get("window_min", 10) or 10) * 60,
                    "cooldown_min": float(cd) if cd not in (None, "") else
                    (10.0 if n == 1 else None), "hits": deque()})
    return out


class RuleEngine:
    """슬라이딩 윈도우 기반 이상 판정 (실시간/리플레이 공용).

    진행 중 검사는 (inner id, Seq 그룹) 으로 잡는다. 한 제품(inner id)이 여러 존(그룹)을 시차를 두고
    지나가는 설비(풍산 2호기: side1 → bottom → side2, 1.5초 간격)에서 inner id 하나로 잡으면 뒤 존의
    시작이 앞 존을 덮어쓰고 먼저 끝난 존의 END 가 진행 중 목록에서 지워 버린다. 그룹은 InspStarter 의
    수신 줄(groupId) → comm 시작 회신의 마지막 토큰 → seq_N 시작 순으로 얻는다(없으면 그룹 0 = 단일).
    """

    def __init__(self, cfg: dict, notifier: Notifier):
        self.cfg = cfg["rules"]
        self.notify = notifier
        self.errors: deque = deque()              # (ts, key)
        self.restarts: deque = deque()            # ts (create 이벤트)
        self.pending: dict[str, float] = {}       # 진행 중 키(inner 또는 inner@그룹) -> 시작 ts
        self._last_start = None                   # (inner, ts, 대기 스레드, 그룹) — 거부 귀속용
        self.durations: deque = deque(maxlen=200)  # 완료 소요(초)
        self.mem: deque = deque()                 # (ts, MB)
        # 사건 분석 근거용 상태 (agent.EvidenceBuilder 가 읽는다)
        self.recent: deque = deque()              # 최근 90분 핵심 이벤트
        self.done: deque = deque(maxlen=200)      # (END ts, inner, 소요 s, 그룹)
        # 결함명 감시 상태 (다존 설비는 inner 단위로 NG 스티키 집계)
        self.dw = self.cfg.get("defect_watch", {})
        self.results: OrderedDict = OrderedDict()  # inner -> {ts, result, defects}
        self.defect_hist: deque = deque()         # (ts, 결함명, inner)
        self.defect_rules = _defect_rules(self.dw)
        # 사용자 정의 로그 패턴 (TailReader 가 줄 단위로 넘긴다)
        self.patterns = PatternEngine(self.cfg.get("patterns") or [], notifier, Alert)
        # 처리 여유 (그룹별 유휴 스레드·점유 시간·투입 간격·기준선) — talog/capacity.py
        self.cap = CapacityModel(self.cfg.get("capacity"))
        self.kpi_sink = None                      # 시간별 KPI 기록 함수 (LiveWatch·리플레이가 꽂음)
        self.state_sink = None                    # 기준선 저장 함수
        self._pgroup: dict[str, int] = {}         # 진행 중 키 -> 그룹
        self._inflight: Counter = Counter()       # 그룹 -> 진행 중 수
        self._dur_g: dict[int, deque] = {}        # 그룹별 완료 소요 (정체 판정)
        self._done_ts: dict[int, float] = {}      # 그룹별 마지막 완료(INSPECT_END) 시각
        self._recv: OrderedDict = OrderedDict()   # inner -> [(그룹, ts)]  InspStarter 수신 줄
        self._unassigned: dict[str, deque] = {}   # 그룹을 아직 모르는 도착 inner -> [(ts, 유휴)]
        self._assigned: dict[str, deque] = {}     # inner -> [(ts, 그룹)] 그룹이 정해진 도착
        self._seq_open: dict = {}                 # (seq 번호, 객체) -> {start, inner, proc, send}
        self._seq_votes: dict[int, Counter] = {}  # seq 파일 번호 -> 그룹 투표
        self._seq_wait: dict[str, deque] = {}     # 그룹을 모르는 seq 시작 inner -> [(ts, seq 번호)]
        self._seq_objs: dict[int, set] = {}       # seq 번호 -> 이번 세대 CommonSeq 인스턴스(스레드) ID
        self._seq_gen = 0

    # 근거 기록 대상 (comm 은 검사 라이프사이클·모델 로드·비상정지만)
    _REC_KINDS = frozenset((
        "INSP_START", "INSP_REJECT", "REJECT_BUSYCAM", "REJECT_NOTREADY",
        "REJECT_SIM", "COMM_MSG", "IMG_TIMEOUT", "ALG_TIMEOUT", "GRAB_FAIL",
        "STORAGE_LOW", "LIGHT_UNSTABLE", "POOL_CREATE", "POOL_DESTROY", "BATCH",
        "CRASH", "EXC_SAFE", "ERROR", "EXC_REDIRECT", "MODEL_FAIL", "COMM_FAIL",
        "RECIPE_FAIL"))
    _REC_COMM = ("INSPECT_START_ACK", "INSPECT_END", "MODEL_LOAD", "EMERGENCY")

    def _record(self, e: Event):
        if e.kind not in self._REC_KINDS:
            return
        if e.kind == "COMM_MSG" and not any(k in e.name for k in self._REC_COMM):
            return
        self.recent.append(e)
        while self.recent and e.ts - self.recent[0].ts > 5400:
            self.recent.popleft()

    def ng_streak_len(self) -> int:
        n = 0
        for v in reversed(self.results.values()):
            if v["result"] not in ("NG", "REWORK"):
                break
            n += 1
        return n

    # ── 진행 중 검사 (inner, 그룹) ────────────────────────────────
    @staticmethod
    def pkey(inner: str, g: int) -> str:
        return f"{inner}@{g}" if g else inner

    @staticmethod
    def inner_of(key: str) -> str:
        return key.split("@", 1)[0]

    def group_of(self, key: str) -> int:
        return self._pgroup.get(key, 0)

    def _padd(self, inner: str, g: int, ts: float):
        k = self.pkey(inner, g)
        if k not in self.pending:
            self._inflight[g] += 1
        self.pending[k] = ts
        self._pgroup[k] = g

    def _ppop(self, inner: str, g: int = 0):
        """진행 중에서 뺀다 → (시작 ts, 그룹). 그룹 키가 없으면 그룹을 모르던 키를 본다."""
        for k in ((self.pkey(inner, g), inner) if g else (inner,)):
            if k in self.pending:
                gg = self._pgroup.pop(k, 0)
                self._inflight[gg] = max(0, self._inflight[gg] - 1)
                return self.pending.pop(k), gg
        return None, g

    def _pclear(self):
        self.pending.clear()
        self._pgroup.clear()
        self._inflight.clear()
        self._unassigned.clear()
        self._seq_open.clear()
        self._seq_wait.clear()

    def _arrival(self, inner: str, ts: float, g: int, idle: int, vote: bool = True):
        self._assigned.setdefault(inner, deque(maxlen=8)).append((ts, g))
        infl = self._vote_inflight(g, ts, self.pkey(inner, g)) if vote else None
        a = self.cap.arrival(ts, g, idle, infl)
        if a:
            self._emit_cap(ts, a)

    def _vote_inflight(self, g: int, ts: float, own: str):
        """풀 추정에 쓸 그룹 g 의 진행 중 수 (이번 도착 제외). 완료 신호가 끊긴 그룹은 None —
        comm 로그가 끊기거나(회전·복사 지연·미추적) 회신이 안 오면 진행 중이 끝없이 쌓여 풀이 부푼다
        (Tenneco 0730: comm 이 10분 먼저 끊겨 풀 3 → 356). 완료 줄이 유실돼 남은 오래된 진행 중도 뺀다."""
        t = self._done_ts.get(g)
        d = self._dur_g.get(g)
        if t is None or not d:
            return None
        lim = max(10.0, 2.0 * max(d))
        if ts - t > lim:
            return None
        return sum(1 for k, st in self.pending.items()
                   if k != own and 0 <= ts - st <= lim and self._pgroup.get(k, 0) == g)

    def _assign(self, inner: str, g: int):
        """그룹을 모르던 도착(수신 줄이 없는 사이트)에 그룹을 붙인다 — 회신·seq 시작 순서는 도착 순서와 같다."""
        q = self._unassigned.get(inner)
        if not q:
            return
        ts0, idle = q.popleft()
        if not q:
            del self._unassigned[inner]
        if g:
            if inner in self.pending and self._pgroup.get(inner, 0) == 0:
                self._ppop(inner, 0)                # 그룹 없는 키 → 그룹 키
            self._padd(inner, g, ts0)
        self._arrival(inner, ts0, g, idle)
        w = self._seq_wait.get(inner)
        if w:
            _sts, n = w.popleft()
            if not w:
                del self._seq_wait[inner]
            self._vote(n, g)

    def _recent_group(self, inner: str, ts: float):
        """ts 직전(1.5초 안)에 그룹이 정해진 같은 inner 의 도착 → 그 그룹."""
        best = None
        for t0, g in self._assigned.get(inner, ()):
            if 0 <= ts - t0 <= 1.5 and (best is None or t0 > best[0]):
                best = (t0, g)
        return best[1] if best else None

    def _g_ok(self, g: int) -> bool:
        """그룹 g 로 기록해도 되는가 — 그룹 0(미상)은 그룹 정보가 아예 없는 단일 그룹 사이트에서만."""
        return bool(g) or not any(k for k in self.cap.g)

    def _vote(self, n: int, g: int):
        if n and self._g_ok(g):
            self._seq_votes.setdefault(n, Counter())[g] += 1

    def seq_group(self, n: int):
        """seq_N 파일 → 그룹 (도착과 seq 시작을 짝지어 배운 값). 모르면 None."""
        v = self._seq_votes.get(n)
        if v:
            g, k = v.most_common(1)[0]
            if k >= 3 and k >= 0.8 * sum(v.values()):
                return g
        return None

    def _emit_cap(self, ts: float, a: dict):
        c = self.cap.c
        cd = 30 if a["key"] == "near" else float(
            c.get("cooldown_min", 60) if a["severity"] == "crit" else c.get("warn_cooldown_min", 240))
        self.notify.emit(Alert(ts, a["rule"], a["severity"], a["title"], a["evidence"],
                               key=a["key"], cooldown_min=cd, meta=a.get("meta")))

    def _on_inspect_end(self, e: Event):
        """INSPECT_END 판정으로 치명 결함·동일 결함 빈발·연속 NG·NG 비율을 본다."""
        dw = self.dw
        if not dw.get("enabled", True):
            return
        res = e.status or "?"
        defects = (_parse_ng_defects(e.extra, e.inner_id, res)
                   if res in ("NG", "REWORK") else [])
        cur = self.results.get(e.inner_id)
        if cur is None:
            cur = {"ts": e.ts, "result": res, "defects": []}
            self.results[e.inner_id] = cur
            if len(self.results) > 1000:
                self.results.popitem(last=False)
        elif cur["result"] not in ("NG", "REWORK"):
            cur["result"] = res                   # 존 하나라도 NG 면 NG 유지
        new = [d for d in defects if d not in cur["defects"]]
        cur["defects"].extend(new)
        win = dw.get("repeat_window_min", 30) * 60
        for d in new:
            self.defect_hist.append((e.ts, d, e.inner_id))
        while self.defect_hist and e.ts - self.defect_hist[0][0] > win:
            self.defect_hist.popleft()
        # 결함 규칙: 결함명(와일드카드)이 window 분 안에 count 건 — 같은 결함명끼리 센다
        for r in self.defect_rules:
            for d in dict.fromkeys(new):
                if not any(fnmatch.fnmatchcase(d.upper(), p) for p in r["match"]):
                    continue
                r["hits"].append((e.ts, d))
                while r["hits"] and e.ts - r["hits"][0][0] > r["window_s"]:
                    r["hits"].popleft()
                n = sum(1 for _t, x in r["hits"] if x == d)
                if n < r["count"]:
                    continue
                crit = r["severity"] == "crit"
                if r["count"] == 1:
                    title = f"{'치명 결함' if crit else '결함'} 검출: {d}"
                    ev = (f"inner id {e.inner_id} · 판정 {res} {'/'.join(cur['defects'])} · "
                          f"최근 {r['window_s'] / 60:.0f}분 {n}건 — 결함 이미지를 확인하고 "
                          f"제품을 격리하십시오.")
                else:
                    title = f"동일 결함 빈발: {d} {r['window_s'] / 60:.0f}분 내 {n}건"
                    ev = (f"마지막 inner id {e.inner_id} — 공정 이상 또는 과검출 여부를 "
                          f"이미지로 확인하십시오.")
                self.notify.emit(Alert(
                    e.ts, "defect_critical" if crit else "defect_repeat", r["severity"],
                    title, ev, key=f"{r['key']}:{d}", cooldown_min=r["cooldown_min"] or 0))
        streak = self.ng_streak_len()
        need = int(dw.get("ng_streak", 0) or 0)
        if need and streak >= need:
            top = Counter(d for v in list(self.results.values())[-streak:]
                          for d in v["defects"]).most_common(3)
            self.notify.emit(Alert(
                e.ts, "ng_streak", "crit", f"연속 NG {streak}건"
                + (f" ({'·'.join(f'{d} {n}' for d, n in top)})" if top else ""),
                f"마지막 inner id {e.inner_id} — 촬상·조명·티칭 이상 또는 공정 이상 "
                f"여부를 확인하십시오.", key="streak"))
        n_win = int(dw.get("ng_rate_window", 0) or 0)
        if n_win and len(self.results) >= n_win:
            last = list(self.results.values())[-n_win:]
            pct = 100.0 * sum(1 for v in last if v["result"] in ("NG", "REWORK")) / n_win
            if pct >= float(dw.get("ng_rate_percent", 50)):
                self.notify.emit(Alert(
                    e.ts, "ng_rate", "warn", f"NG 비율 {pct:.0f}% (최근 {n_win}검사)",
                    f"기준 {dw.get('ng_rate_percent', 50)}% 초과 — 결함 분포와 기종 교체 "
                    f"여부를 확인하십시오.", key="ngrate"))

    def _no_insp_alert(self, ts: float, evidence: str, g: int):
        if self.cfg["no_insp_thread"].get("enabled", True):
            self.notify.emit(Alert(
                ts, "no_insp_thread", "crit", "검사 시작 거부(NoInspThread) — 미검사 임박",
                evidence, key="noinsp", meta={"group": g} if g else None))

    def feed(self, e: Event):
        self._record(e)
        if e.kind in ("ERROR", "EXC_REDIRECT", "MODEL_FAIL", "CRASH",
                      "COMM_FAIL", "RECIPE_FAIL"):
            key = e.model or (e.extra or e.name or e.kind)[:60]
            self.errors.append((e.ts, key))
            if e.kind == "CRASH":
                self.notify.emit(Alert(e.ts, "crash", "crit",
                                       "프로세스 크래시 감지",
                                       "exception.log 에 unhandled exception 기록",
                                       key="crash"))
        elif e.kind == "INSP_RECV":
            # 같은 inner 의 수신이 존마다 잇달아 올 수 있다 — 도착 줄과 순서대로 짝짓는다
            q = self._recv.pop(e.inner_id, None) or deque(maxlen=16)
            q.append((int(e.value or 0), e.ts))
            self._recv[e.inner_id] = q
            if len(self._recv) > 5000:
                self._recv.popitem(last=False)
        elif e.kind == "INSP_START":
            g = 0
            q = self._recv.get(e.inner_id)
            while q and e.ts - q[0][1] > 2.0:
                q.popleft()                       # 짝 없이 남은 오래된 수신
            if q and 0 <= e.ts - q[0][1] <= 2.0:
                g = q.popleft()[0]
            idle = int(e.value)
            if g:
                self._padd(e.inner_id, g, e.ts)
                self._arrival(e.inner_id, e.ts, g, idle)
            else:
                # 수신 줄이 없는 사이트: 회신(그룹 토큰)·seq 시작이 그룹을 알려 줄 때까지 둔다
                self._padd(e.inner_id, 0, e.ts)
                self._unassigned.setdefault(e.inner_id, deque()).append((e.ts, idle))
            self._last_start = (e.inner_id, e.ts, e.value, g)
        elif e.kind == "INSP_REJECT":
            # 거부 라인은 같은 InspStarter 의 도착 라인(대기 스레드 0) 바로 뒤에 찍힌다 —
            # 그 검사는 시작되지 않았으므로 진행 중에서 뺀다 (남기면 몇 분 뒤 거짓 '검사 정체').
            # 도착 라인이 없는 신형 사이트는 직전 도착이 대기 스레드 1 이상이라 건드리지 않는다
            ls = self._last_start
            g = 0
            if ls and ls[2] == 0 and 0 <= e.ts - ls[1] <= 2:
                g = ls[3]
                self._ppop(ls[0], g)
                if self._g_ok(g):
                    self.cap.reject(e.ts, g)
            self._no_insp_alert(e.ts, "가용 Seq 스레드 0. 직전 검사들이 스레드를 점유 중 — "
                                      "병목/정체를 즉시 확인하십시오."
                                + (f" (그룹 {g})" if g else ""), g)
        elif e.kind in ("REJECT_BUSYCAM", "REJECT_NOTREADY", "REJECT_SIM"):
            label = {"REJECT_BUSYCAM": "카메라 점유(BusyCam)",
                     "REJECT_NOTREADY": "모델 미로드 상태",
                     "REJECT_SIM": "시뮬레이션 모드 방치"}[e.kind]
            self.notify.emit(Alert(e.ts, e.kind.lower(), "crit",
                                   f"검사 시작 거부 — {label}",
                                   "설비의 검사 요청이 거부되었습니다. 원인을 "
                                   "즉시 확인하십시오.", key=e.kind))
        elif e.kind == "GRAB_FAIL":
            self.notify.emit(Alert(e.ts, "grab_fail", "crit",
                                   "그랩 실패 — 카메라/트리거 계통",
                                   "조명 소등·설비 정지로 이어지는 경로입니다. "
                                   "카메라 연결과 트리거를 점검하십시오.",
                                   key="grab"))
        elif e.kind == "IMG_TIMEOUT":
            self.notify.emit(Alert(e.ts, "img_timeout", "crit",
                                   "검사 타임아웃 — 판정 미송신",
                                   f"inner id {e.inner_id}: 설비 측은 미검사로 "
                                   f"처리됩니다. GPU 부하/병목을 확인하십시오.",
                                   key="ito"))
        elif e.kind == "ALG_TIMEOUT":
            self.notify.emit(Alert(e.ts, "alg_timeout", "warn",
                                   f"알고리즘 타임아웃 (이미지 {e.roi_idx})",
                                   f"임계 {e.value:.0f}ms 초과 — TIME_OUT NG 로 "
                                   f"강제 판정됩니다.", key=f"ato{e.roi_idx}"))
        elif e.kind in ("STORAGE_LOW", "LIGHT_UNSTABLE"):
            lbl = ("이미지 저장 공간 부족" if e.kind == "STORAGE_LOW"
                   else "조명 컨트롤러 불안정")
            self.notify.emit(Alert(e.ts, e.kind.lower(), "crit", lbl,
                                   "설비 정지(emergency stop)로 이어질 수 있는 "
                                   "상태입니다.", key=e.kind))
        elif e.kind == "COMM_MSG" and e.inner_id:
            g = int(e.value or 0)
            if "INSPECT_END" in e.name:
                st, gg = self._ppop(e.inner_id, g)
                if st is not None:
                    d = e.ts - st
                    self.durations.append(d)
                    self.done.append((e.ts, e.inner_id, d, gg))
                    self._dur_g.setdefault(gg, deque(maxlen=200)).append(d)
                    self._done_ts[gg] = e.ts
                    if self._g_ok(gg):
                        self.cap.hold(e.ts, gg, d, "cycle")
                self._on_inspect_end(e)
            elif "INSPECT_START_ACK" in e.name:
                self._assign(e.inner_id, g)
                if e.status and e.status != "OK":
                    self._ppop(e.inner_id, g)
                    if self._g_ok(g):
                        self.cap.reject(e.ts, g)
                    # 설비 회신(comm)으로도 NoInspThread 를 잡는다 — InspStarter 거부
                    # 라인과 같은 key 라 한 번만 발보된다
                    if e.status == "NoInspThread":
                        self._no_insp_alert(
                            e.ts, f"설비 회신 NoInspThread (inner id {e.inner_id}"
                                  + (f", 그룹 {g}" if g else "")
                                  + ") — 가용 Seq 스레드 0. 병목/정체를 즉시 확인하십시오.", g)
        elif e.kind == "COMM_MSG" and e.name.startswith("M2V_MODEL_LOAD") and \
                not e.name.endswith("ACK"):
            name = e.extra.split(",")[-1].strip() if e.extra else ""
            self.cap.reset(e.ts, f"모델 로드{(' ' + name) if name else ''}")
        elif e.kind == "SEQ_START":
            n = int(e.alg_idx or 0)
            self._seq_open[(n, e.obj_id)] = {"start": e.ts, "inner": e.inner_id}
            if self._seq_gen != self.cap.gen_id:      # 재시작·기종 교체 → 인스턴스를 새로 센다
                self._seq_objs.clear()
                self._seq_gen = self.cap.gen_id
            if e.obj_id:
                objs = self._seq_objs.setdefault(n, set())
                if len(objs) <= 64:
                    objs.add(e.obj_id)
            g = self.seq_group(n)
            if g is not None and n in self._seq_objs:
                self.cap.seq_threads[g] = len(self._seq_objs[n])
            if g is None:
                # 같은 inner 의 도착이 아직 그룹을 못 받았으면(회신보다 seq 시작이 먼저 찍힌 경우)
                # 직전에 그룹이 정해진 앞 존 도착과 짝지으면 seq↔그룹이 한 칸씩 밀린다 — 회신을 기다린다
                gg = (None if self._unassigned.get(e.inner_id)
                      else self._recent_group(e.inner_id, e.ts))
                if gg is not None:
                    self._vote(n, gg)
                elif e.inner_id:
                    w = self._seq_wait.setdefault(e.inner_id, deque(maxlen=8))
                    w.append((e.ts, n))
            elif self._unassigned.get(e.inner_id):
                self._assign(e.inner_id, g)       # 회신보다 seq 시작을 먼저 읽은 경우
        elif e.kind in ("PROC_TIME", "SEND_RESULT"):
            o = self._seq_open.get((int(e.alg_idx or 0), e.obj_id))
            if o is not None:
                o["proc" if e.kind == "PROC_TIME" else "send"] = e.value / 1000.0
        elif e.kind == "SEQ_FINISH":
            n = int(e.alg_idx or 0)
            o = self._seq_open.pop((n, e.obj_id), None)
            g = self.seq_group(n)
            if g is not None and self._g_ok(g):
                hold = e.value / 1000.0
                self.cap.hold(e.ts, g, hold, "seq")
                if o and "proc" in o and "send" in o:
                    self.cap.phase(e.ts, g, o["proc"], o["send"], hold)
        elif e.kind == "POOL_CREATE":
            # 기동 1회에 풀 5종(eFunction/eDraw/eLongRunning/eSaver/eFovProc)이 함께
            # 생성된다 — 30초 안의 흔적은 재시작 1회로 센다 (build_process_gens 와 같은 규칙)
            if not self.restarts or e.ts - self.restarts[-1] > 30:
                self.restarts.append(e.ts)
                self.cap.reset(e.ts, "재시작")
            self._pclear()                        # 재시작 시 진행분 소실
        elif e.kind == "USAGE":
            self.mem.append((e.ts, e.value))
            try:
                cpu = float(e.status) if e.status else None
                total = float(e.extra) if e.extra else None
                thr = int(e.name) if e.name and e.name.isdigit() else None
            except ValueError:
                cpu = total = thr = None
            self.cap.usage_sample(e.ts, e.value, cpu, total, thr)

    def _housekeep(self, now: float):
        """그룹을 끝내 못 받은 도착은 그룹 0(단일)으로 넘기고, 짝 없는 seq 시작은 버린다.
        1시간 넘게 끝나지 않은 진행 중 검사는 소실로 보고 뺀다 (정체 경보는 그 전에 이미 나간다)."""
        stale = [k for k, st in self.pending.items() if now - st > 3600]
        for k in stale:
            g = self._pgroup.pop(k, 0)
            self._inflight[g] = max(0, self._inflight[g] - 1)
            del self.pending[k]
        for inner in list(self._unassigned):
            q = self._unassigned[inner]
            while q and now - q[0][0] > 60:
                ts0, idle = q.popleft()
                if self._g_ok(0):                 # 여러 그룹 사이트면 가짜 그룹 0 을 만들지 않는다
                    self._arrival(inner, ts0, 0, idle, vote=False)
            if not q:
                del self._unassigned[inner]
        if len(self._seq_open) > 5000:
            for k in [k for k, v in self._seq_open.items() if now - v["start"] > 3600]:
                del self._seq_open[k]
        for inner in [k for k, w in self._seq_wait.items() if not w or now - w[-1][0] > 60]:
            del self._seq_wait[inner]
        if len(self._assigned) > 5000:
            for inner in [k for k, d in self._assigned.items() if not d or now - d[-1][0] > 60]:
                del self._assigned[inner]

    def evaluate(self, now: float):
        c = self.cfg
        # 1) 에러 반복
        w = c["error_repeat"]["window_min"] * 60
        while self.errors and now - self.errors[0][0] > w:
            self.errors.popleft()
        counts: dict[str, int] = {}
        for _t, k in self.errors:
            counts[k] = counts.get(k, 0) + 1
        for k, n in counts.items():
            if n >= c["error_repeat"]["count"]:
                self.notify.emit(Alert(
                    now, "error_repeat", "warn",
                    f"동일 에러 {c['error_repeat']['window_min']}분 내 {n}회 반복",
                    f"에러: {k}", key=k))
        # 2) 검사 정체 (완료 예정 시간 초과) — 그룹마다 자기 평소 소요로 본다
        meds = {g: statistics.median(d) for g, d in self._dur_g.items() if len(d) >= 5}
        stalled = []
        for key, st in self.pending.items():
            g = self._pgroup.get(key, 0)
            med = meds.get(g, 0)
            limit = max(c["insp_stall"]["min_seconds"],
                        med * c["insp_stall"]["factor_x_median"]) if med else \
                c["insp_stall"]["min_seconds"] * 3
            if now - st > limit:
                stalled.append((now - st, key, med))
        # 정체 검사는 한 경보로 묶는다 — inner 마다 따로 내면 장애·로그 절단 때 진행 중
        # 검사 수만큼 폭주한다 (Tenneco 0730 재생 402건)
        if stalled:
            stalled.sort(reverse=True)
            age, key, med = stalled[0]
            g = self._pgroup.get(key, 0)
            more = f" 외 {len(stalled) - 1}건" if len(stalled) > 1 else ""
            self.notify.emit(Alert(
                now, "insp_stall", "warn",
                f"검사 정체 {age:.0f}초 (정상 중앙값 {med:.0f}초){more}",
                f"inner id {self.inner_of(key)}{f' (그룹 {g})' if g else ''} 가 완료 신호 없이 "
                f"진행 중 — 소실 위험" + (f" (정체 {len(stalled)}건)" if more else ""),
                key="stall", cooldown_min=c["insp_stall"].get("cooldown_min", 10),
                meta={"group": g} if g else None))
        # 3) 재시작 빈발
        w = c["restart_burst"]["window_min"] * 60
        while self.restarts and now - self.restarts[0] > w:
            self.restarts.popleft()
        if len(self.restarts) >= c["restart_burst"]["count"]:
            self.notify.emit(Alert(
                now, "restart_burst", "crit",
                f"{c['restart_burst']['window_min']}분 내 재시작 "
                f"{len(self.restarts)}회",
                "반복 재시작 중 — 진행 중 검사가 소실됩니다. 원인 확인 전 "
                "추가 재시작을 자제하십시오.", key="burst"))
        # 4) 메모리 추세 (상한 포락선 기울기)
        mt = c["memory_trend"]
        horizon = max(mt["min_hours"] * 3600, 2 * 3600)
        while self.mem and now - self.mem[0][0] > horizon * 4:
            self.mem.popleft()
        if len(self.mem) >= 60:
            buckets: dict[int, float] = {}
            for t, mb in self.mem:
                b = int(t // 300)
                buckets[b] = max(buckets.get(b, 0.0), mb)
            xs = sorted(buckets)
            span_h = (xs[-1] - xs[0]) * 300 / 3600
            if span_h >= mt["min_hours"]:
                ys = [buckets[b] for b in xs]
                hx = [(b - xs[0]) * 300 / 3600 for b in xs]
                mx = statistics.mean(hx)
                my = statistics.mean(ys)
                var = sum((x - mx) ** 2 for x in hx) or 1e-9
                slope = sum((x - mx) * (y - my)
                            for x, y in zip(hx, ys)) / var
                rise = ys[-1] - ys[0]
                if slope > mt["mb_per_hour"] and rise > mt["min_rise_mb"]:
                    self.notify.emit(Alert(
                        now, "memory_trend", "crit",
                        f"메모리 증가 추세 +{slope:,.0f}MB/h ({span_h:.1f}h)",
                        f"상한 기준 +{rise:,.0f}MB — 릭 가능성, 계획 재시작 "
                        f"검토", key="mem",
                        cooldown_min=mt.get("cooldown_min", 240)))
        # 5) 처리 여유 · 처리 시간 추세 (그룹별 기준선 대비) — talog/capacity.py
        self._housekeep(now)
        for a in self.cap.evaluate(now):
            self._emit_cap(now, a)
        rows = self.cap.hourly_rows(now)
        if rows and self.kpi_sink is not None:
            self.kpi_sink(rows)
        if self.cap.changed and self.state_sink is not None:
            self.state_sink()
        while self.cap.events:
            print(f"[talog watch] 처리 여유: {self.cap.events.pop(0)}")
