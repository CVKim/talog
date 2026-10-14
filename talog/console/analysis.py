"""분석 — 진단 리포트·경보 재현 작업(하위 프로세스)·리포트 목록·리포트 화면·리포트에 묻기."""

from __future__ import annotations

import copy
import datetime as dt
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque

from .jobs import JOB_TAGS, self_command
from .util import REPO, TAG_RE


class AnalysisMixin:
    """Console 이 물려받는다 — 공유 상태는 app.Console.__init__ 에 있다."""

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
        cmd = self_command() + ["_job", kind, target, "--config", cfg_path, "--out", out]
        if recipe:
            cmd += ["--recipe", recipe]
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1")
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
                 if os.name == "nt" else 0)

        def work():
            buf = job["log"]
            try:
                p = subprocess.Popen(cmd, cwd=None if getattr(sys, "frozen", False) else REPO, env=env,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     creationflags=flags)
                job["pid"] = p.pid
                for raw in p.stdout:
                    ln = raw.decode("utf-8", "replace").rstrip()
                    if ln.startswith(JOB_TAGS):
                        try:
                            job["reports"] = json.loads(ln[len(JOB_TAGS):])
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
        if not TAG_RE.match(tag or ""):
            raise FileNotFoundError(tag)
        base = os.path.join(self.report_dir, tag)
        if os.path.dirname(os.path.abspath(base)) != os.path.abspath(self.report_dir):
            raise FileNotFoundError(tag)
        return base + ".html", base + ".sqlite"

    def _db(self, tag: str) -> sqlite3.Connection:
        from ..viewer import _open_ro
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
        from ..viewer import query_detail, query_insp, query_meta
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
        from ..ask import OllamaBackend, ToolBox, _build_system
        from ..llm import OllamaClient, resolve_device
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
        from ..agent import fact_check
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
