"""assemble.py(검사 조립기) + diagnose.py(룰 기반 진단) 단위 테스트.

Event 객체를 직접 생성하여(파일 파싱 없이) 조립 규칙과 진단 룰을 검증한다.
외부 드라이브 데이터에 의존하지 않는다.
"""

from __future__ import annotations

from talog.assemble import (
    ChannelRun,
    ProcessGen,
    build_inspections,
    build_process_gens,
    build_runs_for_file,
)
from talog.diagnose import diagnose
from talog.events import Event


def _tt(ts: float) -> str:
    """테스트용 ts_text 생성기. ts 를 자정 기준 초로 보고 HH:MM:SS.fff 로 변환한다."""
    sec = int(ts)
    ms = int(round((ts - sec) * 1000))
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}.{ms:03d}"


def _ev(kind: str, ts: float, **kw) -> Event:
    """Event 를 간결하게 생성하는 헬퍼. ts_text 는 ts 로부터 자동 산출한다."""
    return Event(ts=ts, ts_text=_tt(ts), kind=kind, **kw)


# ---------------------------------------------------------------------------
# 1) build_runs_for_file: RESET -> BLOCK_START -> TACT_INFER 페어링 규칙
# ---------------------------------------------------------------------------
def test_build_runs_pairs_reset_start_infer_as_done():
    """RESET 직후(귀속 창 내) Infer 계열 BLOCK_START 가 같은 obj_id 의
    TACT_INFER 로 닫히면 status=done, infer_ms 가 기록되어야 한다."""
    evs = [
        _ev("RESET", 100.0, inner_id="INSP001", product_id="P1"),
        _ev("BLOCK_START", 101.0, block="InferPatchCore", obj_id="OBJ1"),
        _ev("TACT_INFER", 102.5, obj_id="OBJ1", value=250.0, model="modelA.trt"),
    ]
    runs = build_runs_for_file(alg_idx=3, channel="SW_CAM2", evs=evs)
    assert len(runs) == 1
    r = runs[0]
    assert r.inner_id == "INSP001"
    assert r.alg_idx == 3
    assert r.channel == "SW_CAM2"
    assert r.exec_no == 1
    assert r.status == "done"
    assert r.infer_ms == 250.0
    assert r.model == "modelA.trt"
    assert r.infer_start_ts == 101.0
    assert r.infer_end_ts == 102.5


def test_build_runs_start_without_infer_is_lost():
    """TACT_INFER 가 오지 않은 BLOCK_START 는 status=lost 로 남아야 한다."""
    evs = [
        _ev("RESET", 200.0, inner_id="INSP002"),
        _ev("BLOCK_START", 201.0, block="InferYolo", obj_id="OBJ2"),
        # TACT_INFER 없음 → 실행 중 소실
    ]
    runs = build_runs_for_file(alg_idx=1, channel="CAM1", evs=evs)
    assert len(runs) == 1
    assert runs[0].inner_id == "INSP002"
    assert runs[0].status == "lost"
    assert runs[0].infer_end_ts == 0.0


def test_build_runs_chains_second_exec_on_same_obj():
    """1차 실행 종료 후 5초 이내 같은 obj_id 의 재시작(BLOCK_START)은
    같은 검사의 2차 실행(exec_no=2)으로 연쇄 귀속되어야 한다."""
    evs = [
        _ev("RESET", 100.0, inner_id="INSP003"),
        _ev("BLOCK_START", 101.0, block="InferEff", obj_id="OBJ3"),
        _ev("TACT_INFER", 102.0, obj_id="OBJ3", value=300.0, model="m1.trt"),
        # 종료(102.0) 후 3초 뒤 같은 obj_id 재시작 → 2차 실행
        _ev("BLOCK_START", 105.0, block="InferEff", obj_id="OBJ3"),
        _ev("TACT_INFER", 106.0, obj_id="OBJ3", value=150.0, model="m2.trt"),
    ]
    runs = build_runs_for_file(alg_idx=2, channel="CAM2", evs=evs)
    assert len(runs) == 2
    first, second = runs
    assert first.exec_no == 1 and first.status == "done"
    assert second.exec_no == 2
    assert second.inner_id == "INSP003"      # 같은 검사에 귀속
    assert second.status == "done"
    assert second.infer_ms == 150.0


