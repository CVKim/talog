"""talog MCP 서버 — Claude Code·Claude 데스크톱(구독)이 talog 산출물을 도구로 읽게 한다.

  talog mcp --config <현장 talog.yaml> [--config …] [--work-dir 폴더] [--no-mask] [--kb 폴더]
  talog mcp --self-test --config …        설치 점검 (도구 목록·개요를 한 번 돌려 보고 끝낸다)

talog 가 Claude 를 부르지 않고 Claude 가 talog 를 부른다 — API 키가 없고, 사용자의 Claude 구독 한도 안에서
돈다(추가 과금 없음). 현장 PC 의 talog(룰 + 로컬 Qwen)는 그대로이고, 이 서버는 현장에서 가져온 산출물
(사건·KPI·기준선·진단 리포트)이 있는 사무실 PC 에서 돈다.

전송: MCP stdio — 줄 단위 JSON-RPC 2.0(UTF-8). 표준 출력에는 프로토콜 메시지만 쓰고 나머지는 stderr.
도구 대부분은 읽기 전용이다. 쓰는 도구는 재현 작업(start_replay: 작업 폴더에만 씀)과 사례 저장(save_case)뿐.
마스킹(기본 켬): 12자리 이상 숫자(inner id 등)는 ID-xxxxxx 로, 출처의 로그·데이터 경로는 <로그>·<데이터> 로
가리고, 원본 로그 줄은 요청할 때만 마스킹해서 준다. Claude 가 읽은 내용은 Anthropic 으로 가므로 고객이
승인한 범위만 출처로 넣는다.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.request import pathname2url

import yaml

from . import __version__
from . import logread
from . import settings

SERVER_NAME = "talog"
# 이 서버는 도구·프롬프트·리소스의 기본 기능만 쓰므로 판마다 호환된다 — 클라이언트가 고른 판을 그대로 쓴다
_KNOWN_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
_MAX_TEXT = 22000                 # 도구 결과 한 번의 글자 수 상한 (Claude Code 출력 한도 안)
_TAG_RE = re.compile(r"^[\w\-. ]+$")
_DAY_RE = re.compile(r"^(\d{8})$")
_LONG_DIGITS = re.compile(r"(?<!\d)\d{12,}(?!\d)")
_WIN_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\)[^\s\"'|<>*?]+")
_SEV_KO = {"crit": "심각", "warn": "주의", "info": "정보"}
_SQL_TIMEOUT_S = 20.0


def _now_iso() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _hm(ts) -> str:
    try:
        return dt.datetime.fromtimestamp(float(ts)).strftime("%m/%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "-"


def _cut(text: str, limit: int = _MAX_TEXT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n… (결과가 길어 {len(text) - limit:,}자를 잘랐습니다 — 범위를 좁혀 다시 호출하십시오)"


def _cell(v, width: int = 80) -> str:
    s = "" if v is None else str(v)
    s = s.replace("|", "／").replace("\r", " ").replace("\n", " ")
    return s if len(s) <= width else s[: width - 1] + "…"


def _table(headers: list[str], rows: list[list], width: int = 80) -> str:
    if not rows:
        return "(없음)"
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(_cell(v, width) for v in r) + " |")
    return "\n".join(out)


def _num(v, nd: int = 3):
    if v is None:
        return "-"
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
        return str(int(v))
    if isinstance(v, float):
        s = f"{v:.{nd}f}".rstrip("0").rstrip(".")
        return s or "0"
    return v


# ── 마스킹 ──────────────────────────────────────────────────────────
class Masker:
    """고객 데이터 가리기. 같은 값은 같은 토큰이 되므로 Claude 가 사건 사이를 이어 볼 수 있다."""

    def __init__(self, on: bool, salt: str, roots: dict[str, str] | None = None, map_path: str = ""):
        self.on = on
        self.salt = salt
        # 긴 경로부터 바꾼다 (상위 경로가 하위 경로를 먼저 먹지 않게)
        self.roots = sorted(((k, v) for k, v in (roots or {}).items() if k), key=lambda kv: -len(kv[0]))
        # 토큰 → 원래 값. Claude 가 받은 ID-xxxxxx 로 원본 로그를 찾을 수 있게 작업 폴더에 남긴다 (이 PC 안에서만)
        self.map_path = map_path
        self.rev: dict[str, str] = {}
        self._dirty = False
        if map_path:
            try:
                with open(map_path, encoding="utf-8") as f:
                    self.rev = json.load(f)
            except (OSError, ValueError):
                self.rev = {}

    def add_root(self, root: str, alias: str):
        if root and all(r != root for r, _a in self.roots):
            self.roots = sorted(self.roots + [(root, alias)], key=lambda kv: -len(kv[0]))

    def token(self, digits: str) -> str:
        tok = "ID-" + hashlib.sha1((self.salt + digits).encode("utf-8")).hexdigest()[:6]
        if self.rev.get(tok) != digits and len(self.rev) < 200000:
            self.rev[tok] = digits
            self._dirty = True
        return tok

    def unmask(self, s: str) -> str:
        """ID-xxxxxx 토큰을 원래 값으로 (모르는 토큰은 그대로)."""
        return re.sub(r"ID-[0-9a-f]{6}", lambda m: self.rev.get(m.group(0), m.group(0)), s or "")

    def save(self):
        if self._dirty and self.map_path:
            try:
                tmp = self.map_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.rev, f)
                os.replace(tmp, self.map_path)
                self._dirty = False
            except OSError:
                pass

    def text(self, s) -> str:
        if not isinstance(s, str):
            return s
        if not self.on:
            return s
        for root, alias in self.roots:
            for variant in {root, root.replace("\\", "/"), root.replace("/", "\\")}:
                if variant and variant in s:
                    s = s.replace(variant, alias)
        s = _WIN_PATH.sub(lambda m: "<경로>\\" + re.split(r"[\\/]", m.group(0).rstrip("\\/"))[-1], s)
        return _LONG_DIGITS.sub(lambda m: self.token(m.group(0)), s)

    def obj(self, o):
        if not self.on:
            return o
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, list):
            return [self.obj(x) for x in o]
        if isinstance(o, dict):
            return {self.text(k) if isinstance(k, str) else k: self.obj(v) for k, v in o.items()}
        return o


# ── 데이터 출처 ─────────────────────────────────────────────────────
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


# ── 재현 작업 ───────────────────────────────────────────────────────
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
                m["ended"] = _now_iso()
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
            pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            log = open(os.path.join(jdir, "replay.log"), "w", encoding="utf-8")
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 cwd=pkg_parent, env=env, creationflags=flags)
            log.close()
            self.procs[jid] = p
            meta = {"id": jid, "source": src.name, "site": src.site, "day_dir": day_dir, "dir": jdir,
                    "state": "running", "started": _now_iso(), "t0": time.time(), "pid": p.pid,
                    "carry_baseline": bool(seeded), "seeded_from": seeded, "llm": bool(use_llm)}
            self._save(jid, meta)
            return meta

    def source(self, jid: str) -> Source | None:
        m = self.meta(jid)
        if not m:
            return None
        return Source(f"재현:{jid}", m["dir"], "", os.path.join(m["dir"], "talog.yaml"),
                      m.get("site", ""), kind="재현 작업")


# ── 도구 정의 ───────────────────────────────────────────────────────
def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [],
            "additionalProperties": False}


_S = {"type": "string"}
_SOURCE = {"type": "string", "description": "데이터 출처 이름 (overview 의 '출처' 열, 일부만 써도 됨). 비우면 전체"}
_DAY = {"type": "string", "description": "날짜 YYYYMMDD (예: 20261012). 비우면 가장 최근"}

TOOLS = [
    {"name": "overview", "title": "데이터 개요",
     "description": "처음 부르는 도구. 연결된 talog 데이터 출처(현장)별 사건 수·날짜·처리 여유 기준선·진단 리포트·재현 작업·"
                    "사례 KB 와 마스킹 상태를 보여 준다.",
     "inputSchema": _schema({}), "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "list_incidents", "title": "사건 목록",
     "description": "talog 실시간 감시(또는 경보 재현)가 만든 사건 목록. 사건 = 묶인 경보 + 룰 진단 + LLM 2차 의견. "
                    "id 를 get_incident 에 넘긴다.",
     "inputSchema": _schema({"source": _SOURCE, "day": _DAY,
                             "severity": {"type": "string", "enum": ["all", "crit", "warn"],
                                          "description": "심각도 거르기 (기본 all)"},
                             "limit": {"type": "integer", "minimum": 1, "maximum": 200,
                                       "description": "최대 행 수 (기본 30)"}}),
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "get_incident", "title": "사건 상세",
     "description": "사건 하나의 판단(룰·LLM 일치 여부, 원인, 권고 조치), 룰 근거, 해석 문장, 경보, 근거 데이터"
                    "(처리 여유·추세·단계·일시 지연·자원·검사 흐름·GPU 로그·재시작), 비슷한 과거 사례.",
     "inputSchema": _schema({"incident_id": {"type": "string", "description": "사건 id (예: 20261012-050228-capacity_drift)"},
                             "source": _SOURCE,
                             "include_log_lines": {"type": "boolean", "description": "근거 로그 원문 줄을 넣을지 (기본 false, 마스킹 적용)"},
                             "raw_evidence": {"type": "boolean", "description": "근거 JSON 전체를 덧붙일지 (기본 false)"}},
                            ["incident_id"]),
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "capacity_trend", "title": "처리 여유 추세",
     "description": "Seq 그룹별 처리 여유: 기준선(기동 뒤 첫 1시간의 유휴 스레드·점유 시간·투입 간격·풀)과 그날 시간별 KPI"
                    "(유휴 중앙값, 점유 p50/p99, 한계=풀×투입 간격, 일시 지연 내성, 거부), 프로세스 CPU·메모리.",
     "inputSchema": _schema({"source": _SOURCE, "day": _DAY,
                             "groups": {"type": "array", "items": {"type": "integer"},
                                        "description": "볼 그룹 번호 (비우면 점유가 가장 많이 는 그룹 4개)"}}),
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "list_reports", "title": "진단 리포트 목록",
     "description": "talog analyze(콘솔 '진단 리포트 만들기')가 만든 일자별 리포트와 자동 진단 소견 제목.",
     "inputSchema": _schema({"source": _SOURCE}), "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "get_report", "title": "진단 리포트 소견",
     "description": "리포트 하나의 자동 진단 소견(심각·주의·정상과 근거·권고)과 리포트 DB 의 표·행 수·열 이름"
                    "(query_report 로 SQL 을 쓰기 전에 본다).",
     "inputSchema": _schema({"tag": {"type": "string", "description": "리포트 태그 (list_reports 의 tag)"},
                             "source": _SOURCE}, ["tag"]),
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "query_report", "title": "리포트 DB 조회 (SQL)",
     "description": "리포트 SQLite 를 읽기 전용으로 조회한다. SELECT/WITH 한 문장만, 행 수 제한, 20초 제한. "
                    "표: inspections(검사 1건=1행: start_text, status, duration_s, wait_threads, end_result …), "
                    "events(파싱 이벤트: ts_text, kind, inner_id, value, status, name, extra …), channel_runs, "
                    "process_gens, model_loads, files.",
     "inputSchema": _schema({"tag": {"type": "string"}, "sql": {"type": "string", "description": "SELECT … (LIMIT 이 없으면 붙인다)"},
                             "max_rows": {"type": "integer", "minimum": 1, "maximum": 500, "description": "기본 50"},
                             "source": _SOURCE}, ["tag", "sql"]),
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "log_days", "title": "원본 로그 폴더",
     "description": "원본 talos 로그를 직접 읽을 수 있는 일자 폴더(설정의 로그 루트, 콘솔에서 고른 폴더)와 날짜·파일 수·크기. "
                    "원본 로그를 보기 전에 먼저 부른다.",
     "inputSchema": _schema({"source": _SOURCE}), "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "log_files", "title": "원본 로그 파일 목록",
     "description": "일자 폴더의 로그 파일: 분류(핵심/대용량)·크기·첫/마지막 시각. 어느 파일을 볼지 고를 때 쓴다. "
                    "InspResult·alg·cam 같은 GB 단위 파일은 시각을 주고 읽는다.",
     "inputSchema": _schema({"day": {"type": "string", "description": "일자 폴더 (log_days 의 이름, 예: 12, 2026_10\\12, 20261012). 비우면 가장 최근"},
                             "source": _SOURCE}),
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "read_log", "title": "원본 로그 읽기",
     "description": "파일 하나의 구간을 원문 그대로 읽는다(마스킹 적용). at(시각 HH:MM[:SS]) 앞뒤 · offset(앞 결과의 '다음 위치') · "
                    "tail(끝) 중 하나. 줄 앞 시각으로 이분 탐색해 4GB 파일도 그 시각으로 바로 간다. 한 번 최대 300줄.",
     "inputSchema": _schema({"file": {"type": "string", "description": "파일 이름 (log_files 의 이름, 예: seq_10.log, alg\\alg_dl_2(2_SIDE1).log)"},
                             "day": {"type": "string", "description": "일자 폴더 (비우면 가장 최근)"},
                             "at": {"type": "string", "description": "이 시각 앞뒤를 읽는다 (예: 05:02:28)"},
                             "offset": {"type": "integer", "minimum": 0, "description": "앞 결과의 '다음 위치'부터 이어 읽기"},
                             "tail": {"type": "boolean", "description": "파일 끝을 읽기"},
                             "lines": {"type": "integer", "minimum": 1, "maximum": 300, "description": "읽을 줄 수 (기본 80)"},
                             "before": {"type": "integer", "minimum": 0, "maximum": 200, "description": "at 앞 줄 수 (기본 20)"},
                             "source": _SOURCE}, ["file"]),
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "search_logs", "title": "원본 로그 찾기",
     "description": "일자 폴더의 로그에서 문구(대소문자 무시)나 정규식을 찾아 시간순으로 보여 준다. files 를 비우면 핵심 로그"
                    "(InspStarter·comm·seq_N·exception·WorkerThreadPoolMng 등). 160MB 넘는 파일은 time_from/time_to 를 줘야 찾는다. "
                    "ID-xxxxxx 토큰은 원래 검사 id 로 바꿔 찾는다. 한 번 최대 300건·20초.",
     "inputSchema": _schema({"query": {"type": "string", "description": "찾을 문구 (예: NoInspThread, Waiting thread count = 0, ID-1a2b3c)"},
                             "day": {"type": "string", "description": "일자 폴더 (비우면 가장 최근)"},
                             "files": {"type": "array", "items": {"type": "string"}, "description": "파일 이름·와일드카드 (예: [\"seq_*.log\", \"alg\\\\*.log\"])"},
                             "regex": {"type": "boolean", "description": "query 를 정규식으로 (기본 false)"},
                             "time_from": {"type": "string", "description": "이 시각부터 (예: 04:55)"},
                             "time_to": {"type": "string", "description": "이 시각까지 (예: 05:05)"},
                             "max_hits": {"type": "integer", "minimum": 1, "maximum": 300, "description": "최대 건수 (기본 100)"},
                             "source": _SOURCE}, ["query"]),
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "trace_inspection", "title": "검사 하나 추적",
     "description": "검사 id(원래 값 또는 ID-xxxxxx) 하나를 InspStarter·comm·seq_N·alg 로그에서 시간순으로 모은다 — 도착·유휴 스레드·"
                    "Seq 시작·알고리즘·판정·결과 송신·거부까지. 시작 시각은 id 안의 시각이나 첫 등장에서 찾는다.",
     "inputSchema": _schema({"inspection_id": {"type": "string", "description": "검사(inner) id 또는 ID-xxxxxx"},
                             "day": {"type": "string", "description": "일자 폴더 (비우면 가장 최근)"},
                             "at": {"type": "string", "description": "id 에서 시각을 못 찾을 때 시작 시각 (예: 05:02:40)"},
                             "window_s": {"type": "integer", "minimum": 10, "maximum": 1800, "description": "시작 뒤 몇 초까지 (기본 180)"},
                             "source": _SOURCE}, ["inspection_id"]),
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "start_replay", "title": "경보 재현 시작",
     "description": "현장 로그 폴더 하루치를 지금 talog 규칙으로 시간순 재생한다(백그라운드, 수 분~수십 분). 메일은 보내지 "
                    "않고 작업 폴더에만 쓴다. 규칙을 고친 뒤 결과가 어떻게 바뀌는지, 경보가 언제 났을지 확인할 때 쓴다. "
                    "job_status 로 진행·결과를 본다.",
     "inputSchema": _schema({"source": {"type": "string", "description": "로그 루트가 있는 출처"},
                             "day": {"type": "string", "description": "로그 루트 아래 일자 폴더 이름 (예: 12 또는 2026_10\\12)"},
                             "carry_baseline": {"type": "boolean", "description": "출처에 저장된 처리 여유 기준선을 이어받을지 (기본 true)"},
                             "llm": {"type": "boolean", "description": "사건마다 로컬 LLM 교차 확인도 돌릴지 (기본 false — 느림)"}},
                            ["source", "day"]),
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}},
    {"name": "job_status", "title": "재현 작업 상태",
     "description": "재현 작업의 진행·결과. 끝나면 경보·사건 목록이 나오고, 그 사건은 source='재현:<job id>' 로 "
                    "get_incident·capacity_trend 에 쓸 수 있다. job_id 를 비우면 작업 목록.",
     "inputSchema": _schema({"job_id": {"type": "string"}}),
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "rediagnose", "title": "저장된 사건 재진단",
     "description": "저장된 사건 근거에 지금 코드의 룰 진단을 다시 돌려 원인이 바뀌는 사건을 보여 준다 (규칙 회귀 확인).",
     "inputSchema": _schema({"source": _SOURCE, "day": _DAY}),
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "runbook", "title": "원인·조치 사전",
     "description": "talog 런북: 경보 → 분석 계열, 계열별 원인(이름·판단 근거 설명)과 조치·담당, 판정 문턱값. "
                    "룰 진단과 같은 어휘로 말하려면 먼저 본다.",
     "inputSchema": _schema({"family": {"type": "string", "enum": ["", "reject", "defect", "system"],
                                        "description": "계열 (비우면 전체 요약)"}}),
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "search_cases", "title": "비슷한 과거 사례",
     "description": "사례 KB(과거 RCA: 증상·원인·조치)에서 비슷한 사례를 찾는다 (로컬 임베딩, 없으면 키워드).",
     "inputSchema": _schema({"query": {"type": "string"}, "k": {"type": "integer", "minimum": 1, "maximum": 10}}, ["query"]),
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "save_case", "title": "사례 저장",
     "description": "확정된 RCA 를 사례 KB 에 한 건 추가한다. 다음 배포 때 현장 talog 가 비슷한 사건에 이 사례를 붙인다. "
                    "고객명·검사 id 같은 원본 값은 넣지 말고 현상·원인·조치를 일반화해 쓴다.",
     "inputSchema": _schema({"title": _S, "symptom": _S, "cause": _S, "action": _S,
                             "site": {"type": "string", "description": "현장 (가명 가능)"},
                             "tags": {"type": "array", "items": {"type": "string"}}},
                            ["title", "symptom", "cause", "action"]),
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}},
]

PROMPTS = [
    {"name": "incident_rca", "title": "사건 RCA",
     "description": "talog 사건 하나를 근거 확인 → 원인 판단 → 권고 → 보고서까지",
     "arguments": [{"name": "incident_id", "description": "사건 id (예: 20261012-050228-capacity_drift)", "required": True}]},
    {"name": "daily_review", "title": "하루 검토",
     "description": "하루치 경보·사건·처리 여유를 훑고 놓친 징후·오경보를 짚는다",
     "arguments": [{"name": "source", "description": "출처 (비우면 전체)", "required": False},
                   {"name": "day", "description": "YYYYMMDD (비우면 최근)", "required": False}]},
    {"name": "site_health", "title": "현장 점검",
     "description": "현장별 처리 여유·기준선·재시작·경보 경향과 규칙이 그 현장에 맞는지 점검",
     "arguments": [{"name": "source", "description": "출처 (비우면 전체)", "required": False}]},
    {"name": "log_investigation", "title": "원본 로그 조사",
     "description": "원본 talos 로그를 직접 읽어 증상(시각·문구·검사 id)의 경과와 원인을 찾는다",
     "arguments": [{"name": "day", "description": "일자 폴더 (예: 12, 2026_10\\12 또는 콘솔에서 고른 경로)", "required": False},
                   {"name": "symptom", "description": "증상 — 예: 05:02 NoInspThread, 검사 id, 오류 문구", "required": False}]},
]

_GUIDE = """# talog 용어 (Claude 용 요약)

