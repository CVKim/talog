# talog

**AI 비전 로그 운영 플랫폼** — talos 검사 설비 PC 에 상주하며 로그를 실시간으로 읽고,
이상을 탐지해 원인을 판단하고, 담당자에게 알립니다. 같은 화면에서 로그 폴더 진단
리포트와 자연어 질문까지 처리합니다.

![version](https://img.shields.io/badge/version-2.0.0-orange)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![platform](https://img.shields.io/badge/platform-Windows-lightgrey)
[![CI](https://github.com/CVKim/talog/actions/workflows/ci.yml/badge.svg)](https://github.com/CVKim/talog/actions)

## 컨셉 — 한 에이전트, 한 흐름

```
 수집            탐지              판단                           알림              조사
 오늘 로그 폴더 → 경보 규칙 한 표 → 룰 진단 ─┬─ 합의 관문 ─→ 메일·토스트     → 진단 리포트
 증분 tail       기본 17 + 사용자  (결정)    │  일치/불일치    심각 즉시        → 자연어 질문
 (저부하)        결함명·로그 문구   Qwen ────┘  오경보 의심     LLM 후속 메일      (숫자 대조)
                                  (교차 확인, CPU/GPU/자동)
```

- **룰이 결정하고 LLM 은 교차 확인만** 합니다. 둘이 같은 원인일 때만 "판단 일치"로 보내고,
  다르면 두 의견을 붙여 확인을 요청합니다. LLM 이 쓴 숫자·시각은 근거와 기계 대조합니다.
  (Robot_Sim 운영 에이전트 벤치: 규칙이 9B LLM 보다 정확, LLM 단독 조치는 해로운 비율이 높음)
- 심각 경보는 10초 안에 룰 판단으로 바로 메일이 가고, LLM 의견은 같은 스레드의 후속 메일로 갑니다.

## 쓰는 법

| | |
|---|---|
| `run_talog.bat` 더블클릭 | **콘솔** — 운영 현황 · 사건 · 분석 · 설정 (http://127.0.0.1:8778) |
| 로그 폴더를 `run_talog.bat` 에 끌어놓기 | 그 폴더의 **진단 리포트** |
| `run_service.bat` | 화면 없이 상주 (자동 시작용) |

```bash
talog                         # 콘솔
talog run                     # 화면 없이 상주 감시 (--check 설치 점검, --replay <일자> 사고 재현)
talog analyze <로그 폴더>      # 진단 리포트 (일자·설비·여러 설비 폴더 자동 판별)
```

설정은 `talog.yaml` 하나(약 20키: 수집·탐지·판단·알림)이며 콘솔의 설정 화면에서 고칩니다.
옛 `watch.yaml` 은 그대로 읽고, 콘솔에서 저장하면 값 그대로 새 형식으로 바뀝니다.
개발 환경은 `pip install -e .` 후 `talog` 명령을 씁니다.

## 핵심 특징

- **소스 검증 탐지** — talos-platform/vision 소스와 대조한 이벤트 룰(`talog/rules/events.yaml`):
  NoInspThread·시작 거부 4종·판정 미송신·그랩·타임아웃·재시작·정체·메모리 추세·GPU 온도·NG
- **사용자 규칙** — 치명 결함명(레시피 결함명에서 클릭) · 로그 문구/정규식, "1회 즉시" 또는 "M분 내 N회"
- **사건 판단** — 경보 순간의 검사 상태·GPU 로그·자원으로 근거를 모아 런북(`talog/rules/runbook.yaml`)의
  원인·조치·담당을 고르고, 로컬 Qwen(Ollama) 2차 의견과 합의 관문으로 가름
- **메일** — Microsoft 365(Graph, OAuth2) · Gmail(앱 비밀번호) · 사내 SMTP, 역할별 수신자,
  비밀번호는 Windows DPAPI 암호문으로만 저장
- **조사** — 검사별 판정·RCA·간트·Tact 의 단일 HTML 리포트, 리포트 DB 에 자연어 질문(LLM 이 SQL 조회,
  답의 숫자를 조회 결과와 대조), 과거 사고를 현재 규칙으로 재현
- **저부하** — 증분 tail 20초 · 우선순위 강등 · LLM 기본 CPU(검사 GPU 미사용)

## 검증

5개 사이트(타이어·볼조인트·부싱) 실로그 9만+ 검사 — 원문 전수 감사 판정 오류 0건, 플랫폼 소스 대조
37룰, 사고 재현에서 미검사 33분 전 사전 경보, PC3 0727·Tenneco 0730·c1xx 0729 사건 원인 룰 판정 일치.
pytest 111개.

## 문서

- [USAGE.md](USAGE.md) — 시작하기 · 콘솔 화면 · 설정 · 판단 결과 · 리포트 읽는 법 · 설치
- [docs/INTERNALS.md](docs/INTERNALS.md) — 모듈 구조 · 설정 두 층 · 사건 분석 흐름 · 파싱 명세
- [CHANGELOG.md](CHANGELOG.md) — 변경 이력
