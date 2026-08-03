"""통합 인덱스 생성 (thin wrapper) — 본체는 talog/fleetindex.py.

사용: python make_index.py [out 폴더 (기본 .\\out)]
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from talog.fleetindex import build  # noqa: E402

if __name__ == "__main__":
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "out")
    path = build(out_dir)
    print(f"index: {path}")
