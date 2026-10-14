"""런북(rules/runbook.yaml) — 사건 계열별 원인 후보·조치 어휘·알림 대상과 판단 매개변수."""

from __future__ import annotations

import os

import yaml

from .common import _DEFAULT_PARAMS


_RUNBOOK_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "rules", "runbook.yaml")


class Runbook:
    """rules/runbook.yaml — 원인·조치·담당 어휘와 LLM 이 읽는 원인 설명."""

    def __init__(self, path: str = ""):
        p = path if path and os.path.exists(path) else _RUNBOOK_PATH
        with open(p, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        self.path = p
        self.params = {**_DEFAULT_PARAMS, **(raw.get("params") or {})}
        self.roles: dict = raw.get("roles") or {}
        self.actions: dict = raw.get("actions") or {}
        self.alert_family: dict = raw.get("alert_family") or {}
        self.families: dict = raw.get("families") or {}

    def family_of(self, rule: str) -> tuple[str, str]:
        """경보 룰 → (계열, system 계열이면 원인 id). 모르는 룰은 system/unknown."""
        v = str(self.alert_family.get(rule, "system.unknown"))
        fam, _, cause = v.partition(".")
        return (fam if fam in self.families else "system"), cause

    def causes(self, family: str) -> dict:
        return (self.families.get(family) or {}).get("causes") or {}

    def cause_name(self, family: str, cause: str) -> str:
        if cause == "unknown":
            return "원인 미상"
        c = self.causes(family).get(cause) or {}
        return c.get("name", cause)

    def cause_spec(self, family: str, cause: str) -> dict:
        return self.causes(family).get(cause) or {}

    def action_ko(self, a: str) -> str:
        return str(self.actions.get(a, a))

    def role_ko(self, r: str) -> str:
        return str(self.roles.get(r, r))
