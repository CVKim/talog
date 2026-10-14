"""경보 — Alert 자료형과 Notifier(중복 억제·alerts JSONL 기록·토스트/웹훅·에이전트 전달)."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import urllib.request
from collections import deque
from dataclasses import dataclass


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
    meta: dict | None = None      # 사건 분석에 넘길 부가 정보 (예: {"group": 10})


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
