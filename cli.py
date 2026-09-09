#!/usr/bin/env python3
"""
cli.py  (v3)
============
Command-line access to the same engine the GUI uses — for scripting and for a
quick look without a browser. The graphical interface (server.py) is the main
way in, and deliberately does more than this does.

Cabinet ids are <POD><n>-<ROW><NN>: pods A1-A8, B1-B8, C1-C8, D1-D8, rows S and
N, positions 01-10. Port ids are <CABINET>:<DEVICE>:<PORT>.

Usage:
    python cli.py --rack A1-S05                        devices in a cabinet
    python cli.py --free A1-S05 --type fiber           free ports there
    python cli.py --src A1-S05:FIB-PP-01:12 --dst D5-N06:FIB-PP-01:3
    python cli.py --src ... --dst ... --domain A       force a redundancy leg

DELIBERATELY NOT HERE
---------------------
This reads the map and proposes one route. It does not execute, release,
truncate or extend anything, offer alternative routes, or plan a sheet — those
live in the interface, and every one of them writes, which is why they go
through store.py rather than straight at pathengine as this does. Read-only is
the whole reason this file can take the shortcut.
"""

import argparse
import sys
from pathlib import Path

import workorder
from pathengine import (load_topology, resolve_path, describe_port,
                        devices_by_rack, RouteError)


def _ports_of(T, dev):
    """A device's ports, addressed rather than searched for: a port id is
    <device>:<index>, so this costs the device's own ports instead of all
    120,256 on the map."""
    out = []
    for i in range(1, dev["fiber_ports"] + dev["copper_ports"] + 1):
        p = T["ports"].get(f"{dev['id']}:{i}")
        if p is not None:
            out.append(p)
    return out


def show_rack(T, rack):
    if rack not in T["racks"]:
        print(f"Unknown cabinet '{rack}'. They look like A1-S05: pods A1-A8, "
              f"B1-B8, C1-C8, D1-D8, rows S and N, positions 01-10.")
        return
    meta = T["racks"][rack]
    kind = " [EOR]" if meta["is_eor"] else ""
    if meta.get("is_mda"):
        kind = " [MDA hub]" if meta["is_eor"] else " [MDA pod, reserved]"
    print(f"{rack} — pod {meta['pod']}, row {meta['row']}, position {meta['position']}{kind}")
    devs = sorted(devices_by_rack(T).get(rack, ()), key=lambda d: -d["u_start"])
    for d in devs:
        ports = _ports_of(T, d)
        used = sum(1 for p in ports if p["status"] == "used")
        span = f"U{d['u_start']}" + (f"-U{d['u_start']-d['u_size']+1}" if d["u_size"] > 1 else "")
        print(f"  {span:<10} {d['name']:<12} {d['label']:<38} {used}/{len(ports)} ports used")


def show_free(T, rack, cable_type):
    if rack not in T["racks"]:
        print(f"Unknown cabinet '{rack}'. They look like A1-S05.")
        return
    free = [p for d in devices_by_rack(T).get(rack, ()) for p in _ports_of(T, d)
            if p["status"] == "free" and (not cable_type or p["type"] == cable_type)]
    print(f"{len(free)} free ports on {rack}" + (f" ({cable_type})" if cable_type else ""))
    for p in free[:30]:
        print(f"  {p['id']:<28} {p['type']}")
    if len(free) > 30:
        print(f"  … and {len(free) - 30} more")


def main():
    ap = argparse.ArgumentParser(description="Physical-layer route planner (sandbox).")
    ap.add_argument("--src")
    ap.add_argument("--dst")
    ap.add_argument("--domain", choices=["A", "B"])
    ap.add_argument("--rack", help="list the devices mounted in a rack")
    ap.add_argument("--free", metavar="RACK", help="list free ports on a rack")
    ap.add_argument("--type", choices=["fiber", "copper"], help="filter --free by cable type")
    ap.add_argument("--out", help="where to write the Work Order")
    args = ap.parse_args()

    T = load_topology()

    if args.rack:
        return show_rack(T, args.rack)
    if args.free:
        return show_free(T, args.free, args.type)
    if not args.src or not args.dst:
        ap.error("give --src and --dst, or use --rack / --free to explore")

    try:
        route = resolve_path(args.src, args.dst, domain=args.domain, topology=T)
    except RouteError as e:
        print(f"ROUTE FAILED: {e}", file=sys.stderr)
        sys.exit(1)

    text = workorder.render(route)
    print(text)

    out = Path(args.out) if args.out else (
        Path(__file__).parent / "output" /
        f"{args.src.replace(':','-')}__{args.dst.replace(':','-')}.txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"\n[saved to {out}]")


if __name__ == "__main__":
    main()
