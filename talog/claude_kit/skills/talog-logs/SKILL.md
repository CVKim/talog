---
name: talog-logs
description: talos 원본 로그를 talog MCP 도구로 직접 읽어 증상(시각·문구·검사 id)의 경과와 원인을 찾는다 — 일자 폴더·파일·시각 범위를 좁혀 search_logs·read_log·trace_inspection 으로 시간순 경과표를 만든다. "원본 로그 봐 줘", "05:02 전후 로그", "이 검사 id 따라가 줘", "exception 언제부터" 같은 요청에 쓴다. 사건 판단까지 볼 때는 talog-rca.
argument-hint: "[일자 폴더] [시각] [증상]"
allowed-tools: mcp__talog__log_days, mcp__talog__log_files, mcp__talog__read_log, mcp__talog__search_logs, mcp__talog__trace_inspection, mcp__talog__overview, mcp__talog__list_incidents, mcp__talog__get_incident, mcp__talog__runbook
---

# talog 원본 로그 조사

요청: $ARGUMENTS

요청에서 **일자 폴더**(예: `선택`, `12`, `2026_10\12`), **시각**(예: 05:02), **증상**(문구·검사 id·오류)을 읽는다.
일자가 `선택`이면 콘솔에서 고른 폴더다. 비어 있으면 `log_days` 의 첫 폴더(가장 최근)를 쓰고, 증상이 비면 그날 가장
심각한 이상(NoInspThread·exception·timeout)을 찾는다. 공통 규칙은 `../talog-rca/reference.md`.

## 절차

1. **계획** — 볼 파일과 시각 범위를 3~5줄로 먼저 쓴다. 하루 폴더가 수십 GB 다 — 통째로 훑지 않는다.
2. **폴더·파일** `log_days` → `log_files(day)` — 파일 분류(핵심/대용량)·크기·첫/마지막 시각을 보고 볼 파일을 고른다.
3. **찾기** `search_logs(day, query, time_from, time_to)` — 증상 시각 ±5분부터 시작해 필요할 때만 넓힌다. 핵심 로그에 먼저:
   `NoInspThread`, `Waiting thread count = 0`, `exception`, `timeout`, `CheckFinish done`, `INSPECT_END`.
   160MB 넘는 파일은 시각 범위가 있어야 찾는다.
4. **앞뒤 문맥** `read_log(day, file, at=시각, before, lines)` — 의미 있는 줄 앞뒤를 읽는다. 한 번 최대 300줄.
5. **검사 하나** `trace_inspection(inspection_id)` — 도착·유휴 스레드·Seq 시작·알고리즘·판정·결과 송신·거부를 시간순으로.
   가린 id(`ID-xxxxxx`) 그대로 넘긴다.
6. **talog 판단과 맞추기** — 그 시각 사건이 있으면 `list_incidents(day)`·`get_incident` 와 맞춰 보고, 룰 판단과 원본
   로그가 같은 이야기를 하는지 쓴다.
7. **정리** — 시간순 경과표(시각 · 파일 · 일어난 일), 원인, 근거 원문(짧게 인용), 조치(`runbook` 문장 기준).
   보고서는 reference.md 의 "보고서" 규칙대로 `logs_<HHMM>.html`.

## 조심할 것

- 도구 한 번이 20초를 넘으면 끊긴다 — 시각 범위를 좁혀 다시 부른다. 같은 찾기를 범위만 넓혀 되풀이하지 않는다.
- 원본 줄을 길게 붙여 넣지 않는다 — 근거는 줄 몇 개만 인용한다.
- 답변은 결론 세 줄과 보고서 경로만.