# ---------------------------------------------------------------------------
# 2) build_inspections: 상태 분류
# ---------------------------------------------------------------------------
def test_inspection_complete_with_comm_end():
    """INSP_START + COMM INSPECT_END 쌍이 있으면 complete 로 분류되어야 한다."""
    events = [
        _ev("INSP_START", 100.0, inner_id="A", product_id="PROD-A", value=3.0),
        _ev("COMM_MSG", 110.0, inner_id="A", name="V2M_INSPECT_END", status="OK"),
    ]
    out = build_inspections(events, runs=[], dl_channels={},
                            gens=[], log_end_ts=10000.0, comm_end_ts=10000.0)
    assert len(out) == 1
    it = out[0]
    assert it.inner_id == "A"
    assert it.status == "complete"
    assert it.end_result == "OK"
    assert it.wait_threads == 3


def test_inspection_rejected_by_noinspthread():
    """INSP_START 직후 INSP_REJECT(ack NoInspThread)가 오면 rejected 로
    분류되어야 한다."""
    events = [
        _ev("INSP_START", 200.0, inner_id="B", product_id="PROD-B", value=0.0),
        _ev("INSP_REJECT", 200.5),
    ]
    out = build_inspections(events, runs=[], dl_channels={},
                            gens=[], log_end_ts=10000.0, comm_end_ts=10000.0)
    assert len(out) == 1
    assert out[0].ack_status == "NoInspThread"
    assert out[0].status == "rejected"


def test_inspection_incomplete_lost_when_run_lost():
    """lost 상태의 run 이 존재하면 incomplete_lost 로 분류되고,
    소실 채널 목록이 채워져야 한다."""
    events = [
        _ev("INSP_START", 100.0, inner_id="C", product_id="PROD-C", value=2.0),
    ]
    runs = [ChannelRun(inner_id="C", alg_idx=1, channel="ch1", status="lost",
                       feed_ts=101.0, feed_text=_tt(101.0), infer_start_ts=101.0)]
    out = build_inspections(events, runs=runs, dl_channels={1: "ch1"},
                            gens=[], log_end_ts=10000.0, comm_end_ts=10000.0)
    assert len(out) == 1
    it = out[0]
    assert it.status == "incomplete_lost"
    assert it.n_lost == 1
    assert it.lost_channels == ["1(ch1)"]


def test_inspection_sim_complete_seeded_from_runs():
    """INSP_START 없이 run 만 존재하는 검사는 런에서 시드되고, 전 채널 완료 시
    sim_complete 로 분류되어야 한다(설비 신호 없는 시뮬레이션 실행)."""
    runs = [ChannelRun(inner_id="D", alg_idx=2, channel="ch2", status="done",
                       feed_ts=50.0, feed_text=_tt(50.0),
                       infer_start_ts=50.5, infer_end_ts=51.0, infer_ms=500.0)]
    out = build_inspections([], runs=runs, dl_channels={2: "ch2"},
                            gens=[], log_end_ts=10000.0, comm_end_ts=10000.0)
    assert len(out) == 1
    it = out[0]
    assert it.status == "sim_complete"
    assert it.start_ts == 50.0               # 첫 run 의 feed_ts 로 시드
    assert it.wait_threads == -1             # 설비 시작 신호 없음


def test_inspection_in_progress_eof_beyond_comm_coverage():
    """comm 로그 절단 시각 이후에 시작되고 END 가 없는 검사는 미완료가 아니라
    in_progress_eof(판정 불가)로 분류되어야 한다."""
    events = [
        _ev("INSP_START", 900.0, inner_id="E", product_id="PROD-E", value=1.0),
    ]
    out = build_inspections(events, runs=[], dl_channels={},
                            gens=[], log_end_ts=1100.0, comm_end_ts=1000.0)
    assert len(out) == 1
    assert out[0].status == "in_progress_eof"


