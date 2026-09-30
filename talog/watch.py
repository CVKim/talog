"""talog watch — 예지보전 상주 감시 모드.

검사 프로그램(talos)이 메인으로 도는 설비 PC 에서 부하를 최소화하며
`D:\\AIV_LOG\\<프로세스>\\<YYYY_MM>\\<DD>\\` 로그를 증분(tail)으로 읽어
이상 징후를 조기 감지하고, 별도의 예지보전 로그(alerts JSONL)와
토스트 팝업/웹훅으로 알린다.

저부하 설계:
  - 폴링 tail (기본 20초, 새로 쓰인 바이트만 읽음)
  - 프로세스 우선순위 BELOW_NORMAL 강등
  - 감시 대상은 소형 플랫폼 로그 6종만 (alg/relgraph 등 대용량 제외)
  - LLM 은 선택 기능이며 기본 CPU 모드(num_gpu=0)로 검사 GPU 를 건드리지 않음

사용:
  python -m talog watch [--config watch.yaml] [--once]
  python -m talog watch --replay <일자 폴더> [--config watch.yaml]   # 사고 재현 검증
"""

from __future__ import annotations

import ctypes
import datetime as dt
import fnmatch
import json
import os
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass, field

import yaml

from .assemble import _parse_ng_defects
from .events import Event, Extractor
from .fileclass import classify
from .lineparser import _BATCH_RE, _TAG_RE, _TS_RE, LogRecord, _sniff_encoding
from .tracker import DEFAULT_FILES, PatternEngine, resolve_targets

# 감시 대상 파일 (소형·핵심만 — 저부하 원칙). 추적 모드 default 의 목록이다.
_WATCH_FILES = DEFAULT_FILES

_DEFAULT_CFG = {
    "watch_root": r"D:\AIV_LOG\Talos",
    "poll_seconds": 20,
    "alert_dir": r"D:\AIV_LOG\TalogWatch",
    "low_priority": True,
    "site": "",
    # 추적 대상: default(핵심 9종) | select(files 지정) | auto(일자 폴더 전체)
    "tracking": {"mode": "default", "files": [],
                 "exclude": ["DebugImageSaveInfo.log", "InspCondRelationGraph*.log"],
                 "include_alg": False, "max_initial_mb": 32},
    "rules": {
        "error_repeat": {"window_min": 10, "count": 5},
        "no_insp_thread": {"enabled": True},
        "insp_stall": {"factor_x_median": 2.0, "min_seconds": 180, "cooldown_min": 10},
        "restart_burst": {"window_min": 60, "count": 3},
        "memory_trend": {"mb_per_hour": 100, "min_rise_mb": 500,
                         "min_hours": 2.0},
        "gpu_temp": {"enabled": True, "celsius": 85, "record_min": 5},
        # 결함명 감시 (comm.log INSPECT_END NG/REWORK 페이로드). 기본은 모두 꺼짐 —
        # 사이트마다 결함명·NG 수준이 달라 watch.yaml 에서 켠다
        "defect_watch": {"enabled": True, "critical": [], "critical_cooldown_min": 10,
                         "repeat_window_min": 30, "repeat_count": 0,
                         "ng_streak": 0, "ng_rate_window": 0, "ng_rate_percent": 50},
        # 사용자 정의 로그 패턴 (tracker.PatternEngine): name/match/files/level/
        #   severity/count/window_min/cooldown_min — count 1 = 한 번만 나와도 경보
        "patterns": [],
        # 기본 룰 조정: {룰: {enabled, severity, count, window_min}} — count N 이면
        #   window_min 안에 N번 발생해야 경보 (이벤트형 룰 기준)
        "overrides": {},
    },
    "notify": {"toast": True, "webhook": "", "jsonl": True},
    "llm": {"enabled": False, "url": "http://127.0.0.1:11434", "device": "cpu",
            "model": "qwen2.5:7b", "cpu_threads": 0, "gpu_index": -1,
            "gpu_min_free_mb": 6000, "gpu_max_util": 40, "keep_alive": "",
            "num_ctx": 8192, "timeout_s": 600, "think": None,
            "script": "", "interval_min": 30},
    # 경보 사건 분석 (룰 진단 + LLM 2차 의견 + 합의 관문) — talog/agent.py
    "agent": {"enabled": False, "use_llm": True, "min_severity": "crit",
              "batch_seconds": 60, "reason_first": True, "runbook": "",
              "gpu_log_tail_mb": 16, "casekb": True},
    # 사건 메일 (SMTP) — talog/mailer.py. 비밀번호는 환경변수로만
    "email": {"enabled": False,
              # 발송 방식: smtp (STARTTLS/SSL/사내 릴레이/Direct Send) | graph (Microsoft 365
              #   Graph API, OAuth2 — Exchange Online 은 SMTP 기본 인증을 폐지하는 중)
              "transport": "smtp",
              "graph": {"tenant_id": "", "client_id": "", "sender": "",
                        "secret_env": "TALOG_GRAPH_SECRET", "client_secret_dpapi": ""},
              "smtp_host": "", "smtp_port": 587,
              "security": "starttls", "username": "",
              "password_env": "TALOG_SMTP_PASSWORD", "password_dpapi": "",
              "sender": "", "to": [],
              "roles": {}, "min_severity": "crit", "min_interval_min": 5,
              "max_per_hour": 10, "outbox": True, "dry_run": False,
              "attach_json": True, "timeout_s": 20,
              # 등급 정책: immediate 등급은 묶음을 기다리지 않고 immediate_seconds 뒤
              #   룰 판단으로 바로 보낸다(메일 간격 제한 없음, 시간당 상한만).
              #   LLM 2차 의견은 llm_followup 이면 같은 스레드의 후속 메일로 보낸다
              "immediate": ["crit"], "immediate_seconds": 10, "llm_followup": True},
    "cooldown_min": 30,
}


def _deep_merge(base: dict, over: dict):
    """재귀 병합 — 사용자가 룰의 값 하나만 바꿔도 나머지 기본값이 유지된다."""
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def load_config(path: str) -> dict:
    cfg = json.loads(json.dumps(_DEFAULT_CFG))  # deep copy
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                user = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError) as e:
            print(f"! watch.yaml 읽기 실패 — 기본값으로 진행: {e}")
            user = {}
        if not isinstance(user, dict):
            print("! watch.yaml 최상위가 딕셔너리가 아닙니다 — 기본값으로 진행")
            user = {}
        _deep_merge(cfg, user)
    return cfg


# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Alert:
    ts: float
    rule: str
    severity: str
    title: str
    evidence: str
    key: str = ""
    cooldown_min: float = 0.0     # 0 이면 전역 cooldown_min 사용
    family: str = ""              # 사건 분석 계열 지정 (패턴 경보용, 빈 값 = 런북 매핑)


