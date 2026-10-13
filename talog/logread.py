"""원본 로그 직접 읽기 — Claude 가 MCP 로 talos 로그를 시각·문구·검사 id 로 찾아 읽는다 (읽기 전용).

하루 폴더가 수십 GB 다(풍산 2호기 10/12: 37GB, InspResult_9.log 하나 4.4GB). 그래서 통째로 훑지 않는다:
줄 앞 시각(YYYY/MM/DD-HH:MM:SS.mmm)으로 이분 탐색해 원하는 시각으로 바로 가고, 한 번 부를 때 읽는
줄·바이트·시간에 한도를 둔다. 큰 파일은 시각 범위가 있어야 찾는다.
"""

from __future__ import annotations

import fnmatch
import glob
import os
import re
import time
from collections import Counter
from datetime import date, datetime, timedelta

from .fileclass import classify
from .lineparser import _sniff_encoding

_TS = re.compile(rb"^(\d{4})/(\d{2})/(\d{2})-(\d{2}):(\d{2}):(\d{2})\.(\d{3})")
_ID_TIME = re.compile(r"^\d{5}(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})\d+$")   # 20000 YYMMDD hhmmss 순번

MAX_LINES = 300                 # 한 번에 돌려주는 최대 줄
MAX_LINE_CHARS = 600            # 줄 하나 최대 글자
SCAN_BYTES = 384 << 20          # 한 번 찾을 때 훑는 최대 바이트
SCAN_SECONDS = 20.0             # 한 번 찾을 때 최대 시간
FULL_SCAN_MAX = 160 << 20       # 시각 범위 없이 통째로 훑는 파일 크기 상한
CORE_DEFAULT = ("InspStarter.log", "comm.log", "seq_*.log", "exception.log", "WorkerThreadPoolMng.log",
                "InspSkipper.log", "StopThread.log", "InspectMng.log", "comm_fail.log")
TRACE_FILES = CORE_DEFAULT + ("alg/*.log", "DLInfer.log", "ProcessingBlock.log")


# ── 시각 ────────────────────────────────────────────────────────────
def ts_of(line: bytes) -> float | None:
    """줄 앞 시각 → 초 (날짜 포함, 시간대 없이). 시각이 없는 줄(여러 줄 메시지)은 None."""
    m = _TS.match(line)
    if not m:
        return None
    y, mo, d, h, mi, s, ms = (int(x) for x in m.groups())
    try:
        return (date(y, mo, d).toordinal() - 719163) * 86400 + h * 3600 + mi * 60 + s + ms / 1000
    except ValueError:
        return None


def fmt_ts(t: float | None, with_date: bool = False) -> str:
    if t is None:
        return "-"
    d = datetime(1970, 1, 1) + timedelta(seconds=t)
    return d.strftime("%m/%d %H:%M:%S.%f" if with_date else "%H:%M:%S.%f")[:-3]


def parse_when(s: str, ref: date | None) -> float:
    """'05:02', '05:02:28', '05:02:28.5', '2026/10/12 05:02:28', '2026-10-12T05:02' → 초. 날짜가 없으면 ref 날짜."""
    s = (s or "").strip()
    iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})[T ](.*)$", s)
    if iso:
        s = f"{iso.group(1)}/{iso.group(2)}/{iso.group(3)} {iso.group(4)}"
    m = re.match(r"^(?:(\d{4})/(\d{2})/(\d{2})[ \-])?(\d{1,2}):(\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?$", s)
    if not m:
        raise ValueError(f"시각 형식을 모릅니다: '{s}' (예: 05:02, 05:02:28, 2026/10/12 05:02:28)")
    y, mo, d, h, mi, sec, ms = m.groups()
    dd = date(int(y), int(mo), int(d)) if y else ref
    if dd is None:
        raise ValueError("날짜를 알 수 없습니다 — '2026/10/12 05:02:28' 처럼 날짜를 함께 쓰십시오")
    frac = int((ms or "0").ljust(3, "0")) / 1000
    return (dd.toordinal() - 719163) * 86400 + int(h) * 3600 + int(mi) * 60 + int(sec or 0) + frac


def id_time(raw_id: str) -> float | None:
    """talos inner id(20000 YYMMDD hhmmss 순번)에 든 검사 시작 시각. 형식이 다르면 None."""
    m = _ID_TIME.match(raw_id or "")
    if not m:
        return None
    yy, mo, d, h, mi, s = (int(x) for x in m.groups())
    try:
        return (date(2000 + yy, mo, d).toordinal() - 719163) * 86400 + h * 3600 + mi * 60 + s
    except ValueError:
        return None


