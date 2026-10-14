"""Run every local check before you commit or push.

    python tools/check.py            # lint + page build + full tests
    python tools/check.py --fast     # skip the slow test suite

Checks
  1. pyflakes on talog/, tests/, tools/            (no unused imports, undefined names, …)
  2. Console page: talog/web assembles into one offline HTML (no unresolved markers, no external URLs)
  3. pytest -q                                       (skipped with --fast)
Exit code 0 only if everything passes.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def step(title: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {title}" + (f" — {detail}" if detail else ""))
    return ok


def lint() -> bool:
    p = subprocess.run([sys.executable, "-m", "pyflakes", "talog", "tests", "tools"], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0 and "No module named pyflakes" in (p.stderr or ""):
        return step("pyflakes", False, "not installed — pip install -e .[dev]")
    out = (p.stdout + p.stderr).strip()
    return step("pyflakes", p.returncode == 0, out[:2000])


def page() -> bool:
    from talog.console.page import build_page
    html = build_page()
    problems = []
    if re.search(r"@@include|/\* @@(css|js) \*/", html):
        problems.append("unresolved marker")
    if re.search(r"""(?:src|href)\s*=\s*["']?(?:https?:)?//|@import|url\(\s*['"]?(?:https?:)?//""", html):
        problems.append("external resource")
    return step("console page", not problems, ", ".join(problems) or f"{len(html):,} chars, offline")


def tests() -> bool:
    p = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ROOT)
    return step("pytest", p.returncode == 0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="talog local checks (lint · page build · tests)")
    ap.add_argument("--fast", action="store_true", help="skip the test suite")
    a = ap.parse_args(argv)
    results = [lint(), page()]
    if not a.fast:
        results.append(tests())
    print("ALL CHECKS PASSED" if all(results) else "SOME CHECKS FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
