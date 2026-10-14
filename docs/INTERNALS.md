# talog 내부 구조 및 명세 (INTERNALS)

README 에서 분리한 상세 문서입니다: 파싱 명세, 모듈 구조, 진단 항목,
스케일 대응, 유지보수 방법.

## 빠른 시작 (현장용)

- `run_talog.bat` **더블클릭 = 콘솔**(감시·사건·분석·설정), **로그 폴더를 끌어다 놓기 = 진단 리포트**.
- Python 이 없는 PC 에서는 `dist\talog.exe` 를 같은 방식으로 사용한다.

```
talog.exe [--config talog.yaml]                 콘솔
talog.exe run [--check | --replay <일자> | --once]  화면 없이 상주
talog.exe analyze <로그 폴더> [--recipe <레시피>] [--out <출력>] [--open] [--fast] [--llm]
```

상세 사용법은 `USAGE.md` 참조.

- `<로그 폴더>`: 설비-일자 폴더(플랫폼 로그 + `alg\` 하위) 또는 날짜 하위 폴더들을
  가진 설비 폴더. 설비 폴더를 주면 일자별 리포트를 각각 생성한다.
- `--recipe`: `D:\AIV\MODEL\<제품>` (버전 폴더 자동 선택) 또는 버전 폴더 직접 지정.
  `--recipe-hint <문자열>` 로 특정 버전을 매칭할 수 있다.
- 출력: `<out>\<태그>.html` (진단 리포트), `<out>\<태그>.sqlite` (이벤트/검사 DB)

예:
```
python -m talog "H:\...\log\cmfb#1" --recipe "D:\AIV\MODEL\CMFB - V2" --out .\out
```

## 리포트 구성 (탭)

- **대시보드**: 요약 카드, 일자 타임라인(검사 틱 + 재시작▼/크래시✖/모델로드▲,
  틱 클릭 시 검사 상세로 이동), 시간대별 검사 수·평균 검사시간, 검사시간 분포,
  미완료/이상 검사 표(사유·레시피 조인)
- **검사 조회**: inner id / product id 검색 → 채널×단계 **간트 차트**
  (이상 검사 전건 + 검사시간 상위 + 최근 검사에 대해 내장; 그 외는 sqlite 조회)
- **Tact 분석**: 채널별 min/avg/p95/max + 하루 시계열 스파크라인 + GPU 배치
- **모델**: 설비 모델 로드 명령 이력(M2V→ACK 소요/결과), 채널별 모델 로드
  (Initialize 소요), **모델(가중치)별 Tact 통계**
- **에러**: 전 로그 에러/예외 집계 (exception.log `@원본파일` 역참조 그룹핑)
- **시스템**: 프로세스 세대(재시작 이력, kill/크래시/정상 종료 구분), 파싱 파일 목록

## 진단 항목 (P1)

- 검사 라이프사이클 조립: `M2V_INSPECT_START` → `START_ACK(OK/NoInspThread)` →
  채널별 Reset/전처리/인퍼런스/후처리 → `V2M_INSPECT_END`
- 미완료 사유 자동 분류: 시작 거부(NoInspThread) / 실행 중 소실(재시작·크래시로
  진행분 유실) / 미투입 채널 / 시뮬레이션(설비 신호 없는 실행) 구분
- 플랫폼 `LoggingRemainAlgs` 덤프를 레시피 alg 이름으로 번역
- 프로세스 세대(재시작 이력): WorkerThreadPoolMng + BatchRunLog + exception.log 교차
- 채널별 인퍼런스 Tact 통계(min/avg/p95/max) + 레시피 GPU(dev index) 표시
- 에러/예외 전수 집계 (exception.log `@원본파일` 역참조 그룹핑 포함)

## 구조

```
talog/
  lineparser.py    talos 라인 문법 스트리밍 파서 (멀티라인/인코딩 방어)
  fileclass.py     파일명 분류기 (핵심/대용량 파일 구분)
  recipe.py        레시피 파서 (ALG/ROI/DLMODEL ini → 설계도 모델)
  rules/events.yaml  이벤트 추출 룰 사전 (플랫폼 로그 포맷 변경 시 여기만 수정)
  events.py        룰 기반 이벤트 추출 엔진
  assemble.py      검사/채널 런 조립 + 미완료 사유 분류 + 프로세스 세대
  store.py         SQLite 스키마
  report.py        단일 파일 HTML 리포트
  cli.py           CLI — talog(콘솔) / run(상주) / analyze(리포트), 옛 명령은 별칭
  settings.py      사용자 설정(talog.yaml, 약 20키) ↔ 엔진 설정 변환 (compile / from_engine)
  watch.py         상주 감시 (LiveWatch: tail → RuleEngine → Notifier → 에이전트),
                   capacity_review(사후 리포트용 같은 판정)
  capacity.py      처리 여유 모델 — 그룹별 유휴 스레드·점유·투입 간격·풀·기준선·KPI·상태 저장
  tracker.py       추적 대상 선택(default/select/auto) + 로그 문구 규칙 엔진
  agent.py         사건 분석: 근거 수집 → 룰 진단 → LLM 2차 의견 → 합의 관문 → 숫자 대조
  rules/runbook.yaml  원인·조치·담당 런북 (LLM 이 읽는 원인 설명 포함)
  llm.py           Ollama 클라이언트 (cpu/gpu/auto 장치 선택, keep_alive, think)
  mailer.py        SMTP 발송 (즉시/묶음 정책, 역할 라우팅, 후속 메일 스레드, outbox)
  secret.py        SMTP 비밀번호 DPAPI 암호화
  console/         플랫폼 콘솔 서버 (127.0.0.1, 토큰·Host 확인) — v2.2 에서 파일 하나를 패키지로 나눔
    app.py         Console = 믹스인(ops·incidents·analysis·claude·checks) + 공유 상태(__init__)
    http.py        GET_ROUTES / POST_ROUTES 표 → Console 메서드, 리포트 서빙(/r/<태그>/ + 뷰어 API)
    page.py        talog/web 소스 → 페이지 HTML 하나 (소스가 바뀌면 다시 합침)
    claude.py      Claude Code 실행(바로 묻기·스킬 카드·연결 시험), 구독 전용 환경, 답 수치 대조
    jobs.py        진단 리포트·경보 재현을 `talog _job` 하위 프로세스(낮은 우선순위)로
  web/             콘솔 화면 소스 — console.html 껍데기 · views/ 화면 · partials/ 아이콘·대화상자 ·
                   css/NN-*.css · js/NN-*.js (번호 = 합치는 순서)
  mcp/             Claude 연동 MCP 서버 (stdio JSON-RPC 2.0, 의존성 없음) — 도구 18·프롬프트 5·자원
                   (런북·용어·최근 사건·리포트), 옛 판 initialize + 2026-07-28 무상태 요청, 자동완성
    registry.py    @tool 데코레이터 — 도구 하나 = 설명 + 함수 하나 (tools/ 의 영역 파일)
    tools/         incidents · reports(읽기 전용 SQL) · rawlogs(원본 로그) · replay · knowledge(런북·사례 KB)
    server.py      출처 고르기·마스킹(되찾기 대응표)·재현 작업·JSON-RPC·자원·자동완성
    kit.py         에이전트 폴더(--init-workspace) · selftest.py 설치 점검(--self-test)
  logread.py       원본 talos 로그 읽기 — 줄머리 시각으로 이분 탐색(수 GB 파일도 즉시), 창 읽기·찾기·
                   검사 id 추적, 읽기 예산(300줄·384MB·20초, 160MB 넘는 파일은 시각 범위 필수)
  claude_kit/      에이전트 폴더 원본 (CLAUDE.md·README·작업별 스킬 5개 talog-rca·logs·daily·site·rules,
                   공통 참고·보고서 템플릿). 저장소의 .claude/skills/talog-* 와 같게 유지 (테스트·tools/check.py)
  assets/          AIVEX 워드마크·아이콘(svg·ico·png) — tools/brand_assets.py 가
                   docs/brand/aivex_logo.ai 에서 만든다 (콘솔 머리·파비콘·exe·리포트)
```

## 설정 두 층 (v2.0)

사람이 정하는 값은 `talog.yaml`(settings.DEFAULTS 모양)에만 둔다. 엔진은 예전과 같은
세부 dict(`watch._DEFAULT_CFG` 모양)를 받는다.

```
talog.yaml ──settings.load──▶ 사용자 설정 ──compile──▶ 엔진 설정 ──▶ LiveWatch / 에이전트 / 메일
옛 watch.yaml ─(is_legacy)─▶ from_engine ─┘  (옮길 자리 없는 값 = advanced, compile 마지막에 병합)
```

- `from_engine` 은 `compile(from_engine(x)) == x` 가 되도록 차이를 `advanced` 로 남긴다
  (`_norm_engine` 으로 옛 결함 키·빈 역할 목록을 정규화한 뒤 비교). 옛 LLM 주기 점검 키
  (`llm.enabled/script/interval_min`)는 기능과 함께 없앴다.
- 사건 분석(에이전트)은 늘 켜져 있다. `ai.llm` 이 LLM 교차 확인만 켠다.
- 결함 규칙은 `rules.defect_watch.rules`(결함명 와일드카드·등급·N건/M분, 같은 결함명끼리
  계수) 한 형식이다. 엔진은 옛 `critical`·`repeat_count` 도 같은 규칙으로 옮겨 쓴다
  (`watch._defect_rules`). 등급 crit 는 `defect_critical`, 그 밖은 `defect_repeat` 로 발보.
- 메일 방식(`mail.provider`)이 엔진의 transport·호스트·비밀 키를 정한다. 비밀은 한 칸
  (`mail.secret`, DPAPI)이고, 콘솔은 방식·계정이 바뀌면 옛 비밀을 버린다.
- 리포트 질의(`/api/ask/report`)는 `ask.py` 도구 루프를 쓰고, 시스템 프롬프트에 실제 DB 열
  이름을 넣는다(`_schema_text` — 없으면 7B 모델이 열 이름을 지어내고 값까지 지어냄).
  답의 숫자·시각은 `agent.fact_check` 로 조회 결과와 대조해 화면에 경고한다.

## 사건 분석 흐름

```
TailReader(추적 모드) ─줄→ PatternEngine ─┐
             └─이벤트→ RuleEngine(기본 룰·결함명) ─→ Notifier(overrides·쿨다운)
                                                  ├→ 토스트/웹훅/JSONL
                                                  └→ IncidentAgent.submit
IncidentAgent: 묶음(심각=즉시 10초 / 그 밖=batch_seconds·메일 간격)
  → 경보 순간 검사 상태 사본 + 근거 스냅샷(메인 스레드)
  → 작업 스레드: GpuLogProbe(DLInfer 꼬리) → RuleDiagnoser → [즉시면 룰 판단 메일]
     → LLMAnalyst(서술→JSON 스키마, 한자 재요청) → gate(일치/불일치/오경보) → 후속 메일
  → incidents_YYYYMMDD.jsonl
```

- 원인 id 는 런북과 코드(RuleDiagnoser)가 짝이다. 새 원인은 런북 문단 + 판정 코드를
  같이 추가한다(LLM 만 쓰는 원인은 문단만으로도 동작하지만 합의 관문에서 늘 불일치가 된다).
- 근거의 검사 상태(진행 중·완료 소요)는 **경보 순간** 사본을 쓴다. 마감 전에 재시작이
  끼면 RuleEngine.pending 이 비워지기 때문이다(PC3 0727 09:28 → 09:28:46 kill).
- 리플레이는 같은 경로를 이벤트 시각으로 동기 실행하고, 묶음 마감 시각이 지나면 그 시각의
  상태로 마감한다(다음 이벤트가 한참 뒤여도 '미래' 로그가 근거에 섞이지 않음).

## 처리 여유 모델 (v2.1, capacity.py)

```
InspStarter 수신(groupId) ─┐            seq_N: [STRAT] … Processing … SendResult … CheckFinish
InspStarter 도착(유휴 W) ──┼→ (inner, 그룹) 진행 중 ──→ comm END (그룹 토큰) → 회신 기준 소요
comm 시작 회신(그룹 토큰) ─┘   └ 도착별 (그룹, W, 진행 중) → 풀 = max(p95(W+진행 중), seq 인스턴스 수)
                                  seq_N ↔ 그룹 대응은 도착과 seq 시작을 짝지어 투표로 배운다
CapacityModel: 그룹별 3시간 원자료 → 기준선(기동·기종 교체 + 워밍업 10분 뒤 첫 60분, 도착 100건↑)
               → capacity_drift / tact_drift / 거부 직전(즉시) → Notifier(meta.group) → 에이전트
               → 시간별 KPI(kpi_*.jsonl) · 기준선(capacity_state.json)
```

- 그룹 귀속 순서: InspStarter 수신 줄(같은 inner 의 수신은 도착 줄과 FIFO 로 짝) → comm 시작 회신의
  마지막 토큰(수신 줄이 없는 사이트, 같은 inner 의 도착이 여러 개면 회신 순서대로) → seq_N 시작.
  끝내 모르면 60초 뒤 그룹 0(단일)으로 넘긴다 — 그룹이 여럿 보이는 사이트에서는 가짜 그룹 0 을 만들지
  않는다. 진행 중 키는 `inner` 또는 `inner@그룹`, 1시간 넘게 안 끝난 진행 중은 소실로 보고 뺀다.
- 풀: comm END 는 결과 송신 시점이라 저장·DB 단계 검사가 진행 중에서 빠져 (W+진행 중)이 풀보다 1 작게
  나오곤 한다(풍산 side2 6 → 5). 그래서 최빈값이 아니라 95 분위를 쓰고, seq_N 의 CommonSeq 인스턴스
  수가 최대 W 이상·64 이하면 같이 본다(인스턴스 ID 가 고정인 옛 빌드는 1로 찍혀 걸러진다).
- 투표 관문: (W+진행 중) 투표는 그 그룹의 마지막 완료(INSPECT_END)가 max(10초, 최근 완료 소요 최대의
  2배) 안일 때만 받고, 진행 중도 그 시간 안에 시작된 것만 센다. comm 로그가 끊기거나(회전·복사 지연·미추적)
  완료 줄이 유실되면 진행 중이 쌓여 풀이 부푼다(Tenneco 0730: comm 이 10분 먼저 끊겨 풀 3 → 356).
- 실시간(LiveWatch.step)은 한 주기에 읽은 모든 파일의 이벤트를 시각순으로 정렬해 넣는다(같은 시각은
  파일·줄 순서). 짝짓기·진행 중 수가 시각순을 전제로 하고, 검증한 리플레이도 시각순이기 때문이다.
- seq↔그룹 투표: 같은 inner 의 도착이 아직 그룹을 못 받았으면(수신 줄 없는 사이트에서 seq 시작이 회신보다
  먼저 찍힘) 직전에 그룹이 정해진 앞 존 도착과 짝짓지 않고 회신을 기다린다 — 짝지으면 seq↔그룹이 한 칸씩
  밀리고 그 대응으로 도착까지 잘못 붙는다.
- 점유는 seq `CheckFinish done`(스레드 반환 시점)을 쓰고, 그 그룹에 seq 점유가 10분 안에 없을 때만
  comm 회신 기준 소요(END − 도착)를 쓴다. 단계 = 처리(시작~마지막 Algorithm done) · 결과 송신 ·
  후처리(저장·DB) — PROC_TIME/SEND_RESULT/SEQ_FINISH 를 seq 객체(스레드) ID 로 짝짓는다.
- 거부 직전: 기준 유휴 중앙값 3 이상 그룹에서 유휴 1(0 은 거부 그 자체라 NoInspThread 가 맡음).
- 근거(EvidenceBuilder.capture(경보)): capacity·trend(그룹별 기준선 대비 배율, 시간별 p50 순위상관,
  투입 간격 변화, 함께 늘어난 그룹)·phases·transient(경보 직전 60초에 평소 p99 의 1.3배를 넘은
  그룹)·resources(Talos·장비 CPU, 메모리 기울기). RuleDiagnoser 는 drift 1.2배↑ 또는 유휴 1↓ ·
  투입 ±10% · GPU 커널 1.5배 미만 · 단조 증가면 progressive_slowdown.
- 거부 계열 판정 순서: GPU 치명 오류 → 기동 직후 미초기화 → 타임아웃 후 슬롯 미반환 → 완료 신호 끊김
  (`completion_gap`: 마지막 완료 뒤 60초 넘게 시작만 20건 이상, 또는 10분 완료가 시작의 절반 미만인데 최근
  도착 4건의 유휴가 모두 1 이상·중앙값 ≥ 스레드 수 − 1) → 단건 정체(정체 경보, 거부 없음, 남은 검사 ≤ 스레드
  수의 절반, 최근 도착에 유휴 있음) → 시간 경과형 열화 → 처리량 포화. 완료 신호가 끊기거나 한 건만 남으면
  진행 중 경과가 소요 배율을 부풀려 처리량 포화처럼 보이므로 그보다 먼저 본다(Tenneco 0730 13:59, 0728
  15:53). 완료 수 `ends_10min` 은 (inner, 그룹) 단위.
- 풍산 2호기 10/12 교훈: 7B LLM 은 '알고리즘 단계' 를 '특정 알고리즘' 으로 읽고 결론을 바꿨다 —
  해석 문장은 '처리(시작~판정) … 각 단계는 여러 처리의 합' 으로 쓴다. 원인을 맞혀도 조치는 일반적인
  것을 고르므로(6회 중 5회) LLM 메시지는 원인 설명으로만 쓰고 조치는 런북 문장을 붙인다.
  담당자 문구는 `clean_message` 가 원인·조치 id 를 이름으로 바꾸고, 조치·계획 문장과 근거(capacity.
  tolerance_s, trend.interval_change_pct)에 어긋난 용량 주장('한계 초과'·'처리량 증가', 부정문은 둔다)을
  뺀다. 다 빠지면 gate 가 룰 문구를 쓴다. 원문·뺀 문장은 `message_raw`·`message_dropped` 로 남는다.
- 사후 리포트(`talog analyze`)는 `watch.capacity_review` 로 같은 판정을 돌려 '처리 여유' 소견을 내고,
  NoInspThread 는 검사 조립과 따로 센다(뒤 존의 거부가 앞 존의 '완료'에 묻히는 다존 설비). 여러 날을
  한 번에 분석하면 기준선이 날짜를 넘어 이어진다. 요약은 마지막 검사 시각 기준이고, 그 뒤 재시작으로
  세대가 비었으면 재시작 직전 세대 요약(`CapacityModel.prev_gen`)을 쓴다.

## 파싱 명세 (요약)

- 라인: `yyyy/MM/dd-HH:mm:ss.fff<TAB>[Level][Header][Id]<TAB>msg<CRLF>`
- `[Id]` 는 스레드 ID가 아니라 로깅 객체 인스턴스 주소(`(UINT_PTR)this`),
  static 호출은 0. 같은 값 = 같은 파이프라인 인스턴스 → 실행 짝맞춤 키로 사용.
- Tact 단위는 ms. `exception.log` 는 `메시지, @원본파일.log` 2중 기록.
- 대용량 파일(DLInfer/InspResult/ProcessingBlock 등)은 P1 에서 파싱하지 않음.

## 구현된 고도화 (P2-lite)

- **날짜 경계 스티칭**: 익일 폴더의 alg/comm 첫 1시간을 자동으로 이어붙여
  자정 직전 검사의 완료 여부를 복원 (f150 오탐 사례로 검증)
- **종속성 그래프 활용**: InspCondRelationGraph 의 DEACTIVATE 를 파싱해
  미투입 채널을 "정상 스킵(레시피 조건)" vs "이상 미투입" 으로 구분
- **모델 로드 추적**: comm 의 M2V_MODEL_LOAD*→V2M_MODEL_LOAD_ACK 쌍(소요/결과) +
  채널별 Initialize 쌍 + LoadModels 실패, 타임라인 마커 표시

## 자동 진단 (LLM 불필요)

리포트 생성 시 룰 엔진이 검증된 RCA 플레이북으로 소견을 자동 서술한다
(종합 페이지 최상단 + `<태그>_diagnosis.md`):
미완료-재시작 시간 상관, NoInspThread 스레드 고갈, 투입 주기 대비 병목 채널,
GPU 경합 열화(executeV2 p95/p10 배율), 모델 로드 실패, 메모리 증가 추세.

## 대화형 질의 (talog ask — 로컬 LLM / Claude API)

```
python -m talog ask <out 폴더|.sqlite> [--backend auto|ollama|claude]
                    [--model 이름] [--q "단발 질문"]
```

- **로컬(무료·오프라인)**: Ollama + `qwen2.5:7b` (RTX 3080 에서 검증).
  설치: https://ollama.com → `ollama pull qwen2.5:7b`. 서버 자동 감지.
- **Claude API**: 환경변수 `ANTHROPIC_API_KEY` 설정 시 `--backend claude`
  (기본 모델 claude-sonnet-5, 품질 최상).
- LLM 은 sql/log_lines 도구로 DB 와 원문 로그를 직접 조회해 근거와 함께 답한다.
  소형 모델이 SQL 을 텍스트로 내놓아도 자동 실행 폴백으로 루프가 이어진다.
- 시스템 프롬프트에 스키마 가이드(`talog/guide.py`)와 해당 일자의 자동 진단
  소견이 주입된다.

## Claude 연동 (v2.1~2.2 — mcp/ · console/claude.py)

**MCP 서버** `talog mcp --config <talog.yaml> [--config …] [--work-dir] [--kb] [--no-mask]`

- stdio JSON-RPC 2.0 을 직접 구현했다(SDK 없음, PyInstaller 그대로). 두 시대를 함께 받는다(v2.2):
  옛 판(2025-11-25·06-18·03-26·2024-11-05)은 initialize 핸드셰이크(모르는 판이면 2025-11-25 로 답함), 최신 판
  (2026-07-28)은 요청마다 `params._meta["io.modelcontextprotocol/protocolVersion"]` — 결과에 `resultType`·
  `_meta` serverInfo, 목록·읽기에는 캐시 힌트 `ttlMs`·`cacheScope`, 모르는 판은 -32022(지원 판 목록). `server/discover`.
  tools/list·call·prompts/list·get·resources/list·templates/list·read·completion/complete(사건 id·날짜·출처·일자 폴더·
  증상·리포트 태그)·ping·logging/setLevel. stdout 에는 프로토콜 줄만 쓰고 나머지 print 는 stderr 로 돌린다.
- 데이터 출처(Source) = talog.yaml 하나 = 현장 하나: 사건(alert_dir)·시간별 KPI·capacity_state·리포트(.md + .sqlite).
- 도구 18: overview · list_incidents · get_incident · capacity_trend · list_reports · get_report · query_report ·
  log_days · log_files · read_log · search_logs · trace_inspection · start_replay · job_status · rediagnose ·
  runbook · search_cases · save_case. 쓰기는 start_replay(작업 폴더)·save_case(사례 KB)뿐이고 나머지는
  readOnlyHint. 프롬프트 5: incident_rca · daily_review · site_health · log_investigation(day·symptom) · rules_compare.
  자원: talog://runbook · talog://guide · 최근 사건 20건 `talog://incident/<id>` · 리포트 `talog://report/<tag>`(+ 템플릿)
  — Claude Code 에서 `@talog:` 로 대화에 붙인다. 도구 하나 = `tools/` 의 `@tool` 함수 하나(registry.py).
- 원본 로그 (`logread.py`): 일자 폴더 = 설정의 로그 루트 아래(`YYYY_MM\DD`·바로 아래·루트 자체 중
  InspStarter·comm·alg 가 있는 폴더) + 콘솔에서 고른 폴더(`<작업 폴더>/log_roots.json` {selected, roots},
  `day='선택'` 으로 부름). `..`·절대 경로 패턴 거부, 루트 밖 폴더 거부. 시각 찾기는 줄머리
  `YYYY/MM/DD-HH:MM:SS.mmm` 로 64KB 단위 이분 탐색 — 4.4GB DLInfer 에서 0.02초. search_logs 는 바이트 단위
  소문자 비교(정규식 선택), 시각 범위 밖은 건너뛰고 예산을 넘으면 partial 로 알린다. trace_inspection 은 inner id
  에 박힌 시각(`20000 YYMMDD hhmmss seq`) ±window 로 핵심 파일·alg 로그를 시간순으로 합친다(없으면 InspStarter·
  comm 의 첫 등장 시각). 실측(풍산 10/12, 37GB): NoInspThread 05:00~05:05 찾기 2.1초·3건, 검사 추적 56줄 0.2초.
- 마스킹 되찾기: 토큰 → 원래 숫자 대응표를 work_dir `.mask_map.json` 에 원자적으로 저장(최대 20만). 도구 인자에
  `ID-xxxxxx` 가 오면 원래 값으로 바꿔 찾고, 모르는 토큰은 오류로 알린다.
- query_report: `mode=ro` 연결 + `PRAGMA query_only`, SELECT/WITH 한 문장만, attach·pragma 등 금지어, 20초 진행
  처리기, 최대 500행.
- 마스킹(Masker): 12자리 이상 숫자 → `ID-` + 소금 친 sha1 6자(소금은 work_dir 에 저장해 실행 간 같은 토큰),
  로그·데이터·작업 경로 → `<로그>`·`<데이터>`·`<작업>`. save_case 는 마스킹 중이면 긴 숫자를 거부한다.
- 재현 작업(Jobs): 작업마다 talog.yaml·capacity 시드를 복사해 `talog run --replay` 를 별도 프로세스로(동시 2개).
- `--self-test --json`: 서버 실행·도구·데이터 출처·마스킹·도구 호출 점검과 최근 사건 8건 → 콘솔 '연결 시험'.
- `--init-workspace <폴더>`: claude_kit 를 복사해 에이전트 폴더를 만든다(CLAUDE.md·README 는 있으면 보존,
  스킬·템플릿·.mcp.json 은 새로).

**콘솔 바로 묻기** (`Console.claude_ask` → `_ask_work`)

- 명령: `claude -p <질문> --mcp-config <폴더>/.mcp.json --strict-mcp-config --tools Read,Glob,Edit,Write,Skill
  --allowedTools <ASK_TOOLS> --permission-mode dontAsk --max-turns 60 --output-format stream-json --verbose --include-partial-messages
  -n "talog · <질문 앞부분>" [--model sonnet|opus|haiku] [--fallback-model sonnet(opus 일 때)] [--effort …] [--resume <세션>]`,
  작업 폴더 = 에이전트 폴더. 원본 로그 폴더를 골라 두면 질문 앞에 "day='선택' 으로 읽는다"는 한 줄을 붙인다
  (화면에는 질문만).
- 대화: `ask_thread = {session, turns}`. init 이벤트의 session_id 를 기억해 다음 질문을 `--resume` 으로 잇고,
  '새 대화'(`/api/claude/thread/new`, 답하는 중에는 거부)나 new=true 로 비운다.
- ASK_TOOLS = talog 읽기 도구 16개(원본 로그 5개 포함) + Read·Glob·Skill + `Edit(reports/**)`. 실측: `Write` 를 주면 폴더 루트
  (`.mcp.json`)에도 쓰고, `Edit(reports/**)` 는 reports 안만 허용하며 루트 쓰기는 permission_denials 로 거부된다.
- 실행 환경(`claude_env`): ANTHROPIC_API_KEY·ANTHROPIC_AUTH_TOKEN·ANTHROPIC_BASE_URL·CLAUDE_CODE_USE_BEDROCK/
  VERTEX/FOUNDRY·CLAUDECODE·CLAUDE_CODE_*(OAUTH_TOKEN·GIT_BASH_PATH 제외)·CLAUDE_AGENT_SDK_* 를 뺀다 → 구독
  로그인만 쓴다. init 이벤트의 apiKeySource("none" = 구독 로그인)를 화면에 보인다.
- stream-json 을 읽어 init(모델·talog 연결·인증·세션)·rate_limit_event(구독 사용률·종류·초기화 시각·초과 사용 →
  화면의 사용량 표시)·stream_event text_delta(흐르는 답 live, message_start 마다 비움)·assistant(내용 블록이 한
  이벤트에 같이 오든 따로 오든, 도구 호출 앞의 글은 진행 메모 note 로)·tool_use(진행 단계, 할 일 목록 같은 내부
  도구는 숨김)·user tool_result(단계 완료·오류·결과 앞 900자 미리보기·걸린 ms)·result(답·턴·막힌 동작)를 그
  턴에 담고, 끝나면 reports 아래 새로 생기거나 바뀐 .html·.md 를 링크로 낸다.
  stderr 는 임시 파일로 받는다(파이프가 차서 멈추지 않게). 20분을 넘기거나 멈추기를 누르면 `taskkill /T` 로
  자식(MCP 서버)까지 끝낸다. 로그인 오류는 로그인 안내로 바꿔 보인다.
- **스킬 카드(v2.2)**: `SKILL_CARDS` 다섯 장(rca·logs·daily·site·rules) — 입력 칸(사건·일자 폴더·시각·증상·날짜·
  재현 확인)을 받아 `/talog-<스킬> <인자>` 로 새 대화를 연다(`_skill_request`: 인자 줄바꿈·앞 '/' 제거, 형식 검사,
  고른 폴더는 '선택'으로만). `--tools` 로 셸·웹을 싣지 않아 시스템 프롬프트도 준다(Haiku 실측 2.1.143: 캐시 생성
  24,215 토큰). 재현은 rules 카드에서 확인했을 때만 그 질문의 허용 목록에 `mcp__talog__start_replay` 를 더한다
  (로그 루트 밖 폴더 거부). 에이전트 폴더의 스킬이 talog 판과 다르면 `_ask_ws` 가 연결·스킬을 다시 깐다.
- **답 수치 대조(v2.2)**: 그 대화의 도구 결과 원문(최대 1.5M자)과 질문·스킬 본문을 근거로 `ground_check` 가
  답의 수치·시각을 `agent.fact_check` 로 대조한다(경로·코드·사건 id·목록 번호 제외) → 턴의 `fact`
  {ok, checked, unsupported} → 화면 배지 '수치 근거 N개 확인' / '도구 결과에 없는 값 k개'.
- 엔드포인트: GET `/api/claude`(연결 정보·스킬 카드(skills)·가벼운 질문(quick)·모델·effort·도구(바로 묻기 허용 여부)·로그 폴더·CLI 판·
  로그인 흔적·대화 상태) · GET `/api/claude/ask`(대화 턴·이어 묻기 가능·사용량·고른 로그 폴더) · POST
  `/api/claude/ask` {q, model, effort, new, skill, args, allow_replay} · POST `/api/claude/ask/cancel` · POST `/api/claude/thread/new` ·
  POST `/api/claude/log` {path}(빈 값 = 해제, talos 일자 폴더나 그 상위만) · POST `/api/claude/prompt` {name, args}
  (MCP 프롬프트 문장 그대로) · GET `/api/claude/file`(reports 안 .html·.md 만) · POST `/api/claude/test` ·
  `/api/claude/workspace` · `/api/claude/open`.
- 사건 상세의 `day_dir` 은 사건 날짜로 로그 루트·고른 폴더에서 찾는다(재현 사건 포함, 없으면 빈 값 → '원본 로그로
  확인' 버튼 숨김).
- GET `/api/timeline?days=7`: 날짜·재현 여부별 24시간 배열 — 경보 [심각, 주의, 정보], 검사 수(한 검사가 모든 Seq
  그룹을 지나므로 그룹 합이 아니라 그룹 중 최대), 그룹 수, 거부, 최저 일시 지연 내성 %(그룹 중 최소), 마지막 기록
  시간의 그룹별 값(last). 오늘 줄은 기록이 없어도 낸다. 운영 현황의 상태 기록·추세·KPI·그룹별 여유가 쓴다.

## 콘솔 디자인 시스템 (v2.1~, talog/web)

기준 문서: `design-system/talog/MASTER.md` (UI/UX Pro Max 의 디자인 시스템 저장 형식 — 근거 행·토큰·부품·금지 점검표).

- 기준: UI/UX Pro Max 우선순위 규칙(접근성 → 상호작용 → 성능 → 스타일 → 반응형 → 글자·색 → 움직임 → 입력 →
  탐색 → 차트)과 제품 유형 추천 — 'Status Page / Incident Management'(#166: 실시간 운영 + 데이터 밀집, 상태
  매트릭스·사건 타임라인·심각도 배지·가동 기록, 피할 것: 느린 대시보드·장식 차트·숨은 오류 상태), 'AI/Chatbot
  Platform'(#18: AI 네이티브 + 미니멀, 흐르는 답·입력 중 표시·맥락 인식, 피할 것: 무거운 틀), 차트 추천(목표
  대비 = bullet, 이상 표시 선, 시간 × 날짜 = 히트맵). 파일 하나·외부 리소스 없음(현장 PC 오프라인).
- 셸: 어두운 사이드바(AIVEX 워드마크·설비·메뉴·빠른 이동·흐름 설명, 테마와 무관하게 잉크색) + 위 막대(화면
  제목·설명·감시 상태·감시 시작/정지·테마). Ctrl+K 빠른 이동(`<dialog>` + listbox, 화살표·Enter).
- 운영 현황: 상태 히어로(정상·주의·심각·폴더 없음·정지 — 색 막대 + 아이콘 + 문장) → KPI 4개(작은 추세선) →
  단계 흐름 → 상태 기록(날짜 × 24칸, 셀 색 = 그 시간 최고 등급, title·aria 요약·표 보기) → 처리 여유 추세(SVG 선,
  20%/40% 띠, 최저점 이름표, 컨테이너 너비로 다시 그려 글자 크기 유지) → 그룹별 bullet(점유 p99 / 한계, 60·80%
  구간) → 오늘 사건·경보 타임라인.
- Claude 작업 공간: 대화 줄(사용자 말풍선 · AI 블록 = 머리(모델·effort·이어서·구독·시간·턴) → 접히는 진행 과정
  (메모·도구 단계·결과 미리보기) → 답(작은 마크다운 변환기: 제목·목록·굵게·기울임·코드·표·구분선, 이스케이프
  먼저) → 복사·다시 묻기·AI 표시), 아래 고정 입력창(맥락 칩: 이어서 묻기·원본 로그 폴더, 모델·effort, Ctrl+Enter,
  멈추기), 오른쪽 맥락(원본 로그 폴더·빠른 작업·도구 묶음·데이터/비용 원칙). 0.9초마다 상태를 읽고 바뀐 턴만 다시
  그린다(펼친 단계는 data-k 로 기억), 끝나면 aria-live 로 알림.
- 클래스 이름 충돌 주의: 글자색 유틸리티(.crit/.warn/.ok)는 글자 요소에만 걸어 카드·히어로·노트의 상태 수식어와
  겹치지 않게 했다. 사건 상세 칸은 컨테이너 쿼리(620px 이하 한 줄).
- 토큰: 중립(배경·면·선·글자 4단) + 브랜드 주황(로고·현재 메뉴·초점 링·AI 표시) + 상태(심각·주의·정상·정보,
  각 점/글자/옅은 배경/선). 기본 버튼은 잉크색(#1d1d1d) — 주황 버튼이 빨간 '심각' 배지와 헷갈리지 않게.
  어두운 테마는 같은 이름의 토큰을 따로 정의(시스템 설정을 따르거나 머리 버튼으로 고정, `talog.theme`).
- 대비: `Result/ui_redesign/contrast_check.py` 로 계산 — 글자 4.5:1, 비텍스트(초점 링·상태 점·입력 테두리) 3:1,
  두 테마 모두.
- 아이콘: Lucide 형식 SVG 한 벌(`<symbol id="i-…">`, 24×24·선 1.8). 이모지·기호 문자 아이콘 없음(테스트로 확인).
- 상호작용: 주소 해시 라우팅(`#ops`·`#inc/<id>[/replay]`·`#ana`·`#ai`·`#set/<단계>`, 뒤로 가기, 화면 전환 시
  제목으로 초점), 키보드 초점 링·본문 건너뛰기, 표 행 Enter/Space, 버튼 진행 표시(aria-busy), 스켈레톤·빈 상태,
  확인 대화상자(`<dialog>`), 토스트(role=status/alert), prefers-reduced-motion.
- 반응형: 1024 이상 사이드바 · 768~1023 아이콘 레일 · 768 미만 아래 탭 5개(아이콘 + 이름), 휴대폰은 KPI·단계
  2열·입력창 한 줄, 터치 기기 44px 대상, 가로 스크롤 없음(375·390·900·1280·1440 에서 확인, 화면은
  `Result/ui_redesign/v2/screens`).

## 범용성 / 스케일

- 검증 사이트: 한국타이어 PC3(타이어), CTR 4설비(볼조인트), Tenneco(부싱,
  일 4.3만 검사·133만 이벤트) — 동일 바이너리로 처리.
- 고케이던스 대응: alg 이벤트는 파일 단위로 즉시 런 조립 후 해제,
  comm 의 MOTION/KEEP_ALIVE 홍수 제외, 3천 건 초과 시 타임라인 밀도 스트립,
  리포트 내장 목록 9천 건 상한(이상 건은 전수 유지). `- 복사본` 파일 자동 제외.

## 로드맵

- P5: 레시피 .dot 기반 종속성 그래프 시각화(노드 상태 색칠), 다설비 트렌드,
  과거 RCA 사례 지식베이스(RAG)
- exe 재빌드: `python -m PyInstaller talog.spec --noconfirm`

