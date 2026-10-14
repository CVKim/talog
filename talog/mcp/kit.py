"""Claude 에이전트 폴더 키트 — 이 폴더 하나만 열면 talog RCA 에이전트가 된다.

CLAUDE.md(역할·일하는 방식·배운 점) · .mcp.json(talog 서버) · 작업별 스킬(talog/claude_kit/skills) · reports·mcp_jobs.
스킬 원본은 talog/claude_kit/skills/<이름> 이고 저장소 .claude/skills 에 같은 사본을 둔다(tests/test_mcp.py 가 확인).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys

from .. import __version__
from .sources import Source

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


KIT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "claude_kit")


SKILLS = ("talog-rca", "talog-logs", "talog-daily", "talog-site", "talog-rules")


def server_command(configs: list[str], work_dir: str = "") -> list[str]:
    """Claude 가 talog MCP 서버를 띄울 명령. 실행 파일이면 talog.exe mcp, 소스면 talog_main.py 를 직접 실행해
    어느 폴더에서 불러도 import 된다 (pip 설치가 없어도 됨)."""
    tail = ["mcp"] + [x for c in configs for x in ("--config", os.path.abspath(c))]
    if work_dir:
        tail += ["--work-dir", os.path.abspath(work_dir)]
    if getattr(sys, "frozen", False):
        return [sys.executable] + tail
    main = os.path.join(_REPO, "talog_main.py")
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

    with open(os.path.join(KIT_DIR, "CLAUDE.md"), encoding="utf-8") as f:
        md = f.read()
    md = (md.replace("{{SOURCES}}", ", ".join(f"{s.name}({s.kind})" for s in srcs) or "없음")
          .replace("{{CREATED}}", dt.date.today().isoformat()).replace("{{VERSION}}", __version__))
    put("CLAUDE.md", md, keep=True)
    with open(os.path.join(KIT_DIR, "README.md"), encoding="utf-8") as f:
        put("README.md", f.read(), keep=True)
    cmd = server_command(configs, os.path.join(target, "mcp_jobs"))
    put(".mcp.json", json.dumps({"mcpServers": {"talog": {"type": "stdio", "command": cmd[0], "args": cmd[1:]}}},
                                ensure_ascii=False, indent=2) + "\n")
    for name in SKILLS:
        sk_src = os.path.join(KIT_DIR, "skills", name)
        sk_dst = os.path.join(target, ".claude", "skills", name)
        if os.path.isdir(sk_dst):
            shutil.rmtree(sk_dst)                 # 스킬은 talog 판에 맞춰 늘 새로 (사용자 수정분은 CLAUDE.md 에)
        shutil.copytree(sk_src, sk_dst)
        made += [os.path.relpath(os.path.join(r, f), target) for r, _d, fs in os.walk(sk_dst) for f in fs]
    for d in ("reports", "mcp_jobs"):
        os.makedirs(os.path.join(target, d), exist_ok=True)
    return {"dir": target, "made": made, "kept": kept, "command": cmd, "sources": [s.name for s in srcs]}
