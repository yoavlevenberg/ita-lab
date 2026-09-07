#!/usr/bin/env python3
"""Stand between a shell command and the two things it can quietly destroy.

data/topology.json is not a build artifact. It is tracked in git and it is the
map: 640 racks, 120k ports, and every circuit that has been executed against
it. generate_topology.py is deterministic (seed=42), so re-running it on a
pristine file is a no-op — which is exactly why running it is a habit, and why
running it after an execution silently deletes work that exists nowhere else.
`git reset --hard` and `git restore` on that file do the same thing faster.

The second case is the closed network: nothing can be installed, so a pip
install is worth a question rather than a reflex.

Runs as a PreToolUse hook on Bash.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

TOPOLOGY = "data/topology.json"

REGENERATE = re.compile(
    r"(?:(?:python|python3|py)\s+(?:-\S+\s+)*)?(?:\./|\b)generate_topology\.py\b"
)
RESET_HARD = re.compile(r"\bgit\s+(?:-\S+\s+)*reset\s+(?:\S+\s+)*--hard\b")
DISCARD_FILE = re.compile(
    r"\bgit\s+(?:-\S+\s+)*(?:checkout|restore)\b[^|;&]*\bdata/topology\.json\b"
)
CLEAN = re.compile(r"\bgit\s+(?:-\S+\s+)*clean\b[^|;&]*-\S*[fx]")
PIP_INSTALL = re.compile(r"\b(?:pip|pip3|python3?\s+-m\s+pip)\s+install\b([^|;&]*)")


def decide(decision, reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }))
    return 0


def dirty(project):
    """Is data/topology.json carrying changes that exist only in this checkout?"""
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "--", TOPOLOGY],
            cwd=project, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(out.stdout.strip())


def executed_circuits(project):
    """How many circuits the uncommitted version has that HEAD does not."""
    try:
        head = subprocess.run(
            ["git", "show", f"HEAD:{TOPOLOGY}"],
            cwd=project, capture_output=True, text=True, timeout=15,
        )
        if head.returncode:
            return None
        before = len(json.loads(head.stdout).get("circuits", {}))
        after = len(json.loads((project / TOPOLOGY).read_text(encoding="utf-8")).get("circuits", {}))
    except Exception:
        return None
    return after - before


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0

    if data.get("tool_name") != "Bash":
        return 0
    command = (data.get("tool_input") or {}).get("command") or ""
    project = Path(data.get("cwd") or ".").resolve()
    if not (project / "pathengine.py").exists():
        return 0

    hits = [
        (REGENERATE, "regenerate data/topology.json from seed=42"),
        (RESET_HARD, "throw away every uncommitted change"),
        (DISCARD_FILE, "discard the working copy of data/topology.json"),
        (CLEAN, "delete untracked and ignored files"),
    ]
    for pattern, what in hits:
        if pattern.search(command) and dirty(project):
            delta = executed_circuits(project)
            count = ""
            if delta and delta > 0:
                count = f" It currently holds {delta} circuit(s) that HEAD does not."
            return decide("deny", (
                f"This would {what}, and data/topology.json has uncommitted changes."
                f"{count} Those are executed circuits — real state written by "
                "commit_route(), not generated output, and they exist only in this "
                "checkout.\n\n"
                "Commit and push data/topology.json first, then run this again. If the "
                "changes really are disposable, say so explicitly and the user can run "
                "it themselves."
            ))

    pip = PIP_INSTALL.search(command)
    if pip:
        packages = [
            word for word in pip.group(1).split()
            if not word.startswith("-")
        ]
        strangers = [p for p in packages if re.split(r"[<>=~\[]", p)[0].lower() != "networkx"]
        if strangers:
            return decide("ask", (
                f"Installing {', '.join(strangers)}. ITA Lab runs on a closed network "
                "where nothing can be installed — networkx is the only dependency, and "
                "everything else is standard library on purpose. Fine for a throwaway "
                "tool in this container; not fine if any committed file will import it."
            ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
