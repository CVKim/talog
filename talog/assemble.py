"""검사 조립기 + 진단.

이벤트 스트림을 inner_id 단위 '검사'와 채널 단위 '실행(run)'으로 조립하고,
미완료 사유(미투입 / 실행 중 소실 / 시작 거부)를 분류한다.

PC3 0727 RCA 에서 검증된 귀속 규칙을 사용한다:
  - RESET 직후(수 초 내)의 Infer 계열 BLOCK_START = 해당 검사의 1차 실행
  - 같은 obj_id(인스턴스 주소)에서 TACT_INFER 가 닫는다
  - 종료 직후 같은 obj_id 의 재시작(BLOCK_START) = 같은 검사의 2차 실행(다중 모델 채널)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .events import Event

_ATTACH_WINDOW = 10.0     # RESET -> 첫 인퍼런스 시작 귀속 허용 시간(초)
_CHAIN_WINDOW = 5.0       # 1차 종료 -> 2차 시작 연쇄 귀속 허용 시간(초)


@dataclass(slots=True)
class ChannelRun:
    inner_id: str
    alg_idx: int
    channel: str
    exec_no: int = 1
    feed_ts: float = 0.0
    feed_text: str = ""
    roi_idx: int = 0
    pre_ms: float = 0.0
    infer_start_ts: float = 0.0
    infer_end_ts: float = 0.0
    infer_ms: float = 0.0
    post_ms: float = 0.0
    model: str = ""
    status: str = "done"   # done / lost / no_infer


@dataclass
class Inspection:
    inner_id: str
    product_id: str = ""
    start_ts: float = 0.0
    start_text: str = ""
    wait_threads: int = -1
    ack_status: str = ""
    end_ts: float = 0.0
    end_text: str = ""
    end_result: str = ""
    status: str = ""
    n_fed: int = 0
    n_done: int = 0
    n_lost: int = 0
    n_nofeed: int = 0
    n_skipped: int = 0            # 종속성 그래프 DEACTIVATE 에 의한 정상 스킵
    n_zones: int = 0              # 시작 승인(ACK)된 그룹(존) 수 — Tenneco 등
    n_zones_done: int = 0         # END 를 받은 그룹(존) 수
    timed_out: bool = False       # [TIMEOUT] 으로 판정 미송신 (코드 검증 근거)
    reject_zone: int = 0          # 거부 시점의 투입 존(groupId) — 0=미상
    defects: list[str] = field(default_factory=list)   # NG 판정 결함명 목록
    lost_channels: list[str] = field(default_factory=list)
    nofeed_channels: list[str] = field(default_factory=list)
    lost_idx: list[int] = field(default_factory=list)      # 그래프 색칠용 인덱스
    nofeed_idx: list[int] = field(default_factory=list)
    skipped_idx: list[int] = field(default_factory=list)
    remain_list: str = ""
    gen_id: int = 0


@dataclass(slots=True)
class ProcessGen:
    gen_id: int
    start_ts: float
    start_text: str
    end_ts: float = 0.0
    end_text: str = ""
    end_cause: str = ""    # destroy / crash / kill / eof


@dataclass(slots=True)
class ModelLoad:
    kind: str              # recipe_load(설비 명령) / channel_init(채널 모델 로드)
    ts: float
    ts_text: str
    name: str = ""         # 레시피명 또는 채널명
    alg_idx: int = 0
    dur_s: float = 0.0
    status: str = ""       # OK / FAIL / (빈값=완료 로그 없음)


def build_model_loads(events: list[Event],
                      alg_names: dict[int, str]) -> list[ModelLoad]:
    """모델 로드 이력: comm 의 MODEL_LOAD 명령/응답 쌍 + 채널별 Initialize 쌍."""
    out: list[ModelLoad] = []
    # 1) 설비 레시피 로드 명령 (M2V_MODEL_LOAD* -> V2M_MODEL_LOAD_ACK)
    pending_req: Optional[Event] = None
    for e in events:
        if e.kind != "COMM_MSG" or "MODEL_LOAD" not in e.name:
            continue
        if e.name.startswith("M2V_MODEL_LOAD"):
            pending_req = e
        elif e.name == "V2M_MODEL_LOAD_ACK":
            toks = [t.strip() for t in e.extra.split(",")]
            status = next((t for t in toks if t in ("OK", "NG", "FAIL")), "")
            rname = toks[-1] if toks else ""
            if pending_req is not None:
                out.append(ModelLoad(kind="recipe_load", ts=pending_req.ts,
                                     ts_text=pending_req.ts_text, name=rname,
                                     dur_s=round(e.ts - pending_req.ts, 1),
                                     status=status))
                pending_req = None
            else:
                out.append(ModelLoad(kind="recipe_load", ts=e.ts, ts_text=e.ts_text,
                                     name=rname, status=status))
    if pending_req is not None:
        out.append(ModelLoad(kind="recipe_load", ts=pending_req.ts,
                             ts_text=pending_req.ts_text,
                             name=pending_req.extra.split(",")[-1].strip(),
                             status="응답 없음"))
    # 2) 채널별 DeepLearningInspector Initialize 쌍 (obj_id 로 페어링)
    open_init: dict[tuple[int, str], Event] = {}
    for e in events:
        if e.kind == "INIT_START":
            open_init[(e.alg_idx, e.obj_id)] = e
        elif e.kind == "INIT_END":
            st = open_init.pop((e.alg_idx, e.obj_id), None)
            if st is not None:
                out.append(ModelLoad(kind="channel_init", ts=st.ts, ts_text=st.ts_text,
                                     name=alg_names.get(e.alg_idx, str(e.alg_idx)),
                                     alg_idx=e.alg_idx,
                                     dur_s=round(e.ts - st.ts, 1), status="OK"))
    for (idx, _obj), st in open_init.items():
        out.append(ModelLoad(kind="channel_init", ts=st.ts, ts_text=st.ts_text,
                             name=alg_names.get(idx, str(idx)), alg_idx=idx,
                             status="미완료"))
    out.sort(key=lambda m: m.ts)
    return out


def _parse_ng_defects(extra: str, inner_id: str,
                      decision: str = "NG") -> list[str]:
    """INSPECT_END NG/REWORK 페이로드에서 결함명 목록을 뽑는다.

    형식(CommSender.cpp 검증):
        V3.0,<국>,<판정>[,<개수>,<결함1>,..,<결함N>],<inner>,<product>,<존>
    """
    toks = [t.strip() for t in (extra or "").split(",")]
    try:
        ng_i = toks.index(decision)
        inner_i = toks.index(inner_id)
    except ValueError:
        return []
    start = ng_i + 1
    if start < len(toks) and toks[start].isdigit():
        start += 1                      # <개수> 토큰 건너뛰기
    return [t for t in toks[start:inner_i] if t and not t.isdigit()]


# ---------------------------------------------------------------------------
def build_process_gens(events: list[Event], log_end_ts: float) -> list[ProcessGen]:
    """POOL_CREATE/POOL_DESTROY/CRASH/BATCH(kill/start) 로 프로세스 세대를 복원한다."""
    marks: list[tuple[float, str, str]] = []
    for e in events:
        if e.kind == "POOL_CREATE":
            marks.append((e.ts, "create", e.ts_text))
        elif e.kind == "POOL_DESTROY":
            marks.append((e.ts, "destroy", e.ts_text))
        elif e.kind == "CRASH":
            marks.append((e.ts, "crash", e.ts_text))
        elif e.kind == "BATCH" and "kill" in e.name.lower():
            marks.append((e.ts, "kill", e.ts_text))
    marks.sort()
    # 풀 5종 일반화(eFunction/eDraw/...)로 기동 1회에 create/destroy 마커가
    # 여러 개 생긴다 — 30초 내 같은 종류 연속 마커는 한 번의 기동/정지로 병합
    dedup: list[tuple[float, str, str]] = []
    for m in marks:
        if dedup and m[1] == dedup[-1][1] and m[1] in ("create", "destroy") \
                and m[0] - dedup[-1][0] < 30:
            continue
        dedup.append(m)
    marks = dedup
    gens: list[ProcessGen] = []
    cur: Optional[ProcessGen] = None
    # 첫 create 이전 구간: 전일부터 가동 중이던 세대로 시드한다
    first_create = next((m[0] for m in marks if m[1] == "create"), None)
    log_first = min((e.ts for e in events), default=0.0)
    if log_first and (first_create is None or first_create - log_first > 60):
        cur = ProcessGen(gen_id=1, start_ts=log_first, start_text="(전일부터 가동)")
        gens.append(cur)
    for ts, what, text in marks:
        if what == "create":
            if cur is not None and not cur.end_ts:
                cur.end_ts, cur.end_text, cur.end_cause = ts, text, cur.end_cause or "unknown"
            cur = ProcessGen(gen_id=len(gens) + 1, start_ts=ts, start_text=text)
            gens.append(cur)
        else:
            if cur is not None and not cur.end_ts:
                cur.end_ts, cur.end_text, cur.end_cause = ts, text, what
    if cur is not None and not cur.end_ts:
        cur.end_ts, cur.end_text, cur.end_cause = log_end_ts, "", "eof"
    return gens


# ---------------------------------------------------------------------------
def build_runs_for_file(alg_idx: int, channel: str,
                        evs: list[Event]) -> list[ChannelRun]:
    """alg 파일 하나의 이벤트를 실행(run) 단위로 조립한다.

    고케이던스 사이트(일 10만+ 검사)에서 메모리를 아끼기 위해 파일 단위로
    즉시 조립하고 원본 이벤트는 호출부에서 해제한다.
    """
    runs: list[ChannelRun] = []
    if True:  # (구조 유지 블록 — 별도 함수 분리 예정)
        last_reset: Optional[Event] = None
        last_reset_ts_any = 0.0      # 연쇄(2차 실행) 귀속 오염 방지용
        n_since_reset = 0            # tact-close 모드: 이번 검사에서 닫힌 run 수
        cur_feed: dict[str, float | str | int] = {}
        open_by_obj: dict[str, ChannelRun] = {}      # obj_id -> 진행 중 run
        chain_by_obj: dict[str, tuple[str, float]] = {}  # obj_id -> (inner, 직전 종료 ts)

        def _synth_lost():
            # tact-close 모드에서 투입(FEED)됐으나 Tact 로 닫히지 못한 검사
            # = 실행 중 소실 (Tenneco 30 실측: RESET 28,494 vs TACT 28,491)
            if last_reset is not None and cur_feed and n_since_reset == 0:
                runs.append(ChannelRun(
                    inner_id=last_reset.inner_id, alg_idx=alg_idx,
                    channel=channel, feed_ts=last_reset.ts,
                    feed_text=last_reset.ts_text,
                    roi_idx=int(cur_feed.get("roi", 0) or 0),
                    infer_start_ts=last_reset.ts, status="lost"))

        for e in evs:
            if e.kind == "RESET":
                _synth_lost()
                last_reset = e
                last_reset_ts_any = e.ts
                n_since_reset = 0
                cur_feed = {}
            elif e.kind == "FEED" and last_reset is not None:
                cur_feed = {"roi": e.roi_idx}
            elif e.kind == "TACT_PRE" and last_reset is not None \
                    and e.ts - last_reset.ts <= _ATTACH_WINDOW:
                cur_feed["pre"] = e.value
            elif e.kind == "BLOCK_START" and e.block.startswith("Infer"):
                inner, exec_no = "", 1
                if last_reset is not None and e.ts - last_reset.ts <= _ATTACH_WINDOW:
                    inner = last_reset.inner_id
                    last_reset = None                # 1회 귀속 후 소비
                else:
                    ch = chain_by_obj.get(e.obj_id)
                    # 직전 종료 이후 새 RESET 이 끼면 이 시작은 새 검사의 것일
                    # 수 있으므로 연쇄 귀속을 포기한다 (오귀속 방지)
                    if ch and e.ts - ch[1] <= _CHAIN_WINDOW \
                            and last_reset_ts_any <= ch[1]:
                        inner = ch[0]
                        exec_no = 2
                if not inner:
                    continue                          # 귀속 불가 실행은 통계에서 제외
                run = ChannelRun(inner_id=inner, alg_idx=alg_idx, channel=channel,
                                 exec_no=exec_no, infer_start_ts=e.ts,
                                 feed_ts=e.ts, feed_text=e.ts_text,
                                 roi_idx=int(cur_feed.get("roi", 0) or 0),
                                 pre_ms=float(cur_feed.get("pre", 0) or 0),
                                 status="lost")
                open_by_obj[e.obj_id] = run
                runs.append(run)
            elif e.kind == "TACT_INFER":
                run = open_by_obj.pop(e.obj_id, None)
                if run is None:
                    # tact-close 모드: Execute 시작(Info) 라인을 남기지 않는
                    # 빌드(Tenneco 계열)는 Debug Tact 라인 1줄이 실행 전체를
                    # 대표한다 → 즉석에서 완료 run 을 합성한다. 멀티모델
                    # 채널은 같은 RESET 에 여러 Tact 가 이어지므로 last_reset
                    # 을 소비하지 않고 exec_no 만 증가시킨다.
                    inner, exec_no = "", 1
                    if last_reset is not None \
                            and e.ts - last_reset.ts <= _ATTACH_WINDOW:
                        inner = last_reset.inner_id
                        n_since_reset += 1
                        exec_no = n_since_reset
                    else:
                        ch = chain_by_obj.get(e.obj_id)
                        if ch and e.ts - ch[1] <= _CHAIN_WINDOW \
                                and last_reset_ts_any <= ch[1]:
                            inner, exec_no = ch[0], 2
                    if inner:
                        st = e.ts - (e.value or 0) / 1000.0
                        run = ChannelRun(
                            inner_id=inner, alg_idx=alg_idx, channel=channel,
                            exec_no=exec_no, infer_start_ts=st, feed_ts=st,
                            feed_text=e.ts_text,
                            roi_idx=int(cur_feed.get("roi", 0) or 0),
                            pre_ms=float(cur_feed.get("pre", 0) or 0))
                        runs.append(run)
                if run is not None:
                    run.infer_end_ts = e.ts
                    run.infer_ms = e.value
                    run.model = e.model
                    run.status = "done"
                    chain_by_obj[e.obj_id] = (run.inner_id, e.ts)
            elif e.kind == "TACT_POST":
                # 후처리 블록(DetectAlg 등)은 별도 인스턴스이므로 obj_id 로 짝을 맞출 수
                # 없다 → 같은 파일에서 직전에 완료된 run 에 시간 근접으로 귀속한다.
                # 대량 이벤트에서 O(n^2) 방지를 위해 역방향 탐색을 상한한다.
                for r in list(reversed(runs))[:50]:
                    if r.alg_idx == alg_idx and r.status == "done" \
                            and 0 <= e.ts - r.infer_end_ts <= 10.0:
                        r.post_ms = e.value
                        break
        _synth_lost()      # 파일 말미(EOF)에서 열린 채 끝난 검사
    return runs


# ---------------------------------------------------------------------------
def build_inspections(events: list[Event], runs: list[ChannelRun],
                      dl_channels: dict[int, str], gens: list[ProcessGen],
                      log_end_ts: float, comm_end_ts: float = 0.0) -> list[Inspection]:
    insp: dict[str, Inspection] = {}

    # 종속성 그래프의 검사별 비활성 alg 집합 (정상 스킵 판정)
    deact: dict[str, set[int]] = {}
    for e in events:
        if e.kind == "ALG_DEACT" and e.inner_id:
            deact.setdefault(e.inner_id, set()).add(int(e.value))

    def get(inner: str) -> Inspection:
        if inner not in insp:
            insp[inner] = Inspection(inner_id=inner)
        return insp[inner]

    last_start: Optional[Inspection] = None
    ack_groups: dict[str, set] = {}      # inner -> ACK 받은 그룹(존) 집합
    end_groups: dict[str, set] = {}      # inner -> END 받은 그룹(존) 집합
    last_recv: Optional[Event] = None    # 직전 M2V_INSPECT_START 수신 (존 포함)
    for e in events:
        if e.kind == "INSP_START":
            it = get(e.inner_id)
            if not it.start_ts:
                # 다존 설비(Tenneco 4존)는 같은 inner 로 START 가 존마다
                # 반복 수신된다 — 첫 존 시작 = 검사 시작 (덮어쓰면 검사시간이
                # 마지막 존 사이클로 왜곡: 30일 실측 9.8s → 0.61s 오류)
                it.start_ts, it.start_text = e.ts, e.ts_text
                it.product_id = e.product_id
                it.wait_threads = int(e.value)
            else:
                # 최저 잔여 스레드는 고갈 진단용으로 유지
                it.wait_threads = min(it.wait_threads, int(e.value))
            last_start = it
        elif e.kind == "INSP_RECV":
            last_recv = e
        elif e.kind == "INSP_REJECT":
            # comm.log 부재 시에도 거부를 식별한다 (직전 INSP_START 에 귀속)
            if last_start is not None and not last_start.ack_status:
                last_start.ack_status = "NoInspThread"
            # 거부가 어느 존 투입 시점이었는지 (직전 수신 라인의 groupId)
            if last_start is not None and last_recv is not None \
                    and last_recv.inner_id == last_start.inner_id \
                    and abs(e.ts - last_recv.ts) <= 2.0:
                last_start.reject_zone = int(last_recv.value)
        elif e.kind in ("REJECT_BUSYCAM", "REJECT_NOTREADY", "REJECT_SIM"):
            # 코드 검증된 추가 거부 경로 (InspStarter.cpp)
            if last_start is not None and not last_start.ack_status:
                last_start.ack_status = {
                    "REJECT_BUSYCAM": "BusyCam",
                    "REJECT_NOTREADY": "NotModelLoaded",
                    "REJECT_SIM": "SimulationModelLoaded",
                }[e.kind]
        elif e.kind == "IMG_TIMEOUT" and e.inner_id:
            it = insp.get(e.inner_id)
            if it is not None:
                it.timed_out = True
        elif e.kind == "SEQ_GROUP" and e.inner_id and e.inner_id not in insp:
            # InspStarter 신호가 없는 실행(시뮬레이션 등)을 seq 에서 시드한다
            it = get(e.inner_id)
            it.start_ts, it.start_text = e.ts, e.ts_text
        elif e.kind == "COMM_MSG" and e.inner_id:
            # comm 메시지는 새 검사 항목을 만들지 않는다
            # (스티칭된 익일 메시지가 유령 검사를 만드는 것을 방지)
            it = insp.get(e.inner_id)
            if it is None and "INSPECT_START_ACK" in e.name:
                it = get(e.inner_id)
                it.start_ts, it.start_text = e.ts, e.ts_text
            if it is None:
                continue
            if "INSPECT_START_ACK" in e.name:
                it.ack_status = e.status
                if e.status == "OK" and e.value:
                    ack_groups.setdefault(e.inner_id, set()).add(int(e.value))
            elif "INSPECT_END" in e.name:
                it.end_ts, it.end_text = e.ts, e.ts_text
                # 다존 설비: 어느 한 존이라도 NG 면 최종 판정은 NG 로 유지한다
                # (Tenneco 30 확증: 부품 단위 종합판정 메시지 부재, PLC 가
                #  존 OR 로 배출 판정 — 존 스티키가 유일한 올바른 집계)
                if it.end_result != "NG":
                    it.end_result = e.status
                if e.status in ("NG", "REWORK"):
                    for d in _parse_ng_defects(e.extra, e.inner_id, e.status):
                        if d not in it.defects:
                            it.defects.append(d)
                if e.value:
                    end_groups.setdefault(e.inner_id, set()).add(int(e.value))
        elif e.kind == "REMAIN" and e.inner_id:
            get(e.inner_id).remain_list = e.alg_list
        elif e.kind == "RESET" and e.inner_id and e.inner_id not in insp:
            # InspStarter 로그가 없는 경우의 방어적 시드
            it = get(e.inner_id)
            it.start_ts, it.start_text = e.ts, e.ts_text
            it.product_id = e.product_id

    # 채널 런 집계
    by_inner: dict[str, list[ChannelRun]] = {}
    for r in runs:
        by_inner.setdefault(r.inner_id, []).append(r)

    # 설비 신호 없이 실행된 검사(시뮬레이션 등)는 런에서 시드한다
    for inner, rs in by_inner.items():
        if inner not in insp:
            it = get(inner)
            first = min(rs, key=lambda r: r.feed_ts or r.infer_start_ts)
            it.start_ts = first.feed_ts or first.infer_start_ts
            it.start_text = first.feed_text

    for it in insp.values():
        rs = by_inner.get(it.inner_id, [])
        fed_alg = {r.alg_idx for r in rs}
        done_alg = {r.alg_idx for r in rs if r.status == "done"}
        lost = sorted({r.alg_idx for r in rs if r.status == "lost"})
        it.n_fed = len(fed_alg)
        it.n_done = len(done_alg)
        it.n_lost = len(lost)
        it.lost_idx = list(lost)
        it.lost_channels = [f"{a}({dl_channels.get(a, '?')})" for a in lost]
        if it.ack_status == "OK" or (it.ack_status == "" and fed_alg):
            nofeed = set(dl_channels) - fed_alg
            # 종속성 그래프에서 비활성된 alg 는 '정상 스킵'으로 분리한다
            skipped = nofeed & deact.get(it.inner_id, set())
            missing = sorted(nofeed - skipped)
            it.n_skipped = len(skipped)
            it.n_nofeed = len(missing)
            it.skipped_idx = sorted(skipped)
            it.nofeed_idx = missing
            it.nofeed_channels = [f"{a}({dl_channels.get(a, '?')})" for a in missing]

        # 세대 매핑
        for g in gens:
            if g.start_ts <= it.start_ts <= (g.end_ts or log_end_ts):
                it.gen_id = g.gen_id
                break

        # 상태 분류. wait_threads < 0 이면 설비 시작 신호(INSP_START) 없이
        # RESET 으로만 발견된 검사 = 시뮬레이션/수동 실행으로 간주한다.
        #
        # 완료(END) 판정의 커버리지는 comm 로그가 존재하는 구간까지만 유효하다.
        # 가동 중 복사된 로그는 파일별 절단 시각이 달라(comm 이 먼저 잘리는 사례
        # 실측: Tenneco 30일 13분 차) comm 절단 이후 시작 검사는 "미완료"가 아니라
        # "커버리지 밖(판정 불가)"으로 구분해야 한다.
        # InspStarter 유실 시 comm ACK 만으로도 설비 검사임을 인지한다
        from_machine = it.wait_threads >= 0 or bool(it.ack_status)
        eof_boundary = comm_end_ts or log_end_ts
        near_eof = bool(it.start_ts) and it.start_ts > eof_boundary - 300
        it.n_zones = len(ack_groups.get(it.inner_id, ()))
        it.n_zones_done = len(end_groups.get(it.inner_id, ()))
        if it.ack_status and it.ack_status != "OK":
            it.status = "rejected"                    # NoInspThread 등
        elif it.n_zones and 0 < it.n_zones_done < it.n_zones and not near_eof:
            # 다존 설비에서 일부 존만 END 수신 (커버리지 내) = 존 부분 완료
            it.status = "incomplete"
        elif it.end_ts:
            it.status = "complete"
        elif near_eof:
            # 로그 절단 구간은 소실/시뮬 판정보다 우선한다. 절단 시점 실행 중이던
            # 검사를 '실행 중 소실'로 오판하지 않고, 설비 신호 파일(comm 등)이
            # 먼저 잘려 신호가 '없는 게 아니라 잘린' 검사를 시뮬레이션으로
            # 오판하지 않는다 (Tenneco 30 실측: 파일별 절단 13분 차 → 꼬리
            # 양산 검사 191건이 sim_partial 로 오분류되던 사례)
            it.status = "in_progress_eof"             # 로그 절단(판정 불가)
        elif it.n_lost:
            it.status = "incomplete_lost"             # 실행 중 소실 (채널 근거)
        elif it.n_zones and it.n_zones_done < it.n_zones:
            # ACK 만 받고 END 0건 + 채널 소실 근거도 없음 = 존 미완료
            # (감사 D: end_ts 요구로 존 정보가 우회되던 케이스의 잔여분)
            it.status = "incomplete"
        elif not from_machine:
            it.status = "sim_complete" if (it.n_fed and it.n_done >= it.n_fed) \
                else "sim_partial"
        elif it.start_ts:
            it.status = "incomplete"
        else:
            it.status = "unknown"

    return sorted(insp.values(), key=lambda x: x.start_ts or 0)
