---
name: talog-rca
description: talog(talos 비전 검사 로그 분석기) 산출물로 사건 RCA·하루 검토·현장 점검을 한다. talog MCP 도구로 판단·근거·처리 여유 추세·리포트 DB·원본 로그를 확인하고, 원인 판단과 현장 조치를 담은 독립 HTML 보고서를 Result 폴더에 남긴다. "talog 사건 분석", "NoInspThread 원인", "처리 여유", "어제 경보 검토", "현장 점검", "원본 로그 봐 줘" 같은 요청이나 talog 사건 id(예 20261012-050228-capacity_drift)가 나오면 쓴다.
allowed-tools: mcp__talog__overview, mcp__talog__list_incidents, mcp__talog__get_incident, mcp__talog__capacity_trend, mcp__talog__list_reports, mcp__talog__get_report, mcp__talog__query_report, mcp__talog__log_days, mcp__talog__log_files, mcp__talog__read_log, mcp__talog__search_logs, mcp__talog__trace_inspection, mcp__talog__job_status, mcp__talog__rediagnose, mcp__talog__runbook, mcp__talog__search_cases
---

# talog RCA

talog 는 현장 PC 에서 룰 + 로컬 LLM 으로 경보·사건을 만든다. 이 스킬은 사무실에서 그 산출물을
talog MCP 도구(`mcp__talog__*`)로 읽어 깊게 분석한다. 비용은 사용자의 Claude 구독 안에서만 든다.

## 시작 전에

- talog MCP 도구가 없으면 멈추고 안내한다: talog 콘솔 → **Claude** → **연결 설정**의 "Claude Code 연결" 명령을
  실행한 뒤 Claude Code 를 다시 열어야 한다.
- 데이터는 마스킹돼 있다. 검사 id 는 `ID-xxxxxx`, 경로는 `<로그>`·`<데이터>` 로 보인다. 원래 값을 추측하지 않는다.
- API 키를 만들거나, 데이터를 외부 서비스·claude.ai 로 보내거나, Artifact 로 게시하지 않는다.

## 절차

1. **개요** `overview` — 출처(현장)·날짜·사건 수·기준선·리포트를 본다.
2. **대상** `list_incidents(day=…, severity=…)` — 심각부터. 사용자가 id 를 주면 바로 3번.
3. **사건** `get_incident(incident_id=…)` — 판단(룰·LLM 일치 여부)·원인·룰 근거·해석 문장·경보·근거 데이터.
4. **교차 확인** (사건 종류에 맞게 고른다)
   - `capacity_trend(source, day, groups=[사건 그룹])`: 기준선 대비 점유가 언제부터 늘었나, 내성(한계 − p99)이 0 근처로
     떨어진 시각, 거부 시각, 투입 간격이 그대로인가.
   - `get_report(tag)` → `query_report(tag, sql)`: 거부·검사 시간·재시작·특정 시각 전후를 DB 로 확인 (아래 예시 SQL).
   - `runbook(family='reject')`: 원인 정의와 배제 조건 — 룰과 같은 어휘로 말한다.
   - `search_cases(query)`: 비슷한 과거 사례.
   - **원본 로그** — 요약 근거로 부족하거나 사용자가 원본을 원할 때: `log_days` 로 읽을 수 있는 일자 폴더를 보고
     (콘솔에서 고른 폴더는 `day='선택'`), `log_files(day)` → `search_logs(day, query, time_from, time_to)` 로 경보
     시각 ±몇 분의 줄을 찾고 → `read_log(day, file, at, before, lines)` 로 앞뒤 흐름을 읽는다. 큰 파일(DLInfer 등)은
     꼭 시각 범위를 준다(한 번에 300줄·20초). 검사 하나의 시작→seq→알고리즘→회신은 `trace_inspection(inspection_id)`.
     가린 id(`ID-xxxxxx`)로 그대로 다시 찾을 수 있다.
   - 규칙을 고쳤거나 "그때 경보가 언제 났을까"를 볼 때만 `start_replay` → `job_status`. 수 분 걸린다고 먼저 알린다.
5. **판단** — 룰 원인에 동의하는지 밝히고 근거를 3개 이상 든다. 다른 원인은 왜 아닌지 한 줄씩. 숫자·시각은 도구
   결과에 있는 값만 쓰고, 추정은 "추정"이라고 쓴다.
6. **조치** — runbook 의 조치 문장을 기준으로 **지금 / 다음 정지 때 / 개발팀** 으로 나눈다. 진행 중 검사가 있으면
   지금 재시작을 권하지 않는다.
7. **보고서** — `templates/rca_report.html` 을 그대로 복사해 채운다 (아래 "보고서").
8. **사례** — 결론이 확정되면 `save_case` 로 사례 KB 에 남길지 사용자에게 묻는다. 고객명·id 없이 일반화해서 쓴다.

