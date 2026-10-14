# talog — Claude 작업 안내

talos 비전 검사 로그를 읽어 경보·사건·원인 판단을 내는 운영 플랫폼이다(수집 → 탐지 → 판단 → 알림, 그리고 조사).
Python 3.10 이상, 런타임 의존성은 PyYAML 하나. 현장 PC 는 오프라인이고, 개발자 PC 에서는 Claude 구독으로 원인을 깊게 본다.

**이 PC 에서 앞 작업을 이어받을 때는 `Result/HANDOFF.md` 를 먼저 읽는다** — 지난 세션의 사용자 규칙, 진행 상태, 열려 있는 일,
로컬 결과물 위치가 있다(로컬 전용 파일, 없으면 넘어간다).

## 먼저 볼 문서

| 문서 | 내용 |
|---|---|
| [README.md](README.md) | 개요, 실행 명령, 폴더 구조 (영어 — 여러 개발자용) |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 개발 규칙·점검·자주 하는 변경 순서(도구·API·화면·스킬·규칙·배포) (영어) |
| [USAGE.md](USAGE.md) | 사용자 설명서 — 콘솔 화면, 설정, 판단 결과·리포트 읽는 법, Claude 연동 |
| [docs/DEV_PC_MANUAL.md](docs/DEV_PC_MANUAL.md) | 개발자 PC 매뉴얼 — 현장 로그 복사본과 Claude 구독으로 원인 찾기 |
| [docs/INTERNALS.md](docs/INTERNALS.md) | 모듈 구조, 처리 여유 모델, MCP 서버·원본 로그 도구, 콘솔 내부 |
| [design-system/talog/MASTER.md](design-system/talog/MASTER.md) | 콘솔 디자인 시스템 — 근거, 토큰, 부품, 금지 점검표 |
| [CHANGELOG.md](CHANGELOG.md) | 버전별 변경과 그 근거(현장 재현 결과) |

## 명령

- 점검 한 번에: `python tools/check.py` (pyflakes · 스킬 사본 · 오프라인 페이지 · 테스트, `--fast` 는 테스트 빼고) — 커밋 전에 통과.
- 테스트: `python -m pytest -q` — 전부 통과가 기준. 콘솔 Claude 기능은 가짜 Claude CLI 로 시험한다(`tests/test_console_claude.py`).
- 콘솔: `python -m talog --config <talog.yaml> [--port 8778]` · 진단 리포트: `python -m talog analyze <로그 폴더>`
- Claude 연동 서버 점검: `python -m talog mcp --self-test`
- exe: `python -m PyInstaller talog.spec --noconfirm` (배포판은 `dist/talog.exe`)

## 지킬 것

- 사용자와 문서는 한국어(README.md·CONTRIBUTING.md 만 영어 — 사용자 요청 2026-10-14). 커밋 제목은
  `feat|fix|refactor|docs: v<버전> - <영문 요약>`.
- **고객 현장 데이터(로그·이미지·레시피)와 그 캡처는 저장소·외부 서비스에 올리지 않는다.** 분석 산출물·그림·보고서는
  `Result/`(git 제외)에 둔다.
- **Claude 연동은 구독 로그인만 쓴다.** API 키·과금 경로를 새로 만들지 않는다. 콘솔 '바로 묻기'는 `Console.claude_env` 로
  키를 빼고 띄우고, 쓰기는 에이전트 폴더 `reports` 안(`Edit(reports/**)`)만 허용한다.
- `talog/claude_kit/skills/talog-*` 와 `.claude/skills/talog-*` 는 같은 내용으로 유지한다(테스트가 확인). 스킬의
  `allowed-tools` 에는 읽기 도구만 둔다 — 쓰는 도구(`start_replay`·`save_case`)는 콘솔이 사용자 확인 뒤에만 연다.
- 작업은 master 에 바로 커밋·푸시한다(브랜치·PR 없이 — 사용자 요청 2026-10-14).
- 콘솔 화면 소스는 `talog/web/`(껍데기 `console.html` · `views/` · `partials/` · `css/NN-*.css` · `js/NN-*.js`)이고
  `talog/console/page.py` 가 요청 때 파일 하나로 합친다. 브라우저가 받는 페이지는 외부 리소스 없음(현장 오프라인 —
  테스트가 확인). 화면을 바꾸면 디자인 시스템 문서를 따르고, 대비는 계산으로, 배치는 1440·1024·375 폭과
  밝게/어둡게에서 직접 확인한다.
- 감지·판단 규칙을 고치면 현장 원본 재현(경보 재현)으로 전후를 비교하고 CHANGELOG 에 근거를 남긴다.
- `.claude/launch.json` 은 로컬 전용이다(커밋하지 않음).

## 매뉴얼 그림

`docs/manual/` 의 그림 3장은 archify 명세(`docs/manual/diagrams/*.json`)로 만든다 — 고친 뒤
`archify validate <type> <json> --quality showcase` 를 통과시키고 `deliver` 한다. 콘솔 화면 캡처가 든 HTML 판은
현장 데이터가 보여 저장소에 넣지 않는다.
