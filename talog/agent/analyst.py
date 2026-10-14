"""로컬 LLM 2차 의견 — 근거를 프롬프트로 만들어 원인·알림 문구를 받고 수치를 대조한다."""

from __future__ import annotations

import json

from ..llm import OllamaClient, hanzi_count, parse_json
from .common import _SEV_RANK
from .factcheck import clean_message, fact_check
from .interpret import interpret
from .runbook import Runbook


_ANALYSIS_PROMPT = ("판단하기 전에 분석을 먼저 짧게 써라(6문장 이내): 핵심 관찰과 그 수치, "
                    "그것이 어느 원인을 지지하거나 배제하는지. 아직 JSON 은 쓰지 마라.")


_FINAL_PROMPT = ("이제 최종 판단을 JSON 으로만 답하라. evidence 에는 수치를 넣은 짧은 근거 "
                 "문장(최대 4개), root_cause 는 원인 id, confidence 는 0~1, actions 는 조치 id "
                 "1~3개, notify 는 역할 id, severity 는 info|warn|crit, message_ko 는 담당자에게 "
                 "보낼 한국어 3문장 이내로 원인과 근거만 쓴다(조치·권고 문장은 쓰지 마라 — 조치는 "
                 "런북 문장으로 따로 붙는다. 한글만, 한자 금지, 근거 JSON 에 있는 숫자와 시각만 인용).")