class Notifier:
    def __init__(self, cfg: dict, replay: bool = False):
        self.cfg = cfg
        self.replay = replay
        self.alert_dir = cfg["alert_dir"]
        try:
            os.makedirs(self.alert_dir, exist_ok=True)
        except OSError:
            # 기본 드라이브가 없는 PC 등 — 로컬 사용자 폴더로 폴백
            self.alert_dir = os.path.join(
                os.environ.get("LOCALAPPDATA", "."), "talog")
            os.makedirs(self.alert_dir, exist_ok=True)
            print(f"! 알림 폴더 생성 실패 — 폴백: {self.alert_dir}")
        self._last: dict[tuple, float] = {}      # (rule,key) -> 마지막 발송 ts
        self.sent: list[Alert] = []
        self.listeners: list = []                # 경보 구독자 (사건 분석 에이전트)
        self._occ: dict[str, deque] = {}         # 룰별 발생 시각 (overrides.count 용)

    def _apply_override(self, a: Alert) -> bool:
        """rules.overrides 로 룰을 끄거나 등급·발생 횟수 문턱을 바꾼다. False = 억제."""
        ov = (self.cfg.get("rules", {}).get("overrides") or {}).get(a.rule)
        if not isinstance(ov, dict):
            return True
        if ov.get("enabled") is False:
            return False
        if str(ov.get("severity", "")).lower() in ("info", "warn", "crit"):
            a.severity = str(ov["severity"]).lower()
        need = int(ov.get("count", 1) or 1)
        if need > 1:
            w = float(ov.get("window_min", 10) or 10) * 60
            q = self._occ.setdefault(a.rule, deque())
            q.append(a.ts)
            while q and a.ts - q[0] > w:
                q.popleft()
            if len(q) < need:
                return False                      # 아직 N회 미만 — 세기만 한다
            a.evidence += f" (최근 {w / 60:.0f}분 {len(q)}회)"
        return True

    def emit(self, a: Alert):
        if not self._apply_override(a):
            return
        cd = (a.cooldown_min or self.cfg.get("cooldown_min", 30)) * 60
        k = (a.rule, a.key)
        if k in self._last and a.ts - self._last[k] < cd:
            return
        self._last[k] = a.ts
        self.sent.append(a)
        tstr = dt.datetime.fromtimestamp(a.ts).strftime("%Y/%m/%d-%H:%M:%S")
        line = f"[{tstr}] [{a.severity}] {a.rule}: {a.title} — {a.evidence}"
        print(("(replay) " if self.replay else "") + line)
        if self.cfg["notify"].get("jsonl", True):
            day = dt.datetime.fromtimestamp(a.ts).strftime("%Y%m%d")
            suffix = "_replay" if self.replay else ""
            path = os.path.join(self.alert_dir, f"alerts_{day}{suffix}.jsonl")
            try:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "ts": tstr, "site": self.cfg.get("site", ""),
                        "rule": a.rule, "severity": a.severity,
                        "title": a.title, "evidence": a.evidence,
                    }, ensure_ascii=False) + "\n")
            except OSError as e:
                # 디스크 풀/권한 상실이 알림 발송 자체를 막아선 안 된다
                print(f"  ! 알림 기록 실패(계속): {e}")
        for cb in self.listeners:
            try:
                cb(a)
            except Exception as e:                # 구독자 오류가 경보를 막지 않는다
                print(f"  ! 경보 구독자 오류(계속): {e}")
        if self.replay:
            return                                # 리플레이는 기록만
        if self.cfg["notify"].get("toast", True):
            self._toast(f"[talog] {a.title}", a.evidence[:180])
        hook = self.cfg["notify"].get("webhook", "")
        if hook:
            self._webhook(hook, a, tstr)

    @staticmethod
    def _toast(title: str, msg: str):
        """Windows 토스트 알림 (외부 패키지 없이 PowerShell WinRT 사용)."""
        # PS 단일따옴표 문자열 주입 방지: '→'' 이스케이프 + 개행 제거
        title = title.replace("'", "''").replace("\n", " ")[:80]
        msg = msg.replace("'", "''").replace("\n", " ")[:180]
        ps = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI."
            "Notifications, ContentType=WindowsRuntime] | Out-Null;"
            "$t=[Windows.UI.Notifications.ToastNotificationManager]::"
            "GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]"
            "::ToastText02);"
            "$x=$t.GetElementsByTagName('text');"
            f"$x.Item(0).AppendChild($t.CreateTextNode('{title}'))|Out-Null;"
            f"$x.Item(1).AppendChild($t.CreateTextNode('{msg}'))|Out-Null;"
            "$n=[Windows.UI.Notifications.ToastNotification]::new($t);"
            "[Windows.UI.Notifications.ToastNotificationManager]::"
            "CreateToastNotifier('talog watch').Show($n)")
        try:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden",
                 "-Command", ps],
                creationflags=0x08000000)          # CREATE_NO_WINDOW
        except OSError:
            pass

    def _webhook(self, url: str, a: Alert, tstr: str):
        body = json.dumps({
            "site": self.cfg.get("site", ""), "ts": tstr, "rule": a.rule,
            "severity": a.severity, "title": a.title, "evidence": a.evidence,
            # Teams/Slack 호환 필드
            "text": f"[talog][{a.severity}] {a.title}\n{a.evidence}",
        }, ensure_ascii=False).encode("utf-8")
        try:
            req = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
        except OSError as e:
            print(f"  ! webhook 실패: {e}")