## 원인 구분 (talog runbook 요약 — 자세히는 `runbook(family='reject')`)

| 원인 | 핵심 근거 | 이것과 헷갈리지 말 것 |
|---|---|---|
| 시간 경과형 처리 열화 `progressive_slowdown` | 기준선 대비 점유 1.2배↑ 또는 유휴 1개↓, 시간별로 꾸준히 증가(순위상관 높음), 투입 간격 ±10% 안, GPU 커널 그대로, 여러 그룹이 함께 | 처리량 포화(투입·GPU 가 변함) |
| 처리량 포화 `throughput_saturation` | 투입이 빨라짐(간격 −10%↓) 또는 GPU 커널(executeV2) 1.5배↑, 스레드가 실제로 다 참(유휴 0 근처) | 열화(투입 그대로), 단건 정체(스레드 남음) |
| GPU 컨텍스트 치명 오류 `gpu_context_fault` | 경보 ±5분 enqueueV3·INFER_ERROR·SEH(0xc0000005), 크래시 동반 | 포화 — 진행 중이 많아 보여도 오류가 먼저 |
| 완료 신호 끊김 `completion_signal_lost` | 마지막 INSPECT_END 뒤 60초↑ 시작만 쌓임, 도착 순간 유휴는 스레드 수 근처, seq 점유 평소대로 | 포화 — 진행 중 수·소요 배율은 부푼 값 |
| 단건 정체 `isolated_stall` | 정체 경보인데 남은 검사 1~2건, 유휴 스레드 남음 | 포화 |
| 타임아웃 후 슬롯 미반환 `slot_leak_timeout` | 이미지 처리 타임아웃 뒤 거부, 진행 중 1건 이하 | |
| 기동 직후 미초기화 `not_initialized` | 재시작·모델 로드 5분 안, 진행 중 없음 | |

용어: 유휴 스레드(도착 순간 놀던 Seq 스레드 수, 0 이면 NoInspThread) · 점유(검사 한 건이 스레드를 잡은 시간,
저장·DB 포함) · 한계 = 풀(스레드 수) × 투입 간격 · 내성 = 한계 − 60분 p99 점유 · 기준선 = 기동 뒤 워밍업 10분을
지난 첫 1시간. `talog://guide` 리소스에 요약이 있다.

## 예시 SQL (`query_report`)

```sql
-- 거부 직전 분 단위 흐름 (검사 수·평균 소요·최소 유휴)
SELECT substr(start_text,1,5) AS 분, COUNT(*) AS 검사, ROUND(AVG(duration_s),2) AS 평균소요, MIN(wait_threads) AS 최소유휴
FROM inspections WHERE start_text BETWEEN '05:00' AND '05:05' GROUP BY 1 ORDER BY 1
-- NoInspThread 거부 (value = 그룹)
SELECT ts_text, inner_id, value AS 그룹 FROM events WHERE kind='COMM_MSG' AND status='NoInspThread'
-- 재시작(프로세스 세대)
SELECT start_text, end_text, end_cause FROM process_gens
-- seq 파일별 점유(ms) 평균
SELECT f.name, COUNT(*) AS n, ROUND(AVG(e.value)) AS 평균ms FROM events e JOIN files f ON f.id=e.file_id
WHERE e.kind='SEQ_FINISH' GROUP BY 1 ORDER BY 1
```

`events` 는 수백만 행일 수 있다 — `kind`·`inner_id`·`ts` 조건을 먼저 건다 (20초를 넘으면 끊긴다).

## 보고서

- `templates/rca_report.html` 을 복사해 `{{…}}` 자리를 채운다. 외부 CDN·웹폰트를 넣지 않는다(오프라인에서도 열려야 한다).
- 저장 위치: 작업 폴더에 `reports\` 가 있으면(talog 에이전트 폴더) `reports\<현장>_<YYYYMMDD>\` 에, 없으면 코드가 있는
  드라이브의 `Result\<현장>_<YYYYMMDD>\` 에 `rca_<사건 id>.html` 로 둔다 (예:
  `E:\Dev\talog\Result\풍산2호기_20261012\rca_20261012-050228-capacity_drift.html`). 근거로 쓴 SQL·도구 결과 요약도
  같은 폴더에 `evidence.md` 로 남긴다. C: 임시 폴더나 바탕화면에 남기지 않는다.
- 답변에는 결론 세 줄과 보고서 경로만 쓴다. 보고서 본문을 다시 붙여 넣지 않는다.
- 하루 검토(`daily_review`)·현장 점검(`site_health`)도 같은 템플릿을 쓰되 "사건" 대신 "검토 범위"를 채운다.
