"""콘솔의 분석 작업은 별도 프로세스에서 돈다 — `talog _job report|replay <폴더>`.

같은 프로세스의 스레드로 돌리면 GIL 때문에 수 분 동안 콘솔 응답이 수십 초씩 늦어지고(풍산 12일 리포트 중
/api/claude 13.8초), 검사 PC 의 우선순위도 낮출 수 없다. 콘솔은 analysis.AnalysisMixin.start_job 으로 띄우고
표준 출력을 실행 로그로 읽는다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from .. import settings
from .util import _report_tag


JOB_TAGS = "@@talog-reports "                 # 작업 → 콘솔: 만든 리포트 태그 목록 줄 머리


def self_command() -> list[str]:
    """talog 를 다시 띄우는 명령 — exe 면 exe 그대로, 아니면 이 파이썬으로 -m talog."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-u", "-m", "talog"]


def report_work(target: str, recipe_dir: str, out: str, job: dict) -> int:
    from ..cli import _collect_day_folders, scan_day
    from ..recipe import load_recipe
    recipe = None
    if recipe_dir:
        try:
            recipe = load_recipe(recipe_dir.strip().strip('"'), "")
            print(f"레시피: {recipe.root} [{recipe.version}]")
        except (FileNotFoundError, OSError) as e:
            print(f"레시피를 열 수 없어 레시피 없이 진행합니다 — {e}")
    days = _collect_day_folders(os.path.abspath(target))
    if not days:
        print("분석할 로그 폴더가 아닙니다 (alg\\ 또는 InspStarter.log 가 있는 일자 "
              "폴더, 또는 그 상위 설비 폴더를 고르십시오)")
        return 1
    os.makedirs(out, exist_ok=True)
    for d in days:
        tag = _report_tag(d)
        try:
            scan_day(d, recipe, out, tag, fast=True)
            job["reports"].append(tag)
        except Exception as e:                # 일자 하나의 실패가 전체를 막지 않는다
            print(f"오류: {d} — {type(e).__name__}: {e}")
    print(f"리포트 {len(job['reports'])}건 완료")
    return 0 if job["reports"] else 1


def job_main(argv: list[str]) -> int:
    """콘솔이 띄우는 분석 작업: talog _job report|replay <폴더> --config <설정 json> [--out] [--recipe]."""
    import argparse
    ap = argparse.ArgumentParser(prog="talog _job")
    ap.add_argument("kind", choices=("report", "replay"))
    ap.add_argument("target")
    ap.add_argument("--config", required=True, help="콘솔이 넘긴 사용자 설정(json)")
    ap.add_argument("--out", default="")
    ap.add_argument("--recipe", default="")
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    except (AttributeError, ValueError):
        pass
    with open(a.config, encoding="utf-8") as f:
        user = json.load(f)
    if a.kind == "replay":
        from ..watch import run_replay
        return run_replay(settings.compile(user), a.target)
    job = {"reports": []}
    rc = report_work(a.target, a.recipe, a.out or os.path.join(os.getcwd(), "reports"), job)
    print(JOB_TAGS + json.dumps(job["reports"], ensure_ascii=False), flush=True)
    return rc


def kill_tree(p: subprocess.Popen) -> None:
    """프로세스와 그 자식(MCP 서버)까지 끝낸다."""
    if p.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        p.kill()
    except OSError:
        pass
