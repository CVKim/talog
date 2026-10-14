# talog

**AI operations platform for talos vision-inspection logs.** talog runs on the inspection PC next to the
talos vision software. It reads the logs in real time, detects abnormal behaviour, decides the most likely
cause, and alerts the right people. The same console builds diagnostic reports for any log folder and lets
Claude investigate incidents and raw logs through your existing Claude subscription.

![version](https://img.shields.io/badge/version-2.2.0-orange)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![platform](https://img.shields.io/badge/platform-Windows-lightgrey)
[![CI](https://github.com/CVKim/talog/actions/workflows/ci.yml/badge.svg)](https://github.com/CVKim/talog/actions)

```
 Collect            Detect               Judge                              Notify            Investigate
 today's log    →   one rule table   →   rule diagnosis ─┬─ agreement ──→   e-mail, toast  →  diagnostic report
 folder, tailed     19 built-in +        (decides)       │  gate             critical first    ask the report (SQL)
 incrementally      defect names,        local LLM ──────┘  agree/disagree   LLM follow-up     Claude: skills, raw logs
 (low load)         log phrases          (cross-checks, CPU/GPU/auto)
```

## Why it works this way

- **Rules decide; the LLM only cross-checks.** A local Qwen model (Ollama) gives a second opinion, and an
  incident is reported as *agreed* only when both name the same cause. Numbers and times in any LLM text are
  checked against the evidence before they reach a person.
- **Capacity headroom (v2.1).** For every Seq group, talog learns a baseline right after start-up (idle threads,
  hold time, arrival interval, pool size) and warns before `NoInspThread` rejections start. Group counts, thread
  counts and tact differ per site and are learned from the logs, not configured.
- **Claude on your subscription, no API keys (v2.1–2.2).** The `talog mcp` server lets Claude Code / Claude
  Desktop read incidents, reports and **raw logs** as tools. The console runs Claude Code headless with the
  subscription login only, and offers one card per task (incident RCA, raw-log investigation, daily review,
  site check, rule comparison). There is no billing path.
- **Field data stays on site.** Customer identifiers and paths are masked before Claude sees them, the console
  only listens on `127.0.0.1`, and analysis output is written to local folders, never to external services.

## Quick start

**On an inspection PC (no Python needed).** Unzip the release and double-click `run_talog.bat`. The console
opens at http://127.0.0.1:8778 and live monitoring starts. Drag a log folder onto `run_talog.bat` to get a
diagnostic report for that folder.

**For development:**

```bash
git clone https://github.com/CVKim/talog.git
cd talog
pip install -e ".[dev]"
python -m talog --config talog.yaml          # console (http://127.0.0.1:8778)
python tools/check.py                        # lint, repo checks, full test suite
```

| Command | What it does |
|---|---|
| `talog` | Console: Operations · Incidents · Analysis · Claude · Settings |
| `talog run` | Headless monitoring (`--check` self-check, `--replay <day folder>` replays a past day) |
| `talog analyze <log folder>` | Diagnostic report (day, machine or multi-machine folders are detected) |
| `talog mcp --self-test` | Check the Claude integration server that Claude Code / Desktop launches |
| `talog mcp --init-workspace <dir>` | Create a Claude agent folder (CLAUDE.md, `.mcp.json`, task skills) |
| `python -m PyInstaller talog.spec --noconfirm` | Build `dist/talog.exe` for field PCs |

All settings live in one `talog.yaml` (about 20 keys: collect, detect, judge, notify) and are edited from the
console's Settings screen. The legacy `watch.yaml` format is still read and is converted on the first save.

## Project layout

```
talog/
  cli.py              command line: console · run · analyze · mcp · _job (console child process)
  settings.py         talog.yaml (user settings) ⇄ engine config
  watch.py            live monitoring: tail → RuleEngine → Notifier → incident agent; replay
  capacity.py         capacity headroom model: per-group baseline, hourly KPIs, persisted state
  agent.py            incident analysis: evidence → rule diagnosis → LLM second opinion → agreement gate
  rules/              events.yaml (log event rules) · runbook.yaml (causes, actions, owners)
  events.py, assemble.py, diagnose.py, report.py, viewer.py, store.py
                      offline pipeline: parse → assemble inspections → diagnose → single-file HTML report
  logread.py          raw log reader: binary search on line timestamps, read/search/trace within budgets
  llm.py, ask.py      Ollama client · natural-language questions over a report database
  mailer.py, graphmail.py, secret.py
                      SMTP / Microsoft 365 Graph / Gmail; secrets stored only as Windows DPAPI ciphertext
  console/            local web console (127.0.0.1)
    app.py            Console = mixins: ops · incidents · analysis · claude · checks
    http.py           declarative GET/POST route tables, token + Host checks
    page.py           assembles talog/web into one offline HTML page
  web/                console sources: console.html shell · views/ · partials/ · css/NN-*.css · js/NN-*.js
  mcp/                Claude MCP server (stdio JSON-RPC, MCP 2025-11-25 and 2026-07-28)
    tools/            one file per domain; each tool is one @tool function
  claude_kit/         Claude agent-folder template: CLAUDE.md, task skills (talog-rca/logs/daily/site/rules)
tests/                pytest suite (174 tests, fake Claude CLI for the console integration)
tools/                check.py (local gates) · make_deploy.py / make_dev_kit.py (field & developer kits)
design-system/talog/  console design system (tokens, components, checklist)
```

## Claude integration

| Piece | Where | Notes |
|---|---|---|
| MCP server | `talog/mcp/` | 18 tools (incidents, capacity trend, reports + read-only SQL, raw logs, replay, runbook, case KB), 5 prompts, incident/report resources, argument completion |
| Task skills | `talog/claude_kit/skills/` (mirrored in `.claude/skills/`) | `/talog-rca`, `/talog-logs`, `/talog-daily`, `/talog-site`, `/talog-rules` |
| Console Claude screen | `talog/console/claude.py`, `talog/web/js/5x-*.js` | Skill cards, streamed answers, tool steps, subscription usage, numbers-vs-evidence check |

Safety rails: masking is on by default (inspection ids → `ID-xxxxxx`, paths → `<로그>`); console runs load only
`Read, Glob, Edit, Write, Skill` built-in tools; writes are limited to the agent folder's `reports/`; tools that
write (`start_replay`, `save_case`) are opened only after explicit user confirmation.

## Validation

Real logs from 5 sites (tyre, ball joint and bushing lines), 90k+ inspections: zero verdict errors in a full
manual audit, 37 rules cross-checked against the talos platform source, a pre-alarm 33 minutes before the
first missed inspection in an incident replay, and rule-level root causes matching the confirmed causes of the
PC3 0727, Tenneco 0730 and c1xx 0729 incidents. The capacity model (v2.1) warned 15.9 hours before the
Pungsan line-2 `NoInspThread` stop of 2026-10-12 and escalated to critical 21 seconds before it.

## Documentation

User and field documentation is written in Korean, the language of the operators.

- [USAGE.md](USAGE.md): getting started, console screens, settings, reading verdicts and reports
- [docs/DEV_PC_MANUAL.md](docs/DEV_PC_MANUAL.md): developer-PC manual, investigating field logs with Claude
- [docs/INTERNALS.md](docs/INTERNALS.md): module internals, settings layers, incident analysis flow, parsing spec
- [design-system/talog/MASTER.md](design-system/talog/MASTER.md): console design system
- [CONTRIBUTING.md](CONTRIBUTING.md): development workflow and conventions (English)
- [CHANGELOG.md](CHANGELOG.md): release history with the evidence behind each change