def test_inspection_incomplete_within_comm_coverage():
    """comm 커버리지 내에서 시작했는데 END 가 없으면 incomplete 로
    분류되어야 한다."""
    events = [
        _ev("INSP_START", 100.0, inner_id="F", product_id="PROD-F", value=1.0),
    ]
    runs = [ChannelRun(inner_id="F", alg_idx=1, channel="ch1", status="done",
                       feed_ts=101.0, feed_text=_tt(101.0),
                       infer_start_ts=101.0, infer_end_ts=102.0, infer_ms=300.0)]
    out = build_inspections(events, runs=runs, dl_channels={1: "ch1"},
                            gens=[], log_end_ts=10000.0, comm_end_ts=10000.0)
    assert len(out) == 1
    assert out[0].status == "incomplete"


def test_comm_msg_does_not_create_ghost_inspection():
    """INSPECT_END 등 comm 메시지는 기존 검사에만 적용되어야 하며, 스티칭된
    익일 메시지가 유령 검사를 새로 만들면 안 된다."""
    events = [
        _ev("INSP_START", 100.0, inner_id="A", product_id="PROD-A", value=3.0),
        _ev("COMM_MSG", 110.0, inner_id="A", name="V2M_INSPECT_END", status="OK"),
        # 대응되는 검사가 없는 END 메시지 → 새 항목을 만들면 안 된다
        _ev("COMM_MSG", 120.0, inner_id="9999999999999999",
            name="V2M_INSPECT_END", status="OK"),
    ]
    out = build_inspections(events, runs=[], dl_channels={},
                            gens=[], log_end_ts=10000.0, comm_end_ts=10000.0)
    inners = [i.inner_id for i in out]
    assert "9999999999999999" not in inners
    assert inners == ["A"]


# ---------------------------------------------------------------------------
# 3) build_process_gens: 프로세스 세대 복원
# ---------------------------------------------------------------------------
def test_process_gens_sequence_and_end_causes():
    """create/destroy/create/kill/create 시퀀스에서 세대 3개가 복원되고,
    종료 원인이 각각 destroy / kill / eof 여야 한다."""
    events = [
        _ev("POOL_CREATE", 100.0),
        _ev("POOL_DESTROY", 200.0),
        _ev("POOL_CREATE", 210.0),
        _ev("BATCH", 300.0, name="talos_kill.bat"),
        _ev("POOL_CREATE", 310.0),
    ]
    gens = build_process_gens(events, log_end_ts=400.0)
    assert len(gens) == 3
    assert [g.end_cause for g in gens] == ["destroy", "kill", "eof"]
    assert gens[0].start_ts == 100.0 and gens[0].end_ts == 200.0
    assert gens[1].end_ts == 300.0
    assert gens[2].end_ts == 400.0           # 로그 끝까지 가동


def test_process_gens_seeds_generation_running_since_previous_day():
    """첫 POOL_CREATE 가 로그 시작보다 60초 이상 늦으면 전일부터 가동 중이던
    세대가 시드되어야 한다."""
    events = [
        _ev("RESET", 10.0, inner_id="X"),     # 로그 첫 이벤트(세대 마크 아님)
        _ev("POOL_CREATE", 200.0),
    ]
    gens = build_process_gens(events, log_end_ts=500.0)
    assert len(gens) == 2
    assert gens[0].start_ts == 10.0
    assert gens[0].start_text == "(전일부터 가동)"
    assert gens[0].end_ts == 200.0
    assert gens[0].end_cause == "unknown"    # create 로 대체된 시드 세대
    assert gens[1].end_cause == "eof"


# ---------------------------------------------------------------------------
# 4) diagnose: 룰 기반 소견
# ---------------------------------------------------------------------------
def test_diagnose_incomplete_lost_correlated_with_kill():
    """incomplete_lost 검사와 시각이 근접한 kill 세대가 있으면 '검사 미완료'
    crit 소견에 kill 상관 문구가 포함되어야 한다."""
    from talog.assemble import Inspection
    insp = Inspection(inner_id="2000026072709204650", start_ts=1000.0,
                      start_text=_tt(1000.0), wait_threads=2,
                      status="incomplete_lost", n_lost=1,
                      lost_channels=["3(SW_CAM2)"])
    kill_gen = ProcessGen(gen_id=1, start_ts=0.0, start_text=_tt(0.0),
                          end_ts=1400.0, end_text=_tt(1400.0), end_cause="kill")
    findings = diagnose([insp], runs=[], gens=[kill_gen], events=[],
                        model_loads=[], log_start=0.0, log_end=2000.0)
    f = next(f for f in findings if "검사 미완료" in f.title)
    assert f.severity == "crit"
    joined = " ".join(f.evidence)
    assert "kill과 시간 일치" in joined
    assert "SW_CAM2" in joined


