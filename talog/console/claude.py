"""Claude 연동 — 구독 Claude Code 를 화면 없이 띄워 talog MCP 도구로 답하게 한다(바로 묻기·스킬 카드·연결 시험).

추가 과금 경로를 만들지 않는다: API 키·게이트웨이·클라우드 과금 변수와 부모 Claude 세션 변수를 빼고 띄우고(claude_env),
기본 도구 풀은 Read·Glob·Edit·Write·Skill 만(--tools), 쓰기는 에이전트 폴더 reports 안만(Edit(reports/**)) 허용한다.
파일을 쓰는 재현(start_replay)은 '규칙 바꾼 뒤 비교' 카드에서 사용자가 확인한 그 질문에만 연다."""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

from .jobs import kill_tree
from .util import REPO, tail2_path


class ClaudeMixin:
    """Console 이 물려받는다 — 공유 상태는 app.Console.__init__ 에 있다."""

    # ── Claude 연동 (talog MCP 서버) ──────────────────────────
    def mcp_command(self) -> list[str]:
        """이 콘솔 설정으로 talog MCP 서버를 띄우는 명령 — Claude Code·데스크톱 설정에 그대로 넣는다."""
        from ..mcp import server_command
        return server_command([self.config_path])

    @property
    def workspace_default(self) -> str:
        return os.path.join(self.data_dir, "claude_agent")

    def claude_workspace(self, target: str = "") -> dict:
        """에이전트 폴더 만들기 — Claude 에서 이 폴더 하나만 열면 talog RCA 에이전트가 된다."""
        from ..mcp import init_workspace
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
        from ..mcp import PROMPTS, TOOLS
        cmd = self.mcp_command()

        def q(x: str) -> str:
            return f'"{x}"' if (not x or any(c in x for c in ' &()')) else x

        server = {"command": cmd[0], "args": cmd[1:]}
        appdata = os.environ.get("APPDATA", "")
        desk = (os.path.join(appdata, "Claude", "claude_desktop_config.json") if appdata
                else r"%APPDATA%\Claude\claude_desktop_config.json")
        incs = self.incidents(days=3650, replay=True, limit=100000)
        crit = [r for r in incs if r.get("severity") == "crit"]
        reps = self.reports()
        ws = self.workspace_default
        skill = os.path.join(ws, ".claude", "skills", "talog-rca", "SKILL.md")   # 에이전트 폴더에 스킬이 깔렸나
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
                           "ask": f"mcp__talog__{t['name']}" in self.ASK_TOOLS.split(","),
                           "card": t["name"] == "start_replay"} for t in TOOLS],
                "prompts": [{"name": x["name"], "title": x.get("title", ""), "description": x["description"]}
                            for x in PROMPTS],
                "data": {"incidents": len(incs), "crit": len(crit), "reports": len(reps),
                         "site": self.settings.get("site") or "이 설비", "data_dir": self.data_dir},
                "models": [{"v": "", "label": "기본 (구독 설정)"}, {"v": "sonnet", "label": "Sonnet — 빠르고 가벼움"},
                           {"v": "opus", "label": "Opus — 깊은 분석"}, {"v": "haiku", "label": "Haiku — 가장 가벼움"}],
                "efforts": [{"v": "", "label": "기본"}, {"v": "low", "label": "낮음"}, {"v": "medium", "label": "보통"},
                            {"v": "high", "label": "높음"}, {"v": "xhigh", "label": "매우 높음"}, {"v": "max", "label": "최대"}],
                "skills": self.claude_skill_cards(incs), "quick": self.claude_quick(reps),
                "logs": self.claude_log_dirs()}

    # ── Claude: 작업별 스킬 카드 ─────────────────────────────────────
    # 카드 하나 = 에이전트 폴더의 스킬 하나. 누르면 필요한 값만 받아 '/스킬 인자'로 부른다(-p 에서도 펼쳐진다 — 2.1.143 실측).
    # 재현(start_replay)은 파일을 쓰므로 '규칙 바꾼 뒤 비교' 카드에서 사용자가 확인한 그 질문에만 허용한다.
    SKILL_CARDS = (
        {"key": "rca", "skill": "talog-rca", "title": "사건 RCA", "icon": "siren",
         "desc": "사건 하나를 근거 → 원인 → 조치 → 보고서까지", "leaves": "RCA 보고서(HTML) · 보통 몇 분",
         "fields": [{"name": "incident", "label": "사건", "kind": "incident",
                     "hint": "비우면 가장 최근 심각 사건을 고릅니다"}]},
        {"key": "logs", "skill": "talog-logs", "title": "원본 로그 조사", "icon": "file-search",
         "desc": "날짜·시각·증상으로 원본 로그를 직접 읽어 시간순 경과와 원인",
         "leaves": "경과표 보고서 · 시각 범위를 좁힐수록 빠름",
         "fields": [{"name": "day", "label": "일자 폴더", "kind": "logday"},
                    {"name": "time", "label": "시각", "kind": "time", "placeholder": "05:02"},
                    {"name": "symptom", "label": "증상", "kind": "text",
                     "placeholder": "NoInspThread · 검사 id · 오류 문구 — 비우면 그날 가장 심각한 이상"}]},
        {"key": "daily", "skill": "talog-daily", "title": "하루 검토", "icon": "list",
         "desc": "하루치 경보·사건·처리 여유를 훑고 놓친 징후·오경보를 할 일로",
         "leaves": "하루 검토 보고서 · 보통 몇 분",
         "fields": [{"name": "day", "label": "날짜", "kind": "day"}]},
        {"key": "site", "skill": "talog-site", "title": "현장 점검·규칙 조정", "icon": "gauge",
         "desc": "기준선·재시작·오경보·놓침을 보고 문턱값 조정안을 우선순위로",
         "leaves": "점검·조정안 보고서 · 설정은 바꾸지 않음",
         "fields": [{"name": "focus", "label": "특히 볼 점", "kind": "text",
                     "placeholder": "예: 그룹 9~12 주의 경보가 잦다 — 비워도 됩니다"}]},
        {"key": "rules", "skill": "talog-rules", "title": "규칙 바꾼 뒤 비교", "icon": "repeat",
         "desc": "저장된 사건을 지금 규칙으로 재진단하고, 허락하면 그날을 재현해 전후 비교",
         "leaves": "전후 비교 보고서 · 재현을 켜면 수십 분",
         "fields": [{"name": "day", "label": "일자 폴더", "kind": "logday", "root_only": True},
                    {"name": "replay", "label": "그날 로그를 지금 규칙으로 재현", "kind": "confirm",
                     "hint": "작업 폴더에 결과 파일을 쓰고 하루치에 수 분~수십 분 걸립니다. 메일은 보내지 않습니다."}]},
    )

    def claude_skill_cards(self, incs: list) -> list[dict]:
        """스킬 카드 + 입력 칸 선택지(최근 사건·로그 폴더·날짜). 스킬 파일이 에이전트 폴더에 없으면 실행 때 만든다."""
        from ..logread import inside
        logs = self.claude_log_dirs()
        root = logs["root"]
        days = []
        for p in logs["days"] + ([logs["selected"]] if logs["selected"] else []):
            if p not in [d["v"] for d in days]:
                days.append({"v": p, "label": tail2_path(p), "in_root": bool(root) and inside(p, root) and
                             os.path.abspath(p) != os.path.abspath(root), "selected": p == logs["selected"]})
        incl, seen = [], set()
        for r in sorted(incs, key=lambda r: r.get("id") or "", reverse=True):
            if r.get("id") and r["id"] not in seen and len(incl) < 40:      # 실시간·재현에 같은 사건이 있으면 하나만
                seen.add(r["id"])
                incl.append({"v": r["id"], "label": f"{r['id'][4:6]}/{r['id'][6:8]} {r.get('time') or ''} · "
                                                    f"{r.get('title', '')}"[:110], "severity": r.get("severity") or ""})
        crit = next((x["v"] for x in incl if x["severity"] == "crit"), "")
        dates = sorted({r["id"][:8] for r in incs if re.match(r"^\d{8}", r.get("id", ""))}, reverse=True)[:30]
        out = []
        for c in self.SKILL_CARDS:
            card = {k: v for k, v in c.items() if k != "fields"}
            card["command"] = "/" + c["skill"]
            fields = []
            for f in c["fields"]:
                f = dict(f)
                if f["kind"] == "incident":
                    f["choices"], f["default"] = incl, crit or (incl[0]["v"] if incl else "")
                elif f["kind"] == "logday":
                    ch = [d for d in days if d["in_root"]] if f.get("root_only") else days
                    f["choices"] = ch
                    f["default"] = next((d["v"] for d in ch if d["selected"]), ch[0]["v"] if ch else "")
                elif f["kind"] == "day":
                    f["choices"] = [{"v": d, "label": f"{d[:4]}-{d[4:6]}-{d[6:]}"} for d in dates]
                    f["default"] = dates[0] if dates else ""
                fields.append(f)
            card["fields"] = fields
            out.append(card)
        return out

    def claude_quick(self, reps: list) -> list[dict]:
        """스킬보다 가벼운 한 줄 질문 (빈 대화 화면의 칩)."""
        site = self.settings.get("site") or "이 설비"
        out = [{"key": "cap", "title": "처리 여유 추세", "icon": "activity",
                "text": f"{site}에서 처리 여유가 가장 많이 줄어든 Seq 그룹 3개를 찾아, 기준선 대비 언제부터 줄었는지와 "
                        f"남은 일시 지연 내성을 시간별 표로 보여 줘"}]
        if reps:
            out.append({"key": "rep", "title": "리포트 교차 확인", "icon": "database",
                        "text": f"talog 리포트 {reps[0]['tag']} 의 자동 진단 소견을 리포트 DB 로 교차 확인해 줘 "
                                f"(거부 직전 분 단위 검사 수·소요·유휴)"})
        return out

    @staticmethod
    def _arg(v, n: int = 160) -> str:
        """스킬 인자 한 칸 — 줄바꿈·제어 문자·앞 '/' 를 지우고 길이를 자른다 (다른 슬래시 명령으로 바뀌지 않게)."""
        s = re.sub(r"[\x00-\x1f\x7f]+", " ", str(v or "")).strip().lstrip("/").strip()
        return s[:n]

    def _skill_request(self, key: str, args: dict, allow_replay: bool) -> tuple[str, str, bool]:
        """카드 입력 → (Claude 에 보낼 '/스킬 인자', 화면에 보일 한 줄, 재현 허용). 잘못된 값은 ValueError."""
        card = next((c for c in self.SKILL_CARDS if c["key"] == key), None)
        if card is None:
            raise ValueError(f"모르는 스킬 카드입니다: {key}")
        a = {k: self._arg(v) for k, v in (args or {}).items() if isinstance(k, str)}
        parts, shown = [], []
        if key == "rca":
            iid = a.get("incident", "")
            if iid and not re.match(r"^[\w\-]{8,80}$", iid):
                raise ValueError("사건 id 형식이 아닙니다")
            parts.append(iid)
            shown.append(iid or "가장 최근 심각 사건")
        elif key in ("logs", "rules"):
            from ..logread import day_date, inside
            day, tm, rel = a.get("day", ""), a.get("time", ""), ""
            if key == "logs" and tm and not re.match(r"^\d{1,2}:\d{2}(:\d{2})?$", tm):
                raise ValueError("시각은 HH:MM 또는 HH:MM:SS 로 넣으십시오")
            if key == "rules" and allow_replay:
                root = self.cfg.get("watch_root", "")
                if not (day and root):
                    raise ValueError("재현할 일자 폴더를 고르십시오 (설정의 로그 루트 아래)")
                if not inside(day, root) or os.path.abspath(day) == os.path.abspath(root):
                    raise ValueError("로그 루트 밖의 폴더는 재현할 수 없습니다 — 설정의 로그 루트 아래 일자 폴더를 고르십시오")
                rel = os.path.relpath(os.path.abspath(day), os.path.abspath(root))
            label = ""
            if day:                                        # 값을 다 확인한 뒤에 고른 폴더로 등록 — MCP 가 읽게, Claude 에게는 '선택'으로만
                r = self.claude_select_log(day)
                if not r.get("ok"):
                    raise ValueError(r.get("error") or "로그 폴더를 고르지 못했습니다")
                label = tail2_path(day)
            elif self._log_selected():
                label = tail2_path(self._log_selected())
            if key == "logs":
                parts += ["일자=선택" if label else "일자=최근", f"시각={tm}" if tm else "",
                          f"증상={a['symptom']}" if a.get("symptom") else ""]
                shown += [label or "최근 폴더", tm, a.get("symptom", "")]
            else:
                d = day_date(day) if day else None         # 재진단은 날짜(YYYYMMDD)로 거른다
                parts += [f"날짜={d:%Y%m%d}" if d else "", f"일자={rel}" if rel else "",
                          "재현 허용" if allow_replay else "재현 안 함"]
                shown += [label or "저장된 사건 전체", "재현 포함" if allow_replay else "재진단만"]
        elif key == "daily":
            d = a.get("day", "").replace("-", "")
            if d and not re.match(r"^\d{8}$", d):
                raise ValueError("날짜는 YYYYMMDD 로 넣으십시오")
            parts.append(d)
            shown.append(f"{d[:4]}-{d[4:6]}-{d[6:]}" if d else "가장 최근 날")
        elif key == "site":
            parts.append(a.get("focus", ""))
            shown.append(a.get("focus", "") or "전체")
        prompt = (f"/{card['skill']} " + " ".join(p for p in parts if p)).strip()
        return prompt, f"{card['title']} · " + " · ".join(s for s in shown if s), (key == "rules" and allow_replay)

    @staticmethod
    def claude_prompt(name: str, args: dict) -> dict:
        """MCP 프롬프트 문장 (사건 화면의 'Claude 로 분석'·'원본 로그로 확인'이 같은 문장을 쓴다)."""
        from ..mcp import PROMPTS, prompt_messages
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
        from .. import logread
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
        from .. import logread
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

    # 기본 도구 풀(--tools) — 셸·웹·하위 에이전트를 아예 싣지 않는다. 시스템 프롬프트가 줄어 구독 사용량도 준다
    # (Write 는 풀에만 있고 허용은 Edit(reports/**) 규칙으로 reports 안만 — 실측 write_scope_probe).
    ASK_BUILTIN = "Read,Glob,Edit,Write,Skill"

    ASK_EVIDENCE_MAX = 1_500_000                    # 수치 대조에 쓸 도구 결과 글자 수 상한

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
                 "mcp__talog__start_replay": "경보 재현 시작",
                 "Skill": "스킬 불러오기", "Read": "파일 읽기", "Glob": "파일 찾기", "Write": "보고서 쓰기",
                 "Edit": "보고서 고치기"}

    @staticmethod
    def ground_check(answer: str, evidence: list[str]) -> dict:
        """답에 적힌 수치·시각이 이번 대화의 도구 결과(와 질문·스킬 본문)에 있는가 — 지어낸 값 찾기.
        경로·코드·사건 id·목록 번호는 수치로 보지 않는다. 결과는 화면의 '수치 근거' 배지가 된다."""
        from ..agent import _TIME_TOK, fact_check
        t = re.sub(r"```.*?```", " ", answer or "", flags=re.S)
        t = re.sub(r"`[^`]*`", " ", t)
        t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)                       # 링크 주소
        t = re.sub(r"[A-Za-z]:[\\/]\S+|\S*[\\/]\S+\.(?:html?|md|json|log|txt|yaml|sqlite)\b", " ", t)
        t = re.sub(r"\S+\.(?:html?|md|json|log|txt|yaml|sqlite)\b", " ", t)
        t = re.sub(r"\b\d{8}-\d{6}-[a-z_]+\b", " ", t)                       # 사건 id
        t = re.sub(r"(?m)^\s*(?:#{1,6}\s+|\d+[.)]\s+|[-*]\s+)", "", t)        # 제목·목록 표시
        t = re.sub(r"(\d+\.\d*?[1-9])0+\b|(\d+)\.0+\b", lambda m: m.group(1) or m.group(2), t)
        fc = fact_check(t, evidence)
        n = fc.get("n_numbers", 0) + len(_TIME_TOK.findall(t))
        return {"ok": fc["ok"], "checked": n, "unsupported": fc.get("unsupported", [])}

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
        """바로 묻기 에이전트 폴더. 없거나 스킬이 talog 판과 다르면(새 스킬·고친 스킬) 연결·스킬만 새로 만든다
        — CLAUDE.md 의 '배운 점'은 그대로 둔다."""
        from ..mcp import KIT_DIR, SKILLS, init_workspace
        ws = self.workspace_default

        def same(name: str) -> bool:
            try:
                with open(os.path.join(KIT_DIR, "skills", name, "SKILL.md"), "rb") as a, \
                        open(os.path.join(ws, ".claude", "skills", name, "SKILL.md"), "rb") as b:
                    return a.read() == b.read()
            except OSError:
                return False
        if not os.path.exists(os.path.join(ws, ".mcp.json")) or not all(same(n) for n in SKILLS):
            init_workspace(ws, [self.config_path])
        return ws

    def _reports_in(self, ws: str) -> dict:
        out = {}
        for p in glob.glob(os.path.join(ws, "reports", "**", "*"), recursive=True):
            if os.path.isfile(p) and p.lower().endswith((".html", ".md")):
                out[p] = os.path.getmtime(p)
        return out

    def claude_ask(self, question: str, model: str = "", effort: str = "", new: bool = False,
                   skill: str = "", args: dict | None = None, allow_replay: bool = False) -> dict:
        """질문(자유 글) 또는 스킬 카드(skill=카드 key, args=입력 칸)를 Claude Code 로 보낸다.
        스킬 카드는 '/스킬 인자'로 보내고, 재현은 '규칙 바꾼 뒤 비교' 카드에서 사용자가 확인했을 때만 허용한다."""
        model, effort = (model or "").strip().lower(), (effort or "").strip().lower()
        if model not in self.ASK_MODELS:
            return {"ok": False, "error": f"모르는 모델입니다: {model}"}
        if effort not in self.ASK_EFFORTS:
            return {"ok": False, "error": f"모르는 effort 입니다: {effort}"}
        cli = self.claude_cli()
        if not cli:
            return {"ok": False, "error": "이 PC 에 Claude Code 가 없습니다 — '복사'로 Claude 데스크톱에 붙여 넣으십시오"}
        if self.ask_job.get("state") == "running":
            return {"ok": False, "error": "Claude 가 앞 질문에 답하는 중입니다 — 끝난 뒤 다시 보내십시오"}
        replay = False
        args = args if isinstance(args, dict) else {}
        if skill:
            self._ask_ws()                                  # 스킬 파일이 있는 에이전트 폴더 (없으면 만든다)
            try:
                prompt, q, replay = self._skill_request(skill, args, bool(allow_replay))
            except ValueError as e:
                return {"ok": False, "error": str(e)}
        else:
            q = (question or "").strip()
            if not q:
                return {"ok": False, "error": "질문이 비었습니다"}
        with self._lock:
            if self.ask_job.get("state") == "running":
                return {"ok": False, "error": "Claude 가 앞 질문에 답하는 중입니다 — 끝난 뒤 다시 보내십시오"}
            ws = self._ask_ws()
            if new or skill or not self.ask_thread.get("session"):
                self.ask_thread = {"session": "", "turns": []}      # 스킬 카드는 늘 새 대화 (한 대화에 한 작업)
            sel = self.claude_log_dirs()["selected"]
            if not skill:
                prompt = (("[이 콘솔에서 분석할 원본 로그 폴더가 선택돼 있다 — log_files·search_logs·read_log·"
                           "trace_inspection 에 day='선택' 으로 읽는다]\n\n" if sel else "") + q)
            turn = {"state": "running", "q": q, "t0": time.time(), "steps": [], "answer": "", "live": "",
                    "reports": [], "error": "", "model_req": model, "effort": effort, "log_selected": bool(sel),
                    "resumed": bool(self.ask_thread.get("session")), "skill": prompt.split(" ")[0] if skill else "",
                    "card": skill, "args": {k: self._arg(v) for k, v in args.items()} if skill else {},
                    "replay": replay}
            self.ask_thread["turns"].append(turn)
            self.ask_job = turn
            resume = self.ask_thread.get("session", "")
        tools = self.ASK_TOOLS + (",mcp__talog__start_replay" if replay else "")
        threading.Thread(target=self._ask_work, args=(cli, ws, prompt, self._reports_in(ws), model, effort, resume, tools),
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
                  resume: str = "", tools: str = ""):
        J, TH = self.ask_job, self.ask_thread
        cmd = [cli, "-p", prompt, "--mcp-config", os.path.join(ws, ".mcp.json"), "--strict-mcp-config",
               "--tools", self.ASK_BUILTIN, "--allowedTools", tools or self.ASK_TOOLS, "--permission-mode", "dontAsk",
               "--max-turns", str(self.ASK_MAX_TURNS), "--output-format", "stream-json", "--verbose",
               "--include-partial-messages", "-n", "talog · " + J["q"][:40]]
        if model:
            cmd += ["--model", model]
            if model == "opus":
                cmd += ["--fallback-model", "sonnet"]   # Opus 가 붐비면 Sonnet 으로 (구독 안, 추가 과금 없음)
        if effort:
            cmd += ["--effort", effort]
        if resume:
            cmd += ["--resume", resume]
        evidence: list[str] = [J["q"]]                # 답의 수치·시각을 대조할 근거 — 도구 결과 원문
        ev_size = [0]

        def keep(text: str):
            if ev_size[0] < self.ASK_EVIDENCE_MAX and text:
                evidence.append(text[: self.ASK_EVIDENCE_MAX - ev_size[0]])
                ev_size[0] += len(evidence[-1])
        if J.get("skill"):                            # 스킬 본문의 문턱값·예시도 근거로 (스킬은 도구 결과가 아니다)
            for f in glob.glob(os.path.join(ws, ".claude", "skills", J["skill"].lstrip("/"), "*.md")) + \
                    glob.glob(os.path.join(ws, ".claude", "skills", "talog-rca", "reference.md")):
                try:
                    with open(f, encoding="utf-8") as fh:
                        keep(fh.read())
                except OSError:
                    pass
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
            kill_tree(p)
        timer = threading.Timer(self.ASK_TIMEOUT_S, _timeout)
        timer.start()
        by_id: dict[str, dict] = {}
        started: dict[str, float] = {}                # 도구 호출 시각(초 단위로 반올림한 t 로 빼면 ms 가 음수가 된다)
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
                        started[c.get("id", "")] = time.time()
                elif t == "user":
                    for c in (d.get("message") or {}).get("content") or []:
                        if not (isinstance(c, dict) and c.get("type") == "tool_result"):
                            continue
                        text = self._result_text(c)
                        if not c.get("is_error"):
                            keep(text)
                        if c.get("tool_use_id") in by_id:
                            st = by_id[c["tool_use_id"]]
                            st.update(done=True, error=bool(c.get("is_error")), preview=text[:900],
                                      ms=round((time.time() - started.get(c["tool_use_id"], time.time())) * 1000))
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
            J["fact"] = self.ground_check(J["answer"], evidence)
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
        kill_tree(p)
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
            p = subprocess.run(cmd, capture_output=True, timeout=120, env=env, cwd=REPO,
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