- talos: AiV 비전 검사 플랫폼. 설비가 검사 요청(M2V_INSPECT_START)을 보내면 Seq 그룹의 Seq 스레드가 검사를 맡는다.
- 유휴 스레드(W): 검사 요청이 도착한 순간 그 그룹에 놀고 있던 Seq 스레드 수 (InspStarter 'Waiting thread count').
  0 이면 NoInspThread 로 거부 → 설비는 미검사로 처리한다.
- 점유 시간(hold): 검사 한 건이 스레드를 잡고 있는 시간 (seq 'CheckFinish done' — 결과 송신 뒤 저장·DB 까지).
- 투입 간격: 같은 그룹 도착 간격 중앙값. 풀 = 그룹의 스레드 수(추정). 한계 = 풀 × 투입 간격.
- 일시 지연 내성 = 한계 − 최근 60분 점유 p99. 작을수록 잠깐의 지연 한 번에 거부가 난다.
- 기준선: 기동(재시작)·기종 교체 뒤 워밍업 10분을 지난 첫 1시간의 그룹별 값. 재시작하면 새로 잡는다.
- 단계(phases): 처리(시작~판정) · 결과 송신 · 후처리(저장·DB). 각 단계는 여러 처리의 합이다.
- 판단 구조: 룰이 원인을 정하고 로컬 LLM 은 교차 확인만 한다. 조치는 런북 문장. 같은 원인일 때만 '판단 일치'.
- 주요 원인: 시간 경과형 처리 열화(투입 그대로인데 점유가 기동 뒤 꾸준히 증가) · 처리량 포화(투입이 빨라지거나
  GPU 커널이 느려짐) · GPU 컨텍스트 치명 오류 · 완료 신호 끊김(처리는 되는데 INSPECT_END 만 안 보임) ·
  단건 정체(한두 건만 남고 스레드는 남음) · 타임아웃 후 슬롯 미반환 · 기동 직후 미초기화.
