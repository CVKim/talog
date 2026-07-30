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
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field

import yaml

from .events import Event, Extractor
from .fileclass import classify
from .lineparser import _BATCH_RE, _TAG_RE, _TS_RE, LogRecord

# 감시 대상 파일 (소형·핵심만 — 저부하 원칙). seq_N.log 는 동적으로 추가된다.
_WATCH_FILES = ("inspstarter.log", "comm.log", "workerthreadpoolmng.log",
                "exception.log", "processusage.log", "batchrunlog.txt",
                "seq_1.log", "seq_2.log", "seq_3.log")

_DEFAULT_CFG = {
    "watch_root": r"D:\AIV_LOG\Talos",
    "poll_seconds": 20,
    "alert_dir": r"D:\AIV_LOG\TalogWatch",
    "low_priority": True,
    "site": "",
    "rules": {
        "error_repeat": {"window_min": 10, "count": 5},
        "no_insp_thread": {"enabled": True},
        "insp_stall": {"factor_x_median": 2.0, "min_seconds": 180},
        "restart_burst": {"window_min": 60, "count": 3},
        "memory_trend": {"mb_per_hour": 100, "min_rise_mb": 500,
                         "min_hours": 2.0},
        "gpu_temp": {"enabled": True, "celsius": 85, "record_min": 5},
    },
    "notify": {"toast": True, "webhook": "", "jsonl": True},
    "llm": {"enabled": False, "device": "cpu", "model": "qwen2.5:7b",
            "script": "", "interval_min": 30},
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

    def emit(self, a: Alert):
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
        self.durations: deque = deque(maxlen=200)  # 완료 소요(초)
        self.mem: deque = deque()                 # (ts, MB)

    def feed(self, e: Event):
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
        elif e.kind == "INSP_REJECT":
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
            elif "INSPECT_START_ACK" in e.name and e.status \
                    and e.status != "OK":
                self.pending.pop(e.inner_id, None)
        elif e.kind == "POOL_CREATE":
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
        for inner, st in list(self.pending.items()):
            age = now - st
            if age > limit:
                self.notify.emit(Alert(
                    now, "insp_stall", "warn",
                    f"검사 정체 {age:.0f}초 (정상 중앙값 {med:.0f}초)",
                    f"inner id {inner} 가 완료 신호 없이 진행 중 — 소실 위험",
                    key=inner))
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

    def __init__(self, state_path: str):
        self.state_path = state_path
        self.offsets: dict[str, int] = {}
        self.partial: dict[str, str] = {}
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

    def poll_file(self, path: str) -> list[Event]:
        fi = classify(path)
        if fi is None:
            return []
        cat = fi.category
        rules = self.ex.rules.get(cat, [])
        if not rules and cat != "batchrun":
            return []
        try:
            size = os.path.getsize(path)
        except OSError:
            return []
        off = self.offsets.get(path, 0)
        if size < off:                              # 파일 재생성(날짜 교체 등)
            off = 0
        if size == off:
            return []
        events: list[Event] = []
        try:
            with open(path, "rb") as f:
                f.seek(off)
                chunk = f.read(size - off)
            self.offsets[path] = size
        except OSError:
            return []
        text = self.partial.get(path, "") + chunk.decode("utf-8",
                                                         errors="replace")
        lines = text.split("\n")
        self.partial[path] = lines.pop() if not text.endswith("\n") else ""
        if cat == "batchrun":
            for ln in lines:
                for ts, tt, script in iter_batchrun_line(ln):
                    events.append(Event(ts=ts, ts_text=tt, kind="BATCH",
                                        name=script))
            return events
        for ln in lines:
            ln = ln.rstrip("\r")
            m = _TS_RE.match(ln)
            if not m:
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
            rec = LogRecord(ts=ts, ts_text=f"{hh}:{mi}:{ss}.{ms}", level=level,
                            header=header, obj_id=obj, msg=msg, line_no=0)
            ev = self.ex._match(rec, rules)
            if ev is None and rec.level == "Error":
                ev = Event(ts=rec.ts, ts_text=rec.ts_text, kind="ERROR",
                           extra=rec.msg[:300])
            if ev is not None:
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

    def poll(self, now: float):
        if not self.available:
            return
        gpus = _query_gpu()
        if not gpus:
            return
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
    options = {"temperature": 0.1}
    if llm.get("device", "cpu") == "cpu":
        options["num_gpu"] = 0                    # 검사 GPU 를 쓰지 않음
    try:
        from .ask import _http_json, _OLLAMA
        r = _http_json(f"{_OLLAMA}/api/chat",
                       {"model": llm.get("model", "qwen2.5:7b"),
                        "stream": False, "options": options,
                        "messages": [{"role": "user", "content": prompt}]},
                       {}, timeout=300)
        text = r.get("message", {}).get("content", "")
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
def run_live(cfg: dict, once: bool = False) -> int:
    if cfg.get("low_priority", True):
        _lower_priority()
    notifier = Notifier(cfg)                  # alert_dir 생성/폴백은 Notifier 가 담당
    engine = RuleEngine(cfg, notifier)
    tail = TailReader(os.path.join(notifier.alert_dir, "watch_state.json"))
    gpu = GpuMonitor(cfg, notifier)
    gpu.alert_dir = notifier.alert_dir        # 폴백 경로 일원화
    llm_every = cfg["llm"].get("interval_min", 30) * 60
    last_llm = 0.0
    print(f"[talog watch] 감시 시작: {cfg['watch_root']} "
          f"(주기 {cfg['poll_seconds']}s, 알림 → {notifier.alert_dir}, "
          f"GPU 온도 감시 {'ON' if gpu.available else 'OFF(nvidia-smi 없음)'})")
    fail_streak = 0
    while True:
        try:
            day_dir = _today_dir(cfg["watch_root"])
            if os.path.isdir(day_dir):
                for name in _WATCH_FILES:
                    p = os.path.join(day_dir, name)
                    # 대소문자 변형(Comm.log 등) 대응
                    if not os.path.exists(p):
                        for cand in os.listdir(day_dir):
                            if cand.lower() == name:
                                p = os.path.join(day_dir, cand)
                                break
                    if os.path.exists(p):
                        for e in tail.poll_file(p):
                            engine.feed(e)
                engine.evaluate(time.time())
                tail.save()
            gpu.poll(time.time())
            if cfg["llm"].get("enabled") and time.time() - last_llm > llm_every:
                last_llm = time.time()
                _llm_review(cfg, engine, notifier)
            fail_streak = 0
        except Exception as e:
            # 상주 감시는 단발 예외로 죽어선 안 된다 — 다음 주기에 재시도
            fail_streak += 1
            print(f"! 감시 주기 오류(계속, {fail_streak}회): {e}")
            if fail_streak >= 30:
                print("! 오류가 30주기 연속 — 환경 문제로 판단하고 종료합니다. "
                      "talog watch --check 로 점검하십시오.")
                return 1
        if once:
            break
        time.sleep(cfg["poll_seconds"])
    return 0


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
    for name in os.listdir(day_dir):
        if name.lower() not in _WATCH_FILES:
            continue
        fi = classify(os.path.join(day_dir, name))
        if fi is None:
            continue
        try:
            evs, _n = ex.extract_file(fi, 0)
            events.extend(evs)
        except OSError:
            continue
    events.sort(key=lambda e: e.ts)
    if not events:
        print("이벤트 없음")
        return 1
    next_eval = events[0].ts
    for e in events:
        engine.feed(e)
        if e.ts >= next_eval:                     # 20초 간격 판정 시뮬레이션
            engine.evaluate(e.ts)
            next_eval = e.ts + 20
    engine.evaluate(events[-1].ts)
    print(f"[talog watch] 리플레이 완료 — 이벤트 {len(events):,}개, "
          f"경보 {len(notifier.sent)}건")
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
        found = []
        for name in _WATCH_FILES:
            for cand in os.listdir(day):
                if cand.lower() == name:
                    sz = os.path.getsize(os.path.join(day, cand))
                    found.append(f"{cand} ({sz / 1024:.0f}KB)")
        print(f"[3] 감시 대상 파일 {len(found)}/{len(_WATCH_FILES)}종 발견:")
        for f in found:
            print(f"     - {f}")
        if not found:
            print("     ! 감시 대상 파일이 없습니다 — talos 가동 여부를 확인하십시오")
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

    if cfg["llm"].get("enabled"):
        alive = False
        try:
            with urllib.request.urlopen(f"{_OLLAMA_TAGS}", timeout=3):
                alive = True
        except OSError:
            pass
        print(f"[7] LLM 점검 모드: 활성 / Ollama {'가동 중' if alive else '미가동!'}")
    else:
        print("[7] LLM 점검 모드: 비활성 (기본)")

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


_OLLAMA_TAGS = "http://localhost:11434/api/tags"


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
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    cfg = load_config(args.config)
    if args.check:
        return run_check(cfg)
    if args.replay:
        return run_replay(cfg, args.replay)
    return run_live(cfg, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
