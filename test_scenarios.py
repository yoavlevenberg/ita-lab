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
import re
import sys
from collections import Counter

import assistant
import bulkplan
import placement
import xlsxreader
import serials
import wo_html
import zones
from pathengine import (load_topology, resolve_path,
                        resolve_route_options, RouteError)

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

    # ---------- trunk strands are real, individually-owned ports ----------
    strand_mismatch = sum(1 for e in T["edges"] for ct, d in e["cable_types"].items()
                          if len(d.get("strands") or {}) != d["used"])
    check("every trunk's occupied-strand list matches its used count",
          strand_mismatch == 0, f"{strand_mismatch} mismatches")

    overflow = [f"{e['id']}/{ct}" for e in T["edges"] for ct, d in e["cable_types"].items()
                if any(not (1 <= int(i) <= d["capacity"]) for i in (d.get("strands") or {}))]
    check("no strand number falls outside its trunk's capacity", not overflow, str(overflow[:3]))

    # a strand is one continuous piece of glass: it may belong to ONE circuit
    # only, and that circuit must agree it is using it. Identity includes the
    # media — fiber strand #1 and copper pair #1 of the same trunk are
    # different physical things and legitimately coexist.
    claimed = Counter()
    for c in T["circuits"].values():
        for s in c["strands"]:
            claimed[(s["edge_id"], s["cable_type"], s["strand_index"])] += 1
    check("no strand is claimed by two circuits", all(v == 1 for v in claimed.values()),
          f"{sum(1 for v in claimed.values() if v > 1)} double-booked")

    disagree = 0
    for e in T["edges"]:
        for ct, d in e["cable_types"].items():
            for idx, cid in (d.get("strands") or {}).items():
                circuit = T["circuits"].get(cid)
                if not circuit or not any(s["edge_id"] == e["id"] and s["cable_type"] == ct
                                          and s["strand_index"] == int(idx)
                                          for s in circuit["strands"]):
                    disagree += 1
    check("every occupied strand is acknowledged by the circuit holding it",
          disagree == 0, f"{disagree} disagreements")

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
            ct = e["cable_types"]["fiber"]
            # a trunk is full when every numbered strand is patched, not when
            # a counter says so — occupy them all explicitly
            ct["strands"] = {str(i): "CIR-TEST" for i in range(1, ct["capacity"] + 1)}
            ct["used"] = ct["capacity"]
    s6, d9 = free_port(T2, "A1-S06", "fiber"), free_port(T2, "A1-S09", "fiber")
    try:
        resolve_path(s6, d9, domain="A", topology=T2)
        check("a full trunk blocks its own domain", False, "unexpectedly succeeded")
    except RouteError:
        check("a full trunk blocks its own domain", True)
    fb = resolve_path(s6, d9, topology=T2)
    check("auto mode falls back to the other domain when one is full",
          fb["domain"] == "B", f"got {fb['domain']}")

    # ---------- alternative options must not quote the same resources ----------
    # Options are shown side by side and printed into separate Work Orders. When
    # two of them overlap on a trunk they must still name DIFFERENT strands and
    # different cross-connect ports, or both sheets would send a technician to
    # the same physical fibre.
    src, dst = free_port(T, "A1-S05", "fiber"), free_port(T, "D5-N06", "fiber")
    opts = resolve_route_options(src, dst, count=4, topology=T)
    check("more than one alternative is offered", len(opts) >= 2, f"got {len(opts)}")

    strand_claims = Counter((s["edge_id"], s["strand_index"])
                            for o in opts for s in o["segments"])
    check("no two options quote the same trunk strand",
          all(v == 1 for v in strand_claims.values()),
          f"{sum(1 for v in strand_claims.values() if v > 1)} strands double-quoted")

    port_claims = Counter(t["port"] for o in opts for t in o["transit_points"])
    check("no two options quote the same cross-connect port",
          all(v == 1 for v in port_claims.values()),
          f"{sum(1 for v in port_claims.values() if v > 1)} ports double-quoted")

    check("the first two options are fully disjoint",
          not opts[1]["shared_segments"], str(opts[1]["shared_segments"][:3]))

    # ---------- hop count outranks cable length ----------
    # Distances in this synthetic map are assumptions; hops are real work. The
    # chosen route must never be beaten on hop count by a longer alternative.
    for a, b in (("A1-S05", "D5-N06"), ("A1-S03", "B7-N02"), ("A3-S04", "C5-N08")):
        got = resolve_route_options(free_port(T, a, "fiber"), free_port(T, b, "fiber"),
                                    count=3, topology=T)
        best = min(len(o["segments"]) for o in got)
        check(f"{a}->{b} takes a minimum-hop route",
              len(got[0]["segments"]) == best,
              f"chose {len(got[0]['segments'])} hops when {best} was available")

    # ---------- preflight review ----------
    # The review runs before anything is planned, so it has to predict the plan
    # exactly: a row it passes must plan, a row it fails must not. If the two
    # disagree the review is worse than useless — it teaches you to ignore it.
    def demand(row, src, dst, malformed=""):
        return {"row": row, "src": src, "dst": dst, "group": "", "malformed": malformed}

    fa = [p for p, d in T["ports"].items()
          if d["rack"] == "A1-S05" and d["type"] == "fiber" and d["status"] == "free"][:4]
    fb = [p for p, d in T["ports"].items()
          if d["rack"] == "D5-N06" and d["type"] == "fiber" and d["status"] == "free"][:4]
    cu = next(p for p, d in T["ports"].items()
              if d["rack"] == "A1-S05" and d["type"] == "copper" and d["status"] == "free")
    taken = next(p for p, d in T["ports"].items()
                 if d["rack"] == "A1-S05" and d["status"] == "used")

    sheet = [
        demand(2, fa[0], fb[0]),                       # fine
        demand(3, fa[1], fb[1]),                       # fine
        demand(4, fa[0], fb[2]),                       # reuses row 2's source
        demand(5, "NO:SUCH:PORT", fb[2]),              # unknown port
        demand(6, taken, fb[2]),                       # already patched
        demand(7, cu, fb[2]),                          # fiber <-> copper
        demand(8, fa[2], fa[2]),                       # src == dst
        demand(9, fa[3], "", "no destination port in this row"),
    ]
    review = bulkplan.validate(T, sheet)
    check("preflight passes only the two clean rows",
          review["ok"] == 2 and review["problems"] == 6,
          f"ok={review['ok']} problems={review['problems']}")
    kinds = {i["row"]: i["kind"] for i in review["issues"]}
    check("preflight names each fault correctly",
          kinds == {4: "duplicate_port", 5: "unknown_port", 6: "port_in_use",
                    7: "cable_mismatch", 8: "same_port", 9: "incomplete"},
          str(kinds))
    check("preflight blames the LATER row of a duplicated port", 2 not in kinds, str(kinds))

    planned = bulkplan.plan(T, sheet)
    check("the plan agrees with the preflight, row for row",
          planned["summary"]["planned"] == review["ok"],
          f"review said {review['ok']} would work, plan produced "
          f"{planned['summary']['planned']}")

    # ---------- printable work order ----------
    wo = wo_html.render(planned["results"][0]["route"])
    check("printable Work Order is a complete HTML document",
          wo.startswith("<!doctype html>") and wo.rstrip().endswith("</html>"))
    check("printable Work Order leaves no unfilled placeholders",
          not re.findall(r"\{[a-z_]+\}", re.sub(r"<style>.*?</style>", "", wo, flags=re.S)))
    check("printable Work Order names every strand it reserves",
          all(f"#{s['strand_index']}" in wo
              for s in planned["results"][0]["route"]["segments"]))
    check("an unapproved Work Order is marked PROPOSED",
          "PROPOSED" in wo and "EXECUTED" not in wo)

    # ---------- a file that is not a spreadsheet is explained, not fatal ----------
    # Every entry point must go through the same guard: when only one did, the
    # other let a raw BadZipFile escape and killed the request instead of
    # reporting the problem.
    import io as _io
    for probe in (b"\x01\x02\x03", b"hello, not a spreadsheet"):
        for call in (lambda b: xlsxreader.sheet_names(_io.BytesIO(b)),
                     lambda b: xlsxreader.read_sheet(_io.BytesIO(b)),
                     lambda b: bulkplan.read_demand_sheet(_io.BytesIO(b))):
            try:
                call(probe)
                check("a non-spreadsheet upload is reported, not raised", False,
                      "no error at all")
            except xlsxreader.XlsxError:
                pass
            except Exception as exc:
                check("a non-spreadsheet upload is reported, not raised", False,
                      f"leaked {type(exc).__name__}")
                break
    check("a non-spreadsheet upload is reported, not raised", True)

    # ---------- serials ----------
    all_serials = [d.get("serial") for d in T["devices"].values()]
    check("every device carries a serial", all(all_serials), "some are missing")
    check("serials are unique across the whole map",
          len(set(all_serials)) == len(all_serials),
          f"{len(all_serials) - len(set(all_serials))} duplicated")
    a_dev = T["devices"]["A1-S05:TOR-SW-01"]
    check("serials are stable across a rebuild",
          serials.serial_for("A1-S05:TOR-SW-01") == a_dev["serial"])
    check("a serial typed loosely still resolves",
          serials.normalise(" sn_" + a_dev["serial"][3:].lower() + " ") == a_dev["serial"])

    # ---------- intra-rack patching ----------
    # Two ports in one cabinet used to be refused. It is now the cheapest
    # connection there is, and the placement planner exists to produce it.
    in_rack, by_device = [], {}
    for pid, p in T["ports"].items():
        if p["rack"] == "A1-S05" and p["type"] == "copper" and p["status"] == "free":
            by_device.setdefault(p["device"], pid)
    in_rack = list(by_device.values())[:2]
    local = resolve_route_options(in_rack[0], in_rack[1], topology=T)[0]
    check("two ports in one cabinet can be patched together", local["status"] == "ok")
    check("an intra-rack patch consumes no trunk strand", not local["segments"])
    check("an intra-rack patch needs no cross-connect", not local["transit_points"])
    check("an intra-rack patch is not labelled A or B", local["domain"] == "local")
    try:
        resolve_route_options(in_rack[0], in_rack[0], topology=T)
        check("a port still cannot be patched to itself", False, "unexpectedly succeeded")
    except RouteError:
        check("a port still cannot be patched to itself", True)

    # ---------- free U space ----------
    # u_start is the TOP of a device and it grows downward. Getting that
    # backwards would silently double-book space, so it is checked directly.
    gaps = placement.free_gaps(T, "A1-S05")
    occupied = placement.occupancy(T, "A1-S05")
    from_gaps = {u for top, h in gaps for u in range(top - h + 1, top + 1)}
    check("free gaps and occupancy account for all 42U",
          from_gaps | occupied == set(range(1, 43)) and not (from_gaps & occupied),
          f"{len(from_gaps)} free + {len(occupied)} used")

    # ---------- equipment is never racked in EOR or MDA cabinets ----------
    # Those are cross-connect real estate, so they must not be offered at all
    # — not merely ranked low, which would still put them in front of a user.
    allowed = placement.eligible_racks(T)
    check("no EOR cabinet is offered as a position",
          not [r for r in allowed if T["racks"][r]["is_eor"]],
          str([r for r in allowed if T["racks"][r]["is_eor"]][:3]))
    check("no cabinet inside an MDA pod is offered",
          not [r for r in allowed if T["racks"][r]["is_mda"]],
          str([r for r in allowed if T["racks"][r]["is_mda"]][:3]))
    check("the compute cabinets are all still available",
          len(allowed) == sum(1 for m in T["racks"].values()
                              if not m["is_eor"] and not m["is_mda"]),
          f"{len(allowed)} offered")

    # even when the thing it connects to lives in an EOR, the device goes to a
    # compute cabinet rather than joining it
    eor_target = {"serial": "SN-EORTEST", "type": "switch", "raw_type": "switch",
                  "u_size": 1, "fiber_ports": 4, "copper_ports": 4, "label": "x"}
    near_eor = placement.rank_positions(T, eor_target, ["A1-S01"], limit=3)
    check("a device connecting into an EOR is still racked outside it",
          all(not T["racks"][c["rack"]]["is_eor"] for c in near_eor),
          str([c["rack"] for c in near_eor]))

    # ---------- placing new equipment ----------
    tor_serial = a_dev["serial"]
    specs = [
        {"row": 2, "serial": "SN-TESTSW", "type": "switch", "raw_type": "switch",
         "u_size": 1, "fiber_ports": 4, "copper_ports": 48, "label": "Leaf"},
        {"row": 3, "serial": "SN-TESTSRV1", "type": "server", "raw_type": "server",
         "u_size": 2, "fiber_ports": 2, "copper_ports": 2, "label": "Server"},
        {"row": 4, "serial": "SN-TESTSRV2", "type": "server", "raw_type": "server",
         "u_size": 2, "fiber_ports": 2, "copper_ports": 2, "label": "Server"},
    ]
    wiring = [
        {"row": 2, "src": "SN-TESTSW", "dst": tor_serial, "group": "", "cable": "fiber",
         "malformed": ""},
        {"row": 3, "src": "SN-TESTSRV1", "dst": "SN-TESTSW", "group": "", "cable": "copper",
         "malformed": ""},
        {"row": 4, "src": "SN-TESTSRV2", "dst": "SN-TESTSW", "group": "", "cable": "copper",
         "malformed": ""},
    ]
    built = bulkplan.plan(T, wiring, new_devices=specs)
    sited = {s["serial"]: s for s in built["siting"]["placements"]}
    check("every declared device gets a position",
          all(s["status"] == "ok" for s in sited.values()),
          str([s.get("reason") for s in sited.values() if s["status"] != "ok"]))
    check("a new device is racked beside what it connects to",
          sited["SN-TESTSW"]["rack"] == a_dev["rack"],
          f"went to {sited['SN-TESTSW']['rack']} instead of {a_dev['rack']}")

    # two new devices must never be handed the same U
    spans = []
    for s in sited.values():
        if s["status"] == "ok" and s["rack"] == a_dev["rack"]:
            spans.append(set(range(s["u_end"], s["u_start"] + 1)))
    overlap = any(x & y for i, x in enumerate(spans) for y in spans[i + 1:])
    check("placements never overlap each other in the rack", not overlap, str(spans))

    free_now = {u for top, h in placement.free_gaps(T, a_dev["rack"])
                for u in range(top - h + 1, top + 1)}
    check("placements only use space that was actually free",
          all(span <= free_now for span in spans))

    check("a device chained off another new device still gets cabled",
          built["summary"]["planned"] == 3,
          f"planned {built['summary']['planned']} of 3")
    check("connections inside the chosen cabinet need no trunk",
          all(r["hops"] == 0 for r in built["results"] if r["status"] == "ok"))

    # the servers must land on DIFFERENT ports of the new switch
    switch_ports = [r["dst"] for r in built["results"]
                    if r["status"] == "ok" and "TESTSW" in r["dst"]]
    check("two devices on one switch take different ports",
          len(switch_ports) == len(set(switch_ports)), str(switch_ports))

    # ---------- the Devices tab is checked before anything is sited ----------
    bad_specs = [
        {"row": 2, "serial": "SN-DUP", "type": "switch", "raw_type": "switch",
         "u_size": 1, "fiber_ports": 2, "copper_ports": 0, "label": "a"},
        {"row": 3, "serial": "SN-DUP", "type": "switch", "raw_type": "switch",
         "u_size": 1, "fiber_ports": 2, "copper_ports": 0, "label": "b"},
        {"row": 4, "serial": tor_serial, "type": "server", "raw_type": "server",
         "u_size": 2, "fiber_ports": 1, "copper_ports": 1, "label": "c"},
        {"row": 5, "serial": "SN-HUGE", "type": "server", "raw_type": "server",
         "u_size": 99, "fiber_ports": 1, "copper_ports": 1, "label": "d"},
        {"row": 6, "serial": "SN-NOPORT", "type": "server", "raw_type": "server",
         "u_size": 1, "fiber_ports": 0, "copper_ports": 0, "label": "e"},
        {"row": 7, "serial": "SN-ODD", "type": "toaster", "raw_type": "toaster",
         "u_size": 1, "fiber_ports": 1, "copper_ports": 0, "label": "f"},
    ]
    dv = bulkplan.validate_devices(T, bad_specs, [])
    got = {i["row"]: i["kind"] for i in dv["issues"]}
    check("the Devices tab names each fault correctly",
          got == {3: "duplicate_serial", 4: "serial_exists", 5: "bad_u_size",
                  6: "no_ports", 7: "unknown_type"}, str(got))

    cyc_specs = [
        {"row": 2, "serial": "SN-CA", "type": "switch", "raw_type": "switch",
         "u_size": 1, "fiber_ports": 4, "copper_ports": 0, "label": "a"},
        {"row": 3, "serial": "SN-CB", "type": "switch", "raw_type": "switch",
         "u_size": 1, "fiber_ports": 4, "copper_ports": 0, "label": "b"},
    ]
    cyc_wiring = [
        {"row": 2, "src": "SN-CA", "dst": "SN-CB", "group": "", "cable": "fiber", "malformed": ""},
        {"row": 3, "src": "SN-CB", "dst": "SN-CA", "group": "", "cable": "fiber", "malformed": ""},
    ]
    cv = bulkplan.validate_devices(T, cyc_specs, cyc_wiring)
    check("two new devices that only reference each other are refused",
          {i["kind"] for i in cv["issues"]} == {"dependency_cycle"},
          str([i["kind"] for i in cv["issues"]]))

    # ---------- a serial in a port column needs a cable type ----------
    no_media = [{"row": 2, "src": "SN-TESTSW", "dst": tor_serial, "group": "",
                 "cable": "", "malformed": ""}]
    mv = bulkplan.validate(T, no_media, specs)
    check("a row naming a device by serial must say which media",
          [i["kind"] for i in mv["issues"]] == ["no_cable_type"],
          str([i["kind"] for i in mv["issues"]]))

    # ---------- planner instructions are real constraints ----------
    # "not in D5" has to remove D5 from the ranking, not just be acknowledged.
    plain = placement.rank_positions(T, eor_target, ["A1-S05"], limit=1)[0]
    check("without instructions the device lands beside its target",
          plain["rack"] == "A1-S05", plain["rack"])

    avoided = placement.rank_positions(T, eor_target, ["A1-S05"], limit=3,
                                       constraints={"avoid_racks": {"A1-S05"}})
    check("an avoided cabinet disappears from the ranking",
          all(c["rack"] != "A1-S05" for c in avoided), str([c["rack"] for c in avoided]))

    pod_avoided = placement.rank_positions(T, eor_target, ["A1-S05"], limit=3,
                                           constraints={"avoid_pods": {"A1"}})
    check("avoiding a pod removes every cabinet in it",
          all(not c["rack"].startswith("A1-") for c in pod_avoided),
          str([c["rack"] for c in pod_avoided]))

    preferred = placement.rank_positions(T, eor_target, ["A1-S05"], limit=1,
                                         constraints={"prefer_pods": {"A5"}})[0]
    check("an explicit preference outranks the automatic criteria",
          preferred["pod"] == "A5", f"chose {preferred['rack']}")

    # a preference for somewhere ineligible must not strand the device
    safe = placement.rank_positions(T, eor_target, ["A1-S05"], limit=1,
                                    constraints={"prefer_racks": {"A2-S01"}})[0]
    check("preferring an ineligible cabinet is ignored, not fatal",
          safe["rack"] == "A1-S05", safe["rack"])

    try:
        placement.rank_positions(T, eor_target, ["A1-S05"],
                                 constraints={"avoid_rooms": {"AB", "CD"}})
        check("instructions that exclude everything say so", False, "no error raised")
    except placement.PlacementError as e:
        check("instructions that exclude everything say so", "relax" in str(e), str(e))

    # ---------- the assistant answers from the plan, never from guesswork ----------
    ctx = {"siting": built["siting"]["placements"], "results": built["results"],
           "summary": built["summary"]}

    said = assistant.respond("למה הרכיב הוצב שם?", plan=ctx, topology=T)
    check("the assistant explains a placement with its real rack",
          sited["SN-TESTSW"]["rack"] in said["text"], said["text"][:80])
    check("and names the runner-up it beat", "המועמד הבא" in said["text"])

    where = assistant.respond(f"איפה {a_dev['serial']}", plan=ctx, topology=T)
    check("the assistant locates a device by serial",
          a_dev["rack"] in where["text"] and f"U{a_dev['u_start']}" in where["text"],
          where["text"][:90])

    unknown = assistant.respond("איפה SN-DOESNOTEXIST", plan=ctx, topology=T)
    check("an unknown serial is reported, not invented",
          "לא קיים" in unknown["text"], unknown["text"][:80])

    told = assistant.respond("אל תשתמש ב-A1-S05", plan=ctx, topology=T)
    check("an instruction becomes a real constraint",
          told.get("replan") and "A1-S05" in told["constraints"]["avoid_racks"],
          str(told.get("constraints")))

    kept = assistant.respond("בלי פוד D5", plan=ctx, topology=T,
                             constraints=told["constraints"])
    check("instructions accumulate rather than replace each other",
          "A1-S05" in kept["constraints"]["avoid_racks"]
          and "D5" in kept["constraints"]["avoid_pods"],
          str(kept["constraints"]))

    wiped = assistant.respond("נקה דגשים", plan=ctx, topology=T,
                              constraints=kept["constraints"])
    check("clearing removes every instruction",
          not any(wiped["constraints"].values()), str(wiped["constraints"]))

    vague = assistant.respond("אל תשתמש", plan=ctx, topology=T)
    check("an instruction naming nothing asks for a target, and changes nothing",
          "constraints" not in vague, str(vague.get("constraints")))

    check("an unrecognised question is admitted, not answered anyway",
          assistant.respond("מה מזג האוויר", plan=ctx, topology=T)["intent"] == "unknown")

    # ---------- colour zones ----------
    # Every pod is in exactly one zone, except MDA pods which are in none.
    zmap = zones.summary(T)
    check("every pod has a zone decided for it", set(zmap) == set(T["pods"]),
          f"{len(zmap)} of {len(T['pods'])}")
    check("MDA pods are neutral — no colour at all",
          all(zmap[p] is None for p in T["pods"] if T["pods"][p]["is_mda"]),
          str({p: zmap[p] for p in T["pods"] if T["pods"][p]["is_mda"]}))
    check("every other pod has exactly one colour",
          all(zmap[p] in zones.NAMED_ZONES or zmap[p] == zones.DEFAULT_ZONE
              for p in T["pods"] if not T["pods"][p]["is_mda"]))
    twice = [p for p in T["pods"]
             if sum(p in pods for pods in zones.NAMED_ZONES.values()) > 1]
    check("no pod is claimed by two colours", not twice, str(twice))
    ghosts = sorted({p for pods in zones.NAMED_ZONES.values() for p in pods}
                    - set(T["pods"]))
    check("no colour names a pod that does not exist", not ghosts, str(ghosts))
    check("no colour contains an MDA pod",
          not [p for pods in zones.NAMED_ZONES.values() for p in pods
               if T["pods"].get(p, {}).get("is_mda")])

    check("a zone name is accepted in Hebrew and English",
          zones.resolve("ירוק") == "green" and zones.resolve(" BLUE ") == "blue")
    check("a blank zone means no restriction", zones.resolve("") is None)
    try:
        zones.resolve("סגול")
        check("an unknown colour is refused", False, "accepted it")
    except zones.ZoneError:
        check("an unknown colour is refused", True)

    # the boundary is hard: a device of one colour never lands in another,
    # even when the thing it connects to lives there
    blue_rack = next(r for r, m in T["racks"].items()
                     if zmap.get(m["pod"]) == "blue" and not m["is_eor"])
    for colour in ("green", "white", "yellow"):
        spec = {"serial": f"SN-Z{colour}", "type": "server", "u_size": 2,
                "fiber_ports": 2, "copper_ports": 2, "zone": colour}
        ranked = placement.rank_positions(T, spec, [blue_rack], limit=5)
        check(f"a {colour} device stays inside the {colour} zone",
              all(zmap[c["pod"]] == colour for c in ranked),
              str([(c["rack"], zmap[c["pod"]]) for c in ranked[:3]]))

    unzoned = {"serial": "SN-ZFREE", "type": "server", "u_size": 2,
               "fiber_ports": 2, "copper_ports": 2}
    check("a device with no zone is free to sit beside its target",
          placement.rank_positions(T, unzoned, [blue_rack], limit=1)[0]["rack"] == blue_rack)

    # a zone that cannot fit the device says which zone, not just "no space"
    try:
        placement.rank_positions(T, {"serial": "SN-ZBIG", "type": "server",
                                     "u_size": 42, "fiber_ports": 1,
                                     "copper_ports": 1, "zone": "white"},
                                 [blue_rack])
        check("a zone with no room names itself in the error", False, "no error")
    except placement.PlacementError as e:
        check("a zone with no room names itself in the error", "white" in str(e), str(e))

    # ---------- a bad zone in the sheet is caught before planning ----------
    zone_specs = [
        {"row": 2, "serial": "SN-ZOK", "type": "server", "raw_type": "server",
         "u_size": 2, "fiber_ports": 1, "copper_ports": 1, "label": "a",
         "raw_zone": "ירוק"},
        {"row": 3, "serial": "SN-ZBADCOLOUR", "type": "server", "raw_type": "server",
         "u_size": 2, "fiber_ports": 1, "copper_ports": 1, "label": "b",
         "raw_zone": "purple"},
    ]
    zv = bulkplan.validate_devices(T, zone_specs, [])
    check("an unrecognised zone is reported against its row",
          [(i["row"], i["kind"]) for i in zv["issues"]] == [(3, "unknown_zone")],
          str([(i["row"], i["kind"]) for i in zv["issues"]]))

    # and the sheet's zone actually reaches the planner, even if validation
    # never ran first
    fresh = [{"row": 2, "serial": "SN-ZFRESH", "type": "server", "raw_type": "server",
              "u_size": 2, "fiber_ports": 1, "copper_ports": 1, "label": "c",
              "raw_zone": "לבן"}]
    fresh_wiring = [{"row": 2, "src": "SN-ZFRESH", "dst": free_port(T, blue_rack, "fiber"),
                     "group": "", "cable": "fiber", "malformed": ""}]
    fp = bulkplan.plan(T, fresh_wiring, new_devices=fresh)
    landed = fp["siting"]["placements"][0]
    check("a zone written in the sheet is honoured without validation running first",
          landed["status"] == "ok" and zmap[landed["rack"].split("-")[0]] == "white",
          f"{landed.get('rack')} -> {zmap.get(str(landed.get('rack')).split('-')[0])}")

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} checks passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
