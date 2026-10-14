"""콘솔 'Claude 에게 바로 묻기' — 실제 Claude 없이 가짜 CLI 로 실행 경로를 검증한다.

가짜 CLI 는 Claude Code 의 stream-json 출력(초기화·사용량·흐르는 글·블록마다 따로 오는 assistant·도구 결과·결과)을
흉내 내고, 받은 인자·환경을 작업 폴더에 차례로 남긴다.
"""
import json
import os
import re
import subprocess
import sys
import textwrap
import time

import pytest

from talog import settings
from talog.console import Console

FAKE = textwrap.dedent('''
    import json, os, sys, time
    sys.stdout.reconfigure(encoding="utf-8")   # Claude Code 는 항상 UTF-8 로 쓴다
    args = sys.argv[1:]
    q = args[args.index("-p") + 1]
    def out(d):
        print(json.dumps(d, ensure_ascii=False), flush=True)
    keep = {k: v for k, v in os.environ.items() if "CLAUDE" in k.upper() or "ANTHROPIC" in k.upper()}
    calls = json.load(open("_fake_calls.json", encoding="utf-8")) if os.path.exists("_fake_calls.json") else []
    calls.append({"args": args, "env": keep})
    json.dump(calls, open("_fake_calls.json", "w", encoding="utf-8"), ensure_ascii=False)
    sid = args[args.index("--resume") + 1] if "--resume" in args else "sess-%d" % len(calls)
    out({"type": "system", "subtype": "init", "model": "claude-test", "apiKeySource": "none", "session_id": sid,
         "mcp_servers": [{"name": "talog", "status": "connected"}]})
    out({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning", "rateLimitType": "seven_day",
         "utilization": 0.42, "resetsAt": 1792022400, "isUsingOverage": False}})
    if "SLEEP" in q:
        time.sleep(60)
    out({"type": "stream_event", "event": {"type": "message_start"}})
    for piece in ("먼저 사건을 ", "읽겠습니다."):
        out({"type": "stream_event", "event": {"type": "content_block_delta",
             "delta": {"type": "text_delta", "text": piece}}})
    # Claude Code 는 내용 블록마다 assistant 이벤트를 따로 낸다 — 글 블록 다음에 도구 호출 블록
    out({"type": "assistant", "message": {"content": [{"type": "text", "text": "먼저 사건을 읽겠습니다."}]}})
    out({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "tu1", "name": "mcp__talog__get_incident", "input": {"incident_id": "X1"}},
        {"type": "tool_use", "id": "tu2", "name": "TodoWrite", "input": {}}]}})
    time.sleep(0.3)                            # 도구가 0.3초 걸린 셈 — 걸린 ms 가 음수로 나오면 안 된다
    out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tu1",
         "content": [{"type": "text", "text": "# 사건 X1\\n원인: 처리 여유 고갈 · 그룹 9 점유 4.043초 · 05:02:28 거부"}]}]}})
    os.makedirs("reports", exist_ok=True)
    open(os.path.join("reports", "rca_X1.html"), "w", encoding="utf-8").write("<html></html>")
    if "LOGIN" in q:
        out({"type": "result", "subtype": "success", "is_error": True, "num_turns": 1,
             "result": "Invalid API key · Please run /login"})
        sys.exit(1)
    ans = "## 결론\\n- 그룹 9 점유 4.0초 · 05:02 거부 · 9.9배" if q.startswith("/") else "## 결론\\n- 원인 A"
    out({"type": "assistant", "message": {"content": [{"type": "text", "text": ans}]}})
    out({"type": "result", "subtype": "success", "is_error": False, "num_turns": 3, "session_id": sid,
         "result": ans,
         "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "rm"}}]})
''')


@pytest.fixture
def con(tmp_path, monkeypatch):
    fake = tmp_path / "fake_claude.py"
    fake.write_text(FAKE, encoding="utf-8")
    real_popen = subprocess.Popen

    def popen(cmd, *a, **kw):                     # 가짜 CLI 로만 바꿔 띄운다 (taskkill 등은 그대로)
        if isinstance(cmd, list) and cmd and cmd[0] == "FAKE_CLAUDE":
            cmd = [sys.executable, str(fake)] + cmd[1:]
        return real_popen(cmd, *a, **kw)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(Console, "claude_cli", staticmethod(lambda: "FAKE_CLAUDE"))
    monkeypatch.setattr(Console, "_cli_ver", {"FAKE_CLAUDE": "9.9.9"})
    c = Console(os.path.join(str(tmp_path), "talog.yaml"))
    c.settings["data_dir"] = str(tmp_path / "data")
    c.settings["log_root"] = str(tmp_path / "no_logs")
    c.cfg = settings.compile(c.settings)
    ws = c.workspace_default
    os.makedirs(os.path.join(ws, "reports"))
    with open(os.path.join(ws, ".mcp.json"), "w", encoding="utf-8") as f:
        json.dump({"mcpServers": {}}, f)
    return c


