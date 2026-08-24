#!/usr/bin/env python3
"""
xlsxreader.py
=============
Minimal .xlsx reader built on the Python standard library only.

An .xlsx file is just a ZIP archive of XML parts, so reading one needs no
third-party package at all. That matters here: the whole point of this tool
is that it runs on a closed network with nothing to install (see README) —
adding openpyxl just to read a two-column demand sheet would break that
promise.

Scope is deliberately narrow: cell VALUES as text, which is all a port
demand list needs. No formatting, no formulas re-evaluation, no dates.

    rows = read_sheet("demands.xlsx")        # -> [{"SRC_PORT": "...", ...}, ...]
"""

import re
import zipfile
from xml.etree import ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
DOC_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

_CELL_REF = re.compile(r"^([A-Z]+)(\d+)$")


class XlsxError(Exception):
    """The file isn't a readable .xlsx, or has no usable sheet."""


def _col_index(letters):
    """'A' -> 0, 'B' -> 1, ... 'AA' -> 26."""
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


def _text_of(node):
    """Concatenate every <t> under a node — shared strings can be split into
    <r> runs when part of the cell was formatted differently."""
    return "".join(t.text or "" for t in node.iter(f"{NS}t"))


def _shared_strings(z):
    try:
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [_text_of(si) for si in root.findall(f"{NS}si")]


def _sheet_paths(z):
    """Every sheet in the workbook, as an ordered {name: part path}.

    Resolved through the workbook relationships rather than by assuming
    xl/worksheets/sheet1.xml — Excel does not always name the parts that way,
    and a file with several tabs needs them matched by name.
    """
    try:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    except KeyError as e:
        raise XlsxError(f"not a valid .xlsx (missing {e})")

    targets = {r.get("Id"): r.get("Target") for r in rels.findall(f"{REL_NS}Relationship")}
    sheets = wb.find(f"{NS}sheets")
    if sheets is None or not len(sheets):
        raise XlsxError("the workbook has no sheets")

    out = {}
    for sheet in sheets:
        target = targets.get(sheet.get(f"{DOC_REL}id"))
        if not target:
            continue
        target = target.lstrip("/")
        out[sheet.get("name") or f"Sheet{len(out) + 1}"] = (
            target if target.startswith("xl/") else f"xl/{target}")
    if not out:
        raise XlsxError("could not resolve any sheet in the workbook")
    return out


def sheet_names(path_or_file):
    """Tab names, in workbook order — so a caller can ask 'is there a Devices
    tab?' without reading the whole file."""
    with zipfile.ZipFile(path_or_file) as z:
        return list(_sheet_paths(z))


def _first_sheet_path(z):
    return next(iter(_sheet_paths(z).values()))


def _cell_value(cell, shared):
    ctype = cell.get("t")
    if ctype == "inlineStr":
        is_node = cell.find(f"{NS}is")
        return _text_of(is_node) if is_node is not None else ""
    v = cell.find(f"{NS}v")
    if v is None or v.text is None:
        return ""
    raw = v.text
    if ctype == "s":                                  # shared-string index
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    if ctype == "b":
        return "TRUE" if raw == "1" else "FALSE"
    # numbers arrive as "4" or "4.0"; keep integers looking like integers so
    # a port number never renders as "2.0"
    if ctype in (None, "n"):
        try:
            f = float(raw)
            return str(int(f)) if f.is_integer() else str(f)
        except ValueError:
            return raw
    return raw


