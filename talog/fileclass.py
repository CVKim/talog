"""로그 파일명 분류기.

관측된 64개 파일명 패턴을 분석 카테고리로 매핑한다.
P1 은 진단에 필요한 핵심 파일(core)만 파싱하고, 대용량 상세 파일은 --deep 에서 연다.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

# alg\alg_dl_1(PLUG_STABBED, PLUG_CAULKING).log — 채널명에 쉼표/공백/괄호 이외 문자 허용
_ALG_RE = re.compile(r"^alg_(dl|rb|dummy)_(\d+)\((.+)\)\.log$", re.I)
_IDX_RE = re.compile(r"^([A-Za-z_]+?)_(\d+)(?:\((.+)\))?\.log$")


@dataclass(slots=True)
class FileInfo:
    path: str
    name: str
    category: str            # alg / inspstarter / seq / comm / pool / exception / ...
    alg_kind: str = ""       # dl / rb / dummy (alg 카테고리 한정)
    alg_idx: int = 0         # alg_dl_N 의 N (= 레시피 ALG000N)
    channel: str = ""        # 채널명(파일명 괄호 안)
    size: int = 0
    core: bool = True        # 기본 파싱 대상 여부


# 카테고리 매핑: (정확한 파일명 소문자) -> (카테고리, core 여부)
_EXACT = {
    "inspstarter.log": ("inspstarter", True),
    "workerthreadpoolmng.log": ("pool", True),
    "exception.log": ("exception", True),
    "inspskipper.log": ("inspskipper", True),
    "comm.log": ("comm", True),
    "comm_kl.log": ("comm_kl", False),      # KEEP_ALIVE 대량 — 필요 시 deep
    "comm_fail.log": ("comm_fail", True),
    "inspectmng.log": ("inspectmng", True),
    "stopthread.log": ("stopthread", True),
    "console.log": ("console", True),       # 물리 GPU #N 모델 배치의 유일 소스
    "talos.log": ("talos", False),
    "talosn.log": ("talosn", False),
    "dlinfer.log": ("dlinfer", True),       # GPU-모델 매핑 + executeV2 시계열
    "processingblock.log": ("processingblock", False),
    "alg.log": ("alg_merged", False),
    "processusage.log": ("processusage", True),  # CPU/RAM 시계열 + 릭 감지
    "batchrunlog.txt": ("batchrun", True),
}

# 접두 매핑 (번호 붙는 계열)
_PREFIX = [
    ("seq_", "seq", True),                 # seq_1.log
    ("inspresult_", "inspresult", False),  # 초대형
    ("inspcondrelationgraph", "relgraph", True),   # 종속성 그래프 (스킵 판정용)
    ("cam_ctrl", "cam", False),
    ("guide_seq", "guideseq", False),
    ("guideseq_", "guideseq_time", False),
    ("saveimg", "saveimg", False),
    ("continuousgrab", "grab", False),
]


def classify(path: str) -> Optional[FileInfo]:
    """파일 경로 -> FileInfo. 분석 대상이 아니면 None 이 아닌 low-core 로 반환."""
    name = os.path.basename(path)
    low = name.lower()
    size = os.path.getsize(path) if os.path.exists(path) else 0

    m = _ALG_RE.match(name)
    if m:
        kind, idx, ch = m.group(1).lower(), int(m.group(2)), m.group(3)
        return FileInfo(path=path, name=name, category="alg", alg_kind=kind,
                        alg_idx=idx, channel=ch, size=size, core=True)

    if low in _EXACT:
        cat, core = _EXACT[low]
        return FileInfo(path=path, name=name, category=cat, size=size, core=core)

    for pre, cat, core in _PREFIX:
        if low.startswith(pre):
            info = FileInfo(path=path, name=name, category=cat, size=size, core=core)
            mi = _IDX_RE.match(name)
            if mi and mi.group(2):
                info.alg_idx = int(mi.group(2))
            return info

    # 그 외 전부: 알려지지 않은 모듈 로그 — 목록화만 하고 파싱하지 않음
    return FileInfo(path=path, name=name, category="other", size=size, core=False)


def scan_day_folder(day_dir: str) -> list[FileInfo]:
    """설비-일자 폴더(플랫폼 로그 + alg 하위 폴더)를 스캔한다."""
    out: list[FileInfo] = []
    for base, _dirs, files in os.walk(day_dir):
        for fn in files:
            if not (fn.lower().endswith(".log") or fn.lower() == "batchrunlog.txt"):
                continue
            fi = classify(os.path.join(base, fn))
            if fi:
                out.append(fi)
    return out
