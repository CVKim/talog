"""talog watch 콘솔 — 로컬 웹 UI (상태·추적 파일·경보 규칙·LLM·이메일·사건·리플레이).

사용:
    talog watch --ui [--config watch.yaml] [--port 8778] [--no-open]

- 실시간 감시(LiveWatch)를 작업 스레드로 돌린다. 설정을 저장하면 watch.yaml 을
  .bak 으로 백업한 뒤 다시 쓰고, 감시를 새 설정으로 재시작한다.
- 127.0.0.1 에서만 연다. POST 는 페이지에 심은 세션 토큰(X-Talog-Token)이 있어야
  받고, Host 헤더도 확인한다(다른 사이트의 위조 요청·DNS 리바인딩 차단).
- SMTP 비밀번호는 평문으로 저장하지 않는다: 입력하면 Windows DPAPI 로 암호화해
  email.password_dpapi 에 넣고, 화면·API 로 다시 내보내지 않는다.
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
import sys
import threading
import time
import webbrowser
from collections import Counter, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import yaml

from . import __version__
from .fileclass import classify
from .tracker import PRESETS, compile_patterns, resolve_targets

_PAGE = os.path.join(os.path.dirname(__file__), "console.html")

# 기본 감지 룰 (콘솔 표 — 기본 등급은 watch.py 의 발보 코드와 같다)
BUILTIN_RULES = [
    ("no_insp_thread", "검사 시작 거부 (NoInspThread)", "crit", "event"),
    ("crash", "프로세스 크래시", "crit", "event"),
    ("img_timeout", "검사 타임아웃 — 판정 미송신", "crit", "event"),
    ("grab_fail", "그랩 실패 (카메라/트리거)", "crit", "event"),
    ("storage_low", "이미지 저장 공간 부족", "crit", "event"),
    ("light_unstable", "조명 컨트롤러 불안정", "crit", "event"),
    ("reject_busycam", "시작 거부 — 카메라 점유", "crit", "event"),
    ("reject_notready", "시작 거부 — 모델 미로드", "crit", "event"),
    ("reject_sim", "시작 거부 — 시뮬레이션 모드", "crit", "event"),
    ("alg_timeout", "알고리즘 타임아웃 (TIME_OUT NG)", "warn", "event"),
    ("error_repeat", "동일 에러 반복", "warn", "error_repeat"),
    ("insp_stall", "검사 정체", "warn", "insp_stall"),
    ("restart_burst", "재시작 빈발", "crit", "restart_burst"),
    ("memory_trend", "메모리 증가 추세 (릭 의심)", "crit", "memory_trend"),
    ("gpu_temp", "GPU 과열", "crit", "gpu_temp"),
    ("defect_critical", "치명 결함 검출", "crit", "event"),
    ("defect_repeat", "동일 결함 빈발", "warn", "event"),
    ("ng_streak", "연속 NG", "crit", "event"),
    ("ng_rate", "NG 비율 급증", "warn", "event"),
    ("pattern", "사용자 정의 패턴 (전체)", "-", "event"),
]

# 저장하는 watch.yaml 에 붙일 설명 주석 (키 경로 → 설명)
_DESC = {
    "watch_root": "talos 로그 루트 (YYYY_MM\\DD 자동 추적)",
    "poll_seconds": "폴링 주기(초) — 새로 쓰인 바이트만 읽음",
    "alert_dir": "경보·사건·상태 기록 폴더",
    "low_priority": "프로세스 우선순위 강등 (검사 SW 보호)",
    "site": "설비 이름 (메일 제목·기록에 표시)",
    "cooldown_min": "같은 경보 재발송 억제(분)",
    "tracking": "추적 대상",
    "tracking.mode": "default(핵심 9종) | select(files 지정) | auto(일자 폴더 전체)",
    "tracking.files": "select 모드: 파일명·와일드카드 (alg\\*.log 가능)",
    "tracking.exclude": "auto 모드 제외 목록",
    "tracking.include_alg": "auto 모드에서 alg\\ 하위 포함",
    "tracking.max_initial_mb": "처음 보는 파일이 이보다 크면 끝부분부터 읽음",
    "rules": "감지 룰",
    "rules.defect_watch": "결함명 감시 (comm.log INSPECT_END NG 결함명)",
    "rules.defect_watch.critical": "1건만 나와도 즉시 심각 경보 (와일드카드 * 허용)",
    "rules.defect_watch.repeat_count": "같은 결함명 repeat_window_min 분 안에 N건 (0 = 끔)",
    "rules.defect_watch.ng_streak": "연속 NG N건 (0 = 끔)",
    "rules.defect_watch.ng_rate_window": "최근 N검사 NG 비율 감시 (0 = 끔)",
    "rules.patterns": "사용자 정의 로그 패턴 (count 1 = 한 번만 나와도 경보)",
    "rules.overrides": "기본 룰 조정 {룰: {enabled, severity, count, window_min}}",
    "notify": "기본 알림 채널 (토스트·웹훅·JSONL)",
    "llm": "로컬 LLM (Ollama) — 사건 분석·주기 점검 공용",
    "llm.url": "Ollama 주소 (전용 GPU 서버·사내 분석 PC 가능)",
    "llm.device": "cpu(검사 GPU 미사용) | gpu | auto(여유 VRAM 보고 선택)",
    "llm.cpu_threads": "CPU 모드 스레드 상한 (0 = Ollama 기본)",
    "llm.gpu_min_free_mb": "auto: 이 이상 여유 VRAM 이 있을 때만 GPU",
    "llm.gpu_max_util": "auto: 그 GPU 사용률(%)이 이 이하일 때만",
    "llm.keep_alive": "모델 상주 시간 (빈 값 = 서버 기본, 0 = 바로 내림)",
    "llm.enabled": "주기 점검 모드 (지시문 script 기반)",
    "agent": "경보 사건 분석 (룰 진단 + LLM 2차 의견 + 합의 관문)",
    "agent.min_severity": "이 등급 이상 경보만 분석",
    "agent.batch_seconds": "첫 경보 후 이 시간 동안의 경보를 한 사건으로 묶음",
    "email": "사건 메일 (SMTP)",
    "email.security": "starttls | ssl | none",
    "email.password_env": "비밀번호 환경변수 이름 (있으면 우선)",
    "email.password_dpapi": "콘솔에서 입력한 비밀번호의 암호문 — 이 PC·이 사용자만 복호화",
    "email.roles": "분석이 지목한 담당 역할별 추가 수신자",
    "email.immediate": "이 등급은 묶음 없이 즉시 발송 (룰 판단, LLM 은 후속 메일)",
    "email.min_severity": "이 등급 이상만 메일",
    "email.min_interval_min": "묶음 메일 사이 최소 간격(분)",
    "email.max_per_hour": "시간당 상한 (즉시 메일 포함)",
    "email.dry_run": "true = SMTP 없이 outbox 에 .eml 만 저장",
}


def dump_config(cfg: dict) -> str:
    """설명 주석을 붙여 YAML 로 쓴다 (yaml.safe_dump 는 주석을 잃으므로 줄마다 덧붙임)."""
    body = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False,
                          default_flow_style=False, width=120)
    out, stack = [], []
    key_re = re.compile(r"^(\s*)([A-Za-z_][\w]*):(.*)$")
    for line in body.splitlines():
        m = key_re.match(line)
        if m and not line.lstrip().startswith("-"):
            ind = len(m.group(1))
            while stack and stack[-1][0] >= ind:
                stack.pop()
            stack.append((ind, m.group(2)))
            path = ".".join(k for _i, k in stack)
            if path in _DESC and "#" not in m.group(3):
                line = f"{line}  # {_DESC[path]}"
        out.append(line)
    head = (f"# talog watch 설정 — talog 콘솔({__version__})에서 저장 "
            f"{dt.datetime.now():%Y-%m-%d %H:%M}\n"
            "# 직접 고쳐도 됩니다. 콘솔로 다시 저장하면 이전 파일은 .bak 으로 남습니다.\n")
    return head + "\n".join(out) + "\n"


def _latest_day_dir(root: str) -> str:
    """watch_root 아래 가장 최근 YYYY_MM\\DD 폴더 (오늘 폴더가 없을 때 미리보기용)."""
    best = ""
    for p in glob.glob(os.path.join(root, "[0-9][0-9][0-9][0-9]_[0-9][0-9]", "[0-9][0-9]")):
        if os.path.isdir(p) and p > best:
            best = p
    return best


def _recipe_defects(day_dir: str) -> tuple[list[str], list[list]]:
    """comm.log 에서 (레시피 결함명 목록, 오늘 NG 결함 빈도)를 뽑는다 (꼬리 8MB)."""
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


class _Tee(io.TextIOBase):
    """콘솔 창 출력은 그대로 두고 최근 줄을 UI 로그 화면용으로 보관한다."""

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
        from .watch import load_config
        self.config_path = os.path.abspath(config_path)
        self.port = port
        self.cfg = load_config(self.config_path)
        self.token = secrets.token_urlsafe(24)
        self.log: deque = deque(maxlen=400)
        self.watch = None
        self._stop = None
        self._thread = None
        self._lock = threading.RLock()
        self.replay_job: dict = {"state": "idle"}

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

    # ── 설정 ───────────────────────────────────────────────────
    def public_cfg(self) -> dict:
        c = copy.deepcopy(self.cfg)
        e = c.get("email", {})
        e["has_password"] = bool(e.get("password_dpapi")) or bool(
            os.environ.get(e.get("password_env") or "TALOG_SMTP_PASSWORD"))
        e["password_dpapi"] = ""
        g = e.setdefault("graph", {})
        g["has_secret"] = bool(g.get("client_secret_dpapi")) or bool(
            os.environ.get(g.get("secret_env") or "TALOG_GRAPH_SECRET"))
        g["client_secret_dpapi"] = ""
        return c

    @staticmethod
    def _protect(text: str) -> str:
        from .secret import available, protect
        if not available():
            raise ValueError("이 OS 에서는 비밀 암호화 저장을 지원하지 않습니다 — 환경변수를 "
                             "쓰십시오")
        return protect(text)

    def _merge_user_cfg(self, new: dict, password: str = "", clear: bool = False,
                        graph_secret: str = "", clear_graph: bool = False) -> dict:
        from .watch import _DEFAULT_CFG, _deep_merge
        cfg = json.loads(json.dumps(_DEFAULT_CFG))
        new = copy.deepcopy(new or {})
        e = new.get("email") or {}
        e.pop("has_password", None)
        old_e = self.cfg.get("email", {})
        e["password_dpapi"] = "" if clear else old_e.get("password_dpapi", "")
        if password:
            e["password_dpapi"] = self._protect(password)
        g = e.get("graph") or {}
        g.pop("has_secret", None)
        g["client_secret_dpapi"] = "" if clear_graph else \
            (old_e.get("graph") or {}).get("client_secret_dpapi", "")
        if graph_secret:
            g["client_secret_dpapi"] = self._protect(graph_secret)
        e["graph"] = g
        new["email"] = e
        # 기본값 위에 화면 값 전체를 얹는다 (목록은 통째로 교체, dict 만 재귀 병합)
        _deep_merge(cfg, new)
        return cfg

    def validate(self, cfg: dict) -> list[str]:
        errs = []
        _r, perr = compile_patterns((cfg.get("rules") or {}).get("patterns") or [])
        errs += perr
        mode = str((cfg.get("tracking") or {}).get("mode", "default"))
        if mode not in ("default", "select", "auto"):
            errs.append(f"tracking.mode 값 오류: {mode}")
        if mode == "select" and not (cfg.get("tracking") or {}).get("files"):
            errs.append("select 모드인데 tracking.files 가 비어 있음")
        dev = str((cfg.get("llm") or {}).get("device", "cpu"))
        if dev not in ("cpu", "gpu", "auto"):
            errs.append(f"llm.device 값 오류: {dev}")
        em = cfg.get("email") or {}
        if em.get("enabled") and not em.get("dry_run"):
            if str(em.get("transport", "smtp")) == "graph":
                g = em.get("graph") or {}
                if not (g.get("tenant_id") and g.get("client_id") and g.get("sender")):
                    errs.append("Graph 발송에는 테넌트 ID·앱(클라이언트) ID·보낼 사서함이 필요합니다")
            elif not em.get("smtp_host"):
                errs.append("이메일을 켜려면 SMTP 서버 주소가 필요합니다 (또는 dry_run)")
        for k in ("poll_seconds",):
            if not isinstance(cfg.get(k), (int, float)) or cfg[k] < 2:
                errs.append(f"{k} 는 2 이상의 숫자여야 합니다")
        return errs

    def save(self, new: dict, password: str = "", clear: bool = False,
             graph_secret: str = "", clear_graph: bool = False) -> list[str]:
        cfg = self._merge_user_cfg(new, password, clear, graph_secret, clear_graph)
        errs = self.validate(cfg)
        if errs:
            return errs
        text = dump_config(cfg)
        yaml.safe_load(text)                      # 쓰기 전에 다시 읽혀야 한다
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "rb") as f:
                    old = f.read()
                with open(self.config_path + ".bak", "wb") as f:
                    f.write(old)
            except OSError as e:
                return [f"백업 실패: {e}"]
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write(text)
        was = self.running()
        self.stop_watch()
        self.cfg = cfg
        if was:
            self.start_watch()
        print(f"[콘솔] 설정 저장: {self.config_path} (백업 .bak) — 감시 "
              f"{'재시작' if was else '정지 상태 유지'}")
        return []

    # ── 조회 ───────────────────────────────────────────────────
    def _day_dir(self) -> str:
        from .watch import _today_dir
        d = _today_dir(self.cfg["watch_root"])
        return d if os.path.isdir(d) else ""

    def status(self) -> dict:
        from .llm import query_gpu_free, resolve_device
        snap = self.watch.snapshot() if self.watch else {}
        alert_dir = snap.get("alert_dir") or self.cfg["alert_dir"]
        day = dt.datetime.now().strftime("%Y%m%d")
        sev = Counter()
        try:
            with open(os.path.join(alert_dir, f"alerts_{day}.jsonl"), encoding="utf-8") as f:
                for ln in f:
                    try:
                        sev[json.loads(ln).get("severity", "")] += 1
                    except ValueError:
                        continue
        except OSError:
            pass
        incs = self.incidents(days=1, replay=False, limit=500)
        gfree = query_gpu_free()
        dev, why = resolve_device(self.cfg.get("llm", {}), gfree)
        e = self.cfg.get("email", {})
        return {"running": self.running(), "version": __version__,
                "site": self.cfg.get("site", ""), "config": self.config_path,
                "watch_root": self.cfg.get("watch_root"), "alert_dir": alert_dir,
                "day_dir": snap.get("day_dir") or self._day_dir(),
                "mode": (self.cfg.get("tracking") or {}).get("mode", "default"),
                "files": snap.get("files", []), "alerts": snap.get("alerts", []),
                "live_incidents": snap.get("incidents", []),
                "pattern_errors": snap.get("pattern_errors", []),
                "gpu": snap.get("gpu", []), "gpu_free": gfree,
                "llm": {"url": self.cfg["llm"].get("url"), "model": self.cfg["llm"].get("model"),
                        "device": self.cfg["llm"].get("device"), "resolved": dev,
                        "reason": why, "agent": bool(self.cfg["agent"].get("enabled")),
                        "use_llm": bool(self.cfg["agent"].get("use_llm", True))},
                "email": {"enabled": bool(e.get("enabled")), "dry_run": bool(e.get("dry_run")),
                          "host": e.get("smtp_host"), "to": len(e.get("to") or []),
                          "immediate": e.get("immediate")},
                "counts": {"crit": sev.get("crit", 0), "warn": sev.get("warn", 0),
                           "info": sev.get("info", 0), "incidents": len(incs),
                           "mail_sent": sum(1 for i in incs if i.get("mail") == "발송"),
                           "mail_failed": sum(1 for i in incs if i.get("mail") == "실패")},
                "uptime_s": (time.time() - snap["started"]) if snap.get("started") else 0,
                "replay": {k: v for k, v in self.replay_job.items() if k != "log"}}

    def files(self, folder: str = "") -> dict:
        folder = folder or self._day_dir() or _latest_day_dir(self.cfg["watch_root"])
        rows = []
        tracked = set()
        if folder and os.path.isdir(folder):
            tracked = set(resolve_targets(folder, self.cfg.get("tracking")))
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
            alg = os.path.join(folder, "alg")
            n_alg = len(os.listdir(alg)) if os.path.isdir(alg) else 0
        else:
            n_alg = 0
        names, seen = _recipe_defects(folder) if folder else ([], [])
        return {"folder": folder, "today": self._day_dir(), "files": rows, "alg_files": n_alg,
                "recipe_defects": names, "seen_defects": seen,
                "defaults": ["InspStarter.log", "Comm.log", "WorkerThreadPoolMng.log",
                             "exception.log", "ProcessUsage.log", "BatchRunLog.txt",
                             "seq_1.log", "seq_2.log", "seq_3.log"]}

    def incidents(self, days: int = 3, replay: bool = True, limit: int = 200) -> list[dict]:
        alert_dir = self.cfg["alert_dir"]
        if self.watch is not None:
            alert_dir = self.watch.notifier.alert_dir
        pats = [os.path.join(alert_dir, "incidents_*.jsonl")]
        files = sorted(glob.glob(pats[0]), reverse=True)
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
                    "id": d.get("id"), "replay": is_rep, "day": m.group(1) if m else "",
                    "time": d.get("opened"), "site": d.get("site"),
                    "title": (d.get("alerts") or [{}])[0].get("title", ""),
                    "severity": v.get("severity"), "cause": v.get("cause_name"),
                    "llm": (llm.get("root_cause") if llm.get("ok") else
                            ("실패" if llm else "-")),
                    "llm_device": llm.get("device"), "llm_s": llm.get("wall_s"),
                    "mode": v.get("label"),
                    "mail": ("발송" if em.get("sent") else "보관" if em.get("eml") else
                             ("실패" if em.get("error") else "-"))})
        rows.sort(key=lambda r: (r["day"], r["time"] or ""), reverse=True)
        return rows[:limit]

    def incident(self, iid: str, replay: bool) -> dict | None:
        alert_dir = self.watch.notifier.alert_dir if self.watch else self.cfg["alert_dir"]
        pat = "incidents_*_replay.jsonl" if replay else "incidents_[0-9]*[0-9].jsonl"
        for p in sorted(glob.glob(os.path.join(alert_dir, pat)), reverse=True):
            try:
                with open(p, encoding="utf-8") as f:
                    for ln in f:
                        if f'"id": "{iid}"' in ln:
                            return json.loads(ln)
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
        m = Mailer(self.cfg, self.cfg["alert_dir"], replay=True)
        return m.body_html(inc)

    # ── 테스트 ──────────────────────────────────────────────────
    def test_llm(self, body: dict) -> dict:
        from .llm import OllamaClient
        lc = copy.deepcopy(self.cfg["llm"])
        for k in ("url", "model", "cpu_threads", "keep_alive", "think"):
            if k in body and body[k] not in (None, ""):
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
        dev = body.get("device") or None
        r = cli.chat(msgs, num_predict=80, device=dev if dev in ("cpu", "gpu") else None)
        if r["error"]:
            return {"ok": False, "error": r["error"], "device": r["device"]}
        gen_s = max(0.01, r["wall_s"] - r["load_s"])
        return {"ok": True, "device": r["device"], "reason": r["device_reason"],
                "wall_s": r["wall_s"], "load_s": r["load_s"],
                "prompt_tokens": r["prompt_tokens"], "gen_tokens": r["gen_tokens"],
                "tok_s": round(r["gen_tokens"] / gen_s, 1), "reply": r["content"][:300],
                "model": cli.model}

    def llm_models(self, url: str) -> dict:
        from .llm import OllamaClient
        cli = OllamaClient({"url": url or self.cfg["llm"].get("url")})
        alive = cli.alive()
        return {"alive": alive, "url": cli.url, "models": cli.models() if alive else []}

    def _mailer_for(self, body: dict):
        from .mailer import Mailer
        cfg = copy.deepcopy(self.cfg)
        e = cfg["email"]
        for k, v in (body.get("email") or {}).items():
            if k == "graph" and isinstance(v, dict):
                g = e.setdefault("graph", {})
                for gk, gv in v.items():
                    if gk not in ("client_secret_dpapi", "has_secret"):
                        g[gk] = gv
            elif k not in ("password_dpapi", "has_password"):
                e[k] = v
        e["enabled"] = True
        m = Mailer(cfg, self.watch.notifier.alert_dir if self.watch else cfg["alert_dir"])
        pw = body.get("password") or ""
        if pw:
            m.password = lambda: pw               # 저장 전 입력값으로 시험 (저장·출력 안 함)
        gs = body.get("graph_secret") or ""
        if gs:
            m.graph_secret = lambda: gs
        return m

    def test_smtp(self, body: dict) -> dict:
        m = self._mailer_for(body)
        ok, detail = m.check()
        typed = body.get("graph_secret") if m.transport == "graph" else body.get("password")
        return {"ok": ok, "detail": detail, "transport": m.transport,
                "password_source": ("입력값" if typed else m.password_source() or "없음")}

    def test_email(self, body: dict) -> dict:
        from .mailer import sample_incident
        m = self._mailer_for(body)
        inc = sample_incident(self.cfg.get("site") or "설비")
        inc["verdict"]["notify"] = list((m.e.get("roles") or {}).keys())
        res = m.send_incident(inc)
        res["ok"] = bool(res.get("sent") or res.get("dry_run"))
        return res

    def test_pattern(self, body: dict) -> dict:
        rules, errs = compile_patterns([body.get("pattern") or {}])
        if errs or not rules:
            return {"ok": False, "error": "; ".join(errs) or "패턴이 비어 있음"}
        r = rules[0]
        line = str(body.get("line") or "")
        fname = str(body.get("file") or "")
        m = r.rx.search(line)
        return {"ok": True, "match": bool(m) and (not fname or r.applies(fname)),
                "span": list(m.span()) if m else None,
                "file_ok": (not fname) or r.applies(fname)}

    def inject(self, kind: str) -> dict:
        if self.watch is None or not self.running():
            return {"ok": False, "error": "감시가 정지 상태입니다 — 먼저 시작하십시오"}
        from .watch import Alert
        now = time.time()
        samples = {
            "noinsp": Alert(now, "no_insp_thread", "crit",
                            "[테스트] 검사 시작 거부(NoInspThread) — 미검사 임박",
                            "콘솔에서 주입한 테스트 경보입니다.", key="test-noinsp",
                            cooldown_min=0.05),
            "defect": Alert(now, "defect_critical", "crit", "[테스트] 치명 결함 검출: TEST_DEFECT",
                            "콘솔에서 주입한 테스트 경보입니다.", key="test-defect",
                            cooldown_min=0.05),
            "warn": Alert(now, "pattern", "warn", "[테스트] 로그 패턴 '테스트' 3회",
                          "콘솔에서 주입한 주의 등급 테스트 경보입니다.", key="test-warn",
                          cooldown_min=0.05),
        }
        a = samples.get(kind)
        if a is None:
            return {"ok": False, "error": f"알 수 없는 종류: {kind}"}
        self.watch.notifier.emit(a)
        return {"ok": True, "message": f"{a.title} 주입 — 사건 분석·메일 정책대로 처리됩니다"}

    def start_replay(self, day_dir: str) -> dict:
        if self.replay_job.get("state") == "running":
            return {"ok": False, "error": "이미 리플레이가 진행 중입니다"}
        if not os.path.isdir(day_dir):
            return {"ok": False, "error": f"폴더가 없습니다: {day_dir}"}
        cfg = copy.deepcopy(self.cfg)
        job = {"state": "running", "day_dir": day_dir, "started": time.time(), "log": []}
        self.replay_job = job

        def work():
            from .watch import run_replay
            buf = deque(maxlen=300)
            try:
                # 리플레이 출력은 작업 기록에만 남긴다 (실시간 로그와 섞이지 않게)
                job["log"] = buf
                rc = _run_captured(run_replay, (cfg, day_dir), buf)
                job["state"] = "done" if rc == 0 else "error"
            except Exception as e:
                buf.append(f"오류: {type(e).__name__}: {e}")
                job["state"] = "error"
            finally:
                job["elapsed_s"] = round(time.time() - job["started"], 1)

        threading.Thread(target=work, name="talog-replay", daemon=True).start()
        return {"ok": True}


_capture_local = threading.local()


def _run_captured(fn, args, buf: deque) -> int:
    """리플레이 스레드의 print 를 buf 로 모은다 (다른 스레드 출력은 그대로)."""
    _capture_local.buf = buf
    try:
        return fn(*args)
    finally:
        _capture_local.buf = None


class _ThreadRouter(io.TextIOBase):
    """스레드별 출력 분기: 리플레이 스레드는 작업 기록으로, 나머지는 콘솔 창+UI 로그로."""

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
                if u.path == "/api/status":
                    return self._send(200, con.status())
                if u.path == "/api/config":
                    return self._send(200, {"cfg": con.public_cfg(), "rules": BUILTIN_RULES,
                                            "presets": PRESETS, "smtp": _smtp_presets(),
                                            "roles": _roles()})
                if u.path == "/api/files":
                    return self._send(200, con.files(q.get("dir", "")))
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
                if u.path == "/api/mx":
                    from .mailer import mx_hosts
                    return self._send(200, {"domain": q.get("domain", ""),
                                            "hosts": mx_hosts(q.get("domain", ""))})
                if u.path == "/api/log":
                    return self._send(200, {"lines": list(con.log)[-300:]})
                if u.path == "/api/replay":
                    j = dict(con.replay_job)
                    j["log"] = list(j.get("log") or [])[-200:]
                    return self._send(200, j)
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
                if u == "/api/config":
                    errs = con.save(body.get("cfg") or {}, body.get("password") or "",
                                    bool(body.get("clear_password")),
                                    body.get("graph_secret") or "",
                                    bool(body.get("clear_graph_secret")))
                    return self._send(200 if not errs else 400,
                                      {"ok": not errs, "errors": errs,
                                       "cfg": con.public_cfg()})
                if u == "/api/control":
                    act = body.get("action")
                    if act == "start":
                        con.start_watch()
                    elif act == "stop":
                        con.stop_watch()
                    return self._send(200, {"ok": True, "running": con.running()})
                if u == "/api/test/llm":
                    return self._send(200, con.test_llm(body))
                if u == "/api/test/smtp":
                    return self._send(200, con.test_smtp(body))
                if u == "/api/test/email":
                    return self._send(200, con.test_email(body))
                if u == "/api/test/pattern":
                    return self._send(200, con.test_pattern(body))
                if u == "/api/test/alert":
                    return self._send(200, con.inject(body.get("kind", "")))
                if u == "/api/replay":
                    return self._send(200, con.start_replay(body.get("day_dir", "")))
            except Exception as e:
                return self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            return self._send(404, {"error": "not found"})

    return H


def _smtp_presets() -> dict:
    from .mailer import SMTP_PRESETS
    return SMTP_PRESETS


def _roles() -> dict:
    try:
        from .agent import Runbook
        return Runbook().roles
    except Exception:
        return {}


def serve(config_path: str, port: int = 8778, open_browser: bool = True,
          autostart: bool = True) -> int:
    con = Console(config_path, port)
    router = _ThreadRouter(_Tee(sys.stdout, con.log))
    sys.stdout = router
    srv = ThreadingHTTPServer(("127.0.0.1", port), _handler_factory(con))
    url = f"http://127.0.0.1:{port}/"
    print(f"[talog 콘솔] {url}  (설정 {con.config_path}) — 이 창을 닫으면 감시도 멈춥니다")
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
