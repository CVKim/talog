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
import sys
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
    parts = [p for p in os.path.normpath(day_dir).split(os.sep) if p]
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
                      "agree": sum(1 for i in incs if "일치" in (i.get("mode") or "")),
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

    def incident(self, iid: str, replay: bool) -> dict | None:
        pat = "incidents_*_replay.jsonl" if replay else "incidents_[0-9]*[0-9].jsonl"
        for p in sorted(glob.glob(os.path.join(self.data_dir, pat)), reverse=True):
            try:
                with open(p, encoding="utf-8") as f:
                    for ln in f:
                        if f'"id": "{iid}"' in ln:
                            d = json.loads(ln)
                            day = (d.get("id") or "")[:8]
                            if not replay and day.isdigit():
                                d["day_dir"] = os.path.join(
                                    self.cfg["watch_root"], f"{day[:4]}_{day[4:6]}", day[6:])
                            return d
            except (OSError, ValueError):
                continue
        return None

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
        cfg = copy.deepcopy(self.cfg)
        out = self.report_dir
        job = {"state": "running", "kind": kind, "target": target, "started": time.time(),
               "log": deque(maxlen=400), "reports": []}
        self.job = job

        def work():
            buf = job["log"]
            try:
                if kind == "replay":
                    from .watch import run_replay
                    rc = _run_captured(run_replay, (cfg, target), buf)
                else:
                    rc = _run_captured(self._report_work, (target, recipe, out, job), buf)
                job["state"] = "done" if rc == 0 else "error"
            except Exception as e:
                buf.append(f"오류: {type(e).__name__}: {e}")
                job["state"] = "error"
            finally:
                job["elapsed_s"] = round(time.time() - job["started"], 1)

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


_capture_local = threading.local()


def _run_captured(fn, args, buf: deque) -> int:
    """작업 스레드의 print 를 buf 로 모은다 (다른 스레드 출력은 그대로)."""
    _capture_local.buf = buf
    try:
        return fn(*args)
    finally:
        _capture_local.buf = None


class _ThreadRouter(io.TextIOBase):
    """스레드별 출력 분기: 분석 작업 스레드는 작업 기록으로, 나머지는 콘솔 창+화면 로그로."""

    def __init__(self, default):
        self.default = default

    def write(self, s):
        buf = getattr(_capture_local, "buf", None)
        if buf is not None:
            for line in s.splitlines():
                if line.strip():
                    buf.append(line)
            return len(s)
        return self.default.write(s)

    def flush(self):
        self.default.flush()


# ---------------------------------------------------------------------------
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
    router = _ThreadRouter(_Tee(sys.stdout, con.log))
    sys.stdout = router
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
