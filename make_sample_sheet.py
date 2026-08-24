#!/usr/bin/env python3
"""
make_sample_sheet.py
====================
Writes a sample demand sheet (.xlsx) in the format the bulk planner expects,
filled with ports that are ACTUALLY FREE in the current topology.

    python make_sample_sheet.py                 -> data/sample_demand.xlsx
    python make_sample_sheet.py --with-errors   -> also a sheet full of typos,
                                                   to see the preflight review
    python make_sample_sheet.py -o mysheet.xlsx

Why a generator instead of a checked-in file: the moment you execute a plan,
those ports are taken and a static sample stops being valid. Run this whenever
you want a fresh one.

Writing .xlsx needs no third-party package either — it is a zip of XML, same
as reading one (see xlsxreader.py).
"""

import argparse
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import pathengine

OUT_DEFAULT = Path(__file__).parent / "data" / "sample_demand.xlsx"

SPREADSHEET = "application/vnd.openxmlformats-officedocument.spreadsheetml"
NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"


def _col(i):
    """0 -> A, 25 -> Z, 26 -> AA."""
    s, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def write_xlsx(path, rows, sheet_name="Demands"):
    """Write a sheet of strings. Every value goes through the shared-string
    table, which is what Excel itself produces for text columns."""
    shared, index = [], {}

    def sid(v):
        if v not in index:
            index[v] = len(shared)
            shared.append(v)
        return index[v]

    xml_rows = []
    for r, row in enumerate(rows, start=1):
        cells = "".join(
            f'<c r="{_col(c)}{r}" t="s"><v>{sid(str(val))}</v></c>'
            for c, val in enumerate(row) if str(val) != "")
        xml_rows.append(f'<row r="{r}">{cells}</row>')

    sheet = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             f'<worksheet xmlns="{NS_MAIN}"><sheetData>{"".join(xml_rows)}</sheetData></worksheet>')

    sst = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           f'<sst xmlns="{NS_MAIN}" count="{len(shared)}" uniqueCount="{len(shared)}">'
           + "".join(f"<si><t>{escape(v)}</t></si>" for v in shared) + "</sst>")

    workbook = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<workbook xmlns="{NS_MAIN}" xmlns:r="{NS_REL}"><sheets>'
                f'<sheet name="{escape(sheet_name)}" sheetId="1" r:id="rId1"/>'
                f'</sheets></workbook>')

    wb_rels = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               f'<Relationships xmlns="{NS_PKG}">'
               f'<Relationship Id="rId1" Type="{NS_REL}/worksheet" Target="worksheets/sheet1.xml"/>'
               f'<Relationship Id="rId2" Type="{NS_REL}/sharedStrings" Target="sharedStrings.xml"/>'
               f'</Relationships>')

    root_rels = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 f'<Relationships xmlns="{NS_PKG}">'
                 f'<Relationship Id="rId1" Type="{NS_REL}/officeDocument" Target="xl/workbook.xml"/>'
                 f'</Relationships>')

    # Excel is strict about content types — without these overrides it reports
    # the file as corrupt even though the XML inside is perfectly good.
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/xl/workbook.xml" ContentType="{SPREADSHEET}.sheet.main+xml"/>'
        f'<Override PartName="/xl/worksheets/sheet1.xml" ContentType="{SPREADSHEET}.worksheet+xml"/>'
        f'<Override PartName="/xl/sharedStrings.xml" ContentType="{SPREADSHEET}.sharedStrings+xml"/>'
        '</Types>')

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
        z.writestr("xl/sharedStrings.xml", sst)
    return path


def free_ports(topology, rack, cable_type, n):
    out = []
    for pid, p in topology["ports"].items():
        if p["rack"] == rack and p["type"] == cable_type and p["status"] == "free":
            out.append(pid)
            if len(out) == n:
                break
    return out


# Pairs chosen to exercise different shapes of route: inside one pod, across
# pods on the same MDA, and all the way across to the other room.
BUNDLES = [
    ("SVC-CORE",    "A1-S05", "D5-N06", "fiber",  4),   # cross-room, 6 hops
    ("SVC-EDGE",    "B3-S04", "B3-N07", "fiber",  3),   # inside one pod
    ("SVC-STORAGE", "A1-S03", "A3-S06", "copper", 3),   # two pods, shared MDA
]


def build_clean(topology):
    rows = [["SRC_PORT", "DST_PORT", "GROUP"]]
    for group, a_rack, b_rack, cable, count in BUNDLES:
        a = free_ports(topology, a_rack, cable, count)
        b = free_ports(topology, b_rack, cable, count)
        if len(a) < count or len(b) < count:
            print(f"  ! skipped {group}: not enough free {cable} ports "
                  f"on {a_rack}/{b_rack}")
            continue
        for src, dst in zip(a, b):
            rows.append([src, dst, group])
    return rows


def build_broken(topology):
    """One row per fault the preflight review reports, so you can see what a
    bad sheet looks like before trusting the tool with a real one."""
    a = free_ports(topology, "A1-S05", "fiber", 4)
    b = free_ports(topology, "D5-N06", "fiber", 3)
    copper = free_ports(topology, "A1-S05", "copper", 1)
    taken = next((pid for pid, p in topology["ports"].items()
                  if p["rack"] == "A1-S05" and p["status"] == "used"), "A1-S05:FIB-PP-01:1")
    same_rack = free_ports(topology, "A1-S05", "fiber", 6)

    return [
        ["SRC_PORT", "DST_PORT", "GROUP"],
        [a[0], b[0], "GOOD"],                    # fine
        [a[1], b[1], "GOOD"],                    # fine
        [a[0], b[2], "DUPLICATE"],               # reuses row 2's source port
        ["A1-S99:FIB-PP-01:1", b[2], "TYPO"],    # port does not exist
        [taken, b[2], "BUSY"],                   # already patched
        [copper[0], b[2], "MIXED-MEDIA"],        # copper -> fiber
        [a[2], a[2], "SELF"],                    # source == destination
        [same_rack[4], same_rack[5], "INTRA"],   # both ends in one rack
        [a[3], "", "INCOMPLETE"],                # missing destination
    ]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", default=str(OUT_DEFAULT))
    ap.add_argument("--with-errors", action="store_true",
                    help="also write a sheet containing every fault the review catches")
    args = ap.parse_args()

    topology = pathengine.load_topology()
    out = Path(args.out)

    rows = build_clean(topology)
    write_xlsx(out, rows)
    print(f"Wrote {out}  ({len(rows) - 1} demand rows)")
    for r in rows[1:]:
        print(f"   {r[2]:<12} {r[0]}  ->  {r[1]}")

    if args.with_errors:
        bad = out.with_name(out.stem + "_with_errors" + out.suffix)
        write_xlsx(bad, build_broken(topology))
        print(f"\nWrote {bad}  (deliberately broken — drop it in to see the review)")


if __name__ == "__main__":
    main()
