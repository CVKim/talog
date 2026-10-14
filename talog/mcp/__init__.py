"""talog MCP 서버 — Claude Code·Claude 데스크톱(구독)이 talog 산출물을 도구로 읽게 한다.

  talog mcp --config <현장 talog.yaml> [--config …] [--work-dir 폴더] [--no-mask] [--kb 폴더]
  talog mcp --self-test --config …        설치 점검 (도구 목록·개요를 한 번 돌려 보고 끝낸다)
  talog mcp --init-workspace <폴더>       Claude 에서 열 에이전트 폴더 만들기

talog 가 Claude 를 부르지 않고 Claude 가 talog 를 부른다 — API 키가 없고, 사용자의 Claude 구독 한도 안에서
돈다(추가 과금 없음). 현장 PC 의 talog(룰 + 로컬 Qwen)는 그대로이고, 이 서버는 현장에서 가져온 산출물
(사건·KPI·기준선·진단 리포트)과 원본 로그가 있는 사무실 PC 에서 돈다.

도구 대부분은 읽기 전용이다. 쓰는 도구는 재현 작업(start_replay: 작업 폴더에만 씀)과 사례 저장(save_case)뿐.
마스킹(기본 켬): 12자리 이상 숫자(inner id 등)는 ID-xxxxxx 로, 출처의 로그·데이터 경로는 <로그>·<데이터> 로
가린다. Claude 가 읽은 내용은 Anthropic 으로 가므로 고객이 승인한 범위만 출처로 넣는다.

패키지 구성 — 도구 하나를 더하려면 tools/ 의 영역 파일에 @tool 함수 하나만 더한다(registry.py 의 '지킬 것'):
  server.py    TalogMCP — 출처·마스킹·재현 작업·JSON-RPC(옛 판 + 2026-07-28)·자원·자동완성
  registry.py  @tool 데코레이터와 TOOLS
  tools/       incidents · reports · rawlogs · replay · knowledge
  prompts.py   MCP 프롬프트(슬래시 명령)와 Claude 에게 주는 안내·용어 글
  kit.py       에이전트 폴더(CLAUDE.md·.mcp.json·스킬) 만들기, 서버 실행 명령
  sources.py   Source(현장 하나의 산출물)·Jobs(재현 작업)
  masking.py   Masker(고객 값 가리기)        fmt.py  표·숫자·시각 글
  selftest.py  설치 점검(콘솔 '연결 시험')    cli.py  `talog mcp` 명령
"""

from .cli import main
from .kit import KIT_DIR, SKILLS, init_workspace, server_command
from .masking import Masker
from .prompts import GUIDE, INSTRUCTIONS, PROMPTS, prompt_messages
from .registry import TOOLS
from .selftest import self_test, self_test_report
from .server import SERVER_NAME, TalogMCP, build, serve_stdio
from .sources import Jobs, Source

__all__ = ["GUIDE", "INSTRUCTIONS", "KIT_DIR", "Jobs", "Masker", "PROMPTS", "SERVER_NAME", "SKILLS", "Source", "TOOLS",
           "TalogMCP", "build", "init_workspace", "main", "prompt_messages", "self_test", "self_test_report",
           "serve_stdio", "server_command"]
