---
name: talog-rules
description: talog 규칙을 바꾼 뒤 비교 — 저장된 사건을 지금 규칙으로 재진단(rediagnose)해 원인이 바뀐 사건을 보이고, 사용자가 허락하면 그날 로그를 지금 규칙으로 재현(start_replay)해 경보가 언제·몇 건 나는지 전후를 비교한다. 재현은 파일을 쓰고 수 분 걸리므로 사용자가 직접 부를 때만 쓴다.
argument-hint: "[출처] [일자 폴더] [재현 허용]"
disable-model-invocation: true
allowed-tools: mcp__talog__overview, mcp__talog__list_incidents, mcp__talog__get_incident, mcp__talog__rediagnose, mcp__talog__job_status, mcp__talog__runbook, mcp__talog__capacity_trend
---

# talog 규칙 바꾼 뒤 비교

요청: $ARGUMENTS

요청에서 **출처**, **일자 폴더**(로그 루트 아래 이름, 예: `12`·`2026_10\12`), **재현 허용** 여부를 읽는다.
`재현 허용` 이라는 말이 없으면 재현을 시작하지 않는다. 공통 규칙은 `../talog-rca/reference.md`.

## 절차

1. **계획** — 비교 범위(출처·날짜)와 재현을 할지 먼저 쓴다. 재현은 메일 없이 작업 폴더에만 쓰고, 하루치에 수 분~수십 분
   걸린다는 것도 같이 말한다.
2. **재진단** `rediagnose(source, day)` — 저장된 사건 근거에 지금 코드의 룰 진단을 다시 돌려 원인이 바뀐 사건만 본다.
   바뀐 사건은 `get_incident` 로 근거를 열어 어느 쪽이 맞는지 판단한다.
3. **재현** (요청에 `재현 허용` 이 있을 때만) `start_replay(source, day, carry_baseline=true)` → `job_status(job_id)`.
   - `job_status` 는 몇 번만 확인한다. 끝나지 않았으면 job id 와 "이어서 묻기로 결과를 비교할 수 있다"고 쓰고 멈춘다.
   - 끝났으면 재현 사건을 `list_incidents(source='재현:<job id>')` 로 보고, 원래 사건과 시각·심각도·원인을 표로 맞춘다.
   - `start_replay` 가 막히면(콘솔에서 허락하지 않음) 재현 없이 재진단 결과로만 정리하고, 재현이 필요한 이유를 쓴다.
4. **비교표** — 사건마다 '전(저장된 판단) → 후(지금 규칙)': 시각 · 심각도 · 원인 · 판단 근거 수치. 새로 생긴 경보,
   사라진 경보, 늦어지거나 빨라진 경보(거부까지 남은 시간)를 따로 센다.
5. **판정** — 바뀐 규칙이 낫다/나쁘다/같다와 근거. 나빠진 사건이 있으면 문턱값 되돌리기나 보완을 제안한다.
6. **보고서** — reference.md 의 "보고서" 규칙대로 `rules_<YYYYMMDD>.html`.

답변은 결론 세 줄과 보고서 경로(재현을 돌렸으면 job id 도)만.