class LLMAnalyst:
    """로컬 LLM 2차 의견 — 런북 문단을 읽고 닫힌 어휘로 답한다(서술 → JSON 2단계)."""

    def __init__(self, llm_cfg: dict, rb: Runbook, reason_first: bool = True):
        self.client = OllamaClient(llm_cfg)
        self.rb = rb
        self.reason_first = reason_first

    def _system(self, family: str) -> str:
        fam = self.rb.families.get(family) or {}
        causes = "\n".join(f"- {cid}: {c.get('name', cid)}\n  {c.get('description', '')}"
                           for cid, c in self.rb.causes(family).items())
        acts = "\n".join(f"- {a}: {t}" for a, t in self.rb.actions.items())
        roles = ", ".join(f"{r}({t})" for r, t in self.rb.roles.items())
        return (
            "당신은 talos 비전 검사 설비의 운영 에이전트다. 실시간 감시(talog watch)가 경보를 "
            "올렸다. 아래 런북에서 근거에 맞는 원인 하나를 고르고, 담당자에게 권고할 조치와 알릴 "
            "역할을 정한다. talog 는 설비를 직접 조작하지 않으며 조치는 사람에게 권고하는 것이다.\n\n"
            f"[원인 — {fam.get('title', family)}]\n{causes}\n- unknown: 어느 원인도 근거와 맞지 "
            f"않음\n\n[조치]\n{acts}\n\n[역할] {roles}\n\n"
            "원칙:\n1. 근거에 없는 수치나 사실을 지어내지 않는다. 숫자와 시각은 근거 JSON 에 있는 "
            "값만 인용한다. key_facts 는 근거 수치를 문장으로 옮긴 것이므로, 수치를 직접 읽은 "
            "결과가 key_facts 와 다르면 key_facts 를 따른다.\n2. 근거가 한 원인을 뚜렷이 지지하지 "
            "않으면 unknown 과 "
            "escalate_to_human 을 고른다.\n3. 진행 중 검사(inflight)가 있으면 즉시 재시작을 권하지 "
            "않는다.\n4. 근거 없이 정비 출동이나 제품 격리를 권하지 않는다.\n"
            "5. 모든 서술은 한국어로 쓰고 중국어 한자를 쓰지 않는다.")

    def _schema(self, family: str) -> dict:
        causes = list(self.rb.causes(family)) + ["unknown"]
        return {"type": "object", "properties": {
            "evidence": {"type": "array", "items": {"type": "string", "maxLength": 200},
                         "maxItems": 4},
            "root_cause": {"type": "string", "enum": causes},
            "confidence": {"type": "number"},
            "actions": {"type": "array", "items": {"type": "string",
                                                   "enum": list(self.rb.actions)},
                        "minItems": 1, "maxItems": 3},
            "notify": {"type": "string", "enum": list(self.rb.roles)},
            "severity": {"type": "string", "enum": ["info", "warn", "crit"]},
            "message_ko": {"type": "string", "maxLength": 400}},
            "required": ["evidence", "root_cause", "confidence", "actions", "notify",
                         "severity", "message_ko"]}

    def analyze(self, family: str, ev: dict) -> dict:
        c = self.client
        out = {"ok": False, "error": "", "model": c.model, "url": c.url, "device": "",
               "device_reason": "", "wall_s": 0.0, "prompt_tokens": 0, "gen_tokens": 0,
               "calls": 0, "analysis": ""}
        # 해석 문장(key_facts)을 맨 앞에 둔다 — 숫자를 직접 읽다 틀리는 것을 줄인다
        ordered = {"key_facts": ev.get("key_facts") or interpret(ev, self.rb.params, family),
                   **{k: v for k, v in ev.items() if k != "key_facts"}}
        user = (f"[사건] {ev.get('site') or '설비'} {ev['trigger']['time']} — 경보 "
                f"{ev.get('n_alerts')}건: " + " / ".join(a["title"] for a in ev["alerts"][:4])
                + "\n\n[근거 JSON]\n" + json.dumps(ordered, ensure_ascii=False))
        msgs = [{"role": "system", "content": self._system(family)},
                {"role": "user", "content": user}]
        device = None

        def call(m, fmt=None, n=None):
            nonlocal device
            r = c.chat(m, fmt=fmt, num_predict=n, device=device)
            device = device or r["device"]           # 한 사건 안에서는 장치 고정(재로드 방지)
            out["calls"] += 1
            out["wall_s"] = round(out["wall_s"] + r["wall_s"], 2)
            out["prompt_tokens"] += r["prompt_tokens"]
            out["gen_tokens"] += r["gen_tokens"]
            out["device"], out["device_reason"] = r["device"], out["device_reason"] or \
                r["device_reason"]
            return r

        if self.reason_first:
            r = call(msgs + [{"role": "user", "content": _ANALYSIS_PROMPT}], n=450)
            if r["error"]:
                out["error"] = r["error"]
                return out
            out["analysis"] = r["content"].strip()[:1500]
            msgs = msgs + [{"role": "user", "content": _ANALYSIS_PROMPT},
                           {"role": "assistant", "content": out["analysis"]}]
        msgs = msgs + [{"role": "user", "content": _FINAL_PROMPT}]
        schema = self._schema(family)
        r = call(msgs, fmt=schema, n=700)
        if r["error"]:
            out["error"] = r["error"]
            return out
        d = parse_json(r["content"]) or {}
        if d and hanzi_count(str(d.get("message_ko", ""))):
            # 한자 누출 시 1회 재요청 (Qwen 계열의 알려진 이탈)
            r2 = call(msgs + [{"role": "assistant", "content": r["content"]},
                              {"role": "user", "content": "message_ko 에 한자가 섞였다. 같은 "
                               "판단을 한글로만 다시 써서 같은 JSON 형식으로 답하라."}],
                      fmt=schema, n=700)
            d = parse_json(r2["content"]) or d
        if not d:
            out["error"] = "JSON 파싱 실패"
            out["raw"] = r["content"][:300]
            return out
        causes = list(self.rb.causes(family)) + ["unknown"]
        rc = d.get("root_cause")
        raw = str(d.get("message_ko", ""))[:400]
        msg, dropped = clean_message(raw, ev, self.rb, family,
                                     float(self.rb.params.get("interval_tol_pct", 10)))
        if dropped:
            out["message_raw"], out["message_dropped"] = raw, dropped
        out.update(
            ok=True,
            root_cause=rc if rc in causes else "unknown",
            actions=[a for a in (d.get("actions") or []) if a in self.rb.actions][:3]
            or ["escalate_to_human"],
            notify=d.get("notify") if d.get("notify") in self.rb.roles else "",
            severity=d.get("severity") if d.get("severity") in _SEV_RANK else "",
            confidence=d.get("confidence") if isinstance(d.get("confidence"),
                                                         (int, float)) else None,
            evidence=[str(x)[:200] for x in (d.get("evidence") or [])][:4],
            message_ko=msg)
        out["fact"] = fact_check(out["message_ko"], ev, extra=(self.rb.params,))
        return out
