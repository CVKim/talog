# Contributing to talog

This guide is for developers working on the talog code base. User-facing text (console, reports, e-mails,
manuals) and code comments are written in Korean, the language of the operators. Write new comments in the same
style as the surrounding code.

## Ground rules

1. **Customer field data never leaves the machine.** Logs, images, recipes and anything derived from them
   (reports, screenshots, dumps) go to the git-ignored `Result/` folder, never into the repository, an issue,
   a chat tool or a cloud service.
2. **Claude runs on the user's subscription only.** Do not add API keys, gateways or any other billing path.
   The console strips `ANTHROPIC_API_KEY` and related variables before it launches Claude Code
   (`talog/console/claude.py`, `claude_env`).
3. **The console works offline.** The page the browser receives is a single HTML file with no external
   scripts, styles, fonts or images (a test enforces this).
4. **Runtime dependencies stay minimal.** The only runtime dependency is PyYAML. Discuss before adding another;
   field PCs run the PyInstaller build without Python.
5. **Detection and judgement changes need evidence.** If you change a rule, a threshold or the diagnosis logic,
   replay real field logs before and after the change and record the comparison in `CHANGELOG.md`.

## Setup

```bash
pip install -e ".[dev]"        # pytest, pyflakes, pyinstaller
python tools/check.py          # lint, repo invariants, full test suite (about 50 s)
python tools/check.py --fast   # without the test suite
```

Run the console against a config: `python -m talog --config talog.yaml --port 8778`. Python changes need a
console restart. Page changes under `talog/web/` show up on browser reload.

## Workflow

- Commits currently go straight to `master` (single maintainer, agreed 2026-10-14). Agree on a branching
  model with the maintainer before several people push.
- Before every push: `python tools/check.py` must print `ALL CHECKS PASSED`. CI runs the same checks on Windows
  and Ubuntu.
- Commit subject: `feat|fix|refactor|docs: v<version> - <English summary>`, for example
  `fix: v2.2.0 - replay accepted a sibling folder of the log root`. Put the reason and the evidence in the body.

## Code map

| Area | Location | Start here |
|---|---|---|
| Live monitoring and rules | `talog/watch.py`, `talog/capacity.py`, `talog/rules/events.yaml` | `RuleEngine`, `LiveWatch` |
| Incident judgement | `talog/agent.py`, `talog/rules/runbook.yaml` | `RuleDiagnoser`, `fact_check` |
| Offline reports | `talog/events.py` → `assemble.py` → `diagnose.py` → `report.py` | `cli.scan_day` |
| Console backend | `talog/console/` | `app.py` (state), `http.py` (routes) |
| Console frontend | `talog/web/` | `console.html` (shell), `js/00-core.js` (helpers) |
| Claude MCP server | `talog/mcp/` | `registry.py`, `tools/`, `server.py` |
| Claude agent kit | `talog/claude_kit/` | `CLAUDE.md`, `skills/` |

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
   `claude.py` or `checks.py`). Shared state lives in `Console.__init__` (`app.py`) only.
2. Add one line to `GET_ROUTES` or `POST_ROUTES` in `talog/console/http.py`. Return a dict for a 200 JSON
   response, or `(status, body[, content_type])`. Raise `FileNotFoundError` for a 404.
3. POST routes are already protected by the session token and the Host check. Do not add routes outside
   these tables.

### Change the console UI

- Sources: `talog/web/views/*.html` (markup per screen), `css/NN-*.css`, `js/NN-*.js`. The number prefix is
  the concatenation order, and all JS files share one `<script>`, so later files can use helpers from earlier ones.
- Follow `design-system/talog/MASTER.md`: use tokens only (no raw colours), icons from the SVG sprite in
  `partials/icons.html` (no emoji), and the checklist in section 6.
- Compute contrast for any new colour pair (4.5:1 text, 3:1 non-text, both themes) and check the layout at
  1440, 1024 and 375 px in light and dark themes.
- Never put customer data in screenshots that go into the repository.

### Change a Claude skill

1. Edit `talog/claude_kit/skills/<name>/` and copy the folder to `.claude/skills/<name>/`
   (`tools/check.py` fails if they differ).
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
3. Field kit: `python tools/make_deploy.py --site …`. Developer kit: `python tools/make_dev_kit.py --out <folder>`.

## Tests

- `tests/test_console_claude.py` drives the console's Claude integration with a **fake Claude CLI** that emits
  Claude Code's `stream-json` events. Tests never call the real Claude.
- `tests/test_mcp.py` speaks JSON-RPC to the MCP server directly (both protocol eras).
- Tests write only to pytest's `tmp_path`.