"""

_INSTRUCTIONS = (
    "talog(talos 비전 검사 로그 분석기)의 산출물을 읽는 도구다. overview 로 시작해 list_incidents → get_incident → "
    "capacity_trend·get_report·query_report 순으로 근거를 확인한다. 원본 로그는 log_days → log_files → search_logs"
    "(시각 범위) → read_log → trace_inspection 으로 직접 읽는다 — 하루 폴더가 수십 GB 이므로 늘 시각 범위를 좁혀서 찾는다. "
    "검사 id·경로는 마스킹돼 ID-xxxxxx·<로그> 로 보인다(그 토큰 그대로 다시 찾을 수 있다). "
    "수치·시각은 도구 결과에 있는 값만 인용하고, 룰 진단에 동의하는지 근거와 함께 밝힌다. 조치는 runbook 의 문장을 "
    "기준으로 한다. 사용자에게는 한국어로 답한다. 산출물은 사용자의 로컬 Result 폴더에 파일로 남기고 외부에 게시하지 않는다."
)


# ── 서버 ────────────────────────────────────────────────────────────
class TalogMCP:
    def __init__(self, sources: list[Source], work_dir: str, mask: bool = True, kb_dir: str = ""):
        self.sources = sources
        self.work_dir = os.path.abspath(work_dir)
        os.makedirs(self.work_dir, exist_ok=True)
        self.jobs = Jobs(self.work_dir)
        self.kb_dir = kb_dir
        roots = {}
        for s in sources:
            if s.log_root:
                roots[s.log_root] = "<로그>" if len(sources) == 1 else f"<로그:{s.name}>"
            if s.data_dir:
                roots[s.data_dir] = "<데이터>" if len(sources) == 1 else f"<데이터:{s.name}>"
        roots[self.work_dir] = "<작업>"
        self.mask = Masker(mask, self._salt(), roots, os.path.join(self.work_dir, ".mask_map.json"))
        self.client = {}

    def _salt(self) -> str:
        p = os.path.join(self.work_dir, ".mask_salt")
        try:
            with open(p, encoding="utf-8") as f:
                s = f.read().strip()
                if s:
                    return s
        except OSError:
            pass
        s = secrets.token_hex(8)
        try:
            with open(p, "w", encoding="utf-8") as f:
                f.write(s)
        except OSError:
            pass
        return s

    # ── 출처 고르기 ────────────────────────────────────────────
    def all_sources(self) -> list[Source]:
        out = list(self.sources)
        for m in self.jobs.all():
            if m.get("state") == "done":
                js = self.jobs.source(m["id"])
                if js:
                    out.append(js)
        return out

    def pick(self, name: str = "", need_logs: bool = False) -> list[Source]:
        cands = self.all_sources()
        if name:
            n = name.strip().lower()
            if n.startswith("재현:"):
                js = self.jobs.source(name.split(":", 1)[1].strip())
                return [js] if js else []
            exact = [s for s in cands if s.name.lower() == n]
            cands = exact or [s for s in cands if n in s.name.lower() or n in s.site.lower()]
        if need_logs:
            cands = [s for s in cands if s.log_root]
        return cands

    def _one(self, name: str, need_logs: bool = False) -> Source:
        c = self.pick(name, need_logs)
        if not c:
            raise ValueError(f"출처를 찾지 못했습니다: '{name}' — overview 의 '출처' 이름을 쓰십시오")
        if len(c) > 1 and name:
            raise ValueError("출처가 여럿 맞습니다: " + ", ".join(s.name for s in c) + " — 더 구체적으로 쓰십시오")
        return c[0]

    # ── 도구 ───────────────────────────────────────────────────
    def t_overview(self, _a: dict) -> str:
        lines = [f"# talog 데이터 개요 — talog {__version__}",
                 f"마스킹: {'켜짐 (검사 id → ID-xxxxxx, 경로 → <로그>·<데이터>, 원본 로그 줄은 요청 시만)' if self.mask.on else '꺼짐 (원본 값 그대로)'}",
                 ""]
        rows = []
        for s in self.all_sources():
            incs = s.incidents()
            by_day: dict[str, int] = {}
            n_crit = sum(1 for i in incs if (i.get("verdict") or {}).get("severity") == "crit")
            for i in incs:
                by_day[(i.get("id") or "")[:8]] = by_day.get((i.get("id") or "")[:8], 0) + 1
            latest = max(incs, key=lambda i: i.get("id") or "", default=None)
            st, _p = s.capacity_state()
            base = ""
            if st and st.get("groups"):
                frm = min((g.get("from") or 0) for g in st["groups"].values())
                base = f"{len(st['groups'])}그룹 ({_hm(frm)}~)"
            reps = s.reports()
            rows.append([s.name, s.kind, ", ".join(f"{d[4:6]}/{d[6:]}({n})" for d, n in sorted(by_day.items())[-5:]) or "-",
                         f"{len(incs)} (심각 {n_crit})",
                         (f"{(latest.get('id') or '')[:8]} {latest.get('opened', '')} "
                          f"{((latest.get('alerts') or [{}])[0]).get('title', '')}") if latest else "-",
                         base or "-", ", ".join(r["tag"] for r in reps[-3:]) or "-",
                         "있음" if s.log_root else "-"])
        lines.append(_table(["출처", "종류", "날짜(사건 수)", "사건", "최근 사건", "처리 여유 기준선", "리포트", "로그 루트"], rows, 70))
        jobs = self.jobs.all()
        if jobs:
            lines += ["", f"재현 작업 {len(jobs)}건 (최근 5건):",
                      _table(["job id", "상태", "출처", "시작"],
                             [[j["id"], j.get("state"), j.get("source"), j.get("started")] for j in jobs[:5]])]
        try:
            from .casekb import CaseKB
            kb = CaseKB(self.kb_dir)
            lines += ["", f"사례 KB: {len(kb.cases)}건"]
        except Exception:
            pass
        lines += ["", "다음: list_incidents(day=…) → get_incident(incident_id=…) · capacity_trend(source=…) · "
                      "list_reports → get_report(tag=…) · runbook()"]
        return self.mask.text("\n".join(lines))

    def t_list_incidents(self, a: dict) -> str:
        day = self._day(a.get("day", ""))
        sev = (a.get("severity") or "all").lower()
        limit = int(a.get("limit") or 30)
        rows = []
        for s in self.pick(a.get("source", "")):
            for i in s.incidents():
                iid = i.get("id") or ""
                if day and not iid.startswith(day):
                    continue
                v = i.get("verdict") or {}
                if sev in ("crit", "warn") and v.get("severity") != sev:
                    continue
                al = i.get("alerts") or [{}]
                rows.append((iid, [iid, f"{iid[4:6]}/{iid[6:8]} {i.get('opened', '')}",
                                   _SEV_KO.get(v.get("severity"), v.get("severity") or "-"),
                                   al[0].get("title", "") + (f" (+{len(al) - 1})" if len(al) > 1 else ""),
                                   (v.get("cause_name") or "-").split(" — ")[0], v.get("label") or "-",
                                   s.name + (" ·재현" if i.get("_replay") else "")]))
        rows.sort(key=lambda r: r[0], reverse=True)
        total = len(rows)
        body = _table(["사건 id", "시각", "심각도", "제목", "원인(룰)", "판단", "출처"], [r[1] for r in rows[:limit]], 60)
        head = f"# 사건 {total}건" + (f" — {day}" if day else "") + (f" (최근 {limit}건 표시)" if total > limit else "")
        return self.mask.text(head + "\n" + body + "\n\n다음: get_incident(incident_id=…)")

    def _find_incident(self, iid: str, source: str = "") -> dict:
        hits = [i for s in self.pick(source) for i in s.incidents() if i.get("id") == iid]
        if not hits:
            raise ValueError(f"사건을 찾지 못했습니다: {iid} — list_incidents 로 id 를 확인하십시오")
        live = [h for h in hits if not h.get("_replay")]
        return (live or hits)[-1]

    def t_get_incident(self, a: dict) -> str:
        from .agent import Runbook
        inc = self._find_incident(str(a.get("incident_id", "")).strip(), a.get("source", ""))
        rb = Runbook()
        v, rule, ev = inc.get("verdict") or {}, inc.get("rule") or {}, inc.get("evidence") or {}
        alerts = inc.get("alerts") or []
        out = [f"# 사건 {inc.get('id')} — {(alerts[0] if alerts else {}).get('title', '')}",
               f"- 현장 {inc.get('site') or '-'} · {inc.get('opened', '')}~{inc.get('closed', '')} · 계열 "
               f"{(rb.families.get(inc.get('family') or '') or {}).get('title', inc.get('family'))} · 출처 {inc.get('_source')}"
               + (" (경보 재현)" if inc.get("_replay") else ""), "",
               "## 판단",
               f"- {v.get('label', '-')} · {_SEV_KO.get(v.get('severity'), v.get('severity', '-'))} · 원인: {v.get('cause_name', '-')}",
               f"- 담당자 메시지: {v.get('message', '-')}",
               "- 권고 조치:"]
        out += [f"  {k}. {rb.action_ko(x)}" for k, x in enumerate(v.get("actions") or [], 1)]
        if v.get("notify"):
            out.append("- 담당: " + ", ".join(rb.role_ko(r) for r in v["notify"]))
        if rule.get("facts"):
            out += ["", "## 룰 근거"] + [f"- {f}" for f in rule["facts"]]
        if ev.get("key_facts"):
            out += ["", "## 핵심 사실 (해석 문장 — 로컬 LLM 입력)"] + [f"- {f}" for f in ev["key_facts"]]
        if alerts:
            out += ["", f"## 경보 {len(alerts)}건",
                    _table(["시각", "심각도", "규칙", "제목", "근거"],
                           [[x.get("time"), _SEV_KO.get(x.get("severity"), x.get("severity")), x.get("rule"),
                             x.get("title"), x.get("evidence")] for x in alerts[:12]], 90)]
        out += ["", "## 근거 데이터 (요약)"] + self._evidence_lines(ev)
        llm = inc.get("llm") or {}
        if llm:
            out += ["", "## 로컬 LLM 2차 의견",
                    (f"- {llm.get('model', '')} · {llm.get('device', '')} · {llm.get('wall_s', '')}초 → 원인 "
                     f"{llm.get('root_cause', '-')} (신뢰도 {llm.get('confidence', '-')})" if llm.get("ok")
                     else f"- 실패: {llm.get('error', '')}")]
            out += [f"  · {e}" for e in (llm.get("evidence") or [])[:4]]
            if llm.get("message_dropped"):
                out.append("  · 문구 다듬기로 뺀 문장: " + " / ".join(llm["message_dropped"]))
        if inc.get("similar"):
            out += ["", "## 비슷한 과거 사례 (사례 KB)"] + [
                f"- [{s.get('score')}] {s.get('title')} — 원인: {s.get('cause')} / 조치: {s.get('action')}"
                for s in inc["similar"]]
        logs = ev.get("log_lines") or []
        if logs:
            if a.get("include_log_lines"):
                out += ["", f"## 근거 로그 {len(logs)}줄"] + [f"    {ln}" for ln in logs[:60]]
            else:
                out += ["", f"(근거 로그 {len(logs)}줄은 생략 — include_log_lines=true 로 요청)"]
        if a.get("raw_evidence"):
            ev2 = {k: v2 for k, v2 in ev.items() if k not in ("log_lines", "key_facts")}
            out += ["", "## 근거 JSON", "```json", json.dumps(ev2, ensure_ascii=False, indent=1, default=str)[:12000], "```"]
        return _cut(self.mask.text("\n".join(out)))

    @staticmethod
    def _evidence_lines(ev: dict) -> list[str]:
        out = []
        def v(x, unit=""):
            return "없음" if x is None else f"{_num(x)}{unit}"

        c = ev.get("capacity") or {}
        if c:
            out.append(f"- 처리 여유: 그룹 {c.get('group')} · 풀 {v(c.get('pool'))} · 투입 {v(c.get('interval_s'), '초')} · "
                       f"한계 {v(c.get('limit_s'), '초')} · 유휴 중앙값 {v(c.get('idle_median'))} (기준 "
                       f"{v(c.get('idle_baseline'))}, 최소 {v(c.get('idle_min'))}) · 점유 p50/p99/최대 "
                       f"{v(c.get('hold_p50'))}/{v(c.get('hold_p99'))}/{v(c.get('hold_max'))}초 · 내성 "
                       f"{v(c.get('tolerance_s'), '초')} ({v(c.get('tolerance_ratio'))}) · 점유 출처 {c.get('hold_source') or '-'}")
        t = ev.get("trend") or {}
        if t and t.get("hold_p50_baseline") is not None:
            out.append(f"- 추세: 기준선 {t.get('since')}부터 {v(t.get('hours'), '시간')} · 점유 {v(t.get('hold_p50_baseline'))}→"
                       f"{v(t.get('hold_p50_now'))}초 ({v(t.get('hold_ratio'), '배')}) · 투입 {v(t.get('interval_baseline_s'))}→"
                       f"{v(t.get('interval_now_s'))}초 ({v(t.get('interval_change_pct'), '%')}) · 순위상관 "
                       f"{v(t.get('spearman_hourly'))} · 단조 증가 {'예' if t.get('monotonic') else '아니오'} · 함께 는 그룹 "
                       f"{t.get('groups_rising') or []} / 기준선 있는 그룹 {t.get('groups_with_baseline') or 0}개")
        elif t:
            out.append(f"- 추세: 기준선 없음 (재시작이 잦거나 기동 뒤 1시간이 안 됨) · 지금 점유 {v(t.get('hold_p50_now'), '초')} · "
                       f"투입 {v(t.get('interval_now_s'), '초')}")
        p = ev.get("phases") or {}
        if p:
            out.append(f"- 단계: {p.get('names')} 기준 {p.get('baseline_s')} → 지금 {p.get('now_s')} · 증가 몫 "
                       f"{p.get('share_of_increase_pct')}%")
        tr = ev.get("transient") or {}
        if tr:
            out.append(f"- 일시 지연(직전 60초): {tr.get('n_groups')}개 그룹 {tr.get('groups')} · " +
                       "; ".join(f"g{g}: 최대 {d.get('max_last_60s')}초 (앞 60분 p99 {d.get('p99_prev_60min')})"
                                 for g, d in list((tr.get("detail") or {}).items())[:4]))
        r = ev.get("resources") or {}
        if r:
            parts = []
            if r.get("talos_cpu_now_pct") is not None:
                parts.append(f"talos CPU {r['talos_cpu_now_pct']}% (구간 시작 {r.get('talos_cpu_start_pct', '-')}%)")
            if r.get("total_cpu_now_pct") is not None:
                parts.append(f"장비 CPU {r['total_cpu_now_pct']}%")
            if r.get("threads_now") is not None:
                parts.append(f"스레드 {r.get('threads_start', '-')}→{r['threads_now']}")
            if r.get("mem_slope_mb_per_h") is not None:
                parts.append(f"메모리 기울기 {r['mem_slope_mb_per_h']}MB/h")
            known = {"talos_cpu_now_pct", "talos_cpu_start_pct", "total_cpu_now_pct", "threads_start",
                     "threads_now", "mem_slope_mb_per_h", "since"}
            parts += [f"{k} {v}" for k, v in r.items() if k not in known and v not in (None, "", [], {})]
            out.append("- 자원" + (f"({r['since']}부터)" if r.get("since") else "") + ": " + " · ".join(parts))
        i = ev.get("inspections") or {}
        if i:
            out.append(f"- 검사 흐름: 진행 중 {i.get('inflight')} (가장 오래된 것 {i.get('oldest_inflight_s')}초) · 최근 완료 "
                       f"{i.get('recent_durations_s')}초 (평소 {i.get('baseline_duration_s')}초, {i.get('slowdown_ratio')}배) · "
                       f"소요/투입 {i.get('duration_over_interval')} · 최근 도착 유휴 {i.get('wait_threads_recent')} · 10분 "
                       f"시작/완료 {i.get('starts_10min')}/{i.get('ends_10min')} · 마지막 완료 {i.get('last_end_age_s')}초 전")
        g = ev.get("gpu_log") or {}
        if g:
            out.append(f"- GPU 로그: 치명 {g.get('fatal_near')} · 인퍼런스 오류 {g.get('infer_errors_near')} · executeV2 "
                       f"{g.get('exec_slowdown_ratio')}배 ({g.get('exec_slowest_model', '')})")
        rs = ev.get("restarts") or {}
        if rs:
            last = (f"마지막 {rs['last_restart']} ({rs.get('since_restart_s'):.0f}초 전)"
                    if rs.get("last_restart") and rs.get("since_restart_s") is not None else "최근 재시작 없음")
            out.append(f"- 재시작: {last} · 60분 {rs.get('count_60min') or 0}회")
        t2 = ev.get("timeouts") or {}
        if t2:
            n_it, n_at = t2.get("img_timeout_30min") or 0, t2.get("alg_timeout_30min") or 0
            out.append(f"- 타임아웃(30분): 이미지 {n_it} · 알고리즘 {n_at}" + (f" · 마지막 {t2['last']}" if t2.get("last") else ""))
        e = ev.get("errors") or {}
        if e:
            extra = [f"{k} {v}" for k, v in e.items()
                     if k not in ("seh_near", "crash_near") and v not in (None, 0, "", [], {}, False)]
            out.append(f"- 에러: SEH {e.get('seh_near') or 0} · 크래시 {'있음' if e.get('crash_near') else '없음'}"
                       + (" · " + ", ".join(extra)[:240] if extra else ""))
        m = ev.get("model_load") or {}
        if m:
            if m.get("last_cmd") or m.get("pending"):
                out.append(f"- 모델 로드: {m.get('name') or '-'} · {m.get('last_cmd') or '-'}"
                           + (" · 완료 응답 대기 중" if m.get("pending") else "")
                           + (f" · 기종 교체({m.get('prev_name')}→{m.get('name')})" if m.get("changed") else "")
                           + (f" · {m['minutes_since']}분 전" if m.get("minutes_since") is not None else ""))
            else:
                out.append("- 모델 로드: 최근 없음")
        return out or ["(근거 데이터 없음)"]

    def t_capacity_trend(self, a: dict) -> str:
        s = self._one(a.get("source", ""))
        st, st_path = s.capacity_state()
        days = s.kpi_days()
        day = self._day(a.get("day", "")) or (days[-1] if days else "")
        rows = s.kpi_rows(day) if day else []
        grp_rows = [r for r in rows if isinstance(r.get("group"), int)]
        proc = [r for r in rows if r.get("group") == "process"]
        base = (st or {}).get("groups") or {}
        want = [int(g) for g in (a.get("groups") or [])]
        if not want:
            ratio = {}
            for r in grp_rows:
                b = base.get(str(r["group"])) or {}
                if b.get("hold_p50") and r.get("hold_p50"):
                    ratio[r["group"]] = max(ratio.get(r["group"], 0), r["hold_p50"] / b["hold_p50"])
            want = [g for g, _ in sorted(ratio.items(), key=lambda kv: -kv[1])[:4]] or \
                sorted({r["group"] for r in grp_rows})[:4]
        out = [f"# 처리 여유 — {s.name} · {day[:4]}-{day[4:6]}-{day[6:]}" if day else f"# 처리 여유 — {s.name}"]
        if base:
            out += ["", f"## 기준선 ({os.path.basename(st_path)} · 세대 시작 {_hm((st or {}).get('gen_start'))})",
                    _table(["그룹", "기준 구간", "유휴 중앙값", "점유 p50", "점유 p99", "투입 간격", "풀", "도착 수"],
                           [[g, f"{_hm(b.get('from'))}~{_hm(b.get('to'))[6:]}", b.get("idle_median"), _num(b.get("hold_p50")),
                             _num(b.get("hold_p99")), _num(b.get("interval_s")), b.get("pool"), b.get("n")]
                            for g, b in sorted(base.items(), key=lambda kv: int(kv[0]))])]
        else:
            out += ["", "기준선 없음 — 기동 뒤 안정 구간(워밍업 10분 + 1시간, 도착 100건 이상)이 아직 없거나 재시작이 잦다."]
        if not rows:
            out += ["", f"시간별 KPI 없음 (kpi 파일 날짜: {', '.join(days) or '-'})"]
            return self.mask.text("\n".join(out))
        for g in want:
            gr = sorted((r for r in grp_rows if r["group"] == g), key=lambda r: r["hour"])
            if not gr:
                continue
            b = base.get(str(g)) or {}
            trs = []
            for r in gr:
                lim = (r.get("pool") or 0) * (r.get("interval_s") or 0)
                tol = (lim - r["hold_p99"]) if (lim and r.get("hold_p99") is not None) else None
                ratio = (r["hold_p50"] / b["hold_p50"]) if (b.get("hold_p50") and r.get("hold_p50")) else None
                trs.append([r["hour"][-5:], r.get("n"), _num(r.get("idle_median")), _num(r.get("hold_p50")),
                            _num(ratio, 2) if ratio else "-", _num(r.get("hold_p99")), _num(r.get("hold_max")),
                            _num(lim, 2) if lim else "-", _num(tol, 2) if tol is not None else "-",
                            _num(r.get("interval_s")), r.get("pool"), r.get("rejects") or 0])
            out += ["", f"## 그룹 {g}" + (f" (기준: 유휴 {b.get('idle_median')}, 점유 {b.get('hold_p50')}초)" if b else ""),
                    _table(["시", "도착", "유휴", "점유 p50", "기준 대비", "p99", "최대", "한계", "내성", "투입", "풀", "거부"], trs)]
        if proc:
            out += ["", "## 프로세스", _table(["시", "메모리 최대 MB", "talos CPU %", "장비 CPU %", "스레드"],
                                            [[r["hour"][-5:], r.get("mem_mb_max"), r.get("cpu_pct"),
                                              r.get("total_cpu_pct"), r.get("threads")]
                                             for r in sorted(proc, key=lambda r: r["hour"])])]
        others = sorted({r["group"] for r in grp_rows} - set(want))
        if others:
            out += ["", f"다른 그룹 {others} 는 groups=[…] 로 지정해 본다."]
        return _cut(self.mask.text("\n".join(out)))

    def t_list_reports(self, a: dict) -> str:
        rows = []
        for s in self.pick(a.get("source", "")):
            for r in s.reports():
                rows.append([r["tag"], s.name, _hm(r["mtime"]), "있음" if r["db"] else "-",
                             " / ".join(r["findings"][:4])])
        return self.mask.text("# 진단 리포트\n" + _table(["tag", "출처", "만든 때", "DB", "소견"], rows, 120)
                              + "\n\n다음: get_report(tag=…) → query_report(tag=…, sql=…)")

    def _report(self, tag: str, source: str = "") -> tuple[Source, dict]:
        if not _TAG_RE.match(tag or ""):
            raise ValueError("tag 형식이 올바르지 않습니다")
        for s in self.pick(source):
            for r in s.reports():
                if r["tag"] == tag:
                    return s, r
        raise ValueError(f"리포트를 찾지 못했습니다: {tag} — list_reports 로 확인하십시오")

    @staticmethod
    def _ro(db: str) -> sqlite3.Connection:
        con = sqlite3.connect(f"file:{pathname2url(os.path.abspath(db))}?mode=ro", uri=True, timeout=5)
        con.execute("PRAGMA query_only=ON")
        return con

    def t_get_report(self, a: dict) -> str:
        s, r = self._report(a.get("tag", ""), a.get("source", ""))
        with open(r["diag"], encoding="utf-8") as f:
            diag = f.read()
        out = [f"# 진단 리포트 {r['tag']} — {s.name}", "", diag.strip()]
        if r["db"]:
            con = self._ro(r["db"])
            try:
                meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
                tabs = [x[0] for x in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
                info = []
                for t in tabs:
                    cols = [c[1] for c in con.execute(f"PRAGMA table_info({t})")]
                    n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    info.append([t, f"{n:,}", ", ".join(cols)])
            finally:
                con.close()
            out += ["", "## 리포트 DB", f"- 원본 폴더: {meta.get('day_dir', '-')} · talog {meta.get('version', '-')}",
                    _table(["표", "행", "열"], info, 400),
                    "- *_ts 는 epoch 초, 사람이 읽는 시각은 *_text 열. inspections.status: complete/rejected/incomplete_lost/…",
                    "- events.kind 예: INSP_START(value=유휴 스레드), INSP_REJECT, COMM_MSG(name=V2M_INSPECT_END 등, value=그룹), "
                    "SEQ_FINISH(value=점유 ms), USAGE(value=메모리 MB), POOL_CREATE(재시작)"]
        return _cut(self.mask.text("\n".join(out)))

    def t_query_report(self, a: dict) -> str:
        s, r = self._report(a.get("tag", ""), a.get("source", ""))
        if not r["db"]:
            raise ValueError("이 리포트에는 DB 가 없습니다")
        q = (a.get("sql") or "").strip().rstrip(";").strip()
        q_nc = re.sub(r"--[^\n]*|/\*.*?\*/", " ", q, flags=re.S).strip()
        if not re.match(r"^(select|with)\b", q_nc, re.I):
            raise ValueError("SELECT(또는 WITH … SELECT) 한 문장만 허용됩니다")
        if ";" in q_nc:
            raise ValueError("한 문장만 허용됩니다 (세미콜론 금지)")
        # DB 는 읽기 전용(mode=ro·query_only)으로 열리지만, 파일을 붙이거나 설정을 바꾸는 문은 미리 막는다
        if re.search(r"\b(attach|detach|pragma|insert|update|delete|create|drop|alter|vacuum|reindex)\b", q_nc, re.I):
            raise ValueError("읽기 전용 조회만 허용됩니다")
        max_rows = max(1, min(500, int(a.get("max_rows") or 50)))
        con = self._ro(r["db"])
        deadline = time.time() + _SQL_TIMEOUT_S
        con.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 20000)
        try:
            cur = con.execute(q)
            cols = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(max_rows + 1)
        except sqlite3.OperationalError as e:
            if "interrupted" in str(e):
                raise ValueError(f"조회가 {_SQL_TIMEOUT_S:.0f}초를 넘었습니다 — 조건·인덱스 열(kind, inner_id, ts)로 좁히십시오")
            raise ValueError(f"SQL 오류: {e}")
        finally:
            con.close()
        more = len(rows) > max_rows
        rows = rows[:max_rows]
        body = _table(cols, [[self.mask.text(x) if isinstance(x, str) else _num(x, 4) for x in row] for row in rows], 100)
        return _cut(f"# {r['tag']} 조회 결과 {len(rows)}행" + (" (더 있음 — max_rows 를 늘리거나 조건을 좁히십시오)" if more else "")
                    + "\n```sql\n" + q + "\n```\n" + body)

    # ── 원본 로그 (logread) ──────────────────────────────────────
    def extra_log_roots(self) -> list[str]:
        """콘솔에서 고른 로그 폴더 (작업 폴더의 log_roots.json) — 설정의 로그 루트 밖이어도 읽을 수 있게 한다."""
        cfg = self._log_cfg()
        items = list(cfg.get("roots") or []) + ([cfg["selected"]] if cfg.get("selected") else [])
        return [p for p in items if isinstance(p, str) and os.path.isdir(p)]

    def _log_roots(self, source: str = "") -> list[str]:
        roots = [s.log_root for s in (self.pick(source, need_logs=True) if source else self.sources) if s.log_root]
        for r in self.extra_log_roots():
            self.mask.add_root(r, "<로그 폴더>")
            roots.append(r)
        out = []
        for r in roots:
            if r and all(os.path.normcase(os.path.abspath(r)) != os.path.normcase(os.path.abspath(x)) for x in out):
                out.append(r)
        if not out:
            raise ValueError("읽을 수 있는 원본 로그 폴더가 없습니다 — talog 설정의 로그 루트를 확인하거나 콘솔의 Claude 화면에서 "
                             "로그 폴더를 고르십시오")
        return out

    @staticmethod
    def _strip_ts(line: str) -> str:
        return re.sub(r"^\d{4}/\d{2}/\d{2}-\d{2}:\d{2}:\d{2}\.\d{3}\t?", "", line)

    def t_log_days(self, a: dict) -> str:
        roots = self._log_roots(a.get("source", ""))
        sel = self._log_cfg().get("selected") or ""
        sel = os.path.normcase(os.path.abspath(sel)) if sel else "?"
        rows = []
        for d in logread.day_dirs(roots, 30):
            files = logread._files(d)
            size = sum(os.path.getsize(p) for p in files if os.path.exists(p))
            dd = logread.day_date(d)
            name = next((os.path.relpath(d, r) for r in roots if logread.inside(d, r)), d)
            picked = os.path.normcase(os.path.abspath(d)) == sel
            rows.append(["선택" if picked else (name if name != "." else d), dd.isoformat() if dd else "-", len(files),
                         f"{size / 2**30:.1f} GB", "콘솔에서 고른 폴더" if picked else ""])
        return self.mask.text("# 원본 로그 일자 폴더\n" + _table(["day (이 이름을 쓴다)", "날짜", "파일", "크기", "비고"], rows)
                              + "\n\n다음: log_files(day=…) 로 파일과 시각 범위를 본다. 하루가 수십 GB 이므로 search_logs 는 "
                                "time_from/time_to 로 범위를 좁힌다.")

    def t_log_files(self, a: dict) -> str:
        d = self._day_dir(a)
        rows = [[r["name"], "핵심" if r["core"] else "대용량" if r["size"] > logread.FULL_SCAN_MAX else "기타",
                 f"{r['size'] / 2**20:,.1f} MB", logread.fmt_ts(r["first"], True), logread.fmt_ts(r["last"], True)]
                for r in logread.list_files(d)]
        return self.mask.text(f"# 로그 파일 — {d} ({len(rows)}개)\n"
                              + _table(["파일", "분류", "크기", "첫 줄", "마지막 줄"], rows, width=60)
                              + "\n\n160MB 넘는 파일은 search_logs 에 time_from/time_to 가 필요하다. read_log 는 크기와 상관없이 "
                                "at(시각)으로 바로 간다.")

    def t_read_log(self, a: dict) -> str:
        d = self._day_dir(a)
        files = logread.match_files(d, [a.get("file", "")])
        if not files:
            raise ValueError(f"파일이 없습니다: {a.get('file')} — log_files 로 이름을 확인하십시오")
        p = files[0]
        at = logread.parse_when(a["at"], logread.day_date(d)) if a.get("at") else None
        w = logread.read_window(p, at=at, offset=a.get("offset"), tail=bool(a.get("tail")),
                                lines=a.get("lines") or 80, before=a.get("before", 20))
        ts = [logread.ts_of(x.encode("utf-8", "replace")) for x in w["lines"]]
        ts = [t for t in ts if t is not None]
        head = (f"# {logread.rel(d, p)} — {logread.fmt_ts(ts[0]) if ts else '-'} ~ {logread.fmt_ts(ts[-1]) if ts else '-'}"
                f" · {len(w['lines'])}줄 · 위치 {w['start']:,} / {w['size']:,} 바이트")
        tail = ("\n(파일 끝)" if w["eof"] else
                f"\n이어 읽기: read_log(file='{logread.rel(d, p)}', offset={w['next']})")
        return _cut(self.mask.text(head + "\n```\n" + "\n".join(w["lines"]) + "\n```" + tail))

    def t_search_logs(self, a: dict) -> str:
        d = self._day_dir(a)
        ref = logread.day_date(d)
        t_from = logread.parse_when(a["time_from"], ref) if a.get("time_from") else None
        t_to = logread.parse_when(a["time_to"], ref) if a.get("time_to") else None
        if t_to is not None and t_from is not None and t_to < t_from:
            raise ValueError("time_to 가 time_from 보다 앞입니다")
        files = logread.match_files(d, a.get("files") or list(logread.CORE_DEFAULT))
        if not files:
            raise ValueError("찾을 파일이 없습니다 — log_files 로 이름을 확인하십시오")
        query = self.mask.unmask(a.get("query", ""))
        t0 = time.time()
        r = logread.search(files, query, regex=bool(a.get("regex")), t_from=t_from, t_to=t_to,
                           max_hits=a.get("max_hits") or 100, day_dir=d)
        lines = [f"{logread.fmt_ts(t)}  {name}  {self._strip_ts(text)}" for t, name, text in r["hits"]]
        rng = (f" · {a.get('time_from') or '처음'}~{a.get('time_to') or '끝'}" if (t_from or t_to) else "")
        head = (f"# 찾기 '{a.get('query', '')}' — {len(lines)}건{rng} · 파일 {len(files)}개 · "
                f"{r['scanned'] / 2**20:,.0f}MB 훑음 · {time.time() - t0:.1f}초"
                + (" · 한도에서 멈춤 (범위를 좁히거나 max_hits 를 늘리십시오)" if r["partial"] else ""))
        per = ", ".join(f"{k} {v}" for k, v in sorted(r["per_file"].items(), key=lambda kv: -kv[1]))
        body = ("\n파일별: " + per if per else "") + ("\n```\n" + "\n".join(lines) + "\n```" if lines else "\n(없음)")
        if r["skipped"]:
            body += "\n건너뛴 큰 파일: " + ", ".join(r["skipped"])
        return _cut(self.mask.text(head + body))

    def t_trace_inspection(self, a: dict) -> str:
        d = self._day_dir(a)
        raw = self.mask.unmask((a.get("inspection_id") or "").strip())
        if raw.startswith("ID-"):
            raise ValueError(f"{raw} 의 원래 값을 모릅니다 — 그 id 가 나온 도구(get_incident·search_logs)를 이 대화에서 먼저 부르십시오")
        hint = logread.parse_when(a["at"], logread.day_date(d)) if a.get("at") else None
        r = logread.trace(d, raw, a.get("window_s") or 180, hint=hint)
        lines = [f"{logread.fmt_ts(t)}  {name}  {self._strip_ts(text)}" for t, name, text in r["hits"]]
        per = ", ".join(f"{k} {v}" for k, v in sorted(r["per_file"].items(), key=lambda kv: -kv[1]))
        head = (f"# 검사 {raw} 추적 — {logread.fmt_ts(r['t0'])} 부터 {r['window_s']}초 · {len(lines)}줄 "
                f"(시작 시각: {r['found_by'] or 'at'})" + (" · 한도에서 멈춤" if r["partial"] else ""))
        return _cut(self.mask.text(head + ("\n파일별: " + per if per else "")
                                   + ("\n```\n" + "\n".join(lines) + "\n```" if lines else "\n(이 범위에 없음 — at 이나 window_s 를 바꿔 보십시오)")))

    def t_start_replay(self, a: dict) -> str:
        s = self._one(a.get("source", ""), need_logs=True)
        day = str(a.get("day") or "").strip().strip("\\/")
        if not day or ".." in day.replace("\\", "/").split("/"):
            raise ValueError("day 는 로그 루트 아래 일자 폴더 이름입니다 (예: 12, 2026_10\\12)")
        day_dir = day if os.path.isabs(day) else os.path.join(s.log_root, day)
        root = os.path.abspath(s.log_root)
        if not os.path.abspath(day_dir).startswith(root):
            raise ValueError("로그 루트 밖의 폴더는 재현할 수 없습니다")
        if not os.path.isdir(day_dir):
            subs = sorted(os.listdir(s.log_root))[-12:] if os.path.isdir(s.log_root) else []
            raise ValueError(f"일자 폴더가 없습니다: {day} — 로그 루트의 폴더: {', '.join(subs) or '(읽을 수 없음)'}")
        m = self.jobs.start(s, day_dir, a.get("carry_baseline", True) is not False, bool(a.get("llm")))
        return self.mask.text(
            f"# 재현 작업 시작 — {m['id']}\n- 출처 {s.name} · 일자 폴더 {day_dir}\n"
            f"- 기준선 이어받기: {'예 (' + m['seeded_from'] + ')' if m['carry_baseline'] else '아니오 (그날 첫 1시간으로 새로 잡음)'}"
            f" · 로컬 LLM: {'켬' if m['llm'] else '끔'}\n"
            f"- 하루치에 수 분~수십 분 걸린다. job_status(job_id='{m['id']}') 로 진행을 본다.")

    def t_job_status(self, a: dict) -> str:
        jid = (a.get("job_id") or "").strip()
        if not jid:
            jobs = self.jobs.all()
            for j in jobs:
                if j.get("state") == "running":
                    self.jobs.state(j["id"])
            jobs = self.jobs.all()
            return self.mask.text("# 재현 작업\n" + _table(["job id", "상태", "출처", "일자 폴더", "시작", "끝"],
                                                       [[j["id"], j.get("state"), j.get("source"), j.get("day_dir"),
                                                         j.get("started"), j.get("ended", "-")] for j in jobs[:20]], 70))
        m = self.jobs.state(jid)
        if m is None:
            raise ValueError(f"작업을 찾지 못했습니다: {jid}")
        log = self.jobs.log_text(jid)
        el = time.time() - m.get("t0", time.time())
        out = [f"# 재현 작업 {jid} — {'진행 중' if m['state'] == 'running' else '완료' if m['state'] == 'done' else '실패'}",
               f"- 출처 {m.get('source')} · 일자 폴더 {m.get('day_dir')} · 경과 {el / 60:.1f}분"
               + (f" · 끝 {m.get('ended')}" if m.get("ended") else "")]
        tail = [ln for ln in log.splitlines() if ln.strip()][-12:]
        if m["state"] == "running":
            out += ["", "## 최근 로그", "```", *tail, "```", "", "아직 진행 중 — 잠시 뒤 다시 job_status 를 부른다."]
            return self.mask.text("\n".join(out))
        js = self.jobs.source(jid)
        alerts = []
        for p in sorted(glob.glob(os.path.join(m["dir"], "alerts_*.jsonl"))):
            with open(p, encoding="utf-8") as f:
                alerts += [json.loads(x) for x in f if x.strip()]
        done = [ln for ln in log.splitlines() if "리플레이 완료" in ln]
        out += [f"- {done[-1].strip() if done else '완료 줄 없음'}", "",
                f"## 경보 {len(alerts)}건",
                _table(["시각", "심각도", "규칙", "제목"], [[x.get("ts"), _SEV_KO.get(x.get("severity"), x.get("severity")),
                                                         x.get("rule"), x.get("title")] for x in alerts[:40]], 80)]
        incs = js.incidents() if js else []
        if incs:
            out += ["", f"## 사건 {len(incs)}건 (source='재현:{jid}' 로 get_incident)",
                    _table(["사건 id", "원인(룰)", "판단"],
                           [[i.get("id"), ((i.get("verdict") or {}).get("cause_name") or "-").split(" — ")[0],
                             (i.get("verdict") or {}).get("label")] for i in incs[:40]])]
        if m["state"] == "failed":
            out += ["", "## 로그 끝부분", "```", *tail, "```"]
        return _cut(self.mask.text("\n".join(out)))

    def t_rediagnose(self, a: dict) -> str:
        from . import watch
        from .agent import RuleDiagnoser, Runbook
        from .watch import Alert
        day = self._day(a.get("day", ""))
        d = RuleDiagnoser(Runbook(), watch.load_config(""))
        rows, n, ch = [], 0, 0
        for s in self.pick(a.get("source", "")):
            for inc in s.incidents():
                if day and not (inc.get("id") or "").startswith(day):
                    continue
                if not inc.get("evidence") or not inc.get("rule"):
                    continue
                alerts = [Alert(0, x.get("rule", ""), x.get("severity", "warn"), x.get("title", ""), x.get("evidence", ""))
                          for x in inc.get("alerts") or []]
                # 옛 판의 사건은 근거 블록이 빠졌을 수 있다 — 빈 블록으로 채워 판정한다
                ev = {k: {} for k in ("gpu_log", "restarts", "timeouts", "inspections", "model_load", "errors",
                                      "defects", "memory")}
                ev.update(inc["evidence"])
                try:
                    new = d.diagnose(inc.get("family", ""), ev, alerts, inc.get("sys_cause", ""))
                except Exception as e:      # 옛 근거 형식 — 건너뛰고 알린다
                    rows.append([inc.get("id"), inc["rule"].get("cause"), f"오류 {type(e).__name__}", "", s.name])
                    continue
                n += 1
                changed = new["cause"] != inc["rule"].get("cause")
                ch += changed
                rows.append([inc.get("id"), inc["rule"].get("cause"), new["cause"], "바뀜" if changed else "",
                             s.name + (" ·재현" if inc.get("_replay") else "")])
        rows.sort(key=lambda r: (r[3] != "바뀜", r[0] or ""))
        return _cut(self.mask.text(f"# 재진단 — 사건 {n}건 중 원인이 바뀐 사건 {ch}건\n" +
                                   _table(["사건 id", "저장된 원인", "지금 규칙", "", "출처"], rows[:120])))

    def t_runbook(self, a: dict) -> str:
        from .agent import Runbook
        rb = Runbook()
        fam = (a.get("family") or "").strip()
        out = ["# talog 런북 (원인·조치 사전)"]
        if not fam:
            amap: dict[str, list[str]] = {}
            for rule, f in rb.alert_family.items():
                amap.setdefault(str(f).split(".")[0], []).append(rule)
            for k, f in rb.families.items():
                causes = rb.causes(k)
                out += ["", f"## {k} — {f.get('title', '')}",
                        "- 원인: " + ", ".join(f"{cid}({(c.get('name') or cid).split(' — ')[0]})" for cid, c in causes.items())]
                if amap.get(k):
                    out.append("- 경보 규칙: " + ", ".join(amap[k]))
            out += ["", "## 판정 문턱값 (params)", ", ".join(f"{k}={v}" for k, v in rb.params.items()),
                    "", "계열을 지정하면 원인별 판단 근거 설명과 조치를 보여 준다: runbook(family='reject')"]
            return "\n".join(out)
        f = rb.families.get(fam)
        if not f:
            raise ValueError(f"계열이 없습니다: {fam}")
        out.append(f"## {fam} — {f.get('title', '')}")
        for cid, c in rb.causes(fam).items():
            out += ["", f"### {cid} — {c.get('name', cid)}", (c.get("description") or "").strip(),
                    "- 조치: " + " / ".join(rb.action_ko(x) for x in c.get("actions") or []),
                    "- 담당: " + ", ".join(rb.role_ko(r) for r in c.get("notify") or [])]
        return _cut("\n".join(out))

    def t_search_cases(self, a: dict) -> str:
        from .casekb import CaseKB
        kb = CaseKB(self.kb_dir)
        k = max(1, min(10, int(a.get("k") or 5)))
        hits = kb.search(str(a.get("query") or ""), k)
        if not hits:
            return f"비슷한 사례 없음 (사례 KB {len(kb.cases)}건)"
        rows = [[round(sc, 2), c.id, c.site, c.date, c.title, c.cause, c.action] for sc, c in hits]
        return self.mask.text(f"# 비슷한 사례 {len(rows)}건 (사례 KB {len(kb.cases)}건)\n" +
                              _table(["점수", "id", "현장", "날짜", "제목", "원인", "조치"], rows, 90))

    def t_save_case(self, a: dict) -> str:
        from .casekb import Case, CaseKB
        kb = CaseKB(self.kb_dir)
        fields = {k: str(a.get(k) or "").strip() for k in ("title", "symptom", "cause", "action", "site")}
        if not all(fields[k] for k in ("title", "symptom", "cause", "action")):
            raise ValueError("title·symptom·cause·action 은 비울 수 없습니다")
        if self.mask.on and _LONG_DIGITS.search(" ".join(fields.values())):
            raise ValueError("사례에 검사 id 같은 원본 값이 있습니다 — 일반화해서 다시 쓰십시오")
        case = Case(id="", date=dt.date.today().isoformat(), tags=[str(t) for t in a.get("tags") or []], **fields)
        kb.add(case)
        return f"사례 저장: {case.id} — {case.title} (사례 KB {len(kb.cases) + 1}건, {kb.path})"

    # ── 공용 ────────────────────────────────────────────────────
    @staticmethod
    def _day(day: str) -> str:
        day = (day or "").strip().replace("-", "").replace("/", "")
        if not day:
            return ""
        if re.match(r"^\d{4}$", day):                       # MMDD → 올해
            day = f"{dt.date.today().year}{day}"
        if not _DAY_RE.match(day):
            raise ValueError("day 는 YYYYMMDD 형식입니다 (예: 20261012)")
        return day

    def call_tool(self, name: str, args: dict) -> str:
        fn = getattr(self, "t_" + name, None)
        if fn is None or name not in {t["name"] for t in TOOLS}:
            raise KeyError(name)
        try:
            return fn(args or {})
        finally:
            self.mask.save()                     # 새로 만든 ID 토큰 → 원래 값 (다음 대화에서도 다시 찾을 수 있게)

    def get_prompt(self, name: str, args: dict) -> dict:
        return prompt_messages(name, args)

    def resources(self) -> list[dict]:
        return [{"uri": "talog://runbook", "name": "runbook", "title": "talog 런북 (원인·조치 사전 원문)",
                 "mimeType": "text/yaml", "description": "rules/runbook.yaml"},
                {"uri": "talog://guide", "name": "guide", "title": "talog 용어 요약",
                 "mimeType": "text/markdown", "description": "유휴 스레드·점유·한계·내성·기준선·원인 이름"}]

    def read_resource(self, uri: str) -> dict:
        if uri == "talog://runbook":
            from .agent import _RUNBOOK_PATH
            with open(_RUNBOOK_PATH, encoding="utf-8") as f:
                return {"uri": uri, "mimeType": "text/yaml", "text": f.read()}
        if uri == "talog://guide":
            return {"uri": uri, "mimeType": "text/markdown", "text": _GUIDE}
        raise KeyError(uri)

    # ── 원본 로그 폴더 설정 (콘솔이 고른 폴더) ─────────────────────
    def _log_cfg(self) -> dict:
        try:
            with open(os.path.join(self.work_dir, "log_roots.json"), encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return {}
        return d if isinstance(d, dict) else {"roots": [x for x in d if isinstance(x, str)]} if isinstance(d, list) else {}

    def _day_dir(self, a: dict) -> str:
        """일자 폴더 인자 → 경로. '선택'(또는 비움 + 콘솔에서 고른 폴더가 있으면) = 콘솔에서 고른 폴더."""
        roots = self._log_roots(a.get("source", ""))
        day = str(a.get("day") or "").strip()
        sel = self._log_cfg().get("selected", "")
        if day in ("선택", "selected", "@") or (not day and sel and os.path.isdir(sel)):
            if not sel or not os.path.isdir(sel):
                raise ValueError("콘솔에서 고른 로그 폴더가 없습니다 — log_days 로 폴더 이름을 확인하십시오")
            if not logread._looks_like_day(sel):
                found = logread.day_dirs([sel], 1)
                if not found:
                    raise ValueError("고른 폴더에 talos 로그가 없습니다")
                return found[0]
            return sel
        return logread.resolve_day(roots, day)

    # ── JSON-RPC ───────────────────────────────────────────────
    def handle(self, msg: dict) -> dict | None:
        """요청 하나 → 응답(알림이면 None)."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        method, mid = msg.get("method"), msg.get("id")
        if method is None:
            return None                                  # 클라이언트가 보낸 응답 — 이 서버는 요청을 보내지 않는다
        if "id" not in msg:
            return None                                  # 알림 (initialized, cancelled …)
        p = msg.get("params") or {}
        try:
            if method == "initialize":
                req = str(p.get("protocolVersion") or "")
                ver = req if re.match(r"^\d{4}-\d{2}-\d{2}$", req) else _KNOWN_VERSIONS[1]
                self.client = p.get("clientInfo") or {}
                res = {"protocolVersion": ver,
                       "capabilities": {"tools": {"listChanged": False}, "prompts": {"listChanged": False},
                                        "resources": {"subscribe": False, "listChanged": False}, "logging": {}},
                       "serverInfo": {"name": SERVER_NAME, "title": "talog — AI 비전 로그 분석", "version": __version__},
                       "instructions": _INSTRUCTIONS}
            elif method == "ping":
                res = {}
            elif method == "tools/list":
                res = {"tools": TOOLS}
            elif method == "tools/call":
                name = p.get("name", "")
                if name not in {t["name"] for t in TOOLS}:
                    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"Unknown tool: {name}"}}
                try:
                    text = self.call_tool(name, p.get("arguments") or {})
                    res = {"content": [{"type": "text", "text": text}], "isError": False}
                except (ValueError, KeyError, RuntimeError, OSError, sqlite3.Error) as e:
                    res = {"content": [{"type": "text", "text": f"오류: {e}"}], "isError": True}
            elif method == "prompts/list":
                res = {"prompts": PROMPTS}
            elif method == "prompts/get":
                res = self.get_prompt(p.get("name", ""), p.get("arguments") or {})
            elif method == "resources/list":
                res = {"resources": self.resources()}
            elif method == "resources/templates/list":
                res = {"resourceTemplates": []}
            elif method == "resources/read":
                res = {"contents": [self.read_resource(p.get("uri", ""))]}
            elif method == "logging/setLevel":
                res = {}
            elif method == "completion/complete":
                res = {"completion": {"values": [], "total": 0, "hasMore": False}}
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
        except KeyError as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"Invalid params: {e}"}}
        except Exception as e:                           # 서버는 죽지 않는다
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": res}