def _wait(con, timeout=30):
    t0 = time.time()
    while con.ask_job.get("state") == "running" and time.time() - t0 < timeout:
        time.sleep(0.1)
    return con.ask_job


def _calls(con) -> list:
    with open(os.path.join(con.workspace_default, "_fake_calls.json"), encoding="utf-8") as f:
        return json.load(f)


def test_claude_env_drops_billing_and_parent_session(monkeypatch):
    for k, v in {"ANTHROPIC_API_KEY": "sk-x", "ANTHROPIC_AUTH_TOKEN": "t", "ANTHROPIC_BASE_URL": "http://gw",
                 "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s", "CLAUDE_CODE_USE_BEDROCK": "1",
                 "CLAUDE_AGENT_SDK_VERSION": "1", "CLAUDE_CODE_OAUTH_TOKEN": "oauth", "TALOG_X": "keep"}.items():
        monkeypatch.setenv(k, v)
    env = {k.upper(): v for k, v in Console.claude_env().items()}
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDECODE",
              "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_AGENT_SDK_VERSION"):
        assert k not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth" and env["TALOG_X"] == "keep"


def test_ask_tools_are_read_only_and_writes_stay_in_reports():
    tools = Console.ASK_TOOLS.split(",")
    assert "Edit(reports/**)" in tools and "Write" not in tools and "Bash" not in tools
    assert not {"mcp__talog__start_replay", "mcp__talog__save_case"} & set(tools)
    assert all(t.startswith("mcp__talog__") or t in ("Read", "Glob", "Skill", "Edit(reports/**)") for t in tools)
    for t in ("read_log", "search_logs", "trace_inspection", "log_files", "log_days"):   # 원본 로그는 읽기만
        assert f"mcp__talog__{t}" in tools


