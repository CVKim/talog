"""talog MCP 서버 핵심 — 출처 고르기·마스킹·재현 작업·JSON-RPC·자원·자동완성. 도구 구현은 tools/ 에 있다.

전송: MCP stdio — 줄 단위 JSON-RPC 2.0(UTF-8). 표준 출력에는 프로토콜 메시지만 쓰고 나머지는 stderr.
두 시대를 함께 받는다: 옛 판(2025-11-25 이전)은 initialize 핸드셰이크, 최신 판(2026-07-28)은 요청마다 params._meta.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import sqlite3
import sys

from .. import __version__, logread
from . import registry
from . import tools
from .fmt import SEV_KO
from .masking import Masker
from .prompts import GUIDE, INSTRUCTIONS, PROMPTS, prompt_messages
from .registry import TOOLS
from .sources import Jobs, Source

SERVER_NAME = "talog"
TOOL_MODULES = tools.__all__     # import 하면 도구가 registry 에 등록된다 (incidents·reports·…)
# 최신(요청마다 _meta 로 판을 알리는 무상태 방식)과 옛 판(initialize 핸드셰이크)을 함께 받는다 — stdio 하나로 두 시대 모두
_MODERN_VERSIONS = ("2026-07-28",)
_LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
_KNOWN_VERSIONS = _MODERN_VERSIONS + _LEGACY_VERSIONS
_MV = "io.modelcontextprotocol/protocolVersion"
_LIST_TTL_MS = {"tools/list": 3_600_000, "prompts/list": 3_600_000, "resources/templates/list": 3_600_000,
                "server/discover": 3_600_000, "resources/list": 60_000, "resources/read": 30_000}
_DAY_RE = re.compile(r"^(\d{8})$")


class TalogMCP:
    """MCP 서버 하나 = 출처(현장) 여럿 + 작업 폴더(재현 작업·마스킹 소금·콘솔이 고른 로그 폴더).

    tools/ 의 도구 함수가 쓰는 공개 API:
      속성  sources · mask(Masker) · jobs(Jobs) · kb_dir · work_dir
      출처  all_sources() · pick(name, need_logs) · one(name, need_logs)
      인자  day_arg(day) → YYYYMMDD · day_dir(args) → 원본 로그 일자 폴더 · log_roots() · log_cfg() · extra_log_roots()
    """

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

    def one(self, name: str, need_logs: bool = False) -> Source:
        c = self.pick(name, need_logs)
        if not c:
            raise ValueError(f"출처를 찾지 못했습니다: '{name}' — overview 의 '출처' 이름을 쓰십시오")
        if len(c) > 1 and name:
            raise ValueError("출처가 여럿 맞습니다: " + ", ".join(s.name for s in c) + " — 더 구체적으로 쓰십시오")
        return c[0]

    # ── 원본 로그 (logread) ──────────────────────────────────────
    def extra_log_roots(self) -> list[str]:
        """콘솔에서 고른 로그 폴더 (작업 폴더의 log_roots.json) — 설정의 로그 루트 밖이어도 읽을 수 있게 한다."""
        cfg = self.log_cfg()
        items = list(cfg.get("roots") or []) + ([cfg["selected"]] if cfg.get("selected") else [])
        return [p for p in items if isinstance(p, str) and os.path.isdir(p)]

    def log_roots(self, source: str = "") -> list[str]:
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

    # ── 공용 ────────────────────────────────────────────────────
    @staticmethod
    def day_arg(day: str) -> str:
        day = (day or "").strip().replace("-", "").replace("/", "")
        if not day:
            return ""
        if re.match(r"^\d{4}$", day):                       # MMDD → 올해
            day = f"{dt.date.today().year}{day}"
        if not _DAY_RE.match(day):
            raise ValueError("day 는 YYYYMMDD 형식입니다 (예: 20261012)")
        return day

    def call_tool(self, name: str, args: dict) -> str:
        """도구 하나 실행 (registry 의 @tool 함수). 새로 만든 ID 토큰 대응표는 다음 대화를 위해 바로 저장한다."""
        if name not in registry.names():
            raise KeyError(name)
        try:
            return registry.call(self, name, args or {})
        finally:
            self.mask.save()

    def get_prompt(self, name: str, args: dict) -> dict:
        return prompt_messages(name, args)

    # 자원 — 런북·용어 + 최근 사건·리포트. Claude Code 에서 '@talog:talog://incident/<id>' 로 대화에 바로 붙인다
    RESOURCE_TEMPLATES = [
        {"uriTemplate": "talog://incident/{incident_id}", "name": "incident", "title": "talog 사건",
         "mimeType": "text/markdown", "description": "사건 하나의 판단·룰 근거·경보·근거 데이터 (get_incident 와 같은 내용, 마스킹)"},
        {"uriTemplate": "talog://report/{tag}", "name": "report", "title": "talog 진단 리포트 소견",
         "mimeType": "text/markdown", "description": "리포트 하나의 자동 진단 소견과 DB 표 (get_report 와 같은 내용)"}]

    def resources(self) -> list[dict]:
        out = [{"uri": "talog://runbook", "name": "runbook", "title": "talog 런북 (원인·조치 사전 원문)",
                "mimeType": "text/yaml", "description": "rules/runbook.yaml"},
               {"uri": "talog://guide", "name": "guide", "title": "talog 용어 요약",
                "mimeType": "text/markdown", "description": "유휴 스레드·점유·한계·내성·기준선·원인 이름"}]
        seen = set()
        for i in sorted((i for s in self.all_sources() for i in s.incidents()), key=lambda i: i.get("id") or "",
                        reverse=True):
            iid = i.get("id") or ""
            if not iid or iid in seen or len(seen) >= 20:
                continue
            seen.add(iid)
            v = i.get("verdict") or {}
            out.append({"uri": f"talog://incident/{iid}", "name": iid, "mimeType": "text/markdown",
                        "title": self.mask.text(f"{iid[4:6]}/{iid[6:8]} {i.get('opened', '')} "
                                                f"{((i.get('alerts') or [{}])[0]).get('title', '')}")[:120],
                        "description": f"{SEV_KO.get(v.get('severity'), '-')} · "
                                       f"{(v.get('cause_name') or '-').split(' — ')[0]} · {v.get('label') or '-'}"})
        for s in self.sources:
            for r in s.reports()[-10:]:
                out.append({"uri": f"talog://report/{r['tag']}", "name": r["tag"], "mimeType": "text/markdown",
                            "title": f"진단 리포트 {r['tag']}", "description": " / ".join(r["findings"][:2])[:200]})
        return out

    def read_resource(self, uri: str) -> dict:
        if uri == "talog://runbook":
            from ..agent import _RUNBOOK_PATH
            with open(_RUNBOOK_PATH, encoding="utf-8") as f:
                return {"uri": uri, "mimeType": "text/yaml", "text": f.read()}
        if uri == "talog://guide":
            return {"uri": uri, "mimeType": "text/markdown", "text": GUIDE}
        m = re.match(r"^talog://(incident|report)/(.+)$", uri or "")
        if m:
            kind, key = m.group(1), m.group(2)
            try:
                text = self.call_tool("get_incident" if kind == "incident" else "get_report",
                                      {"incident_id": key} if kind == "incident" else {"tag": key})
            except ValueError as e:
                raise KeyError(f"{uri} — {e}") from None
            finally:
                self.mask.save()
            return {"uri": uri, "mimeType": "text/markdown", "text": text}
        raise KeyError(uri)

    # 인자 자동완성 — 프롬프트(/mcp__talog__incident_rca 의 사건 id 등)와 자원 템플릿
    _SYMPTOMS = ("NoInspThread", "Waiting thread count = 0", "exception", "timeout", "CheckFinish done", "INSPECT_END",
                 "enqueueV3", "INFER_ERROR")

    def complete(self, ref: dict, arg: dict) -> list[str]:
        name, val = str((arg or {}).get("name") or ""), str((arg or {}).get("value") or "").strip().lower()
        if name == "incident_id":
            cands = sorted({i.get("id") for s in self.all_sources() for i in s.incidents() if i.get("id")}, reverse=True)
        elif name == "source":
            cands = [s.name for s in self.all_sources()]
        elif name == "tag":
            cands = [r["tag"] for s in self.sources for r in s.reports()]
        elif name == "symptom":
            cands = list(self._SYMPTOMS)
        elif name == "day" and (ref or {}).get("name") == "log_investigation":
            try:
                roots = self.log_roots()
            except ValueError:
                return []
            cands = (["선택"] if self.log_cfg().get("selected") else []) + [
                next((os.path.relpath(d, r) for r in roots if logread.inside(d, r) and d != r), d)
                for d in logread.day_dirs(roots, 30)]
        elif name == "day":
            cands = sorted({(i.get("id") or "")[:8] for s in self.all_sources() for i in s.incidents()
                            if re.match(r"^\d{8}", i.get("id") or "")} | set(k for s in self.sources for k in s.kpi_days()),
                           reverse=True)
        else:
            return []
        hit = [c for c in cands if c.lower().startswith(val)]
        return hit + [c for c in cands if val and val in c.lower() and c not in hit]

    # ── 원본 로그 폴더 설정 (콘솔이 고른 폴더) ─────────────────────
    def log_cfg(self) -> dict:
        try:
            with open(os.path.join(self.work_dir, "log_roots.json"), encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return {}
        return d if isinstance(d, dict) else {"roots": [x for x in d if isinstance(x, str)]} if isinstance(d, list) else {}

    def day_dir(self, a: dict) -> str:
        """일자 폴더 인자 → 경로. '선택'(또는 비움 + 콘솔에서 고른 폴더가 있으면) = 콘솔에서 고른 폴더."""
        roots = self.log_roots(a.get("source", ""))
        day = str(a.get("day") or "").strip()
        sel = self.log_cfg().get("selected", "")
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
    CAPABILITIES = {"tools": {"listChanged": False}, "prompts": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False}, "completions": {}}
    SERVER_INFO = {"name": SERVER_NAME, "title": "talog — AI 비전 로그 분석", "version": __version__}

    def handle(self, msg: dict) -> dict | None:
        """요청 하나 → 응답(알림이면 None).

        두 시대를 함께 받는다: 옛 판(2025-11-25 이전)은 initialize 핸드셰이크로, 최신 판(2026-07-28)은 요청마다
        params._meta 에 판·클라이언트를 싣는다. 최신 요청에는 resultType·serverInfo(_meta)·캐시 힌트(ttlMs)를 붙이고,
        모르는 판이면 UnsupportedProtocolVersionError(-32022)로 지원 판을 알린다."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        method, mid = msg.get("method"), msg.get("id")
        if method is None:
            return None                                  # 클라이언트가 보낸 응답 — 이 서버는 요청을 보내지 않는다
        if "id" not in msg:
            return None                                  # 알림 (initialized, cancelled …)
        p = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        meta = p.get("_meta") if isinstance(p.get("_meta"), dict) else {}
        mver = str(meta.get(_MV) or "")
        if mver and mver not in _KNOWN_VERSIONS:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32022, "message": "Unsupported protocol version",
                                                          "data": {"supported": list(_KNOWN_VERSIONS), "requested": mver}}}

        def err(code: int, message: str) -> dict:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}
        try:
            if method == "initialize":                   # 옛 판 핸드셰이크 — 아는 판이면 그 판, 아니면 아는 가장 새 옛 판
                req = str(p.get("protocolVersion") or "")
                ver = req if req in _LEGACY_VERSIONS else _LEGACY_VERSIONS[0]
                self.client = p.get("clientInfo") or {}
                res = {"protocolVersion": ver, "capabilities": dict(self.CAPABILITIES, logging={}),
                       "serverInfo": self.SERVER_INFO, "instructions": INSTRUCTIONS}
            elif method == "server/discover":
                res = {"supportedVersions": list(_KNOWN_VERSIONS), "capabilities": self.CAPABILITIES,
                       "instructions": INSTRUCTIONS}
            elif method == "ping":
                res = {}
            elif method == "tools/list":
                res = {"tools": TOOLS}
            elif method == "tools/call":
                name = p.get("name", "")
                if name not in registry.names():
                    return err(-32602, f"Unknown tool: {name}")
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
                res = {"resourceTemplates": self.RESOURCE_TEMPLATES}
            elif method == "resources/read":
                res = {"contents": [self.read_resource(p.get("uri", ""))]}
            elif method == "logging/setLevel":            # 옛 판 호환 (2026-07-28 에서 폐지 예정)
                res = {}
            elif method == "completion/complete":
                vals = self.complete(p.get("ref") or {}, p.get("argument") or {})
                res = {"completion": {"values": vals[:100], "total": len(vals), "hasMore": len(vals) > 100}}
            else:
                return err(-32601, f"Method not found: {method}")
        except KeyError as e:
            return err(-32602, f"Invalid params: {e}")
        except Exception as e:                           # 서버는 죽지 않는다
            return err(-32603, f"{type(e).__name__}: {e}")
        if mver or method == "server/discover":          # 최신 판 결과 모양
            res = dict(res, resultType="complete")
            res["_meta"] = dict(res.get("_meta") or {}, **{"io.modelcontextprotocol/serverInfo": self.SERVER_INFO})
            if method in _LIST_TTL_MS:
                res.update(ttlMs=_LIST_TTL_MS[method], cacheScope="private")
        return {"jsonrpc": "2.0", "id": mid, "result": res}


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