def prompt_messages(name: str, args: dict) -> dict:
    """MCP 프롬프트 본문 — Claude 앱의 슬래시 명령과 콘솔의 빠른 작업이 같은 문장을 쓴다."""
    args = args or {}
    save = ("결과는 작업 폴더의 reports\\ (없으면 코드가 있는 드라이브의 Result\\)에 독립 실행 HTML 로 저장하고 "
            "(.claude/skills/talog-rca/templates/rca_report.html 형식), 답변에는 경로만 적는다. 외부에 게시하지 않는다.")
    if name == "incident_rca":
        iid = args.get("incident_id", "")
        text = (f"talog 사건 `{iid}` 의 RCA 를 해 주세요 (talog-rca 절차).\n"
                f"1. get_incident(incident_id='{iid}') 로 판단·룰 근거·해석 문장·경보·근거 데이터를 읽는다.\n"
                "2. capacity_trend 로 그 그룹의 기준선과 시간별 추세를 확인한다 (기동 뒤 언제부터 여유가 줄었나).\n"
                "3. 필요하면 get_report·query_report 로 리포트 DB 를, start_replay 로 재현을 확인한다.\n"
                "4. runbook 의 원인 사전 기준으로 룰 진단에 동의하는지 근거와 함께 밝히고, 다른 원인 가능성을 배제한다.\n"
                "5. 현장 조치(지금 / 다음 정지 때 / 개발팀)와 재발 방지를 정리한다. 확정되면 save_case 로 사례를 남길지 묻는다.\n"
                "수치·시각은 도구 결과에 있는 값만 쓴다. " + save)
    elif name == "daily_review":
        src, day = args.get("source", ""), args.get("day", "")
        text = (f"talog 하루 검토를 해 주세요 — 출처 '{src or '전체'}', 날짜 '{day or '최근'}'.\n"
                "1. overview 와 list_incidents 로 그날 경보·사건을 훑는다 (심각 먼저).\n"
                "2. 사건마다 get_incident 로 판단(룰·LLM 일치 여부)과 근거를 보고, 오경보 의심·판단 불일치를 짚는다.\n"
                "3. capacity_trend 로 처리 여유가 줄어드는 그룹이 있는지, 다음 정지 전에 거부가 날 위험이 있는지 본다.\n"
                "4. 진단 리포트가 있으면 get_report 로 소견을 맞춰 본다.\n"
                "5. 요약: 오늘의 결론, 위험 순위, 할 일(담당별), 규칙 개선 제안. " + save)
    elif name == "site_health":
        src = args.get("source", "")
        text = (f"talog 현장 점검을 해 주세요 — 출처 '{src or '전체'}'.\n"
                "1. overview 로 날짜·사건·기준선·리포트를 본다.\n"
                "2. capacity_trend 로 날짜별 기준선과 점유·유휴 추세, 재시작으로 기준선이 자주 초기화되는지 본다.\n"
                "3. list_incidents·rediagnose 로 원인 분포와 지금 규칙으로 바뀌는 사건이 있는지 본다.\n"
                "4. 이 현장에 규칙·문턱값이 맞는지(오경보·놓침), 로그 수집에 빠진 파일이 없는지 평가한다.\n"
                "5. 개선 제안을 우선순위로 정리한다. " + save)
    elif name == "log_investigation":
        day, sym = args.get("day", ""), args.get("symptom", "")
        text = (f"talog 원본 로그를 직접 읽어 조사해 주세요 — 일자 폴더 '{day or '최근'}', 증상 '{sym or '그날 가장 심각한 이상'}'.\n"
                "1. log_days·log_files 로 폴더와 파일·시각 범위를 본다 (하루가 수십 GB — 통째로 훑지 않는다).\n"
                "2. 증상 시각 앞뒤로 search_logs(time_from/time_to) 를 핵심 로그에 건다 — 예: NoInspThread, "
                "'Waiting thread count = 0', exception, timeout, CheckFinish done.\n"
                "3. 의미 있는 줄은 read_log(at=시각) 로 앞뒤 문맥을 읽고, 관련 검사는 trace_inspection 으로 처음부터 끝까지 따라간다.\n"
                "4. talog 사건이 있으면 list_incidents·get_incident 와 맞춰 보고, 룰 판단과 원본 로그가 같은 이야기를 하는지 본다.\n"
                "5. 시간순 경과표(시각·파일·사건)·원인·근거 원문(짧게 인용)·조치(runbook 문장 기준)로 정리한다. " + save)
    else:
        raise KeyError(name)
    meta = next(p for p in PROMPTS if p["name"] == name)
    return {"description": meta["description"],
            "messages": [{"role": "user", "content": {"type": "text", "text": text}}]}