# ---------------------------------------------------------------------------
class RuleEngine:
    """슬라이딩 윈도우 기반 이상 판정 (실시간/리플레이 공용)."""

    def __init__(self, cfg: dict, notifier: Notifier):
        self.cfg = cfg["rules"]
        self.notify = notifier
        self.errors: deque = deque()              # (ts, key)
        self.restarts: deque = deque()            # ts (create 이벤트)
        self.pending: dict[str, float] = {}       # inner_id -> start ts
        self._last_start = None                   # (inner, ts, 대기 스레드) — 거부 귀속용
        self.durations: deque = deque(maxlen=200)  # 완료 소요(초)
        self.mem: deque = deque()                 # (ts, MB)
        # 사건 분석 근거용 상태 (agent.EvidenceBuilder 가 읽는다)
        self.recent: deque = deque()              # 최근 90분 핵심 이벤트
        self.done: deque = deque(maxlen=200)      # (END ts, inner, 소요 s)
        # 결함명 감시 상태 (다존 설비는 inner 단위로 NG 스티키 집계)
        self.dw = self.cfg.get("defect_watch", {})
        self.results: OrderedDict = OrderedDict()  # inner -> {ts, result, defects}
        self.defect_hist: deque = deque()         # (ts, 결함명, inner)
        # 사용자 정의 로그 패턴 (TailReader 가 줄 단위로 넘긴다)
        self.patterns = PatternEngine(self.cfg.get("patterns") or [], notifier, Alert)

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

    def _critical_pattern(self, name: str) -> str:
        for p in self.dw.get("critical") or []:
            if fnmatch.fnmatchcase(name.upper(), str(p).upper()):
                return str(p)
        return ""

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
        for d in new:
            if self._critical_pattern(d):
                n = sum(1 for _t, x, _i in self.defect_hist if x == d)
                self.notify.emit(Alert(
                    e.ts, "defect_critical", "crit", f"치명 결함 검출: {d}",
                    f"inner id {e.inner_id} · 판정 {res} {'/'.join(cur['defects'])} · "
                    f"최근 {win / 60:.0f}분 {n}건 — 결함 이미지를 확인하고 제품을 "
                    f"격리하십시오.", key=f"crit:{d}",
                    cooldown_min=dw.get("critical_cooldown_min", 10)))
        need = int(dw.get("repeat_count", 0) or 0)
        if need:
            for d in set(new):
                n = sum(1 for _t, x, _i in self.defect_hist if x == d)
                if n >= need:
                    self.notify.emit(Alert(
                        e.ts, "defect_repeat", "warn",
                        f"동일 결함 빈발: {d} {win / 60:.0f}분 내 {n}건",
                        f"마지막 inner id {e.inner_id} — 공정 이상 또는 과검출 여부를 "
                        f"이미지로 확인하십시오.", key=f"rep:{d}"))
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
        elif e.kind == "INSP_START":
            self.pending[e.inner_id] = e.ts
            self._last_start = (e.inner_id, e.ts, e.value)
        elif e.kind == "INSP_REJECT":
            # 거부 라인은 같은 InspStarter 의 도착 라인(대기 스레드 0) 바로 뒤에 찍힌다 —
            # 그 검사는 시작되지 않았으므로 진행 중에서 뺀다 (남기면 몇 분 뒤 거짓 '검사 정체').
            # 도착 라인이 없는 신형 사이트는 직전 도착이 대기 스레드 1 이상이라 건드리지 않는다
            ls = self._last_start
            if ls and ls[2] == 0 and 0 <= e.ts - ls[1] <= 2:
                self.pending.pop(ls[0], None)
            if self.cfg["no_insp_thread"].get("enabled", True):
                self.notify.emit(Alert(
                    e.ts, "no_insp_thread", "crit",
                    "검사 시작 거부(NoInspThread) — 미검사 임박",
                    "가용 Seq 스레드 0. 직전 검사들이 스레드를 점유 중 — "
                    "병목/정체를 즉시 확인하십시오.", key="noinsp"))
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
            if "INSPECT_END" in e.name:
                st = self.pending.pop(e.inner_id, None)
                if st is not None:
                    self.durations.append(e.ts - st)
                    self.done.append((e.ts, e.inner_id, e.ts - st))
                self._on_inspect_end(e)
            elif "INSPECT_START_ACK" in e.name and e.status \
                    and e.status != "OK":
                self.pending.pop(e.inner_id, None)
                # 설비 회신(comm)으로도 NoInspThread 를 잡는다 — InspStarter 거부
                # 라인과 같은 key 라 한 번만 발보된다
                if e.status == "NoInspThread" and \
                        self.cfg["no_insp_thread"].get("enabled", True):
                    self.notify.emit(Alert(
                        e.ts, "no_insp_thread", "crit",
                        "검사 시작 거부(NoInspThread) — 미검사 임박",
                        f"설비 회신 NoInspThread (inner id {e.inner_id}) — 가용 Seq "
                        f"스레드 0. 병목/정체를 즉시 확인하십시오.", key="noinsp"))
        elif e.kind == "POOL_CREATE":
            # 기동 1회에 풀 5종(eFunction/eDraw/eLongRunning/eSaver/eFovProc)이 함께
            # 생성된다 — 30초 안의 흔적은 재시작 1회로 센다 (build_process_gens 와 같은 규칙)
            if not self.restarts or e.ts - self.restarts[-1] > 30:
                self.restarts.append(e.ts)
            self.pending.clear()                  # 재시작 시 진행분 소실
        elif e.kind == "USAGE":
            self.mem.append((e.ts, e.value))

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
        # 2) 검사 정체 (완료 예정 시간 초과)
        med = statistics.median(self.durations) if len(self.durations) >= 5 \
            else 0
        limit = max(c["insp_stall"]["min_seconds"],
                    med * c["insp_stall"]["factor_x_median"]) if med else \
            c["insp_stall"]["min_seconds"] * 3
        # 정체 검사는 한 경보로 묶는다 — inner 마다 따로 내면 장애·로그 절단 때 진행 중
        # 검사 수만큼 폭주한다 (Tenneco 0730 재생 402건)
        stalled = sorted(((now - st, inner) for inner, st in self.pending.items()
                          if now - st > limit), reverse=True)
        if stalled:
            age, inner = stalled[0]
            more = f" 외 {len(stalled) - 1}건" if len(stalled) > 1 else ""
            self.notify.emit(Alert(
                now, "insp_stall", "warn",
                f"검사 정체 {age:.0f}초 (정상 중앙값 {med:.0f}초){more}",
                f"inner id {inner} 가 완료 신호 없이 진행 중 — 소실 위험"
                + (f" (정체 {len(stalled)}건)" if more else ""),
                key="stall", cooldown_min=c["insp_stall"].get("cooldown_min", 10)))
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


