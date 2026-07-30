"""현장 배포 키트 생성기.

사용: python tools\\make_deploy.py --site "CMFB#1" [--webhook URL] [--out deploy]

산출: deploy\\talog_watch_<site>\\  (+ .zip)
  talog.exe / run_watch.bat / watch.yaml(사이트 프리셋) /
  watch_script_example.txt / DEPLOY.md
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_DEPLOY_MD = """# talog watch 현장 설치 안내 — {site}

## 구성 파일
- `talog.exe`           : 분석기/감시기 단일 실행 파일 (Python 설치 불필요)
- `run_watch.bat`       : 상주 감시 실행 (더블클릭)
- `watch.yaml`          : 감시 설정 (경로·룰 임계·알림)
- `watch_script_example.txt` : LLM 감시 지시문 예시 (선택 기능)

## 설치 (5분)
1. 이 폴더를 설비 PC 의 `D:\\talog\\` 로 복사합니다.
2. **점검**: 명령 프롬프트에서
   `D:\\talog\\talog.exe watch --check --config D:\\talog\\watch.yaml`
   → 경로/파일/알림 점검 결과와 테스트 토스트 팝업이 표시됩니다.
3. **시작**: `run_watch.bat` 더블클릭 (콘솔 창이 떠 있는 동안 감시 동작).
4. (권장) 로그인 시 자동 시작 등록:
   `schtasks /Create /TN "talog watch" /SC ONLOGON /TR "D:\\talog\\run_watch.bat" /RL LIMITED`

## 확인 방법
- 경보 발생 시: 화면 우하단 **토스트 팝업**
- 기록: `D:\\AIV_LOG\\TalogWatch\\alerts_YYYYMMDD.jsonl` (예지보전 로그)
- 감시 대상: `D:\\AIV_LOG\\Talos\\<년_월>\\<일>\\` (talos 가 쓰는 로그)

## 안전 설계 (검사 프로그램 영향 없음)
- 새로 쓰인 로그 바이트만 20초 주기로 읽습니다 (초당 수 KB 수준)
- 프로세스 우선순위 BELOW_NORMAL — 검사 SW 에 항상 CPU 양보
- GPU 를 사용하지 않습니다 (LLM 옵션도 기본 CPU 모드)
- 문제가 생기면 콘솔 창을 닫는 것만으로 완전히 중지됩니다

## 임계값 튜닝
`watch.yaml` 의 rules 항목을 수정 후 bat 재시작. 자세한 설명은 파일 내 주석 참조.
사고 재현 검증: `talog.exe watch --replay <과거 일자 폴더> --config watch.yaml`

## 리포트 분석 (같은 exe)
`talog.exe "D:\\AIV_LOG\\Talos" --open`  → 일자별 진단 리포트 생성
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True, help='설비 이름 (예: "CMFB#1")')
    ap.add_argument("--webhook", default="", help="웹훅 URL (없으면 토스트/JSONL만)")
    ap.add_argument("--out", default=os.path.join(ROOT, "deploy"))
    args = ap.parse_args()

    exe = os.path.join(ROOT, "dist", "talog.exe")
    if not os.path.exists(exe):
        print("dist\\talog.exe 가 없습니다. 먼저 빌드하십시오: "
              "python -m PyInstaller talog.spec --noconfirm")
        return 2

    safe = re.sub(r"[^0-9A-Za-z가-힣_#-]+", "_", args.site)
    dst = os.path.join(args.out, f"talog_watch_{safe}")
    os.makedirs(dst, exist_ok=True)

    shutil.copy2(exe, os.path.join(dst, "talog.exe"))
    shutil.copy2(os.path.join(ROOT, "watch_script_example.txt"), dst)

    # run_watch.bat — 배포 폴더 기준 상대 경로 버전
    with open(os.path.join(dst, "run_watch.bat"), "w", encoding="utf-8") as f:
        f.write('@echo off\nchcp 65001 >nul\ncd /d "%~dp0"\n'
                'talog.exe watch --config "%~dp0watch.yaml"\npause\n')

    # watch.yaml — 사이트 프리셋
    with open(os.path.join(ROOT, "watch.yaml"), "r", encoding="utf-8") as f:
        y = f.read()
    y = y.replace('site: ""', f'site: "{args.site}"')
    if args.webhook:
        y = y.replace('webhook: ""', f'webhook: "{args.webhook}"')
    with open(os.path.join(dst, "watch.yaml"), "w", encoding="utf-8") as f:
        f.write(y)

    with open(os.path.join(dst, "DEPLOY.md"), "w", encoding="utf-8") as f:
        f.write(_DEPLOY_MD.format(site=args.site))

    zpath = dst + ".zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for name in os.listdir(dst):
            z.write(os.path.join(dst, name), name)

    print(f"배포 키트 생성: {dst}")
    print(f"압축본: {zpath} "
          f"({os.path.getsize(zpath) / 1048576:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
