"""모드 — 현장(field) · 운영자(operator) · 개발자(developer) (talog/access.py). 콘솔 왼쪽 위에서 바꾼다.

    현장     보기·리포트 만들기·질문. 설정은 보기만
    운영자   + 설정 저장·시험·감시 정지·경보 재현                     ← http.py 의 oper(…)
    개발자   + Claude 연동                                           ← http.py 의 dev(…)

위로 올릴 때만 고정 PIN 을 묻고(5번 틀리면 1분 거절), 고른 모드는 talog.yaml 의 mode 에 남긴다.
"""

from __future__ import annotations

import copy
import os
import time

from .. import access, settings

FAIL_MAX, FAIL_WAIT_S = 5, 60  # PIN 을 연달아 틀리면 잠시 받지 않는다


class ModeMixin:
    """Console 이 물려받는다 — 공유 상태(mode·_pin_fail)는 app.Console.__init__ 에 있다."""

    def is_dev(self) -> bool:
        return self.mode == "developer"

    def at_least(self, mode: str) -> bool:
        return access.RANK[self.mode] >= access.RANK[mode]

    def access_info(self) -> dict:
        return {"mode": self.mode, "mode_label": access.MODES[self.mode],
                "modes": [{"key": k, "label": v} for k, v in access.MODES.items()]}

    def set_mode(self, mode: str, pin: str = "") -> tuple[int, dict]:
        """모드를 바꾸고 talog.yaml 에 남긴다. 위로 올릴 때만 PIN 을 확인한다."""
        mode = str(mode or "").strip().lower()
        if mode not in access.MODES:
            return 400, {"ok": False, "error": f"모르는 모드: {mode}"}
        if access.needs_pin(self.mode, mode):
            now = time.time()
            self._pin_fail = [t for t in self._pin_fail if now - t < FAIL_WAIT_S]
            if len(self._pin_fail) >= FAIL_MAX:
                wait = int(FAIL_WAIT_S - (now - self._pin_fail[0])) + 1
                return 429, {"ok": False, "error": f"PIN 을 여러 번 틀렸습니다 — {wait}초 뒤에 다시 하십시오"}
            if not access.check_pin(pin):
                self._pin_fail.append(now)
                return 403, {"ok": False, "error": "PIN 이 맞지 않습니다", "left": FAIL_MAX - len(self._pin_fail)}
            self._pin_fail = []
        s = copy.deepcopy(self.settings)
        s["mode"] = mode
        errs = self._write_settings(s)
        if errs:
            return 400, {"ok": False, "error": "; ".join(errs)}
        was, self.mode = self.mode, mode
        print(f"[콘솔] {access.MODES[was]} → {access.MODES[mode]}")
        return 200, {"ok": True, "access": self.access_info()}

    def keep_protected(self, s: dict) -> dict:
        """화면의 설정 저장은 모드를 바꾸지 못한다 — 모드는 왼쪽 위 전환(set_mode)으로만 바꾸고, 파일 값을 그대로 둔다."""
        s = settings.normalize(s)
        disk = settings.load(self.config_path)[0] if os.path.exists(self.config_path) else self.settings
        s["mode"] = disk.get("mode", access.DEFAULT_MODE)
        return s
