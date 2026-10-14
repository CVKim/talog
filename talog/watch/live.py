"""실시간 감시 — 오늘 로그 폴더를 폴링 tail 로 읽어 룰 엔진·GPU 감시·상태 페이지를 돌린다 (저부하)."""

from __future__ import annotations

import ctypes
import datetime as dt
import os
import threading
import time

from ..events import Event
from ..fileclass import classify
from ..tracker import resolve_targets
from .alerts import Notifier
from .config import _today_dir
from .gpu import GpuMonitor
from .rules import RuleEngine
from .status import _write_status
from .tail import TailReader
from .wiring import _ENGINE_KINDS, _attach_capacity_state, _make_agent


def _lower_priority():
    try:
        h = ctypes.windll.kernel32.GetCurrentProcess()
        ctypes.windll.kernel32.SetPriorityClass(h, 0x00004000)  # BELOW_NORMAL
    except Exception:
        pass


class LiveWatch:
    """실시간 감시 1개 인스턴스 — CLI 상주 루프와 콘솔(UI) 작업 스레드가 같이 쓴다."""

    def __init__(self, cfg: dict, stop_event: threading.Event | None = None):
        self.cfg = cfg
        self.stop = stop_event or threading.Event()
        if cfg.get("low_priority", True):
            _lower_priority()
        self.notifier = Notifier(cfg)         # alert_dir 생성/폴백은 Notifier 가 담당
        self.engine = RuleEngine(cfg, self.notifier)
        self.cap_state = _attach_capacity_state(self.engine, self.notifier.alert_dir)
        tc = cfg.get("tracking") or {}
        self.mode = str(tc.get("mode", "default") or "default").lower()
        self.tail = TailReader(
            os.path.join(self.notifier.alert_dir, "watch_state.json"),
            max_initial_mb=tc.get("max_initial_mb", 32) if self.mode != "default" else 0)
        self.keep = None if self.mode == "default" else _ENGINE_KINDS
        self.gpu = GpuMonitor(cfg, self.notifier)
        self.gpu.alert_dir = self.notifier.alert_dir   # 폴백 경로 일원화
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
            evs: list[Event] = []
            for p in self.targets:
                evs.extend(self.tail.poll_file(p, line_hook=hook, keep_kinds=self.keep))
            # 파일 순서대로 넣으면 한 주기(기본 20초) 안에서 도착(InspStarter)을 다 넣은 뒤에야 회신(comm)
            # 이 들어가, 그사이 끝난 검사까지 진행 중으로 세어 풀이 부풀고 짝짓기가 어긋난다 —
            # 리플레이(검증한 경로)처럼 시각순으로 넣는다 (같은 시각은 파일·줄 순서 유지)
            evs.sort(key=lambda e: e.ts)
            for e in evs:
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
                "capacity": list(self.engine.cap.summary),
                "alert_dir": self.notifier.alert_dir}


def run_live(cfg: dict, once: bool = False) -> int:
    return LiveWatch(cfg).run(once=once)