# ---------------------------------------------------------------------------
class TailReader:
    """파일별 오프셋을 기억하며 새로 쓰인 부분만 파싱한다."""

    def __init__(self, state_path: str, max_initial_mb: float = 0):
        self.state_path = state_path
        self.offsets: dict[str, int] = {}
        self.partial: dict[str, str] = {}
        self.enc: dict[str, str] = {}
        self.meta: dict[str, dict] = {}          # 콘솔 표시용 (크기·마지막 기록 시각)
        self.last_ts: dict[str, float] = {}      # 연속 줄(시각 없음)의 패턴 시각
        # 처음 보는 파일이 이보다 크면 끝부분부터 읽는다 (0 = 처음부터, 기본 모드)
        self.max_initial = int(float(max_initial_mb or 0) * 1048576)
        self.ex = Extractor()
        if os.path.exists(state_path):
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    self.offsets = json.load(f)
            except (OSError, json.JSONDecodeError):
                pass

    def save(self):
        try:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(self.offsets, f)
        except OSError:
            pass

    def poll_file(self, path: str, line_hook=None, keep_kinds=None) -> list[Event]:
        """새로 쓰인 줄을 읽어 이벤트로 돌려준다.

        line_hook(ts, 파일명, 레벨, 줄) 이 있으면 모든 줄을 넘긴다 (사용자 패턴 경보).
        keep_kinds 가 있으면 그 종류의 이벤트만 남긴다 (auto 모드의 대용량 계측 제외).
        """
        fi = classify(path)
        if fi is None:
            return []
        cat = fi.category
        rules = self.ex.rules.get(cat, [])
        if not rules and cat != "batchrun" and line_hook is None:
            return []
        try:
            size = os.path.getsize(path)
        except OSError:
            return []
        skip_first = False
        if path in self.offsets:
            off = self.offsets[path]
        else:
            off = 0
            if self.max_initial and size > self.max_initial:
                off = size - min(size, 262144)     # 과거분은 건너뛰고 최근 256KB 부터
                skip_first = True
        if size < off:                              # 파일 재생성(날짜 교체 등)
            off = 0
        if size == off:
            return []
        events: list[Event] = []
        try:
            if path not in self.enc:
                self.enc[path] = _sniff_encoding(path)
            with open(path, "rb") as f:
                f.seek(off)
                chunk = f.read(size - off)
            self.offsets[path] = size
        except OSError:
            return []
        enc = self.enc.get(path, "utf-8")
        text = self.partial.get(path, "") + chunk.decode(
            "utf-8" if enc == "utf-8-sig" and off else enc, errors="replace")
        lines = text.split("\n")
        self.partial[path] = lines.pop() if not text.endswith("\n") else ""
        if skip_first and lines:
            lines = lines[1:]                        # 중간에서 잘린 첫 줄
        fname = os.path.basename(path)
        meta = self.meta.setdefault(path, {"lines": 0, "last_ts": 0.0})
        meta["size"] = size
        meta["lines"] += len(lines)
        if cat == "batchrun":
            for ln in lines:
                for ts, tt, script in iter_batchrun_line(ln):
                    events.append(Event(ts=ts, ts_text=tt, kind="BATCH",
                                        name=script))
                    meta["last_ts"] = ts
                    if line_hook is not None:
                        line_hook(ts, fname, "", ln.strip())
            return events
        for ln in lines:
            ln = ln.rstrip("\r")
            m = _TS_RE.match(ln)
            if not m:
                # 콜스택 등 연속 줄 — 패턴 검사만 (직전 레코드 시각 사용)
                if line_hook is not None and ln.strip() and path in self.last_ts:
                    line_hook(self.last_ts[path], fname, "", ln)
                continue
            yy, mo, dd, hh, mi, ss, ms = m.groups()
            tm = _TAG_RE.match(ln[m.end():])
            if not tm:
                level, header, obj, msg = "", "", "0", ln[m.end():]
            else:
                level, header, obj, msg = tm.groups()
            try:
                # tail 특성상 찢어진(torn)/손상 라인이 배치 분석보다 흔하다
                ts = dt.datetime(int(yy), int(mo), int(dd), int(hh), int(mi),
                                 int(ss), int(ms) * 1000).timestamp()
            except (ValueError, OverflowError, OSError):
                continue
            self.last_ts[path] = ts
            meta["last_ts"] = ts
            if line_hook is not None:
                line_hook(ts, fname, level, ln)
            rec = LogRecord(ts=ts, ts_text=f"{hh}:{mi}:{ss}.{ms}", level=level,
                            header=header, obj_id=obj, msg=msg, line_no=0)
            ev = self.ex._match(rec, rules) if rules else None
            if ev is None and rec.level == "Error":
                ev = Event(ts=rec.ts, ts_text=rec.ts_text, kind="ERROR",
                           extra=rec.msg[:300])
            if ev is not None:
                if keep_kinds is not None and ev.kind not in keep_kinds:
                    continue
                if cat == "comm" and ev.kind == "COMM_MSG":
                    Extractor._enrich_comm(ev)
                events.append(ev)
        return events


def iter_batchrun_line(line: str):
    m = _BATCH_RE.match(line.strip())
    if m:
        yy, mo, dd, hh, mi, ss, script = m.groups()
        ts = dt.datetime(int(yy), int(mo), int(dd), int(hh), int(mi),
                         int(ss)).timestamp()
        yield ts, f"{hh}:{mi}:{ss}.000", script


def _parse_smi(text: str) -> list[dict]:
    """nvidia-smi CSV 출력 파싱: index, temp(°C), util(%), mem_used(MB), power(W)."""
    out = []
    for line in (text or "").strip().splitlines():
        toks = [t.strip() for t in line.split(",")]
        if len(toks) < 4:
            continue
        try:
            out.append({"gpu": int(toks[0]), "temp": float(toks[1]),
                        "util": float(toks[2]), "mem": float(toks[3]),
                        "power": float(toks[4]) if len(toks) > 4 and
                        toks[4].replace(".", "").isdigit() else 0.0})
        except ValueError:
            continue
    return out


