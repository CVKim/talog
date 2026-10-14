# talog 콘솔 디자인 시스템 — MASTER

UI/UX Pro Max 스킬의 "디자인 시스템 저장(persist)" 형식을 따른 기준 문서다. 화면을 고치거나 새 화면을 만들 때
먼저 이 문서를 보고, 화면 하나만 다르게 해야 하면 `design-system/talog/pages/<화면>.md` 에 차이만 적는다
(없으면 이 문서가 전부다). 구현: `talog/web/` 소스(`css/00-tokens.css` 토큰 · `css/2x` 부품 · `css/3x` 화면 ·
`js/NN-*.js` 화면별 동작 · `views/*.html` 화면 마크업)를 `talog/console/page.py` 가 파일 하나로 합쳐 내보낸다
(외부 리소스 없음 — 현장 PC 오프라인).

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
| ux-guidelines (v2.2) | #105 Consistent Help · #116 Compact Label Overflow(마우스 올림 전용 툴팁 금지) · #88 Onboarding User Freedom | 도움은 늘 같은 자리, 누르는 풀이, 건너뛸 수 있는 안내 | 위 막대 (?) 버튼·? 키 → 도움말 서랍(이 화면·용어 사전·단축키), 용어 옆 풀이 버튼, 처음 안내 6단계(건너뛰기·이전) |
| ux-guidelines (v2.2) | #80 Error Recovery · #79 Empty States · #81 Progress Indicators · #37 Color Only · #113 Essential Text Truncation | 다음 단계 제시, 단계 표시, 색만으로 알리지 않기, 핵심 글 줄이지 않기 | 운영 현황 '지금 할 일', 분석 ① 폴더 ② 할 일 ③ 결과, KPI 상태 낱말(여유·빠듯·위험), 사건 요약 네 칸 |
| charts (v2.2) | #5·#8·#10·#18 Accessibility Notes · A11y Fallback | "글 주석·서술 요약", "구간마다 글 이름표", "강도 요약 + 표" | 차트마다 데이터로 만든 '요약' 한 줄, 처리 여유 막대 구간 이름표(여유·빠듯·위험·한계), 기존 '표로 보기' 유지 |

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
| 용어 풀이 (v2.2) | `term("키")`·정적 마크업은 `<span data-tip="키">` — 글 옆 작은 (?) 버튼, 누르면 풀이(Esc·바깥 누르기로 닫고 초점 복귀). 풀이 글은 `js/13-help.js` 의 GLOSSARY 한 곳 (테스트가 키를 확인). 버튼 안에는 넣지 않는다 |
| 도움말 서랍 (v2.2) | 위 막대 (?)·? 키·Ctrl+K. 오른쪽 서랍 `dialog.drawer`: 이 화면 읽는 법(HELP_VIEWS: 순서·자주 묻는 것·용어 칩) · 용어 사전(찾기) · 단축키 · 처음 안내 다시 보기 |
| 캡션 (v2.2) | 차트·표 아래 `.sum` — "요약 ·" + 그림과 같은 데이터로 만든 한 줄(색 + 아이콘 + 낱말). 고정된 해설은 `.howto`(읽는 법). 지난 기록·재현 값을 '지금'으로 말하지 않는다 |
| 지금 할 일 (v2.2) | 상태 히어로 안 `.next` — 상태에서 사람이 할 일을 급한 순서로 최대 4개(감시 정지·심각 사건·여유 20%↓·오늘 폴더 없음·메일 실패·LLM 응답 없음·메일 꺼짐), 줄마다 이동 버튼. 없으면 "할 일 없음" |
| KPI (v2.2) | 이름(+풀이) · 큰 수 · 상태 낱말 칩 `.kst`(여유·빠듯·위험 / 확인 필요) · 한 줄 해설 `.kx` · 추세선 |
| 사건 요약 (v2.2) | 판단 탭 맨 위 `.isum` 네 칸: 무슨 일 · 왜(원인) · 지금 할 일(첫 권고 조치, 강조) · 판단 확신(규칙·AI 관계를 쉬운 말로) |
| 단계·선택 카드 (v2.2) | 분석 `.steps-h`(① 폴더 ② 할 일 ③ 결과, aria-current=step) + `.ana-opt` 카드(하는 일·걸리는 시간·밖으로 나가는지). 스킬 카드에도 남기는 것·시간(`.skl-t`) |
| 처음 안내 (v2.2) | 오른쪽 아래 비모달 `dialog.tour`, 6단계, 짚는 곳 주황 테두리(`.tour-hl`, 다시 그려져도 되살림), 건너뛰기·이전·다음, 이 브라우저에 한 번(localStorage, 실패해도 동작) |

## 6. 하지 말 것 (점검표)

- 이모지·기호 문자를 아이콘으로 쓰지 않는다 — Lucide 형식 SVG 한 벌(`<symbol id="i-…">`, 테스트가 확인).
- 장식 차트·그라디언트 배경·유리 효과를 넣지 않는다(#166 anti-pattern). 차트는 판단에 쓰는 것만.
- 상태를 색만으로 나타내지 않는다. 오류·빈 상태를 숨기지 않는다.
- 클래스 이름을 일반어로 겹쳐 쓰지 않는다 — 글자색 유틸리티(`.crit/.warn/.ok`)는 글자 요소에만 걸리고, AI 카드
  격자는 `.ai-acts`, 진행 과정은 `.ai-steps`, 흐르는 답은 `.streaming`(위 막대의 `.live` 와 구분).
- 폴링으로 다시 그릴 때 사용자가 펼친 것·선택한 글을 지우지 않는다 — 바뀐 턴만 다시 그리고 펼침 상태는 `data-k`.
- 가로 스크롤 금지 — 375·390·768·900·1280·1440 에서 확인(`Result/ui_redesign/v2/screens`).
- (v2.2) 전문 용어만 덩그러니 두지 않는다 — 쉬운 말을 먼저 쓰고 용어에는 풀이 버튼. 숫자에는 상태 낱말과 한 줄 해설.
- (v2.2) 차트·표를 그림만으로 두지 않는다 — 같은 데이터로 만든 '요약' 한 줄과 구간 이름표. 재현·지난 기록을 '지금'이라 쓰지 않는다.
- (v2.2) 휴대폰 위 막대 버튼은 넷까지(감시 상태 점·감시 버튼·도움말·테마) — 더 넣으면 제목이 가려진다(375px 실측).
