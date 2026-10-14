# Contributing to talog

This guide is for developers working on the talog code base. User-facing text (console, reports, e-mails,
manuals) and code comments are written in Korean, the language of the operators. Write new comments in the same
style as the surrounding code.

## Ground rules

1. **Customer field data never leaves the machine.** Logs, images, recipes and anything derived from them
   (reports, screenshots, dumps) go to the git-ignored `Result/` folder, never into the repository, an issue,
   a chat tool or a cloud service.
2. **Claude runs on the user's subscription only, and only in developer mode.** Do not add API keys, gateways
   or any other billing path. The console strips `ANTHROPIC_API_KEY` and related variables before it launches
   Claude Code (`talog/console/claude.py`, `claude_env`), and every Claude route is wrapped in `dev(…)`.
   Never commit the mode PIN itself; `talog/access.py` keeps only its hash.
3. **The console works offline.** The page the browser receives is a single HTML file with no external
   scripts, styles, fonts or images (a test enforces this).
4. **Runtime dependencies stay minimal.** The only runtime dependency is PyYAML. Discuss before adding another;
   field PCs run the PyInstaller build without Python.
5. **Detection and judgement changes need evidence.** If you change a rule, a threshold or the diagnosis logic,
   replay real field logs before and after the change and record the comparison in `CHANGELOG.md`.

## Setup

```bash
pip install -e ".[dev]"        # pytest, pyflakes, pyinstaller
python tools/check.py          # lint, page build, full test suite
python tools/check.py --fast   # without the test suite
```

Run the console against a config: `python -m talog --config talog.yaml --port 8778 --mode developer`
(`--mode field` or `operator` shows the other modes; the switch at the top left changes the mode too).
Python changes need a console restart. Page changes under `talog/web/` show up on browser reload.

## Workflow

- Commits currently go straight to `master` (single maintainer). Agree on a branching model with the maintainer
  before several people push.
- Before every push: `python tools/check.py` must print `ALL CHECKS PASSED`.
- Commit subject: `feat|fix|refactor|docs: v<version> - <English summary>`, for example
  `fix: v2.2.0 - replay accepted a sibling folder of the log root`. Put the reason and the evidence in the body.
  Do not add co-author or tool-attribution trailers.

## Code map

| Area | Location | Start here |
|---|---|---|
| Modes | `talog/access.py`, `talog/console/modes.py`, `talog/web/js/14-access.js` | `resolve_mode`, `ModeMixin.set_mode` |
| Live monitoring and rules | `talog/watch/`, `talog/capacity.py`, `talog/rules/events.yaml` | `watch/rules.py` (`RuleEngine`), `watch/live.py` (`LiveWatch`) |
| Incident judgement | `talog/agent/`, `talog/rules/runbook.yaml` | `agent/diagnoser.py` (`RuleDiagnoser`), `agent/factcheck.py` (`fact_check`) |
| Incident closing and learning | `talog/console/feedback.py`, `talog/casekb.py`, `web/js/31-incident-agent.js` | `set_incident_state`, `search_all` |
| Offline reports | `talog/events.py` → `assemble.py` → `diagnose.py` → `report/` | `cli.scan_day`, `report/document.py` (`render`) |
| Console backend | `talog/console/` | `app.py` (state), `http.py` (routes) |
| Console frontend | `talog/web/` | `console.html` (shell), `js/00-core.js` (helpers) |
| Claude MCP server | `talog/mcp/` | `registry.py`, `tools/`, `server.py` |
| Claude agent kit | `talog/claude_kit/` | `CLAUDE.md`, `skills/` |

The `watch`, `agent` and `report` packages re-export every name of the former single modules, so
`from talog.watch import RuleEngine` keeps working. Import from the submodule in new code.

`docs/INTERNALS.md` (Korean) describes the data flow, the two settings layers and the parsing specification.

## Recipes

### Add an MCP tool

1. Pick the domain file in `talog/mcp/tools/` (or add a new one at the **end** of the import list in
   `tools/__init__.py`; the import order is the `tools/list` order that clients cache).
2. Write one function with its spec next to it:

   ```python
   @tool({"name": "group_health", "title": "그룹 상태",
          "description": "…when Claude should call it, what it returns…",
          "inputSchema": schema({"source": SOURCE, "group": {"type": "integer"}}, ["group"]),
          "annotations": READ_ONLY})
   def group_health(srv: TalogMCP, a: dict) -> str:
       s = srv.one(a.get("source", ""))
       ...
       return cut(srv.mask.text(text))      # mask customer values, cap the length
   ```