def _query_gpu() -> list[dict]:
    """드라이버 기본 동봉 nvidia-smi 로 GPU 상태를 읽는다 (별도 설치 불필요)."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,temperature.gpu,utilization.gpu,"
             "memory.used,power.draw", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
            creationflags=0x08000000)
        if r.returncode != 0:
            return []
        return _parse_smi(r.stdout)
    except (OSError, subprocess.TimeoutExpired):
        return []


class GpuMonitor:
    """GPU 온도/부하 주기 수집 + 임계 경보 + 이력 JSONL 적재."""

    def __init__(self, cfg: dict, notifier: Notifier):
        self.cfg = cfg["rules"].get("gpu_temp", {})
        self.notify = notifier
        self.alert_dir = cfg["alert_dir"]
        self.available = bool(_query_gpu()) if self.cfg.get("enabled", True) \
            else False
        self._last_rec = 0.0
        self.last: list[dict] = []      # 상태 페이지용 최신 샘플
        self.hist: deque = deque(maxlen=120)   # (ts, 샘플) — 사건 분석 근거용 약 40분

    def poll(self, now: float):
        if not self.available:
            return
        gpus = _query_gpu()
        if not gpus:
            return
        self.last = gpus
        self.hist.append((now, gpus))
        limit = self.cfg.get("celsius", 85)
        for g in gpus:
            if g["temp"] >= limit:
                self.notify.emit(Alert(
                    now, "gpu_temp", "crit",
                    f"GPU{g['gpu']} 온도 {g['temp']:.0f}°C (임계 {limit}°C)",
                    f"사용률 {g['util']:.0f}% · 메모리 {g['mem']:.0f}MB · "
                    f"전력 {g['power']:.0f}W — 냉각/팬 상태를 점검하십시오",
                    key=f"gpu{g['gpu']}"))
        # 이력 적재 (기본 5분 간격 — 온도 추세 분석용)
        rec_iv = self.cfg.get("record_min", 5) * 60
        if now - self._last_rec >= rec_iv:
            self._last_rec = now
            day = dt.datetime.fromtimestamp(now).strftime("%Y%m%d")
            try:
                with open(os.path.join(self.alert_dir, f"gpu_{day}.jsonl"),
                          "a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "ts": dt.datetime.fromtimestamp(now)
                        .strftime("%H:%M:%S"),
                        "gpus": gpus}, ensure_ascii=False) + "\n")
            except OSError:
                pass


def _today_dir(root: str) -> str:
    now = dt.datetime.now()
    return os.path.join(root, f"{now.year:04d}_{now.month:02d}",
                        f"{now.day:02d}")


def _lower_priority():
    try:
        h = ctypes.windll.kernel32.GetCurrentProcess()
        ctypes.windll.kernel32.SetPriorityClass(h, 0x00004000)  # BELOW_NORMAL
    except Exception:
        pass


# ---------------------------------------------------------------------------
def _agent_status_html(agent) -> str:
    """상태 페이지의 '최근 사건 분석' 표 (에이전트가 없으면 빈 문자열)."""
    if agent is None:
        return ""
    import html as _html
    esc = _html.escape
    rows = "".join(
        f"<tr><td>{esc(r['time'] or '')}</td><td>{esc(r['severity'])}</td>"
        f"<td>{esc(r['title'])}</td><td>{esc(r['cause'])}</td><td>{esc(r['mode'])}</td>"
        f"<td>{esc(r['email'])}</td></tr>"
        for r in reversed(agent.status_rows()[-8:])) or \
        "<tr><td colspan='6' style='color:#2e7d32'>분석한 사건 없음</td></tr>"
    return ("<h3 style=\"font-size:15px\">최근 사건 분석 (최대 8건)</h3><table><thead><tr>"
            "<th>시각</th><th>심각도</th><th>사건</th><th>원인(룰)</th><th>판단</th>"
            f"<th>메일</th></tr></thead><tbody>{rows}</tbody></table>")


def _write_status(cfg: dict, notifier: Notifier, gpu: "GpuMonitor",
                  started: float, agent=None):
    """현장 모니터용 상태 페이지(status.html)를 갱신한다 (30초 자동 새로고침)."""
    now = dt.datetime.now()
    site = cfg.get("site") or "(사이트 미지정)"
    sev_color = {"crit": "#c62828", "warn": "#e65100", "info": "#546e7a"}
    rows = "".join(
        f"<tr><td>{dt.datetime.fromtimestamp(a.ts).strftime('%m/%d %H:%M:%S')}</td>"
        f"<td style='color:{sev_color.get(a.severity, '#333')};font-weight:700'>"
        f"{a.severity}</td><td>{a.title}</td><td>{a.evidence[:90]}</td></tr>"
        for a in reversed(notifier.sent[-15:])) or \
        "<tr><td colspan='4' style='color:#2e7d32'>경보 없음 — 정상 감시 중</td></tr>"
    gpus = " · ".join(f"GPU{g['gpu']} {g['temp']:.0f}°C/{g['util']:.0f}%"
                      for g in gpu.last) or "미수집"
    up_h = (time.time() - started) / 3600
    html_doc = f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="30"><title>talog watch — {site}</title>
<style>body{{font-family:'Malgun Gothic',sans-serif;margin:24px;background:#f8fafc}}
h1{{font-size:20px}} table{{border-collapse:collapse;width:100%;font-size:13px;
background:#fff}} th,td{{border:1px solid #ddd;padding:6px 10px;text-align:left}}
th{{background:#f0f4fa}} .meta{{color:#555;font-size:13px;margin:6px 0}}</style>
</head><body>
<h1>talog watch — {site} <span style="color:#2e7d32;font-size:14px">● 가동 중</span></h1>
<div class="meta">가동 {up_h:.1f}시간 · 마지막 갱신 {now.strftime('%H:%M:%S')}
 (30초 자동 새로고침) · GPU: {gpus}</div>
<h3 style="font-size:15px">최근 경보 (최대 15건)</h3>
<table><thead><tr><th>시각</th><th>심각도</th><th>제목</th><th>내용</th></tr></thead>
<tbody>{rows}</tbody></table>
{_agent_status_html(agent)}
<div class="meta">기록: {notifier.alert_dir}\\alerts_*.jsonl · 이 페이지는
talog watch 가 자동 갱신합니다</div></body></html>"""
    try:
        with open(os.path.join(notifier.alert_dir, "status.html"), "w",
                  encoding="utf-8") as f:
            f.write(html_doc)
    except OSError:
        pass


def _llm_review(cfg: dict, engine: RuleEngine, notifier: Notifier):
    """사용자 지시문(스크립트) 기반 LLM 점검. 기본 CPU 모드로 검사 GPU 보호."""
    llm = cfg["llm"]
    script = ""
    if llm.get("script") and os.path.exists(llm["script"]):
        with open(llm["script"], "r", encoding="utf-8") as f:
            script = f.read()
    if not script:
        script = ("최근 상태에서 설비 이상 징후가 있는지 판단하라. 반복 에러, "
                  "검사 정체, 메모리 추세를 중심으로 본다.")
    recent_alerts = "\n".join(
        f"- [{a.severity}] {a.title}: {a.evidence}" for a in notifier.sent[-10:]) \
        or "- (최근 알림 없음)"
    mem_tail = ", ".join(f"{mb:.0f}MB" for _t, mb in list(engine.mem)[-6:])
    med_line = (f"[진행 중 검사] {len(engine.pending)}건, 완료 소요 중앙값 "
                f"{statistics.median(engine.durations):.1f}초\n"
                if engine.durations else "")
    ctx = f"[최근 알림]\n{recent_alerts}\n\n{med_line}[최근 RAM] {mem_tail}\n"
    prompt = (f"당신은 검사 설비 감시자다. 아래 감시 지시문과 현재 상태를 보고 "
              f"JSON 한 개로만 답하라: "
              f'{{"alert": true|false, "severity": "info|warn|crit", '
              f'"summary": "<한국어 한 문장>"}}\n\n'
              f"[감시 지시문]\n{script}\n\n[현재 상태]\n{ctx}")
    try:
        # 주소·장치(cpu/gpu/auto)·스레드 상한은 사건 분석과 같은 llm 설정을 쓴다
        from .llm import OllamaClient
        r = OllamaClient(llm).chat([{"role": "user", "content": prompt}])
        if r["error"]:
            raise RuntimeError(r["error"])
        text = r["content"]
        import re as _re
        m = _re.search(r"\{.*\}", text, _re.S)
        if m:
            j = json.loads(m.group(0))
            if j.get("alert"):
                notifier.emit(Alert(time.time(), "llm_review",
                                    j.get("severity", "info"),
                                    "LLM 점검 소견",
                                    str(j.get("summary", ""))[:300],
                                    key="llm"))
            else:
                print(f"  [LLM 점검] 이상 없음: {j.get('summary', '')[:120]}")
    except Exception as e:
        # LLM 점검은 부가 기능 — 어떤 실패(JSON 이탈 포함)도 감시를 죽이지 않는다
        print(f"  ! LLM 점검 실패(무시): {e}")


