"""MCP 프롬프트(Claude 앱의 슬래시 명령 /mcp__talog__…)와 Claude 에게 주는 안내·용어 글.

콘솔의 스킬 카드는 작업 폴더의 스킬(talog/claude_kit/skills)을 쓰고, Claude 데스크톱 채팅처럼 스킬이 없는 곳은
이 프롬프트가 같은 절차를 글로 싣는다 — 절차를 고치면 두 곳을 함께 고친다.
"""

from __future__ import annotations


PROMPTS = [
    {"name": "incident_rca", "title": "사건 RCA",
     "description": "talog 사건 하나를 근거 확인 → 원인 판단 → 권고 → 보고서까지",
     "arguments": [{"name": "incident_id", "description": "사건 id (예: 20261012-050228-capacity_drift)", "required": True}]},
    {"name": "daily_review", "title": "하루 검토",
     "description": "하루치 경보·사건·처리 여유를 훑고 놓친 징후·오경보를 짚는다",
     "arguments": [{"name": "source", "description": "출처 (비우면 전체)", "required": False},
                   {"name": "day", "description": "YYYYMMDD (비우면 최근)", "required": False}]},
    {"name": "site_health", "title": "현장 점검",
     "description": "현장별 처리 여유·기준선·재시작·경보 경향과 규칙이 그 현장에 맞는지 점검",
     "arguments": [{"name": "source", "description": "출처 (비우면 전체)", "required": False}]},
    {"name": "log_investigation", "title": "원본 로그 조사",
     "description": "원본 talos 로그를 직접 읽어 증상(시각·문구·검사 id)의 경과와 원인을 찾는다",
     "arguments": [{"name": "day", "description": "일자 폴더 (예: 12, 2026_10\\12 또는 콘솔에서 고른 경로)", "required": False},
                   {"name": "symptom", "description": "증상 — 예: 05:02 NoInspThread, 검사 id, 오류 문구", "required": False}]},
    {"name": "rules_compare", "title": "규칙 바꾼 뒤 비교",
     "description": "저장된 사건을 지금 규칙으로 재진단하고, 사용자가 허락하면 그날을 재현해 전후를 비교한다",
     "arguments": [{"name": "source", "description": "출처 (비우면 전체)", "required": False},
                   {"name": "day", "description": "날짜 YYYYMMDD (재진단 범위)", "required": False}]},
]


GUIDE = """# talog 용어 (Claude 용 요약)

- talos: AiV 비전 검사 플랫폼. 설비가 검사 요청(M2V_INSPECT_START)을 보내면 Seq 그룹의 Seq 스레드가 검사를 맡는다.
- 유휴 스레드(W): 검사 요청이 도착한 순간 그 그룹에 놀고 있던 Seq 스레드 수 (InspStarter 'Waiting thread count').
  0 이면 NoInspThread 로 거부 → 설비는 미검사로 처리한다.
- 점유 시간(hold): 검사 한 건이 스레드를 잡고 있는 시간 (seq 'CheckFinish done' — 결과 송신 뒤 저장·DB 까지).
- 투입 간격: 같은 그룹 도착 간격 중앙값. 풀 = 그룹의 스레드 수(추정). 한계 = 풀 × 투입 간격.
- 일시 지연 내성 = 한계 − 최근 60분 점유 p99. 작을수록 잠깐의 지연 한 번에 거부가 난다.
- 기준선: 기동(재시작)·기종 교체 뒤 워밍업 10분을 지난 첫 1시간의 그룹별 값. 재시작하면 새로 잡는다.
- 단계(phases): 처리(시작~판정) · 결과 송신 · 후처리(저장·DB). 각 단계는 여러 처리의 합이다.
- 판단 구조: 룰이 원인을 정하고 로컬 LLM 은 교차 확인만 한다. 조치는 런북 문장. 같은 원인일 때만 '판단 일치'.
- 주요 원인: 시간 경과형 처리 열화(투입 그대로인데 점유가 기동 뒤 꾸준히 증가) · 처리량 포화(투입이 빨라지거나
  GPU 커널이 느려짐) · GPU 컨텍스트 치명 오류 · 완료 신호 끊김(처리는 되는데 INSPECT_END 만 안 보임) ·
  단건 정체(한두 건만 남고 스레드는 남음) · 타임아웃 후 슬롯 미반환 · 기동 직후 미초기화.
"""


INSTRUCTIONS = (
    "talog(talos 비전 검사 로그 분석기)의 산출물을 읽는 도구다. overview 로 시작해 list_incidents → get_incident → "
    "capacity_trend·get_report·query_report 순으로 근거를 확인한다. 원본 로그는 log_days → log_files → search_logs"
    "(시각 범위) → read_log → trace_inspection 으로 직접 읽는다 — 하루 폴더가 수십 GB 이므로 늘 시각 범위를 좁혀서 찾는다. "
    "작업 폴더에 talog 스킬(talog-rca·talog-logs·talog-daily·talog-site·talog-rules)이 있으면 그 절차를 따른다. "
    "검사 id·경로는 마스킹돼 ID-xxxxxx·<로그> 로 보인다(그 토큰 그대로 다시 찾을 수 있다). "
    "수치·시각은 도구 결과에 있는 값만 인용하고, 룰 진단에 동의하는지 근거와 함께 밝힌다. 조치는 runbook 의 문장을 "
    "기준으로 한다. 사용자에게는 한국어로 답한다. 산출물은 사용자의 로컬 Result 폴더에 파일로 남기고 외부에 게시하지 않는다."
)