def serve_stdio(server: TalogMCP, stdin=None, stdout=None) -> int:
    """줄 단위 JSON-RPC 루프. 표준 출력에는 프로토콜 메시지만 — 다른 출력은 stderr 로 돌린다."""
    inp = stdin or sys.stdin.buffer
    out = stdout or sys.stdout.buffer
    if stdout is None:
        sys.stdout = sys.stderr                          # 엔진 모듈의 print 가 프로토콜을 깨지 않게

    def send(obj):
        out.write(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8") + b"\n")
        out.flush()

    for raw in inp:
        line = raw.decode("utf-8", errors="replace").strip() if isinstance(raw, bytes) else raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        if isinstance(msg, list):                        # 옛 판의 일괄 요청
            res = [r for r in (server.handle(m) for m in msg) if r is not None]
            if res:
                send(res)
            continue
        r = server.handle(msg)
        if r is not None:
            send(r)
    return 0


_KIT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "claude_kit")


def server_command(configs: list[str], work_dir: str = "") -> list[str]:
    """Claude 가 talog MCP 서버를 띄울 명령. 실행 파일이면 talog.exe mcp, 소스면 talog_main.py 를 직접 실행해
    어느 폴더에서 불러도 import 된다 (pip 설치가 없어도 됨)."""
    tail = ["mcp"] + [x for c in configs for x in ("--config", os.path.abspath(c))]
    if work_dir:
        tail += ["--work-dir", os.path.abspath(work_dir)]
    if getattr(sys, "frozen", False):
        return [sys.executable] + tail
    main = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "talog_main.py")
    if os.path.exists(main):
        return [sys.executable, main] + tail
    return [sys.executable, "-m", "talog"] + tail


