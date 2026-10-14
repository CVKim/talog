# talog RCA 에이전트 폴더

talog 콘솔(Claude 연동 → 에이전트 폴더 만들기) 또는 `talog mcp --init-workspace <폴더>` 가 만든 폴더다.
지금 쓰는 Claude 구독으로 돈다 — API 키가 없고 추가 비용이 들지 않는다.

## 여는 법

- **Claude 데스크톱**: Code 탭 → 폴더 선택 → 이 폴더. 처음 열 때 `.mcp.json` 의 talog 서버를 쓸지 물으면 허용한다.
- **Claude Code (터미널)**: 이 폴더에서 `claude` 실행.

## 들어 있는 것

| 파일 | 하는 일 |
|---|---|
| `CLAUDE.md` | 에이전트 역할·일하는 방식·워크플로·하지 말 것·배운 점 (Claude 가 매번 읽는다) |
| `.mcp.json` | talog MCP 서버 연결 (현장 talog.yaml 을 데이터 출처로) |
| `.claude/skills/talog-rca/` | RCA 절차와 보고서 템플릿 |
| `reports/` | 보고서가 쌓이는 곳 |
| `mcp_jobs/` | 경보 재현 작업 폴더 |

## 첫 질문 예

- "talog 개요 보여 줘"
- "가장 최근 심각 사건을 분석해 줘"
- `/mcp__talog__daily_review`

`CLAUDE.md` 의 "배운 점"에 현장 사정을 한 줄씩 더할수록 분석이 그 현장에 맞아진다.
