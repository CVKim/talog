---
name: talog-rca
description: talog(talos 비전 검사 로그 분석기) 사건 하나의 RCA — 판단·룰 근거·처리 여유 추세·리포트 DB·원본 로그를 talog MCP 도구로 확인하고, 원인 판단과 현장 조치를 담은 독립 HTML 보고서를 남긴다. "talog 사건 분석", "NoInspThread 원인", "이 경보 왜 났어" 같은 요청이나 talog 사건 id(예 20261012-050228-capacity_drift)가 나오면 쓴다. 하루치 검토는 talog-daily, 원본 로그만 볼 때는 talog-logs, 현장 규칙 점검은 talog-site.
argument-hint: "[사건 id]"
allowed-tools: mcp__talog__overview, mcp__talog__list_incidents, mcp__talog__get_incident, mcp__talog__capacity_trend, mcp__talog__list_reports, mcp__talog__get_report, mcp__talog__query_report, mcp__talog__log_days, mcp__talog__log_files, mcp__talog__read_log, mcp__talog__search_logs, mcp__talog__trace_inspection, mcp__talog__job_status, mcp__talog__rediagnose, mcp__talog__runbook, mcp__talog__search_cases
---

# talog 사건 RCA

대상 사건: $ARGUMENTS

talog 는 현장 PC 에서 룰 + 로컬 LLM 으로 경보·사건을 만든다. 이 스킬은 그 사건 하나를 talog MCP 도구(`mcp__talog__*`)로
깊게 본다. 비용은 사용자의 Claude 구독 안에서만 든다. 원인 표·용어·예시 SQL·보고서 규칙은 [reference.md](reference.md).

## 시작 전에

- talog MCP 도구가 없으면 멈추고 안내한다: talog 콘솔 → **Claude** → **연결 설정**의 "Claude Code 연결" 명령을
  실행한 뒤 Claude Code 를 다시 열어야 한다.
- 대상 사건이 비었으면 `list_incidents(severity='crit')` 로 가장 최근 심각 사건을 고르고, 무엇을 골랐는지 먼저 말한다.

## 절차

1. **계획** — 확인할 것을 3~5줄로 먼저 쓴다.
2. **사건** `get_incident(incident_id=…)` — 판단(룰·LLM 일치 여부)·원인·룰 근거·해석 문장·경보·근거 데이터.
3. **교차 확인** (사건 종류에 맞게 고른다)
   - `capacity_trend(source, day, groups=[사건 그룹])`: 기준선 대비 점유가 언제부터 늘었나, 내성(한계 − p99)이 0 근처로
     떨어진 시각, 거부 시각, 투입 간격이 그대로인가.
   - `get_report(tag)` → `query_report(tag, sql)`: 거부·검사 시간·재시작·특정 시각 전후를 DB 로 확인.
   - `runbook(family='reject')`: 원인 정의와 배제 조건 — 룰과 같은 어휘로 말한다.
   - `search_cases(query)`: 비슷한 과거 사례.
   - **원본 로그** — 요약 근거로 부족하거나 사용자가 원본을 원할 때만. 순서는 reference.md 의 "원본 로그 읽는 순서".
   - 규칙을 고쳤거나 "그때 경보가 언제 났을까"를 볼 때는 talog-rules 스킬로 넘긴다.
4. **판단** — 룰 원인에 동의하는지 밝히고 근거를 3개 이상 든다. 다른 원인은 왜 아닌지 한 줄씩. 숫자·시각은 도구
   결과에 있는 값만 쓰고, 추정은 "추정"이라고 쓴다.
5. **조치** — runbook 의 조치 문장을 기준으로 **지금 / 다음 정지 때 / 개발팀** 으로 나눈다. 진행 중 검사가 있으면
   지금 재시작을 권하지 않는다. "언제까지 괜찮다"는 예측은 쓰지 않는다(reference.md 의 용어 — 거부는 일시 지연 한 번으로 난다).
6. **보고서** — reference.md 의 "보고서" 규칙대로 `rca_<사건 id>.html` 과 `evidence.md` 를 남긴다.
7. **사례** — 결론이 확정되면 `save_case` 로 사례 KB 에 남길지 사용자에게 묻는다(직접 저장하지 않는다).
   고객명·id 없이 일반화해서 쓴다.

## 답변 형식

결론 세 줄(원인 · 룰 판단과 같은지 · 가장 급한 조치)과 보고서 경로만 쓴다.