# ── 폴더·파일 ───────────────────────────────────────────────────────
def _norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(p))


def inside(path: str, root: str) -> bool:
    p, r = _norm(path), _norm(root)
    return p == r or p.startswith(r.rstrip("\\/") + os.sep)


def _looks_like_day(p: str) -> bool:
    try:
        names = {n.lower() for n in os.listdir(p)}
    except OSError:
        return False
    return bool(names & {"inspstarter.log", "comm.log", "alg"})


def day_dirs(roots: list[str], limit: int = 40) -> list[str]:
    """로그 루트 아래 일자 폴더 (talos 기본 YYYY_MM\\DD, 바로 아래 폴더, 루트 자체) — 새것부터."""
    out: dict[str, float] = {}
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        cands = glob.glob(os.path.join(root, "[0-9][0-9][0-9][0-9]_[0-9][0-9]", "[0-9][0-9]"))
        try:
            cands += [os.path.join(root, n) for n in os.listdir(root)]
        except OSError:
            pass
        cands.append(root)
        for c in cands:
            if os.path.isdir(c) and _looks_like_day(c):
                try:
                    out[os.path.abspath(c)] = os.path.getmtime(c)
                except OSError:
                    pass
    return [p for p, _ in sorted(out.items(), key=lambda kv: -kv[1])][:limit]


def resolve_day(roots: list[str], day: str) -> str:
    """일자 폴더 이름(12, 2026_10\\12, 20261012) 또는 절대 경로 → 허용된 루트 안의 폴더."""
    day = (day or "").strip().strip('"')
    if not day:
        found = day_dirs(roots, 1)
        if not found:
            raise ValueError("읽을 수 있는 로그 폴더가 없습니다 — 설정의 로그 루트나 콘솔에서 고른 폴더를 확인하십시오")
        return found[0]
    if ".." in re.split(r"[\\/]", day):
        raise ValueError("'..' 는 쓸 수 없습니다")
    cands = [day] if os.path.isabs(day) else []
    for r in roots:
        if not r:
            continue
        if not os.path.isabs(day):
            cands.append(os.path.join(r, day))
        m = re.match(r"^(\d{4})[-_/]?(\d{2})[-_/]?(\d{2})$", day)
        if m:
            y, mo, d = m.groups()
            cands += [os.path.join(r, f"{y}_{mo}", d), os.path.join(r, d), os.path.join(r, f"{y}{mo}{d}")]
    for c in cands:
        if os.path.isdir(c) and any(inside(c, r) for r in roots if r):
            return os.path.abspath(c)
    near = ", ".join(os.path.relpath(p, roots[0]) if roots and inside(p, roots[0]) else os.path.basename(p)
                     for p in day_dirs(roots, 8))
    raise ValueError(f"로그 폴더를 찾지 못했습니다: '{day}' — 읽을 수 있는 폴더: {near or '(없음)'}")


def _files(day_dir: str) -> list[str]:
    out = []
    for base in (day_dir, os.path.join(day_dir, "alg")):
        try:
            for n in os.listdir(base):
                p = os.path.join(base, n)
                if os.path.isfile(p) and n.lower().endswith((".log", ".txt")):
                    out.append(p)
        except OSError:
            continue
    return out


def rel(day_dir: str, path: str) -> str:
    return os.path.relpath(path, day_dir)


def match_files(day_dir: str, patterns) -> list[str]:
    """파일 이름·와일드카드(대소문자 무시, 'alg\\*.log' 도 됨) → 경로. 일자 폴더 밖은 막는다."""
    pats = [p.strip().replace("/", os.sep).replace("\\", os.sep) for p in (patterns or []) if str(p).strip()]
    allf = _files(day_dir)
    out = []
    for p in pats:
        if ".." in p.split(os.sep) or os.path.isabs(p):
            raise ValueError(f"파일은 일자 폴더 안의 이름으로 씁니다: {p}")
        hit = [f for f in allf if fnmatch.fnmatch(rel(day_dir, f).lower(), p.lower())
               or fnmatch.fnmatch(os.path.basename(f).lower(), p.lower())]
        out += [h for h in hit if h not in out]
    return out


def _head_tail_ts(path: str, size: int) -> tuple[float | None, float | None]:
    first = last = None
    try:
        with open(path, "rb") as f:
            for ln in f.read(16384).splitlines():
                first = ts_of(ln)
                if first is not None:
                    break
            f.seek(max(0, size - 65536))
            for ln in reversed(f.read(65536).splitlines()):
                last = ts_of(ln)
                if last is not None:
                    break
    except OSError:
        pass
    return first, last