def test_ask_runs_clean_and_reports_progress(con, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-ok")
    assert con.claude_ask("사건 X1 분석", model="sonnet") == {"ok": True}
    j = _wait(con)
    assert j["state"] == "done", j
    assert (j["auth"], j["model"], j["mcp"], j["turns"]) == ("none", "claude-test", "connected", 3)
    # 도구를 부르기 전에 쓴 글은 진행 메모, 내부 도구(TodoWrite)는 숨김, 도구 결과는 미리보기로
    assert [(s["kind"], s["label"]) for s in j["steps"]] == [("note", "먼저 사건을 읽겠습니다."), ("tool", "사건 상세")]
    st = j["steps"][1]
    assert st["what"] == "X1" and st["done"] and not st["error"] and 250 <= st["ms"] < 5000
    assert st["preview"].startswith("# 사건 X1") and "처리 여유 고갈" in st["preview"]
    assert j["live"] == "" and j["answer"].startswith("## 결론")      # 흐르던 글은 끝나면 비운다
    assert j["reports"] == [os.path.join("reports", "rca_X1.html")]
    assert j["denied"] == ["Bash"] and j["elapsed_s"] >= 0
    a = _calls(con)[0]["args"]
    assert a[a.index("--allowedTools") + 1] == Console.ASK_TOOLS
    assert a[a.index("--tools") + 1] == Console.ASK_BUILTIN == "Read,Glob,Edit,Write,Skill"   # 셸·웹 없음
    assert "--fallback-model" not in a                               # Sonnet 은 대체 모델이 필요 없다
    assert a[a.index("--permission-mode") + 1] == "dontAsk" and "--strict-mcp-config" in a
    assert j["fact"] == {"ok": True, "checked": 0, "unsupported": []}   # 답에 수치가 없다
    assert a[a.index("--max-turns") + 1] == "60" and a[a.index("--model") + 1] == "sonnet"
    assert "--include-partial-messages" in a and "--resume" not in a and "--effort" not in a
    env = {k.upper() for k in _calls(con)[0]["env"]}
    assert "ANTHROPIC_API_KEY" not in env and "CLAUDECODE" not in env and "CLAUDE_CODE_OAUTH_TOKEN" in env
    st = con.claude_ask_status()
    assert "cli" not in st and st["state"] == "done" and st["can_continue"] and len(st["turns"]) == 1
    u = st["usage"]                                                  # 구독 사용량 (rate_limit_event)
    assert (u["type"], u["utilization"], u["status"], u["overage"]) == ("seven_day", 0.42, "allowed_warning", False)


def test_follow_up_resumes_session_and_new_thread_resets(con):
    assert con.claude_ask("첫 질문", effort="low")["ok"]
    assert _wait(con)["state"] == "done"
    assert con.ask_thread["session"] == "sess-1"
    assert con.claude_ask("이어서 묻기")["ok"]
    j = _wait(con)
    assert j["state"] == "done" and j["resumed"]
    a1, a2 = _calls(con)[0]["args"], _calls(con)[1]["args"]
    assert a1[a1.index("--effort") + 1] == "low" and "--resume" not in a1
    assert a2[a2.index("--resume") + 1] == "sess-1" and "--effort" not in a2
    st = con.claude_ask_status()
    assert [t["q"] for t in st["turns"]] == ["첫 질문", "이어서 묻기"] and st["can_continue"]
    assert con.claude_new_thread() == {"ok": True}
    st = con.claude_ask_status()
    assert st["turns"] == [] and not st["can_continue"] and st["state"] == "idle"
    assert con.claude_ask("새 질문", new=True)["ok"]
    assert not _wait(con)["resumed"]
    assert "--resume" not in _calls(con)[2]["args"]
    assert "모르는 effort" in con.claude_ask("x", effort="turbo")["error"]


def test_ask_validation_busy_and_cancel(con):
    assert not con.claude_ask("  ")["ok"]
    assert "모르는 모델" in con.claude_ask("x", model="gpt-9")["error"]
    assert not con.claude_ask_cancel()["ok"]                        # 진행 중인 질문 없음
    assert con.claude_ask("SLEEP 오래 걸리는 질문")["ok"]
    t0 = time.time()
    while "auth" not in con.ask_job and time.time() - t0 < 20:
        time.sleep(0.1)
    assert "답하는 중" in con.claude_ask("두 번째")["error"]        # 한 번에 하나
    assert not con.claude_new_thread()["ok"]                         # 답하는 중에는 새 대화 불가
    assert con.claude_ask_cancel() == {"ok": True}
    j = _wait(con, timeout=20)
    assert j["state"] == "cancelled" and time.time() - t0 < 25


def test_ask_login_error_is_explained(con):
    assert con.claude_ask("LOGIN 필요")["ok"]
    j = _wait(con)
    assert j["state"] == "failed" and "로그인되어 있지 않습니다" in j["error"]


def test_select_log_folder_and_question_uses_alias(con, tmp_path):
    day = tmp_path / "logs" / "12"
    day.mkdir(parents=True)
    (day / "InspStarter.log").write_text("2026/10/12-05:02:49.421 start\n", encoding="utf-8")
    other = tmp_path / "not_logs"
    other.mkdir()
    assert "talos 로그 폴더가 아닙니다" in con.claude_select_log(str(other))["error"]
    assert not con.claude_select_log(str(tmp_path / "missing"))["ok"]
    r = con.claude_select_log(str(day))
    assert r["ok"] and r["selected"] == str(day) and str(day) in r["days"]
    assert con.claude_info()["logs"]["selected"] == str(day)
    assert con.claude_ask_status()["log_selected"] == str(day)
    assert con.claude_ask("05:02 무렵 원본 로그를 봐 줘")["ok"]
    j = _wait(con)
    assert j["log_selected"] and j["q"] == "05:02 무렵 원본 로그를 봐 줘"      # 화면에는 질문만
    a = _calls(con)[-1]["args"]
    prompt = a[a.index("-p") + 1]
    assert "day='선택'" in prompt and str(day) not in prompt                 # Claude 에게는 경로 대신 '선택'
    r = con.claude_select_log("")
    assert r["ok"] and r["selected"] == "" and con.claude_ask_status()["log_selected"] == ""


def test_claude_file_only_serves_reports(con):
    ws = con.workspace_default
    for rel, body in (("reports/a.html", "<p>a</p>"), ("reports/n.txt", "x"), ("CLAUDE.md", "# rules")):
        with open(os.path.join(ws, rel), "w", encoding="utf-8") as f:
            f.write(body)
    data, ct = con.claude_file("reports/a.html")
    assert data == b"<p>a</p>" and ct.startswith("text/html")
    for bad in ("CLAUDE.md", "reports/../CLAUDE.md", "reports/n.txt", "../talog.yaml", ".mcp.json"):
        with pytest.raises(FileNotFoundError):
            con.claude_file(bad)


def test_claude_info_lists_skills_models_and_efforts(con):
    info = con.claude_info()
    assert info["cli"] == "FAKE_CLAUDE" and info["cli_version"] == "9.9.9"
    assert info["ask"]["state"] == "idle" and len(info["tools"]) == 18
    assert {t["name"] for t in info["tools"] if not t["ask"]} == {"start_replay", "save_case"}   # 바로 묻기 기본 목록 밖
    assert [t["name"] for t in info["tools"] if t["card"]] == ["start_replay"]                 # 비교 카드에서 확인 후
    assert [m["v"] for m in info["models"]] == ["", "sonnet", "opus", "haiku"]
    assert tuple(e["v"] for e in info["efforts"]) == Console.ASK_EFFORTS
    from talog.mcp_server import SKILLS
    sk = info["skills"]
    assert [k["key"] for k in sk] == ["rca", "logs", "daily", "site", "rules"]
    assert [k["command"] for k in sk] == ["/" + n for n in SKILLS]
    kinds = {k["key"]: [f["kind"] for f in k["fields"]] for k in sk}
    assert kinds["rca"] == ["incident"] and kinds["logs"] == ["logday", "time", "text"] and kinds["rules"][-1] == "confirm"
    assert [q["key"] for q in info["quick"]] == ["cap"]                # 리포트가 없으면 교차 확인 칩도 없다
    assert set(info["logs"]) == {"root", "days", "selected"}


def test_claude_prompt_and_incident_log_folder(con, tmp_path):
    r = con.claude_prompt("log_investigation", {"day": "선택", "symptom": "NoInspThread"})
    assert r["ok"] and "'선택'" in r["text"] and "NoInspThread" in r["text"]
    assert not con.claude_prompt("nope", {})["ok"]
    root = tmp_path / "talos"
    day = root / "2026_10" / "12"
    day.mkdir(parents=True)
    (day / "comm.log").write_text("x", encoding="utf-8")
    con.settings["log_root"] = str(root)
    con.cfg = settings.compile(con.settings)
    os.makedirs(con.data_dir, exist_ok=True)
    with open(os.path.join(con.data_dir, "incidents_20261012_replay.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"id": "20261012-050228-capacity_drift", "alerts": []}) + "\n")
    inc = con.incident("20261012-050228-capacity_drift", True)          # 재현 사건도 원본 로그 폴더를 찾는다
    assert inc["day_dir"] == str(day)
    with open(os.path.join(con.data_dir, "incidents_20261013_replay.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"id": "20261013-010101-x", "alerts": []}) + "\n")
    assert con.incident("20261013-010101-x", True)["day_dir"] == ""      # 그날 폴더가 없으면 빈 값


def test_timeline_counts_inspections_once_and_keeps_last_groups(con):
    d = con.data_dir
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "alerts_20261012_replay.jsonl"), "w", encoding="utf-8") as f:
        for ts, sev in (("2026-10-12 05:02:49", "crit"), ("2026-10-12 05:03:00", "warn"),
                        ("2026-10-12 04:10:00", "warn"), ("bad", "crit")):
            f.write(json.dumps({"ts": ts, "severity": sev}) + "\n")
    rows = [{"hour": "2026-10-12 04:00", "group": g, "n": 2387, "pool": 6, "interval_s": 1.5,
             "hold_p99": 4.0, "rejects": 0} for g in (1, 2)]
    rows += [{"hour": "2026-10-12 05:00", "group": 2, "n": 113, "pool": 6, "interval_s": 1.5,
              "hold_p99": 4.0, "rejects": 0},
             {"hour": "2026-10-12 05:00", "group": 1, "n": 113, "pool": 6, "interval_s": 1.5,
              "hold_p99": 9.1, "hold_p50": 5.5, "idle_median": 3, "rejects": 1},
             {"hour": "2026-10-12 05:00", "group": "process", "mem_mb_max": 1}]
    with open(os.path.join(d, "kpi_20261012_replay.jsonl"), "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r) for r in rows) + "\n")
    t = con.timeline(7)
    r = next(x for x in t["days"] if x["day"] == "20261012" and x["replay"])
    assert r["alerts"][5] == [1, 1, 0] and r["alerts"][4] == [0, 1, 0]
    assert r["n"][4] == 2387 and r["groups"][4] == 2                  # 한 검사가 모든 그룹을 지나므로 합이 아니다
    assert r["n"][6] is None and r["rejects"][5] == 1
    assert r["tol_pct"][4] == round(100 * (9.0 - 4.0) / 9.0, 1)
    assert r["tol_pct"][5] == round(100 * (9.0 - 9.1) / 9.0, 1)       # 그룹 중 가장 낮은 값
    assert r["last"]["hour"] == 5 and [g["group"] for g in r["last"]["groups"]] == [1, 2]
    assert r["last"]["groups"][0]["hold_p99"] == 9.1
    assert any(x["day"] == t["today"] and not x["replay"] for x in t["days"])   # 오늘 줄은 기록이 없어도 있다


def test_console_page_has_core_controls():
    from talog import console
    page = open(console._PAGE, encoding="utf-8").read()
    for i in ("btnCa", "btnCaStop", "caModel", "caEffort", "caThread", "caCtx", "btnNewThread", "logSel",
              "btnLogSel", "aiUsage", "aiSkills", "opsStrip", "opsTol", "capBullets", "incSearch", "cmdk",
              "saveBar", "btnSaveGo", "dlg", "btnTheme", "main"):
        assert f'id="{i}"' in page, i
    assert page.count("__TALOG_TOKEN__") == 1
    # 이모지·기호를 아이콘으로 쓰지 않는다 (SVG 한 벌)
    assert not re.search("[\U0001F300-\U0001FAFF✦⚑◉▤⚙]", page)
    defined = set(re.findall(r'<symbol id="i-([a-z-]+)"', page))
    used = set(re.findall(r'#i-([a-z-]+)', page)) | set(re.findall(r'ic\("([a-z-]+)"', page))
    tool_ic = re.search(r"const TOOL_IC = \{(.*?)\};", page, re.S).group(1)
    used |= set(re.findall(r':\s*"([a-z-]+)"', tool_ic))
    assert used <= defined, used - defined


def test_skill_icons_exist_in_page(con):
    from talog import console
    page = open(console._PAGE, encoding="utf-8").read()
    defined = set(re.findall(r'<symbol id="i-([a-z-]+)"', page))
    info = con.claude_info()
    assert {a["icon"] for a in info["skills"] + info["quick"]} <= defined
    for i in ("skDlg", "skFields", "skOk", "skNo", "aiSkills", "aiTools"):
        assert f'id="{i}"' in page, i


def _incidents(con, day="20261012"):
    os.makedirs(con.data_dir, exist_ok=True)
    with open(os.path.join(con.data_dir, f"incidents_{day}_replay.jsonl"), "w", encoding="utf-8") as f:
        for iid, sev in ((f"{day}-040101-capacity_drift", "warn"), (f"{day}-050228-capacity_drift", "crit")):
            f.write(json.dumps({"id": iid, "opened": iid[9:11] + ":" + iid[11:13], "alerts": [{"title": "t"}],
                                "verdict": {"severity": sev}}) + "\n")


def test_skill_card_runs_slash_command_in_new_thread(con):
    _incidents(con)
    rca = con.claude_info()["skills"][0]
    assert rca["fields"][0]["default"] == "20261012-050228-capacity_drift"     # 기본 = 가장 최근 심각 사건
    assert con.claude_ask("앞 질문")["ok"] and _wait(con)["state"] == "done"
    assert con.claude_ask("", skill="rca", args={"incident": "20261012-050228-capacity_drift"})["ok"]
    j = _wait(con)
    assert j["state"] == "done" and not j["resumed"] and j["skill"] == "/talog-rca" and j["card"] == "rca"
    assert j["q"] == "사건 RCA · 20261012-050228-capacity_drift"            # 화면에는 한 줄 요약
    a = _calls(con)[-1]["args"]
    assert a[a.index("-p") + 1] == "/talog-rca 20261012-050228-capacity_drift" and "--resume" not in a
    assert "mcp__talog__start_replay" not in a[a.index("--allowedTools") + 1]
    # 스킬 파일이 에이전트 폴더에 깔린다 (고친 스킬도 다시 깔린다)
    from talog.mcp_server import SKILLS
    for n in SKILLS:
        assert os.path.exists(os.path.join(con.workspace_default, ".claude", "skills", n, "SKILL.md")), n
    # 답의 수치 대조 — 4.0(4.043)·05:02 는 도구 결과에 있고 9.9 는 없다
    assert j["fact"]["checked"] >= 3 and j["fact"]["unsupported"] == ["9.9"] and not j["fact"]["ok"]
    st = con.claude_ask_status()
    assert len(st["turns"]) == 1 and st["turns"][0]["args"] == {"incident": "20261012-050228-capacity_drift"}


def test_skill_card_validation(con):
    assert "모르는 스킬 카드" in con.claude_ask("", skill="nope")["error"]
    assert "사건 id 형식" in con.claude_ask("", skill="rca", args={"incident": "x y; rm"})["error"]
    assert "HH:MM" in con.claude_ask("", skill="logs", args={"time": "5시"})["error"]
    assert "YYYYMMDD" in con.claude_ask("", skill="daily", args={"day": "어제"})["error"]
    assert con.ask_job.get("state") != "running"
    # 줄바꿈·앞 '/' 는 지워 다른 슬래시 명령으로 바뀌지 않는다
    assert con.claude_ask("", skill="site", args={"focus": "/clear\n그룹 9"})["ok"]
    _wait(con)
    a = _calls(con)[-1]["args"]
    assert a[a.index("-p") + 1] == "/talog-site clear 그룹 9"


def test_logs_card_selects_folder_and_uses_alias(con, tmp_path):
    day = tmp_path / "talos" / "2026_10" / "12"
    day.mkdir(parents=True)
    (day / "InspStarter.log").write_text("2026/10/12-05:02:49.421 start\n", encoding="utf-8")
    assert con.claude_ask("", skill="logs", args={"day": str(day), "time": "05:02", "symptom": "NoInspThread"})["ok"]
    j = _wait(con)
    a = _calls(con)[-1]["args"]
    prompt = a[a.index("-p") + 1]
    assert prompt == "/talog-logs 일자=선택 시각=05:02 증상=NoInspThread" and str(day) not in prompt
    assert con.claude_ask_status()["log_selected"] == str(day) and "2026_10\\12" in j["q"]


def test_rules_card_opens_replay_only_when_confirmed(con, tmp_path):
    root = tmp_path / "talos"
    day = root / "2026_10" / "12"
    day.mkdir(parents=True)
    (day / "comm.log").write_text("2026/10/12-05:02:49.421 x\n", encoding="utf-8")
    other = tmp_path / "elsewhere" / "12"
    other.mkdir(parents=True)
    (other / "comm.log").write_text("x\n", encoding="utf-8")
    con.settings["log_root"] = str(root)
    con.cfg = settings.compile(con.settings)
    card = next(k for k in con.claude_info()["skills"] if k["key"] == "rules")
    assert [d["v"] for d in card["fields"][0]["choices"]] == [str(day)]       # 재현은 로그 루트 아래만
    assert con.claude_ask("", skill="rules", args={"day": str(day)})["ok"]   # 확인 안 함 → 재진단만
    j = _wait(con)
    a = _calls(con)[-1]["args"]
    assert "mcp__talog__start_replay" not in a[a.index("--allowedTools") + 1] and not j["replay"]
    assert a[a.index("-p") + 1] == "/talog-rules 날짜=20261012 재현 안 함"
    assert "로그 루트 밖" in con.claude_ask("", skill="rules", args={"day": str(other)}, allow_replay=True)["error"]
    assert con.claude_ask("", skill="rules", args={"day": str(day)}, allow_replay=True, model="opus")["ok"]
    j = _wait(con)
    a = _calls(con)[-1]["args"]
    assert a[a.index("--allowedTools") + 1] == Console.ASK_TOOLS + ",mcp__talog__start_replay" and j["replay"]
    assert a[a.index("-p") + 1] == f"/talog-rules 날짜=20261012 일자={os.path.join('2026_10', '12')} 재현 허용"
    assert a[a.index("--fallback-model") + 1] == "sonnet"


def test_ground_check_ignores_paths_ids_and_list_numbers():
    ev = ["그룹 9 점유 4.043초, 유휴 3, 05:02:28 거부, 내성 0.332"]
    ans = ("1. 그룹 9 점유 4.04초 (05:02)\n2. 내성 33.2%\n- 보고서 `reports\\풍산_20261012\\rca_x.html`\n"
           "- 사건 20261012-050228-capacity_drift · 13:10 재시작 · 7.7배")
    r = Console.ground_check(ans, ev)
    assert set(r["unsupported"]) == {"13:10", "7.7"} and not r["ok"]
    assert Console.ground_check("근거 없음", ev) == {"ok": True, "checked": 0, "unsupported": []}
