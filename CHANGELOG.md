# Changelog

## 1.5.0 (2026-08-05)

- **프리미엄 UI 전면 개편**: 다크 테마 기본 + 라이트 토글(localStorage
  유지, 인쇄 시 자동 라이트). 제품형 헤더(그라디언트 로고·버전 칩·sticky
  블러), 세그먼트 탭, 필 칩 서브내브
- **히어로 KPI**: "설비 상태 — 심각 N건" 대형 카드가 첫 시선을 받고,
  이상/NG=주황·에러=적색 시맨틱 정리, 단위(s) 축소 표기
- **소견 카드 재설계**: 풀틴트 제거 → 뉴트럴 서피스 + 좌측 액센트 보더 +
  심각도 배지 + 아이콘 칩, 심각→주의→참고→정상 정렬(낮은 심각도는 1줄
  컴팩트), 권장조치 구분선, 본문 96ch 제한
- **디자인 크리틱 패널 반영**(위계/타이포/색 3렌즈): 라이트 상태 텍스트
  대비 토큰 분리(WCAG), 상태 필 배지 클래스화, inner id 모노스페이스,
  Initialize 테이블 스크롤 컨테이너+소요 미니바+지브라, 종속성 그래프 빈
  이미지 컬럼 접기+연결선 대비 상향, 로드 명령 "응답 없음" 텍스트 신호
- 차트 테마 대응: 시리즈 색 CSS 변수화(다크 전용 스텝), 라인 차트 영역
  채움, 타임라인/간트/그래프 SVG 다크 렌더링. fleet 인덱스 동일 셸 적용

- **GPU 리소스 섹션** (상세 › 시스템): GPU0/GPU1 개별 — ① NVML 스냅샷
  (`[GPU STATUS]` — VRAM 사용량·온도, PC3 실측 29만건에서 **최고 89°C 임계
  초과** 검출), ② 모델 로드 시점 VRAM(cudaMemGetInfo 델타, console.log 의
  `GPU #N` 로드 이벤트로 물리 GPU 보정), ③ CUDA 메모리풀(usedCur),
  ④ GPU 전역 락 대기(WaitForCriticalSection), ⑤ TalogWatch gpu_*.jsonl
  조인(사용률 % 포함 — 로그에 없는 유일 지표). 진단 룰 3종 추가(온도 임계,
  락 대기 p95, 비상주 모델 안내)
- **에러 컨텍스트 아코디언**: 에러성 이벤트(ERROR/CRASH/MODEL_FAIL 등
  10종)에 원본 로그 전후 ±3줄 발췌를 저장(events.context)하고 에러/예외
  전체 탭에서 행마다 펼쳐보기 제공
- **레시피 인퍼런스 플래그**: DLMODEL.ini 의 on memory infer(상주)/use
  patch infer/AthenaModelType/alg blocks name 파싱 → 모델·GPU 탭에
  "레시피 모델 구성" 표(비상주 모델 경고 포함), DB recipe_models 확장
- console.log 를 core 파싱 대상으로 승격(물리 GPU 배치의 유일 소스),
  GPU 계측 홍수 균등 샘플링 확장, AI_GUIDE 스키마 갱신. 테스트 65개

## 1.3.0 (2026-08-05)

- **리포트 비주얼 전면 리디자인**: 검증된 데이터 시각화 팔레트로 교체
  (CVD 안전 검증 완료 — 계열 blue `#2a78d6`/orange/aqua, 상태색
  good/warning/serious/critical 분리). 라이트 서피스 토큰 시스템(:root CSS
  변수), 컴팩트 KPI 타일, 언더라인 탭, 헤어라인 테이블(hover 워시 +
  tabular-nums), 8px 라운드 바, 대문자 마이크로 섹션 헤더
- 상태색 의미 정리: 완료 green / 거부·미완 critical red / 소실 serious
  orange / 절단·시뮬 부분 muted / 시뮬 완료 aqua
- fleet 인덱스·추이 차트도 동일 팔레트 적용
- cmfb#1/MR 실로그를 설비 레시피(CMFB, 1163-M7AA)와 조인 재검증
  (종속성 그래프·모델명 매핑·DEACTIVATE 분류 활성화)

## 1.2.0 (2026-08-03)

- **NG 판정 분포**: INSPECT_END NG 페이로드의 결함명을 파싱해 리포트 종합에
  결함별 발생 분포 차트 + NG 카드 + 검사 조회 판정 컬럼 (c1xx 실측: NG 39건,
  DOT_GREASE_MISS 19건 등)