def prompt_messages(name: str, args: dict) -> dict:
    """MCP 프롬프트 본문 — Claude 앱의 슬래시 명령과 콘솔의 빠른 작업이 같은 문장을 쓴다."""
    args = args or {}
    save = ("결과는 작업 폴더의 reports\\ (없으면 코드가 있는 드라이브의 Result\\)에 독립 실행 HTML 로 저장하고 "
            "(.claude/skills/talog-rca/templates/rca_report.html 형식), 답변에는 경로만 적는다. 외부에 게시하지 않는다.")
    if name == "incident_rca":
        iid = args.get("incident_id", "")
        text = (f"talog 사건 `{iid}` 의 RCA 를 해 주세요 (talog-rca 절차).\n"
                f"1. get_incident(incident_id='{iid}') 로 판단·룰 근거·해석 문장·경보·근거 데이터를 읽는다.\n"
                "2. capacity_trend 로 그 그룹의 기준선과 시간별 추세를 확인한다 (기동 뒤 언제부터 여유가 줄었나).\n"
                "3. 필요하면 get_report·query_report 로 리포트 DB 를, start_replay 로 재현을 확인한다.\n"
                "4. runbook 의 원인 사전 기준으로 룰 진단에 동의하는지 근거와 함께 밝히고, 다른 원인 가능성을 배제한다.\n"
                "5. 현장 조치(지금 / 다음 정지 때 / 개발팀)와 재발 방지를 정리한다. 확정되면 save_case 로 사례를 남길지 묻는다.\n"
                "수치·시각은 도구 결과에 있는 값만 쓴다. " + save)
    elif name == "daily_review":
        src, day = args.get("source", ""), args.get("day", "")
        text = (f"talog 하루 검토를 해 주세요 — 출처 '{src or '전체'}', 날짜 '{day or '최근'}'.\n"
                "1. overview 와 list_incidents 로 그날 경보·사건을 훑는다 (심각 먼저).\n"
                "2. 사건마다 get_incident 로 판단(룰·LLM 일치 여부)과 근거를 보고, 오경보 의심·판단 불일치를 짚는다.\n"
                "3. capacity_trend 로 처리 여유가 줄어드는 그룹이 있는지, 다음 정지 전에 거부가 날 위험이 있는지 본다.\n"
                "4. 진단 리포트가 있으면 get_report 로 소견을 맞춰 본다.\n"
                "5. 요약: 오늘의 결론, 위험 순위, 할 일(담당별), 규칙 개선 제안. " + save)
    elif name == "site_health":
        src = args.get("source", "")
        text = (f"talog 현장 점검을 해 주세요 — 출처 '{src or '전체'}'.\n"
                "1. overview 로 날짜·사건·기준선·리포트를 본다.\n"
                "2. capacity_trend 로 날짜별 기준선과 점유·유휴 추세, 재시작으로 기준선이 자주 초기화되는지 본다.\n"
                "3. list_incidents·rediagnose 로 원인 분포와 지금 규칙으로 바뀌는 사건이 있는지 본다.\n"
                "4. 이 현장에 규칙·문턱값이 맞는지(오경보·놓침), 로그 수집에 빠진 파일이 없는지 평가한다.\n"
                "5. 개선 제안을 우선순위로 정리한다. " + save)
    elif name == "log_investigation":
        day, sym = args.get("day", ""), args.get("symptom", "")
        text = (f"talog 원본 로그를 직접 읽어 조사해 주세요 — 일자 폴더 '{day or '최근'}', 증상 '{sym or '그날 가장 심각한 이상'}'.\n"
                "1. log_days·log_files 로 폴더와 파일·시각 범위를 본다 (하루가 수십 GB — 통째로 훑지 않는다).\n"
                "2. 증상 시각 앞뒤로 search_logs(time_from/time_to) 를 핵심 로그에 건다 — 예: NoInspThread, "
                "'Waiting thread count = 0', exception, timeout, CheckFinish done.\n"
                "3. 의미 있는 줄은 read_log(at=시각) 로 앞뒤 문맥을 읽고, 관련 검사는 trace_inspection 으로 처음부터 끝까지 따라간다.\n"
                "4. talog 사건이 있으면 list_incidents·get_incident 와 맞춰 보고, 룰 판단과 원본 로그가 같은 이야기를 하는지 본다.\n"
                "5. 시간순 경과표(시각·파일·사건)·원인·근거 원문(짧게 인용)·조치(runbook 문장 기준)로 정리한다. " + save)
    elif name == "rules_compare":
        src, day = args.get("source", ""), args.get("day", "")
        text = (f"talog 규칙을 바꾼 뒤 비교해 주세요 — 출처 '{src or '전체'}', 날짜 '{day or '전체'}' (talog-rules 절차).\n"
                "1. rediagnose 로 저장된 사건을 지금 규칙으로 다시 판정해 원인이 바뀐 사건만 본다.\n"
                "2. 바뀐 사건은 get_incident 로 근거를 열어 어느 판단이 맞는지 밝힌다.\n"
                "3. 그날을 재현(start_replay)할지 먼저 나에게 묻는다 — 작업 폴더에 파일을 쓰고 수 분~수십 분 걸린다. "
                "허락하면 job_status 로 결과를 보고 원래 사건과 시각·심각도·원인을 표로 맞춘다.\n"
                "4. 새로 생긴·사라진·빨라지거나 늦어진 경보를 세고, 바뀐 규칙이 나은지 판정한다. " + save)
    else:
        raise KeyError(name)
    meta = next(p for p in PROMPTS if p["name"] == name)
    return {"description": meta["description"],
            "messages": [{"role": "user", "content": {"type": "text", "text": text}}]}
