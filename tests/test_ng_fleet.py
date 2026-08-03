"""NG 결함 분포 파싱과 fleet 인덱스 빌더 테스트."""

import os

from talog.assemble import _parse_ng_defects, build_inspections
from talog.events import Event
from talog.fleetindex import render


INNER = "2000026080312345678"


def _ev(ts, kind, **kw):
    e = Event(ts=ts, ts_text=f"00:00:{int(ts):02d}.000", kind=kind)
    for k, v in kw.items():
        setattr(e, k, v)
    return e


def test_parse_ng_defects_with_count():
    """NG,<개수>,<결함들>,<inner> 페이로드에서 결함명만 추출된다."""
    extra = (f"V3.0,TALOS1,NG,4,CLINCH_ANGLE,HOLE_MISSING,HOLE_ANGLE,"
             f"CLINCH_MISSING,{INNER},ProductID,3")
    assert _parse_ng_defects(extra, INNER) == [
        "CLINCH_ANGLE", "HOLE_MISSING", "HOLE_ANGLE", "CLINCH_MISSING"]


def test_parse_ng_defects_malformed_returns_empty():
    """NG/inner 토큰이 없는 페이로드는 빈 목록을 반환한다 (크래시 금지)."""
    assert _parse_ng_defects("V3.0,TALOS1,OK,123", INNER) == []
    assert _parse_ng_defects("", INNER) == []


def test_ng_end_collects_defects_and_stays_ng():
    """NG END 수신 시 결함이 수집되고, 이후 OK 존이 와도 NG 가 유지된다."""
    events = [
        _ev(1.0, "INSP_START", inner_id=INNER, value=3),
        _ev(2.0, "COMM_MSG", name="V2M_INSPECT_START_ACK",
            inner_id=INNER, status="OK", value=1.0),
        _ev(2.5, "COMM_MSG", name="V2M_INSPECT_START_ACK",
            inner_id=INNER, status="OK", value=2.0),
        _ev(5.0, "COMM_MSG", name="V2M_INSPECT_END", inner_id=INNER,
            status="NG", value=1.0,
            extra=f"V3.0,TALOS1,NG,2,BOOT_DAMAGED,HEXA_MISS,{INNER},ProductID,1"),
        _ev(6.0, "COMM_MSG", name="V2M_INSPECT_END", inner_id=INNER,
            status="OK", value=2.0,
            extra=f"V3.0,TALOS1,OK,{INNER},ProductID,2"),
    ]
    insp = build_inspections(events, runs=[], dl_channels={}, gens=[],
                             log_end_ts=10000.0, comm_end_ts=10000.0)
    it = next(i for i in insp if i.inner_id == INNER)
    assert it.end_result == "NG"          # 한 존이라도 NG 면 NG 유지
    assert it.defects == ["BOOT_DAMAGED", "HEXA_MISS"]
    assert it.status == "complete"        # 전 존 END 수신 → 판정 자체는 완료


def test_fleet_index_render_contains_rows():
    """인덱스 렌더가 태그·NG 열을 포함한 HTML 을 만든다."""
    rows = [{"tag": "eq1_01", "total": 100, "complete": 98, "bad": 2,
             "lost": 1, "rejected": 1, "sim": 0, "gens": 3, "crash": 0,
             "errs": 5, "avg_dur": 12.34, "ng": 7}]
    html_doc = render(rows)
    assert "eq1_01" in html_doc
    assert "NG" in html_doc
    assert "12.3s" in html_doc
