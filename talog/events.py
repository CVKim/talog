"""이벤트 추출 엔진.

rules/events.yaml 의 카테고리별 룰을 컴파일하여 LogRecord 스트림에 적용한다.
룰 미적중이라도 level=Error 인 레코드는 ERROR 이벤트로 보존한다.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

import yaml

from .fileclass import FileInfo
from .lineparser import LogRecord, iter_batchrun, iter_records

_RULES_PATH = os.path.join(os.path.dirname(__file__), "rules", "events.yaml")


@dataclass(slots=True)
class Event:
    ts: float
    ts_text: str
    kind: str
    file_id: int = 0
    level: str = ""
    obj_id: str = ""
    inner_id: str = ""
    product_id: str = ""
    roi_idx: int = 0
    alg_idx: int = 0          # 이벤트가 난 alg 채널 인덱스 (alg 파일 한정)
    block: str = ""
    model: str = ""
    value: float = 0.0
    status: str = ""
    name: str = ""
    alg_list: str = ""
    extra: str = ""
    line_no: int = 0


@dataclass(slots=True)
class CompiledRule:
    kind: str
    msg_re: re.Pattern
    header_re: Optional[re.Pattern]
    level: str


class Extractor:
    def __init__(self, rules_path: str = _RULES_PATH):
        with open(rules_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        self.rules: dict[str, list[CompiledRule]] = {}
        for cat, items in raw.items():
            lst = []
            for it in items or []:
                lst.append(CompiledRule(
                    kind=it["kind"],
                    msg_re=re.compile(it["msg"]),
                    header_re=re.compile(it["header"]) if it.get("header") else None,
                    level=it.get("level", ""),
                ))
            self.rules[cat] = lst

    # ---- 개별 파일 추출 ---------------------------------------------------
    def extract_file(self, fi: FileInfo, file_id: int) -> tuple[list[Event], int]:
        """파일 하나를 파싱해 (이벤트 목록, 총 레코드 수)를 반환한다."""
        if fi.category == "batchrun":
            evs = [Event(ts=ts, ts_text=tt, kind="BATCH", name=script, file_id=file_id)
                   for ts, tt, script in iter_batchrun(fi.path)]
            return evs, len(evs)

        rules = self.rules.get(fi.category, [])
        out: list[Event] = []
        n = 0
        for rec in iter_records(fi.path):
            n += 1
            ev = self._match(rec, rules)
            if ev is None and rec.level == "Error":
                # 룰 미적중 에러도 전수 보존 (에러 테이블용)
                ev = Event(ts=rec.ts, ts_text=rec.ts_text, kind="ERROR",
                           extra=rec.msg[:500], line_no=rec.line_no)
            if ev is not None:
                ev.file_id = file_id
                ev.level = rec.level
                ev.obj_id = rec.obj_id
                ev.alg_idx = fi.alg_idx
                if not ev.line_no:
                    ev.line_no = rec.line_no
                if fi.category == "comm" and ev.kind == "COMM_MSG":
                    self._enrich_comm(ev)
                out.append(ev)
        return out, n

    def _match(self, rec: LogRecord, rules: list[CompiledRule]) -> Optional[Event]:
        for r in rules:
            if r.level and rec.level != r.level:
                continue
            if r.header_re is not None:
                hm = r.header_re.search(rec.header)
                if not hm:
                    continue
                hgroups = hm.groupdict()
            else:
                hgroups = {}
            m = r.msg_re.search(rec.msg)
            if not m:
                continue
            g = {**hgroups, **m.groupdict()}
            return Event(
                ts=rec.ts, ts_text=rec.ts_text, kind=r.kind,
                inner_id=g.get("inner_id", "") or "",
                product_id=(g.get("product_id", "") or "").strip(),
                roi_idx=int(g["roi_idx"]) if g.get("roi_idx") else 0,
                block=g.get("block", "") or "",
                model=(g.get("model", "") or "").strip(),
                value=float(g["value"]) if g.get("value") else 0.0,
                status=g.get("status", "") or "",
                name=(g.get("name", "") or "").strip(),
                alg_list=(g.get("alg_list", "") or "").strip().rstrip(","),
                extra=(g.get("extra", "") or "")[:500],
                line_no=rec.line_no,
            )
        return None

    @staticmethod
    def _enrich_comm(ev: Event):
        """comm 메시지 페이로드에서 inner_id / status / 그룹(존) 을 뽑는다.

        예: V2M_INSPECT_START_ACK,V3.0,TALOS3,OK,2000026072709204650,1642226107,1
            V2M_INSPECT_END,V3.0,TALOS1,NG,...,<inner>,ProductID,3
        마지막 토큰이 1~2자리 숫자면 그룹(존) 번호다 (Tenneco 는 검사당 4존).
        """
        toks = [t.strip() for t in ev.extra.split(",")]
        inner = next((t for t in toks if t.isdigit() and len(t) >= 15), "")
        ev.inner_id = inner
        if inner:
            i = toks.index(inner)
            prev = toks[:i]
            # NG END 는 'NG,<개수>,<결함1..N>,inner,...' 라 inner 직전 토큰이
            # 결함명이다 (CommSender.cpp 실측) → 알려진 상태 토큰을 우선 탐색
            known = ("OK", "NG", "NoInspThread", "BusyCam", "NotModelLoaded",
                     "SimulationModelLoaded", "GroupIndexError",
                     "NotMatchedSequenceType", "StorageNotEnough",
                     "LightDisconnected")
            ev.status = next((t for t in prev if t in known),
                             prev[-1] if prev and not prev[-1].isdigit() else "")
        if toks and toks[-1].isdigit() and len(toks[-1]) <= 2:
            ev.value = float(toks[-1])       # 그룹(존) 번호
        ev.extra = ev.extra[:200]
