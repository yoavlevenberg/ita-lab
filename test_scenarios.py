#!/usr/bin/env python3
"""
test_scenarios.py  (v3 — 4 columns x 8 pods + MDA hierarchy)
===============================================================
Checks the routing engine against the cases that actually matter in this
layout, plus verifies the generated dataset is internally consistent
(trunk usage really does equal the number of circuits crossing it, every
used port really does have a far end).
"""

import copy
import sys
from collections import Counter

from pathengine import load_topology, resolve_path, RouteError

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def free_port(topology, rack, cable_type):
    for pid, p in topology["ports"].items():
        if p["rack"] == rack and p["type"] == cable_type and p["status"] == "free":
            return pid
    raise RuntimeError(f"no free {cable_type} port on {rack}")


def main():
    T = load_topology()

    # ---------- dataset integrity ----------
    crossings = Counter()
    for c in T["circuits"].values():
        for sid in c["segment_ids"]:
            crossings[(sid, c["cable_type"])] += 1
    mismatch = sum(1 for e in T["edges"] for ct, d in e["cable_types"].items()
                   if d["used"] != crossings.get((e["id"], ct), 0))
    check("trunk usage matches the circuits crossing each trunk", mismatch == 0, f"{mismatch} mismatches")

    orphan = [p for p in T["ports"].values() if p["status"] == "used" and not p["circuit"]]
    check("every used port belongs to a circuit", not orphan, f"{len(orphan)} orphans")

    asym = [p for p in T["ports"].values()
            if p["role"] == "endpoint" and T["ports"][p["peer"]]["peer"] != p["id"]]
    check("endpoint ports point back at each other", not asym, f"{len(asym)} asymmetric")

    # ---------- layout ----------
    all_pods = list(T["pods"].keys())
    check("32 pods across 4 columns x 8 positions", len(all_pods) == 32, f"got {len(all_pods)}")

    mda_pods = sorted(p for p, m in T["pods"].items() if m["is_mda"])
    check("exactly the 4 specified pods are MDA", mda_pods == ["A2", "A6", "D2", "D6"], str(mda_pods))

    check("EOR/hub sits at S01 and N10 in every pod",
          all(T["racks"][f"{pod}-S01"]["is_eor"] and T["racks"][f"{pod}-N10"]["is_eor"]
              for pod in all_pods))
    check("every rack is 42U", all(r["u_height"] == 42 for r in T["racks"].values()))
    check("devices sit at real U positions with ports attached",
          all(1 <= d["u_start"] <= 42 for d in T["devices"].values())
          and all(T["ports"][p]["device"] in T["devices"] for p in T["ports"]))

    mda_has_compute = any(d["type"] == "server" for d in T["devices"].values()
                          if T["racks"][d["rack"]]["is_mda"])
    check("MDA pods contain zero compute (no server devices)", not mda_has_compute)

    served = {p for m in mda_pods for p in T["pods"][m]["serves"]}
    check("every regular pod is served by exactly one MDA",
          served == set(p for p in all_pods if p not in mda_pods), str(len(served)))
    check("each MDA serves exactly 7 regular pods",
          all(len(T["pods"][m]["serves"]) == 7 for m in mda_pods),
          str({m: len(T["pods"][m]["serves"]) for m in mda_pods}))

    # ---------- routing: inside one regular pod ----------
    r = resolve_path(free_port(T, "A1-S05", "fiber"), free_port(T, "A1-S08", "fiber"), topology=T)
    check("same-row fiber connects inside a pod", r["status"] == "ok")
    check("same-row path stays in one domain", len({s["domain"] for s in r["segments"]}) == 1)

    r = resolve_path(free_port(T, "A1-S04", "copper"), free_port(T, "A1-N04", "copper"), topology=T)
    check("cross-row copper connects inside the pod (dual-homed racks)", r["status"] == "ok")

    # ---------- routing: two regular pods sharing the same MDA (A1, A3 -> A2) ----------
    check("A1 and A3 are both served by MDA A2",
          T["pods"]["A1"]["mda"] == "A2" and T["pods"]["A3"]["mda"] == "A2")
    r = resolve_path(free_port(T, "A1-S05", "copper"), free_port(T, "A3-S05", "copper"), topology=T)
    check("copper works between two pods sharing one MDA (no backbone hop needed)", r["status"] == "ok")
    check("that route lands on the shared MDA hub", "A2-S01" in r["hop_racks"], str(r["hop_racks"]))

    # ---------- routing: two regular pods on different MDAs, same room ----------
    src, dst = free_port(T, "A1-S03", "fiber"), free_port(T, "A5-N06", "fiber")
    ra = resolve_path(src, dst, domain="A", topology=T)
    rb = resolve_path(src, dst, domain="B", topology=T)
    check("A1<->A5 (different MDAs, same room) works on domain A", ra["status"] == "ok")
    check("A1<->A5 (different MDAs, same room) works on domain B", rb["status"] == "ok")
    check("domain A crosses the room-internal A2<->A6 backbone",
          "A2-S01" in ra["hop_racks"] and "A6-S01" in ra["hop_racks"], str(ra["hop_racks"]))
    check("domain B crosses the room-internal A2<->A6 backbone",
          "A2-N10" in rb["hop_racks"] and "A6-N10" in rb["hop_racks"], str(rb["hop_racks"]))
    shared = {s["edge_id"] for s in ra["segments"]} & {s["edge_id"] for s in rb["segments"]}
    check("the A leg and the B leg share zero physical segments", not shared, str(shared))

    try:
        resolve_path(free_port(T, "A1-S03", "copper"), free_port(T, "A5-N06", "copper"), topology=T)
        check("copper between different-MDA pods is refused (no copper on MDA backbone)", False,
              "unexpectedly succeeded")
    except RouteError:
        check("copper between different-MDA pods is refused (no copper on MDA backbone)", True)

    # ---------- routing: cross-room (room AB -> room CD) ----------
    src, dst = free_port(T, "A1-S02", "fiber"), free_port(T, "D5-N07", "fiber")
    ra = resolve_path(src, dst, domain="A", topology=T)
    rb = resolve_path(src, dst, domain="B", topology=T)
    check("cross-room fiber works on domain A", ra["status"] == "ok")
    check("cross-room fiber works on domain B", rb["status"] == "ok")
    check("domain A crosses the room backbone via the position-2 MDAs",
          "A2-S01" in ra["hop_racks"] and "D2-S01" in ra["hop_racks"], str(ra["hop_racks"]))
    check("domain B crosses the room backbone via the position-6 MDAs",
          "A6-N10" in rb["hop_racks"] and "D6-N10" in rb["hop_racks"], str(rb["hop_racks"]))
    shared = {s["edge_id"] for s in ra["segments"]} & {s["edge_id"] for s in rb["segments"]}
    check("cross-room A leg and B leg share zero physical segments", not shared, str(shared))

    try:
        resolve_path(free_port(T, "A1-S02", "copper"), free_port(T, "D5-N07", "copper"), topology=T)
        check("cross-room copper is refused", False, "unexpectedly succeeded")
    except RouteError:
        check("cross-room copper is refused", True)

    # ---------- rejections ----------
    try:
        resolve_path(free_port(T, "A1-S02", "fiber"), free_port(T, "A1-S03", "copper"), topology=T)
        check("cable type mismatch is refused", False, "unexpectedly succeeded")
    except RouteError:
        check("cable type mismatch is refused", True)

    used = next(p["id"] for p in T["ports"].values() if p["status"] == "used")
    try:
        resolve_path(used, free_port(T, "B1-S05", "fiber"), topology=T)
        check("an already-patched port is refused", False, "unexpectedly succeeded")
    except RouteError:
        check("an already-patched port is refused", True)

    # ---------- capacity ----------
    T2 = copy.deepcopy(T)
    for e in T2["edges"]:
        if e["id"] == "A1-S06->A1-S01":
            e["cable_types"]["fiber"]["used"] = e["cable_types"]["fiber"]["capacity"]
    s6, d9 = free_port(T2, "A1-S06", "fiber"), free_port(T2, "A1-S09", "fiber")
    try:
        resolve_path(s6, d9, domain="A", topology=T2)
        check("a full trunk blocks its own domain", False, "unexpectedly succeeded")
    except RouteError:
        check("a full trunk blocks its own domain", True)
    fb = resolve_path(s6, d9, topology=T2)
    check("auto mode falls back to the other domain when one is full",
          fb["domain"] == "B", f"got {fb['domain']}")

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} checks passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
