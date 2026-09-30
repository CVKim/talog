"""SMTP 비밀번호 보관 — Windows DPAPI(CryptProtectData) 암호화.

설정 파일에는 평문 대신 DPAPI 암호문(talog.yaml 의 `mail.secret`)으로 저장한다. 같은 PC 의
같은 Windows 사용자만 복호화할 수 있으므로 설정 파일이 복사·유출돼도 비밀번호는
드러나지 않는다. 다른 OS 에서는 사용할 수 없고(available() == False) 환경변수
(`email.password_env`) 방식을 쓴다.
"""

from __future__ import annotations

import base64
import ctypes
import sys

_ENTROPY = b"talog-watch-smtp"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def available() -> bool:
    return sys.platform == "win32"


def _blob(data: bytes) -> _Blob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _call(fn_name: str, data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    src, ent, out = _blob(data), _blob(_ENTROPY), _Blob()
    fn = getattr(crypt32, fn_name)
    # CRYPTPROTECT_UI_FORBIDDEN(0x1): 대화상자 없이 동작
    ok = fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0x1,
            ctypes.byref(out))
    if not ok:
        raise OSError(f"{fn_name} 실패 (Windows 오류 {ctypes.GetLastError()})")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def protect(text: str) -> str:
    if not available():
        raise OSError("DPAPI 는 Windows 에서만 쓸 수 있습니다")
    return base64.b64encode(_call("CryptProtectData", text.encode("utf-8"))).decode("ascii")


def unprotect(b64: str) -> str:
    if not available():
        raise OSError("DPAPI 는 Windows 에서만 쓸 수 있습니다")
    return _call("CryptUnprotectData", base64.b64decode(b64)).decode("utf-8")
