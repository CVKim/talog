"""talog 콘솔 — AI 비전 로그 운영 플랫폼의 로컬 웹 화면.

    talog [--config talog.yaml] [--port 8778] [--no-open]

화면 4개가 에이전트 흐름을 그대로 따른다:
  운영 현황  수집 → 탐지 → 판단 → 알림 상태와 오늘의 사건·경보
  사건       사건별 근거·룰 진단·LLM 의견·메일, 사건에 대한 질문
  분석       로그 폴더 진단 리포트·경보 재현(리플레이)·리포트에 대한 자연어 질문
  설정       수집·탐지(경보 규칙 한 표)·판단(AI)·알림(메일) — talog.yaml 로 저장

- 실시간 감시(LiveWatch)는 작업 스레드로 돈다. 설정을 저장하면 파일을 .bak 으로
  백업한 뒤 다시 쓰고 감시를 새 설정으로 재시작한다.
- 127.0.0.1 에서만 연다. POST 는 페이지에 심은 세션 토큰(X-Talog-Token)이 있어야
  받고 Host 헤더도 확인한다(다른 사이트의 위조 요청·DNS 리바인딩 차단).
- 메일 비밀번호·클라이언트 암호는 평문으로 저장하지 않는다: 입력하면 Windows DPAPI
  로 암호화해 mail.secret 에 넣고, 화면·API 로 다시 내보내지 않는다.
"""

from __future__ import annotations

import copy
import datetime as dt
import glob
import html
import io
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from collections import Counter, deque
from fnmatch import fnmatchcase
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__, settings
from .fileclass import classify
from .tracker import PRESETS, compile_patterns, resolve_targets

_PAGE = os.path.join(os.path.dirname(__file__), "console.html")
_ASSETS = os.path.join(os.path.dirname(__file__), "assets")
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TAG_RE = re.compile(r"^[\w.#\- ()가-힣]{1,120}$")


def _latest_day_dir(root: str) -> str:
    days = recent_days(root, 1)
    return days[0] if days else ""


def recent_days(root: str, n: int = 14) -> list[str]:
    """log_root 아래 최근 YYYY_MM\\DD 폴더 (새것부터)."""
    got = [p for p in glob.glob(os.path.join(root, "[0-9][0-9][0-9][0-9]_[0-9][0-9]",
                                             "[0-9][0-9]")) if os.path.isdir(p)]
    return sorted(got, reverse=True)[:n]


def _recipe_defects(day_dir: str) -> tuple[list[str], list[list]]:
    """comm.log 에서 (레시피 결함명 목록, 그날 NG 결함 빈도)를 뽑는다 (꼬리 8MB)."""
    path = ""
    try:
        for n in os.listdir(day_dir):
            if n.lower() == "comm.log":
                path = os.path.join(day_dir, n)
    except OSError:
        return [], []
    if not path:
        return [], []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - 8 * 1048576))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return [], []
    names: list[str] = []
    cnt: Counter = Counter()
    for ln in text.splitlines():
        if "V2M_MODEL_LOAD_DEFECT_NAME," in ln:
            toks = [t.strip() for t in ln.split("V2M_MODEL_LOAD_DEFECT_NAME,", 1)[1].split(",")]
            # V3.0, TALOSn, <개수>, 결함명...
            if len(toks) > 3 and toks[2].isdigit():
                names = [t for t in toks[3:] if t]
        elif "V2M_INSPECT_END," in ln and ",NG," in ln:
            toks = [t.strip() for t in ln.split(",")]
            try:
                i = toks.index("NG")
            except ValueError:
                continue
            j = i + 2 if i + 1 < len(toks) and toks[i + 1].isdigit() else i + 1
            for t in toks[j:]:
                if t.isdigit() and len(t) >= 15:
                    break
                if t and not t.isdigit():
                    cnt[t] += 1
    return names, [[k, v] for k, v in cnt.most_common(30)]


def _report_tag(day_dir: str) -> str:
    """리포트 이름: ...\\2026_09\\28 → 2026_09_28, 그 밖은 마지막 두 폴더 이름."""
    parts = [p for p in re.split(r"[\\/]+", os.path.normpath(day_dir)) if p]
    tail = parts[-2:] if len(parts) >= 2 else parts
    tag = "_".join(tail)
    return re.sub(r"[^\w.\-가-힣]+", "_", tag).strip("_") or "report"


class _Tee(io.TextIOBase):
    """콘솔 창 출력은 그대로 두고 최근 줄을 화면의 실행 로그용으로 보관한다."""

    def __init__(self, stream, buf: deque):
        self.stream = stream
        self.buf = buf
        self._part = ""

    def write(self, s):
        try:
            self.stream.write(s)
        except (UnicodeError, OSError, ValueError):
            pass
        self._part += s
        while "\n" in self._part:
            line, self._part = self._part.split("\n", 1)
            self.buf.append(f"{dt.datetime.now():%H:%M:%S} {line}")
        return len(s)

    def flush(self):
        try:
            self.stream.flush()
        except (OSError, ValueError):
            pass


