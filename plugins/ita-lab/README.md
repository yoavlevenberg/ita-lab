# ita-lab plugin

Repository tooling for the ITA Lab route planner. It carries the three rules
this project keeps having to re-learn, and makes two of them automatic.

## Install

```
claude plugin marketplace add yoavlevenberg/ita-lab
claude plugin install ita-lab@ita-lab-tools
```

## Hooks

| Event | What it does |
|---|---|
| `SessionStart` | Installs `networkx` if it is missing, fast-forwards the branch when the tree is clean, and says so when `data/topology.json` carries uncommitted circuits. Reports only when there is something to report. |
| `PreToolUse` (Bash) | **Denies** `generate_topology.py`, `git reset --hard`, `git restore`/`checkout` of the topology, and `git clean -f` while `data/topology.json` is dirty — those changes are executed circuits and exist nowhere else. **Asks** before a `pip install` of anything but `networkx`. |
| `PostToolUse` (Write/Edit) | Parses the edited `.py` file and flags any import outside the standard library and `networkx`, naming the pattern this repo uses instead. |

The Bash guard is silent when the topology is clean, since re-running
`generate_topology.py` on a pristine file is a deterministic no-op.

## Skills

- **`closed-network`** — the one-dependency rule and the stdlib patterns that
  replace `openpyxl`, `pandas`, `flask`, `requests`, `jinja2` and `pytest`;
  plus the OOXML traps (Excel omits empty rows; every part needs a
  `[Content_Types]` override).
- **`route-invariants`** — the constants that are product decisions: `HOP_COST`
  putting hops ahead of metres, the placement ladder and `PREFER_BONUS`, strand
  ownership with media as part of strand identity, zones as a hard boundary with
  neutral MDA pods, and the rule that the bulk review must predict the plan.

## Commands

| Command | |
|---|---|
| `/ita-lab:verify` | Run the suite and report the real count; tells a regression apart from an exhausted fixture. |
| `/ita-lab:audit` | The recurring audit pass, item by item, against the invariants that have broken before. |
| `/ita-lab:context-sync` | Check `CONTEXT.md` against the repository and fix the drift. |
| `/ita-lab:serve` | Bring the server up and verify through the API and served HTML, not screenshots. |

## Changing it

`hooks/hooks.json` wires the three scripts in `scripts/`. Each script exits 0
and stays silent unless it has something to say, so a broken guard degrades to
no guard rather than to a blocked session. After editing, re-check with:

```
claude plugin validate ./plugins/ita-lab
claude plugin validate .
```
