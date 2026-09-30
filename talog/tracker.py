"""talog watch 추적 대상 선택 + 사용자 정의 로그 패턴 경보.

추적 모드(`tracking.mode`):
  default — 소형 핵심 로그 9종 (기존 동작, 저부하)
  select  — 사용자가 고른 파일/와일드카드만 (예: Comm.log, seq_*.log, DLInfer.log)
  auto    — 일자 폴더의 *.log/*.txt 전체 (exclude 제외, 선택적으로 alg\\ 하위 포함)
select/auto 에서 처음 보는 큰 파일은 끝부분부터 읽는다(과거분 재파싱 방지).

패턴 경보(`rules.patterns`): 줄에 문구/정규식이 나오면 센다.
  count 1 = 한 번만 나와도 즉시, count N = window_min 분 안에 N번이면 경보.
  등급(info/warn/crit)은 메일 정책(즉시/묶음/안 보냄)과 이어진다.
"""

from __future__ import annotations

import os
import re
from collections import deque
from dataclasses import dataclass, field
from fnmatch import fnmatch

# 기본 모드의 감시 대상 (소형·핵심만 — 저부하 원칙)
DEFAULT_FILES = ("inspstarter.log", "comm.log", "workerthreadpoolmng.log",
                 "exception.log", "processusage.log", "batchrunlog.txt",
                 "seq_1.log", "seq_2.log", "seq_3.log")

_AUTO_EXT = (".log", ".txt")


def _ci(pat: str, name: str) -> bool:
    return fnmatch(name.lower(), pat.lower())


def resolve_targets(day_dir: str, tcfg: dict | None) -> list[str]:
    """추적 모드에 따라 일자 폴더에서 읽을 파일 경로 목록을 만든다."""
    tcfg = tcfg or {}
    mode = str(tcfg.get("mode", "default") or "default").lower()
    try:
        names = sorted(n for n in os.listdir(day_dir)
                       if os.path.isfile(os.path.join(day_dir, n)))
    except OSError:
        return []
    out: list[str] = []
    if mode == "select":
        pats = [str(p).replace("/", "\\") for p in (tcfg.get("files") or [])]
        for p in pats:
            if "\\" in p:                          # alg\\*.log 처럼 하위 폴더 지정
                sub, _, fp = p.rpartition("\\")
                sdir = os.path.join(day_dir, sub)
                try:
                    for n in sorted(os.listdir(sdir)):
                        fpath = os.path.join(sdir, n)
                        if os.path.isfile(fpath) and _ci(fp, n) and fpath not in out:
                            out.append(fpath)
                except OSError:
                    continue
            else:
                for n in names:
                    fpath = os.path.join(day_dir, n)
                    if _ci(p, n) and fpath not in out:
                        out.append(fpath)
        return out
    if mode == "auto":
        excl = [str(p) for p in (tcfg.get("exclude") or [])]
        dirs = [day_dir]
        if tcfg.get("include_alg"):
            dirs.append(os.path.join(day_dir, "alg"))
        for d in dirs:
            try:
                ns = sorted(os.listdir(d))
            except OSError:
                continue
            for n in ns:
                fpath = os.path.join(d, n)
                if not os.path.isfile(fpath) or not n.lower().endswith(_AUTO_EXT):
                    continue
                if any(_ci(p, n) for p in excl):
                    continue
                out.append(fpath)
        return out
    # default: 핵심 9종 (대소문자 변형 대응)
    lower = {n.lower(): n for n in names}
    for want in DEFAULT_FILES:
        if want in lower:
            out.append(os.path.join(day_dir, lower[want]))
    return out


# ---------------------------------------------------------------------------
@dataclass
class PatternRule:
    name: str
    rx: re.Pattern
    files: list = field(default_factory=list)   # 소문자 와일드카드, 비면 전체
    level: str = ""                              # Error/Debug/... (빈 값 = 무관)
    severity: str = "warn"
    count: int = 1
    window_s: float = 600.0
    cooldown_min: float = 30.0
    family: str = "system"
    hits: deque = field(default_factory=deque)
    samples: deque = field(default_factory=lambda: deque(maxlen=6))

    def applies(self, fname: str) -> bool:
        return not self.files or any(_ci(p, fname) for p in self.files)


