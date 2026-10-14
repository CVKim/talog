"""데이터 출처와 재현 작업.

Source  talog.yaml 하나 = 현장 하나의 산출물 폴더(data_dir: 사건·KPI·기준선·리포트)와 원본 로그 루트.
Jobs    start_replay 가 띄운 재현 작업 — 작업 폴더(work_dir/jobs/<id>)에 설정·산출물·로그가 남는다.
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time

import yaml

from .. import settings
from .fmt import now_iso


class Source:
    """talog.yaml 하나 = 현장 하나의 산출물 폴더(data_dir)와 로그 루트."""

    def __init__(self, name: str, data_dir: str, log_root: str = "", config_path: str = "",
                 site: str = "", kind: str = "현장", llm: dict | None = None):
        self.name = name
        self.data_dir = os.path.abspath(data_dir) if data_dir else ""
        self.log_root = log_root
        self.config_path = config_path
        self.site = site or name
        self.kind = kind
        self.llm = llm or {}

    @classmethod
    def from_config(cls, path: str) -> "Source":
        s, _legacy = settings.load(path)
        cfg = settings.compile(s)
        site = str(s.get("site") or cfg.get("site") or "").strip()
        name = site or os.path.splitext(os.path.basename(path))[0]
        return cls(name, cfg.get("alert_dir", ""), cfg.get("watch_root", ""), os.path.abspath(path),
                   site, llm=dict(s.get("ai") or {}))

    @property
    def report_dir(self) -> str:
        return os.path.join(self.data_dir, "reports")

    def _glob(self, pattern: str) -> list[str]:
        return sorted(glob.glob(os.path.join(self.data_dir, pattern))) if self.data_dir else []

    def incident_files(self) -> list[str]:
        return self._glob("incidents_*.jsonl")

    def incidents(self) -> list[dict]:
        out = []
        for p in self.incident_files():
            rep = p.endswith("_replay.jsonl")
            try:
                with open(p, encoding="utf-8") as f:
                    for ln in f:
                        ln = ln.strip()
                        if not ln:
                            continue
                        try:
                            d = json.loads(ln)
                        except ValueError:
                            continue
                        d["_replay"] = rep
                        d["_source"] = self.name
                        out.append(d)
            except OSError:
                continue
        return out

    def kpi_days(self) -> list[str]:
        days = set()
        for p in self._glob("kpi_*.jsonl"):
            m = re.search(r"kpi_(\d{8})", os.path.basename(p))
            if m:
                days.add(m.group(1))
        return sorted(days)

    def kpi_rows(self, day: str) -> list[dict]:
        rows = []
        for p in self._glob(f"kpi_{day}*.jsonl"):
            try:
                with open(p, encoding="utf-8") as f:
                    rows += [json.loads(x) for x in f if x.strip()]
            except (OSError, ValueError):
                continue
        return rows

    def capacity_state(self) -> tuple[dict | None, str]:
        cands = [p for p in (os.path.join(self.data_dir, "capacity_state.json"),
                             os.path.join(self.data_dir, "capacity_state_replay.json"))
                 if os.path.exists(p)]
        if not cands:
            return None, ""
        p = max(cands, key=os.path.getmtime)
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f), p
        except (OSError, ValueError):
            return None, p

    def reports(self) -> list[dict]:
        out = []
        for p in sorted(glob.glob(os.path.join(self.report_dir, "*_diagnosis.md"))):
            tag = os.path.basename(p)[: -len("_diagnosis.md")]
            db = os.path.join(self.report_dir, tag + ".sqlite")
            findings = []
            try:
                with open(p, encoding="utf-8") as f:
                    findings = [ln[3:].strip() for ln in f if ln.startswith("## ")]
            except OSError:
                pass
            out.append({"tag": tag, "diag": p, "db": db if os.path.exists(db) else "",
                        "mtime": os.path.getmtime(p), "findings": findings})
        return out


class Jobs:
    """start_replay 가 띄운 재현 작업 — 작업 폴더(work_dir/jobs/<id>)에 설정·산출물·로그가 남는다."""

    MAX_RUNNING = 2

    def __init__(self, work_dir: str):
        self.dir = os.path.join(work_dir, "jobs")
        self.procs: dict[str, subprocess.Popen] = {}
        self.lock = threading.Lock()

    def _meta_path(self, jid: str) -> str:
        return os.path.join(self.dir, jid, "job.json")

    def meta(self, jid: str) -> dict | None:
        if not re.match(r"^[\w\-]+$", jid or ""):
            return None
        try:
            with open(self._meta_path(jid), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _save(self, jid: str, meta: dict):
        with open(self._meta_path(jid), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)

    def all(self) -> list[dict]:
        out = []
        for p in sorted(glob.glob(os.path.join(self.dir, "*", "job.json")), reverse=True):
            try:
                with open(p, encoding="utf-8") as f:
                    out.append(json.load(f))
            except (OSError, ValueError):
                continue
        return out

    def running(self) -> int:
        return sum(1 for p in self.procs.values() if p.poll() is None)

    def state(self, jid: str) -> dict | None:
        m = self.meta(jid)
        if m is None:
            return None
        if m.get("state") == "running":
            p = self.procs.get(jid)
            rc = p.poll() if p is not None else None
            log = self.log_text(jid)
            done = "리플레이 완료" in log
            if p is not None and rc is not None or (p is None and done):
                m["state"] = "done" if (rc in (0, None) and done) else "failed"
                m["ended"] = now_iso()
                m["returncode"] = rc
                self._save(jid, m)
            elif p is None and not done and time.time() - m.get("t0", 0) > 6 * 3600:
                m["state"] = "failed"
                m["note"] = "서버가 다시 떠 작업 진행을 알 수 없음"
                self._save(jid, m)
        return m

    def log_text(self, jid: str) -> str:
        try:
            with open(os.path.join(self.dir, jid, "replay.log"), encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    def start(self, src: Source, day_dir: str, carry: bool, use_llm: bool) -> dict:
        with self.lock:
            if self.running() >= self.MAX_RUNNING:
                raise RuntimeError(f"재현 작업이 이미 {self.MAX_RUNNING}개 돌고 있습니다 — job_status 로 끝나기를 기다리십시오")
            os.makedirs(self.dir, exist_ok=True)
            slug = re.sub(r"[^\w\-]+", "_", f"{src.name}_{os.path.basename(day_dir.rstrip(chr(92) + '/'))}")[:40]
            jid = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + slug.strip("_")
            jdir = os.path.join(self.dir, jid)
            os.makedirs(jdir, exist_ok=True)
            seeded = ""
            if carry:
                st, path = src.capacity_state()
                if st:
                    with open(os.path.join(jdir, "capacity_state_replay.json"), "w", encoding="utf-8") as f:
                        json.dump(st, f, ensure_ascii=False)
                    seeded = os.path.basename(path)
            ai = {"llm": bool(use_llm)}
            if use_llm:
                for k in ("url", "model", "device"):
                    if src.llm.get(k):
                        ai[k] = src.llm[k]
            cfg = {"site": src.site, "log_root": src.log_root, "data_dir": jdir, "tracking": "core",
                   "ai": ai, "mail": {"provider": "off"}, "notify": {"toast": False, "webhook": ""},
                   "advanced": {"agent": {"min_severity": "warn"}}}
            cfg_path = os.path.join(jdir, "talog.yaml")
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write("# talog MCP 재현 작업 (자동 생성)\n")
                yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
            if getattr(sys, "frozen", False):
                cmd = [sys.executable, "run", "--config", cfg_path, "--replay", day_dir]
            else:
                cmd = [sys.executable, "-u", "-m", "talog", "run", "--config", cfg_path, "--replay", day_dir]
            # 버퍼를 끄면 job_status 가 진행 중에도 로그 끝을 보여 준다
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1")
            pkg_parent = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            log = open(os.path.join(jdir, "replay.log"), "w", encoding="utf-8")
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 cwd=pkg_parent, env=env, creationflags=flags)
            log.close()
            self.procs[jid] = p
            meta = {"id": jid, "source": src.name, "site": src.site, "day_dir": day_dir, "dir": jdir,
                    "state": "running", "started": now_iso(), "t0": time.time(), "pid": p.pid,
                    "carry_baseline": bool(seeded), "seeded_from": seeded, "llm": bool(use_llm)}
            self._save(jid, meta)
            return meta

    def source(self, jid: str) -> Source | None:
        m = self.meta(jid)
        if not m:
            return None
        return Source(f"재현:{jid}", m["dir"], "", os.path.join(m["dir"], "talog.yaml"),
                      m.get("site", ""), kind="재현 작업")
