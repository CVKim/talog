"""답 검증 — 문장 속 숫자·시각을 근거 데이터와 대조(fact_check)하고, 지시문·키 노출·과장을 걸러낸다."""

from __future__ import annotations

import re

from ..llm import hanzi_count


_TIME_TOK = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?(?!\d)")


# '9시 28분 3초' 식 한국어 시각 표기 (초는 선택)
_KTIME_TOK = re.compile(r"(?<!\d)(\d{1,2})\s*시\s*(\d{1,2})\s*분(?:\s*(\d{1,2})\s*초)?")


_NUM_TOK = re.compile(r"(?<![\w.])-?\d+(?:,\d{3})*(?:\.\d+)?")


def _walk(obj, nums: set, times: set, ids: set):
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        nums.add(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _walk(v, nums, times, ids)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _walk(v, nums, times, ids)
    elif isinstance(obj, str):
        for m in _TIME_TOK.finditer(obj):
            times.add(f"{int(m.group(1)):02d}:{m.group(2)}")
        rest = _TIME_TOK.sub(" ", obj)
        for tok in _NUM_TOK.findall(rest):
            digits = tok.replace(",", "").lstrip("-")
            if len(digits.split(".")[0]) >= 10:
                ids.add(digits)
            else:
                try:
                    nums.add(float(tok.replace(",", "")))
                except ValueError:
                    pass


def fact_check(message: str, evidence: dict, extra: tuple = ()) -> dict:
    """알림 문구의 숫자·시각이 근거 데이터에 있는가 (Robot_Sim report.py 규칙 확장).

    - 시각(HH:MM[:SS])은 근거에 있는 시각과 HH:MM 이 같아야 한다
    - 10자리 이상 숫자(inner id 등)는 문자열로 정확히 같아야 한다
    - 그 밖의 숫자는 적힌 자릿수로 반올림해 근거 값(또는 ×100 백분율)과 같으면 통과
    - 0~3 의 작은 정수는 개수 표기가 흔해 통과시킨다
    """
    nums, times, ids = set(), set(), set()
    _walk(evidence, nums, times, ids)
    for x in extra:
        _walk(x, nums, times, ids)
    bad = []
    msg = message or ""
    for m in _TIME_TOK.finditer(msg):
        if f"{int(m.group(1)):02d}:{m.group(2)}" not in times:
            bad.append(m.group(0))
    for m in _KTIME_TOK.finditer(msg):
        if f"{int(m.group(1)):02d}:{int(m.group(2)):02d}" not in times:
            bad.append(m.group(0))
    rest = _KTIME_TOK.sub(" ", _TIME_TOK.sub(" ", msg))
    n_tok = 0
    for tok in _NUM_TOK.findall(rest):
        n_tok += 1
        digits = tok.replace(",", "").lstrip("-")
        if len(digits.split(".")[0]) >= 10:
            if digits not in ids:
                bad.append(tok)
            continue
        x = float(tok.replace(",", ""))
        dec = len(digits.split(".")[1]) if "." in digits else 0
        if dec == 0 and 0 <= x <= 3:
            continue
        ok = False
        for y in nums:
            for cand in (y, -y, y * 100.0):
                if round(cand, dec) == round(x, dec) or (dec == 0 and abs(cand - x) < 0.5):
                    ok = True
                    break
            if ok:
                break
        if not ok:
            bad.append(tok)
    return {"ok": not bad, "unsupported": bad[:8], "n_numbers": n_tok,
            "hanzi": hanzi_count(msg)}


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


# 조치·계획 문장 — 조치는 런북 문장으로 따로 붙으므로 LLM 문구에서는 뺀다
_DIRECTIVE = re.compile(r"하십시오|해\s?주세요|하세요|하겠습니다|않겠습니다|지속합니다|필요합니다|"
                        r"바랍니다|해야\s?합니다|해야\s?한다|권장|권고|감시\s?지속|감시를\s?지속|재시작하지\s?않|"
                        r"(확인|점검|조치)\s?필요|(보고|점검|조치|재시작|감시)(합니다|한다)")


# JSON 키가 문장에 새어 나온 꼴 — "(root_cause: progressive_slowdown)"
_KEY_LEAK = re.compile(r"\s*\((?:root_cause|cause|actions?|원인)\s*[:=][^)]*\)")


_CLAIM_LIMIT = re.compile(r"한계[를에]?\s?(초과|넘)")


_CLAIM_LOAD = re.compile(r"(처리량|투입량?|부하)[이가은는]?\s?(꾸준히\s?|계속\s?)?(증가|늘)")


_NEGATION = re.compile(r"아니|아닙|아님|않|없")


def clean_message(text: str, ev: dict, rb, family: str, tol_pct: float = 10.0) -> tuple[str, list]:
    """LLM 담당자 문구를 다듬는다 → (남은 문구, 뺀 문장).

    원인·조치 id 는 한국어 이름으로 바꾸고, 조치·계획 문장과 근거에 어긋나는 용량 주장
    (일시 지연 내성이 남았는데 '한계 초과', 투입 간격이 그대로인데 '처리량·투입 증가')은 뺀다.
    다 빠지면 빈 문자열 — gate 가 룰 문구로 대신한다."""
    s = _KEY_LEAK.sub("", text or "")
    names = {**{c: rb.cause_name(family, c).split(" — ")[0] for c in rb.causes(family)},
             **{a: rb.action_ko(a).split(" — ")[0].split(" (")[0] for a in rb.actions}}
    for k, v in names.items():                  # 런북 문장에 경로가 있어 치환 문자열은 함수로 넘긴다
        s = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(k)}(?![A-Za-z0-9_])", lambda _m, v=v: v, s)
    cap = ev.get("capacity") or {}
    chg = (ev.get("trend") or {}).get("interval_change_pct")
    tol = cap.get("tolerance_s")
    keep, drop = [], []
    for sent in _SENT_SPLIT.split(s.strip()):
        if not sent:
            continue
        neg = bool(_NEGATION.search(sent))
        bad = (bool(_DIRECTIVE.search(sent))
               or (tol is not None and tol > 0 and not neg and bool(_CLAIM_LIMIT.search(sent)))
               or (chg is not None and abs(chg) <= tol_pct and not neg and bool(_CLAIM_LOAD.search(sent))))
        (drop if bad else keep).append(sent)
    return " ".join(keep), drop
