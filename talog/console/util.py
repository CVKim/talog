"""콘솔 공용 도우미 — 페이지·자산 경로, 일자 폴더, 리포트 이름, 실행 로그 보관."""

from __future__ import annotations

import datetime as dt
import glob
import io
import os
import re
from collections import Counter, deque

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # talog 패키지 폴더
REPO = os.path.dirname(PKG)                                           # 저장소 루트 (소스 실행일 때)
PAGE = os.path.join(PKG, "console.html")
ASSETS = os.path.join(PKG, "assets")
TAG_RE = re.compile(r"^[\w.#\- ()가-힣]{1,120}$")                     # 리포트 태그 (경로 문자 없이)


def _latest_day_dir(root: str) -> str:
    days = recent_days(root, 1)
    return days[0] if days else ""


def tail2_path(p: str) -> str:
    """경로의 마지막 두 단계 (화면 표시용 — 예: 2026_10\12)."""
    return "\\".join([x for x in re.split(r"[\\/]", str(p or "")) if x][-2:])


def recent_days(root: str, n: int = 14) -> list[str]:
    """log_root 아래 최근 YYYY_MM\\DD 폴더 (새것부터)."""
    got = [p for p in glob.glob(os.path.join(root, "[0-9][0-9][0-9][0-9]_[0-9][0-9]",
                                             "[0-9][0-9]")) if os.path.isdir(p)]
    return sorted(got, reverse=True)[:n]


def _recipe_defects(day_dir: str) -> tuple[list[str], list[list]]:
    """comm.log 에서 (레시피 결함명 목록, 그날 NG 결함 빈도)를 뽑는다 (꼬리 8MB)."""
    path = ""
    try:
        for n in os.listdir(day_dir):
            if n.lower() == "comm.log":
                path = os.path.join(day_dir, n)
    except OSError:
        return [], []
    if not path:
        return [], []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - 8 * 1048576))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return [], []
    names: list[str] = []
    cnt: Counter = Counter()
    for ln in text.splitlines():
        if "V2M_MODEL_LOAD_DEFECT_NAME," in ln:
            toks = [t.strip() for t in ln.split("V2M_MODEL_LOAD_DEFECT_NAME,", 1)[1].split(",")]
            # V3.0, TALOSn, <개수>, 결함명...
            if len(toks) > 3 and toks[2].isdigit():
                names = [t for t in toks[3:] if t]
        elif "V2M_INSPECT_END," in ln and ",NG," in ln:
            toks = [t.strip() for t in ln.split(",")]
            try:
                i = toks.index("NG")
            except ValueError:
                continue
            j = i + 2 if i + 1 < len(toks) and toks[i + 1].isdigit() else i + 1
            for t in toks[j:]:
                if t.isdigit() and len(t) >= 15:
                    break
                if t and not t.isdigit():
                    cnt[t] += 1
    return names, [[k, v] for k, v in cnt.most_common(30)]


def _report_tag(day_dir: str) -> str:
    """리포트 이름: ...\\2026_09\\28 → 2026_09_28, 그 밖은 마지막 두 폴더 이름."""
    parts = [p for p in re.split(r"[\\/]+", os.path.normpath(day_dir)) if p]
    tail = parts[-2:] if len(parts) >= 2 else parts
    tag = "_".join(tail)
    return re.sub(r"[^\w.\-가-힣]+", "_", tag).strip("_") or "report"


class _Tee(io.TextIOBase):
    """콘솔 창 출력은 그대로 두고 최근 줄을 화면의 실행 로그용으로 보관한다."""

    def __init__(self, stream, buf: deque):
        self.stream = stream
        self.buf = buf
        self._part = ""

    def write(self, s):
        try:
            self.stream.write(s)
        except (UnicodeError, OSError, ValueError):
            pass
        self._part += s
        while "\n" in self._part:
            line, self._part = self._part.split("\n", 1)
            self.buf.append(f"{dt.datetime.now():%H:%M:%S} {line}")
        return len(s)

    def flush(self):
        try:
            self.stream.flush()
        except (OSError, ValueError):
            pass