def test_diagnose_gpu_contention_warn():
    """DLINFER_EXEC 분포에서 p95/p10 >= 4 (그리고 p95 >= 200ms)이면
    GPU 경합 warn 소견이 나와야 한다."""
    events = []
    # 한산 구간 30건(50ms) + 혼잡 구간 10건(400ms) → p10=50, p95=400 (8배)
    for i in range(30):
        events.append(_ev("DLINFER_EXEC", 100.0 + i, model="netA.trt", value=50.0))
    for i in range(10):
        events.append(_ev("DLINFER_EXEC", 200.0 + i, model="netA.trt", value=400.0))
    findings = diagnose([], runs=[], gens=[], events=events,
                        model_loads=[], log_start=0.0, log_end=1000.0)
    f = next(f for f in findings if "GPU 경합" in f.title)
    assert f.severity == "warn"
    assert any("netA" in line for line in f.evidence)


def test_diagnose_model_fail_crit():
    """MODEL_FAIL 이벤트가 있으면 모델 로드 실패 crit 소견이 나와야 한다."""
    events = [
        _ev("MODEL_FAIL", 300.0, model="DLMODEL0005"),
        _ev("MODEL_FAIL", 305.0, model="DLMODEL0005"),
    ]
    findings = diagnose([], runs=[], gens=[], events=events,
                        model_loads=[], log_start=0.0, log_end=1000.0)
    f = next(f for f in findings if "모델 로드 실패" in f.title)
    assert f.severity == "crit"
    assert "DLMODEL0005" in f.title
    assert "2건" in f.title


def test_no_machine_signal_near_eof_is_not_simulation():
    """설비 신호 파일(comm/InspStarter)이 먼저 잘린 꼬리 구간의 검사는 신호가
    '없는 게 아니라 잘린' 것이므로 시뮬레이션이 아니라 in_progress_eof 로
    분류되어야 한다 (Tenneco 30 실사고: 양산 191건이 sim_partial 오분류)."""
    runs = [ChannelRun(inner_id="T", alg_idx=2, channel="ch2", status="done",
                       feed_ts=950.0, feed_text=_tt(950.0),
                       infer_start_ts=950.0, infer_end_ts=951.0,
                       infer_ms=300.0)]
    out = build_inspections([], runs=runs, dl_channels={2: "ch2"},
                            gens=[], log_end_ts=1100.0, comm_end_ts=1000.0)
    assert len(out) == 1
    assert out[0].status == "in_progress_eof"


def test_no_machine_signal_within_coverage_stays_simulation():
    """comm 커버리지 안쪽(절단과 무관)에서 설비 신호 없이 돈 검사는 여전히
    시뮬레이션으로 분류되어야 한다 (PC3 새벽 시뮬 케이스 보존)."""
    runs = [ChannelRun(inner_id="S", alg_idx=2, channel="ch2", status="done",
                       feed_ts=100.0, feed_text=_tt(100.0),
                       infer_start_ts=100.0, infer_end_ts=101.0,
                       infer_ms=300.0)]
    out = build_inspections([], runs=runs, dl_channels={2: "ch2"},
                            gens=[], log_end_ts=10000.0, comm_end_ts=10000.0)
    assert len(out) == 1
    assert out[0].status == "sim_complete"