def init_workspace(target: str, configs: list[str]) -> dict:
    """Claude 데스크톱·Claude Code 에서 이 폴더 하나만 열면 talog RCA 에이전트가 되게 한다.
    CLAUDE.md(역할·일하는 방식·배운 점) · .mcp.json(talog 서버) · RCA 스킬·템플릿 · reports·mcp_jobs 폴더.
    이미 있는 CLAUDE.md 는 덮어쓰지 않는다 — 사용자가 더한 '배운 점'을 지킨다."""
    import shutil
    target = os.path.abspath(target)
    os.makedirs(target, exist_ok=True)
    srcs = [Source.from_config(c) for c in configs]
    made, kept = [], []

    def put(rel: str, text: str, keep: bool = False):
        p = os.path.join(target, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if keep and os.path.exists(p):
            kept.append(rel)
            return
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        made.append(rel)

    with open(os.path.join(_KIT, "CLAUDE.md"), encoding="utf-8") as f:
        md = f.read()
    md = (md.replace("{{SOURCES}}", ", ".join(f"{s.name}({s.kind})" for s in srcs) or "없음")
          .replace("{{CREATED}}", dt.date.today().isoformat()).replace("{{VERSION}}", __version__))
    put("CLAUDE.md", md, keep=True)
    with open(os.path.join(_KIT, "README.md"), encoding="utf-8") as f:
        put("README.md", f.read(), keep=True)
    cmd = server_command(configs, os.path.join(target, "mcp_jobs"))
    put(".mcp.json", json.dumps({"mcpServers": {"talog": {"type": "stdio", "command": cmd[0], "args": cmd[1:]}}},
                                ensure_ascii=False, indent=2) + "\n")
    sk_src = os.path.join(_KIT, "skills", "talog-rca")
    sk_dst = os.path.join(target, ".claude", "skills", "talog-rca")
    if os.path.isdir(sk_dst):
        shutil.rmtree(sk_dst)                     # 스킬은 talog 판에 맞춰 늘 새로 (사용자 수정분은 CLAUDE.md 에)
    shutil.copytree(sk_src, sk_dst)
    made += [os.path.relpath(os.path.join(r, f), target) for r, _d, fs in os.walk(sk_dst) for f in fs]
    for d in ("reports", "mcp_jobs"):
        os.makedirs(os.path.join(target, d), exist_ok=True)
    return {"dir": target, "made": made, "kept": kept, "command": cmd, "sources": [s.name for s in srcs]}


def build(configs: list[str], work_dir: str = "", mask: bool = True, kb_dir: str = "") -> TalogMCP:
    srcs = []
    for c in configs:
        if not os.path.exists(c):
            raise FileNotFoundError(f"설정 파일이 없습니다: {c}")
        srcs.append(Source.from_config(c))
    # 이름이 겹치면 번호를 붙인다
    seen: dict[str, int] = {}
    for s in srcs:
        k = seen.get(s.name, 0)
        seen[s.name] = k + 1
        if k:
            s.name = f"{s.name} ({k + 1})"
    if not work_dir:
        base = srcs[0].data_dir if srcs and srcs[0].data_dir else os.getcwd()
        work_dir = os.path.join(base, "mcp")
    return TalogMCP(srcs, work_dir, mask, kb_dir)


def self_test_report(server: TalogMCP) -> dict:
    """설치 점검 — 실제 클라이언트처럼 초기화·목록·도구 호출을 해 보고 결과를 구조화해 돌려준다.
    콘솔 '연결 시험'은 이 JSON 을 체크리스트·표로 그린다 (마크다운 원문을 그대로 보이지 않게)."""
    seq = [0]

    def call(method, params=None):
        seq[0] += 1
        t0 = time.perf_counter()
        r = server.handle({"jsonrpc": "2.0", "id": seq[0], "method": method, "params": params or {}}) or {}
        return r, round((time.perf_counter() - t0) * 1000)

    checks = []

    def check(key, title, ok, detail, ms=None):
        checks.append({"key": key, "title": title, "ok": bool(ok), "detail": detail, "ms": ms})

    r, ms = call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "talog-self-test", "version": __version__}})
    info = r.get("result") or {}
    si = info.get("serverInfo") or {}
    check("server", "서버 실행", bool(info), f"{si.get('name', '?')} {si.get('version', '')} · MCP {info.get('protocolVersion', '?')}", ms)
    r, _ = call("tools/list")
    tools = (r.get("result") or {}).get("tools") or []
    r, _ = call("prompts/list")
    prompts = (r.get("result") or {}).get("prompts") or []
    check("tools", "도구·프롬프트", len(tools) == len(TOOLS) and len(prompts) == len(PROMPTS),
          f"도구 {len(tools)}개 · 프롬프트 {len(prompts)}개")
    srcs = []
    for s in server.sources:
        incs = s.incidents()
        st, _p = s.capacity_state()
        srcs.append({"name": s.name, "kind": s.kind, "incidents": len(incs),
                     "crit": sum(1 for i in incs if (i.get("verdict") or {}).get("severity") == "crit"),
                     "reports": len(s.reports()), "baseline_groups": len((st or {}).get("groups") or {}),
                     "logs": bool(s.log_root and os.path.isdir(s.log_root))})
    has_data = any(x["incidents"] or x["reports"] for x in srcs)
    check("data", "데이터 출처", bool(srcs) and has_data,
          " · ".join(f"{x['name']} — 사건 {x['incidents']}(심각 {x['crit']}) · 리포트 {x['reports']} · 기준선 "
                     f"{x['baseline_groups']}그룹" for x in srcs) or "출처 없음")
    check("mask", "마스킹", True, ("검사 id → ID-xxxxxx · 경로 → <로그>·<데이터> · 원본 로그 줄은 요청 시만"
                                   if server.mask.on else "꺼짐 — 원본 값이 그대로 Claude 에 갑니다"))
    calls = []
    ok_calls = True
    for name, args in (("overview", {}), ("list_incidents", {"limit": 5})):
        r, ms = call("tools/call", {"name": name, "arguments": args})
        res = r.get("result") or {}
        bad = bool(res.get("isError")) or "error" in r
        ok_calls &= not bad
        calls.append(f"{name} {ms}ms" + (" 실패" if bad else ""))
    check("calls", "도구 호출", ok_calls, " · ".join(calls))
    rows = []
    for s in server.sources:
        for inc in s.incidents():
            v = inc.get("verdict") or {}
            al = inc.get("alerts") or [{}]
            iid = inc.get("id") or ""
            rows.append({"id": iid, "day": f"{iid[4:6]}/{iid[6:8]}", "time": inc.get("opened", ""),
                         "severity": v.get("severity", ""), "title": server.mask.text(al[0].get("title", "")),
                         "more": max(0, len(al) - 1), "cause": (v.get("cause_name") or "").split(" — ")[0],
                         "verdict": v.get("label", ""), "gate": v.get("mode", ""), "source": s.name,
                         "replay": bool(inc.get("_replay"))})
    rows.sort(key=lambda x: x["id"], reverse=True)
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "sources": srcs, "incidents": rows[:8],
            "n_incidents": len(rows), "tools": [t["name"] for t in tools], "prompts": [p["name"] for p in prompts],
            "work_dir": server.work_dir, "masked": server.mask.on}


