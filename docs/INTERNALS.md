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
  watch.py         상주 감시 (LiveWatch: tail → RuleEngine → Notifier → 에이전트)
  tracker.py       추적 대상 선택(default/select/auto) + 로그 문구 규칙 엔진
  agent.py         사건 분석: 근거 수집 → 룰 진단 → LLM 2차 의견 → 합의 관문 → 숫자 대조
  rules/runbook.yaml  원인·조치·담당 런북 (LLM 이 읽는 원인 설명 포함)
  llm.py           Ollama 클라이언트 (cpu/gpu/auto 장치 선택, keep_alive, think)
  mailer.py        SMTP 발송 (즉시/묶음 정책, 역할 라우팅, 후속 메일 스레드, outbox)
  secret.py        SMTP 비밀번호 DPAPI 암호화
  console.py       플랫폼 콘솔 서버 (console.html: 운영 현황·사건·분석·설정) — 127.0.0.1,
                   토큰·Host 확인, 리포트 서빙(/r/<태그>/ + 뷰어 API), 사건·리포트 질의
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

