# -*- coding: utf-8 -*-
"""talog.lineparser / talog.fileclass 단위 테스트.

외부 드라이브의 실제 로그에 의존하지 않고 tmp_path 에 합성 로그를 생성하여 검증한다.
talos 로그 라인 문법: "yyyy/MM/dd-HH:mm:ss.fff<TAB>[Level][Header][ObjId]<TAB>msg<CRLF>"
"""

from datetime import datetime

import pytest

from talog.fileclass import classify
from talog.lineparser import _sniff_encoding, iter_batchrun, iter_records


def _write_bytes(tmp_path, name: str, data: bytes) -> str:
    """바이트를 그대로 기록하여 인코딩/개행을 정밀하게 통제한다."""
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


# ---------------------------------------------------------------------------
# 1. iter_records: 정상 라인 파싱
# ---------------------------------------------------------------------------

def test_iter_records_parses_normal_line(tmp_path):
    # 탭 구분 + CRLF 종결의 표준 라인 1건을 파싱한다.
    line = "2026/07/29-09:21:14.811\t[Debug][Class::Func][2778188550208]\t메시지\r\n"
    path = _write_bytes(tmp_path, "seq_1.log", line.encode("utf-8"))

    records = list(iter_records(path))
    assert len(records) == 1

    rec = records[0]
    # 타임스탬프는 로컬 타임존 기준 epoch 초(밀리초 포함)이다.
    expected_ts = datetime(2026, 7, 29, 9, 21, 14, 811000).timestamp()
    assert rec.ts == pytest.approx(expected_ts)
    assert rec.ts_text == "09:21:14.811"
    assert rec.level == "Debug"
    assert rec.header == "Class::Func"
    assert rec.obj_id == "2778188550208"
    assert rec.msg == "메시지"
    assert rec.line_no == 1


# ---------------------------------------------------------------------------
# 2. iter_records: 멀티라인 메시지 병합
# ---------------------------------------------------------------------------

def test_iter_records_merges_multiline_message(tmp_path):
    # 타임스탬프로 시작하지 않는 연속 줄은 직전 레코드 msg 에 개행으로 병합되어야 한다.
    content = (
        "2026/07/29-09:21:14.811\t[Error][Ex::Dump][0]\t예외 발생\r\n"
        "  at Foo.Bar()\r\n"
        "  at Baz.Qux()\r\n"
        "2026/07/29-09:21:15.000\t[Info][Next::Line][42]\t다음 레코드\r\n"
    )
    path = _write_bytes(tmp_path, "exception.log", content.encode("utf-8"))

    records = list(iter_records(path))
    assert len(records) == 2

    first, second = records
    # 연속 줄 2개가 개행으로 이어 붙는다 (CRLF 는 제거된 상태).
    assert first.msg == "예외 발생\n  at Foo.Bar()\n  at Baz.Qux()"
    assert first.line_no == 1
    # 다음 레코드는 병합의 영향을 받지 않는다.
    assert second.msg == "다음 레코드"
    assert second.obj_id == "42"
    assert second.line_no == 4


# ---------------------------------------------------------------------------
# 3. iter_records: 태그 블록이 없는 비정형 줄
# ---------------------------------------------------------------------------

def test_iter_records_untagged_line_has_empty_level(tmp_path):
    # [Level][Header][ObjId] 3중 태그가 아닌 줄은 level/header 빈 문자열,
    # obj_id "0" 으로 파싱되고 나머지 전체가 msg 가 되어야 한다.
    line = "2026/07/29-09:21:14.811\t[exception_callback] unhandled exception occurred\r\n"
    path = _write_bytes(tmp_path, "talos.log", line.encode("utf-8"))

    records = list(iter_records(path))
    assert len(records) == 1

    rec = records[0]
    assert rec.level == ""
    assert rec.header == ""
    assert rec.obj_id == "0"
    assert rec.msg == "[exception_callback] unhandled exception occurred"


# ---------------------------------------------------------------------------
# 4. _sniff_encoding: UTF-8 / CP949 판별
# ---------------------------------------------------------------------------

def test_sniff_encoding_utf8(tmp_path):
    # BOM 없는 UTF-8 한글 파일은 "utf-8" 로 판별되어야 한다.
    data = "2026/07/29-09:21:14.811\t[Debug][A::B][0]\t한글 메시지\r\n".encode("utf-8")
    path = _write_bytes(tmp_path, "utf8.log", data)
    assert _sniff_encoding(path) == "utf-8"


def test_sniff_encoding_cp949(tmp_path):
    # CP949 로 인코딩한 한글 바이트는 UTF-8 디코딩에 실패하므로 "cp949" 로 판별되어야 한다.
    data = "2026/07/29-09:21:14.811\t[Debug][A::B][0]\t한글 메시지\r\n".encode("cp949")
    path = _write_bytes(tmp_path, "cp949.log", data)
    assert _sniff_encoding(path) == "cp949"


# ---------------------------------------------------------------------------
# 5. iter_batchrun: BatchRunLog.txt 파싱
# ---------------------------------------------------------------------------

def test_iter_batchrun_parses_kill_script(tmp_path):
    content = (
        "2026-07-27 08:30:57 - talos_kill.bat task start.\r\n"
        "무관한 잡음 줄입니다\r\n"
        "2026-07-27 08:31:10 - talos_start.bat task start.\r\n"
    )
    path = _write_bytes(tmp_path, "BatchRunLog.txt", content.encode("utf-8"))

    rows = list(iter_batchrun(path))
    assert len(rows) == 2

    ts, ts_text, script = rows[0]
    expected_ts = datetime(2026, 7, 27, 8, 30, 57).timestamp()
    assert ts == pytest.approx(expected_ts)
    assert ts_text == "08:30:57.000"
    assert script == "talos_kill.bat"

    # 두 번째 항목도 스크립트명이 정확히 추출되는지 확인한다.
    assert rows[1][2] == "talos_start.bat"


# ---------------------------------------------------------------------------
# 6. fileclass.classify: 파일명 분류
# ---------------------------------------------------------------------------

def test_classify_alg_dl_with_comma_channel():
    # 채널명에 쉼표/공백이 포함되어도 alg 패턴이 정확히 분해되어야 한다.
    info = classify("alg_dl_1(PLUG_STABBED, PLUG_CAULKING).log")
    assert info is not None
    assert info.category == "alg"
    assert info.alg_kind == "dl"
    assert info.alg_idx == 1
    assert info.channel == "PLUG_STABBED, PLUG_CAULKING"
    assert info.core is True


def test_classify_copy_suffix_is_other():
    # 윈도우 "복사본" 파일은 어떤 패턴에도 걸리지 않고 other/비core 로 분류되어야 한다.
    info = classify("comm - 복사본.log")
    assert info is not None
    assert info.category == "other"
    assert info.core is False


@pytest.mark.parametrize(
    "name, category, core",
    [
        ("InspStarter.log", "inspstarter", True),
        ("DLInfer.log", "dlinfer", True),
        ("ProcessUsage.log", "processusage", True),
        ("InspCondRelationGraph_1.log", "relgraph", True),
    ],
)
def test_classify_known_files(name, category, core):
    # 대소문자 혼용 파일명이 정확한 카테고리와 core 플래그로 매핑되어야 한다.
    info = classify(name)
    assert info is not None
    assert info.category == category
    assert info.core is core