3. Raise `ValueError` with a Korean sentence for bad input; the server turns it into an `isError` result.
4. A tool that writes files uses `annotations: WRITES_LOCAL`. It must not be added to the console's default
   allow-list (`ClaudeMixin.ASK_TOOLS`) or to any skill's `allowed-tools`.
5. Add tests in `tests/test_mcp.py` (the `site` fixture builds a realistic data folder).

### Add a console API endpoint

1. Add the method to the mixin that owns the data (`talog/console/ops.py`, `incidents.py`, `analysis.py`,
   `claude.py`, `checks.py`, `modes.py` or `feedback.py`). Shared state lives in `Console.__init__` (`app.py`) only.
2. Add one line to `GET_ROUTES` or `POST_ROUTES` in `talog/console/http.py`. Return a dict for a 200 JSON
   response, or `(status, body[, content_type])`. Raise `FileNotFoundError` for a 404.
3. Decide which mode may call it and say so in the table:
   - `oper(fn)` — operator mode or higher: changes settings or the running system (save, tests, stop
     monitoring, replays). Field mode answers `403` with `need: "operator"`.
   - `dev(fn)` — developer mode only: anything that runs or configures Claude.
   - no wrapper — viewing and everyday work in field mode (status, incidents, building a report).
4. POST routes are already protected by the session token and the Host check. Do not add routes outside
   these tables. In the page, hide developer-only elements with `class="dev-only"` (or `isDev()` in JS) and put
   `await needMode("operator", "…")` in front of operator actions, so field users get the switch dialog
   (PIN) instead of an error.

### Change the console UI

- Sources: `talog/web/views/*.html` (markup per screen), `css/NN-*.css`, `js/NN-*.js`. The number prefix is
  the concatenation order, and all JS files share one `<script>`, so later files can use helpers from earlier ones.
- Follow `design-system/talog/MASTER.md`: use tokens only (no raw colours), icons from the SVG sprite in
  `partials/icons.html` (no emoji), and the checklist in section 6.
- Compute contrast for any new colour pair with `python tools/contrast_check.py` (4.5:1 text, 3:1 non-text, both
  themes) and check the layout at 1440, 1024 and 375 px in light and dark themes, in field and developer mode.
- Never put customer data in screenshots that go into the repository.

### Add a report section

1. Write `_xxx_section(ctx) -> str` in the `talog/report/` module for that area (inspections, findings, errors,
   models, resources) and use `util._esc` for every value taken from logs.
2. Place it in the body template of `report/document.py` (`render`). Styles go to `report/assets/report.css`,
   page behaviour to `report/assets/report.js` (the report stays one self-contained HTML file).
3. Check the output with `python -m talog analyze <day folder> --out <folder>` on a real day and on an empty one.

### Change a Claude skill

1. Edit `talog/claude_kit/skills/<name>/`. `talog mcp --init-workspace <dir>` copies the skills into an agent
   folder; re-run it (or let the console create its agent folder) to pick up the change.
2. Keep `allowed-tools` read-only. Skills that start writing work (like `talog-rules`) use
   `disable-model-invocation: true`, so they run only when a person asks.
3. Update the matching MCP prompt in `talog/mcp/prompts.py` if the procedure changed. Claude Desktop chat uses
   prompts instead of skills.

### Change detection or judgement

1. Reproduce the case with real logs: `talog run --replay <day folder>` (or the console's "경보 재현").
2. Compare alerts and incidents before and after, including the other sites' replays.
3. Record the numbers, the sites and the reasoning in `CHANGELOG.md`.

### Release

1. Bump the version in `pyproject.toml` and `talog/__init__.py` and finish the `CHANGELOG.md` entry.
2. `python tools/check.py`, then `python -m PyInstaller talog.spec --noconfirm` → `dist/talog.exe`.
3. Field kit (`mode: field`): `python tools/make_deploy.py --site …`. Developer kit (`mode: developer`):
   `python tools/make_dev_kit.py --out <folder>`.

## Tests

- `tests/test_console_claude.py` drives the console's Claude integration with a **fake Claude CLI** that emits
  Claude Code's `stream-json` events. Tests never call the real Claude.
- `tests/test_access.py` covers the three modes, the PIN and the route guards over HTTP (with a test PIN).
- `tests/test_mcp.py` speaks JSON-RPC to the MCP server directly (both protocol eras).
- Tests write only to pytest's `tmp_path`.