def test_tact_close_mode_builds_runs_without_block_start():
    """Execute 시작(Info) 라인이 없는 빌드(Tenneco)에서는 Debug Tact 라인이
    실행 전체를 대표한다 — run 이 즉석 합성되어야 한다 (감사 A: channel_runs
    0건 실사고). 멀티모델 채널은 같은 RESET 에 exec_no 가 증가한다."""
    evs = [
        _ev("RESET", 100.0, inner_id="X", product_id="P"),
        _ev("FEED", 100.1, roi_idx=7),
        _ev("TACT_INFER", 100.5, model="OD_A", value=400.0),
        _ev("TACT_INFER", 100.9, model="OD_B", value=350.0),
        _ev("RESET", 105.0, inner_id="Y", product_id="P"),
        _ev("FEED", 105.1, roi_idx=7),
        _ev("TACT_INFER", 105.6, model="OD_A", value=410.0),
    ]
    runs = build_runs_for_file(1, "OD", evs)
    done = [r for r in runs if r.status == "done"]
    assert len(done) == 3
    assert [r.inner_id for r in done] == ["X", "X", "Y"]
    assert [r.exec_no for r in done] == [1, 2, 1]
    assert done[0].infer_ms == 400.0
    assert done[0].roi_idx == 7


def test_tact_close_mode_synthesizes_lost_run():
    """투입(FEED)됐으나 Tact 로 닫히지 못한 검사(재기동 경계)는 lost run 으로
    합성되어야 한다 (Tenneco 30 실측: RESET 28,494 vs TACT 28,491)."""
    evs = [
        _ev("RESET", 100.0, inner_id="X", product_id="P"),
        _ev("FEED", 100.1, roi_idx=3),
        # Tact 없이 파일 종료 (크래시/재기동)
    ]
    runs = build_runs_for_file(1, "OD", evs)
    assert len(runs) == 1
    assert runs[0].status == "lost"
    assert runs[0].inner_id == "X"


def test_multizone_first_start_preserved_duration():
    """다존 설비: 같은 inner 로 START 가 존마다 반복 수신돼도 검사 시작은
    첫 START 를 유지해야 한다 (감사 B: 검사시간 9.8s → 0.61s 왜곡 실사고)."""
    events = [
        _ev("INSP_START", 100.0, inner_id="Z", product_id="P", value=3.0),
        _ev("COMM_MSG", 100.1, inner_id="Z", name="V2M_INSPECT_START_ACK",
            status="OK", value=1.0),
        _ev("COMM_MSG", 101.0, inner_id="Z", name="V2M_INSPECT_END",
            status="OK", value=1.0),
        _ev("INSP_START", 103.0, inner_id="Z", product_id="P", value=2.0),
        _ev("COMM_MSG", 103.1, inner_id="Z", name="V2M_INSPECT_START_ACK",
            status="OK", value=2.0),
        _ev("COMM_MSG", 104.0, inner_id="Z", name="V2M_INSPECT_END",
            status="OK", value=2.0),
    ]
    out = build_inspections(events, runs=[], dl_channels={},
                            gens=[], log_end_ts=10000.0, comm_end_ts=10000.0)
    assert len(out) == 1
    it = out[0]
    assert it.start_ts == 100.0            # 첫 존 START 유지
    assert it.end_ts == 104.0              # 마지막 존 END
    assert it.wait_threads == 2            # 최저 잔여 스레드 기록
    assert it.status == "complete"


def test_zone_partial_without_any_end_is_incomplete():
    """ACK 존만 있고 END 가 0건인 다존 검사도 존 부분 완료(incomplete)로
    분류되어야 한다 (감사 D: end_ts 요구 조건이 존 정보를 우회시키던 버그)."""
    events = [
        _ev("INSP_START", 100.0, inner_id="W", product_id="P", value=3.0),
        _ev("COMM_MSG", 100.1, inner_id="W", name="V2M_INSPECT_START_ACK",
            status="OK", value=1.0),
        _ev("COMM_MSG", 100.2, inner_id="W", name="V2M_INSPECT_START_ACK",
            status="OK", value=2.0),
    ]
    out = build_inspections(events, runs=[], dl_channels={},
                            gens=[], log_end_ts=10000.0, comm_end_ts=10000.0)
    assert len(out) == 1
    assert out[0].status == "incomplete"
    assert out[0].n_zones == 2 and out[0].n_zones_done == 0
