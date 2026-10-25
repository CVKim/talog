"""처음 설정을 한 번에 — talos 로그 폴더를 이 PC 에서 찾아 '이 폴더로 시작' 한 번으로 감시를 맞춘다.

talos 는 `<드라이브>:\\AIV_LOG\\<프로세스>\\YYYY_MM\\DD\\` 에 로그를 쓴다(사이트마다 드라이브·폴더 이름이 다르다).
고정 디스크(네트워크·CD 는 건너뜀)의 맨 위 폴더 중 이름에 LOG 가 든 것과 그 아래 한 단계에서 YYYY_MM\\DD 일자
폴더가 있고 InspStarter.log·comm.log 가 든 곳을 찾는다. 몇 초 안에 끝나도록 둘러보는 폴더 수를 묶고 결과는 1분 둔다.
"""

from __future__ import annotations

import copy
import os
import re
import string
import sys
import time

from .util import recent_days

_KEY = ("inspstarter.log", "comm.log")
_YM = re.compile(r"^\d{4}_\d{2}$")
_BUDGET_S, _MAX_TOP, _MAX_SUB = 4.0, 60, 40


def _fixed_drives() -> list[str]:
    """고정 디스크 드라이브 (Windows). 네트워크 드라이브는 응답이 늦을 수 있어 뺀다."""
    if sys.platform != "win32":
        return []
    import ctypes
    k = ctypes.windll.kernel32
    mask, out = k.GetLogicalDrives(), []
    for i, ch in enumerate(string.ascii_uppercase):
        if mask & (1 << i):
            root = f"{ch}:\\"
            if k.GetDriveTypeW(ctypes.c_wchar_p(root)) == 3:          # DRIVE_FIXED
                out.append(root)
    return out


def _has_months(d: str) -> bool:
    try:
        return any(_YM.match(n) for n in os.listdir(d))
    except OSError:
        return False


def _describe(root: str) -> dict | None:
    days = recent_days(root, 1)
    if not days:
        return None
    try:
        names = {n.lower() for n in os.listdir(days[0])}
    except OSError:
        return None
    key = [k for k in _KEY if k in names]
    if not key:
        return None
    rel = os.path.relpath(days[0], root)
    today = time.strftime("%Y_%m") + os.sep + time.strftime("%d")
    return {"root": root, "latest": rel, "today": rel == today, "files": len(names)}


def find_log_roots(bases: list[str] | None = None, budget_s: float = _BUDGET_S) -> list[dict]:
    """talos 로그 루트 후보 — 최근 일자 폴더가 새것부터. bases 를 주면 그 폴더들만 본다(시험용)."""
    t0, seen, out = time.time(), set(), []

    def look(d: str):
        if time.time() - t0 > budget_s:
            return
        key = os.path.normcase(os.path.abspath(d))
        if key in seen:
            return
        seen.add(key)
        if _has_months(d):
            got = _describe(d)
            if got:
                out.append(got)
            return
        try:
            subs = sorted(os.listdir(d))[:_MAX_SUB]
        except OSError:
            return
        for n in subs:
            p = os.path.join(d, n)
            if os.path.isdir(p) and _has_months(p):
                got = _describe(p)
                if got:
                    out.append(got)

    if bases is None:
        bases = []
        for drv in _fixed_drives():
            try:
                tops = sorted(os.listdir(drv))[:_MAX_TOP]
            except OSError:
                continue
            bases += [os.path.join(drv, n) for n in tops if "log" in n.lower() and os.path.isdir(os.path.join(drv, n))]
    for b in bases:
        look(b)
    out.sort(key=lambda x: (x["today"], x["latest"]), reverse=True)
    return out


class SetupMixin:
    """Console 이 물려받는다 — 공유 상태는 app.Console.__init__ 에 있다."""

    def setup_detect(self) -> dict:
        """지금 로그 루트가 맞는지와 이 PC 에서 찾은 후보 — 운영 현황의 '이 폴더로 시작' 버튼이 쓴다."""
        now = time.time()
        cache = getattr(self, "_setup_cache", None)
        if not cache or now - cache[0] > 60:
            cache = (now, find_log_roots())
            self._setup_cache = cache
        cur = self.settings.get("log_root", "")
        same = os.path.normcase(os.path.abspath(cur)) if cur else ""
        cands = [c for c in cache[1] if os.path.normcase(os.path.abspath(c["root"])) != same]
        return {"current": cur, "current_ok": bool(cur and _describe(cur)), "candidates": cands[:5]}

    def setup_root(self, root: str) -> tuple[int, dict]:
        """로그 루트만 바꿔 저장하고 감시를 다시 시작한다 (운영자 모드 — http.py)."""
        root = str(root or "").strip()
        if not root or not os.path.isdir(root) or not _describe(root):
            return 400, {"ok": False, "error": "talos 로그 폴더가 아닙니다 — 그 아래에 YYYY_MM\\DD 일자 폴더와 InspStarter.log·comm.log 가 있어야 합니다"}
        s = copy.deepcopy(self.settings)
        s["log_root"] = root
        errs = self.save(s)
        if errs:
            return 400, {"ok": False, "error": "; ".join(errs)}
        self._setup_cache = None
        return 200, {"ok": True, "log_root": root, "running": self.running()}
