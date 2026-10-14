"""운영 — 감시 수명·설정 저장(메일 비밀은 DPAPI)·운영 현황·수집 폴더 미리보기."""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import threading
import time
from collections import Counter

from .. import __version__, settings
from ..fileclass import classify
from ..tracker import PRESETS, resolve_targets
from .util import _latest_day_dir, _recipe_defects, recent_days


class OpsMixin:
    """Console 이 물려받는다 — 공유 상태는 app.Console.__init__ 에 있다."""

    # ── 감시 수명 ───────────────────────────────────────────────
    def start_watch(self):
        from ..watch import LiveWatch
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
        from ..mailer import Mailer
        try:
            from ..agent import Runbook
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
                "legacy": self.legacy, "secret_env": env if env.startswith("환경") else "",
                "access": self.access_info()}

    @staticmethod
    def _protect(text: str) -> str:
        from ..secret import available, protect
        if not available():
            raise ValueError("이 OS 에서는 비밀 암호화 저장을 지원하지 않습니다 — 환경변수 "
                             "TALOG_SMTP_PASSWORD / TALOG_GRAPH_SECRET 를 쓰십시오")
        return protect(text)

    def _with_secret(self, new: dict, secret: str = "", clear: bool = False) -> dict:
        """화면 값 + 비밀. 발송 방식이 바뀌면 옛 비밀은 버린다 (다른 계정의 비밀이므로).
        모드는 화면 값이 아니라 지금 파일 값을 그대로 둔다 (modes.keep_protected)."""
        s = self.keep_protected(copy.deepcopy(new or {}))
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
        errs = self._write_settings(s)
        if errs:
            return errs
        was = self.running()
        self.stop_watch()
        self.cfg = settings.compile(s)
        if was:
            self.start_watch()
        print(f"[콘솔] 설정 저장: {self.config_path} (이전 파일 .bak) — 감시 "
              f"{'재시작' if was else '정지 상태 유지'}")
        return []

    def _write_settings(self, s: dict) -> list[str]:
        """검증 → 이전 파일을 .bak 으로 → talog.yaml 쓰기 → self.settings 갱신. 감시는 건드리지 않는다."""
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
        self.settings, self.legacy = s, False
        return []

    # ── 운영 현황 ───────────────────────────────────────────────
    def _llm_up(self) -> bool:
        t, up = self._llm_alive
        if time.time() - t > 30:
            from ..llm import OllamaClient
            cli = OllamaClient(self.cfg["llm"])
            up = cli.alive() and cli.model in cli.models()
            self._llm_alive = (time.time(), up)
        return up

    def status(self) -> dict:
        from ..llm import query_gpu_free, resolve_device
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
            "config": self.config_path, "data_dir": self.data_dir, "access": self.access_info(),
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
        from ..watch import _today_dir
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
