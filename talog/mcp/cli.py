"""`talog mcp` 명령 — stdio 서버 · --self-test · --init-workspace."""

from __future__ import annotations

import argparse
import sys

from .. import settings
from .kit import init_workspace
from .selftest import self_test
from .server import build, serve_stdio


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="talog mcp",
                                 description="talog MCP 서버 — Claude Code·Claude 데스크톱이 talog 산출물을 도구로 읽는다 (stdio)")
    ap.add_argument("--config", action="append", default=[],
                    help="데이터 출처 talog.yaml (현장마다 하나, 여러 번 지정 가능). 없으면 기본 talog.yaml")
    ap.add_argument("--work-dir", default="", help="재현 작업·마스킹 소금 저장 폴더 (기본: 첫 출처 data_dir\\mcp)")
    ap.add_argument("--kb", default="", help="사례 KB 폴더 (기본: talog 의 kb)")
    ap.add_argument("--no-mask", action="store_true", help="마스킹 끄기 (사내 전용 분석에서만)")
    ap.add_argument("--self-test", action="store_true", help="설치 점검만 하고 끝낸다")
    ap.add_argument("--json", action="store_true", help="--self-test 결과를 JSON 으로 (콘솔 연결 시험용)")
    ap.add_argument("--init-workspace", metavar="폴더", default="",
                    help="Claude 데스크톱·Claude Code 에서 열 에이전트 폴더를 만든다 (CLAUDE.md·.mcp.json·RCA 스킬)")
    args = ap.parse_args(argv)
    configs = args.config or [settings.default_path()]
    if args.init_workspace:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        try:
            r = init_workspace(args.init_workspace, configs)
        except (OSError, ValueError) as e:
            print(f"[talog mcp] 에이전트 폴더를 만들지 못했습니다: {e}")
            return 1
        print(f"[talog mcp] 에이전트 폴더: {r['dir']}")
        print(f"  데이터 출처: {', '.join(r['sources'])}")
        print(f"  만든 파일: {', '.join(r['made'])}")
        if r["kept"]:
            print(f"  그대로 둔 파일: {', '.join(r['kept'])} (사용자가 고친 내용 보존)")
        print("  다음: Claude 데스크톱 → Code 탭 → 폴더 선택 → 이 폴더 (또는 이 폴더에서 claude 실행)")
        return 0
    try:
        server = build(configs, args.work_dir, not args.no_mask, args.kb)
    except (OSError, ValueError) as e:
        print(f"[talog mcp] 시작 실패: {e}", file=sys.stderr)
        return 1
    if args.self_test:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        return self_test(server, args.json)
    print(f"[talog mcp] 대기 — 출처 {len(server.sources)}개 ({', '.join(s.name for s in server.sources)}), "
          f"마스킹 {'켬' if server.mask.on else '끔'}", file=sys.stderr)
    return serve_stdio(server)
