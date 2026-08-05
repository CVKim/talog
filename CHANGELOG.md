# Changelog

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
