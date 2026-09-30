"""talog watch 사건 메일 발송기 — SMTP(STARTTLS/SSL/평문 릴레이) + outbox(.eml) 보관.

보안: 비밀번호는 설정 파일에 평문으로 쓰지 않고 환경변수(`email.password_env`, 기본
TALOG_SMTP_PASSWORD)로만 읽는다. 리플레이와 dry_run 은 SMTP 에 접속하지 않고
outbox 에 .eml 만 남긴다(설치 초기 점검·감사용).

폭주 방지: 첫 경보 뒤 batch_seconds 동안 모은 경보를 한 통으로 보내고, 메일 사이
최소 간격(min_interval_min)과 시간당 상한(max_per_hour)을 지킨다. 막힌 동안의 경보는
다음 메일에 합쳐진다(IncidentAgent 가 묶음을 유지).
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import smtplib
import ssl
import threading
from collections import deque
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from . import __version__

_SEV_COLOR = {"crit": "#c62828", "warn": "#e65100", "info": "#546e7a"}
_SEV_KO = {"crit": "심각", "warn": "주의", "info": "정보"}


# 콘솔 UI 의 SMTP 프리셋 (값은 각 서비스 공개 안내 기준 — 조직 정책에 따라 다를 수 있음)
SMTP_PRESETS = {
    "m365_graph": {"label": "Microsoft 365 — Graph API (권장)", "transport": "graph",
                   "help": "IT 관리자가 Entra ID 앱 등록 1회(Mail.Send 애플리케이션 권한 + 관리자 "
                           "동의, 권장: Application Access Policy 로 보낼 사서함 1개만 허용)를 해 "
                           "주면 됩니다. 테넌트 ID(또는 회사 도메인)·앱 ID·클라이언트 암호·보낼 "
                           "사서함을 넣습니다. 비밀번호 폐지와 무관하게 동작합니다."},
    "m365_direct": {"label": "Microsoft 365 — Direct Send (사내 수신 전용)", "transport": "smtp",
                    "smtp_host": "", "smtp_port": 25, "security": "starttls", "mx": True,
                    "help": "받는 사람 도메인의 MX 서버로 인증 없이 바로 넣습니다(사내 주소로만). "
                            "공장 공인 IP 가 SPF 에 없으면 정크함으로 갈 수 있으니, 안정적으로 "
                            "쓰려면 IT 에 IP 를 SPF 또는 수신 커넥터로 등록해 달라고 하십시오."},
    "gmail": {"label": "Gmail / Google Workspace", "smtp_host": "smtp.gmail.com",
              "smtp_port": 587, "security": "starttls", "transport": "smtp",
              "help": "Google 계정 2단계 인증을 켠 뒤 '앱 비밀번호'(16자리)를 만들어 비밀번호 "
                      "칸에 넣습니다. 계정 비밀번호는 쓰지 않습니다."},
    "office365": {"label": "Microsoft 365 — SMTP 인증", "smtp_host": "smtp.office365.com",
                  "smtp_port": 587, "security": "starttls", "transport": "smtp",
                  "help": "Exchange Online 은 SMTP 기본 인증(아이디·비밀번호)을 폐지하는 중이라 "
                          "막혀 있을 가능성이 큽니다. 실패하면 Graph API 또는 Direct Send 를 "
                          "쓰십시오."},
    "naver": {"label": "네이버 메일", "smtp_host": "smtp.naver.com", "smtp_port": 587,
              "security": "starttls", "transport": "smtp",
              "help": "네이버 메일 환경설정에서 IMAP/SMTP 사용을 켜고, 2단계 인증 계정은 "
                      "애플리케이션 비밀번호를 넣습니다."},
    "relay": {"label": "사내 SMTP 릴레이 (인증 없음)", "smtp_host": "", "smtp_port": 25,
              "security": "none", "transport": "smtp",
              "help": "공장망 메일 릴레이 주소·포트를 IT 담당자에게 받습니다. 사용자 이름을 "
                      "비우면 인증 없이 보냅니다."},
}


def mx_hosts(domain: str) -> list[str]:
    """도메인의 MX 서버 (Windows nslookup). Direct Send 프리셋용 — 실패하면 빈 목록."""
    import re
    import subprocess
    domain = (domain or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", domain):
        return []
    try:
        r = subprocess.run(["nslookup", "-type=mx", domain], capture_output=True, text=True,
                           timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return []
    found = re.findall(r"preference\s*=\s*(\d+),\s*mail exchanger\s*=\s*(\S+)", r.stdout, re.I)
    return [h.rstrip(".") for _p, h in sorted(found, key=lambda x: int(x[0]))]


def _as_list(v) -> list[str]:
    if not v:
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.replace(";", ",").split(",") if x.strip()]
    return [str(x).strip() for x in v if str(x).strip()]


class Mailer:
    def __init__(self, cfg: dict, alert_dir: str, replay: bool = False):
        self.cfg = cfg
        self.e = cfg.get("email") or {}
        self.enabled = bool(self.e.get("enabled"))
        self.transport = str(self.e.get("transport", "smtp") or "smtp").lower()
        self._graph = None
        self.replay = replay
        self.dry_run = replay or bool(self.e.get("dry_run"))
        self.min_severity = str(self.e.get("min_severity", "crit"))
        self.site = cfg.get("site", "") or "설비"
        self.outbox = os.path.join(alert_dir, "outbox_replay" if replay else "outbox")
        self._slots: deque = deque()            # 예약·발송 시각 (간격·상한 계산)
        self._lock = threading.Lock()

    # ── 수신자·속도 제한 ────────────────────────────────────────
    def recipients(self, roles) -> list[str]:
        out: list[str] = []
        for addr in _as_list(self.e.get("to")):
            if addr not in out:
                out.append(addr)
        role_map = self.e.get("roles") or {}
        for r in roles or []:
            for addr in _as_list(role_map.get(r)):
                if addr not in out:
                    out.append(addr)
        return out

    def is_immediate(self, severity: str) -> bool:
        """이 등급은 묶음을 기다리지 않고 바로 보낸다 (email.immediate)."""
        imm = self.e.get("immediate")
        if imm is None:
            imm = ["crit"]
        return str(severity).lower() in [str(x).lower() for x in _as_list(imm)]

    def next_allowed(self, immediate: bool = False) -> float:
        """다음 메일을 보낼 수 있는 가장 이른 시각. 즉시 등급은 간격 제한 없이
        시간당 상한만 본다."""
        gap = 0.0 if immediate else float(self.e.get("min_interval_min", 5)) * 60
        cap = int(self.e.get("max_per_hour", 10))
        with self._lock:
            while len(self._slots) > max(cap, 1) * 2:
                self._slots.popleft()
            t = self._slots[-1] + gap if self._slots else 0.0
            if cap and len(self._slots) >= cap:
                t = max(t, self._slots[-cap] + 3600)
        return t

    def can_send(self, now: float) -> tuple[bool, str]:
        t = self.next_allowed()
        if now < t:
            return False, f"{(t - now) / 60:.1f}분 뒤 발송 가능(간격·시간당 상한)"
        return True, ""

    def reserve(self, now: float) -> float:
        """사건 마감 시점에 발송 몫을 잡는다 (분석 중 다음 사건이 끼어들지 않게)."""
        with self._lock:
            self._slots.append(now)
        return now

    def release(self, token: float):
        """메일을 보내지 않기로 한 사건의 몫을 돌려준다."""
        with self._lock:
            try:
                self._slots.remove(token)
            except ValueError:
                pass

    # ── 본문 구성 ───────────────────────────────────────────────
    def subject(self, inc: dict, with_gate: bool = True) -> str:
        v = inc["verdict"]
        sev = v["severity"].upper()
        # 경보 제목의 설명 꼬리("— 미검사 임박")와 원인 설명("— …")은 본문에만 둔다
        title = inc["alerts"][0]["title"].split(" — ")[0]
        cause = (v.get("cause_name") or "").split(" — ")[0]
        tail = f" — {cause}" if cause and v.get("cause") != "unknown" else ""
        gate = {"agree": "룰·LLM 일치", "disagree": "판단 불일치·확인 필요",
                "llm_only": "LLM 의견·확인 필요", "rule_only": "룰 판단",
                "none": ""}.get(v.get("mode"), "")
        if v.get("label", "").startswith("오경보"):
            gate = "오경보 의심"
        s = f"[talog][{sev}][{self.site}] {title}{tail}" + \
            (f" ({gate})" if gate and with_gate else "")
        return s[:180]

    def _texts(self, inc: dict) -> dict:
        v = inc["verdict"]
        acts = [(a, inc.get("action_text", {}).get(a, a)) for a in v.get("actions", [])]
        roles = [inc.get("role_text", {}).get(r, r) for r in v.get("notify", [])]
        return {"acts": acts, "roles": roles}

    def body_text(self, inc: dict) -> str:
        v, ev = inc["verdict"], inc.get("evidence", {})
        t = self._texts(inc)
        lines = [f"[{_SEV_KO.get(v['severity'], v['severity'])}] {self.site} · "
                 f"{inc['opened']} · {inc['alerts'][0]['title']}", "",
                 f"판단: {v['label']}"]
        if v.get("cause_name"):
            lines.append(f"원인: {v['cause_name']}" + (
                f" / LLM: {v['llm_cause_name']}" if v.get("llm_cause_name")
                and v.get("llm_cause") != v.get("cause") else ""))
        lines += ["", "요약: " + v.get("message", ""), ""]
        if t["acts"]:
            lines.append("권고 조치:")
            lines += [f"  {i}. {txt}" for i, (_a, txt) in enumerate(t["acts"], 1)]
        if t["roles"]:
            lines.append("담당: " + ", ".join(t["roles"]))
        rule = inc.get("rule") or {}
        if rule.get("facts"):
            lines += ["", "룰 진단 근거:"] + [f"  - {f}" for f in rule["facts"]]
        llm = inc.get("llm") or {}
        if llm.get("ok"):
            lines += ["", f"LLM 의견 ({llm.get('model')}, {llm.get('device')}, "
                          f"{llm.get('wall_s')}s):"] + \
                     [f"  - {x}" for x in llm.get("evidence", [])]
        elif llm:
            lines += ["", f"LLM 응답 실패: {llm.get('error', '')}"]
        lines += ["", f"경보 {len(inc['alerts'])}건" + (
            f" (+ 한도 초과 {inc['dropped']}건)" if inc.get("dropped") else "") + ":"]
        lines += [f"  {a['time']} [{a['severity']}] {a['title']} — {a['evidence']}"
                  for a in inc["alerts"][:20]]
        if ev.get("log_lines"):
            lines += ["", "근거 로그(요약):"] + [f"  {x}" for x in ev["log_lines"]]
        for s in inc.get("similar") or []:
            lines.append(f"유사 사례({s['score']}): {s['title']} — 원인 {s['cause']} / "
                         f"조치 {s['action']}")
        lines += ["", f"talog watch {__version__} · 사건 {inc['id']} · 자동 발송 메일입니다."]
        return "\n".join(lines)

    def body_html(self, inc: dict) -> str:
        v, ev = inc["verdict"], inc.get("evidence", {})
        t = self._texts(inc)
        E = html.escape
        col = _SEV_COLOR.get(v["severity"], "#333")
        mode_col = {"agree": "#2e7d32", "rule_only": "#455a64"}.get(v.get("mode"), "#e65100")
        if v.get("label", "").startswith("오경보"):
            mode_col = "#6d4c41"
        td = "padding:6px 10px;border-bottom:1px solid #e5e7eb;vertical-align:top"
        th = ("padding:6px 10px;border-bottom:1px solid #d1d5db;text-align:left;"
              "background:#f3f4f6;font-weight:600")
        parts = [
            "<div style=\"font-family:'Malgun Gothic','Apple SD Gothic Neo',sans-serif;"
            "color:#111827;font-size:14px;line-height:1.55;max-width:760px\">",
            f"<div style='border-left:6px solid {col};padding:10px 14px;background:#fafafa'>"
            f"<div style='font-size:12px;color:#6b7280'>{E(self.site)} · {E(inc['opened'])}"
            f" · 사건 {E(inc['id'])}</div>"
            f"<div style='font-size:18px;font-weight:700;margin-top:2px'>"
            f"<span style='color:{col}'>[{E(_SEV_KO.get(v['severity'], v['severity']))}]</span> "
            f"{E(inc['alerts'][0]['title'])}</div>"
            f"<div style='margin-top:6px'><span style='display:inline-block;padding:2px 8px;"
            f"border-radius:10px;background:{mode_col};color:#fff;font-size:12px'>"
            f"{E(v['label'])}</span>"
            + (f" <b>원인:</b> {E(v['cause_name'])}" if v.get("cause_name") else "")
            + (f" <span style='color:#6b7280'>/ LLM: {E(v['llm_cause_name'])}</span>"
               if v.get("llm_cause_name") and v.get("llm_cause") != v.get("cause") else "")
            + "</div></div>",
            f"<p style='margin:14px 0 6px'>{E(v.get('message', ''))}</p>"]
        if t["acts"]:
            parts.append("<div style='font-weight:700;margin-top:12px'>권고 조치</div><ol style="
                         "'margin:4px 0 0 18px;padding:0'>" + "".join(
                             f"<li>{E(txt)}</li>" for _a, txt in t["acts"]) + "</ol>")
        if t["roles"]:
            parts.append(f"<div style='margin-top:6px'><b>담당:</b> {E(', '.join(t['roles']))}"
                         "</div>")
        rule = inc.get("rule") or {}
        llm = inc.get("llm") or {}
        if rule or llm:
            rows = ""
            if rule:
                rows += (f"<tr><td style='{td};width:110px'><b>룰 진단</b></td><td style='{td}'>"
                         f"{E(rule.get('name', ''))}<ul style='margin:4px 0 0 18px;padding:0'>"
                         + "".join(f"<li>{E(f)}</li>" for f in rule.get("facts", []))
                         + "</ul></td></tr>")
            if llm.get("ok"):
                rows += (f"<tr><td style='{td}'><b>LLM 의견</b><div style='font-size:11px;"
                         f"color:#6b7280'>{E(str(llm.get('model')))}<br>{E(str(llm.get('device')))}"
                         f" · {llm.get('wall_s')}s</div></td><td style='{td}'>"
                         f"{E(v.get('llm_cause_name') or '')}<ul style='margin:4px 0 0 18px;"
                         "padding:0'>" + "".join(f"<li>{E(x)}</li>" for x in llm.get("evidence", []))
                         + "</ul>" + ("" if (llm.get("fact") or {}).get("ok", True) else
                                      f"<div style='color:#b45309;font-size:12px'>근거에 없는 수치 "
                                      f"{E(', '.join(llm['fact']['unsupported']))} — LLM 문구 대신 룰 "
                                      "문구를 사용했습니다</div>") + "</td></tr>")
            elif llm:
                rows += (f"<tr><td style='{td}'><b>LLM 의견</b></td><td style='{td};color:#b45309'>"
                         f"응답 실패: {E(llm.get('error', ''))}</td></tr>")
            parts.append("<div style='font-weight:700;margin-top:14px'>판단 근거</div>"
                         f"<table style='border-collapse:collapse;width:100%;font-size:13px'>{rows}"
                         "</table>")
        arows = "".join(
            f"<tr><td style='{td};white-space:nowrap'>{E(a['time'] or '')}</td>"
            f"<td style='{td};color:{_SEV_COLOR.get(a['severity'], '#333')};font-weight:700'>"
            f"{E(a['severity'])}</td><td style='{td}'>{E(a['title'])}<div style='color:#6b7280;"
            f"font-size:12px'>{E(a['evidence'])}</div></td></tr>" for a in inc["alerts"][:20])
        parts.append(f"<div style='font-weight:700;margin-top:14px'>경보 {len(inc['alerts'])}건"
                     + (f" (+ 한도 초과 {inc['dropped']}건)" if inc.get("dropped") else "")
                     + "</div><table style='border-collapse:collapse;width:100%;font-size:13px'>"
                     f"<tr><th style='{th}'>시각</th><th style='{th}'>심각도</th>"
                     f"<th style='{th}'>내용</th></tr>{arows}</table>")
        if ev.get("log_lines"):
            parts.append("<div style='font-weight:700;margin-top:14px'>근거 로그 (요약)</div>"
                         "<pre style='background:#f8fafc;border:1px solid #e5e7eb;padding:8px;"
                         "font-size:12px;white-space:pre-wrap'>"
                         + E("\n".join(ev["log_lines"])) + "</pre>")
        for s in inc.get("similar") or []:
            parts.append(f"<div style='font-size:12px;margin-top:6px'>유사 사례({s['score']}): "
                         f"{E(s['title'])} — 원인 {E(s['cause'])} / 조치 {E(s['action'])}</div>")
        parts.append(f"<div style='margin-top:18px;font-size:11px;color:#9ca3af'>talog watch "
                     f"{E(__version__)} · 자동 발송 메일입니다. 근거 전체는 첨부 JSON 과 "
                     f"alert_dir 의 incidents_*.jsonl 에 있습니다.</div></div>")
        return "".join(parts)

    def compose(self, inc: dict, to: list[str], reply_to: str = "",
                tag: str = "") -> EmailMessage:
        msg = EmailMessage()
        sender = self.e.get("sender") or self.e.get("username") or "talog@localhost"
        subj = self.subject(inc, with_gate=not tag)      # 후속 메일은 태그가 판단을 말한다
        if reply_to:
            subj = f"Re: {subj}"
        if tag:
            subj = f"{subj} {tag}"
        msg["Subject"] = subj[:200]
        msg["From"] = sender
        msg["To"] = ", ".join(to) if to else "undisclosed-recipients:;"
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain="talog.local")
        if reply_to:                              # 같은 사건 스레드로 묶이게
            msg["In-Reply-To"] = reply_to
            msg["References"] = reply_to
        msg["X-Talog-Incident"] = inc["id"]
        msg.set_content(self.body_text(inc))
        msg.add_alternative(self.body_html(inc), subtype="html")
        if self.e.get("attach_json", True):
            data = json.dumps({k: inc.get(k) for k in ("id", "site", "family", "alerts",
                                                       "verdict", "rule", "llm", "evidence")},
                              ensure_ascii=False, indent=1, default=str).encode("utf-8")
            msg.add_attachment(data, maintype="application", subtype="json",
                               filename=f"incident_{inc['id']}.json")
        return msg

    # ── 발송 ──────────────────────────────────────────────────
    def _connect(self):
        host = str(self.e.get("smtp_host") or "")
        if not host:
            raise ValueError("email.smtp_host 가 비어 있습니다")
        port = int(self.e.get("smtp_port") or 587)
        sec = str(self.e.get("security", "starttls")).lower()
        timeout = float(self.e.get("timeout_s", 20))
        ctx = ssl.create_default_context()
        if sec == "ssl":
            s = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx)
        else:
            s = smtplib.SMTP(host, port, timeout=timeout)
            s.ehlo()
            if sec == "starttls":
                s.starttls(context=ctx)
                s.ehlo()
        user = str(self.e.get("username") or "")
        if user:
            pw = self.password()
            if not pw:
                s.close()
                raise ValueError("SMTP 비밀번호가 없습니다 (콘솔에서 입력하거나 환경변수 "
                                 f"{self.e.get('password_env') or 'TALOG_SMTP_PASSWORD'} 설정)")
            s.login(user, pw)
        return s

    def password(self) -> str:
        """환경변수 → DPAPI 암호문 순으로 찾는다 (평문 저장은 지원하지 않음)."""
        env = str(self.e.get("password_env") or "TALOG_SMTP_PASSWORD")
        if os.environ.get(env):
            return os.environ[env]
        blob = str(self.e.get("password_dpapi") or "")
        if blob:
            try:
                from .secret import unprotect
                return unprotect(blob)
            except (OSError, ValueError) as e:
                print(f"  ! 저장된 SMTP 비밀번호 복호화 실패(다른 PC/사용자에서 저장?): {e}")
        return ""

    def password_source(self) -> str:
        if self.transport == "graph":
            g = self.e.get("graph") or {}
            env = str(g.get("secret_env") or "TALOG_GRAPH_SECRET")
            if os.environ.get(env):
                return f"환경변수 {env}"
            return "암호화 저장(DPAPI)" if g.get("client_secret_dpapi") else ""
        env = str(self.e.get("password_env") or "TALOG_SMTP_PASSWORD")
        if os.environ.get(env):
            return f"환경변수 {env}"
        if self.e.get("password_dpapi"):
            return "암호화 저장(DPAPI)"
        return ""

    def graph_secret(self) -> str:
        """Graph 클라이언트 암호: 환경변수 → DPAPI 암호문 순."""
        g = self.e.get("graph") or {}
        env = str(g.get("secret_env") or "TALOG_GRAPH_SECRET")
        if os.environ.get(env):
            return os.environ[env]
        blob = str(g.get("client_secret_dpapi") or "")
        if blob:
            try:
                from .secret import unprotect
                return unprotect(blob)
            except (OSError, ValueError) as e:
                print(f"  ! 저장된 Graph 암호 복호화 실패(다른 PC/사용자에서 저장?): {e}")
        return ""

    def graph(self):
        if self._graph is None:
            from .graphmail import GraphSender
            self._graph = GraphSender(self.e.get("graph") or {},
                                      lambda: self.graph_secret(),
                                      timeout=float(self.e.get("timeout_s", 20)))
        return self._graph

    def deliver(self, msg: EmailMessage, to: list[str] | None = None) -> tuple[bool, str]:
        if self.transport == "graph":
            return self.graph().send(msg, list(to or []))
        try:
            s = self._connect()
            try:
                s.send_message(msg)
            finally:
                try:
                    s.quit()
                except (smtplib.SMTPException, OSError):
                    pass
            return True, ""
        except (smtplib.SMTPException, OSError, ValueError, ssl.SSLError) as e:
            return False, f"{type(e).__name__}: {e}"[:300]

    def _save(self, msg: EmailMessage, inc_id: str) -> str:
        try:
            os.makedirs(self.outbox, exist_ok=True)
            path = os.path.join(self.outbox, f"{inc_id}.eml")
            with open(path, "wb") as f:
                f.write(msg.as_bytes())
            return path
        except OSError as e:
            print(f"  ! outbox 저장 실패(계속): {e}")
            return ""

    def send_incident(self, inc: dict, reply_to: str = "", tag: str = "",
                      suffix: str = "") -> dict:
        to = self.recipients(inc["verdict"].get("notify"))
        msg = self.compose(inc, to, reply_to=reply_to, tag=tag)
        res = {"to": to, "subject": msg["Subject"], "sent": False, "error": "",
               "eml": "", "dry_run": self.dry_run, "message_id": msg["Message-ID"]}
        if self.e.get("outbox", True) or self.dry_run:
            res["eml"] = self._save(msg, inc["id"] + suffix)
        if self.dry_run:
            return res
        if not to:
            res["error"] = "수신자 없음 (email.to / email.roles 확인)"
            return res
        res["sent"], res["error"] = self.deliver(msg, to)
        return res

    # ── 설치 점검 ───────────────────────────────────────────────
    def check(self) -> tuple[bool, str]:
        """SMTP 접속·로그인(또는 Graph 토큰 발급)만 확인한다 (메일은 보내지 않음)."""
        if self.transport == "graph":
            return self.graph().check()
        try:
            s = self._connect()
            s.quit()
            return True, "접속·인증 OK"
        except (smtplib.SMTPException, OSError, ValueError, ssl.SSLError) as e:
            return False, f"{type(e).__name__}: {e}"[:200]


def sample_incident(site: str) -> dict:
    """--test-email 용 예시 사건 (PC3 2026-07-27 NoInspThread 형태, 수치는 예시)."""
    now = dt.datetime.now().strftime("%H:%M:%S")
    return {
        "id": dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-test",
        "site": site, "opened": now, "closed": now, "family": "reject", "dropped": 0,
        "alerts": [{"time": now, "severity": "crit", "rule": "no_insp_thread",
                    "title": "[테스트] 검사 시작 거부(NoInspThread) — 미검사 임박",
                    "evidence": "talog watch --test-email 이 보낸 설치 점검 메일입니다."}],
        "verdict": {"mode": "agree", "label": "룰·LLM 판단 일치", "severity": "crit",
                    "cause": "throughput_saturation",
                    "cause_name": "처리량 포화 — 검사 소요가 투입 간격보다 길어 Seq 스레드 고갈",
                    "actions": ["avoid_restart_now", "check_gpu_load"],
                    "notify": [], "message": "메일 경로 점검용 예시입니다. 이 메일이 보이면 "
                                             "talog watch 의 이메일 알림 설정이 정상입니다."},
        "action_text": {"avoid_restart_now": "진행 중 검사가 끝날 때까지 재시작 보류",
                        "check_gpu_load": "GPU 부하·동시 인퍼런스 점검"},
        "role_text": {}, "rule": {"name": "처리량 포화", "facts": ["예시 근거"]},
        "llm": None, "evidence": {"log_lines": ["(테스트 메일 — 로그 없음)"]}}
