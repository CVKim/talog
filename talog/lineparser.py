"""talos 로그 라인 문법 파서.

라인 문법 (AIV.UtilFactory CSLogWriter 출력):
    yyyy/MM/dd-HH:mm:ss.fff<TAB>[Level][Header][ObjId]<TAB>message<CRLF>

주의 사항 (talos-common IAivLog.h 분석 결과):
  - ObjId 는 스레드 ID가 아니라 로깅 객체의 인스턴스 주소((UINT_PTR)this)이며,
    static/직접 WriteAsync 호출은 0 이다. 같은 값 = 같은 파이프라인 인스턴스.
  - Header 는 __FUNCTIONW__ (Class::Func) 또는 리터럴(Tact, USAGE, console 등).
  - 메시지에 개행이 포함될 수 있다(exception.log 콜스택 등)
    → 타임스탬프로 시작하지 않는 줄은 직전 레코드의 연속으로 붙인다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, Optional

# 2026/07/29-09:21:14.811
_TS_RE = re.compile(r"^(\d{4})/(\d{2})/(\d{2})-(\d{2}):(\d{2}):(\d{2})\.(\d{3})\t")
_TAG_RE = re.compile(r"^\[(\w+)\]\[([^\]]*)\]\[(\d+)\]\t?(.*)$", re.S)


@dataclass(slots=True)
class LogRecord:
    ts: float           # epoch seconds (밀리초 포함)
    ts_text: str        # "HH:MM:SS.fff" (리포트 표기용)
    level: str          # Debug / Info / Error / Verbose (비표준이면 원문)
    header: str         # Class::Func 또는 리터럴
    obj_id: str         # 인스턴스 주소 문자열 ("0" 포함)
    msg: str
    line_no: int        # 파일 내 시작 라인 번호(1-base)


def _sniff_encoding(path: str, probe: int = 65536) -> str:
    """UTF-8 우선, 실패 시 CP949. BOM 계열도 함께 판별한다."""
    with open(path, "rb") as f:
        head = f.read(probe)
    if head.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if head.startswith(b"\xff\xfe") or head.startswith(b"\xfe\xff"):
        return "utf-16"
    try:
        head.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "cp949"


def iter_records(path: str) -> Iterator[LogRecord]:
    """로그 파일을 스트리밍 파싱한다. 멀티라인 메시지는 직전 레코드에 병합."""
    enc = _sniff_encoding(path)
    pending: Optional[LogRecord] = None
    with open(path, "r", encoding=enc, errors="replace", newline="") as f:
        for line_no, raw in enumerate(f, 1):
            line = raw.rstrip("\r\n")
            m = _TS_RE.match(line)
            if not m:
                # 연속 줄: 직전 레코드 메시지에 병합 (비정형 파일 방어를 위해 상한 유지)
                if pending is not None and len(pending.msg) < 65536:
                    pending.msg += "\n" + line
                continue
            if pending is not None:
                yield pending
            yy, mo, dd, hh, mi, ss, ms = m.groups()
            rest = line[m.end():]
            tm = _TAG_RE.match(rest)
            if tm:
                level, header, obj_id, msg = tm.groups()
            else:
                # 태그 블록이 없는 비정형 줄 (BatchRunLog 형식 등은 별도 파서 사용)
                level, header, obj_id, msg = "", "", "0", rest
            ts = datetime(int(yy), int(mo), int(dd), int(hh), int(mi), int(ss),
                          int(ms) * 1000).timestamp()
            pending = LogRecord(ts=ts, ts_text=f"{hh}:{mi}:{ss}.{ms}", level=level,
                                header=header, obj_id=obj_id, msg=msg, line_no=line_no)
    if pending is not None:
        yield pending


# BatchRunLog.txt: "2026-07-27 08:30:57 - talos_kill.bat task start."
_BATCH_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2}) - (\S+) task start")


def iter_batchrun(path: str) -> Iterator[tuple[float, str, str]]:
    """BatchRunLog.txt 전용 파서. (ts, ts_text, 스크립트명) 을 낸다."""
    enc = _sniff_encoding(path)
    with open(path, "r", encoding=enc, errors="replace") as f:
        for line in f:
            m = _BATCH_RE.match(line.strip())
            if m:
                yy, mo, dd, hh, mi, ss, script = m.groups()
                ts = datetime(int(yy), int(mo), int(dd), int(hh), int(mi), int(ss)).timestamp()
                yield ts, f"{hh}:{mi}:{ss}.000", script
