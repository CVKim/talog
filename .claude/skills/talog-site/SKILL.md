---
name: talog-site
description: talog 현장 점검·규칙 조정 — 현장별 처리 여유 기준선·재시작 빈도·경보 경향을 보고 규칙·문턱값이 그 현장에 맞는지(오경보·놓침), 로그 수집에 빠진 파일이 없는지 평가해 조정안을 우선순위로 낸다. "현장 점검", "규칙이 맞아?", "문턱값 조정", "오경보 줄이기" 같은 요청에 쓴다. 고친 규칙의 효과 비교는 talog-rules.
argument-hint: "[출처] [보고 싶은 점]"
allowed-tools: mcp__talog__overview, mcp__talog__list_incidents, mcp__talog__get_incident, mcp__talog__capacity_trend, mcp__talog__rediagnose, mcp__talog__list_reports, mcp__talog__get_report, mcp__talog__query_report, mcp__talog__runbook, mcp__talog__log_days, mcp__talog__log_files
---

# talog 현장 점검·규칙 조정

대상: $ARGUMENTS

대상에서 **출처**(overview 의 '출처' 이름)와 사용자가 특히 보고 싶은 점을 읽는다. 비면 전체 출처를 본다.
공통 규칙은 `../talog-rca/reference.md`.

## 절차

1. **개요** `overview` — 날짜·사건·기준선·리포트·로그 루트.
2. **기준선과 추세** `capacity_trend` 를 날짜별로 — 그룹별 기준선, 점유·유휴 추세, 재시작으로 기준선이 자주 다시
   잡히는지(세대가 짧으면 기준선을 못 잡는다), 남은 내성.
3. **원인 분포** `list_incidents` 로 원인·심각도 분포, `rediagnose` 로 지금 규칙에서 원인이 바뀌는 사건.
4. **규칙 맞춤 평가** `runbook()` 의 판정 문턱값과 비교해:
   - 오경보 의심: 근거가 문턱값 바로 위에서만 나는 경보, 같은 그룹에서 반복되는데 거부로 이어지지 않는 경보.
   - 놓침 의심: 거부(NoInspThread)가 있었는데 그 전에 주의 경보가 없던 날.
   - 수집 누락: `log_days`·`log_files` 에서 seq_N·InspStarter·comm 이 빠진 날.
5. **조정안** — 문턱값·재알림 간격·워밍업 같은 설정 값을 '지금 값 → 제안 값 · 근거 · 예상 효과'로 쓴다.
   설정 파일을 직접 고치지 않는다. 제안을 적용한 뒤에는 talog-rules 로 비교하라고 안내한다.
6. **보고서** — reference.md 의 "보고서" 규칙대로 `site_<YYYYMMDD>.html`.

답변은 결론 세 줄(가장 중요한 조정안 포함)과 보고서 경로만.
