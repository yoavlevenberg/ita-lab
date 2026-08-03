#!/usr/bin/env python3
"""
cli.py  (v2)
============
Command-line access to the same engine the GUI uses. Handy for scripting and
for bulk work later; the graphical interface (server.py) is the main way in.

Port ids look like:   <RACK>:<DEVICE>:<PORT>     e.g.  A-S05:FIB-PP-01:12

Usage:
    python cli.py --rack A-S05                       list devices in a rack
    python cli.py --free A-S05 --type fiber          list free ports
    python cli.py --src A-S05:FIB-PP-01:12 --dst B-N06:FIB-PP-01:3
    python cli.py --src ... --dst ... --domain A
"""

import argparse
import sys
from pathlib import Path

import workorder
from pathengine import load_topology, resolve_path, describe_port, RouteError


def show_rack(T, rack):
    if rack not in T["racks"]:
        print(f"Unknown rack '{rack}'. Racks look like A-S01 … A-S10, A-N01 … A-N10 (same for pod B).")
        return
    meta = T["racks"][rack]
    print(f"{rack} — pod {meta['pod']}, row {meta['row']}, position {meta['position']}"
          + (" [EOR]" if meta["is_eor"] else ""))
    devs = sorted((d for d in T["devices"].values() if d["rack"] == rack),
                  key=lambda d: -d["u_start"])
    for d in devs:
        ports = [p for p in T["ports"].values() if p["device"] == d["id"]]
        used = sum(1 for p in ports if p["status"] == "used")
        span = f"U{d['u_start']}" + (f"-U{d['u_start']-d['u_size']+1}" if d["u_size"] > 1 else "")
        print(f"  {span:<10} {d['name']:<12} {d['label']:<38} {used}/{len(ports)} ports used")


def show_free(T, rack, cable_type):
    free = [p for p in T["ports"].values()
            if p["rack"] == rack and p["status"] == "free"
            and (not cable_type or p["type"] == cable_type)]
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
