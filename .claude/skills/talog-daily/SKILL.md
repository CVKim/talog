---
name: talog-daily
description: talog 하루 검토 — 하루치 경보·사건·처리 여유·진단 리포트를 훑어 놓친 징후·오경보·판단 불일치를 짚고 담당별 할 일로 정리한다. "어제 경보 검토", "오늘 어땠어", "하루 요약", "아침 점검" 같은 요청에 쓴다. 사건 하나를 깊게 볼 때는 talog-rca.
argument-hint: "[날짜 YYYYMMDD] [출처]"
allowed-tools: mcp__talog__overview, mcp__talog__list_incidents, mcp__talog__get_incident, mcp__talog__capacity_trend, mcp__talog__list_reports, mcp__talog__get_report, mcp__talog__query_report, mcp__talog__runbook, mcp__talog__search_cases, mcp__talog__rediagnose
---

# talog 하루 검토

범위: $ARGUMENTS

범위에서 **날짜**(YYYYMMDD)와 **출처**(overview 의 '출처' 이름)를 읽는다. 날짜가 비면 사건이 있는 가장 최근 날,
출처가 비면 전체. 공통 규칙은 `../talog-rca/reference.md`.

## 절차

1. **개요** `overview` — 출처별 날짜·사건 수·기준선·리포트. 고른 날짜와 출처를 먼저 말한다.
2. **사건 훑기** `list_incidents(day, severity='all')` — 심각부터. 사건이 많으면 원인별로 묶는다.
3. **판단 보기** 심각·판단 불일치 사건만 `get_incident` 로 연다(최대 5건). 룰·LLM 이 다르게 본 사건, 근거가 약한 경보
   (오경보 의심)를 짚는다.
4. **처리 여유** `capacity_trend(day)` — 여유가 줄어드는 그룹과 남은 내성(20% 아래면 다음 정지 전 조치 대상).
5. **리포트** `list_reports` → `get_report(tag)` 가 있으면 소견과 사건이 같은 이야기를 하는지 맞춰 본다.
6. **회귀** 규칙이 바뀐 뒤라면 `rediagnose(day)` 로 원인이 바뀌는 사건만 본다.
7. **정리** — 오늘의 결론 한 줄, 위험 순위(그룹·원인·근거 수치), 할 일(현장·설비·개발팀), 규칙 개선 제안.
   보고서는 reference.md 의 "보고서" 규칙대로 `daily_<YYYYMMDD>.html`.

답변은 결론 세 줄과 보고서 경로만.
