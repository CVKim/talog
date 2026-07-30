"""존(그룹) 단위 완료 판정과 GPU 모니터 파서 테스트."""

from talog.assemble import build_inspections
from talog.events import Event, Extractor
from talog.watch import _parse_smi


def _ev(ts, kind, **kw):
    e = Event(ts=ts, ts_text=f"00:00:{int(ts):02d}.000", kind=kind)
    for k, v in kw.items():
        setattr(e, k, v)
    return e


INNER = "2000026073012345678"


def test_comm_enrich_extracts_zone_group():
    """END 페이로드의 마지막 토큰(1~2자리 숫자)이 그룹 번호로 추출된다."""
    ev = Event(ts=1.0, ts_text="t", kind="COMM_MSG", name="V2M_INSPECT_END",
               extra=f"V3.0,TALOS1,OK,{INNER},ProductID,3")
    Extractor._enrich_comm(ev)
    assert ev.inner_id == INNER
    assert ev.status == "OK"
    assert ev.value == 3.0


def test_partial_zone_completion_is_incomplete():
    """4존 중 2존만 END 를 받으면 complete 가 아니라 incomplete 로 판정된다."""
    events = [
        _ev(1.0, "INSP_START", inner_id=INNER, value=5),
    ]
    # 4개 존 ACK, 2개 존만 END
    for g in (1, 2, 3, 4):
        events.append(_ev(2.0 + g, "COMM_MSG", name="V2M_INSPECT_START_ACK",
                          inner_id=INNER, status="OK", value=float(g)))
    for g in (1, 2):
        events.append(_ev(10.0 + g, "COMM_MSG", name="V2M_INSPECT_END",
                          inner_id=INNER, status="OK", value=float(g)))
    insp = build_inspections(events, runs=[], dl_channels={}, gens=[],
                             log_end_ts=10000.0, comm_end_ts=10000.0)
    it = next(i for i in insp if i.inner_id == INNER)
    assert it.n_zones == 4
    assert it.n_zones_done == 2
    assert it.status == "incomplete"


def test_all_zones_done_is_complete():
    """모든 존이 END 를 받으면 complete."""
    events = [_ev(1.0, "INSP_START", inner_id=INNER, value=5)]
    for g in (1, 2):
        events.append(_ev(2.0 + g, "COMM_MSG", name="V2M_INSPECT_START_ACK",
                          inner_id=INNER, status="OK", value=float(g)))
        events.append(_ev(10.0 + g, "COMM_MSG", name="V2M_INSPECT_END",
                          inner_id=INNER, status="OK", value=float(g)))
    insp = build_inspections(events, runs=[], dl_channels={}, gens=[],
                             log_end_ts=10000.0, comm_end_ts=10000.0)
    it = next(i for i in insp if i.inner_id == INNER)
    assert it.status == "complete"
    assert it.n_zones == it.n_zones_done == 2


def test_single_zone_unaffected():
    """단일 존(PC3/CTR 형) 설비는 기존 판정이 그대로 유지된다."""
    events = [
        _ev(1.0, "INSP_START", inner_id=INNER, value=2),
        _ev(2.0, "COMM_MSG", name="V2M_INSPECT_START_ACK",
            inner_id=INNER, status="OK", value=1.0),
        _ev(9.0, "COMM_MSG", name="V2M_INSPECT_END",
            inner_id=INNER, status="OK", value=1.0),
    ]
    insp = build_inspections(events, runs=[], dl_channels={}, gens=[],
                             log_end_ts=10000.0, comm_end_ts=10000.0)
    assert insp[0].status == "complete"


def test_parse_smi_normal_and_garbage():
    """nvidia-smi CSV 파싱: 정상 2 GPU + 잡음 라인 무시."""
    text = ("0, 41, 3, 512, 45.10\n"
            "1, 87, 99, 9800, 310.55\n"
            "garbage line\n")
    gpus = _parse_smi(text)
    assert len(gpus) == 2
    assert gpus[0]["gpu"] == 0 and gpus[0]["temp"] == 41.0
    assert gpus[1]["temp"] == 87.0 and gpus[1]["mem"] == 9800.0
