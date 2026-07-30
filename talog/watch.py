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
from .lineparser import _TAG_RE, _TS_RE, iter_batchrun

# 감시 대상 파일 (소형·핵심만 — 저부하 원칙)
_WATCH_FILES = ("inspstarter.log", "comm.log", "workerthreadpoolmng.log",
                "exception.log", "processusage.log", "batchrunlog.txt")

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
    },
    "notify": {"toast": True, "webhook": "", "jsonl": True},
    "llm": {"enabled": False, "device": "cpu", "model": "qwen2.5:7b",
            "script": "", "interval_min": 30},
    "cooldown_min": 30,
}


def load_config(path: str) -> dict:
    cfg = json.loads(json.dumps(_DEFAULT_CFG))  # deep copy
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
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
        os.makedirs(self.alert_dir, exist_ok=True)
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
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": tstr, "site": self.cfg.get("site", ""),
                    "rule": a.rule, "severity": a.severity,
                    "title": a.title, "evidence": a.evidence,
                }, ensure_ascii=False) + "\n")
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
                 "-Command", ps.replace("'", "'")],
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
            from .lineparser import LogRecord
            rec = LogRecord(
                ts=dt.datetime(int(yy), int(mo), int(dd), int(hh), int(mi),
                               int(ss), int(ms) * 1000).timestamp(),
                ts_text=f"{hh}:{mi}:{ss}.{ms}", level=level, header=header,
                obj_id=obj, msg=msg, line_no=0)
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
    from .lineparser import _BATCH_RE
    m = _BATCH_RE.match(line.strip())
    if m:
        yy, mo, dd, hh, mi, ss, script = m.groups()
        ts = dt.datetime(int(yy), int(mo), int(dd), int(hh), int(mi),
                         int(ss)).timestamp()
        yield ts, f"{hh}:{mi}:{ss}.000", script


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
    ctx = (f"[최근 알림]\n{recent_alerts}\n\n"
           f"[진행 중 검사] {len(engine.pending)}건, "
           f"완료 소요 중앙값 {statistics.median(engine.durations):.1f}초\n"
           if engine.durations else "") + f"[최근 RAM] {mem_tail}\n"
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
    except OSError as e:
        print(f"  ! LLM 점검 실패(무시): {e}")


# ---------------------------------------------------------------------------
def run_live(cfg: dict, once: bool = False) -> int:
    if cfg.get("low_priority", True):
        _lower_priority()
    os.makedirs(cfg["alert_dir"], exist_ok=True)
    notifier = Notifier(cfg)
    engine = RuleEngine(cfg, notifier)
    tail = TailReader(os.path.join(cfg["alert_dir"], "watch_state.json"))
    llm_every = cfg["llm"].get("interval_min", 30) * 60
    last_llm = 0.0
    print(f"[talog watch] 감시 시작: {cfg['watch_root']} "
          f"(주기 {cfg['poll_seconds']}s, 알림 → {cfg['alert_dir']})")
    while True:
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
        if cfg["llm"].get("enabled") and time.time() - last_llm > llm_every:
            last_llm = time.time()
            _llm_review(cfg, engine, notifier)
        if once:
            break
        time.sleep(cfg["poll_seconds"])
    return 0


def run_replay(cfg: dict, day_dir: str) -> int:
    """과거 일자 폴더를 시간순으로 재생하여 룰 경보를 검증한다."""
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


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="talog watch",
                                 description="예지보전 상주 감시")
    ap.add_argument("--config", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "watch.yaml"))
    ap.add_argument("--once", action="store_true", help="1회 스캔 후 종료")
    ap.add_argument("--replay", default="", help="과거 일자 폴더 재생 검증")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    cfg = load_config(args.config)
    if args.replay:
        return run_replay(cfg, args.replay)
    return run_live(cfg, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
