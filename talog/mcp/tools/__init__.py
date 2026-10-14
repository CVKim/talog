"""talog MCP 도구 — 영역별 파일에 @tool 함수로 둔다 (registry.py 의 '지킬 것').

  incidents.py  overview · list_incidents · get_incident · capacity_trend   (사건·처리 여유)
  reports.py    list_reports · get_report · query_report                     (진단 리포트 DB)
  rawlogs.py    log_days · log_files · read_log · search_logs · trace_inspection (원본 로그 직접 읽기)
  replay.py     start_replay · job_status · rediagnose                       (재현·재진단)
  knowledge.py  runbook · search_cases · save_case                           (런북·사례 KB)

import 순서가 tools/list 순서다 — 클라이언트 캐시를 위해 바꾸지 않고, 새 영역은 끝에 더한다.
"""

from . import incidents, reports, rawlogs, replay, knowledge   # import 하면 도구가 등록된다

__all__ = ["incidents", "reports", "rawlogs", "replay", "knowledge"]