def list_files(day_dir: str) -> list[dict]:
    rows = []
    for p in _files(day_dir):
        try:
            size = os.path.getsize(p)
        except OSError:
            continue
        fi = classify(p)
        first, last = _head_tail_ts(p, size)
        rows.append({"name": rel(day_dir, p), "size": size, "category": fi.category if fi else "other",
                     "core": bool(fi and fi.core), "first": first, "last": last})
    rows.sort(key=lambda r: (not r["core"], r["name"].lower()))
    return rows


def day_date(day_dir: str) -> date | None:
    """일자 폴더의 날짜: 이름(YYYY_MM\\DD) → 없으면 InspStarter·comm 가운데쯤 줄의 날짜."""
    m = re.search(r"(\d{4})_(\d{2})[\\/](\d{2})$", os.path.abspath(day_dir))
    if m:
        try:
            return date(*(int(x) for x in m.groups()))
        except ValueError:
            pass
    for name in ("InspStarter.log", "comm.log"):
        p = os.path.join(day_dir, name)
        if os.path.isfile(p):
            size = os.path.getsize(p)
            with open(p, "rb") as f:
                t, _ = _ts_after(f, size // 2, size)
            if t is not None:
                return date.fromordinal(int(t // 86400) + 719163)
    files = _files(day_dir)
    for p in files[:5]:
        first, _ = _head_tail_ts(p, os.path.getsize(p))
        if first is not None:
            return date.fromordinal(int(first // 86400) + 719163)
    return None


# ── 읽기 ────────────────────────────────────────────────────────────
def _ts_after(f, pos: int, size: int) -> tuple[float | None, int]:
    """pos 뒤 첫 '시각 있는 줄'의 시각과 그 줄 시작 위치."""
    f.seek(pos)
    if pos > 0:
        f.readline()
    for _ in range(200):
        start = f.tell()
        ln = f.readline()
        if not ln:
            return None, size
        t = ts_of(ln)
        if t is not None:
            return t, start
    return None, f.tell()


def seek_time(f, size: int, target: float) -> int:
    """시각이 target 이상인 첫 줄의 위치 (줄은 시간순이라고 본다)."""
    lo, hi = 0, size
    while hi - lo > 65536:
        mid = (lo + hi) // 2
        t, _p = _ts_after(f, mid, size)
        if t is None or t >= target:
            hi = mid
        else:
            lo = mid
    f.seek(lo)
    if lo > 0:
        f.readline()
    while True:
        p = f.tell()
        ln = f.readline()
        if not ln:
            return size
        t = ts_of(ln)
        if t is not None and t >= target:
            return p


def _back_lines(f, pos: int, n: int) -> int:
    """줄 시작 pos(또는 파일 끝)에서 n 줄 거슬러 간 줄 시작 위치."""
    if n <= 0 or pos <= 0:
        return pos
    f.seek(pos - 1)
    need = n + 1 if f.read(1) == b"\n" else n         # 바로 앞 줄바꿈은 그 줄의 끝
    chunk = 65536
    while True:
        start = max(0, pos - chunk)
        f.seek(start)
        buf = f.read(pos - start)
        idx, found = len(buf), 0
        while found < need:
            j = buf.rfind(b"\n", 0, idx)
            if j < 0:
                break
            found, idx = found + 1, j
        if found >= need:
            return start + idx + 1
        if start == 0:
            return 0
        chunk *= 4


def _clip(s: str) -> str:
    return s if len(s) <= MAX_LINE_CHARS else s[:MAX_LINE_CHARS] + f" …(+{len(s) - MAX_LINE_CHARS}자)"


def read_window(path: str, at: float | None = None, offset: int | None = None, tail: bool = False,
                lines: int = 80, before: int = 20) -> dict:
    """한 파일의 한 구간: 시각(at) 앞뒤, 바이트 위치(offset, 앞 호출의 next), 끝(tail), 또는 처음."""
    lines = max(1, min(int(lines or 80), MAX_LINES))
    size = os.path.getsize(path)
    enc = _sniff_encoding(path)
    with open(path, "rb") as f:
        if tail:
            pos = _back_lines(f, size, lines)
        elif at is not None:
            pos = _back_lines(f, seek_time(f, size, at), max(0, min(int(before or 0), lines - 1)))
        elif offset is not None:
            pos = max(0, min(int(offset), size))
            if pos > 0:
                f.seek(pos - 1)
                if f.read(1) != b"\n":
                    f.readline()
                    pos = f.tell()
        else:
            pos = 0
        f.seek(pos)
        out = []
        for _ in range(lines):
            ln = f.readline()
            if not ln:
                break
            out.append(_clip(ln.decode(enc, "replace").rstrip("\r\n")))
        nxt = f.tell()
    return {"start": pos, "next": nxt, "size": size, "lines": out, "eof": nxt >= size}


def search(paths: list[str], query: str, regex: bool = False, t_from: float | None = None,
           t_to: float | None = None, max_hits: int = 100, seconds: float = SCAN_SECONDS,
           max_bytes: int = SCAN_BYTES, day_dir: str = "") -> dict:
    """여러 파일에서 문구(대소문자 무시) 또는 정규식을 찾는다. 시각 범위가 있으면 그 범위로 바로 간다."""
    query = (query or "").strip()
    if not query:
        raise ValueError("찾을 문구가 비었습니다")
    if regex:
        if len(query) > 300:
            raise ValueError("정규식이 너무 깁니다 (300자 이하)")
        try:
            pat = re.compile(query, re.I)
        except re.error as e:
            raise ValueError(f"정규식 오류: {e}")
    max_hits = max(1, min(int(max_hits or 100), MAX_LINES))
    deadline = time.time() + seconds
    hits, per, skipped = [], Counter(), []
    scanned, partial = 0, False
    for p in paths:
        name = rel(day_dir, p) if day_dir else os.path.basename(p)
        try:
            size = os.path.getsize(p)
        except OSError:
            continue
        if t_from is None and size > FULL_SCAN_MAX:
            skipped.append(f"{name}({size >> 20}MB — 시각 범위를 주면 찾음)")
            continue
        enc = _sniff_encoding(p)
        qb = None if regex else query.lower().encode(enc, "ignore")
        last_t, n = None, 0
        with open(p, "rb") as f:
            if t_from is not None:
                f.seek(seek_time(f, size, t_from))
            for raw in f:
                scanned += len(raw)
                n += 1
                t = ts_of(raw)
                if t is not None:
                    last_t = t
                    if t_to is not None and t > t_to:
                        break
                ok = (pat.search(raw[:4000].decode(enc, "replace")) is not None) if regex else (qb in raw.lower())
                if ok:
                    hits.append((last_t if t is None else t, name, _clip(raw.decode(enc, "replace").rstrip("\r\n"))))
                    per[name] += 1
                    if len(hits) >= max_hits:
                        partial = True
                        break
                if scanned > max_bytes or (n % 4000 == 0 and time.time() > deadline):
                    partial = True
                    break
        if partial:
            break
    hits.sort(key=lambda h: (h[0] is None, h[0] or 0))
    return {"hits": hits, "per_file": dict(per), "scanned": scanned, "partial": partial, "skipped": skipped}


def trace(day_dir: str, raw_id: str, window_s: int = 180, hint: float | None = None) -> dict:
    """검사 하나(inner id)를 여러 로그에서 시간순으로 모은다. 시작 시각은 id 안의 시각 → InspStarter 첫 등장 순."""
    window_s = max(10, min(int(window_s or 180), 1800))
    files = match_files(day_dir, TRACE_FILES)

    def first_seen() -> float | None:
        r0 = search(match_files(day_dir, ["InspStarter.log", "comm.log"]), raw_id, max_hits=1, day_dir=day_dir)
        return r0["hits"][0][0] if r0["hits"] else None

    t0, found_by = (hint, "at") if hint is not None else (id_time(raw_id), "id 안의 시각")
    r = None
    if t0 is not None:
        r = search(files, raw_id, t_from=t0 - 5, t_to=t0 + window_s, max_hits=MAX_LINES, day_dir=day_dir)
    if (r is None or not r["hits"]) and hint is None:      # id 에 시각이 없거나 다른 형식인 현장
        t1 = first_seen()
        if t1 is not None:
            t0, found_by = t1, "InspStarter·comm 첫 등장"
            r = search(files, raw_id, t_from=t0 - 5, t_to=t0 + window_s, max_hits=MAX_LINES, day_dir=day_dir)
    if r is None:
        raise ValueError("이 검사 id 의 시각을 찾지 못했습니다 — 시각(at)을 함께 주거나 search_logs 로 먼저 찾으십시오")
    r["t0"], r["found_by"], r["window_s"] = t0, found_by, window_s
    return r
