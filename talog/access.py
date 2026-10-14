"""모드 — 같은 talog 를 누가·어디서 쓰는지에 따라 여는 기능을 나눈다. 콘솔 왼쪽 위에서 고른다.

    field      현장 모드 (기본)   운영 현황·사건·분석(리포트 만들기·질문)을 쓰고, 설정은 보기만 한다.
    operator   운영자 모드        현장 모드 + 설정 저장·시험(LLM·메일·규칙·시험 경보)·감시 정지·경보 재현.
    developer  개발자 모드        운영자 모드 + Claude 연동(구독 로그인 — 개발자 PC 에서 현장 로그 사본을 깊게 본다).

위 모드로 올릴 때만 고정 PIN 을 묻고(코드에는 해시만 둔다), 내릴 때는 묻지 않는다. 고른 모드는 talog.yaml 의
`mode` 에 남아 다음에 열 때도 같다. 실행할 때 `talog --mode <모드>` 로 이번 실행만 바꿀 수도 있다.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

MODES = {"field": "현장 모드", "operator": "운영자 모드", "developer": "개발자 모드"}
RANK = {"field": 0, "operator": 1, "developer": 2}
DEFAULT_MODE = "field"
# 위 모드로 올리는 고정 PIN 의 해시 (PBKDF2-SHA256, 20만 번) — PIN 은 사내 담당자가 알려 준다
PIN_HASH = ("pbkdf2_sha256$200000$0f5d4c130956a151cca71c0db451fbcd$"
            "4a04463c18c5976053d92e289ca786abf2b1863fc01b13c425fe58d4dc6d939f")
_HASH_RE = re.compile(r"^pbkdf2_sha256\$(\d+)\$([0-9a-f]{32})\$([0-9a-f]{64})$")


def resolve_mode(file_mode: str | None, override: str = "") -> str:
    """실행 인자(override) > talog.yaml 의 mode > field. 모르는 값은 field (안전한 쪽)."""
    for m in (override, file_mode):
        m = str(m or "").strip().lower()
        if m in MODES:
            return m
    return DEFAULT_MODE


def needs_pin(current: str, target: str) -> bool:
    """지금보다 위 모드로 올릴 때만 PIN 을 묻는다."""
    return RANK.get(target, 0) > RANK.get(current, 0)


def hash_pin(pin: str) -> str:
    """PIN → 'pbkdf2_sha256$반복$소금$해시' (PIN_HASH 를 다시 만들 때 쓴다)."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", str(pin).encode("utf-8"), salt, 200_000)
    return f"pbkdf2_sha256$200000${salt.hex()}${dk.hex()}"


def check_pin(pin: str, stored: str = "") -> bool:
    m = _HASH_RE.match(str(stored or PIN_HASH))
    if not m or not pin:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", str(pin).encode("utf-8"), bytes.fromhex(m.group(2)), int(m.group(1)))
    return hmac.compare_digest(dk.hex(), m.group(3))
