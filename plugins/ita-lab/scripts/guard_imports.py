#!/usr/bin/env python3
"""Catch a new third-party import the moment it is written.

This repo runs on a closed network where nothing can be installed, so the
dependency list is one name long: networkx. Everything else — reading and
writing xlsx, serving HTTP, hashing serials — is done on the standard library
on purpose. The rule is easy to state and easy to break by reflex, because the
imports that break it are the ones every other Python project starts with.

Runs as a PostToolUse hook on Write/Edit. Exit 2 puts the message in front of
Claude while the edit is still fresh; the file is left alone.
"""

import ast
import json
import os
import sys
from pathlib import Path

# The dependency, and the reflexes it exists to catch.
ALLOWED_THIRD_PARTY = {"networkx"}

INSTEAD = {
    "openpyxl": "xlsxreader.py reads xlsx with zipfile + xml.etree; make_sample_sheet.py writes it the same way",
    "xlrd": "xlsxreader.py reads xlsx with zipfile + xml.etree",
    "xlsxwriter": "make_sample_sheet.py writes xlsx with zipfile + xml.etree",
    "pandas": "the sheets are small and already parsed into dicts by xlsxreader.py",
    "numpy": "the scoring in placement.py is plain arithmetic over a few hundred racks",
    "flask": "server.py is http.server.ThreadingHTTPServer with a _guard() wrapper",
    "fastapi": "server.py is http.server.ThreadingHTTPServer with a _guard() wrapper",
    "django": "server.py is http.server.ThreadingHTTPServer with a _guard() wrapper",
    "requests": "urllib.request, if anything here ever needs to fetch",
    "httpx": "urllib.request, if anything here ever needs to fetch",
    "pytest": "test_scenarios.py is a plain script with a check() function; run it with python3",
    "jinja2": "wo_html.py builds its HTML with f-strings",
    "pydantic": "the topology is plain dicts loaded from JSON",
    "matplotlib": "the cable-hop diagram in ui.html is hand-built SVG",
    "scipy": "pathengine.py uses networkx for the graph work",
}


def payload():
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def import_roots(tree):
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:          # relative import; stays inside the project
                continue
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def main():
    data = payload()
    raw = (data.get("tool_input") or {}).get("file_path")
    if not raw:
        return 0

    path = Path(raw)
    if path.suffix != ".py" or not path.is_file():
        return 0

    project = Path(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or ".").resolve()
    try:
        path.resolve().relative_to(project)
    except ValueError:
        return 0                    # edited something outside the checkout
    if not (project / "pathengine.py").exists():
        return 0                    # not the ITA Lab checkout

    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        try:
            path.resolve().relative_to(Path(plugin_root).resolve())
            return 0                # the plugin's own scripts
        except ValueError:
            pass

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return 0                    # half-written file; the next edit will parse

    local = {p.stem for p in project.glob("*.py")}
    allowed = set(sys.stdlib_module_names) | ALLOWED_THIRD_PARTY | local | {"__future__"}

    strangers = sorted(import_roots(tree) - allowed)
    if not strangers:
        return 0

    lines = [
        f"{path.name} imports a package this project cannot have: "
        + ", ".join(strangers) + ".",
        "",
        "ITA Lab runs on a closed network where nothing can be installed. networkx is",
        "the only third-party dependency; the rest is the standard library on purpose.",
        "",
    ]
    for name in strangers:
        if name in INSTEAD:
            lines.append(f"  {name} -> {INSTEAD[name]}")
        else:
            lines.append(f"  {name} -> no stdlib equivalent is set up; ask before adding it")
    lines += [
        "",
        "Rewrite the import on the standard library, or say why this one is worth",
        "breaking the rule and let the user decide.",
    ]
    print("\n".join(lines), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
