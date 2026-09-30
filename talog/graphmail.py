"""Microsoft 365 발송 — Microsoft Graph sendMail (OAuth2 클라이언트 자격 증명).

Exchange Online 은 SMTP 기본 인증(아이디·비밀번호)을 폐지하는 중이라, 무인(설비 PC)
발송은 Entra ID 앱 토큰으로 Graph API 를 부르는 방식이 가장 확실하다.

IT 관리자 1회 작업:
  1. Entra ID > 앱 등록 > 새 등록 (이름 예: talog-watch) → 디렉터리(테넌트) ID·앱(클라이언트) ID
  2. 인증서 및 암호 > 새 클라이언트 암호 → 값 (콘솔 비밀 칸에 입력, DPAPI 로 암호화 저장)
  3. API 권한 > Microsoft Graph > 애플리케이션 권한 Mail.Send > 관리자 동의
  4. (권장) Exchange Online PowerShell 로 Application Access Policy 를 걸어 보낼 사서함
     1개(예: talog-alert@회사도메인)만 허용
토큰은 만료 60초 전까지 재사용한다. 외부 패키지 없이 urllib 만 쓴다.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage

TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
API_URL = "https://graph.microsoft.com/v1.0"


class GraphSender:
    def __init__(self, gcfg: dict, secret_fn, timeout: float = 20.0):
        self.g = gcfg or {}
        self.secret_fn = secret_fn
        self.timeout = timeout
        self._tok = ""
        self._exp = 0.0

    def _post(self, url: str, data: bytes, headers: dict) -> tuple[int, dict]:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read()
                return r.status, (json.loads(body) if body else {})
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}

    def token(self) -> str:
        if self._tok and time.time() < self._exp - 60:
            return self._tok
        tenant = str(self.g.get("tenant_id") or "").strip()
        cid = str(self.g.get("client_id") or "").strip()
        secret = self.secret_fn() or ""
        if not (tenant and cid and secret):
            raise ValueError("Graph 설정이 비어 있습니다 (tenant_id·client_id·클라이언트 암호)")
        url = str(self.g.get("token_url") or TOKEN_URL).format(
            tenant=urllib.parse.quote(tenant))
        data = urllib.parse.urlencode({
            "client_id": cid, "client_secret": secret, "grant_type": "client_credentials",
            "scope": "https://graph.microsoft.com/.default"}).encode()
        code, j = self._post(url, data, {"Content-Type": "application/x-www-form-urlencoded"})
        if code != 200 or not j.get("access_token"):
            raise ValueError(f"토큰 발급 실패 HTTP {code}: "
                             f"{j.get('error_description') or j.get('error') or ''}"[:240])
        self._tok = j["access_token"]
        self._exp = time.time() + float(j.get("expires_in", 3600))
        return self._tok

    @staticmethod
    def payload(msg: EmailMessage, to: list[str]) -> dict:
        html_part = msg.get_body(preferencelist=("html",))
        text_part = msg.get_body(preferencelist=("plain",))
        if html_part is not None:
            body = {"contentType": "HTML", "content": html_part.get_content()}
        else:
            body = {"contentType": "Text",
                    "content": text_part.get_content() if text_part is not None else ""}
        atts = []
        for part in msg.iter_attachments():
            data = part.get_payload(decode=True) or b""
            atts.append({"@odata.type": "#microsoft.graph.fileAttachment",
                         "name": part.get_filename() or "attachment",
                         "contentType": part.get_content_type(),
                         "contentBytes": base64.b64encode(data).decode("ascii")})
        m = {"subject": str(msg["Subject"] or ""), "body": body,
             "toRecipients": [{"emailAddress": {"address": a}} for a in to]}
        if atts:
            m["attachments"] = atts
        hdr = [{"name": "X-Talog-Incident", "value": str(msg["X-Talog-Incident"])}] \
            if msg["X-Talog-Incident"] else []
        if hdr:
            m["internetMessageHeaders"] = hdr     # Graph 는 X- 머리글만 허용
        return {"message": m, "saveToSentItems": True}

    def send(self, msg: EmailMessage, to: list[str]) -> tuple[bool, str]:
        sender = str(self.g.get("sender") or "").strip()
        if not sender:
            return False, "Graph 보내는 사서함(email.graph.sender)이 비어 있습니다"
        try:
            tok = self.token()
        except (ValueError, OSError) as e:
            return False, str(e)[:300]
        url = f"{str(self.g.get('api_url') or API_URL).rstrip('/')}/users/" \
              f"{urllib.parse.quote(sender)}/sendMail"
        data = json.dumps(self.payload(msg, to), ensure_ascii=False).encode("utf-8")
        try:
            code, j = self._post(url, data, {"Content-Type": "application/json",
                                             "Authorization": f"Bearer {tok}"})
        except OSError as e:
            return False, f"{type(e).__name__}: {e}"[:300]
        if code == 202:
            return True, ""
        if code == 401:
            self._tok = ""                          # 다음 발송 때 토큰 재발급
        err = (j.get("error") or {}) if isinstance(j, dict) else {}
        return False, f"Graph 발송 실패 HTTP {code}: {err.get('code', '')} " \
                      f"{err.get('message', '')}"[:300]

    def check(self) -> tuple[bool, str]:
        """토큰 발급만 확인한다 (메일은 보내지 않음)."""
        try:
            self.token()
            return True, f"토큰 발급 OK (만료까지 {max(0, self._exp - time.time()) / 60:.0f}분)"
        except (ValueError, OSError) as e:
            return False, str(e)[:240]
