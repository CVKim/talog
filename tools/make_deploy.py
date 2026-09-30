"""현장 배포 키트 생성기.

사용: python tools\\make_deploy.py --site "CMFB#1" [--email-to a@x.com] [--webhook URL] [--out deploy]

산출: deploy\\talog_<site>\\  (+ .zip)
  talog.exe / run_talog.bat / run_service.bat / talog.yaml(사이트 프리셋) / DEPLOY.md
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_DEPLOY_MD = """# talog 현장 설치 안내 — {site}

talog 는 설비 PC 에 상주하는 **AI 비전 로그 운영 플랫폼**입니다.
수집(오늘 로그 폴더 실시간 추적) → 탐지(경보 규칙) → 판단(룰 진단 + 로컬 LLM 교차 확인)
→ 알림(메일·토스트)을 한 흐름으로 처리하고, 같은 화면에서 로그 폴더 진단 리포트와
자연어 질문을 제공합니다.

## 구성 파일
- `talog.exe`        : 단일 실행 파일 (Python 설치 불필요)
- `run_talog.bat`    : **더블클릭 = 콘솔** (http://127.0.0.1:8778, 이 PC 에서만 접속)
                       로그 폴더를 끌어다 놓으면 그 폴더의 진단 리포트를 만듭니다
- `run_service.bat`  : 콘솔 없이 상주 감시 (자동 시작용)
- `talog.yaml`       : 설정 — 콘솔의 '설정' 화면에서 고칩니다

## 설치 (5분)
1. 이 폴더를 설비 PC 의 `D:\\talog\\` 로 복사합니다.
2. 점검: 명령 프롬프트에서 `D:\\talog\\talog.exe run --check`
   → 경로·추적 파일·메일·LLM·GPU 점검 결과와 테스트 토스트가 나옵니다.
3. `run_talog.bat` 더블클릭 → 브라우저의 콘솔에서
   - 설정 → 1 수집: 설비 이름·로그 루트 확인
   - 설정 → 2 탐지: 치명 결함명(레시피 결함명 목록에서 클릭), 꼭 잡을 로그 문구
   - 설정 → 3 판단: LLM 교차 확인을 쓸지, CPU/GPU/자동
   - 설정 → 4 알림: 발송 방식(Microsoft 365 권장)·받는 사람 → 테스트 메일
4. (선택) 로그인 시 자동 시작:
   `schtasks /Create /TN "talog" /SC ONLOGON /TR "D:\\talog\\run_service.bat" /RL LIMITED`

## 화면
- 운영 현황: 수집·탐지·판단·알림 네 단계의 지금 상태, 오늘 사건·경보
- 사건: 사건별 판단(룰·LLM 일치 여부)·권고 조치·근거·보낸 메일, 사건에 질문
- 분석: 로그 폴더 진단 리포트 / 경보 재현(과거 일자를 현재 규칙으로 다시 돌림) / 리포트에 질문

## 안전 설계 (검사 프로그램 영향 최소)
- 새로 쓰인 로그 바이트만 20초 주기로 읽습니다 (핵심 로그 모드는 초당 수 KB)
- 프로세스 우선순위 BELOW_NORMAL — 검사 SW 에 늘 CPU 양보
- LLM 은 기본 CPU 모드(검사 GPU 미사용), 자동 모드는 GPU 여유가 있을 때만
- 메일 비밀번호·암호는 Windows DPAPI 로 암호화해 이 PC 에만 저장
- 콘솔 창을 닫으면 완전히 멈춥니다
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True, help='설비 이름 (예: "CMFB#1")')
    ap.add_argument("--webhook", default="", help="웹훅 URL (없으면 토스트/기록만)")
    ap.add_argument("--email-to", default="",
                    help="메일 받는 사람 (쉼표 구분) — 발송 방식·인증은 현장 콘솔에서 설정")
    ap.add_argument("--out", default=os.path.join(ROOT, "deploy"))
    args = ap.parse_args()

    exe = os.path.join(ROOT, "dist", "talog.exe")
    if not os.path.exists(exe):
        print("dist\\talog.exe 가 없습니다. 먼저 빌드하십시오: "
              "python -m PyInstaller talog.spec --noconfirm")
        return 2

    from talog import settings
    safe = re.sub(r"[^0-9A-Za-z가-힣_#-]+", "_", args.site)
    dst = os.path.join(args.out, f"talog_{safe}")
    os.makedirs(dst, exist_ok=True)

    shutil.copy2(exe, os.path.join(dst, "talog.exe"))
    for bat in ("run_talog.bat", "run_service.bat"):
        shutil.copy2(os.path.join(ROOT, bat), os.path.join(dst, bat))

    # talog.yaml — 사이트 프리셋 (주석은 저장소 템플릿 그대로, 값만 채운다)
    with open(os.path.join(ROOT, "talog.yaml"), encoding="utf-8") as f:
        y = f.read()
    y = y.replace('site: ""  ', f'site: "{args.site}"  ', 1)
    if args.webhook:
        y = y.replace('webhook: ""  ', f'webhook: "{args.webhook}"  ', 1)
    if args.email_to:
        to = ", ".join(a.strip() for a in args.email_to.split(",") if a.strip())
        y = y.replace("to: []  ", f"to: [{to}]  ", 1)
    s = settings.normalize(__import__("yaml").safe_load(y))
    assert s["site"] == args.site, "talog.yaml 템플릿의 site 줄을 찾지 못했습니다"
    with open(os.path.join(dst, "talog.yaml"), "w", encoding="utf-8") as f:
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