# ---------------------------------------------------------------------------
def _make_agent(cfg: dict, engine: RuleEngine, notifier: Notifier, gpu=None,
                replay: bool = False, day_dir: str = ""):
    """agent.enabled 또는 email.enabled 이면 사건 분석/메일 에이전트를 붙인다."""
    if not (cfg.get("agent", {}).get("enabled") or cfg.get("email", {}).get("enabled")):
        return None
    from .agent import IncidentAgent
    from .mailer import Mailer
    mailer = Mailer(cfg, notifier.alert_dir, replay=replay)
    if replay:
        def day_fn():
            return day_dir
    else:
        def day_fn():
            return _today_dir(cfg["watch_root"])
    agent = IncidentAgent(cfg, engine, notifier.alert_dir, day_fn, gpu=gpu,
                          mailer=mailer, replay=replay)
    notifier.listeners.append(agent.submit)
    a, llm, e = cfg["agent"], cfg["llm"], cfg["email"]
    parts = []
    if a.get("enabled"):
        parts.append("사건 분석 " + (f"룰+LLM({llm.get('model')}, {llm.get('device')}, "
                                     f"{llm.get('url')})" if a.get("use_llm", True)
                                     else "룰만"))
    if e.get("enabled"):
        parts.append("메일 " + ("outbox 만(리플레이/dry_run)" if mailer.dry_run else
                               f"{e.get('smtp_host')}:{e.get('smtp_port')} → "
                               f"{len(mailer.recipients(list((e.get('roles') or {}).keys())))}명"))
    print(f"[talog watch] 에이전트: {' / '.join(parts)} — 기준 심각도 "
          f"{a.get('min_severity', 'crit')}, 묶음 {a.get('batch_seconds', 60)}초")
    return agent


# select/auto 모드에서 룰 엔진으로 넘길 이벤트 종류 (대용량 계측 이벤트 제외)
_ENGINE_KINDS = frozenset((
    "ERROR", "EXC_REDIRECT", "MODEL_FAIL", "CRASH", "COMM_FAIL", "RECIPE_FAIL",
    "INSP_START", "INSP_REJECT", "REJECT_BUSYCAM", "REJECT_NOTREADY", "REJECT_SIM",
    "GRAB_FAIL", "IMG_TIMEOUT", "ALG_TIMEOUT", "STORAGE_LOW", "LIGHT_UNSTABLE",
    "COMM_MSG", "POOL_CREATE", "POOL_DESTROY", "USAGE", "BATCH", "EXC_SAFE"))


class LiveWatch:
    """실시간 감시 1개 인스턴스 — CLI 상주 루프와 콘솔(UI) 작업 스레드가 같이 쓴다."""

    def __init__(self, cfg: dict, stop_event: threading.Event | None = None):
        self.cfg = cfg
        self.stop = stop_event or threading.Event()
        if cfg.get("low_priority", True):
            _lower_priority()
        self.notifier = Notifier(cfg)         # alert_dir 생성/폴백은 Notifier 가 담당
        self.engine = RuleEngine(cfg, self.notifier)
        tc = cfg.get("tracking") or {}
        self.mode = str(tc.get("mode", "default") or "default").lower()
        self.tail = TailReader(
            os.path.join(self.notifier.alert_dir, "watch_state.json"),
            max_initial_mb=tc.get("max_initial_mb", 32) if self.mode != "default" else 0)
        self.keep = None if self.mode == "default" else _ENGINE_KINDS
        self.gpu = GpuMonitor(cfg, self.notifier)
        self.gpu.alert_dir = self.notifier.alert_dir   # 폴백 경로 일원화
        self.llm_every = cfg["llm"].get("interval_min", 30) * 60
        self.last_llm = 0.0
        self.started = time.time()
        self.last_status = 0.0
        self.fail_streak = 0
        self.day_dir = ""
        self.targets: list[str] = []
        print(f"[talog watch] 감시 시작: {cfg['watch_root']} "
              f"(주기 {cfg['poll_seconds']}s, 추적 {self.mode}, 알림 → "
              f"{self.notifier.alert_dir}, GPU 온도 감시 "
              f"{'ON' if self.gpu.available else 'OFF(nvidia-smi 없음)'})")
        if self.engine.patterns.active:
            print(f"[talog watch] 사용자 패턴 {len(self.engine.patterns.rules)}개: "
                  + ", ".join(r.name for r in self.engine.patterns.rules))
        self.agent = _make_agent(cfg, self.engine, self.notifier, gpu=self.gpu)

    def step(self):
        cfg, engine = self.cfg, self.engine
        self.day_dir = _today_dir(cfg["watch_root"])
        if os.path.isdir(self.day_dir):
            self.targets = resolve_targets(self.day_dir, cfg.get("tracking"))
            hook = engine.patterns.feed if engine.patterns.active else None
            for p in self.targets:
                for e in self.tail.poll_file(p, line_hook=hook, keep_kinds=self.keep):
                    engine.feed(e)
            engine.evaluate(time.time())
            self.tail.save()
        else:
            self.targets = []
        self.gpu.poll(time.time())
        if self.agent is not None:
            self.agent.tick(time.time())      # 묶음 마감 → 작업 스레드가 분석·발송
        if time.time() - self.last_status >= 30:
            self.last_status = time.time()
            _write_status(cfg, self.notifier, self.gpu, self.started, self.agent)
        if cfg["llm"].get("enabled") and time.time() - self.last_llm > self.llm_every:
            self.last_llm = time.time()
            _llm_review(cfg, engine, self.notifier)

    def run(self, once: bool = False) -> int:
        while not self.stop.is_set():
            try:
                self.step()
                self.fail_streak = 0
            except Exception as e:
                # 상주 감시는 단발 예외로 죽어선 안 된다 — 다음 주기에 재시도
                self.fail_streak += 1
                print(f"! 감시 주기 오류(계속, {self.fail_streak}회): {e}")
                if self.fail_streak >= 30:
                    print("! 오류가 30주기 연속 — 환경 문제로 판단하고 종료합니다. "
                          "talog watch --check 로 점검하십시오.")
                    return 1
            if once:
                if self.agent is not None:
                    # 1회 스캔 모드: 묶음을 바로 마감하고 분석·발송이 끝날 때까지 기다린다
                    self.agent.flush(
                        time.time(),
                        wait=float(self.cfg["llm"].get("timeout_s", 600)) * 3 + 60)
                    _write_status(self.cfg, self.notifier, self.gpu, self.started,
                                  self.agent)
                break
            self.stop.wait(self.cfg["poll_seconds"])
        if self.agent is not None and self.stop.is_set():
            self.agent.close()                    # 콘솔 재시작 시 작업 스레드 정리
        return 0

    def snapshot(self) -> dict:
        """콘솔 표시용 현재 상태 (추적 파일·최근 경보·GPU·사건)."""
        files = []
        for p in list(self.targets):
            m = self.tail.meta.get(p, {})
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            fi = classify(p)
            files.append({"name": os.path.relpath(p, self.day_dir) if self.day_dir else p,
                          "size": size, "offset": self.tail.offsets.get(p, 0),
                          "last": (dt.datetime.fromtimestamp(m["last_ts"]).strftime("%H:%M:%S")
                                   if m.get("last_ts") else None),
                          "lines": m.get("lines", 0),
                          "category": fi.category if fi else ""})
        alerts = [{"time": dt.datetime.fromtimestamp(a.ts).strftime("%m/%d %H:%M:%S"),
                   "severity": a.severity, "rule": a.rule, "title": a.title,
                   "evidence": a.evidence} for a in self.notifier.sent[-60:]]
        return {"running": not self.stop.is_set(), "started": self.started,
                "day_dir": self.day_dir, "mode": self.mode, "files": files,
                "alerts": alerts[::-1], "gpu": list(self.gpu.last),
                "incidents": self.agent.status_rows()[::-1] if self.agent else [],
                "pattern_errors": list(self.engine.patterns.errors),
                "alert_dir": self.notifier.alert_dir}


