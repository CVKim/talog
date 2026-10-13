# talog 콘솔 디자인 시스템 — MASTER

UI/UX Pro Max 스킬의 "디자인 시스템 저장(persist)" 형식을 따른 기준 문서다. 화면을 고치거나 새 화면을 만들 때
먼저 이 문서를 보고, 화면 하나만 다르게 해야 하면 `design-system/talog/pages/<화면>.md` 에 차이만 적는다
(없으면 이 문서가 전부다). 구현: `talog/console.html` (파일 하나, 외부 리소스 없음 — 현장 PC 오프라인).

## 1. 근거 — 스킬 데이터에서 고른 행

| 데이터 | 행 | 내용 | talog 에서 |
|---|---|---|---|
| products | #166 Status Page / Incident Management | Data-Dense Dashboard + Real-Time Monitoring, 보조 Minimal & Swiss · Dark Mode. "Service status matrix. Incident timeline. Severity badges. SLA uptime history" | 운영 현황·사건 화면 |
| ui-reasoning | #166 (신뢰도 0.94) | 꼭: service-status-matrix · incident-timeline · severity-badges · uptime-history. 피할 것: slow dashboards · decorative charts · hidden error states. "operators need instant state recognition and a reliable incident chronology" | 상태 히어로, 상태 기록 히트맵, 경보 타임라인, 심각도 배지, 모든 목록의 스켈레톤·빈 상태·오류 상태 |
| products / ui-reasoning | #18 AI/Chatbot Platform | AI-Native UI + Minimal. "Conversational UI. Streaming text. Context awareness. Minimal chrome." 효과: Streaming text + Typing indicators. 피할 것: Heavy chrome + Slow response feedback | Claude 작업 공간 |
| charts | #5 Heatmap | "activity by hour × day" | 최근 상태 기록 (날짜 × 24시간) |
| charts | #10 Anomaly Detection | Line Chart with Highlights | 처리 여유 추세 (20%·40% 띠, 최저점 강조) |
| charts | #8 · #18 Performance vs Target | Bullet Chart (여러 KPI 나란히) | 그룹별 처리 여유 (점유 p99 / 한계) |
| ux-guidelines | #92 Disclaimer · #93 Streaming · #98 Feedback Loop | AI 생성 표시, 토큰 단위 흐르는 답, 다시 묻기 | 답 아래 "AI 가 만든 답 — 근거를 확인하십시오", 흐르는 답·입력 중 점, 다시 묻기 |

스킬이 권한 색 중 바꾼 것: #18 의 'AI 보라(#6366F1)' 대신 AIVEX 주황을 AI 강조색으로 쓴다(브랜드 하나, 색 늘리지 않음).
스킬의 `search.py` 는 내려받은 코드라 실행하지 않고 같은 CSV 행을 직접 읽었다.

## 2. 토큰

밝게/어둡게 두 벌을 같은 이름으로 `:root` 에 둔다. 화면 코드에는 날 색을 쓰지 않는다(상태 칸·차트도 토큰).

