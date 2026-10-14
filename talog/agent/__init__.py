"""talog watch 사건 분석 에이전트 — 경보 → 근거 수집 → 룰 진단 + LLM 2차 의견 → 합의 → 알림.

Robot_Sim 운영 에이전트 벤치(2026-09-28, 트윈 고장 주입 403건)의 결론을 따른다.
  - 증상이 수치로 구조화된 경우 규칙이 9B 로컬 LLM 보다 정확하다(조치 정답 85% 대 55%).
  - LLM 단독은 해로운 조치가 36% 다. 그래서 규칙과 LLM 이 같은 원인을 말할 때만
    '판단 일치'로 내보내고, 다르면 두 의견을 붙여 사람에게 넘긴다(해로운 조치 6% → 3.5%).
  - 알림 문구 속 숫자·시각은 근거 데이터와 기계적으로 대조한다(지어낸 숫자 차단).
talog 는 설비를 직접 조작하지 않는다. '조치'는 담당자에게 권고하는 닫힌 어휘다.

흐름 (모듈):
  Notifier.emit(경보) → IncidentAgent.submit (incident) → 묶음(batch_seconds) → tick() 이 마감
    → EvidenceBuilder.snapshot (evidence — 메인 스레드: 룰 엔진 상태를 복사)
    → 작업 스레드: GpuLogProbe (evidence) → RuleDiagnoser (diagnoser, 해석 문장은 interpret)
      → LLMAnalyst (analyst, 수치 대조는 factcheck) → gate (gate) → Mailer / JSONL
  원인 후보·조치 어휘는 runbook, 공통 상수는 common.
리플레이는 같은 경로를 이벤트 시각 기준으로 동기 실행한다.
예전 `talog.agent` 단일 모듈의 이름은 모두 여기서 그대로 가져다 쓸 수 있다."""

from .common import _SEV_RANK, _FAMILY_RANK, _DEFAULT_PARAMS, _ERROR_KINDS, sev_rank, _hms, _r, _alert_dict
from .runbook import _RUNBOOK_PATH, Runbook
from .evidence import GpuLogProbe, EvidenceBuilder
from .interpret import interpret, _interpret_all, _gname, _interpret_capacity, completion_gap, _gap_text
from .diagnoser import RuleDiagnoser
from .factcheck import (_TIME_TOK, _KTIME_TOK, _NUM_TOK, _walk, fact_check, _SENT_SPLIT, _DIRECTIVE,
    _KEY_LEAK, _CLAIM_LIMIT, _CLAIM_LOAD, _NEGATION, clean_message)
from .analyst import _ANALYSIS_PROMPT, _FINAL_PROMPT, LLMAnalyst
from .gate import rule_message, gate
from .incident import IncidentAgent

__all__ = ["_SEV_RANK", "_FAMILY_RANK", "_DEFAULT_PARAMS", "_ERROR_KINDS", "sev_rank", "_hms", "_r",
    "_alert_dict", "_RUNBOOK_PATH", "Runbook", "GpuLogProbe", "EvidenceBuilder", "interpret",
    "_interpret_all", "_gname", "_interpret_capacity", "completion_gap", "_gap_text", "RuleDiagnoser",
    "_TIME_TOK", "_KTIME_TOK", "_NUM_TOK", "_walk", "fact_check", "_SENT_SPLIT", "_DIRECTIVE", "_KEY_LEAK",
    "_CLAIM_LIMIT", "_CLAIM_LOAD", "_NEGATION", "clean_message", "_ANALYSIS_PROMPT", "_FINAL_PROMPT",
    "LLMAnalyst", "rule_message", "gate", "IncidentAgent"]