def run_live(cfg: dict, once: bool = False) -> int:
    return LiveWatch(cfg).run(once=once)


def _replay_file(fi, ex: Extractor, pat, keep) -> list[Event]:
    """리플레이용 1회 읽기: 룰 이벤트 + 사용자 패턴에 걸린 줄(kind=LINE)."""
    from .lineparser import iter_batchrun, iter_records
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
        if fi.category == "comm" and ev.kind == "COMM_MSG":
            Extractor._enrich_comm(ev)
        out.append(ev)
    return out


def run_replay(cfg: dict, day_dir: str) -> int:
    """과거 일자 폴더를 시간순으로 재생하여 룰 경보를 검증한다."""
    if not os.path.isdir(day_dir):
        print(f"리플레이 폴더가 없습니다: {day_dir}")
        return 1
    print(f"[talog watch] 리플레이: {day_dir}")
    notifier = Notifier(cfg, replay=True)
    engine = RuleEngine(cfg, notifier)
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
    if agent is not None:
        agent.flush(agent.due() or events[-1].ts)
    print(f"[talog watch] 리플레이 완료 — 이벤트 {len(events):,}개, "
          f"경보 {len(notifier.sent)}건"
          + (f", 사건 분석 {agent.processed}건 → {notifier.alert_dir}\\"
             f"incidents_*_replay.jsonl" if agent is not None else ""))
    return 0


