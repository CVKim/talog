"""로그 증분 읽기 — 새로 쓰인 바이트만 읽고 파일 회전·인코딩·사용자 패턴 줄을 따라간다."""

from __future__ import annotations

import datetime as dt
import json
import os

from ..events import Event, Extractor
from ..fileclass import classify
from ..lineparser import _BATCH_RE, _TAG_RE, _TS_RE, LogRecord, _sniff_encoding


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
                ev.obj_id = obj                       # seq 인스턴스(스레드) 짝맞춤용
                ev.alg_idx = fi.alg_idx               # seq_N.log 의 N (그룹 대응)
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