# ---------------------------------------------------------------------------
class Console:
    def __init__(self, config_path: str, port: int = 8778):
        self.config_path = os.path.abspath(config_path)
        self.port = port
        self.settings, self.legacy = settings.load(self.config_path)
        self.cfg = settings.compile(self.settings)
        self.token = secrets.token_urlsafe(24)
        self.log: deque = deque(maxlen=400)
        self.watch = None
        self._stop = None
        self._thread = None
        self._lock = threading.RLock()
        self.job: dict = {"state": "idle"}
        self.ask_job: dict = {"state": "idle"}     # Claude 에게 바로 묻기 — 지금(또는 마지막) 질문 하나
        self.ask_thread: dict = {"session": "", "turns": []}   # 한 대화 (이어 묻기는 같은 Claude 세션을 이어 간다)
        self.claude_usage: dict = {}               # 마지막으로 본 구독 사용량 (rate_limit_event)
        self._dbs: dict[str, sqlite3.Connection] = {}
        self._db_lock = threading.Lock()
        self._llm_alive = (0.0, False)

    # ── 감시 수명 ───────────────────────────────────────────────
    def start_watch(self):
        from .watch import LiveWatch
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop = threading.Event()
            self.watch = LiveWatch(copy.deepcopy(self.cfg), self._stop)
            self._thread = threading.Thread(target=self.watch.run, name="talog-watch",
                                            daemon=True)
            self._thread.start()

    def stop_watch(self):
        with self._lock:
            if self._stop is not None:
                self._stop.set()
            if self._thread is not None:
                self._thread.join(timeout=float(self.cfg.get("poll_seconds", 20)) + 30)
            self._thread = None

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def data_dir(self) -> str:
        return self.watch.notifier.alert_dir if self.watch else self.cfg["alert_dir"]

    # ── 설정 ───────────────────────────────────────────────────
    def public_settings(self) -> dict:
        s = copy.deepcopy(self.settings)
        m = s["mail"]
        m["has_secret"] = bool(m.get("secret"))
        m["secret"] = ""
        return s

    def meta(self) -> dict:
        from .mailer import Mailer
        try:
            from .agent import Runbook
            roles = Runbook().roles
        except Exception:
            roles = {}
        env = ""
        try:
            env = Mailer(self.cfg, self.data_dir, replay=True).password_source()
        except Exception:
            pass
        return {"builtin": [{"name": b[0], "label": b[1], "severity": b[2], "kind": b[3],
                             "default": b[4]} for b in settings.BUILTIN],
                "presets": PRESETS, "providers": settings.PROVIDERS, "roles": roles,
                "version": __version__, "config": self.config_path,
                "legacy": self.legacy, "secret_env": env if env.startswith("환경") else ""}

    @staticmethod
    def _protect(text: str) -> str:
        from .secret import available, protect
        if not available():
            raise ValueError("이 OS 에서는 비밀 암호화 저장을 지원하지 않습니다 — 환경변수 "
                             "TALOG_SMTP_PASSWORD / TALOG_GRAPH_SECRET 를 쓰십시오")
        return protect(text)

    def _with_secret(self, new: dict, secret: str = "", clear: bool = False) -> dict:
        """화면 값 + 비밀. 발송 방식이 바뀌면 옛 비밀은 버린다 (다른 계정의 비밀이므로)."""
        s = settings.normalize(copy.deepcopy(new or {}))
        s["mail"].pop("has_secret", None)
        old = self.settings["mail"]
        same = (s["mail"].get("provider") == old.get("provider")
                and s["mail"].get("account") == old.get("account"))
        s["mail"]["secret"] = "" if clear or not same else old.get("secret", "")
        if secret:
            s["mail"]["secret"] = self._protect(secret)
        return s

    def save(self, new: dict, secret: str = "", clear: bool = False) -> list[str]:
        s = self._with_secret(new, secret, clear)
        errs = settings.validate(s)
        if errs:
            return errs
        text = settings.dump(s)
        import yaml
        yaml.safe_load(text)                      # 쓰기 전에 다시 읽혀야 한다
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "rb") as f:
                    old = f.read()
                with open(self.config_path + ".bak", "wb") as f:
                    f.write(old)
            except OSError as e:
                return [f"백업 실패: {e}"]
        os.makedirs(os.path.dirname(self.config_path) or ".", exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write(text)
        was = self.running()
        self.stop_watch()
        self.settings, self.legacy = s, False
        self.cfg = settings.compile(s)
        if was:
            self.start_watch()
        print(f"[콘솔] 설정 저장: {self.config_path} (이전 파일 .bak) — 감시 "
              f"{'재시작' if was else '정지 상태 유지'}")
        return []

    # ── 운영 현황 ───────────────────────────────────────────────
    def _llm_up(self) -> bool:
        t, up = self._llm_alive
        if time.time() - t > 30:
            from .llm import OllamaClient
            cli = OllamaClient(self.cfg["llm"])
            up = cli.alive() and cli.model in cli.models()
            self._llm_alive = (time.time(), up)
        return up

    def status(self) -> dict:
        from .llm import query_gpu_free, resolve_device
        snap = self.watch.snapshot() if self.watch else {}
        day = dt.datetime.now().strftime("%Y%m%d")
        sev = Counter()
        last_alert = None
        try:
            with open(os.path.join(self.data_dir, f"alerts_{day}.jsonl"),
                      encoding="utf-8") as f:
                for ln in f:
                    try:
                        a = json.loads(ln)
                    except ValueError:
                        continue
                    sev[a.get("severity", "")] += 1
                    last_alert = a
        except OSError:
            pass
        incs = self.incidents(days=1, replay=False, limit=500)
        s = self.settings
        files = snap.get("files", [])
        last_read = max((f["last"] for f in files if f.get("last")), default=None)
        gfree = query_gpu_free()
        ai = s["ai"]
        dev, why = resolve_device(self.cfg["llm"], gfree)
        m = s["mail"]
        prov = m.get("provider", "off")
        tr = s["tracking"]
        return {
            "running": self.running(), "version": __version__, "site": s.get("site", ""),
            "config": self.config_path, "data_dir": self.data_dir,
            "uptime_s": (time.time() - snap["started"]) if snap.get("started") else 0,
            "collect": {"day_dir": snap.get("day_dir") or self._day_dir(),
                        "exists": bool(self._day_dir()),
                        "tracking": ("직접 선택" if isinstance(tr, list) else
                                     "폴더 전체" if tr == "all" else "핵심 로그"),
                        "files": len(files), "last_read": last_read},
            "detect": {"crit": sev.get("crit", 0), "warn": sev.get("warn", 0),
                       "info": sev.get("info", 0),
                       "rules": len(settings.BUILTIN) + len(s["rules"]["custom"]),
                       "last": last_alert,
                       "pattern_errors": snap.get("pattern_errors", [])},
            "judge": {"llm": bool(ai.get("llm")), "model": ai.get("model"),
                      "url": ai.get("url"), "device": ai.get("device"),
                      "resolved": dev, "reason": why,
                      "alive": self._llm_up() if ai.get("llm") else None,
                      "incidents": len(incs),
                      "agree": sum(1 for i in incs if i.get("gate") == "agree"),
                      "last": incs[0] if incs else None},
            "notify": {"provider": prov,
                       "label": settings.PROVIDERS.get(prov, {}).get("label", prov),
                       "to": len(m.get("to") or []), "digest": bool(m.get("digest")),
                       "has_secret": bool(m.get("secret")),
                       "sent": sum(1 for i in incs if i.get("mail") == "발송"),
                       "failed": sum(1 for i in incs if i.get("mail") == "실패"),
                       "toast": bool(s["notify"].get("toast"))},
            "alerts": snap.get("alerts", [])[:40],
            "incidents": incs[:20],
            "files": files, "gpu": snap.get("gpu", []), "gpu_free": gfree,
            "capacity": snap.get("capacity", []),
            "job": {k: v for k, v in self.job.items() if k != "log"}}

    def _day_dir(self) -> str:
        from .watch import _today_dir
        d = _today_dir(self.cfg["watch_root"])
        return d if os.path.isdir(d) else ""

    def folder(self, folder: str = "", tracking=None) -> dict:
        """폴더 파일 목록(추적 여부)·레시피 결함명·그날 NG 결함, 최근 일자 폴더."""
        root = self.cfg["watch_root"]
        folder = folder or self._day_dir() or _latest_day_dir(root)
        rows = []
        tcfg = self.cfg.get("tracking")
        if tracking is not None:
            tmp = copy.deepcopy(self.settings)
            tmp["tracking"] = tracking
            tcfg = settings.compile(tmp).get("tracking")
        if folder and os.path.isdir(folder):
            tracked = set(resolve_targets(folder, tcfg))
            for n in sorted(os.listdir(folder), key=str.lower):
                p = os.path.join(folder, n)
                if not os.path.isfile(p):
                    continue
                st = os.stat(p)
                fi = classify(p)
                rows.append({"name": n, "size": st.st_size,
                             "mtime": dt.datetime.fromtimestamp(st.st_mtime).strftime(
                                 "%m/%d %H:%M"),
                             "category": fi.category if fi else "",
                             "tracked": p in tracked})
        names, seen = _recipe_defects(folder) if folder else ([], [])
        return {"folder": folder, "today": self._day_dir(), "files": rows,
                "recipe_defects": names, "seen_defects": seen,
                "days": recent_days(root)}

    # ── 사건 ───────────────────────────────────────────────────
    def incidents(self, days: int = 3, replay: bool = True, limit: int = 200) -> list[dict]:
        files = sorted(glob.glob(os.path.join(self.data_dir, "incidents_*.jsonl")),
                       reverse=True)
        cutoff = (dt.datetime.now() - dt.timedelta(days=days - 1)).strftime("%Y%m%d")
        rows = []
        for p in files:
            base = os.path.basename(p)
            is_rep = base.endswith("_replay.jsonl")
            if is_rep and not replay:
                continue
            m = re.search(r"incidents_(\d{8})", base)
            if not is_rep and m and m.group(1) < cutoff:
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    lines = f.readlines()
            except OSError:
                continue
            for ln in lines:
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                v = d.get("verdict") or {}
                llm = d.get("llm") or {}
                em = d.get("email") or {}
                rows.append({
                    "id": d.get("id"), "replay": is_rep,
                    "day": (d.get("id") or "")[:8] or (m.group(1) if m else ""),
                    "time": d.get("opened"), "site": d.get("site"),
                    "title": (d.get("alerts") or [{}])[0].get("title", ""),
                    "n_alerts": len(d.get("alerts") or []),
                    "severity": v.get("severity"), "cause": v.get("cause_name"),
                    "llm": (llm.get("root_cause") if llm.get("ok") else
                            ("실패" if llm else "")),
                    "mode": v.get("label"), "gate": v.get("mode"),
                    "mail": ("발송" if em.get("sent") else "보관" if em.get("eml") else
                             ("실패" if em.get("error") else "-"))})
        rows.sort(key=lambda r: (r["day"], r["time"] or ""), reverse=True)
        return rows[:limit]

    def timeline(self, days: int = 7) -> dict:
        """날짜별·시간별 경보(심각·주의·정보 수)와 처리량·거부·최저 일시 지연 내성 — 운영 현황의 상태 띠와 추세.
        실시간 감시 기록과 경보 재현 기록을 따로 줄로 낸다. 줄마다 마지막 기록 시간의 그룹별 값(last)도 붙인다
        — 실시간 처리 여유가 아직 없을 때 화면이 최근 기록으로 그룹별 여유를 보여 준다."""
        rows: dict[tuple[str, bool], dict] = {}
        hours: dict[tuple[str, bool], dict[int, list]] = {}

        def row(day: str, rep: bool) -> dict:
            return rows.setdefault((day, rep), {
                "day": day, "replay": rep, "alerts": [[0, 0, 0] for _ in range(24)],
                "n": [None] * 24, "groups": [0] * 24, "rejects": [None] * 24, "tol_pct": [None] * 24,
                "last": None})
        sev_i = {"crit": 0, "warn": 1, "info": 2}
        for p in glob.glob(os.path.join(self.data_dir, "alerts_*.jsonl")):
            m = re.search(r"alerts_(\d{8})(_replay)?\.jsonl$", os.path.basename(p))
            if not m:
                continue
            r = row(m.group(1), bool(m.group(2)))
            try:
                with open(p, encoding="utf-8") as f:
                    for ln in f:
                        try:
                            a = json.loads(ln)
                            h = int(str(a.get("ts", ""))[11:13])
                        except (ValueError, TypeError):
                            continue
                        if 0 <= h < 24 and a.get("severity") in sev_i:
                            r["alerts"][h][sev_i[a["severity"]]] += 1
            except OSError:
                continue
        for p in glob.glob(os.path.join(self.data_dir, "kpi_*.jsonl")):
            m = re.search(r"kpi_(\d{8})(_replay)?\.jsonl$", os.path.basename(p))
            if not m:
                continue
            r = row(m.group(1), bool(m.group(2)))
            try:
                with open(p, encoding="utf-8") as f:
                    for ln in f:
                        try:
                            k = json.loads(ln)
                            h = int(str(k.get("hour", ""))[11:13])
                        except (ValueError, TypeError):
                            continue
                        if not 0 <= h < 24 or not isinstance(k.get("group"), int):
                            continue
                        # 한 검사가 모든 Seq 그룹을 지나므로 검사 수는 그룹 합이 아니라 그룹 중 최대
                        r["n"][h] = max(r["n"][h] or 0, int(k.get("n") or 0))
                        r["groups"][h] += 1
                        r["rejects"][h] = (r["rejects"][h] or 0) + int(k.get("rejects") or 0)
                        lim = (k.get("pool") or 0) * (k.get("interval_s") or 0)
                        if lim > 0 and k.get("hold_p99") is not None:
                            pct = round(100 * (lim - k["hold_p99"]) / lim, 1)
                            r["tol_pct"][h] = pct if r["tol_pct"][h] is None else min(r["tol_pct"][h], pct)
                        hours.setdefault((r["day"], r["replay"]), {}).setdefault(h, []).append(
                            {x: k.get(x) for x in ("group", "n", "idle_median", "hold_p50", "hold_p99",
                                                   "pool", "interval_s", "rejects")})
            except OSError:
                continue
        for key, by_h in hours.items():
            h = max(by_h)
            rows[key]["last"] = {"hour": h, "groups": sorted(by_h[h], key=lambda g: g["group"])}
        today = dt.datetime.now().strftime("%Y%m%d")
        row(today, False)                                  # 오늘 줄은 기록이 없어도 보인다
        out = sorted(rows.values(), key=lambda r: (r["day"], not r["replay"]), reverse=True)
        return {"today": today, "hour": dt.datetime.now().hour, "days": out[:max(1, min(int(days or 7), 31))]}

    def incident(self, iid: str, replay: bool) -> dict | None:
        pat = "incidents_*_replay.jsonl" if replay else "incidents_[0-9]*[0-9].jsonl"
        for p in sorted(glob.glob(os.path.join(self.data_dir, pat)), reverse=True):
            try:
                with open(p, encoding="utf-8") as f:
                    for ln in f:
                        if f'"id": "{iid}"' in ln:
                            d = json.loads(ln)
                            day = (d.get("id") or "")[:8]
                            if day.isdigit():
                                d["day_dir"] = self._find_day_dir(day)
                            return d
            except (OSError, ValueError):
                continue
        return None

    def _find_day_dir(self, day: str) -> str:
        """사건 날짜(YYYYMMDD)의 원본 로그 폴더 — 설정의 로그 루트와 콘솔에서 고른 폴더에서 찾는다(재현 사건 포함). 없으면 ''."""
        from . import logread
        roots = [self.cfg.get("watch_root", "")] + [r for r in self._log_cfg().get("roots", []) if isinstance(r, str)]
        try:
            return logread.resolve_day([r for r in roots if r], day)
        except ValueError:
            return ""

    def mail_html(self, iid: str, replay: bool, which: str = "initial") -> str:
        from .mailer import Mailer
        inc = self.incident(iid, replay)
        if inc is None:
            return "<p>사건을 찾을 수 없습니다.</p>"
        em = inc.get("email_followup") if which == "followup" else inc.get("email")
        path = (em or {}).get("eml", "")
        if path and os.path.exists(path):
            import email
            from email import policy
            with open(path, "rb") as f:
                msg = email.message_from_binary_file(f, policy=policy.default)
            part = msg.get_body(preferencelist=("html",))
            if part is not None:
                subj = html.escape(str(msg["Subject"]))
                to = html.escape(str(msg["To"]))
                return (f"<div style='font:12px sans-serif;color:#555;margin-bottom:8px'>"
                        f"<b>제목</b> {subj}<br><b>받는 사람</b> {to}</div>"
                        + part.get_content())
        return Mailer(self.cfg, self.data_dir, replay=True).body_html(inc)

    def ask_incident(self, iid: str, replay: bool, question: str) -> dict:
        """사건 기록 안의 사실만으로 LLM 이 답한다 (도구 없음, 근거 밖은 모른다고)."""
        from .llm import OllamaClient
        inc = self.incident(iid, replay)
        if inc is None:
            return {"ok": False, "error": "사건을 찾을 수 없습니다"}
        question = (question or "").strip()[:500]
        if not question:
            return {"ok": False, "error": "질문이 비어 있습니다"}
        ev = inc.get("evidence") or {}
        ctx = {"사건": inc.get("id"), "설비": inc.get("site"), "시각": inc.get("opened"),
               "계열": inc.get("family_title") or inc.get("family"),
               "판단": inc.get("verdict"), "룰 진단": inc.get("rule"),
               "LLM 2차 의견": {k: (inc.get("llm") or {}).get(k) for k in
                             ("root_cause", "evidence", "analysis") if inc.get("llm")},
               "경보": inc.get("alerts"), "핵심 사실": ev.get("key_facts"),
               "근거 로그": (ev.get("log_lines") or [])[-40:]}
        text = json.dumps(ctx, ensure_ascii=False, default=str)[:14000]
        msgs = [{"role": "system", "content":
                 "너는 비전 검사 설비의 사건 분석 보조다. 아래 사건 기록에 있는 사실만으로 "
                 "한국어로 짧게 답한다. 기록에 없는 수치·원인은 지어내지 말고 '기록에 없음'"
                 "이라고 답한다. 조치를 물으면 기록의 권고 조치를 우선한다."},
                {"role": "user", "content": f"[사건 기록]\n{text}\n\n[질문]\n{question}"}]
        r = OllamaClient(self.cfg["llm"]).chat(msgs, num_predict=600)
        if r["error"]:
            return {"ok": False, "error": r["error"], "device": r["device"]}
        return {"ok": True, "answer": r["content"].strip(), "device": r["device"],
                "wall_s": r["wall_s"], "model": self.cfg["llm"].get("model")}

    # ── 분석 (리포트·리플레이·질문) ─────────────────────────────
    @property
    def report_dir(self) -> str:
        return os.path.join(self.data_dir, "reports")

    def start_job(self, kind: str, target: str, recipe: str = "") -> dict:
        if self.job.get("state") == "running":
            return {"ok": False, "error": "이미 작업이 진행 중입니다"}
        target = (target or "").strip().strip('"')
        if not os.path.isdir(target):
            return {"ok": False, "error": f"폴더가 없습니다: {target}"}
        if kind not in ("report", "replay"):
            return {"ok": False, "error": f"알 수 없는 작업: {kind}"}
        out = self.report_dir
        job = {"state": "running", "kind": kind, "target": target, "started": time.time(),
               "log": deque(maxlen=400), "reports": []}
        self.job = job
        # 분석은 별도 프로세스(낮은 우선순위)에서 돌린다. 같은 프로세스의 스레드로 돌리면 GIL 때문에 수 분 동안
        # 콘솔 응답이 수십 초씩 늦어지고(풍산 12일 리포트 중 /api/claude 13.8초), 검사 PC 의 우선순위도 못 낮춘다.
        user = copy.deepcopy(self.settings)                 # 지금 적용 중인 설정 그대로 (메일 비밀은 빼고)
        (user.get("mail") or {}).pop("secret", None)
        fd, cfg_path = tempfile.mkstemp(prefix="talog_job_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(user, f, ensure_ascii=False, default=str)
        cmd = _self_command() + ["_job", kind, target, "--config", cfg_path, "--out", out]
        if recipe:
            cmd += ["--recipe", recipe]
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1")
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
                 if os.name == "nt" else 0)

        def work():
            buf = job["log"]
            try:
                p = subprocess.Popen(cmd, cwd=None if getattr(sys, "frozen", False) else _REPO, env=env,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     creationflags=flags)
                job["pid"] = p.pid
                for raw in p.stdout:
                    ln = raw.decode("utf-8", "replace").rstrip()
                    if ln.startswith(_JOB_TAGS):
                        try:
                            job["reports"] = json.loads(ln[len(_JOB_TAGS):])
                        except ValueError:
                            pass
                    elif ln.strip():
                        buf.append(ln)
                job["state"] = "done" if p.wait() == 0 else "error"
            except Exception as e:
                buf.append(f"오류: {type(e).__name__}: {e}")
                job["state"] = "error"
            finally:
                job["elapsed_s"] = round(time.time() - job["started"], 1)
                try:
                    os.remove(cfg_path)
                except OSError:
                    pass

        threading.Thread(target=work, name=f"talog-{kind}", daemon=True).start()
        return {"ok": True}

    @staticmethod
    def _report_work(target: str, recipe_dir: str, out: str, job: dict) -> int:
        from .cli import _collect_day_folders, scan_day
        from .recipe import load_recipe
        recipe = None
        if recipe_dir:
            try:
                recipe = load_recipe(recipe_dir.strip().strip('"'), "")
                print(f"레시피: {recipe.root} [{recipe.version}]")
            except (FileNotFoundError, OSError) as e:
                print(f"레시피를 열 수 없어 레시피 없이 진행합니다 — {e}")
        days = _collect_day_folders(os.path.abspath(target))
        if not days:
            print("분석할 로그 폴더가 아닙니다 (alg\\ 또는 InspStarter.log 가 있는 일자 "
                  "폴더, 또는 그 상위 설비 폴더를 고르십시오)")
            return 1
        os.makedirs(out, exist_ok=True)
        for d in days:
            tag = _report_tag(d)
            try:
                scan_day(d, recipe, out, tag, fast=True)
                job["reports"].append(tag)
            except Exception as e:                # 일자 하나의 실패가 전체를 막지 않는다
                print(f"오류: {d} — {type(e).__name__}: {e}")
        print(f"리포트 {len(job['reports'])}건 완료")
        return 0 if job["reports"] else 1

    def reports(self) -> list[dict]:
        out = []
        for p in glob.glob(os.path.join(self.report_dir, "*.html")):
            tag = os.path.splitext(os.path.basename(p))[0]
            db = os.path.join(self.report_dir, tag + ".sqlite")
            diag = os.path.join(self.report_dir, tag + "_diagnosis.md")
            findings = []
            try:
                with open(diag, encoding="utf-8") as f:
                    findings = [ln[3:].strip() for ln in f if ln.startswith("## ")]
            except OSError:
                pass
            st = os.stat(p)
            out.append({"tag": tag, "mtime": dt.datetime.fromtimestamp(st.st_mtime)
                        .strftime("%m/%d %H:%M"), "ts": st.st_mtime, "size": st.st_size,
                        "db": os.path.exists(db), "findings": findings[:6],
                        "n_findings": len(findings)})
        out.sort(key=lambda r: r["ts"], reverse=True)
        return out

    def _report_paths(self, tag: str) -> tuple[str, str]:
        if not _TAG_RE.match(tag or ""):
            raise FileNotFoundError(tag)
        base = os.path.join(self.report_dir, tag)
        if os.path.dirname(os.path.abspath(base)) != os.path.abspath(self.report_dir):
            raise FileNotFoundError(tag)
        return base + ".html", base + ".sqlite"

    def _db(self, tag: str) -> sqlite3.Connection:
        from .viewer import _open_ro
        with self._db_lock:
            con = self._dbs.get(tag)
            if con is None:
                _h, db = self._report_paths(tag)
                if not os.path.exists(db):
                    raise FileNotFoundError(db)
                con = self._dbs[tag] = _open_ro(db)
            return con

    def report_page(self, tag: str) -> str:
        page, _db = self._report_paths(tag)
        with open(page, encoding="utf-8") as f:
            text = f.read()
        inject = f"<script>window.TALOG_API={json.dumps('/r/' + tag)};</script>"
        return text.replace("<head>", "<head>" + inject, 1) if "<head>" in text \
            else inject + text

    def report_api(self, tag: str, what: str, q: dict):
        from .viewer import query_detail, query_insp, query_meta
        con = self._db(tag)
        with self._db_lock:
            if what == "insp":
                return query_insp(con, q.get("filter", "all"), q.get("q", ""),
                                  q.get("sort", "desc"), int(q.get("offset", 0) or 0),
                                  min(1000, int(q.get("limit", 400) or 400)),
                                  q.get("from", ""), q.get("to", ""))
            if what == "detail":
                d = query_detail(con, q.get("inner", ""))
                return d if d is not None else {"error": "not found"}
            return query_meta(con)

    def ask_report(self, tag: str, question: str) -> dict:
        """리포트 DB 를 LLM 이 SQL 로 조회해 답한다 (talog ask 와 같은 도구 루프)."""
        from .ask import OllamaBackend, ToolBox, _build_system
        from .llm import OllamaClient, resolve_device
        question = (question or "").strip()[:500]
        if not question:
            return {"ok": False, "error": "질문이 비어 있습니다"}
        _h, db = self._report_paths(tag)
        if not os.path.exists(db):
            return {"ok": False, "error": "이 리포트에는 DB(.sqlite)가 없습니다"}
        lc = self.cfg["llm"]
        cli = OllamaClient(lc)
        if not cli.alive():
            return {"ok": False, "error": f"LLM 서버 응답 없음: {cli.url} — 설정 → 판단 에서 "
                                          f"주소를 확인하십시오"}
        dev, _why = resolve_device(lc)
        opts = {k: v for k, v in cli.options(dev).items() if k not in ("temperature",)}
        be = OllamaBackend(cli.model, cli.url, opts)
        t0 = time.time()
        try:
            ans = be.chat(_build_system(db), question, ToolBox(db), verbose=False)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        # 답에 적힌 숫자·시각·ID 가 실제 조회 결과에 있는지 대조한다 (7B 모델은 값을 지어낸다)
        from .agent import fact_check
        results = [str(r) for _n, _a, r in be.trace]
        # 결과 행 수(개수 답)와 끝자리 0 을 뗀 값도 근거로 본다 (1785113123.97 = .970)
        rows = [max(0, len([ln for ln in r.splitlines() if " | " in ln]) - 1) for r in results]
        norm = re.sub(r"(\d+\.\d*?[1-9])0+\b|(\d+)\.0+\b", lambda m: m.group(1) or m.group(2),
                      ans)
        fc = fact_check(norm, results, extra=(question, rows, sum(rows)))
        return {"ok": True, "answer": ans, "device": dev, "model": cli.model,
                "wall_s": round(time.time() - t0, 1), "fact": fc,
                "trace": [{"tool": n, "args": a, "result": str(r)[:1500]}
                          for n, a, r in be.trace][:12]}

    # ── 시험 ────────────────────────────────────────────────────
    # ── Claude 연동 (talog MCP 서버) ──────────────────────────
    def mcp_command(self) -> list[str]:
        """이 콘솔 설정으로 talog MCP 서버를 띄우는 명령 — Claude Code·데스크톱 설정에 그대로 넣는다."""
        from .mcp_server import server_command
        return server_command([self.config_path])

    @property
    def workspace_default(self) -> str:
        return os.path.join(self.data_dir, "claude_agent")

    def claude_workspace(self, target: str = "") -> dict:
        """에이전트 폴더 만들기 — Claude 에서 이 폴더 하나만 열면 talog RCA 에이전트가 된다."""
        from .mcp_server import init_workspace
        r = init_workspace(target.strip() or self.workspace_default, [self.config_path])
        return {"ok": True, **r}

    @staticmethod
    def open_folder(path: str) -> dict:
        path = os.path.abspath(path or "")
        if not os.path.isdir(path):
            return {"ok": False, "error": f"폴더가 없습니다: {path}"}
        try:
            os.startfile(path)                     # Windows 탐색기
        except (AttributeError, OSError) as e:
            return {"ok": False, "error": f"열지 못했습니다: {e}"}
        return {"ok": True}

    def claude_info(self) -> dict:
        from .mcp_server import PROMPTS, TOOLS
        cmd = self.mcp_command()

        def q(x: str) -> str:
            return f'"{x}"' if (not x or any(c in x for c in ' &()')) else x

        server = {"command": cmd[0], "args": cmd[1:]}
        appdata = os.environ.get("APPDATA", "")
        desk = (os.path.join(appdata, "Claude", "claude_desktop_config.json") if appdata
                else r"%APPDATA%\Claude\claude_desktop_config.json")
        skill = os.path.join(_REPO, ".claude", "skills", "talog-rca", "SKILL.md")
        incs = self.incidents(days=3650, replay=True, limit=100000)
        crit = [r for r in incs if r.get("severity") == "crit"]
        reps = self.reports()
        ws = self.workspace_default
        cli = self.claude_cli()
        ci = self.claude_cli_info(cli)
        return {"command": cmd, "workspace_default": ws, "cli": cli, "cli_version": ci["version"],
                "cli_login": ci["login"], "ask": self.claude_ask_status(),
                "workspace_exists": os.path.exists(os.path.join(ws, "CLAUDE.md")),
                "claude_code": "claude mcp add --scope user talog -- " + " ".join(q(c) for c in cmd),
                "mcp_json": json.dumps({"mcpServers": {"talog": {"type": "stdio", **server}}}, ensure_ascii=False, indent=2),
                "desktop_json": json.dumps({"mcpServers": {"talog": server}}, ensure_ascii=False, indent=2),
                "desktop_path": desk, "desktop_exists": os.path.exists(desk) if appdata else False,
                "skill": os.path.exists(skill), "skill_path": skill,
                "tools": [{"name": t["name"], "title": t.get("title", ""), "description": t["description"],
                           "write": not (t.get("annotations") or {}).get("readOnlyHint", True),
                           "ask": f"mcp__talog__{t['name']}" in self.ASK_TOOLS.split(",")} for t in TOOLS],
                "prompts": [{"name": x["name"], "title": x.get("title", ""), "description": x["description"]}
                            for x in PROMPTS],
                "data": {"incidents": len(incs), "crit": len(crit), "reports": len(reps),
                         "site": self.settings.get("site") or "이 설비", "data_dir": self.data_dir},
                "models": [{"v": "", "label": "기본 (구독 설정)"}, {"v": "sonnet", "label": "Sonnet — 빠르고 가벼움"},
                           {"v": "opus", "label": "Opus — 깊은 분석"}, {"v": "haiku", "label": "Haiku — 가장 가벼움"}],
                "efforts": [{"v": "", "label": "기본"}, {"v": "low", "label": "낮음"}, {"v": "medium", "label": "보통"},
                            {"v": "high", "label": "높음"}, {"v": "xhigh", "label": "매우 높음"}, {"v": "max", "label": "최대"}],
                "actions": self.claude_actions(incs, crit, reps), "logs": self.claude_log_dirs()}

    def claude_actions(self, incs: list, crit: list, reps: list) -> list[dict]:
        """빠른 작업 — MCP 프롬프트(Claude 앱의 슬래시 명령과 같은 문장)와 자주 하는 질문."""
        from .mcp_server import prompt_messages

        def text(name, args):
            return prompt_messages(name, args)["messages"][0]["content"]["text"]
        site = self.settings.get("site") or "이 설비"
        pick = (crit or incs or [{}])[0]
        logs = self.claude_log_dirs()
        out = [{"key": "log", "title": "원본 로그 조사", "icon": "file-search", "prompt": "log_investigation",
                "desc": "선택한 로그 폴더를 Claude 가 직접 읽고 시간순 경과·원인·근거를 정리",
                "needs": "log", "text": text("log_investigation", {"day": "선택" if logs["selected"] else "",
                                                                  "symptom": "그날 가장 심각한 이상 (예: NoInspThread)"})}]
        if pick.get("id"):
            out.append({"key": "rca", "title": "사건 RCA", "icon": "siren", "prompt": "incident_rca",
                        "desc": f"가장 최근 {'심각 ' if crit else ''}사건을 근거 → 원인 → 조치 → 보고서까지",
                        "text": text("incident_rca", {"incident_id": pick["id"]})})
        out.append({"key": "daily", "title": "하루 검토", "icon": "list", "prompt": "daily_review",
                    "desc": "하루치 경보·사건을 훑고 놓친 징후·오경보를 담당별 할 일로", "text": text("daily_review", {})})
        out.append({"key": "site", "title": "현장 점검", "icon": "gauge", "prompt": "site_health",
                    "desc": "기준선·재시작·규칙이 이 현장에 맞는지와 개선 제안", "text": text("site_health", {})})
        out.append({"key": "cap", "title": "처리 여유 추세", "icon": "activity", "prompt": "",
                    "desc": "여유가 가장 많이 줄어든 Seq 그룹과 시작 시점",
                    "text": f"{site}에서 처리 여유가 가장 많이 줄어든 Seq 그룹 3개를 찾아, 기준선 대비 언제부터 줄었는지 시간별 표로 보여 줘"})
        if reps:
            out.append({"key": "rep", "title": "리포트 교차 확인", "icon": "database", "prompt": "",
                        "desc": f"{reps[0]['tag']} 소견을 리포트 DB 로 검증",
                        "text": f"talog 리포트 {reps[0]['tag']} 의 자동 진단 소견을 리포트 DB 로 교차 확인해 줘 (거부 직전 분 단위 검사 수·소요·유휴)"})
        return out

    @staticmethod
    def claude_prompt(name: str, args: dict) -> dict:
        """MCP 프롬프트 문장 (사건 화면의 'Claude 로 분석'·'원본 로그로 확인'이 같은 문장을 쓴다)."""
        from .mcp_server import PROMPTS, prompt_messages
        if name not in {p["name"] for p in PROMPTS}:
            return {"ok": False, "error": f"모르는 프롬프트: {name}"}
        return {"ok": True, "text": prompt_messages(name, args)["messages"][0]["content"]["text"]}

    # ── Claude: 분석할 원본 로그 폴더 ──────────────────────────────
    def _log_roots_path(self, ws: str = "") -> str:
        return os.path.join(ws or self.workspace_default, "mcp_jobs", "log_roots.json")

    def _log_cfg(self) -> dict:
        try:
            with open(self._log_roots_path(), encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return {}
        return d if isinstance(d, dict) else {}

    def claude_log_dirs(self) -> dict:
        """바로 묻기에서 고를 원본 로그 폴더: 설정의 로그 루트 아래 일자 폴더 + 전에 고른 폴더."""
        from . import logread
        root = self.cfg.get("watch_root", "")
        cfg = self._log_cfg()
        days = logread.day_dirs([root] + [r for r in cfg.get("roots", []) if isinstance(r, str)], 24)
        sel = cfg.get("selected", "")
        return {"root": root, "days": days, "selected": sel if sel and os.path.isdir(sel) else ""}

    def _log_selected(self) -> str:
        sel = self._log_cfg().get("selected", "")
        return sel if isinstance(sel, str) and sel and os.path.isdir(sel) else ""

    def claude_select_log(self, path: str) -> dict:
        """원본 로그 폴더를 고른다 — talog MCP 가 읽을 수 있게 등록하고, Claude 에게는 경로 대신 '선택'으로 알린다."""
        from . import logread
        ws = self._ask_ws()
        path = (path or "").strip().strip('"')
        p = self._log_roots_path(ws)
        cur = self._log_cfg()
        if not path:
            cur.pop("selected", None)
        else:
            path = os.path.abspath(path)
            if not os.path.isdir(path):
                return {"ok": False, "error": f"폴더가 없습니다: {path}"}
            if not (logread._looks_like_day(path) or logread.day_dirs([path], 1)):
                return {"ok": False, "error": "talos 로그 폴더가 아닙니다 — InspStarter.log·comm.log·alg 가 있는 일자 폴더나 그 상위 폴더를 고르십시오"}
            roots = [r for r in cur.get("roots", []) if isinstance(r, str) and os.path.isdir(r) and r != path]
            cur = {"selected": path, "roots": (roots + [path])[-20:]}
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=1)
        return {"ok": True, **self.claude_log_dirs()}

    # ── Claude 에게 바로 묻기 ───────────────────────────────────
    # 이 PC 의 Claude Code CLI 를 화면 없이(-p) 실행한다. 구독 로그인만 쓰도록 API 키·게이트웨이·클라우드 과금 변수와
    # 이 콘솔을 띄운 다른 Claude 세션의 변수를 빼고 띄운다(추가 과금 없음, 구독 사용량에는 포함).
    # talog 도구는 읽기 전용(원본 로그 읽기 포함)만, 파일 쓰기는 에이전트 폴더의 reports 안만 허용한다(.mcp.json·CLAUDE.md 보호).
    # 이어 묻기는 --resume 으로 같은 Claude 세션을 이어 간다.
    ASK_TOOLS = ("mcp__talog__overview,mcp__talog__list_incidents,mcp__talog__get_incident,"
                 "mcp__talog__capacity_trend,mcp__talog__list_reports,mcp__talog__get_report,"
                 "mcp__talog__query_report,mcp__talog__runbook,mcp__talog__search_cases,mcp__talog__rediagnose,"
                 "mcp__talog__job_status,mcp__talog__log_days,mcp__talog__log_files,mcp__talog__read_log,"
                 "mcp__talog__search_logs,mcp__talog__trace_inspection,Read,Glob,Skill,Edit(reports/**)")
    ASK_MODELS = ("", "sonnet", "opus", "haiku")
    ASK_EFFORTS = ("", "low", "medium", "high", "xhigh", "max")
    ASK_MAX_TURNS = 60
    ASK_TIMEOUT_S = 1200
    _ENV_DROP = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDECODE", "CLAUDE_PID",
                 "CLAUDE_EFFORT", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"}
    _ENV_KEEP = {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_GIT_BASH_PATH"}
    _cli_ver: dict = {}
    ASK_STEPS = {"mcp__talog__overview": "talog 개요", "mcp__talog__list_incidents": "사건 목록",
                 "mcp__talog__get_incident": "사건 상세", "mcp__talog__capacity_trend": "처리 여유 추세",
                 "mcp__talog__list_reports": "진단 리포트 목록", "mcp__talog__get_report": "리포트 소견",
                 "mcp__talog__query_report": "리포트 DB 조회", "mcp__talog__runbook": "원인·조치 사전",
                 "mcp__talog__search_cases": "과거 사례", "mcp__talog__rediagnose": "사건 재진단",
                 "mcp__talog__job_status": "재현 작업", "mcp__talog__log_days": "원본 로그 폴더",
                 "mcp__talog__log_files": "로그 파일 목록", "mcp__talog__read_log": "원본 로그 읽기",
                 "mcp__talog__search_logs": "원본 로그 찾기", "mcp__talog__trace_inspection": "검사 추적",
                 "Skill": "RCA 절차 불러오기", "Read": "파일 읽기", "Glob": "파일 찾기", "Write": "보고서 쓰기",
                 "Edit": "보고서 고치기"}

    @classmethod
    def claude_env(cls) -> dict:
        """구독 로그인만 쓰는 Claude Code 실행 환경 — API 키·과금 경로와 부모 Claude 세션 변수를 뺀다."""
        out = {}
        for k, v in os.environ.items():
            u = k.upper()
            if u in cls._ENV_DROP or u.startswith(("CLAUDE_AGENT_SDK", "CLAUDE_PREVIEW")):
                continue
            if u.startswith("CLAUDE_CODE_") and u not in cls._ENV_KEEP:
                continue
            out[k] = v
        return out

    def claude_cli_info(self, cli: str) -> dict:
        """Claude Code 판(경로별 한 번만 묻는다)과 로그인 흔적. macOS 는 키체인이라 로그인을 모른다(None)."""
        if not cli:
            return {"version": "", "login": None}
        ver = self._cli_ver.get(cli)
        if ver is None:
            ver = ""
            try:
                p = subprocess.run([cli, "--version"], capture_output=True, timeout=15, env=self.claude_env(),
                                   stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                m = re.search(r"\d+\.\d+\.\d+", (p.stdout or b"").decode("utf-8", "replace"))
                ver = m.group(0) if m else ""
            except (OSError, subprocess.TimeoutExpired):
                pass
            self._cli_ver[cli] = ver
        if sys.platform == "darwin":
            return {"version": ver, "login": None}
        login = bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")) or os.path.exists(
            os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json"))
        return {"version": ver, "login": login}

    @staticmethod
    def claude_cli() -> str:
        """이 PC 의 Claude Code CLI 경로 (없으면 "")."""
        import shutil
        cands = [shutil.which("claude") or "", os.path.join(os.path.expanduser("~"), ".local", "bin", "claude.exe")]
        appdata = os.environ.get("APPDATA", "")
        if appdata:                                # 데스크톱 앱이 내려받은 판 (새 판부터)
            cands += sorted(glob.glob(os.path.join(appdata, "Claude", "claude-code", "*", "*", "claude.exe")),
                            reverse=True)
        return next((c for c in cands if c and os.path.exists(c)), "")

    def _ask_ws(self) -> str:
        ws = self.workspace_default
        if not os.path.exists(os.path.join(ws, ".mcp.json")):
            from .mcp_server import init_workspace
            init_workspace(ws, [self.config_path])
        return ws

    def _reports_in(self, ws: str) -> dict:
        out = {}
        for p in glob.glob(os.path.join(ws, "reports", "**", "*"), recursive=True):
            if os.path.isfile(p) and p.lower().endswith((".html", ".md")):
                out[p] = os.path.getmtime(p)
        return out

    def claude_ask(self, question: str, model: str = "", effort: str = "", new: bool = False) -> dict:
        q = (question or "").strip()
        if not q:
            return {"ok": False, "error": "질문이 비었습니다"}
        model, effort = (model or "").strip().lower(), (effort or "").strip().lower()
        if model not in self.ASK_MODELS:
            return {"ok": False, "error": f"모르는 모델입니다: {model}"}
        if effort not in self.ASK_EFFORTS:
            return {"ok": False, "error": f"모르는 effort 입니다: {effort}"}
        cli = self.claude_cli()
        if not cli:
            return {"ok": False, "error": "이 PC 에 Claude Code 가 없습니다 — '복사'로 Claude 데스크톱에 붙여 넣으십시오"}
        with self._lock:
            if self.ask_job.get("state") == "running":
                return {"ok": False, "error": "Claude 가 앞 질문에 답하는 중입니다 — 끝난 뒤 다시 보내십시오"}
            ws = self._ask_ws()
            if new or not self.ask_thread.get("session"):
                self.ask_thread = {"session": "", "turns": []}
            sel = self.claude_log_dirs()["selected"]
            prompt = (("[이 콘솔에서 분석할 원본 로그 폴더가 선택돼 있다 — log_files·search_logs·read_log·trace_inspection 에 "
                       "day='선택' 으로 읽는다]\n\n" if sel else "") + q)
            turn = {"state": "running", "q": q, "t0": time.time(), "steps": [], "answer": "", "live": "",
                    "reports": [], "error": "", "model_req": model, "effort": effort, "log_selected": bool(sel),
                    "resumed": bool(self.ask_thread.get("session"))}
            self.ask_thread["turns"].append(turn)
            self.ask_job = turn
            resume = self.ask_thread.get("session", "")
        threading.Thread(target=self._ask_work, args=(cli, ws, prompt, self._reports_in(ws), model, effort, resume),
                         name="talog-claude", daemon=True).start()
        return {"ok": True}

    @staticmethod
    def _what(name: str, arg: dict) -> str:
        """진행 단계에 붙일 짧은 대상 (사건 id·파일·문구·시각)."""
        if name.endswith(("search_logs",)):
            rng = "~".join(x for x in (arg.get("time_from"), arg.get("time_to")) if x)
            return f"'{arg.get('query', '')}'" + (f" {rng}" if rng else "")
        if name.endswith("read_log"):
            return " ".join(str(x) for x in (arg.get("file"), arg.get("at") or ("끝" if arg.get("tail") else "")) if x)
        if name.endswith("query_report"):
            return re.sub(r"\s+", " ", str(arg.get("sql", "")))[:70]
        if name == "Skill":
            return str(arg.get("skill") or arg.get("command") or "")
        return str(arg.get("incident_id") or arg.get("inspection_id") or arg.get("tag") or arg.get("day")
                   or arg.get("source") or os.path.basename(str(arg.get("file_path", ""))) or "")

    @staticmethod
    def _result_text(c: dict) -> str:
        v = c.get("content")
        if isinstance(v, list):
            v = "\n".join(x.get("text", "") for x in v if isinstance(x, dict))
        return str(v or "")

    def _ask_work(self, cli: str, ws: str, prompt: str, before: dict, model: str = "", effort: str = "",
                  resume: str = ""):
        J, TH = self.ask_job, self.ask_thread
        cmd = [cli, "-p", prompt, "--mcp-config", os.path.join(ws, ".mcp.json"), "--strict-mcp-config",
               "--allowedTools", self.ASK_TOOLS, "--permission-mode", "dontAsk",
               "--max-turns", str(self.ASK_MAX_TURNS), "--output-format", "stream-json", "--verbose",
               "--include-partial-messages", "-n", "talog · " + J["q"][:40]]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        if resume:
            cmd += ["--resume", resume]
        errf = tempfile.TemporaryFile()               # stderr 는 파일로 (파이프가 차서 멈추지 않게)
        try:
            p = subprocess.Popen(cmd, cwd=ws, env=self.claude_env(), stdout=subprocess.PIPE, stderr=errf,
                                 stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            errf.close()
            J.update(state="failed", error=f"Claude Code 를 실행하지 못했습니다: {e}")
            return
        J["pid"] = p.pid
        self._ask_proc = p

        def _timeout():
            J["error"] = f"{self.ASK_TIMEOUT_S // 60}분이 지나 끊었습니다 — 질문을 좁혀 다시 보내십시오"
            _kill_tree(p)
        timer = threading.Timer(self.ASK_TIMEOUT_S, _timeout)
        timer.start()
        by_id: dict[str, dict] = {}
        pending: list[str] = []                       # 아직 도구 호출이 뒤따르지 않은 글
        try:
            for raw in p.stdout:
                try:
                    d = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                t = d.get("type")
                if t == "system" and d.get("subtype") == "init":
                    mcp = {m.get("name"): m.get("status") for m in d.get("mcp_servers") or []}
                    J["model"] = d.get("model", "")
                    J["mcp"] = mcp.get("talog", "없음")
                    J["auth"] = d.get("apiKeySource", "")    # "none" = 구독 로그인
                    J["session_id"] = d.get("session_id", "")
                    TH["session"] = J["session_id"] or TH.get("session", "")
                elif t == "rate_limit_event":
                    info = d.get("rate_limit_info") or {}
                    self.claude_usage = {"type": info.get("rateLimitType", ""), "utilization": info.get("utilization"),
                                         "status": info.get("status", ""), "resets_at": info.get("resetsAt"),
                                         "overage": bool(info.get("isUsingOverage")), "at": time.time()}
                elif t == "stream_event":
                    ev = d.get("event") or {}
                    if ev.get("type") == "message_start":
                        J["live"] = ""
                    elif ev.get("type") == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
                        J["live"] += ev["delta"].get("text", "")
                elif t == "assistant":
                    # 내용 블록이 한 이벤트에 같이 오든 블록마다 따로 오든, 도구를 부르기 전에 쓴 말은 진행 메모가 된다
                    # (마지막 답은 result 로 따로 온다)
                    for c in (d.get("message") or {}).get("content") or []:
                        if c.get("type") == "text" and c.get("text", "").strip():
                            pending.append(c["text"].strip())
                            continue
                        if c.get("type") != "tool_use":
                            continue
                        for x in pending:
                            J["steps"].append({"t": round(time.time() - J["t0"]), "kind": "note", "label": x[:400]})
                        pending.clear()
                        J["live"] = ""                # 흐르던 글은 메모로 옮겨졌다
                        name = c.get("name", "")
                        label = self.ASK_STEPS.get(name)
                        if not label:
                            continue                  # 내부 도구(할 일 목록·도구 검색)는 보이지 않는다
                        step = {"t": round(time.time() - J["t0"]), "kind": "tool", "tool": name.split("__")[-1],
                                "label": label, "what": self._what(name, c.get("input") or {})[:90], "done": False}
                        J["steps"].append(step)
                        by_id[c.get("id", "")] = step
                elif t == "user":
                    for c in (d.get("message") or {}).get("content") or []:
                        if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("tool_use_id") in by_id:
                            st = by_id[c["tool_use_id"]]
                            st.update(done=True, error=bool(c.get("is_error")),
                                      preview=self._result_text(c)[:900],
                                      ms=round((time.time() - J["t0"] - st["t"]) * 1000))
                elif t == "result":
                    J["answer"] = d.get("result") or ""
                    J["live"] = ""
                    J["turns"] = d.get("num_turns")
                    J["is_error"] = bool(d.get("is_error"))
                    J["denied"] = sorted({x.get("tool_name", "") for x in d.get("permission_denials") or []} - {""})
            p.wait(timeout=30)
        except Exception as e:                       # 콘솔은 죽지 않는다
            J["error"] = f"{type(e).__name__}: {e}"
        finally:
            timer.cancel()
            self._ask_proc = None
        try:
            errf.seek(0)
            err = errf.read().decode("utf-8", "replace").strip()
        finally:
            errf.close()
        after = self._reports_in(ws)
        J["reports"] = sorted((os.path.relpath(f, ws) for f, m in after.items() if before.get(f) != m),
                              key=lambda r: (not r.lower().endswith(".html"), r))
        J["elapsed_s"] = round(time.time() - J["t0"])
        if J.get("cancelled"):
            J["state"], J["error"] = "cancelled", "사용자가 멈췄습니다 — 그때까지 쓴 보고서는 남아 있습니다"
        elif p.returncode == 0 and J.get("answer") and not J.get("is_error"):
            J["state"] = "done"
        else:
            msg = J.get("error") or err[-800:] or (J.get("answer") or "")[:800] or f"종료 코드 {p.returncode}"
            if re.search(r"\blog ?in\b|/login|invalid api key|authenticat|credentials?\b", msg, re.I):
                msg = ("Claude Code 에 로그인되어 있지 않습니다 — 명령 프롬프트에서 claude 를 한 번 실행해 "
                       "구독 계정(Pro·Max)으로 로그인한 뒤 다시 보내십시오.\n\n" + msg[-300:])
            J["state"], J["error"] = "failed", msg

    def claude_ask_cancel(self) -> dict:
        p = getattr(self, "_ask_proc", None)
        if self.ask_job.get("state") != "running" or p is None or p.poll() is not None:
            return {"ok": False, "error": "진행 중인 질문이 없습니다"}
        self.ask_job["cancelled"] = True
        _kill_tree(p)
        return {"ok": True}

    def claude_new_thread(self) -> dict:
        with self._lock:
            if self.ask_job.get("state") == "running":
                return {"ok": False, "error": "Claude 가 답하는 중입니다 — 멈추거나 끝난 뒤 새 대화를 여십시오"}
            self.ask_thread = {"session": "", "turns": []}
            self.ask_job = {"state": "idle"}
        return {"ok": True}

    def claude_ask_status(self) -> dict:
        now = time.time()
        turns = []
        for t in self.ask_thread.get("turns", []):
            x = dict(t)
            if x.get("state") == "running":
                x["elapsed_s"] = round(now - x.get("t0", now))
            turns.append(x)
        return {"state": self.ask_job.get("state", "idle"), "turns": turns,
                "can_continue": bool(self.ask_thread.get("session")), "usage": self.claude_usage,
                "log_selected": self._log_selected()}

    def claude_file(self, rel: str) -> tuple[bytes, str]:
        """에이전트 폴더 reports 안의 보고서만 내준다."""
        ws = os.path.abspath(self.workspace_default)
        base = os.path.join(ws, "reports")
        p = os.path.abspath(os.path.join(ws, rel or ""))
        if not p.startswith(base + os.sep) or not os.path.isfile(p) or not p.lower().endswith((".html", ".md")):
            raise FileNotFoundError(rel)
        with open(p, "rb") as f:
            data = f.read()
        return data, ("text/html; charset=utf-8" if p.lower().endswith(".html") else "text/plain; charset=utf-8")

    def claude_test(self) -> dict:
        """연결 시험 — Claude 가 띄울 명령 그대로 talog MCP 서버를 띄워 자가 점검(JSON)을 돌린다."""
        cmd = self.mcp_command() + ["--self-test", "--json"]
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        t0 = time.time()
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=120, env=env, cwd=_REPO,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            return {"ok": False, "output": "120초 안에 끝나지 않았습니다 — 데이터 폴더가 네트워크 드라이브인지 확인하십시오",
                    "elapsed_s": 120}
        except OSError as e:
            return {"ok": False, "output": f"실행 실패: {e}", "elapsed_s": 0}
        out = (p.stdout or b"").decode("utf-8", "replace")
        err = (p.stderr or b"").decode("utf-8", "replace")
        report = None
        for ln in reversed(out.splitlines()):
            if ln.strip().startswith("{"):
                try:
                    report = json.loads(ln)
                    break
                except ValueError:
                    pass
        return {"ok": p.returncode == 0 and bool(report and report.get("ok")), "report": report,
                "output": (out + err)[-14000:], "elapsed_s": round(time.time() - t0, 1), "command": " ".join(cmd)}

    def test_llm(self, body: dict) -> dict:
        from .llm import OllamaClient
        lc = copy.deepcopy(self.cfg["llm"])
        for k in ("url", "model"):
            if body.get(k):
                lc[k] = body[k]
        lc["timeout_s"] = 300
        cli = OllamaClient(lc)
        if not cli.alive():
            return {"ok": False, "error": f"Ollama 서버 응답 없음: {cli.url}"}
        if cli.model not in cli.models():
            return {"ok": False, "error": f"모델 {cli.model} 가 서버에 없습니다 (ollama pull)"}
        msgs = [{"role": "system", "content": "한국어로만 답한다."},
                {"role": "user", "content": "검사 설비에서 NoInspThread 거부가 났다. 담당자에게 보낼 "
                                            "한 문장 알림을 써라."}]
        dev = body.get("device")
        r = cli.chat(msgs, num_predict=80, device=dev if dev in ("cpu", "gpu") else None)
        if r["error"]:
            return {"ok": False, "error": r["error"], "device": r["device"]}
        gen_s = max(0.01, r["wall_s"] - r["load_s"])
        return {"ok": True, "device": r["device"], "reason": r["device_reason"],
                "wall_s": r["wall_s"], "load_s": r["load_s"],
                "tok_s": round(r["gen_tokens"] / gen_s, 1), "reply": r["content"][:300],
                "model": cli.model}

    def llm_models(self, url: str) -> dict:
        from .llm import OllamaClient
        cli = OllamaClient({"url": url or self.cfg["llm"].get("url")})
        alive = cli.alive()
        return {"alive": alive, "url": cli.url, "models": cli.models() if alive else []}

    def _mailer_for(self, body: dict):
        from .mailer import Mailer
        s = self._with_secret(body.get("settings") or self.settings)
        cfg = settings.compile(s)
        m = Mailer(cfg, self.data_dir)
        typed = body.get("secret") or ""
        if typed:                                 # 저장 전 입력값으로 시험 (저장·출력 안 함)
            m.password = lambda: typed
            m.graph_secret = lambda: typed
        return m, s, bool(typed)

    def test_mail_check(self, body: dict) -> dict:
        m, s, typed = self._mailer_for(body)
        if s["mail"]["provider"] == "off":
            return {"ok": False, "detail": "발송 방식이 '보내지 않음' 입니다"}
        ok, detail = m.check()
        return {"ok": ok, "detail": detail, "transport": m.transport,
                "secret_source": "입력값" if typed else (m.password_source() or "없음")}

    def test_mail(self, body: dict) -> dict:
        from .mailer import sample_incident
        m, s, _typed = self._mailer_for(body)
        if s["mail"]["provider"] == "off":
            return {"ok": False, "error": "발송 방식이 '보내지 않음' 입니다"}
        errs = settings.validate(s)
        if errs:
            return {"ok": False, "error": " / ".join(errs)}
        inc = sample_incident(s.get("site") or "설비")
        inc["verdict"]["notify"] = list((m.e.get("roles") or {}).keys())
        res = m.send_incident(inc)
        res["ok"] = bool(res.get("sent") or res.get("dry_run"))
        return res

    def test_rule(self, body: dict) -> dict:
        """경보 규칙 시험: 로그 한 줄(log 형) 또는 결함명(defect 형)이 걸리는가."""
        text = str(body.get("text") or "")
        fname = str(body.get("file") or "")
        out = []
        for i, it in enumerate(body.get("rules") or []):
            if not isinstance(it, dict):
                continue
            name = it.get("name") or f"규칙{i + 1}"
            if it.get("type") == "defect":
                pats = settings._as_list(it.get("match"))
                hit = any(fnmatchcase(text.strip().upper(), p.upper()) for p in pats)
                out.append({"name": name, "ok": True, "match": hit, "file_ok": True})
                continue
            rules, errs = compile_patterns([{k: v for k, v in it.items() if k != "type"}])
            if errs or not rules:
                out.append({"name": name, "ok": False, "error": "; ".join(errs) or "비어 있음"})
                continue
            r = rules[0]
            file_ok = (not fname) or r.applies(fname)
            out.append({"name": name, "ok": True, "match": bool(r.rx.search(text)) and file_ok,
                        "file_ok": file_ok})
        return {"ok": True, "results": out}

    def inject(self, kind: str) -> dict:
        if self.watch is None or not self.running():
            return {"ok": False, "error": "감시가 정지 상태입니다 — 먼저 시작하십시오"}
        from .watch import Alert
        now = time.time()
        samples = {
            "noinsp": Alert(now, "no_insp_thread", "crit",
                            "[시험] 검사 시작 거부(NoInspThread) — 미검사 임박",
                            "콘솔에서 넣은 시험 경보입니다.", key="test-noinsp",
                            cooldown_min=0.05),
            "defect": Alert(now, "defect_critical", "crit", "[시험] 치명 결함 검출: TEST_DEFECT",
                            "콘솔에서 넣은 시험 경보입니다.", key="test-defect",
                            cooldown_min=0.05),
            "warn": Alert(now, "pattern", "warn", "[시험] 로그 문구 '시험' 3회",
                          "콘솔에서 넣은 주의 등급 시험 경보입니다.", key="test-warn",
                          cooldown_min=0.05),
        }
        a = samples.get(kind)
        if a is None:
            return {"ok": False, "error": f"알 수 없는 종류: {kind}"}
        self.watch.notifier.emit(a)
        return {"ok": True, "message": f"{a.title} — 실제 경보와 같은 경로로 처리됩니다"}


_JOB_TAGS = "@@talog-reports "


def _self_command() -> list[str]:
    """talog 를 다시 띄우는 명령 — exe 면 exe 그대로, 아니면 이 파이썬으로 -m talog."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-u", "-m", "talog"]


def job_main(argv: list[str]) -> int:
    """콘솔이 띄우는 분석 작업: talog _job report|replay <폴더> --config <설정 json> [--out] [--recipe]."""
    import argparse
    ap = argparse.ArgumentParser(prog="talog _job")
    ap.add_argument("kind", choices=("report", "replay"))
    ap.add_argument("target")
    ap.add_argument("--config", required=True, help="콘솔이 넘긴 사용자 설정(json)")
    ap.add_argument("--out", default="")
    ap.add_argument("--recipe", default="")
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    except (AttributeError, ValueError):
        pass
    with open(a.config, encoding="utf-8") as f:
        user = json.load(f)
    if a.kind == "replay":
        from .watch import run_replay
        return run_replay(settings.compile(user), a.target)
    job = {"reports": []}
    rc = Console._report_work(a.target, a.recipe, a.out or os.path.join(os.getcwd(), "reports"), job)
    print(_JOB_TAGS + json.dumps(job["reports"], ensure_ascii=False), flush=True)
    return rc


# ---------------------------------------------------------------------------
def _kill_tree(p: subprocess.Popen) -> None:
    """프로세스와 그 자식(MCP 서버)까지 끝낸다."""
    if p.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        p.kill()
    except OSError:
        pass


def _handler_factory(con: Console):
    class H(BaseHTTPRequestHandler):
        server_version = "talog-console"

        def log_message(self, fmt, *args):     # 접근 로그는 남기지 않는다
            pass

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _send(self, code: int, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else (
                json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
                if not isinstance(body, str) else body.encode("utf-8"))
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {"error": "host"})
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/":
                    with open(_PAGE, encoding="utf-8") as f:
                        page = f.read().replace("__TALOG_TOKEN__", con.token)
                    return self._send(200, page, "text/html; charset=utf-8")
                if u.path.startswith("/r/"):          # 리포트 (뷰어 API 포함)
                    rest = unquote(u.path[3:])
                    tag, _, sub = rest.partition("/")
                    if sub in ("", "index.html"):
                        return self._send(200, con.report_page(tag),
                                          "text/html; charset=utf-8")
                    if sub in ("api/insp", "api/detail", "api/meta"):
                        return self._send(200, con.report_api(tag, sub[4:], q))
                    return self._send(404, {"error": "not found"})
                if u.path in ("/favicon.ico", "/favicon.svg"):
                    name = "talog.ico" if u.path.endswith(".ico") else "talog_icon.svg"
                    with open(os.path.join(_ASSETS, name), "rb") as f:
                        return self._send(200, f.read(), "image/x-icon" if name.endswith(".ico")
                                          else "image/svg+xml")
                if u.path == "/api/claude":
                    return self._send(200, con.claude_info())
                if u.path == "/api/claude/ask":
                    return self._send(200, con.claude_ask_status())
                if u.path == "/api/claude/file":
                    data, ct = con.claude_file(q.get("path", ""))
                    return self._send(200, data, ct)
                if u.path == "/api/status":
                    return self._send(200, con.status())
                if u.path == "/api/settings":
                    return self._send(200, {"settings": con.public_settings(),
                                            "meta": con.meta()})
                if u.path == "/api/folder":
                    tr = q.get("tracking")
                    trv = json.loads(tr) if tr else None
                    return self._send(200, con.folder(q.get("dir", ""), trv))
                if u.path == "/api/incidents":
                    return self._send(200, {"rows": con.incidents(
                        int(q.get("days", 3)), q.get("replay", "1") == "1")})
                if u.path == "/api/incident":
                    d = con.incident(q.get("id", ""), q.get("replay") == "1")
                    return self._send(200 if d else 404, d or {"error": "없음"})
                if u.path == "/api/mail":
                    return self._send(200, con.mail_html(q.get("id", ""), q.get("replay") == "1",
                                                         q.get("which", "initial")),
                                      "text/html; charset=utf-8")
                if u.path == "/api/llm/models":
                    return self._send(200, con.llm_models(q.get("url", "")))
                if u.path == "/api/log":
                    return self._send(200, {"lines": list(con.log)[-300:]})
                if u.path == "/api/job":
                    j = {k: v for k, v in con.job.items() if k != "log"}
                    j["log"] = list(con.job.get("log") or [])[-250:]
                    return self._send(200, j)
                if u.path == "/api/timeline":
                    return self._send(200, con.timeline(int(q.get("days", 7))))
                if u.path == "/api/reports":
                    return self._send(200, {"rows": con.reports()})
            except FileNotFoundError as e:
                return self._send(404, {"error": f"없음: {e}"})
            except Exception as e:
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._host_ok() or self.headers.get("X-Talog-Token") != con.token:
                return self._send(403, {"error": "토큰 불일치 — 콘솔 페이지를 새로고침하십시오"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
            except (ValueError, UnicodeError):
                return self._send(400, {"error": "JSON 형식 오류"})
            u = urlparse(self.path).path
            try:
                if u == "/api/settings":
                    errs = con.save(body.get("settings") or {}, body.get("secret") or "",
                                    bool(body.get("clear_secret")))
                    return self._send(200 if not errs else 400,
                                      {"ok": not errs, "errors": errs,
                                       "settings": con.public_settings()})
                if u == "/api/control":
                    act = body.get("action")
                    if act == "start":
                        con.start_watch()
                    elif act == "stop":
                        con.stop_watch()
                    return self._send(200, {"ok": True, "running": con.running()})
                if u == "/api/test/llm":
                    return self._send(200, con.test_llm(body))
                if u == "/api/test/mail-check":
                    return self._send(200, con.test_mail_check(body))
                if u == "/api/test/mail":
                    return self._send(200, con.test_mail(body))
                if u == "/api/test/rule":
                    return self._send(200, con.test_rule(body))
                if u == "/api/test/alert":
                    return self._send(200, con.inject(body.get("kind", "")))
                if u == "/api/job":
                    return self._send(200, con.start_job(body.get("kind", ""),
                                                         body.get("dir", ""),
                                                         body.get("recipe", "")))
                if u == "/api/ask/incident":
                    return self._send(200, con.ask_incident(body.get("id", ""),
                                                            bool(body.get("replay")),
                                                            body.get("q", "")))
                if u == "/api/claude/test":
                    return self._send(200, con.claude_test())
                if u == "/api/claude/ask":
                    return self._send(200, con.claude_ask(body.get("q", ""), body.get("model", ""),
                                                          body.get("effort", ""), bool(body.get("new"))))
                if u == "/api/claude/ask/cancel":
                    return self._send(200, con.claude_ask_cancel())
                if u == "/api/claude/prompt":
                    return self._send(200, con.claude_prompt(body.get("name", ""), body.get("args") or {}))
                if u == "/api/claude/thread/new":
                    return self._send(200, con.claude_new_thread())
                if u == "/api/claude/log":
                    return self._send(200, con.claude_select_log(body.get("path", "")))
                if u == "/api/claude/workspace":
                    return self._send(200, con.claude_workspace(body.get("dir", "")))
                if u == "/api/claude/open":
                    return self._send(200, con.open_folder(body.get("dir", "")))
                if u == "/api/ask/report":
                    return self._send(200, con.ask_report(body.get("tag", ""),
                                                          body.get("q", "")))
            except FileNotFoundError as e:
                return self._send(404, {"ok": False, "error": f"없음: {e}"})
            except Exception as e:
                return self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            return self._send(404, {"error": "not found"})

    return H


def serve(config_path: str, port: int = 8778, open_browser: bool = True,
          autostart: bool = True) -> int:
    con = Console(config_path, port)
    sys.stdout = _Tee(sys.stdout, con.log)        # 콘솔 창 + 화면의 실행 로그 (분석 작업은 별도 프로세스)
    srv = ThreadingHTTPServer(("127.0.0.1", port), _handler_factory(con))
    url = f"http://127.0.0.1:{port}/"
    print(f"[talog] AI 비전 로그 운영 플랫폼 {__version__} — {url}")
    print(f"        설정 {con.config_path}"
          + (" (옛 watch.yaml 형식 — 콘솔에서 저장하면 새 형식으로 바뀝니다)"
             if con.legacy else "")
          + " · 이 창을 닫으면 감시도 멈춥니다")
    if autostart:
        con.start_watch()
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        con.stop_watch()
        srv.server_close()
    return 0