def run_check(cfg: dict) -> int:
    """현장 설치 자가 점검: 경로/파일/알림 채널을 확인하고 테스트 토스트를 쏜다."""
    print("=" * 56)
    print(" talog watch 설치 자가 점검")
    print("=" * 56)
    ok = True

    root = cfg["watch_root"]
    print(f"[1] 감시 루트: {root}", "→ 존재" if os.path.isdir(root) else "→ 없음!")
    ok &= os.path.isdir(root)

    day = _today_dir(root)
    if os.path.isdir(day):
        print(f"[2] 오늘 날짜 폴더: {day} → 존재")
        tc = cfg.get("tracking") or {}
        mode = str(tc.get("mode", "default")).lower()
        found = []
        for p in resolve_targets(day, tc):
            sz = os.path.getsize(p)
            found.append(f"{os.path.relpath(p, day)} ({sz / 1024:.0f}KB)")
        want = (f"/{len(_WATCH_FILES)}종" if mode == "default" else "개")
        print(f"[3] 추적 모드 {mode} — 대상 파일 {len(found)}{want} 발견:")
        for f in found[:30]:
            print(f"     - {f}")
        if len(found) > 30:
            print(f"     … 외 {len(found) - 30}개")
        if not found:
            print("     ! 감시 대상 파일이 없습니다 — talos 가동 여부나 tracking.files 를 "
                  "확인하십시오")
    else:
        print(f"[2] 오늘 날짜 폴더 없음: {day}")
        print("     ! talos 가 오늘 아직 기동되지 않았거나 경로 설정이 다릅니다")

    try:
        os.makedirs(cfg["alert_dir"], exist_ok=True)
        probe = os.path.join(cfg["alert_dir"], "_write_test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        print(f"[4] 알림 폴더 쓰기: {cfg['alert_dir']} → OK")
    except OSError as e:
        print(f"[4] 알림 폴더 쓰기 실패: {e}")
        ok = False

    r = cfg["rules"]
    print(f"[5] 감지 룰: 에러반복 {r['error_repeat']['count']}회/"
          f"{r['error_repeat']['window_min']}분 · NoInspThread 즉시 · "
          f"정체 {r['insp_stall']['factor_x_median']}×중앙값 · "
          f"재시작 {r['restart_burst']['count']}회/{r['restart_burst']['window_min']}분 · "
          f"메모리 +{r['memory_trend']['mb_per_hour']}MB/h")
    print(f"[6] 사이트: '{cfg.get('site') or '(미설정 — watch.yaml 에서 지정 권장)'}'"
          f" / 웹훅: {'설정됨' if cfg['notify'].get('webhook') else '없음(토스트/JSONL만)'}")

    llm_cfg, a_cfg, e_cfg = cfg["llm"], cfg["agent"], cfg["email"]
    need_llm = llm_cfg.get("enabled") or (a_cfg.get("enabled") and a_cfg.get("use_llm", True))
    if need_llm:
        from .llm import OllamaClient, resolve_device
        cli = OllamaClient(llm_cfg)
        alive = cli.alive()
        has = cli.model in cli.models() if alive else False
        dev, why = resolve_device(llm_cfg)
        print(f"[7] LLM: {cli.url} {'가동 중' if alive else '미가동!'} · 모델 {cli.model} "
              f"{'설치됨' if has else '없음!(ollama pull 필요)'} · 장치 설정 "
              f"{llm_cfg.get('device')} → 지금은 {dev} ({why})"
              + (f" · 주기 점검 {llm_cfg.get('interval_min')}분" if llm_cfg.get("enabled")
                 else ""))
        ok &= alive and has
    else:
        print("[7] LLM: 비활성 (기본)")
    dw = cfg["rules"].get("defect_watch", {})
    print(f"[7b] 결함명 감시: 치명 {len(dw.get('critical') or [])}종"
          f"{' ' + str(dw.get('critical')) if dw.get('critical') else ''} · 빈발 "
          f"{dw.get('repeat_count') or '끔'}"
          f"{'건/' + str(dw.get('repeat_window_min')) + '분' if dw.get('repeat_count') else ''}"
          f" · 연속 NG {dw.get('ng_streak') or '끔'} · NG 비율 "
          f"{str(dw.get('ng_rate_percent')) + '%/' + str(dw.get('ng_rate_window')) + '검사' if dw.get('ng_rate_window') else '끔'}")
    print(f"[7c] 사건 분석 에이전트: {'활성' if a_cfg.get('enabled') else '비활성'}"
          + (f" (기준 {a_cfg.get('min_severity')}, 묶음 {a_cfg.get('batch_seconds')}초, "
             f"{'룰+LLM' if a_cfg.get('use_llm', True) else '룰만'})"
             if a_cfg.get("enabled") else ""))
    if e_cfg.get("enabled"):
        from .mailer import Mailer
        m = Mailer(cfg, cfg["alert_dir"])
        to_all = m.recipients(list((e_cfg.get("roles") or {}).keys()))
        if m.dry_run:
            print(f"[7d] 이메일: dry_run — SMTP 없이 {m.outbox} 에 .eml 만 저장 "
                  f"(수신자 {len(to_all)}명 설정)")
        else:
            env = e_cfg.get("password_env") or "TALOG_SMTP_PASSWORD"
            pw = ("설정됨" if os.environ.get(env) else "없음!") if e_cfg.get("username") \
                else "인증 없음(사내 릴레이)"
            good, detail = m.check()
            print(f"[7d] 이메일: {e_cfg.get('smtp_host')}:{e_cfg.get('smtp_port')} "
                  f"{e_cfg.get('security')} · 비밀번호 환경변수 {env} {pw} · 수신자 "
                  f"{len(to_all)}명 · 접속 {'OK' if good else '실패'} ({detail})")
            ok &= good and bool(to_all)
            if not to_all:
                print("     ! email.to / email.roles 에 받는 사람을 적으십시오")
        print("     실제 발송 확인: talog watch --test-email --config <watch.yaml>")
    else:
        print("[7d] 이메일: 비활성 (기본)")

    gpus = _query_gpu()
    if gpus:
        stat = " · ".join(f"GPU{g['gpu']} {g['temp']:.0f}°C/{g['util']:.0f}%"
                          for g in gpus)
        print(f"[8] GPU 감시: nvidia-smi OK — {stat} "
              f"(임계 {cfg['rules'].get('gpu_temp', {}).get('celsius', 85)}°C)")
    else:
        print("[8] GPU 감시: nvidia-smi 미검출 — GPU 온도 룰 비활성")

    if cfg["notify"].get("toast", True):
        Notifier._toast("[talog] 설치 점검", "테스트 알림입니다 - 이 팝업이 보이면 정상")
        print("[9] 테스트 토스트 발사 — 화면 우하단 팝업을 확인하십시오")
    print("=" * 56)
    print(" 점검 " + ("통과 — run_watch.bat 로 상주 감시를 시작하십시오"
                     if ok else "실패 항목 있음 — watch.yaml 경로를 확인하십시오"))
    return 0 if ok else 1


def run_test_email(cfg: dict) -> int:
    """예시 사건 메일 1통을 설정된 경로로 보낸다 (dry_run 이면 outbox 에만)."""
    from .mailer import Mailer, sample_incident
    if not cfg["email"].get("enabled"):
        print("email.enabled 가 false 입니다 — watch.yaml 의 email 항목을 먼저 설정하십시오.")
        return 2
    notifier = Notifier(cfg, replay=True)            # alert_dir 폴백만 빌려 쓴다
    m = Mailer(cfg, notifier.alert_dir)
    inc = sample_incident(cfg.get("site") or "설비")
    inc["verdict"]["notify"] = list((cfg["email"].get("roles") or {}).keys())
    res = m.send_incident(inc)
    print(f"[talog watch] 테스트 메일: 수신 {res['to'] or '(없음)'} · 제목 {res['subject']}")
    if res["eml"]:
        print(f"  보관: {res['eml']}")
    if res["dry_run"]:
        print("  dry_run — SMTP 로 보내지 않았습니다 (email.dry_run: false 로 실제 발송)")
        return 0
    print("  → 발송 " + ("성공" if res["sent"] else f"실패: {res['error']}"))
    return 0 if res["sent"] else 1


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="talog watch",
                                 description="예지보전 상주 감시")
    ap.add_argument("--config", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "watch.yaml"))
    ap.add_argument("--once", action="store_true", help="1회 스캔 후 종료")
    ap.add_argument("--replay", default="", help="과거 일자 폴더 재생 검증")
    ap.add_argument("--check", action="store_true",
                    help="설치 자가 점검 (경로·파일·알림 테스트)")
    ap.add_argument("--test-email", action="store_true",
                    help="예시 사건 메일 1통 발송 (email 설정 확인)")
    ap.add_argument("--ui", action="store_true",
                    help="로컬 웹 콘솔로 감시 (설정·상태·테스트, http://127.0.0.1:8778)")
    ap.add_argument("--port", type=int, default=8778, help="콘솔 포트 (--ui)")
    ap.add_argument("--no-open", action="store_true", help="콘솔 브라우저 자동 열기 끔")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    if args.ui:
        from .console import serve
        try:
            return serve(args.config, port=args.port, open_browser=not args.no_open)
        except OSError as e:
            print(f"[talog 콘솔] 포트 {args.port} 사용 불가 — --port 로 바꾸십시오 ({e})")
            return 1
    cfg = load_config(args.config)
    if args.check:
        return run_check(cfg)
    if args.test_email:
        return run_test_email(cfg)
    if args.replay:
        return run_replay(cfg, args.replay)
    return run_live(cfg, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
