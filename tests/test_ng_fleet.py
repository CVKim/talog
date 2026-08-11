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


def test_viewer_queries(tmp_path):
    """talog view 의 페이지/상세 쿼리 — SUMMARY/DETAIL 포맷 계약 검증."""
    import sqlite3
    from talog import store
    from talog.assemble import ChannelRun, Inspection
    from talog.viewer import query_insp, query_detail, query_meta
    db = str(tmp_path / "v.sqlite")
    con = store.open_db(db)
    insp = [Inspection(inner_id=f"I{i:03d}", product_id="P", start_ts=100.0 + i,
                       start_text=f"00:00:{i:02d}.000", end_ts=101.0 + i,
                       end_result="NG" if i % 2 else "OK", status="complete",
                       n_fed=2, n_done=2) for i in range(10)]
    con.executemany(
        "INSERT INTO inspections(inner_id,product_id,start_ts,start_text,"
        "wait_threads,ack_status,end_ts,end_text,end_result,status,duration_s,"
        "n_fed,n_done,n_lost,n_nofeed,n_skipped,n_zones,n_zones_done,defects,"
        "lost_channels,nofeed_channels,remain_list,gen_id,reject_zone) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(i.inner_id, i.product_id, i.start_ts, i.start_text, -1, "OK",
          i.end_ts, "", i.end_result, i.status, 1.0, i.n_fed, i.n_done,
          0, 0, 0, 0, 0, "", "1(CH_A)", "", "", 1, 0) for i in insp])
    con.execute(
        "INSERT INTO channel_runs(inner_id,alg_idx,channel,exec_no,feed_ts,"
        "feed_text,roi_idx,pre_ms,infer_start_ts,infer_end_ts,infer_ms,"
        "post_ms,model,status) VALUES('I003',1,'CH_A',1,103.0,'',0,5.0,"
        "103.1,103.5,400.0,0,'M1','done')")
    con.commit()
    r = query_insp(con, flt="ng", sort="asc", offset=0, limit=3)
    assert r["total"] == 5 and len(r["rows"]) == 3
    assert r["rows"][0][0] == "I001" and r["rows"][0][9] == "NG"
    d = query_detail(con, "I003")
    assert d["status"] == "complete" and len(d["runs"]) == 1
    assert d["runs"][0][0] == 1 and d["runs"][0][6] == "M1"
    assert d["lostIdx"] == [1]
    m = query_meta(con)
    assert m["total"] == 10
    con.close()