def self_test(server: TalogMCP, as_json: bool = False) -> int:
    rep = self_test_report(server)
    if as_json:
        print(json.dumps(rep, ensure_ascii=False))
        return 0 if rep["ok"] else 1
    for c in rep["checks"]:
        print(f"[talog mcp] {'✓' if c['ok'] else '✗'} {c['title']}: {c['detail']}")
    print(f"[talog mcp] 도구: {', '.join(rep['tools'])}")
    print(f"[talog mcp] 프롬프트: {', '.join(rep['prompts'])} · 작업 폴더 {rep['work_dir']}")
    for x in rep["incidents"][:5]:
        print(f"    {x['day']} {x['time']}  {x['severity']:<4} {x['title']}  →  {x['cause'] or '-'} · {x['verdict'] or '-'}")
    print(f"[talog mcp] 자가 점검 {'통과' if rep['ok'] else '실패'}")
    return 0 if rep["ok"] else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="talog mcp",
                                 description="talog MCP 서버 — Claude Code·Claude 데스크톱이 talog 산출물을 도구로 읽는다 (stdio)")
    ap.add_argument("--config", action="append", default=[],
                    help="데이터 출처 talog.yaml (현장마다 하나, 여러 번 지정 가능). 없으면 기본 talog.yaml")
    ap.add_argument("--work-dir", default="", help="재현 작업·마스킹 소금 저장 폴더 (기본: 첫 출처 data_dir\\mcp)")
    ap.add_argument("--kb", default="", help="사례 KB 폴더 (기본: talog 의 kb)")
    ap.add_argument("--no-mask", action="store_true", help="마스킹 끄기 (사내 전용 분석에서만)")
    ap.add_argument("--self-test", action="store_true", help="설치 점검만 하고 끝낸다")
    ap.add_argument("--json", action="store_true", help="--self-test 결과를 JSON 으로 (콘솔 연결 시험용)")
    ap.add_argument("--init-workspace", metavar="폴더", default="",
                    help="Claude 데스크톱·Claude Code 에서 열 에이전트 폴더를 만든다 (CLAUDE.md·.mcp.json·RCA 스킬)")
    args = ap.parse_args(argv)
    configs = args.config or [settings.default_path()]
    if args.init_workspace:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        try:
            r = init_workspace(args.init_workspace, configs)
        except (OSError, ValueError) as e:
            print(f"[talog mcp] 에이전트 폴더를 만들지 못했습니다: {e}")
            return 1
        print(f"[talog mcp] 에이전트 폴더: {r['dir']}")
        print(f"  데이터 출처: {', '.join(r['sources'])}")
        print(f"  만든 파일: {', '.join(r['made'])}")
        if r["kept"]:
            print(f"  그대로 둔 파일: {', '.join(r['kept'])} (사용자가 고친 내용 보존)")
        print("  다음: Claude 데스크톱 → Code 탭 → 폴더 선택 → 이 폴더 (또는 이 폴더에서 claude 실행)")
        return 0
    try:
        server = build(configs, args.work_dir, not args.no_mask, args.kb)
    except (OSError, ValueError) as e:
        print(f"[talog mcp] 시작 실패: {e}", file=sys.stderr)
        return 1
    if args.self_test:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        return self_test(server, args.json)
    print(f"[talog mcp] 대기 — 출처 {len(server.sources)}개 ({', '.join(s.name for s in server.sources)}), "
          f"마스킹 {'켬' if server.mask.on else '끔'}", file=sys.stderr)
    return serve_stdio(server)