| 묶음 | 토큰 | 쓰임 |
|---|---|---|
| 면 | `--bg` `--surface` `--surface-2` `--sunken` `--hover` | 페이지 · 카드 · 보조 면 · 움푹한 면(입력·코드·빈 칸) · 마우스 올림 |
| 선 | `--line` `--line-2` `--line-strong` | 카드 테두리 · 버튼 테두리 · 입력 테두리(3:1) |
| 글자 | `--ink` `--ink-2` `--sub` `--mute` | 제목 · 본문 강조 · 본문 · 보조(4.5:1 이상) |
| 브랜드 | `--brand` `--brand-ink` `--brand-soft` `--brand-line` | 로고·현재 메뉴·초점 링·AI 표시·사용자 말풍선. 버튼 바탕으로 쓰지 않는다 |
| 기본 버튼 | `--primary` `--on-primary` | 잉크색(#1d1d1d / 어둡게는 밝은 회색) — 주황 버튼이 빨간 '심각'과 헷갈리지 않게 |
| 상태 | `--crit` `--warn` `--ok` `--info` + `-ink` `-soft` `-line` | 점·막대 / 글자 / 옅은 바탕 / 테두리. 상태는 늘 글자(심각·주의·정상·정보)나 아이콘과 함께 |
| 사이드바 | `--side-bg` `--side-ink` `--side-mute` `--side-active` | 테마와 무관하게 잉크색 사이드바 |
| 모양 | `--r-sm` 6 · `--r` 8 · `--r-lg` 12 · `--r-xl` 16, `--sh-1~3` | 배지 · 버튼·입력 · 카드 · 히어로·대화상자 |
| 움직임 | `--t1` 120ms · `--t2` 200ms, `--ease` | 색·그림자만 바꾼다(누름에 transform 없음). `prefers-reduced-motion` 이면 끔 |

대비는 `Result/ui_redesign/contrast_check.py` 로 계산한다 — 글자 4.5:1, 비텍스트(초점 링·상태 점·입력 테두리) 3:1,
두 테마 모두.

## 3. 글자

`Pretendard Variable → Segoe UI Variable → Segoe UI → 맑은 고딕 → Noto Sans KR → system-ui` (웹폰트 내려받지 않음).
본문 14px/1.55, 휴대폰 15px(입력 16px — iOS 확대 방지), 숫자는 `tabular-nums`, 코드·경로·id 는 `--mono`.
한국어 줄바꿈은 `word-break: keep-all`. 화면 제목 19px · 카드 제목 15px · KPI 26px(휴대폰 22px).

## 4. 셸

- 어두운 사이드바 248px: AIVEX 워드마크 · 설비 · 메뉴 5개(운영 현황·사건·분석·Claude·설정, 현재 화면 = 주황 막대) ·
  빠른 이동(Ctrl+K) · 흐름 설명. 1023px 이하 84px 아이콘 레일, 767px 이하 아래 탭 5개.
- 위 막대: 화면 제목·한 줄 설명 · 감시 상태 알약(ping) · 감시 시작/정지 · 테마(시스템·밝게·어둡게).
- 주소 해시가 화면을 가리킨다(`#ops` `#inc/<id>[/replay]` `#ana` `#ai[/setup]` `#set/<단계>`), 뒤로 가기 동작.

## 5. 부품

| 부품 | 규칙 |
|---|---|
| 상태 히어로 | 왼쪽 5px 상태 막대 + 아이콘 칸 + 제목·한 줄·메타 + 행동 버튼 하나. 정상·주의·심각·폴더 없음·정지 |
| KPI | 이름(아이콘) · 큰 수 · 한 줄 설명 · 작은 추세선. 값이 없으면 "—" 와 이유 |
| 상태 기록 | 24칸 × 날짜 줄, 칸 = 그 시간 최고 등급, title 에 수치, 줄마다 aria 요약, "표로 보기" |
| 선 차트 | 컨테이너 너비로 다시 그려 글자 11px 유지, 기준 띠, 이상점 크게 + 이름표(선과 겹치지 않게 옆에) |
| bullet | 바탕 구간(60·80%) + 값 막대 + 오른쪽 수치, 가장 빠듯한 순 6개 + 표 |
| 사건 목록 | 날짜 머리 + 버튼 행(심각도·제목·시각·원인·판단·메일), 찾기·심각도 칩(건수) |
| 탭 | role=tablist, 화살표 이동, 선택 = 주황 밑줄 |
| AI 블록 | 아바타 · 머리(모델·effort·이어서·구독·시간·턴) · 접히는 진행 과정(메모·도구 단계·결과 미리보기) · 답 카드 · 복사·다시 묻기 · AI 표시 |
| 입력창 | 아래 고정, 맥락 칩(이어서 묻기·원본 로그 폴더, 지우기 ×) · 모델·effort · Ctrl+Enter · 멈추기/보내기 |
| 대화상자 | 되돌릴 수 없는 동작만(감시 정지·시험 메일·변경 되돌리기·분석 멈추기). 위험 동작은 '취소'에 초점 |
| 토스트 | 위 오른쪽(휴대폰은 아래), 성공 4.5초·오류 9초, role=status/alert |

## 6. 하지 말 것 (점검표)

- 이모지·기호 문자를 아이콘으로 쓰지 않는다 — Lucide 형식 SVG 한 벌(`<symbol id="i-…">`, 테스트가 확인).
- 장식 차트·그라디언트 배경·유리 효과를 넣지 않는다(#166 anti-pattern). 차트는 판단에 쓰는 것만.
- 상태를 색만으로 나타내지 않는다. 오류·빈 상태를 숨기지 않는다.
- 클래스 이름을 일반어로 겹쳐 쓰지 않는다 — 글자색 유틸리티(`.crit/.warn/.ok`)는 글자 요소에만 걸리고, AI 카드
  격자는 `.ai-acts`, 진행 과정은 `.ai-steps`, 흐르는 답은 `.streaming`(위 막대의 `.live` 와 구분).
- 폴링으로 다시 그릴 때 사용자가 펼친 것·선택한 글을 지우지 않는다 — 바뀐 턴만 다시 그리고 펼침 상태는 `data-k`.
- 가로 스크롤 금지 — 375·390·768·900·1280·1440 에서 확인(`Result/ui_redesign/v2/screens`).
