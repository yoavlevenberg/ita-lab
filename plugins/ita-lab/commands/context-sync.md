---
description: Check CONTEXT.md against the repository and correct what has drifted.
allowed-tools: Bash, Read, Grep, Glob, Edit
---

`CONTEXT.md` is the handoff note for whoever picks this project up next, so a
stale line in it is worse than no line. Check it against reality and fix what
has moved.

Verify at least:

- **The file table.** Every `*.py` in the repo appears, and every row still
  describes what that file does. New modules are the usual gap.
- **The test count.** Run `python3 test_scenarios.py` and use the real number.
- **The API list.** Compare against the routes actually handled in `server.py`.
- **The TODO list.** Items get done without the list being updated — check each
  one against the code before believing it. `decommission_route()`, a search
  box, and a capacity report were all listed as pending after they landed.
- **The status section.** Branch, remote, and whether `data/topology.json` is
  pristine (1,000 circuits, `CIR-0001..CIR-1000`) or carries executed work.
- **Dataset counts** (racks, devices, ports, circuits) against
  `data/topology.json`.

Write in the same voice as the rest of the file — Hebrew, specific, and
explaining *why* a decision was made rather than only what it is. Keep the
"traps already fallen into" section: it is the most valuable part.

Report what you changed and what you confirmed was still accurate.