- **fleet 모드**: `talog fleet <루트>` — 설비 폴더 여러 개를 일괄 분석하고
  통합 인덱스(설비별 추이 + NG 열) 자동 생성·열기
- **검사 조회 UI**: 상태 필터 버튼(전체/이상만/NG/완료/절단) + CSV 내보내기
  (Excel 호환 BOM)
- **watch 상태 페이지**: `alert_dir\status.html` 30초 자동 갱신 — 현장 모니터에
  띄워두는 가동 상태/최근 경보/GPU 온도 대시보드
- `--version` 플래그, 인덱스 생성기 패키지화(talog/fleetindex), 아키텍처
  다이어그램 화살표 직각 라우팅 정리

## 1.1.0 (2026-07-30)

- **존(그룹) 단위 완료 판정**: 다존 설비(Tenneco 4존)에서 ACK 받은 모든 존이
  END 를 수신해야 complete — 실데이터에서 "반쪽 검사" 17건 신규 검출
- **소스 코드 감사**: talos-platform/vision 대조로 37룰 검증, NG END status
  오추출 수정, 코드 검증 신규 룰 12종(BusyCam/NotReady/Sim 거부, ALG/IMG
  타임아웃, GRAB_FAIL, STORAGE_LOW, LIGHT_UNSTABLE, TRT 누락, INFER_ERROR,
  RuleBase 블록 실패, CPU 백엔드, OCR Tact 폴백)
- **사례 지식베이스**: `talog kb add/list/search`, 리포트에 유사 과거 사례
  자동 첨부 (Ollama 임베딩 + 키워드 폴백)
- **watch**: GPU 온도 감시(nvidia-smi, 임계 경보 + 이력 JSONL), `--check`
  설치 자가 점검, seq 로그 감시 + 신규 장애 즉시 경보, 시뮬 세션 집계
- **현장 안정화**: 배치 파일 ASCII 재작성(인코딩·UNC·exe 탐색 결함 수정),
  CLI 우아한 오류 처리, `tools/make_deploy.py` 배포 키트 생성기

## 1.0.0 (2026-07-30)

첫 제품 릴리스. 5개 사이트(타이어 PC3 · CTR 볼조인트 4설비 · Tenneco 부싱)
실로그 9만+ 검사로 검증.

### 사후 분석 (talog)
- talos 로그 스트리밍 파서: 라인 문법·인코딩 방어, 64종 파일 자동 분류
- YAML 룰 사전 기반 이벤트 추출 (플랫폼 로그 포맷 변경 시 YAML만 수정)
- inner id 단위 검사 조립 + 레시피(ALG/ROI/DLMODEL) 조인
- RCA 자동 진단 엔진: 미완료↔재시작 상관, NoInspThread, 투입주기 대비 병목,
  GPU 경합(executeV2 p95/p10), 모델 로드 실패, 메모리 추세
- 인터랙티브 단일 HTML 리포트(탭 2개): KPI·타임라인·미완료 사유·간트·
  종속성 그래프·채널/모델 Tact·GPU 추이·에러·CPU/RAM
- 검사 상태 분류: complete / incomplete(_lost) / rejected / sim_* /
  in_progress_eof(로그 절단 — comm 커버리지 기준 판정)
- 날짜 경계 스티칭(익일 첫 1시간), 고케이던스(일 4만+ 검사) 스케일 대응
- SQLite DB + 진단 마크다운 + index 생성기(make_index)

### 대화형 질의 (talog ask)
- 로컬 Ollama(qwen2.5 등, CPU/GPU 선택) / Claude API 겸용 tool-use 루프
- sql / log_lines 도구로 DB·원문 근거 기반 답변, 소형모델 SQL 자동실행 폴백
- `--llm`: 룰 진단 소견을 AI 종합 소견 문단으로 (한국어 강제 재시도)

### 예지보전 (talog watch)
- 증분 tail 상주 감시(저부하: BELOW_NORMAL, 소형 로그 6종만)
- 감지 룰 5종 + 크래시 즉시 경보, 룰별 쿨다운
- 알림: Windows 토스트 / 웹훅(JSON POST) / alerts JSONL
- LLM 감시 지시문 모드(기본 CPU, num_gpu=0로 검사 GPU 보호)
- `--replay`: 과거 사고 재생 검증 (PC3 0727: 미검사 33분 전 사전 경보 재현)

### 패키징
- run_talog.bat / run_watch.bat (드래그&드롭), PyInstaller 단일 exe
