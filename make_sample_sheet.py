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
import copy
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import pathengine
import serials
import zones

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


def write_xlsx(path, tabs):
    """Write a workbook. `tabs` is an ordered {sheet_name: rows} mapping, so a
    demand file can carry both its P2P tab and its Devices tab.

    Every value goes through the shared-string table, which is what Excel
    itself produces for text columns.
    """
    shared, index = [], {}

    def sid(v):
        if v not in index:
            index[v] = len(shared)
            shared.append(v)
        return index[v]

    sheets_xml = []
    for rows in tabs.values():
        xml_rows = []
        for r, row in enumerate(rows, start=1):
            cells = "".join(
                f'<c r="{_col(c)}{r}" t="s"><v>{sid(str(val))}</v></c>'
                for c, val in enumerate(row) if str(val) != "")
            xml_rows.append(f'<row r="{r}">{cells}</row>')
        sheets_xml.append(
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<worksheet xmlns="{NS_MAIN}"><sheetData>{"".join(xml_rows)}</sheetData></worksheet>')

    sst = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           f'<sst xmlns="{NS_MAIN}" count="{len(shared)}" uniqueCount="{len(shared)}">'
           + "".join(f"<si><t>{escape(v)}</t></si>" for v in shared) + "</sst>")

    sheet_tags = "".join(
        f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>'
        for i, name in enumerate(tabs, start=1))
    workbook = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<workbook xmlns="{NS_MAIN}" xmlns:r="{NS_REL}">'
                f'<sheets>{sheet_tags}</sheets></workbook>')

    sst_rid = len(tabs) + 1
    rel_tags = "".join(
        f'<Relationship Id="rId{i}" Type="{NS_REL}/worksheet" '
        f'Target="worksheets/sheet{i}.xml"/>'
        for i in range(1, len(tabs) + 1))
    wb_rels = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               f'<Relationships xmlns="{NS_PKG}">{rel_tags}'
               f'<Relationship Id="rId{sst_rid}" Type="{NS_REL}/sharedStrings" '
               f'Target="sharedStrings.xml"/></Relationships>')

    root_rels = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 f'<Relationships xmlns="{NS_PKG}">'
                 f'<Relationship Id="rId1" Type="{NS_REL}/officeDocument" Target="xl/workbook.xml"/>'
                 f'</Relationships>')

    # Excel is strict about content types — without an override per part it
    # reports the file as corrupt even though the XML inside is perfectly good.
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
        f'ContentType="{SPREADSHEET}.worksheet+xml"/>'
        for i in range(1, len(tabs) + 1))
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/xl/workbook.xml" ContentType="{SPREADSHEET}.sheet.main+xml"/>'
        f'{overrides}'
        f'<Override PartName="/xl/sharedStrings.xml" ContentType="{SPREADSHEET}.sharedStrings+xml"/>'
        '</Types>')

    # `path` may be an open file object — the server builds a sample straight
    # into memory rather than writing one to disk to serve it back
    if not hasattr(path, "write"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        for i, doc in enumerate(sheets_xml, start=1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", doc)
        z.writestr("xl/sharedStrings.xml", sst)
    return path


def free_ports(topology, rack, cable_type, n):
    out = []
    for pid, p in topology["ports"].items():
        if (p["rack"] == rack and p["type"] == cable_type and p["status"] == "free"
                and not topology["devices"][p["device"]].get("external")):
            out.append(pid)
            if len(out) == n:
                break
    return out


def _routable_pairs(topology, cable_type, count, want, tries=80):
    """Cabinet pairs with `count` free ports of this media at both ends AND a
    route between them, found on whatever map is loaded. The fixed BUNDLES
    name cabinets of the synthetic map; a site from ITA has its own."""
    racks = sorted(r for r, meta in topology["racks"].items()
                   if not meta.get("external")
                   and len(free_ports(topology, r, cable_type, count)) == count)
    out, used = [], set()
    for i, a in enumerate(racks):
        for b in racks[i + 1:]:
            if tries <= 0 or len(out) == want:
                return out
            if a in used or b in used:
                continue
            tries -= 1
            # every row of the bundle has to route, not just the first: a trunk
            # with one free fiber carries one row, so plan them all on a copy
            trial = copy.deepcopy(topology)
            try:
                for src, dst in zip(free_ports(trial, a, cable_type, count),
                                    free_ports(trial, b, cable_type, count)):
                    route = pathengine.resolve_route_options(src, dst, count=1,
                                                             topology=trial)[0]
                    pathengine.commit_route(trial, route, pathengine.next_circuit_id(trial))
            except pathengine.RouteError:
                continue
            out.append((a, b))
            used.update((a, b))
    return out


def _bundles(topology):
    """The fixed bundles when this is the synthetic map, otherwise bundles
    found on the map itself."""
    if all(a in topology["racks"] and b in topology["racks"] for _, a, b, _, _ in BUNDLES):
        return BUNDLES
    out = []
    for cable, count, want in (("fiber", 3, 2), ("copper", 2, 1)):
        # a small or tightly patched site may only carry single rows
        pairs = _routable_pairs(topology, cable, count, want) or             _routable_pairs(topology, cable, 1, want)
        n = count if pairs and len(free_ports(topology, pairs[0][0], cable, count)) == count             and _routable_pairs(topology, cable, count, want) else 1
        for a, b in pairs:
            out.append((f"SVC-{len(out) + 1}", a, b, cable, n))
    return out


# Pairs chosen to exercise different shapes of route: inside one pod, across
# pods on the same MDA, and all the way across to the other room.
BUNDLES = [
    ("SVC-CORE",    "A1-S05", "D5-N06", "fiber",  4),   # cross-room, 6 hops
    ("SVC-EDGE",    "B3-S04", "B3-N07", "fiber",  3),   # inside one pod
    ("SVC-STORAGE", "A1-S03", "A3-S06", "copper", 3),   # two pods, shared MDA
]


def endpoint(topology, port_id):
    """Split a port id into the two cells the sheet now uses: the serial of the
    box, and the port number on it."""
    dev_id, _, index = port_id.rpartition(":")
    serial = topology["devices"][dev_id].get("serial")
    # a serial that is blank, junk or shared cannot name the box: use its id,
    # which the sheet accepts just the same
    if serials.index(topology).get(serials.canonical(serial)) != dev_id:
        return dev_id, index
    return serial, index


def build_clean(topology):
    rows = [["SRC_DEVICE", "SRC_PORT", "DST_DEVICE", "DST_PORT", "GROUP", "LABEL"]]
    for group, a_rack, b_rack, cable, count in _bundles(topology):
        a = free_ports(topology, a_rack, cable, count)
        b = free_ports(topology, b_rack, cable, count)
        if len(a) < count or len(b) < count:
            print(f"  ! skipped {group}: not enough free {cable} ports "
                  f"on {a_rack}/{b_rack}")
            continue
        for src, dst in zip(a, b):
            a_sn, a_p = endpoint(topology, src)
            b_sn, b_p = endpoint(topology, dst)
            rows.append([a_sn, a_p, b_sn, b_p, group, ""])
    return rows


def build_broken(topology):
    """One row per fault the preflight review reports, so you can see what a
    bad sheet looks like before trusting the tool with a real one."""
    E = lambda pid: endpoint(topology, pid)
    ra, rb = "A1-S05", "D5-N06"
    if ra not in topology["racks"] or rb not in topology["racks"]:
        found = _routable_pairs(topology, "fiber", 6, 1)
        if not found:
            raise ValueError("this map has no two cabinets with six free fiber ports "
                             "and a route between them — nothing to build an example from")
        ra, rb = found[0]
    a = [E(p) for p in free_ports(topology, ra, "fiber", 4)]
    b = [E(p) for p in free_ports(topology, rb, "fiber", 3)]
    copper_free = free_ports(topology, ra, "copper", 1) or next(
        ([pid] for pid, p in topology["ports"].items()
         if p["type"] == "copper" and p["status"] == "free"
         and not topology["devices"][p["device"]].get("external")), [])
    copper = E(copper_free[0]) if copper_free else (a[3][0], "1")
    used = next((pid for pid, p in topology["ports"].items()
                 if p["rack"] == ra and p["status"] == "used"), None)
    taken = E(used) if used else a[0]
    same_rack = [E(p) for p in free_ports(topology, ra, "fiber", 6)]

    return [
        ["SRC_DEVICE", "SRC_PORT", "DST_DEVICE", "DST_PORT", "GROUP"],
        [*a[0], *b[0], "GOOD"],                      # fine
        [*a[1], *b[1], "GOOD"],                      # fine
        [*a[0], *b[2], "DUPLICATE"],                 # reuses row 2's source port
        ["9999999999", "1", *b[2], "TYPO"],          # no such serial
        [*taken, *b[2], "BUSY"],                     # already patched
        [*copper, *b[2], "MIXED-MEDIA"],             # copper -> fiber
        [*a[2], *a[2], "SELF"],                      # source == destination
        [*same_rack[4], *same_rack[5], "INTRA"],     # both ends in one rack
        [*a[3], "", "", "INCOMPLETE"],               # missing destination
        ["", "7", *b[2], "PORT-NO-DEVICE"],          # port number, no box beside it
        [a[3][0], "9999", *b[2], "NO-SUCH-PORT"],    # box exists, port number does not
    ]


def build_new_devices(topology):
    """A workbook showing the other half of the tool: kit that has not been
    installed yet. The Devices tab says what is arriving; the P2P tab wires it
    up by serial, exactly as if it were already racked."""
    if "A1-S05:TOR-SW-01" in topology["devices"]:
        tor = topology["devices"]["A1-S05:TOR-SW-01"]["serial"]
    else:
        # a site from ITA: uplink to any switch with a free fiber port, named
        # the way the sheet will name it
        index = serials.index(topology)
        sw = next((d for d in sorted(topology["devices"].values(), key=lambda d: d["id"])
                   if d["type"] == "switch" and not d.get("external")
                   and any(p["type"] == "fiber" and p["status"] == "free"
                           for p in topology["ports"].values() if p["device"] == d["id"])), None)
        if sw is None:
            raise ValueError("this map has no switch with a free fiber port to uplink to")
        tor = sw["serial"] if index.get(serials.canonical(sw["serial"])) == sw["id"] else sw["id"]
    # a zone is a hard boundary: name it in the example only where this map
    # actually has cabinets of that colour
    green = "ירוק" if zones.racks_in(topology, "green") else ""
    blue = "blue" if zones.racks_in(topology, "blue") else ""

    # ZONE is a hard boundary: a device of a colour is installed inside that
    # colour's pods or not at all. Leave it blank for no restriction.
    # Serials are plain numbers. LABEL is optional everywhere it appears.
    devices = [
        ["SERIAL", "TYPE", "U_SIZE", "FIBER", "COPPER", "ZONE", "LABEL"],
        ["7700100001", "switch", "1", "4", "48", green,
         "Leaf switch 48x RJ45 + 4x SFP+"],
        ["7700100002", "server", "2", "2", "2", green, "App server 2U"],
        ["7700100003", "server", "2", "2", "2", green, ""],
        ["7700100004", "server", "2", "2", "2", blue, "App server (blue zone)"],
    ]
    # note the chaining: the servers hang off the NEW switch, which itself
    # uplinks to an existing ToR.
    # Each end is a serial in its own column, with the port number optional
    # beside it — blank means "any suitable port on that box", which is the
    # normal case when the kit is still in its crate.
    p2p = [
        ["SRC_DEVICE", "SRC_PORT", "DST_DEVICE", "DST_PORT", "GROUP", "CABLE", "LABEL"],
        ["7700100001", "",  tor,          "",  "UPLINK", "fiber",  "Leaf uplink to ToR"],
        ["7700100002", "3", "7700100001", "5", "ACCESS", "copper", "App server 1"],
        ["7700100003", "",  "7700100001", "",  "ACCESS", "copper", ""],
        ["7700100004", "",  "7700100001", "",  "ACCESS", "copper", "Blue-zone server"],
    ]
    return {"P2P": p2p, "Devices": devices}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", default=str(OUT_DEFAULT))
    ap.add_argument("--with-errors", action="store_true",
                    help="also write a sheet containing every fault the review catches")
    ap.add_argument("--with-devices", action="store_true",
                    help="also write a workbook with a Devices tab of new equipment")
    args = ap.parse_args()

    topology = pathengine.load_topology()
    out = Path(args.out)

    rows = build_clean(topology)
    write_xlsx(out, {"P2P": rows})
    print(f"Wrote {out}  ({len(rows) - 1} demand rows)")
    for r in rows[1:]:
        print(f"   {r[2]:<12} {r[0]}  ->  {r[1]}")

    if args.with_errors:
        bad = out.with_name(out.stem + "_with_errors" + out.suffix)
        write_xlsx(bad, {"P2P": build_broken(topology)})
        print(f"\nWrote {bad}  (deliberately broken — drop it in to see the review)")

    if args.with_devices:
        newkit = out.with_name(out.stem + "_new_devices" + out.suffix)
        tabs = build_new_devices(topology)
        write_xlsx(newkit, tabs)
        print(f"\nWrote {newkit}  (tabs: {', '.join(tabs)})")
        print("   tick 'הקובץ כולל רכיבים חדשים' before uploading this one")
        for r in tabs["Devices"][1:]:
            print(f"   {r[0]:<12} {r[1]:<8} {r[2]}U  zone={r[5]:<8} {r[6] or '(no label)'}")


if __name__ == "__main__":
    main()
