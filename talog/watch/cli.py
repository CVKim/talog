"""`talog run` / `talog watch` 명령 — 설정을 읽어 실시간·한 번·리플레이·자가 점검·시험 메일로 나눈다."""

from __future__ import annotations

import sys

from .config import load_config
from .live import run_live
from .replay import run_replay
from .selfcheck import run_check, run_test_email


def main(argv=None, prog: str = "talog watch") -> int:
    import argparse
    from ..settings import default_path
    ap = argparse.ArgumentParser(prog=prog, description="상주 감시 (화면 없이)")
    ap.add_argument("--config", default=default_path())
    ap.add_argument("--once", action="store_true", help="1회 스캔 후 종료")
    ap.add_argument("--replay", default="", help="과거 일자 폴더 재생 검증")
    ap.add_argument("--check", action="store_true",
                    help="설치 자가 점검 (경로·파일·알림 테스트)")
    ap.add_argument("--test-email", action="store_true",
                    help="예시 사건 메일 1통 발송 (메일 설정 확인)")
    # 옛 `talog watch --ui` 호환 (콘솔은 이제 `talog` 로 연다)
    ap.add_argument("--ui", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--port", type=int, default=8778, help=argparse.SUPPRESS)
    ap.add_argument("--no-open", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    if args.ui:
        from ..console import serve
        try:
            return serve(args.config, port=args.port, open_browser=not args.no_open)
        except OSError as e:
            print(f"[talog 콘솔] 포트 {args.port} 사용 불가 — --port 로 바꾸십시오 ({e})")
            return 1
    cfg = load_config(args.config)
    if args.check:
        return run_check(cfg)
    if args.test_email:
        return run_test_email(cfg)
    if args.replay:
        return run_replay(cfg, args.replay)
    return run_live(cfg, once=args.once)
