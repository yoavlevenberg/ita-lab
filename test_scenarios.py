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
import test_agreement
import fuzzy
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


def _zone_error(text):
    try:
        zones.resolve(text)
        return ""
    except zones.ZoneError as e:
        return str(e)


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

    # A truncated circuit deliberately has one end and no peer, so symmetry is
    # asserted only where a peer is claimed — and separately, that a peerless
    # endpoint belongs to a circuit that admits it is partial, so this cannot
    # quietly excuse a genuinely broken pair.
    asym = [p for p in T["ports"].values()
            if p["role"] == "endpoint" and p["peer"]
            and T["ports"][p["peer"]]["peer"] != p["id"]]
    check("endpoint ports point back at each other", not asym, f"{len(asym)} asymmetric")

    loose = [p for p in T["ports"].values()
             if p["role"] in ("endpoint", "open_end") and not p["peer"]
             and not (T["circuits"].get(p["circuit"]) or {}).get("partial")]
    check("a peerless endpoint only exists on a circuit marked partial",
          not loose, f"{len(loose)} loose ends")

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

    # Asking for more alternatives than physically exist must return the ones
    # that do. The options hold spare capacity while they are being offered, so
    # a later one can find the last strand on a trunk already taken by an
    # earlier one — that is "no further alternative", not "this request failed",
    # and it must not throw away the options already found.
    greedy = resolve_route_options(src, dst, count=40, topology=T)
    check("asking for far more options than exist returns the ones that do",
          1 <= len(greedy) <= 40, f"got {len(greedy)}")
    check("every option returned under pressure is still a complete route",
          all(o["segments"] and o["src_port"] == src and o["dst_port"] == dst
              for o in greedy))
    claims = Counter((s["edge_id"], s["strand_index"])
                     for o in greedy for s in o["segments"])
    check("options offered under pressure still quote distinct strands",
          all(v == 1 for v in claims.values()),
          f"{sum(1 for v in claims.values() if v > 1)} double-quoted")

    # A port pair with genuinely no route must still raise, rather than coming
    # back as an empty list the caller has to guess about.
    try:
        resolve_route_options(free_port(T, "A1-S05", "fiber"),
                              free_port(T, "A1-S05", "copper"), topology=T)
        check("a media mismatch is still refused outright", False, "no error raised")
    except RouteError:
        check("a media mismatch is still refused outright", True)

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
          serials.normalise("  S/N " + a_dev["serial"] + "  ") == a_dev["serial"]
          and serials.parse(a_dev["serial"] + ":7") == (a_dev["serial"], 7),
          serials.normalise("  S/N " + a_dev["serial"] + "  "))

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
    eor_target = {"serial": "9900000000", "type": "switch", "raw_type": "switch",
                  "u_size": 1, "fiber_ports": 4, "copper_ports": 4, "label": "x"}
    near_eor = placement.rank_positions(T, eor_target, ["A1-S01"], limit=3)
    check("a device connecting into an EOR is still racked outside it",
          all(not T["racks"][c["rack"]]["is_eor"] for c in near_eor),
          str([c["rack"] for c in near_eor]))

    # ---------- placing new equipment ----------
    # Chain onto a switch that still has a free fibre port. Hard-coding one
    # made the suite pass only on a pristine map: after a real execution the
    # obvious candidate can be full, and the failure then looks like a bug in
    # the planner rather than a used-up fixture.
    uplink = next((d for d in T["devices"].values()
                   if d["type"] == "switch" and not d["rack"].endswith(("S01", "N10"))
                   and any(T["ports"][f"{d['id']}:{i}"]["status"] == "free"
                           for i in range(1, d["fiber_ports"] + 1))),
                  a_dev)
    tor_serial = uplink["serial"]
    specs = [
        {"row": 2, "serial": "9900000001", "type": "switch", "raw_type": "switch",
         "u_size": 1, "fiber_ports": 4, "copper_ports": 48, "label": "Leaf"},
        {"row": 3, "serial": "9900000002", "type": "server", "raw_type": "server",
         "u_size": 2, "fiber_ports": 2, "copper_ports": 2, "label": "Server"},
        {"row": 4, "serial": "9900000003", "type": "server", "raw_type": "server",
         "u_size": 2, "fiber_ports": 2, "copper_ports": 2, "label": "Server"},
    ]
    wiring = [
        {"row": 2, "src": "9900000001", "dst": tor_serial, "group": "", "cable": "fiber",
         "malformed": ""},
        {"row": 3, "src": "9900000002", "dst": "9900000001", "group": "", "cable": "copper",
         "malformed": ""},
        {"row": 4, "src": "9900000003", "dst": "9900000001", "group": "", "cable": "copper",
         "malformed": ""},
    ]
    built = bulkplan.plan(T, wiring, new_devices=specs)
    sited = {s["serial"]: s for s in built["siting"]["placements"]}
    check("every declared device gets a position",
          all(s["status"] == "ok" for s in sited.values()),
          str([s.get("reason") for s in sited.values() if s["status"] != "ok"]))
    check("a new device is racked beside what it connects to",
          sited["9900000001"]["rack"] == uplink["rack"],
          f"went to {sited['9900000001']['rack']} instead of {uplink['rack']}")

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
        {"row": 2, "serial": "9900000004", "type": "switch", "raw_type": "switch",
         "u_size": 1, "fiber_ports": 2, "copper_ports": 0, "label": "a"},
        {"row": 3, "serial": "9900000004", "type": "switch", "raw_type": "switch",
         "u_size": 1, "fiber_ports": 2, "copper_ports": 0, "label": "b"},
        {"row": 4, "serial": tor_serial, "type": "server", "raw_type": "server",
         "u_size": 2, "fiber_ports": 1, "copper_ports": 1, "label": "c"},
        {"row": 5, "serial": "9900000005", "type": "server", "raw_type": "server",
         "u_size": 99, "fiber_ports": 1, "copper_ports": 1, "label": "d"},
        {"row": 6, "serial": "9900000006", "type": "server", "raw_type": "server",
         "u_size": 1, "fiber_ports": 0, "copper_ports": 0, "label": "e"},
        {"row": 7, "serial": "9900000007", "type": "toaster", "raw_type": "toaster",
         "u_size": 1, "fiber_ports": 1, "copper_ports": 0, "label": "f"},
    ]
    dv = bulkplan.validate_devices(T, bad_specs, [])
    got = {i["row"]: i["kind"] for i in dv["issues"]}
    check("the Devices tab names each fault correctly",
          got == {3: "duplicate_serial", 4: "serial_exists", 5: "bad_u_size",
                  6: "no_ports", 7: "unknown_type"}, str(got))

    cyc_specs = [
        {"row": 2, "serial": "9900000008", "type": "switch", "raw_type": "switch",
         "u_size": 1, "fiber_ports": 4, "copper_ports": 0, "label": "a"},
        {"row": 3, "serial": "9900000009", "type": "switch", "raw_type": "switch",
         "u_size": 1, "fiber_ports": 4, "copper_ports": 0, "label": "b"},
    ]
    cyc_wiring = [
        {"row": 2, "src": "9900000008", "dst": "9900000009", "group": "", "cable": "fiber", "malformed": ""},
        {"row": 3, "src": "9900000009", "dst": "9900000008", "group": "", "cable": "fiber", "malformed": ""},
    ]
    cv = bulkplan.validate_devices(T, cyc_specs, cyc_wiring)
    check("two new devices that only reference each other are refused",
          {i["kind"] for i in cv["issues"]} == {"dependency_cycle"},
          str([i["kind"] for i in cv["issues"]]))

    # ---------- a serial in a port column needs a cable type ----------
    no_media = [{"row": 2, "src": "9900000001", "dst": tor_serial, "group": "",
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
          sited["9900000001"]["rack"] in said["text"], said["text"][:80])
    check("and names the runner-up it beat", "המועמד הבא" in said["text"])

    where = assistant.respond(f"איפה {a_dev['serial']}", plan=ctx, topology=T)
    check("the assistant locates a device by serial",
          a_dev["rack"] in where["text"] and f"U{a_dev['u_start']}" in where["text"],
          where["text"][:90])

    unknown = assistant.respond("איפה 9900000021", plan=ctx, topology=T)
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
        spec = {"serial": f"9910000{len(colour):03d}", "type": "server", "u_size": 2,
                "fiber_ports": 2, "copper_ports": 2, "zone": colour}
        ranked = placement.rank_positions(T, spec, [blue_rack], limit=5)
        check(f"a {colour} device stays inside the {colour} zone",
              all(zmap[c["pod"]] == colour for c in ranked),
              str([(c["rack"], zmap[c["pod"]]) for c in ranked[:3]]))

    unzoned = {"serial": "9900000010", "type": "server", "u_size": 2,
               "fiber_ports": 2, "copper_ports": 2}
    check("a device with no zone is free to sit beside its target",
          placement.rank_positions(T, unzoned, [blue_rack], limit=1)[0]["rack"] == blue_rack)

    # a zone that cannot fit the device says which zone, not just "no space"
    try:
        placement.rank_positions(T, {"serial": "9900000011", "type": "server",
                                     "u_size": 42, "fiber_ports": 1,
                                     "copper_ports": 1, "zone": "white"},
                                 [blue_rack])
        check("a zone with no room names itself in the error", False, "no error")
    except placement.PlacementError as e:
        check("a zone with no room names itself in the error", "white" in str(e), str(e))

    # ---------- a bad zone in the sheet is caught before planning ----------
    zone_specs = [
        {"row": 2, "serial": "9900000012", "type": "server", "raw_type": "server",
         "u_size": 2, "fiber_ports": 1, "copper_ports": 1, "label": "a",
         "raw_zone": "ירוק"},
        {"row": 3, "serial": "9900000013", "type": "server", "raw_type": "server",
         "u_size": 2, "fiber_ports": 1, "copper_ports": 1, "label": "b",
         "raw_zone": "purple"},
    ]
    zv = bulkplan.validate_devices(T, zone_specs, [])
    check("an unrecognised zone is reported against its row",
          [(i["row"], i["kind"]) for i in zv["issues"]] == [(3, "unknown_zone")],
          str([(i["row"], i["kind"]) for i in zv["issues"]]))

    # and the sheet's zone actually reaches the planner, even if validation
    # never ran first
    fresh = [{"row": 2, "serial": "9900000014", "type": "server", "raw_type": "server",
              "u_size": 2, "fiber_ports": 1, "copper_ports": 1, "label": "c",
              "raw_zone": "לבן"}]
    fresh_wiring = [{"row": 2, "src": "9900000014", "dst": free_port(T, blue_rack, "fiber"),
                     "group": "", "cable": "fiber", "malformed": ""}]
    fp = bulkplan.plan(T, fresh_wiring, new_devices=fresh)
    landed = fp["siting"]["placements"][0]
    check("a zone written in the sheet is honoured without validation running first",
          landed["status"] == "ok" and zmap[landed["rack"].split("-")[0]] == "white",
          f"{landed.get('rack')} -> {zmap.get(str(landed.get('rack')).split('-')[0])}")

    # ---------- the Devices review and the planner cannot disagree ----------
    # A row the review rejects must never come out of the planner as sited.
    # The zone case is the one that mattered: swallowing a ZoneError and
    # carrying on with no zone turned the hardest boundary in the system into
    # "anywhere" on a typo — silently, and in the damaging direction.
    def dspec(row, serial, **kw):
        return {"row": row, "serial": serial,
                "raw_type": kw.get("t", "server"), "type": kw.get("t", "server"),
                "u_size": kw.get("u", 2), "fiber_ports": kw.get("f", 1),
                "copper_ports": kw.get("c", 1), "label": serial,
                "raw_zone": kw.get("z", "")}

    mixed = [dspec(2, "9900000015", z="ירוק"), dspec(3, "9900000016", z="purple"),
             dspec(4, "9900000017", u=99), dspec(5, "9900000018", f=0, c=0),
             dspec(6, "9900000019", t="toaster")]
    drev = bulkplan.validate_devices(T, [dict(s) for s in mixed], [])
    dsit = bulkplan.plan_devices(T, [dict(s) for s in mixed], [])
    passes = {s["row"] for s in mixed} - {i["row"] for i in drev["issues"]}
    sited_rows = {p["row"] for p in dsit["placements"] if p["status"] == "ok"}
    check("the Devices review predicts the planner exactly",
          passes == sited_rows, f"review {sorted(passes)} vs planner {sorted(sited_rows)}")
    typo = next(p for p in dsit["placements"] if p["serial"] == "9900000016")
    check("a mistyped zone is refused, never downgraded to 'anywhere'",
          typo["status"] == "failed" and "zone" in typo["reason"].lower(),
          f"{typo['status']}: {typo.get('reason', '')[:60]}")

    # ---------- the assistant admits what it does not understand ----------
    # "?" used to be a help trigger, which swallowed every unrecognised
    # question into the help text and left this path effectively unreachable —
    # an earlier test only passed because it happened to omit the question mark.
    for phrasing in ("מה מזג האוויר", "מה מזג האוויר?", "כמה עולה פיצה?"):
        check(f"'{phrasing}' is admitted as not understood",
              assistant.respond(phrasing, topology=T)["intent"] == "unknown",
              assistant.respond(phrasing, topology=T)["intent"])
    check("a bare question mark still asks for help",
          assistant.respond("?", topology=T)["intent"] == "help")

    # "clear" is a valid command even with nothing set; it used to come back
    # as "I didn't understand"
    empty_clear = assistant.respond("נקה דגשים", topology=T, constraints={})
    check("clearing with nothing set is understood, not rejected",
          empty_clear["intent"] == "clear_constraints", empty_clear["intent"])
    check("and clearing nothing does not trigger a pointless re-plan",
          not empty_clear.get("replan"))

    # ---------- malformed input degrades, it does not crash ----------
    # Everything the assistant sees arrives over HTTP from a browser, so none
    # of its shape is guaranteed.
    for junk in (123, None, ["a", "b"], {"x": 1}):
        try:
            assistant.respond(junk, topology=T)
        except Exception as exc:
            check("a malformed message is handled, not raised", False,
                  f"{junk!r} raised {type(exc).__name__}")
            break
    else:
        check("a malformed message is handled, not raised", True)

    for junk in ("not-a-dict", 5, ["a"], {"avoid_pods": "A1"}, {"avoid_pods": [1, 2]}):
        try:
            assistant.respond("אל תשתמש ב-A1-S05", topology=T, constraints=junk)
        except Exception as exc:
            check("malformed constraints are coerced, not raised", False,
                  f"{junk!r} raised {type(exc).__name__}")
            break
    else:
        check("malformed constraints are coerced, not raised", True)

    # ---------- typos are forgiven, guesses are not ----------
    # One character's slip is corrected; two is refused with the near misses
    # named. The line matters: "gren" is obviously green, "grey" is not.
    for typed, expected in (("green", "green"), ("GrEeN", "green"), ("ירוק", "green"),
                            ("  BLUE  ", "blue"), ("gren", "green"), ("Wihte", "white"),
                            ("yelow", "yellow"), ("bleu", "blue"), ("ירוקק", "green"),
                            ("צהב", "yellow"), ("כחל", "blue"), ("", None)):
        check(f"zone '{typed}' reads as {expected}",
              zones.resolve(typed) == expected, str(zones.resolve(typed)))

    for typed in ("grey", "purple", "bl", "xyz"):
        try:
            zones.resolve(typed)
            check(f"zone '{typed}' is refused rather than guessed", False, "accepted")
        except zones.ZoneError:
            check(f"zone '{typed}' is refused rather than guessed", True)

    check("a corrected zone is reported, never applied silently",
          zones.correction("gren") and not zones.correction("green"),
          f"{zones.correction('gren')!r} / {zones.correction('green')!r}")

    check("a near miss names what it nearly matched",
          "green" in str(_zone_error("grey")), _zone_error("grey"))

    # the edit budget itself
    check("one slip is within budget, two is not",
          fuzzy.distance("gren", "green") == 1 and fuzzy.distance("grey", "green") == 2)
    check("a transposition counts as one slip, not two",
          fuzzy.distance("zoen", "zone") == 1 and fuzzy.distance("bleu", "blue") == 1)
    check("an ambiguous correction is refused",
          fuzzy.match("xy", {"ax": 1, "xz": 2}) is None)
    check("a tie between spellings of the SAME answer is not ambiguous",
          fuzzy.match("srcport", dict.fromkeys(("SRC_PORT", "SOURCE_PORT"), "hit"))
          is not None)

    # mistyped headers, tab names and device types
    check("a mistyped column header still reads",
          bulkplan._pick({"SRC_PROT": "v", xlsxreader.ROW_KEY: 2},
                         bulkplan.SRC_ALIASES) == "v")
    check("an unrelated column is not mistaken for one",
          bulkplan._pick({"QTY": "v", xlsxreader.ROW_KEY: 2},
                         bulkplan.SRC_ALIASES) == "")
    check("a correctly spelled header beats a mistyped one in the same row",
          bulkplan._pick({"SRC_PORT": "right", "SRC_PROT": "wrong",
                          xlsxreader.ROW_KEY: 2}, bulkplan.SRC_ALIASES) == "right")
    typed_dev = bulkplan.devices_from_rows(
        [{"SERAIL": "9900000020", "TYP": "swich", "U SIZE": "1", "FIBRE": "4",
          xlsxreader.ROW_KEY: 2}])[0]
    check("a Devices row with several mistyped headers still reads",
          typed_dev["serial"] == "9900000020" and typed_dev["type"] == "switch"
          and typed_dev["u_size"] == 1 and typed_dev["fiber_ports"] == 4,
          str(typed_dev))

    # ---------- the chat tolerates typos without hijacking ordinary words ----
    # "כמה" and "מה" are each one edit from "למה"; left unguarded they routed
    # every such question to the why-handler and hid the intents behind it.
    for phrase, expect in (("נקה דגשים", "clear_constraints"),
                           ("נקא דגשים", "clear_constraints"),
                           ("למא הרכיב הוצב שם?", "why_placed"),
                           ("תעדיפ פוד A3", "prefer"),
                           ("prefre pod A3", "prefer"),
                           ("אל תשתמשש ב-D5", "avoid"),
                           ("hlep", "help"),
                           ("מה עומד להיגמר?", "capacity"),
                           ("כמה מקום יש ב-A1-S05", "rack_status"),
                           ("מה מזג האוויר?", "unknown"),
                           ("כמה עולה פיצה?", "unknown")):
        got = assistant.respond(phrase, topology=T)["intent"]
        check(f"'{phrase}' -> {expect}", got == expect, got)

    # ---------- serials are numbers, and a port can be named outright -------
    check("every serial is a plain 10-digit number",
          all(str(d["serial"]).isdigit() and len(d["serial"]) == 10
              for d in T["devices"].values()))
    check("a bare serial names the device and leaves the port open",
          serials.parse(a_dev["serial"]) == (a_dev["serial"], None))
    check("<serial>:<port> names the socket exactly",
          serials.parse(a_dev["serial"] + ":12") == (a_dev["serial"], 12))
    check("a port id is not mistaken for a serial",
          not serials.looks_like("A1-S05:FIB-PP-01:2"))
    check("a stray SN prefix is tolerated",
          serials.parse("S/N " + a_dev["serial"])[0] == a_dev["serial"])

    # Find a switch that genuinely has both a free and a taken fibre port
    # rather than assuming a particular one does: after a real execution the
    # obvious candidate may be full, and a test that assumes otherwise dies
    # mid-run and takes every later check with it.
    def _idx_by_status(dev_id, dev, want_free):
        return [i for i in range(1, dev["fiber_ports"] + 1)
                if (T["ports"][f"{dev_id}:{i}"]["status"] == "free") == want_free]

    tor_id, tor_dev, free_idx, used_idx = None, None, None, None
    for dev_id, dev in T["devices"].items():
        if dev["type"] != "switch" or dev["fiber_ports"] < 2 or dev["copper_ports"] < 1:
            continue
        free_here = _idx_by_status(dev_id, dev, True)
        used_here = _idx_by_status(dev_id, dev, False)
        if free_here and used_here:
            tor_id, tor_dev = dev_id, dev
            free_idx, used_idx = free_here[0], used_here[0]
            break
    if tor_id is None:                       # fall back to any switch with room
        tor_id, tor_dev = next((i, d) for i, d in T["devices"].items()
                               if d["type"] == "switch" and _idx_by_status(i, d, True))
        free_idx = _idx_by_status(tor_id, tor_dev, True)[0]

    far = free_port(T, "D5-N06", "fiber")

    def one(src, cable=""):
        return [{"row": 2, "src": src, "dst": far, "group": "", "cable": cable,
                 "label": "", "malformed": ""}]

    got = bulkplan._resolve_endpoints(T, one(f"{tor_dev['serial']}:{free_idx}"))[0]
    check("an explicitly named free port is used as written",
          got["src"] == f"{tor_id}:{free_idx}" and not got.get("malformed"),
          f"{got['src']} {got.get('malformed', '')}")
    check("naming a port needs no CABLE column", not got.get("malformed"))

    bad_port = bulkplan._resolve_endpoints(T, one(f"{tor_dev['serial']}:999"))[0]
    check("a port the device does not have is refused",
          "no port 999" in (bad_port.get("malformed") or ""), bad_port.get("malformed"))

    wrong_media = bulkplan._resolve_endpoints(
        T, one(f"{tor_dev['serial']}:{tor_dev['fiber_ports'] + 1}", "fiber"))[0]
    check("a named port of the wrong media is refused",
          "copper" in (wrong_media.get("malformed") or ""), wrong_media.get("malformed"))

    used_idx = next((i for i in range(1, tor_dev["fiber_ports"] + 1)
                     if T["ports"][f"{tor_id}:{i}"]["status"] != "free"), None)
    if used_idx:
        busy = bulkplan._resolve_endpoints(T, one(f"{tor_dev['serial']}:{used_idx}"))[0]
        check("a named port that is already patched is refused",
              "already patched" in (busy.get("malformed") or ""), busy.get("malformed"))

    # Two rows of the same sheet claiming one port is a different problem from
    # a port already patched on the map, and the row number is what the user
    # needs in order to go and fix it.
    clash = bulkplan._resolve_endpoints(T, [
        {"row": 2, "src": f"{tor_dev['serial']}:{free_idx}", "dst": far,
         "group": "", "cable": "", "label": "", "malformed": ""},
        {"row": 7, "src": f"{tor_dev['serial']}:{free_idx}",
         "dst": free_port(T, "D5-N07", "fiber"),
         "group": "", "cable": "", "label": "", "malformed": ""}])
    check("the first row to claim a named port keeps it", not clash[0].get("malformed"),
          clash[0].get("malformed"))
    check("a later row claiming the same port is told which row took it",
          "row 2" in (clash[1].get("malformed") or ""), clash[1].get("malformed"))

    # ---------- LABEL is optional on both tabs ----------
    labelled = bulkplan.demands_from_rows([
        {"SRC_PORT": "a", "DST_PORT": "b", "LABEL": "Core uplink",
         xlsxreader.ROW_KEY: 2},
        {"SRC_PORT": "c", "DST_PORT": "d", xlsxreader.ROW_KEY: 3}])
    check("a P2P row can carry an optional name",
          labelled[0]["label"] == "Core uplink" and labelled[1]["label"] == "",
          str([d["label"] for d in labelled]))
    check("a Devices row without a label still reads",
          bulkplan.devices_from_rows([{"SERIAL": "9906000001", "TYPE": "server",
                                       "U_SIZE": "1", "FIBER": "1",
                                       xlsxreader.ROW_KEY: 2}])[0]["label"] == "server")

    # ---------- device and port in separate columns ----------
    # A sheet may name each end as one cell, or as a serial with the port
    # number beside it. The device column is what decides which: with it
    # present SRC_PORT holds a bare number, without it a whole endpoint.
    split = bulkplan.demands_from_rows([
        {"SRC_DEVICE": "4827193056", "SRC_PORT": "12",
         "DST_DEVICE": "5488209915", "DST_PORT": "3", xlsxreader.ROW_KEY: 2},
        {"SRC_DEVICE": "4827193056", "SRC_PORT": "",
         "DST_DEVICE": "5488209915", "DST_PORT": "", xlsxreader.ROW_KEY: 3},
    ])
    check("a serial and a port number in separate columns become one endpoint",
          split[0]["src"] == "4827193056:12" and split[0]["dst"] == "5488209915:3",
          str([split[0]["src"], split[0]["dst"]]))
    check("a blank port column means 'any suitable port on that box'",
          split[1]["src"] == "4827193056" and split[1]["dst"] == "5488209915",
          str([split[1]["src"], split[1]["dst"]]))

    # the single-column form has to keep working — sheets written last week
    # still arrive, and SRC_PORT means something different in each
    single = bulkplan.demands_from_rows([
        {"SRC_PORT": "A1-S05:FIB-PP-01:2", "DST_PORT": "D5-N06:FIB-PP-01:1",
         xlsxreader.ROW_KEY: 2}])
    check("without a device column SRC_PORT is still a whole endpoint",
          single[0]["src"] == "A1-S05:FIB-PP-01:2", single[0]["src"])

    # The UI shows device ids, and every one of them contains colons
    # (A1-S05:FIB-PP-01). Copying one into the device column and typing the
    # port beside it is the obvious thing to do, and it has to work — testing
    # "does the device cell contain a colon" to mean "it already names a port"
    # silently dropped the port column and blamed the device for not existing.
    panel = next(d for d in T["devices"].values()
                 if len(_idx_by_status(d["id"], d, True)) >= 2)
    p1, p2 = _idx_by_status(panel["id"], panel, True)[:2]

    by_id = bulkplan.demands_from_rows([
        {"SRC_DEVICE": panel["id"], "SRC_PORT": str(p1),
         "DST_DEVICE": panel["serial"], "DST_PORT": str(p2), xlsxreader.ROW_KEY: 2}])
    check("a device id in the device column keeps the port beside it",
          by_id[0]["src"] == f"{panel['id']}:{p1}", by_id[0]["src"])

    # the two spellings must land on the identical port, or a sheet written one
    # way plans differently from the same sheet written the other
    by_serial = bulkplan._resolve_endpoints(T, bulkplan.demands_from_rows([
        {"SRC_DEVICE": panel["serial"], "SRC_PORT": str(p1),
         "DST_DEVICE": panel["serial"], "DST_PORT": str(p2), xlsxreader.ROW_KEY: 2}]))[0]
    check("a device id and a serial address the same port the same way",
          by_serial["src"] == f"{panel['id']}:{p1}",
          f"{by_serial['src']} vs {panel['id']}:{p1} — {by_serial.get('malformed')}")

    # a serial that already carries its port must not have a second one glued on
    dup = bulkplan.demands_from_rows([
        {"SRC_DEVICE": f"{panel['serial']}:{p1}", "SRC_PORT": str(p1),
         "DST_DEVICE": panel["serial"], "DST_PORT": str(p2), xlsxreader.ROW_KEY: 2}])
    check("a serial already carrying its port is left alone",
          dup[0]["src"] == f"{panel['serial']}:{p1}", dup[0]["src"])

    # A blank device column is not a licence to read the port number as an
    # endpoint: that would turn "12" into a port id and fail far from the typo.
    orphan = bulkplan.demands_from_rows([
        {"SRC_DEVICE": "", "SRC_PORT": "7",
         "DST_DEVICE": "5488209915", "DST_PORT": "3", xlsxreader.ROW_KEY: 2}])
    check("a port number with no device beside it is reported, not invented",
          orphan[0]["src"] == "" and "no source port" in orphan[0]["malformed"],
          str(orphan[0]))

    # The whole point of naming the socket: it says which medium, so the sheet
    # does not have to. Demanding CABLE for it would reject rows the planner
    # accepts — the disagreement this project cannot afford.
    # find two boxes that really do have a free fibre port right now, rather
    # than naming one and hoping the map still agrees
    def _free_fiber_port(exclude_rack=None):
        for pid, p in T["ports"].items():
            if (p["type"] == "fiber" and p["status"] == "free"
                    and p["rack"] != exclude_rack):
                dev = T["devices"][p["device"]]
                if dev.get("serial"):
                    return dev, p["index"]
        raise RuntimeError("no free fibre port anywhere on the map")

    dev_a, idx_a = _free_fiber_port()
    dev_b, idx_b = _free_fiber_port(exclude_rack=dev_a["rack"])

    named = bulkplan.validate(T, bulkplan.demands_from_rows([
        {"SRC_DEVICE": dev_a["serial"], "SRC_PORT": str(idx_a),
         "DST_DEVICE": dev_b["serial"], "DST_PORT": str(idx_b),
         xlsxreader.ROW_KEY: 2}]))
    check("naming the port needs no CABLE column",
          not any(i["kind"] == "no_cable_type" for i in named["issues"]),
          str(named["issues"]))
    bare = bulkplan.validate(T, bulkplan.demands_from_rows([
        {"SRC_DEVICE": dev_a["serial"], "SRC_PORT": "",
         "DST_DEVICE": dev_b["serial"], "DST_PORT": "",
         xlsxreader.ROW_KEY: 2}]))
    check("a bare serial on both ends still needs CABLE",
          any(i["kind"] == "no_cable_type" for i in bare["issues"]),
          str(bare["issues"]))

    # One named socket settles the medium for the whole row, whichever end it
    # is on — asking for CABLE anyway rejected rows the planner routes happily,
    # and made the answer depend on which side was left open.
    for a_open in (False, True):
        row = {"SRC_DEVICE": dev_a["id"], "SRC_PORT": str(idx_a),
               "DST_DEVICE": dev_b["serial"], "DST_PORT": "", xlsxreader.ROW_KEY: 2}
        if a_open:                       # same row, sides swapped
            row = {"SRC_DEVICE": dev_b["serial"], "SRC_PORT": "",
                   "DST_DEVICE": dev_a["id"], "DST_PORT": str(idx_a),
                   xlsxreader.ROW_KEY: 2}
        demands = bulkplan.demands_from_rows([row])
        rev = bulkplan.validate(T, demands)
        got = bulkplan._resolve_endpoints(T, demands)[0]
        where = "destination" if a_open else "source"
        check(f"a socket named at the {where} settles the media without CABLE",
              not rev["issues"] and not got.get("malformed"),
              f"review={[i['kind'] for i in rev['issues']]} planner={got.get('malformed')}")

    # asking one box for more ports than it has left is knowable before any
    # routing happens, and several rows must draw on the same supply
    small = min((d for d in T["devices"].values()
                 if d.get("serial") and 0 < d["fiber_ports"] <= 4),
                key=lambda d: d["fiber_ports"])
    # every row needs its OWN destination, or the sheet trips over the
    # duplicate-port rule before it ever reaches the capacity question
    spare = [(T["devices"][p["device"]]["serial"], p["index"])
             for p in T["ports"].values()
             if p["type"] == "fiber" and p["status"] == "free"
             and p["rack"] != small["rack"]
             and T["devices"][p["device"]].get("serial")][:small["fiber_ports"] + 2]
    greedy = bulkplan.validate(T, bulkplan.demands_from_rows([
        {"SRC_DEVICE": small["serial"], "SRC_PORT": "", "CABLE": "fiber",
         "DST_DEVICE": sn, "DST_PORT": str(idx), xlsxreader.ROW_KEY: r}
        for r, (sn, idx) in enumerate(spare, start=2)]))
    check("rows asking one box for 'any port' draw on a shared supply",
          any(i["kind"] == "device_full" for i in greedy["issues"]),
          f"{small['id']} has {small['fiber_ports']} fibre ports, "
          f"{len(spare)} rows asked — {greedy['by_kind']}")

    # ---------- every planned row carries its diagram ----------
    # The browser cannot look these ports up itself: a plan's route may pass
    # through a device the plan has not installed yet, which is simply absent
    # from the map the browser downloaded.
    drawn = bulkplan.plan(T, one(free_port(T, "A1-S05", "fiber")))["results"][0]
    check("a planned row carries a drawable jump chain",
          len(drawn["jump"]) == drawn["hops"] + 1,
          f"{len(drawn['jump'])} stops for {drawn['hops']} hops")
    check("every stop knows its cabinet, U and port",
          all(s["rack"] and s["u"] and s["port"] for s in drawn["jump"]),
          str(drawn["jump"][:2]))

    local_ports, by_dev = [], {}
    for pid, p in T["ports"].items():
        if p["rack"] == "A1-S05" and p["type"] == "copper" and p["status"] == "free":
            by_dev.setdefault(p["device"], pid)
    local_ports = list(by_dev.values())[:2]
    patch = bulkplan.plan(T, [{"row": 2, "src": local_ports[0], "dst": local_ports[1],
                               "group": "", "cable": "", "label": "",
                               "malformed": ""}])["results"][0]
    check("an intra-rack patch still shows BOTH of its ends",
          len(patch["jump"]) == 2 and patch["jump"][0]["rack"] == patch["jump"][1]["rack"],
          str(patch["jump"]))

    # ---------- releasing a connection ----------
    # decommission_route claims to be the exact inverse of commit_route, and
    # "exact" is checkable: fingerprint the map, commit, release, compare. A
    # release that leaves one strand behind would show up as free capacity that
    # is really carrying traffic, which is the kind of error nothing else
    # notices until a technician is standing at the wrong cabinet.
    import hashlib as _hashlib
    import json as _json
    from pathengine import decommission_route, DecommissionError, next_circuit_id

    def _fingerprint(t):
        return _hashlib.sha256(_json.dumps(t, sort_keys=True, default=str).encode()).hexdigest()

    round_trips, checked = 0, 0
    for a_rack, b_rack, media in (("A1-S05", "D5-N06", "fiber"),
                                  ("A1-S03", "A3-S06", "copper"),
                                  ("B3-S04", "B3-N07", "fiber")):
        work = copy.deepcopy(T)
        before = _fingerprint(work)
        route = resolve_route_options(free_port(work, a_rack, media),
                                      free_port(work, b_rack, media),
                                      count=1, topology=work)[0]
        cid = next_circuit_id(work)
        pathengine_commit = __import__("pathengine").commit_route
        pathengine_commit(work, route, cid)
        checked += 1
        if _fingerprint(work) == before:
            continue                      # commit did nothing; not a round trip
        decommission_route(work, cid)
        round_trips += _fingerprint(work) == before
    check("committing then releasing leaves the map byte-identical",
          round_trips == checked, f"{round_trips} of {checked} round-tripped")

    work = copy.deepcopy(T)
    victim = next(c for c in sorted(work["circuits"])
                  if not work["circuits"][c].get("partial"))
    held = work["circuits"][victim]
    rec = decommission_route(work, victim)
    check("releasing an existing circuit frees its ports",
          all(work["ports"][p]["status"] == "free" and not work["ports"][p]["circuit"]
              for p in rec["ports"]), str(rec["ports"][:2]))
    check("releasing gives every strand back to its trunk",
          all(str(s["strand_index"]) not in
              (next(e for e in work["edges"] if e["id"] == s["edge_id"])
               ["cable_types"][s["cable_type"]].get("strands") or {})
              for s in held["strands"]))
    check("a released trunk's used count matches its remaining strands",
          all(ct["used"] == len(ct.get("strands") or {})
              for e in work["edges"] for ct in e["cable_types"].values()))
    check("the circuit itself is gone", victim not in work["circuits"])

    try:
        decommission_route(work, victim)
        check("releasing the same circuit twice is refused", False, "no error")
    except DecommissionError:
        check("releasing the same circuit twice is refused", True)

    # The one that matters: a strand another circuit now holds must never be
    # taken back, and a refusal must change nothing at all.
    work = copy.deepcopy(T)
    target = next(c for c in sorted(work["circuits"])
                  if work["circuits"][c].get("strands") and not work["circuits"][c].get("partial"))
    s = work["circuits"][target]["strands"][0]
    edge = next(e for e in work["edges"] if e["id"] == s["edge_id"])
    edge["cable_types"][s["cable_type"]]["strands"][str(s["strand_index"])] = "CIR-OTHER"
    guarded = _fingerprint(work)
    try:
        decommission_route(work, target)
        check("refuses to take back a strand another circuit holds", False, "released it")
    except DecommissionError:
        check("refuses to take back a strand another circuit holds", True)
    check("and a refusal leaves the map completely untouched",
          _fingerprint(work) == guarded)
    forced = decommission_route(work, target, force=True)
    check("force still will not steal the other circuit's strand",
          edge["cable_types"][s["cable_type"]]["strands"].get(str(s["strand_index"])) == "CIR-OTHER"
          and bool(forced["conflicts"]))

    # ---------- pulling only the last leg ----------
    # The backbone strands between rooms are the expensive part of a route.
    # When only the far end is wrong they should survive changing a patch lead.
    from pathengine import truncate_route, extend_route

    work = copy.deepcopy(T)
    start = _fingerprint(work)
    # skip anything already truncated: the map is allowed to hold a partial
    # circuit, and a fixture that trips over one is a fixture, not a bug
    long_cid = next(c["id"] for c in work["circuits"].values()
                    if len(c["strands"]) >= 4 and not c.get("partial"))
    was = copy.deepcopy(work["circuits"][long_cid])
    tr = truncate_route(work, long_cid)
    cut = work["circuits"][long_cid]
    check("truncating drops exactly one hop",
          len(cut["strands"]) == len(was["strands"]) - 1,
          f"{len(was['strands'])} -> {len(cut['strands'])}")
    check("the far endpoint is freed", work["ports"][was["b_port"]]["status"] == "free")
    check("every earlier strand is untouched",
          all(next(e for e in work["edges"] if e["id"] == s["edge_id"])
              ["cable_types"][s["cable_type"]]["strands"].get(str(s["strand_index"]))
              == long_cid for s in cut["strands"]))
    check("the loose end stays patched and is marked as one",
          work["ports"][tr["open_end"]]["status"] == "used"
          and work["ports"][tr["open_end"]]["role"] == "open_end")
    check("the circuit admits it is unfinished",
          cut["partial"] and cut["b_port"] is None)
    check("trunk used counts still match after truncating",
          all(ct["used"] == len(ct.get("strands") or {})
              for e in work["edges"] for ct in e["cable_types"].values()))

    # putting it back exactly must be indistinguishable from never having cut it
    extend_route(work, long_cid, was["b_port"])
    check("truncate then re-extend to the same port restores the map exactly",
          _fingerprint(work) == start)

    # and the point of the feature: send it somewhere else, keeping the path
    truncate_route(work, long_cid)
    end_rack = work["ports"][work["circuits"][long_cid]["open_end"]]["rack"]
    elsewhere = next(p["id"] for p in work["ports"].values()
                     if p["status"] == "free" and p["type"] == was["cable_type"]
                     and p["rack"] not in (end_rack, work["ports"][was["b_port"]]["rack"]))
    extend_route(work, long_cid, elsewhere)
    now = work["circuits"][long_cid]
    kept = sum(1 for s in now["strands"] if s in was["strands"])
    check("re-aiming the last leg keeps every earlier hop",
          kept == len(was["strands"]) - 1, f"kept {kept} of {len(was['strands']) - 1}")
    check("and the source end never moved", now["a_port"] == was["a_port"])
    check("and the abandoned destination stayed free",
          work["ports"][was["b_port"]]["status"] == "free")
    check("a re-aimed circuit is whole again, with no partial flag left behind",
          not now.get("partial") and now["b_port"] == elsewhere)

    # a truncated circuit must still be releasable — its shape is different
    # Built here rather than borrowed, so "back to how it was" means back to
    # before this circuit existed at all — releasing an EXISTING one correctly
    # leaves the map different, since that circuit is now gone.
    work2 = copy.deepcopy(T)
    fresh = _fingerprint(work2)
    made = resolve_route_options(free_port(work2, "A1-S05", "fiber"),
                                 free_port(work2, "D5-N06", "fiber"),
                                 count=1, topology=work2)[0]
    cid2 = next_circuit_id(work2)
    __import__("pathengine").commit_route(work2, made, cid2)
    truncate_route(work2, cid2)
    rel2 = decommission_route(work2, cid2)
    check("a truncated circuit can still be released, loose end and all",
          not rel2["conflicts"] and _fingerprint(work2) == fresh,
          f"conflicts={rel2['conflicts'][:2]} identical={_fingerprint(work2) == fresh}")

    # the whole point: a freed port can be patched somewhere else
    work = copy.deepcopy(T)
    ep = next(p for p in work["ports"].values()
              if p["status"] == "used" and p["role"] == "endpoint" and p["peer"])
    old_peer = ep["peer"]
    decommission_route(work, ep["circuit"])
    check("a released port becomes free again", work["ports"][ep["id"]]["status"] == "free")
    far = next(p for p in work["ports"].values()
               if p["status"] == "free" and p["type"] == ep["type"]
               and p["rack"] != ep["rack"] and p["id"] != old_peer)
    try:
        again = resolve_route_options(ep["id"], far["id"], count=1, topology=work)
        check("and can be routed to a different destination", bool(again))
    except RouteError as e:
        check("and can be routed to a different destination", False, str(e))

    # ---------- the review must predict the plan (a property, not examples) ----
    # Every other check here is one hand-written row and one expected message,
    # which is exactly what let this rule break twice while 172 checks passed.
    # These generate sheets and assert a relationship between the two sides.
    # A longer sweep lives in test_agreement.py; this is the standing guard.
    test_agreement.run(T, check, sheets=250, plans=6)

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} checks passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
