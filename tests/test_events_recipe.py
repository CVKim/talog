"""events.py + rules/events.yaml + recipe.py 통합 테스트.

실제 배포 룰(talog/rules/events.yaml)로 Extractor 를 생성하고, tmp 경로에
합성한 talos 로그 파일을 extract_file 로 파싱하여 이벤트 추출 규약을 검증한다.
레시피 파서(load_recipe)는 합성 ini 3종(ALG/ROI/DLMODEL)으로 검증한다.
외부 드라이브(H:, D:) 데이터에는 의존하지 않는다.
"""

from __future__ import annotations

import pytest

from talog.events import Extractor
from talog.fileclass import classify
from talog.recipe import load_recipe

# 탐지 대상 obj_id (인스턴스 주소 문자열) — 라인 문법 예시와 동일하게 사용한다.
_OBJ = "2778188550208"


def _line(ts: str, level: str, header: str, msg: str, obj_id: str = _OBJ) -> str:
    """talos 로그 한 줄을 조립한다: ts<TAB>[Level][Header][ObjId]<TAB>msg"""
    return f"{ts}\t[{level}][{header}][{obj_id}]\t{msg}"


def _write_log(path, lines):
    """CRLF 종결 로그 파일을 기록한다 (newline='' 로 변환을 막는다)."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        for ln in lines:
            f.write(ln + "\r\n")


def _by_kind(events):
    """이벤트 목록을 kind 별로 묶는다."""
    d: dict[str, list] = {}
    for ev in events:
        d.setdefault(ev.kind, []).append(ev)
    return d


@pytest.fixture(scope="module")
def extractor():
    # 실제 배포 룰 파일(rules/events.yaml)로 추출기를 생성한다.
    return Extractor()


# ---------------------------------------------------------------------------
# 1. alg 파일: RESET / BLOCK_START / TACT_INFER / MODEL_FAIL
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def alg_result(extractor, tmp_path_factory):
    root = tmp_path_factory.mktemp("alg")
    path = root / "alg_dl_1(CH_A).log"
    _write_log(path, [
        _line("2026/07/29-09:21:14.811", "Debug", "BaseInspector::Reset",
              "Reset. inner id: 2000026072709262821, product id: TIRE-ABC"),
        _line("2026/07/29-09:21:15.001", "Info",
              "Dataflow::InferMultiChannelAthenaAlg::Execute", "+Execute"),
        _line("2026/07/29-09:21:15.500", "Debug", "Tact",
              "Dataflow::InferMultiChannelAthenaAlg::Execute- "
              "Model : X-123, InspectMC Tact = 1234.0"),
        _line("2026/07/29-09:21:16.000", "Error", "DeepLearningInspector::Initialize",
              "Failed to create ALG Blocks - DLMODEL: DLMODEL0005"),
    ])
    fi = classify(str(path))
    assert fi is not None and fi.category == "alg" and fi.alg_idx == 1
    events, n = extractor.extract_file(fi, file_id=7)
    return events, n


def test_alg_reset_captures_inner_and_product_id(alg_result):
    # RESET 이벤트에서 inner_id / product_id 캡처를 검증한다.
    events, _ = alg_result
    resets = _by_kind(events)["RESET"]
    assert len(resets) == 1
    ev = resets[0]
    assert ev.inner_id == "2000026072709262821"
    assert ev.product_id == "TIRE-ABC"
    assert ev.level == "Debug"


def test_alg_block_start_requires_info_and_dataflow_header(alg_result):
    # BLOCK_START 는 Info 레벨 + Dataflow::<block>::Execute 헤더에서만 발생하며
    # 헤더의 블럭명이 block 필드로 캡처되어야 한다.
    events, _ = alg_result
    blocks = _by_kind(events)["BLOCK_START"]
    assert len(blocks) == 1
    ev = blocks[0]
    assert ev.block == "InferMultiChannelAthenaAlg"
    assert ev.level == "Info"


def test_alg_tact_infer_captures_model_and_value(alg_result):
    # TACT_INFER: "Model : X-123, InspectMC Tact = 1234.0" 에서 model / value 캡처.
    events, _ = alg_result
    tacts = _by_kind(events)["TACT_INFER"]
    assert len(tacts) == 1
    ev = tacts[0]
    assert ev.model == "X-123"
    assert ev.value == pytest.approx(1234.0)


def test_alg_model_fail_captures_model(alg_result):
    # MODEL_FAIL 이벤트에서 실패 모델명 캡처를 검증한다.
    events, _ = alg_result
    fails = _by_kind(events)["MODEL_FAIL"]
    assert len(fails) == 1
    assert fails[0].model == "DLMODEL0005"
    assert fails[0].level == "Error"


def test_alg_common_fields_and_record_count(alg_result):
    # 총 레코드 수와 공통 필드(file_id, alg_idx, obj_id) 전파를 검증한다.
    events, n = alg_result
    assert n == 4
    assert len(events) == 4
    for ev in events:
        assert ev.file_id == 7
        assert ev.alg_idx == 1     # 파일명 alg_dl_1(...) 의 채널 인덱스
        assert ev.obj_id == _OBJ


# ---------------------------------------------------------------------------
# 2. comm 파일: COMM_MSG 캡처 + MOTION/KEEP_ALIVE 제외
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def comm_result(extractor, tmp_path_factory):
    root = tmp_path_factory.mktemp("comm")
    path = root / "comm.log"
    _write_log(path, [
        _line("2026/07/29-09:21:14.811", "Debug", "CCommMng::Recv",
              "V2M_INSPECT_START_ACK,V3.0,TALOS3,NoInspThread,"
              "2000026072709262821,1641694923,1"),
        _line("2026/07/29-09:21:15.000", "Debug", "CCommMng::Recv",
              "M2V_REQUEST_MOTION_INDEX_ACK,V3.0,TALOS3,OK,1,2"),
        _line("2026/07/29-09:21:15.200", "Debug", "CCommMng::Recv",
              "V2M_KEEP_ALIVE,V3.0,TALOS3,OK"),
    ])
    fi = classify(str(path))
    assert fi is not None and fi.category == "comm"
    return extractor.extract_file(fi, file_id=2)


def test_comm_msg_captures_name_status_inner_id(comm_result):
    # V2M_INSPECT_START_ACK 페이로드에서 name / status / inner_id 캡처를 검증한다.
    events, _ = comm_result
    msgs = _by_kind(events).get("COMM_MSG", [])
    assert len(msgs) == 1
    ev = msgs[0]
    assert ev.name == "V2M_INSPECT_START_ACK"
    assert ev.status == "NoInspThread"
    assert ev.inner_id == "2000026072709262821"


def test_comm_excludes_motion_and_keepalive(comm_result):
    # M2V_REQUEST_MOTION_* 와 V2M_KEEP_ALIVE 는 수집 대상에서 제외되어야 한다.
    events, n = comm_result
    assert n == 3
    assert len(events) == 1        # COMM_MSG 1건 외에는 어떤 이벤트도 없어야 한다
    names = [ev.name for ev in events]
    assert "M2V_REQUEST_MOTION_INDEX_ACK" not in names
    assert all("KEEP_ALIVE" not in ev.name for ev in events)


# ---------------------------------------------------------------------------
# 3. inspstarter 파일: INSP_START / INSP_REJECT
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def inspstarter_result(extractor, tmp_path_factory):
    root = tmp_path_factory.mktemp("inspstarter")
    path = root / "InspStarter.log"
    _write_log(path, [
        _line("2026/07/29-09:21:14.811", "Debug", "CInspStarter::OnRecv",
              "eAIVProtocol_M2V_InspStart is arrived. Waiting thread count = 0, "
              "innerid: 2000026072709262821, productId: TIRE-ABC"),
        _line("2026/07/29-09:21:15.000", "Debug", "CInspStarter::OnRecv",
              "All Seq thread is running or not initialized."),
    ])
    fi = classify(str(path))
    assert fi is not None and fi.category == "inspstarter"
    return extractor.extract_file(fi, file_id=3)


def test_inspstarter_insp_start(inspstarter_result):
    # INSP_START: 대기 스레드 수(value) / innerid / productId 캡처를 검증한다.
    events, _ = inspstarter_result
    starts = _by_kind(events)["INSP_START"]
    assert len(starts) == 1
    ev = starts[0]
    assert ev.value == pytest.approx(0.0)
    assert ev.inner_id == "2000026072709262821"
    assert ev.product_id == "TIRE-ABC"


def test_inspstarter_insp_reject(inspstarter_result):
    # 시퀀스 스레드 포화 메시지는 INSP_REJECT 로 분류되어야 한다.
    events, _ = inspstarter_result
    assert len(_by_kind(events)["INSP_REJECT"]) == 1


# ---------------------------------------------------------------------------
# 4. processusage 파일: USAGE (value=RAM MB, status=CPU%, name=스레드 수)
# ---------------------------------------------------------------------------

def test_processusage_usage_captures(extractor, tmp_path):
    path = tmp_path / "ProcessUsage.log"
    _write_log(path, [
        _line("2026/07/29-09:21:14.811", "Info", "USAGE",
              "CPU Usage : 12.5%   Memory Usage : 2048.3Mb   thread count : 61",
              obj_id="0"),
    ])
    fi = classify(str(path))
    assert fi is not None and fi.category == "processusage"
    events, n = extractor.extract_file(fi, file_id=4)
    assert n == 1
    usages = _by_kind(events)["USAGE"]
    assert len(usages) == 1
    ev = usages[0]
    assert ev.value == pytest.approx(2048.3)   # RAM(MB)
    assert ev.status == "12.5"                 # CPU%
    assert ev.name == "61"                     # 스레드 수


# ---------------------------------------------------------------------------
# 5. dlinfer 파일: DLINFER_EXEC
# ---------------------------------------------------------------------------

def test_dlinfer_exec_captures_gpu_model_tact(extractor, tmp_path):
    path = tmp_path / "dlinfer.log"
    _write_log(path, [
        _line("2026/07/29-09:21:14.811", "Debug", "CDLInferExecutor::executeV2",
              "GPU: 0 - Model: A.onnx, DeviceIdx:0, executeV2 Tact = 35.0"),
    ])
    fi = classify(str(path))
    assert fi is not None and fi.category == "dlinfer"
    events, n = extractor.extract_file(fi, file_id=5)
    assert n == 1
    execs = _by_kind(events)["DLINFER_EXEC"]
    assert len(execs) == 1
    ev = execs[0]
    assert ev.status == "0"                    # GPU 인덱스
    assert ev.model == "A.onnx"
    assert ev.value == pytest.approx(35.0)     # 순수 GPU 커널 시간(ms)


# ---------------------------------------------------------------------------
# 6. recipe.load_recipe: ALG.ini / ROI.ini / DLMODEL.ini 파싱
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def recipe(tmp_path_factory):
    root = tmp_path_factory.mktemp("recipe")
    # ALG.ini — 값 앞 공백("= 3") 형태를 섞어 파싱 견고성을 함께 확인한다.
    (root / "ALG.ini").write_text(
        "[ALG Information]\n"
        "alg count= 3\n"
        "thread count=5\n"
        "\n"
        "[ALG0001]\n"
        "Name = TestAlg\n"
        "ID = ALG_TEST\n"
        "requireroiidx = 1\n"
        "DLModel = 2\n"
        "AlgorithmType = 3\n"
        "DLL = alg_body.dll\n",
        encoding="utf-8")
    # ROI.ini — 값 앞 공백("= 61") 처리 확인용.
    (root / "ROI.ini").write_text(
        "[ROI0001]\n"
        "name = TopROI\n"
        "baseimgidx = 61\n",
        encoding="utf-8")
    (root / "DLMODEL.ini").write_text(
        "[DLMODEL Information]\n"
        "dlmodel path = D:\\AIV\\MODEL\\TEST\n"
        "\n"
        "[DLMODEL0002]\n"
        "name = TestModel\n"
        "model name = model.onnx\n"
        "dev type = 1\n"
        "dev index = 1\n"
        "instance count = 2\n"
        "infer dll name = dl_infer.dll\n",
        encoding="utf-8")
    # ALG.ini 가 root 바로 아래에 있으므로 버전 폴더 탐색 없이 그대로 파싱된다.
    return load_recipe(str(root))


def test_recipe_alg_ini_counts_and_links(recipe):
    # ALG Information 의 카운트와 ALG0001 의 ROI/DLModel 연결을 검증한다.
    assert recipe.alg_count == 3
    assert recipe.thread_count == 5
    assert 1 in recipe.algs
    a = recipe.algs[1]
    assert a.name == "TestAlg"
    assert a.roi_idx == [1]        # requireroiidx = 1
    assert a.dl_model == [2]       # DLModel = 2


def test_recipe_roi_ini_leading_space_value(recipe):
    # "baseimgidx = 61" 처럼 값 앞에 공백이 있어도 정수로 파싱되어야 한다.
    assert 1 in recipe.rois
    roi = recipe.rois[1]
    assert roi.name == "TopROI"
    assert roi.base_img == 61


def test_recipe_dlmodel_ini(recipe):
    # DLMODEL Information 경로와 DLMODEL0002 의 장치/인스턴스 설정을 검증한다.
    assert recipe.model_path == "D:\\AIV\\MODEL\\TEST"
    assert 2 in recipe.models
    m = recipe.models[2]
    assert m.model_file == "model.onnx"
    assert m.dev_index == 1        # "dev index" 키
    assert m.instance_count == 2   # "instance count" 키


def test_recipe_alg_models_link(recipe):
    # ALG0001 -> DLMODEL0002 참조가 alg_models 로 풀려야 한다.
    models = recipe.alg_models(1)
    assert [m.idx for m in models] == [2]
    assert models[0].name == "TestModel"
