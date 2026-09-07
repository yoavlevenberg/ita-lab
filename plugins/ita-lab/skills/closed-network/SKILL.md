---
name: closed-network
description: The dependency rule for ITA Lab and the stdlib patterns that replace the usual libraries. Use when adding an import, reading or writing xlsx, adding an HTTP endpoint, picking a test framework, hashing anything, or when tempted to reach for pandas, openpyxl, flask, requests, or pytest in this repo.
---

# One dependency, on purpose

ITA Lab is destined for a closed network with no package index and no npm. The
whole build is `pip install networkx` and `python server.py`. That is not an
accident of a small project — it is the property that lets the tool run where
the real ITA lives.

So: **networkx is the only third-party import.** Everything else is the Python
standard library. When a task seems to need a library, the answer is almost
always already in the repo, written the hard way once so it never has to be
written again.

## What is already solved without a dependency

| Temptation | What this repo does instead |
|---|---|
| `openpyxl` / `xlrd` to read a sheet | `xlsxreader.py` — `zipfile` + `xml.etree` over the OOXML parts |
| `openpyxl` / `xlsxwriter` to write one | `make_sample_sheet.py` — builds the parts and zips them by hand |
| `pandas` for the rows | `xlsxreader` hands back dicts; the sheets are tens of rows |
| `flask` / `fastapi` for the API | `server.py` — `http.server.ThreadingHTTPServer`, one `_guard()` wrapper, `WRITE_LOCK` around read-modify-write |
| `requests` | `urllib.request` (only `webbrowser` currently reaches outward at all) |
| `jinja2` for the work order | `wo_html.py` — f-strings |
| `matplotlib` / `d3` for the hop diagram | hand-built SVG in `ui.html` |
| `pytest` | `test_scenarios.py` is a script with a `check()` function; run it with `python3` |
| `numpy` for the placement score | plain arithmetic over a few hundred racks |
| a hashing library for serials | `hashlib.sha256`, truncated to 10 digits (`serials.py`) |

A `PostToolUse` hook flags a new third-party import as soon as it is written.
It is a reminder, not a wall — but treat it as a design question, not a lint
error. If a dependency genuinely is the right call, that is the user's decision
to make, not one to slip in.

## Writing xlsx by hand — the parts that bite

Both directions of OOXML have already cost this project a debugging session:

- **Excel omits empty rows.** A sheet with data on rows 1, 2 and 7 contains
  three `<row>` elements. Read the `r` attribute; never infer the row number
  from position. Every validation message in `bulkplan.py` quotes a row number
  the user will look for in Excel, so an off-by-N here is a message that sends
  someone to the wrong line.
- **`[Content_Types].xml` needs an override for every part.** Miss one and
  Excel declares the file corrupt, with no hint as to which part.
- Strings live in the shared-strings table; a cell holds an index, not text.

## Testing without a framework

`test_scenarios.py` is the suite (`python3 test_scenarios.py`, ~35s, exits
non-zero on failure). It collects results through `check(name, ok, detail)` and
prints `N/N checks passed`. `test_agreement.py` is a property sweep over
generated sheets, and `test_scenarios.py` calls into it so the property runs as
part of the standing suite.

**A fixture that hardcodes a port id is a bug in the test.** The suite used to
name `A1-S05:TOR-SW-01` and assume free ports on it; once a real execution
consumed them, the suite failed and it looked like a planner bug rather than an
exhausted fixture. New fixtures search the topology for a device in the state
they need.
