# talog

**AI operations platform for talos vision-inspection logs.** talog runs on the inspection PC next to the
talos vision software. It reads the logs in real time, detects abnormal behaviour, decides the most likely
cause, and alerts the right people. The same console builds diagnostic reports for any log folder. On a
developer PC it also lets Claude investigate incidents and raw logs through your existing Claude subscription.

![version](https://img.shields.io/badge/version-2.3.0-orange)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![platform](https://img.shields.io/badge/platform-Windows-lightgrey)

```
 Collect            Detect               Judge                              Notify            Investigate
 today's log    →   one rule table   →   rule diagnosis ─┬─ agreement ──→   e-mail, toast  →  diagnostic report
 folder, tailed     19 built-in +        (decides)       │  gate             critical first    ask the report (SQL)
 incrementally      defect names,        local LLM ──────┘  agree/disagree   LLM follow-up     Claude (developer mode)
 (low load)         log phrases          (cross-checks, CPU/GPU/auto)
```

## Why it works this way

- **An agent you can watch and teach (v2.3).** The Operations screen opens with an agent briefing (what happened
  since you last looked, today's work, the tightest capacity group); every incident shows the agent's step-by-step
  trace; people close incidents as resolved or false alarm, and resolutions become cases the agent attaches to the
  next similar incident. Rule feedback counts false alarms per rule. All text is built from recorded data.
- **Rules decide; the LLM only cross-checks.** A local Qwen model (Ollama) gives a second opinion, and an
  incident is reported as *agreed* only when both name the same cause. Numbers and times in any LLM text are
  checked against the evidence before they reach a person.
- **Capacity headroom (v2.1).** For every Seq group, talog learns a baseline right after start-up (idle threads,
  hold time, arrival interval, pool size) and warns before `NoInspThread` rejections start. Group counts, thread
  counts and tact differ per site and are learned from the logs, not configured.
- **Three modes, switched at the top left (v2.2).** Field, operator and developer modes open different
  capabilities in the same program, so field users cannot change detection settings or reach Claude by accident.
- **Claude on your subscription, no API keys (v2.1–2.2).** In developer mode the `talog mcp` server lets
  Claude Code / Claude Desktop read incidents, reports and **raw logs** as tools, and the console runs Claude Code
  headless with the subscription login only. There is no billing path.
- **Field data stays on site.** Customer identifiers and paths are masked before Claude sees them, the console
  only listens on `127.0.0.1`, and analysis output is written to local folders, never to external services.

## Three modes: field, operator, developer

Pick the mode in the switch at the top left of the console (or on the Settings screen on a phone). Going **up**
asks for the fixed company PIN (only its PBKDF2 hash is in the code); going down does not. The chosen mode is
saved as `mode` in `talog.yaml`, so the console reopens in it. `talog --mode <mode>` overrides it for one run.

| Mode | Who | What it opens |
|---|---|---|
| Field (`field`, default) | Line staff on the inspection PC | Operations, incidents, analysis (build and ask reports); settings are read-only |
| Operator (`operator`) | The engineer who runs talog on site | + save settings, tests (LLM, mail, rules, test alert), stop monitoring, alert replay |
| Developer (`developer`) | Developers with copies of field logs | + Claude: screen, skill cards, "Ask Claude" buttons (subscription login) |

The server enforces the same rules: routes are wrapped in `oper(…)` or `dev(…)` and answer `403` below that mode.
Live monitoring starts with the console in every mode (`--no-watch` to open without it). The field kit writes
`mode: field`, the developer kit `mode: developer`.

## Quick start

**On an inspection PC (no Python needed).** Unzip the field kit and double-click `run_talog.bat`. The console
opens at http://127.0.0.1:8778 in field mode and live monitoring starts. To change settings, switch to operator
mode at the top left (PIN) and switch back when done. Drag a log folder onto `run_talog.bat` to get a diagnostic
report for that folder.

**On a developer PC.** Unzip the developer kit (its `talog.yaml` has `mode: developer`), copy a field day folder,
and double-click `run_talog.bat`. Log in to Claude Code with your subscription to use the Claude screen.

**For development:**

```bash
git clone <repository-url> talog
cd talog
pip install -e ".[dev]"
python -m talog --config talog.yaml --mode developer   # console (http://127.0.0.1:8778)
python tools/check.py                                  # lint, page build, full test suite
```

| Command | What it does |
|---|---|
| `talog` | Console: Operations · Incidents · Analysis · Claude (developer mode) · Settings (`--mode`, `--no-watch`) |
| `talog run` | Headless monitoring (`--check` self-check, `--replay <day folder>` replays a past day) |
| `talog analyze <log folder>` | Diagnostic report (day, machine or multi-machine folders are detected) |
| `talog mcp --self-test` | Check the Claude integration server that Claude Code / Desktop launches |
| `talog mcp --init-workspace <dir>` | Create a Claude agent folder (CLAUDE.md, `.mcp.json`, task skills) |
| `python -m PyInstaller talog.spec --noconfirm` | Build `dist/talog.exe` for field PCs |

All settings live in one `talog.yaml` (about 20 keys: mode, collect, detect, judge, notify) and are edited from
the console's Settings screen. The legacy `watch.yaml` format is still read and is converted on the first save.

## Project layout

```
talog/
  cli.py              command line: console · run · analyze · mcp · _job (console child process)
  settings.py         talog.yaml (user settings) ⇄ engine config
  access.py           modes (field · operator · developer) and the PIN hash
  watch/              live monitoring: live.py (tail.py → rules.py RuleEngine → alerts.py Notifier → agent),
                      replay.py (replay, capacity_review), gpu.py, status.py, selfcheck.py, cli.py
  capacity.py         capacity headroom model: per-group baseline, hourly KPIs, persisted state
  agent/              incident analysis: incident.py runs evidence.py → diagnoser.py (rules) →
                      analyst.py (LLM second opinion) → gate.py (agreement); factcheck.py checks numbers
  rules/              events.yaml (log event rules) · runbook.yaml (causes, actions, owners)
  events.py, assemble.py, diagnose.py, viewer.py, store.py
                      offline pipeline: parse → assemble inspections → diagnose
  report/             single-file HTML report: document.py renders sections (inspections, findings, errors,
                      models, resources, charts) with assets/report.css and report.js
  logread.py          raw log reader: binary search on line timestamps, read/search/trace within budgets
  llm.py, ask.py      Ollama client · natural-language questions over a report database
  mailer.py, graphmail.py, secret.py
                      SMTP / Microsoft 365 Graph / Gmail; secrets stored only as Windows DPAPI ciphertext
  console/            local web console (127.0.0.1)
    app.py            Console = mixins: ops · incidents · analysis · claude · checks · modes · feedback
    http.py           declarative GET/POST route tables; oper(…) and dev(…) mark the mode each route needs
    page.py           assembles talog/web into one offline HTML page
  web/                console sources: console.html shell · views/ · partials/ · css/NN-*.css · js/NN-*.js
  mcp/                Claude MCP server (stdio JSON-RPC, MCP 2025-11-25 and 2026-07-28)
    tools/            one file per domain; each tool is one @tool function
  claude_kit/         Claude agent-folder template: CLAUDE.md, task skills (talog-rca/logs/daily/site/rules)
tests/                pytest suite (fake Claude CLI for the console integration)
tools/                check.py (local gates) · make_deploy.py / make_dev_kit.py (field & developer kits)
design-system/talog/  console design system (tokens, components, checklist)
```

## Claude integration (developer mode)

| Piece | Where | Notes |
|---|---|---|
| MCP server | `talog/mcp/` | 18 tools (incidents, capacity trend, reports + read-only SQL, raw logs, replay, runbook, case KB), 5 prompts, incident/report resources, argument completion |
| Task skills | `talog/claude_kit/skills/` (copied into each agent folder by `talog mcp --init-workspace`) | `/talog-rca`, `/talog-logs`, `/talog-daily`, `/talog-site`, `/talog-rules` |
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

- [USAGE.md](USAGE.md): getting started, modes, console screens, settings, reading verdicts and reports
- [docs/DEV_PC_MANUAL.md](docs/DEV_PC_MANUAL.md): developer-PC manual, investigating field logs with Claude
- [docs/INTERNALS.md](docs/INTERNALS.md): module internals, settings layers, incident analysis flow, parsing spec
- [design-system/talog/MASTER.md](design-system/talog/MASTER.md): console design system
- [CONTRIBUTING.md](CONTRIBUTING.md): development workflow and conventions (English)
- [CHANGELOG.md](CHANGELOG.md): release history with the evidence behind each change