def read_rows(path_or_file, sheet=None):
    """Every row as (excel_row_number, [cell strings]), padded so all rows are
    equal width. Blank trailing rows are dropped.

    The row number comes from the row's own `r` attribute, NOT from its
    position in the list: Excel omits empty rows entirely, so counting
    positions would report the wrong line for every row after the first gap —
    and these numbers are how a user finds the offending line in their file.
    """
    try:
        z = zipfile.ZipFile(path_or_file)
    except zipfile.BadZipFile:
        raise XlsxError("not a .xlsx file (it is not a zip archive) — if this "
                        "is an old .xls, re-save it as .xlsx")
    with z:
        shared = _shared_strings(z)
        if sheet is None:
            part = _first_sheet_path(z)
        else:
            paths = _sheet_paths(z)
            # tab names are typed by hand, so match them forgivingly
            match = next((p for name, p in paths.items()
                          if _norm(name) == _norm(sheet)), None)
            if match is None:
                raise XlsxError(f"no sheet named '{sheet}' — this workbook has: "
                                + ", ".join(paths))
            part = match
        doc = ET.fromstring(z.read(part))

        rows, width = [], 0
        for n, row in enumerate(doc.iter(f"{NS}row"), start=1):
            try:
                rownum = int(row.get("r"))
            except (TypeError, ValueError):
                rownum = n                      # malformed sheet: fall back to position
            cells = {}
            for c in row.findall(f"{NS}c"):
                m = _CELL_REF.match(c.get("r") or "")
                if not m:
                    continue
                cells[_col_index(m.group(1))] = _cell_value(c, shared).strip()
            if not cells:
                rows.append((rownum, []))
                continue
            top = max(cells) + 1
            width = max(width, top)
            rows.append((rownum, [cells.get(i, "") for i in range(top)]))

    while rows and not any(rows[-1][1]):
        rows.pop()
    return [(n, r + [""] * (width - len(r))) for n, r in rows]


ROW_KEY = "__row__"      # real Excel line number, attached to every returned dict


def read_sheet(path_or_file, required=(), required_any=(), sheet=None):
    """Rows as dicts keyed by the header row, each carrying its true Excel
    line number under ROW_KEY. Header matching is case- and space-insensitive
    so 'Src Port', 'SRC_PORT' and 'src port' all work — real demand sheets are
    written by hand and never agree on spelling.

    `required` names columns that must all be present. `required_any` takes
    GROUPS of alternative spellings, one of which must appear per group — that
    is what a demand sheet needs, where the source column may be called
    SRC_PORT, From, or A_PORT.

    The header is the first row that actually satisfies those conditions, so a
    title or blank rows above the real header (common in sheets people send)
    don't derail the read. If no row qualifies, the error lists what the first
    non-empty row did contain, which is far easier to act on than a KeyError
    deep inside the planner.
    """
    rows = read_rows(path_or_file, sheet=sheet)
    if not rows:
        raise XlsxError("the sheet is empty")

    want = [_norm(c) for c in required]
    want_any = [[_norm(c) for c in group] for group in required_any]

    def qualifies(cells):
        norm = {_norm(h) for h in cells if h}
        return (all(c in norm for c in want)
                and all(any(c in norm for c in group) for group in want_any))

    header_at = None
    for i, (_, cells) in enumerate(rows):
        if not any(cells):
            continue
        if qualifies(cells):
            header_at = i
            break
        if not want and not want_any:           # nothing to look for: first row wins
            header_at = i
            break

    if header_at is None:
        first = next(((n, c) for n, c in rows if any(c)), None)
        found = ", ".join(h for h in (first[1] if first else []) if h)
        needed = list(want) + [" / ".join(g[:3]) + "…" for g in want_any]
        raise XlsxError(
            f"could not find a header row containing: {', '.join(needed)}. "
            f"First row of the sheet holds: {found or '(nothing)'}")

    header = [_norm(h) for h in rows[header_at][1]]
    out = []
    for rownum, cells in rows[header_at + 1:]:
        if not any(cells):
            continue
        rec = {header[i]: v for i, v in enumerate(cells)
               if i < len(header) and header[i]}
        rec[ROW_KEY] = rownum
        out.append(rec)
    return out


def _norm(name):
    """'Src Port' / 'SRC_PORT' / 'src-port' all collapse to 'SRC_PORT'."""
    return re.sub(r"[\s\-]+", "_", str(name).strip()).upper()
