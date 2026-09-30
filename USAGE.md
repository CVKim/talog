# talog 사용 설명서

talos 계열 검사 설비(타이어·볼조인트·부싱·미러 등) 로그를 자동 분석하여
**인터랙티브 HTML 리포트 + 자동 진단 소견 + 조회용 DB**를 생성하고,
로컬 LLM으로 자연어 질의까지 지원하는 로그 분석기입니다.

- 저장소: https://github.com/CVKim/talog (버전은 `talog.exe --version`)
- 검증 사이트: 한국타이어 PC3, CTR 4설비(c1xx·cmfb#1·f150·v710), Tenneco
  부싱, MR 미러 (일 4.3만 검사·300만 라인급까지 검증)
- **설치 불필요** — `talog.exe` 는 Python 없이 Windows 에서 바로 실행됩니다.

---

## 1. 가장 간단한 사용법

### 방법 A — 드래그&드롭
`run_talog.bat` 위에 **로그 폴더를 끌어다 놓기** → 레시피 경로 입력(없으면 Enter)
→ 완료되면 리포트가 자동으로 열립니다.

### 방법 B — 명령줄 (exe, Python 불필요)
```
dist\talog.exe "H:\...\log\cmfb#1" --recipe "D:\AIV\MODEL\CMFB - V2" --open
```

- 로그 폴더는 **설비-일자 폴더**(`...\29`) 또는 **설비 폴더**(`...\cmfb#1`,
  날짜별 일괄 생성) 모두 가능
- 결과: `<출력폴더>\<태그>.html`(리포트), `.sqlite`(DB), `_diagnosis.md`(소견)
- 출력 폴더 기본값은 `<로그폴더>\talog_out`, `--out` 으로 지정 가능

### 주요 옵션
| 옵션 | 설명 |
|---|---|
| `--recipe <폴더>` | 레시피 조인 (alg번호→결함명 번역, GPU 배치, 스킵 판정) |
| `--open` | 완료 후 리포트 자동 열기 |
| `--llm` | 로컬 LLM(Ollama) **AI 종합 소견**을 리포트 상단에 추가 |
| `--fast` | 대용량 종속성 그래프 로그 생략 (속도 우선) |
| `--detail N` | 간트 상세 내장 검사 수 (기본 60) |

여러 설비 폴더를 한 번에 돌리고 통합 인덱스까지 만들려면:
```
talog.exe fleet <설비 폴더들이 있는 루트>   → 설비별 리포트 + index.html
```

**설비 여러 대를 한 번에 (fleet 모드)**:
```
talog.exe fleet "H:\...\log"        → 루트 아래 설비 폴더 전체 분석
                                      + 통합 인덱스 자동 생성·열기
```

**리포트 안에서**: 검사 조회 탭에 상태 필터(전체/이상만/NG/완료/절단)와
**⬇ CSV 내보내기** 버튼이 있어 필터된 목록을 Excel 로 바로 가져갈 수 있습니다.
종합 탭의 **NG 판정 분포**에서 결함명별 발생 건수를 확인할 수 있습니다.

**watch 상태 페이지**: 감시 가동 중 `D:\AIV_LOG\TalogWatch\status.html` 이
30초마다 자동 갱신됩니다 — 현장 모니터/브라우저에 띄워두면 가동 상태·최근
경보·GPU 온도가 실시간으로 보입니다.

---

## 2. 리포트 읽는 법 (탭 2개)

### 종합 탭 — 열자마자 이것만 보면 됩니다
1. **KPI 카드**: 검사 수/완료/이상/에러/평균·최대 검사시간/재시작 (클릭 시 상세 이동)
2. **AI 종합 소견**(--llm 시) + **자동 진단 소견**: 룰 엔진이 원인을 문장으로 서술
   - `[심각]` 미완료와 재시작의 시간 상관, 모델 로드 실패, 메모리 증가 추세
   - `[주의]` 병목 채널, GPU 경합 열화(한산↔혼잡 배율)
   - `[참고]` 로그 절단 구간 등
3. **타임라인**: 검사 밀도 + 재시작▼/크래시✖/모델로드▲ 마커 (클릭 시 해당 검사로)
4. **미완료/이상 검사 표**: 사유(소실 채널·REMAIN 덤프·NoInspThread)까지 표시
5. 모델별 평균 검사시간 막대 / 에러 요약 / 메모리 추이

### 상세 분석 탭
검사 조회(inner id 검색 → **채널별 간트**) · **종속성 그래프**(아래) ·
채널 Tact(시계열 스파크라인) · 모델·GPU(로드 이력, executeV2 추이) ·
에러 전체 · 시스템(재시작 이력, CPU/RAM)

**종속성 그래프**: 레시피의 이미지→ROI→알고리즘 연결을 그대로 그립니다.
기본은 일자 요약(파랑=당일 실행, 주황=소실 발생 채널)이고, 검사 조회에서
inner id 를 선택하면 그 검사의 상태(완료/소실/이상 미투입/정상 스킵)로
노드가 색칠되어 "어느 갈래에서 끊겼는지"가 한눈에 보입니다.
레시피가 없으면 관측된 ROI→알고리즘 관계로 축약 표시됩니다.

### 검사 상태의 의미
| 상태 | 의미 | 이상 집계 |
|---|---|---|
| 완료 | 설비에 END 회신 완료 | — |
| 실행 중 소실 | 재시작/크래시로 진행분 증발 | 포함 |
| 시작 거부 | NoInspThread (스레드 고갈) | 포함 |
| 미완료 | comm 로그가 온전한데 END 없음 | 포함 |
| **로그 절단(판정 불가)** | 완료 신호 커버리지(comm) 밖에서 시작 — 로그를 다시 뜨면 완료 확정 | 제외 |
| 시뮬레이션 | 설비 신호 없는 수동/시뮬 실행 | 제외 |

※ "로그 절단"은 로그를 **가동 중에 복사**하면 파일별 절단 시각이 달라(실측
최대 13분) 생기는 것으로, 실제 미완료가 아닙니다. 익일 폴더가 있으면 첫
1시간을 자동 스티칭해 완료로 확정합니다.

---

## 3. LLM 자연어 질의 (talog ask)

```
python -m talog ask <출력폴더 또는 .sqlite> --q "미완료 검사 원인은?"
python -m talog ask <출력폴더>                       ← 대화형(REPL)
```

- **로컬(기본, 무료·오프라인)**: Ollama + qwen2.5:7b (설치 완료, RTX 3080 사용)
  - 다른 모델: `--model qwen2.5-coder:14b`(SQL 강화), `exaone3.5:7.8b`(한국어 특화),
    `deepseek-r1:14b`(복합 추론) — `ollama pull <이름>` 후 사용
- **Claude API(품질 최상)**: 환경변수 `ANTHROPIC_API_KEY` 설정 후 `--backend claude`
- LLM은 로그 원문이 아니라 **구조화 DB를 SQL로 조회**하고 필요 시 원문 라인만
  창 단위로 열람합니다 (대용량 로그를 통째로 넣지 않는 3단 파이프라인).

---

## 4. 자주 하는 작업 레시피

| 하고 싶은 것 | 방법 |
|---|---|
| "어제 미검사 왜 났어?" | 리포트 열기 → 자동 진단 소견 → 미완료 표에서 inner id 클릭 → 간트 |
| 특정 바코드 추적 | 상세 분석 → 검사 조회에 inner id 입력 |
| 어떤 모델이 느린지 | 종합의 모델별 막대 → 클릭 → 모델·GPU 상세(executeV2 추이) |
| 메모리 릭 의심 | 시스템 탭 RAM 추이 (자동 판정 배지) ※ 설비 LogConfig.ini 에서 `Process Usage Log=1` 필요 |
| 재시작이 몇 번? 누가? | 시스템 탭 프로세스 세대 표 (kill 스크립트/크래시/정상 구분) |
| 임의 분석 | `.sqlite` 를 DB 도구/파이썬으로 직접 쿼리 (스키마: AI_GUIDE.md) |
| 로그 수집 시 주의 | 가급적 설비 유휴 시간에 복사, `BatchRunLog.txt` 포함, 익일 폴더도 함께 |

---

## 5. 예지보전 상주 감시 (talog watch)

설비 PC에서 백그라운드로 돌며 `D:\AIV_LOG\Talos\<YYYY_MM>\<DD>\` 를 실시간
추적하고, 이상 징후를 **별도 예지보전 로그(`D:\AIV_LOG\TalogWatch\`)와
토스트 팝업/웹훅**으로 알립니다.

```
run_watch.bat                          ← 더블클릭 (설정: watch.yaml)
talog.exe watch --config watch.yaml    ← 명령줄
talog.exe watch --replay <일자폴더>    ← 과거 사고 재생으로 룰 검증
```

**저부하 설계** (검사 프로그램 보호):
새로 쓰인 바이트만 증분 읽기(20초 주기) · 소형 플랫폼 로그 6종만 감시 ·
프로세스 우선순위 자동 강등 · LLM은 선택 기능이며 **기본 CPU 모드**
(`num_gpu=0`)라 검사용 GPU를 건드리지 않습니다.

**감지 룰** (watch.yaml 또는 콘솔에서 임계 조정):
동일 에러 반복 · NoInspThread(미검사 임박, 즉시 — InspStarter 거부 라인과 comm
설비 회신 둘 다) · 검사 정체(정상 소요 중앙값의 2배) · 재시작 빈발 · 메모리 증가
추세(릭 의심) · GPU 온도 · 타임아웃·그랩 실패·저장 공간·조명 · **결함명 감시**
(치명 결함·빈발·연속 NG·NG 비율) · **사용자 정의 로그 패턴**

**LLM 감시 지시문(스크립트) 모드**: `watch.yaml` 의 `llm.enabled: true` +
`llm.script: watch_script_example.txt` 처럼 자연어 지시문 파일을 주면, 주기
(기본 30분)마다 현재 상태 요약을 LLM(CPU/GPU 선택)에 넘겨 지시문 관점으로
점검하고, 이상 판단 시 알림을 발송합니다.

### 5-1. 웹 콘솔 (v1.10) — 설정·상태·테스트를 화면으로

```
run_console.bat                        ← 더블클릭 (감시 + 콘솔, http://127.0.0.1:8778)
talog.exe watch --ui --config watch.yaml [--port 8778] [--no-open]
```

| 탭 | 하는 일 |
|---|---|
| 상태 | 오늘 심각/주의 경보·사건·메일 수, GPU(nvidia-smi), 최근 경보·사건, 테스트 경보 주입 |
| 추적 파일 | **기본**(핵심 9종) / **선택**(파일·와일드카드 체크) / **자동**(일자 폴더 전체) |
| 경보 규칙 | 기본 룰 켜기·등급·"N분 내 N회" · 치명 결함명(레시피 결함명 목록에서 선택) · 사용자 정의 로그 패턴(프리셋·줄 붙여넣기 시험) |
| 분석·LLM | 사건 분석 켜기, Ollama 주소·모델, **CPU / GPU / 자동** 장치, 속도 시험 |
| 이메일 | SMTP 프리셋(Gmail·Microsoft 365·네이버·사내 릴레이), 인증, 수신자·역할별 수신자, 등급별 정책, 접속 확인·테스트 메일 |
| 사건 기록 | 룰 진단·LLM 의견·합의·메일 미리보기 (리플레이 사건 포함) |
| 리플레이 | 과거 사고 폴더를 **현재 설정으로 재생** — 경보·메일을 미리 확인 (발송 없음) |

콘솔은 이 PC(127.0.0.1)에서만 열리고, 저장 시 `watch.yaml` 을 `.bak` 으로 백업한 뒤
주석을 붙여 다시 쓰고 감시를 새 설정으로 재시작합니다.

### 5-2. 경보 규칙 — 결함명·패턴·등급

- **치명 결함명** (`rules.defect_watch.critical`): comm.log 판정 NG 의 결함명이 목록에
  있으면 **1건만 나와도 즉시 심각**. 와일드카드 `*` 허용. 빈발(`repeat_count`)·연속 NG
  (`ng_streak`)·NG 비율(`ng_rate_window`)은 0 = 끔
- **사용자 정의 패턴** (`rules.patterns`): 추적 중인 파일의 모든 줄을 검사
  ```yaml
  patterns:
    - name: GPU 컨텍스트 치명 오류
      match: enqueueV3 cudaGetLastError    # 문구 포함 / 정규식은 "re:..."
      files: [DLInfer.log]                  # tracking 에 포함돼 있어야 함 (select/auto)
      severity: crit                        # info | warn | crit
      count: 1                              # window_min 안에 N회 → 경보 (1 = 즉시)
      window_min: 10
  ```
- **기본 룰 조정** (`rules.overrides`): `{img_timeout: {count: 3, window_min: 10},
  grab_fail: {severity: warn}, alg_timeout: {enabled: false}}`
- **등급 → 메일**: 심각(`email.immediate`)은 10초 뒤 즉시(룰 판단), 주의는 묶음, 정보는 기록만

### 5-3. 사건 분석 에이전트 — 룰 진단 + LLM 2차 의견

`agent.enabled: true` 면 경보를 사건으로 묶어 근거(경보 순간의 진행 중 검사·소요·
투입 간격·재시작·타임아웃·에러·NG 분포·GPU, 사건 시점 DLInfer.log 꼬리)를 모으고
원인을 판단합니다. 원인·조치·담당 사전은 `talog/rules/runbook.yaml`
(문구 교체: `agent.runbook`).

| 판단 | 의미 | 메일 |
|---|---|---|
| 룰·LLM 판단 일치 | 규칙과 LLM 이 같은 원인 | 지목된 담당 역할에 권고 조치와 함께 |
| 판단 불일치 — 담당자 확인 필요 | 두 의견이 다름 | 두 의견을 모두 적고 확인 요청 조치 추가 |
| 오경보 의심 | 근거가 경보 기준에 못 미침 (예: 재시작 1회를 5회로 센 경보) | 주의로 강등 (기본 메일 제외) |

LLM 이 쓴 알림 문구의 숫자·시각은 근거 데이터와 대조해, 근거에 없으면 룰 문구로
바꿉니다. 기록: `alert_dir\incidents_YYYYMMDD.jsonl`.

**LLM 장치** (`llm.device`): `cpu`(기본, 검사 GPU 미사용, `cpu_threads` 로 스레드 상한) /
`gpu` / `auto`(요청마다 여유 VRAM `gpu_min_free_mb`·사용률 `gpu_max_util` 을 보고 선택).
특정 GPU 만 쓰려면 그 GPU 로 고정한 전용 Ollama 서버를 띄우고 `llm.url` 을
`http://127.0.0.1:11435` 로 바꿉니다 (bat 파일 예):
```
set CUDA_VISIBLE_DEVICES=1
set OLLAMA_HOST=127.0.0.1:11435
set OLLAMA_VULKAN=0
ollama serve
```
(`OLLAMA_VULKAN=0` 이 없으면 Vulkan 백엔드가 다른 GPU 를 잡을 수 있습니다.)
이 경우 `device: auto` 의 여유 VRAM 판정도 그 GPU 로 하도록 `llm.gpu_index` 를 같은 번호로
맞추십시오. 모델이 이미 GPU 에 올라가 있으면(`/api/ps`) auto 는 그대로 GPU 를 씁니다.

### 5-4. 이메일 — SMTP·인증

```yaml
email:
  enabled: true
  smtp_host: smtp.gmail.com     # 사내 릴레이면 security: none + username 비움
  smtp_port: 587
  security: starttls
  username: sender@example.com
  to: [line-leader@example.com]
  roles: {vision_engineer: [vision@example.com], quality: [quality@example.com]}
```
- **비밀번호는 파일에 평문으로 쓰지 않습니다**: 콘솔에 입력하면 Windows DPAPI 로
  암호화해 `password_dpapi` 에 저장(이 PC·이 사용자만 복호화), 또는 환경변수
  `TALOG_SMTP_PASSWORD` (`setx TALOG_SMTP_PASSWORD "앱비밀번호"`)
- Gmail 은 2단계 인증 후 **앱 비밀번호**, Microsoft 365 는 조직이 SMTP AUTH 를 막았으면
  사내 릴레이를 사용
- 확인: `talog.exe watch --check` (접속·인증만) → `talog.exe watch --test-email` (예시 메일 발송)
- 처음 설치할 때는 `dry_run: true` 로 두면 SMTP 없이 `alert_dir\outbox\*.eml` 만 남습니다

**검증 실적**: PC3 0727 사고 리플레이에서 — 새벽 00:27 모델 로드 실패 반복
경보, **08:55 검사 정체 사전 경보(사고 33분 전)**, 09:28:03 NoInspThread
즉시 경보, 09:28:54 크래시 감지, 09:29 재시작 빈발 경보.

**부팅 시 자동 시작 등록** (선택):
```
schtasks /Create /TN "talog watch" /SC ONLOGON /TR "E:\talos-log-analyzer\run_watch.bat" /RL LIMITED
```

## 6. 유지보수

- **플랫폼 로그 메시지가 바뀌면**: 코드가 아니라 `talog\rules\events.yaml` 만 수정
- **exe 재빌드**: `python -m PyInstaller talog.spec --noconfirm` → `dist\talog.exe`
- **판정 기준 튜닝 위치**: 로그말미 5분(`assemble.py` 300), GPU 경합 4배/메모리
  100MB/h(`diagnose.py`), 간트 내장 60건(`--detail`)
- 문서: `README.md`(구조/명세), `AI_GUIDE.md`(DB 스키마·LLM 가이드)

## v0.1 범위와 다음 버전 후보

포함: 파서·레시피 조인·자동 진단·인터랙티브 리포트·날짜 스티칭·커버리지 판정·
GPU/메모리 분석·로컬 LLM 소견·ask·exe/bat 패키징 (5개 사이트 실검증)

v0.2 진행: **종속성 그래프 시각화 완료** (레시피/관측 겸용, 검사별 상태 색칠)

v0.2 잔여 후보: 다설비 트렌드 대시보드, 과거 RCA 사례 지식베이스(벡터 검색),
Tenneco 그룹(존) 단위 완료 판정 세분화
