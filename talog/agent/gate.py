"""합의 관문 — 규칙과 LLM 이 같은 원인을 말할 때만 '판단 일치'로 내보내고, 다르면 사람에게 넘긴다."""

from __future__ import annotations

from .common import sev_rank
from .runbook import Runbook


def rule_message(family: str, rule: dict, alerts: list, rb: Runbook) -> str:
    a0 = alerts[0]
    facts = "; ".join(rule["facts"][:2]) if rule.get("facts") else "근거 부족"
    acts = ", ".join(rb.action_ko(a).split(" — ")[0].split(" (")[0]
                     for a in rule["actions"][:2])
    return f"{a0.title}. 룰 진단: {rule['name']}. 근거: {facts}. 권고: {acts}."


def gate(family: str, rule: dict, llm: dict | None, alerts: list, rb: Runbook) -> dict:
    """합의 관문: 규칙과 LLM 이 같은 원인을 말할 때만 '판단 일치'."""
    sev = max((a.severity for a in alerts), key=sev_rank)
    notify = list(rule["notify"])
    actions = list(rule["actions"])
    msg = rule_message(family, rule, alerts, rb)
    llm_ok = bool(llm and llm.get("ok"))
    if not llm:
        mode, label = "rule_only", "룰 판단 (LLM 미사용)"
    elif not llm_ok:
        mode, label = "rule_only", f"룰 판단 (LLM 응답 실패: {llm.get('error', '')[:60]})"
    elif llm["root_cause"] == rule["cause"] and rule["cause"] != "unknown":
        mode, label = "agree", "룰·LLM 판단 일치"
    elif rule["cause"] == "unknown" and llm["root_cause"] != "unknown":
        mode, label = "llm_only", "룰 판단 불가 — LLM 의견만 있음, 담당자 확인 필요"
    else:
        mode, label = "disagree", "판단 불일치 — 담당자 확인 필요"
    if llm_ok and llm.get("notify") and llm["notify"] not in notify and mode != "rule_only":
        notify.append(llm["notify"])
    if mode in ("disagree", "llm_only") or rule["cause"] == "unknown":
        if "escalate_to_human" not in actions:
            actions.append("escalate_to_human")
    msg_src = "rule"
    if mode == "agree":
        f = llm.get("fact") or {}
        if llm.get("message_ko") and f.get("ok") and not f.get("hanzi"):
            # 7B 모델은 원인을 맞혀도 조치를 엉뚱하게 쓰곤 한다(풍산 2호기 시험 6회 중 5회)
            # — LLM 문구는 원인 설명으로만 쓰고 조치는 런북 문장을 붙인다
            acts = ", ".join(rb.action_ko(x).split(" — ")[0].split(" (")[0] for x in actions[:3])
            msg, msg_src = f"{llm['message_ko'].strip()} 권고: {acts}.", "llm"
    elif mode in ("disagree", "llm_only"):
        msg += (f" LLM 의견: {rb.cause_name(family, llm['root_cause'])}"
                f" — 두 의견을 함께 확인하십시오.")
    if not rule.get("confirmed", True):
        sev = "warn"
        label = "오경보 의심 — " + label
    return {"mode": mode, "label": label, "cause": rule["cause"],
            "cause_name": rule["name"],
            "llm_cause": llm.get("root_cause") if llm_ok else None,
            "llm_cause_name": rb.cause_name(family, llm["root_cause"]) if llm_ok else None,
            "actions": actions, "notify": notify, "severity": sev,
            "message": msg, "message_source": msg_src}
