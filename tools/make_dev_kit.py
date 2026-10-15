"""개발자 PC 배포판 생성기 — 현장 로그 복사본과 Claude 구독으로 원인을 찾는 PC 에 줄 talog 묶음.

사용: python tools\\make_dev_kit.py [--out deploy] [--manual-dir Result\\manual]

산출: <out>\\talog_<버전>_개발자PC\\  (+ .zip)
  talog.exe · run_talog.bat · talog.yaml(개발자 PC 프리셋) · 시작하기.txt
  매뉴얼\\ talog_개발자PC_매뉴얼.html · diagrams\\*.html(대화형 그림) · USAGE.md
  (HTML 매뉴얼이 없으면 docs\\DEV_PC_MANUAL.md 와 그림을 넣는다)

dist\\talog.exe 의 판이 소스 판(talog/__init__.py)과 다르면 묶지 않는다 — 옛 exe 를 배포하는 사고를 막는다.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_START = """talog {ver} — 개발자 PC 배포판

현장에서 가져온 talos 로그를 이 PC 에서 talog 와 Claude(구독)로 살펴보는 묶음입니다.

1. 이 폴더를 원하는 곳(예: D:\\talog)에 풉니다.
2. 현장 로그 일자 폴더를 통째로 복사합니다. 예: D:\\AIV_LOG\\설비A\\2026_10\\12
3. run_talog.bat 을 더블클릭합니다. 브라우저에 콘솔(http://127.0.0.1:8778)이 개발자 모드로 열립니다
   (talog.yaml 의 mode: developer — 왼쪽 메뉴에 Claude 가 있습니다. 모드는 콘솔 왼쪽 위에서 바꿉니다).
4. 콘솔 → 설정 → 1 수집에서 talos 로그 루트(D:\\AIV_LOG\\설비A)와 저장 폴더를 맞추고 '저장하고 적용'을 누릅니다.
5. Claude 를 쓰려면 이 PC 의 Claude Code 가 구독 계정(Pro·Max)으로 로그인돼 있어야 합니다.
   명령 프롬프트에서 claude 를 실행해 로그인 화면이 나오지 않으면 됩니다. API 키는 필요 없습니다.

자세한 사용법: 매뉴얼\\{manual} (브라우저로 열기)
매뉴얼 화면은 현장 로그로 찍었고(설비 이름·경로는 가림) USAGE.md 에 검증 현장 이름이 있으므로 사내 개발자에게만 돌리십시오.
"""


def exe_version(exe: str) -> str:
    out = subprocess.run([exe, "--help"], capture_output=True, timeout=60).stdout.decode("utf-8", "replace")
    m = re.search(r"talog (\d+\.\d+\.\d+)", out)
    return m.group(1) if m else ""


def dev_yaml() -> str:
    """저장소 talog.yaml 템플릿(주석 그대로)에 개발자 PC 값만 채운다."""
    with open(os.path.join(ROOT, "talog.yaml"), encoding="utf-8") as f:
        y = f.read()
    pairs = [
        (r"^# talog 설정 — .*$", "# talog 설정 — 개발자 PC 용 (현장 로그 복사본 분석 · Claude). 설비마다 이 파일을 하나씩 둘 수 있다"),
        (r"^mode: field    (\s+#)", r"mode: developer\1"),             # 주석 열을 맞춘다
        (r"^log_root: .*?(\s+#)", r"log_root: D:\\AIV_LOG\\설비A\1"),
        (r"^data_dir: .*?(\s+#)", r"data_dir: D:\\talog_data\\설비A\1"),
        (r"^(\s+)toast: true", r"\1toast: false"),
    ]
    for pat, rep in pairs:
        y, n = re.subn(pat, rep, y, count=1, flags=re.M)
        assert n == 1, f"talog.yaml 템플릿에서 찾지 못함: {pat}"
    from talog import settings
    s = settings.normalize(__import__("yaml").safe_load(y))
    assert s["log_root"] == r"D:\AIV_LOG\설비A" and s["notify"]["toast"] is False and s["mail"]["provider"] == "off"
    assert s["mode"] == "developer", "개발자 PC 배포판은 개발자 모드로 연다 (Claude 연동)"
    return y


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "deploy"))
    ap.add_argument("--manual-dir", default=os.path.join(ROOT, "Result", "manual"),
                    help="HTML 매뉴얼 폴더 (talog_개발자PC_매뉴얼.html 과 diagrams). 없으면 docs 의 마크다운판")
    args = ap.parse_args()

    from talog import __version__
    exe = os.path.join(ROOT, "dist", "talog.exe")
    if not os.path.exists(exe):
        print("dist\\talog.exe 가 없습니다. 먼저 빌드하십시오: python -m PyInstaller talog.spec --noconfirm")
        return 2
    ver = exe_version(exe)
    if ver != __version__:
        print(f"dist\\talog.exe 판({ver or '알 수 없음'})이 소스 판({__version__})과 다릅니다. 다시 빌드하십시오.")
        return 2

    dst = os.path.join(os.path.abspath(args.out), f"talog_{ver}_개발자PC")
    if os.path.exists(dst):
        shutil.rmtree(dst)
    man = os.path.join(dst, "매뉴얼")
    os.makedirs(man)
    shutil.copy2(exe, os.path.join(dst, "talog.exe"))
    shutil.copy2(os.path.join(ROOT, "run_talog.bat"), os.path.join(dst, "run_talog.bat"))
    with open(os.path.join(dst, "talog.yaml"), "w", encoding="utf-8") as f:
        f.write(dev_yaml())

    html = os.path.join(args.manual_dir, "talog_개발자PC_매뉴얼.html")
    if os.path.exists(html):
        manual = os.path.basename(html)
        shutil.copy2(html, os.path.join(man, manual))
        dia = os.path.join(man, "diagrams")
        os.makedirs(dia)
        for p in glob.glob(os.path.join(args.manual_dir, "diagrams", "*.html")):
            if ".visual-check" not in p:
                shutil.copy2(p, dia)
    else:
        manual = "DEV_PC_MANUAL.md"
        shutil.copy2(os.path.join(ROOT, "docs", manual), os.path.join(man, manual))
        shutil.copytree(os.path.join(ROOT, "docs", "manual"), os.path.join(man, "manual"))
    shutil.copy2(os.path.join(ROOT, "USAGE.md"), os.path.join(man, "USAGE.md"))
    with open(os.path.join(dst, "시작하기.txt"), "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(_START.format(ver=ver, manual=manual))

    zpath = dst + ".zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for base, _dirs, files in os.walk(dst):
            for name in sorted(files):
                p = os.path.join(base, name)
                z.write(p, os.path.join(os.path.basename(dst), os.path.relpath(p, dst)))
    print(f"개발자 PC 배포판: {dst}")
    print(f"압축본: {zpath} ({os.path.getsize(zpath) / 1048576:.1f} MB)")
    for base, _dirs, files in os.walk(dst):
        for name in sorted(files):
            p = os.path.join(base, name)
            print(f"  {os.path.relpath(p, dst)}  {os.path.getsize(p) // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