def compile_patterns(items) -> tuple[list[PatternRule], list[str]]:
    """설정의 패턴 목록을 컴파일한다. 잘못된 항목은 건너뛰고 오류 문구를 모은다."""
    rules, errors = [], []
    for i, it in enumerate(items or []):
        if not isinstance(it, dict) or it.get("enabled", True) is False:
            continue
        name = str(it.get("name") or f"패턴{i + 1}")
        raw = str(it.get("match") or "")
        if not raw:
            errors.append(f"{name}: match 가 비어 있음")
            continue
        flags = re.I if it.get("ignore_case", True) else 0
        try:
            rx = (re.compile(raw[3:], flags) if raw.startswith("re:")
                  else re.compile(re.escape(raw), flags))
        except re.error as e:
            errors.append(f"{name}: 정규식 오류 {e}")
            continue
        sev = str(it.get("severity", "warn")).lower()
        rules.append(PatternRule(
            name=name, rx=rx,
            files=[str(f).lower() for f in (it.get("files") or []) if str(f).strip()],
            level=str(it.get("level") or ""),
            severity=sev if sev in ("info", "warn", "crit") else "warn",
            count=max(1, int(it.get("count", 1) or 1)),
            window_s=float(it.get("window_min", 10) or 10) * 60,
            cooldown_min=float(it.get("cooldown_min", 30) or 30),
            family=str(it.get("family", "system") or "system")))
    return rules, errors


class PatternEngine:
    """사용자 정의 패턴의 발생 횟수를 시간 창으로 세고 문턱에서 경보를 낸다."""

    def __init__(self, items, notifier, alert_cls):
        self.rules, self.errors = compile_patterns(items)
        self.notify = notifier
        self.Alert = alert_cls
        for e in self.errors:
            print(f"! 패턴 설정 오류(건너뜀): {e}")

    @property
    def active(self) -> bool:
        return bool(self.rules)

    def wants(self, fname: str) -> bool:
        return any(r.applies(fname) for r in self.rules)

    def prefilter(self, fname: str, level: str, text: str) -> bool:
        """리플레이용: 이 줄이 어떤 패턴에라도 걸리는가 (걸리는 줄만 이벤트로 남긴다)."""
        return any(r.applies(fname) and (not r.level or r.level == level)
                   and r.rx.search(text) for r in self.rules)

    def feed(self, ts: float, fname: str, level: str, text: str):
        for r in self.rules:
            if not r.applies(fname) or (r.level and r.level != level):
                continue
            if not r.rx.search(text):
                continue
            r.hits.append(ts)
            r.samples.append((ts, fname, text[:300]))
            while r.hits and ts - r.hits[0] > r.window_s:
                r.hits.popleft()
            n = len(r.hits)
            if n < r.count:
                continue
            title = (f"로그 패턴 '{r.name}' 발생" if r.count == 1 else
                     f"로그 패턴 '{r.name}' {r.window_s / 60:.0f}분 내 {n}회")
            self.notify.emit(self.Alert(
                ts, "pattern", r.severity, title, f"{fname}: {text.strip()[:220]}",
                key=r.name, cooldown_min=r.cooldown_min, family=r.family))

    def samples(self, name: str) -> list:
        for r in self.rules:
            if r.name == name:
                return list(r.samples)
        return []


# 콘솔 UI 의 패턴 프리셋 (현장 로그 실측 문구)
PRESETS = [
    {"name": "GPU 컨텍스트 치명 오류", "match": "enqueueV3 cudaGetLastError",
     "files": ["DLInfer.log"], "severity": "crit", "count": 1, "window_min": 10,
     "family": "reject"},
    {"name": "SEH 예외(액세스 위반)", "match": "Code: 0xc0000005", "files": ["exception.log"],
     "severity": "crit", "count": 1, "window_min": 10},
    {"name": "알고리즘 타임아웃 반복", "match": "Algorithm timeout", "files": ["seq_*.log"],
     "severity": "warn", "count": 3, "window_min": 10},
    {"name": "그랩 실패", "match": "[FAIL] Grab fail", "files": ["seq_*.log"],
     "severity": "crit", "count": 1, "window_min": 10},
    {"name": "비상 정지 송신", "match": "V2M_EMERGENCY_STOP", "files": ["comm.log"],
     "severity": "crit", "count": 1, "window_min": 10},
    {"name": "Error 레벨 급증", "match": "re:.", "level": "Error", "files": [],
     "severity": "warn", "count": 50, "window_min": 10},
]
