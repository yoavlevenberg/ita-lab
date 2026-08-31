#!/usr/bin/env python3
"""
pathengine.py  (v2 — device/U model)
=====================================
The routing brain. Given two ports, it computes a physical path through the
tray/trunk infrastructure while enforcing every constraint a network engineer
would enforce by hand.

MODEL
-----
  rack   : a physical cabinet, 42U, belongs to a pod + row (S or N)
  device : mounted at a specific U inside a rack (patch panel, ToR switch,
           server). Devices OWN ports.
  port   : lives on a device. Has a cable type (fiber/copper) and is either
           free or consumed by a circuit.
  edge   : a physical tray/trunk between two racks, with separate capacity
           per cable type.
  circuit: a committed end-to-end connection. Records both endpoints, every
           rack it passes through, and the transit ports it occupies.

CONSTRAINTS ENFORCED
--------------------
  1. Both ports exist and are free.
  2. Cable type matches end-to-end (fiber can't terminate on a copper port).
  3. Every trunk segment has remaining capacity for that cable type.
  4. If a redundancy domain is requested, the WHOLE path stays inside it, so
     the A leg and the B leg of a redundant pair never share a physical
     segment.

This module has no idea ITA exists. When the real API becomes available,
only `load_topology()` and `commit_route()` need a second implementation —
the constraint logic below stays exactly as it is.
"""

import json
import os
from pathlib import Path

import networkx as nx

TOPOLOGY_PATH = Path(__file__).parent / "data" / "topology.json"

# Every hop costs a patch, a panel port and a hands-on cross-connect, so a
# route with fewer hops beats a shorter one almost every time. Weighting each
# segment as HOP_COST + length_m sorts by hop count first and uses the metre
# figure only to break ties between paths of equal depth — the distances in
# this synthetic map are assumptions anyway, while the hop count is real.
HOP_COST = 10_000.0

# Two ports in the same cabinet are joined by a patch lead — no trunk, no
# cross-connect, no strand consumed. This is the CHEAPEST possible connection
# and, once the planner can place new equipment, the outcome it should be
# aiming for: rack the new box beside what it talks to.
INTRA_RACK_M = 3.0

# When patching through an intermediate rack we prefer landing on a patch
# panel rather than eating a switch port.
_TRANSIT_DEVICE_PREFERENCE = ("fiber_patch_panel", "copper_patch_panel", "switch")


class RouteError(Exception):
    """No valid physical path exists. The message explains why, in plain language."""


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

def load_topology(path=TOPOLOGY_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_topology(topology, path=TOPOLOGY_PATH):
    """Write the map, atomically.

    write_text() truncates the file and then writes 32MB into it, which leaves
    roughly half a second on every commit where a crash, a full disk, or a
    closed window would leave topology.json cut in half — the whole map gone,
    to save one circuit. Writing a temporary file and renaming it means the
    real file is either the old map or the new one, never a partial one:
    os.replace is atomic on both POSIX and Windows.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    # Serialised whole and written once. json.dump() streams into the file in
    # small pieces and costs four times as much for a map this size; the string
    # form keeps the write as cheap as the unsafe version it replaces, so
    # safety here costs nothing.
    blob = json.dumps(topology, indent=1, ensure_ascii=False)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(blob)
        fh.flush()
        os.fsync(fh.fileno())      # on the disk, not sitting in a buffer
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _port(topology, port_id):
    p = topology["ports"].get(port_id)
    if p is None:
        raise RouteError(f"Port '{port_id}' does not exist.")
    return p


# --------------------------------------------------------------------------
# trunk strands  (port-to-port model)
# --------------------------------------------------------------------------
# A trunk is not an abstract capacity counter — it is a bundle of individually
# numbered strands, and strand #7 at one end is physically the same glass (or
# copper pair) as strand #7 at the other end. So every strand is addressable
# as a pair of real ports:
#
#     A1-S05->A1-S01#F007@A1-S05   <-->   A1-S05->A1-S01#F007@A1-S01
#
# Only OCCUPIED strands are stored (edge["cable_types"][ct]["strands"], a
# {index: circuit_id} map); a strand with no entry is free. Free is the
# default state and there are ~46k strands in this topology, so recording
# only what's taken keeps the dataset small without losing any addressability
# — every strand still has a stable number and two derivable port ids.

_STRAND_PREFIX = {"fiber": "F", "copper": "C"}


def strand_port_id(edge_id, cable_type, index, rack):
    """The port id of one END of one strand. The other end is the same call
    with the trunk's other rack."""
    return f"{edge_id}#{_STRAND_PREFIX[cable_type]}{index:03d}@{rack}"


def is_trunk_full(edge, cable_type):
    ct = edge["cable_types"].get(cable_type)
    if not ct:
        return True
    return len(ct.get("strands") or {}) >= ct["capacity"]


def first_free_strand(edge, cable_type, claimed=()):
    """Lowest-numbered free strand — technicians patch bottom-up, and a
    deterministic choice keeps Work Orders reproducible.

    `claimed` holds strand numbers already spoken for by routes that have been
    proposed but not yet committed (the alternative options of the same
    request). Without it, two options that overlap on a trunk would both be
    written up as using the very same strand.
    """
    ct = edge["cable_types"].get(cable_type)
    if not ct:
        return None
    taken = ct.get("strands") or {}
    for i in range(1, ct["capacity"] + 1):
        if str(i) not in taken and i not in claimed:
            return i
    return None


def _build_graph(topology, cable_type, domain=None):
    """Graph of only the segments that can actually carry this request:
    right cable type, spare capacity, and matching redundancy domain."""
    g = nx.Graph()
    g.add_nodes_from(topology["racks"].keys())
    for edge in topology["edges"]:
        ct = edge["cable_types"].get(cable_type)
        if not ct:
            continue                                    # trunk doesn't carry this media at all
        if is_trunk_full(edge, cable_type):
            continue                                    # every strand already patched
        if domain is not None and edge["domain"] != domain:
            continue                                    # wrong redundancy leg
        used = len(ct.get("strands") or {})
        g.add_edge(edge["from"], edge["to"], obj=edge,
                   id=edge["id"], domain=edge["domain"], length_m=edge["length_m"],
                   weight=HOP_COST + edge["length_m"],
                   capacity=ct["capacity"], used=used,
                   remaining=ct["capacity"] - used)
    return g


def _pick_transit_port(topology, rack_id, cable_type, exclude):
    """Choose a concrete free port on an intermediate rack so the technician
    gets a real cross-connect position, not just 'somewhere on the EOR'."""
    candidates = []
    for port_id, p in topology["ports"].items():
        if (p["rack"] == rack_id and p["type"] == cable_type
                and p["status"] == "free" and port_id not in exclude):
            dev_type = topology["devices"][p["device"]]["type"]
            try:
                rank = _TRANSIT_DEVICE_PREFERENCE.index(dev_type)
            except ValueError:
                rank = len(_TRANSIT_DEVICE_PREFERENCE)
            candidates.append((rank, p["device"], p["index"], port_id))
    if not candidates:
        raise RouteError(f"No free {cable_type} port left on {rack_id} to cross-connect through.")
    candidates.sort()
    return candidates[0][3]


def intra_rack_route(topology, src_port_id, dst_port_id, cable_type):
    """A direct patch between two ports in one cabinet: zero trunk segments,
    zero transit ports, no strand consumed. Shaped exactly like any other
    route so every consumer — Work Order, commit, the UI — handles it without
    a special case."""
    return {
        "status": "ok",
        "cable_type": cable_type,
        "domain": "local",              # never leaves the cabinet, so no A/B leg
        "src_port": src_port_id,
        "dst_port": dst_port_id,
        "src_location": describe_port(topology, src_port_id),
        "dst_location": describe_port(topology, dst_port_id),
        "hop_racks": [topology["ports"][src_port_id]["rack"]],
        "segments": [],
        "transit_points": [],
        "total_length_m": INTRA_RACK_M,
        "intra_rack": True,
    }


def direct_route(topology, src_port_id, dst_port_id):
    """One jump and no more: patch these two ports to each other directly.

    The automatic router answers "how do I get there", which for two cabinets
    with no cable between them means six hops across the site. Sometimes the
    question is the other one — "is there a single cable I can patch here?" —
    and a six-hop answer to that is not a smaller version of the right answer,
    it is a different one. So this refuses rather than routes, and says what it
    found instead.

    Two ports in one cabinet are a patch lead and consume no trunk at all. Two
    cabinets with a trunk between them are one strand of it.
    """
    if src_port_id == dst_port_id:
        raise RouteError(f"Source and destination are the same port ({src_port_id}).")

    src, dst = _port(topology, src_port_id), _port(topology, dst_port_id)
    if src["status"] != "free":
        raise RouteError(f"Source port {src_port_id} is already in use.")
    if dst["status"] != "free":
        raise RouteError(f"Destination port {dst_port_id} is already in use.")
    if src["type"] != dst["type"]:
        raise RouteError(f"Cable type mismatch: source is {src['type']}, "
                         f"destination is {dst['type']}. Both ends must be the "
                         f"same media.")

    cable_type = src["type"]
    a, b = src["rack"], dst["rack"]
    if a == b:
        route = intra_rack_route(topology, src_port_id, dst_port_id, cable_type)
        route.update(shared_segments=[], fully_disjoint_from_previous=True,
                     option_index=1, direct=True)
        return route

    edge = next((e for e in topology["edges"]
                 if {e["from"], e["to"]} == {a, b} and cable_type in e["cable_types"]),
                None)
    if edge is None:
        raise RouteError(
            f"No {cable_type} cable runs directly between {a} and {b}, so there "
            f"is no single jump to make. Turn off direct mode to route through "
            f"the site instead.")
    if is_trunk_full(edge, cable_type):
        raise RouteError(f"The {cable_type} trunk between {a} and {b} is full — "
                         f"no strand left for a direct patch.")

    ct = edge["cable_types"][cable_type]
    used = len(ct.get("strands") or {})
    seg = _segment({"id": edge["id"], "obj": edge, "domain": edge["domain"],
                    "length_m": edge["length_m"], "capacity": ct["capacity"],
                    "used": used, "remaining": ct["capacity"] - used},
                   a, b, cable_type)
    return {
        "status": "ok",
        "cable_type": cable_type,
        "domain": edge["domain"],
        "src_port": src_port_id,
        "dst_port": dst_port_id,
        "src_location": describe_port(topology, src_port_id),
        "dst_location": describe_port(topology, dst_port_id),
        "hop_racks": [a, b],
        "segments": [seg],
        "transit_points": [],           # one jump has nothing in the middle
        "total_length_m": round(edge["length_m"], 1),
        "shared_segments": [],
        "fully_disjoint_from_previous": True,
        "option_index": 1,
        "direct": True,
    }


def _segment(e, a, b, cable_type, reserved=None):
    """One hop of a route: which trunk, and which numbered strand inside it
    gets patched at each end.

    `reserved` maps edge_id -> set of strand numbers already handed to a
    sibling option of this same request, so alternatives never quote the same
    physical strand."""
    edge = e["obj"]
    claimed = (reserved or {}).get(e["id"], ())
    index = first_free_strand(edge, cable_type, claimed)
    if index is None:
        raise RouteError(f"Trunk '{e['id']}' has no free {cable_type} strand left.")
    if reserved is not None:
        reserved.setdefault(e["id"], set()).add(index)
    # the strand is physically continuous, so its two port ids are just its
    # two ends — reported in the direction the route actually travels
    return {
        "edge_id": e["id"], "from_rack": a, "to_rack": b, "domain": e["domain"],
        "length_m": e["length_m"], "capacity": e["capacity"],
        "used_before": e["used"], "remaining_before": e["remaining"],
        "strand_index": index,
        "strand_port_from": strand_port_id(e["id"], cable_type, index, a),
        "strand_port_to": strand_port_id(e["id"], cable_type, index, b),
    }


def describe_strand(seg, cable_type):
    """Human-readable strand label, e.g. 'fiber strand #7'."""
    kind = "fiber strand" if cable_type == "fiber" else "copper pair"
    return f"{kind} #{seg['strand_index']}"


def describe_port(topology, port_id):
    """Human-friendly location string, e.g. 'D5-N05-U20-P33 (FIB-PP-01)'."""
    p = topology["ports"][port_id]
    dev = topology["devices"][p["device"]]
    return f"{p['rack']}-U{dev['u_start']}-P{p['index']} ({dev['name']})"


# --------------------------------------------------------------------------
# routing
# --------------------------------------------------------------------------

def resolve_path(src_port_id, dst_port_id, domain=None, topology=None):
    """Propose a physical route. Never mutates anything — proposing and
    committing are deliberately separate, so an engineer approves first."""
    topology = topology if topology is not None else load_topology()

    src, dst = _port(topology, src_port_id), _port(topology, dst_port_id)

    if src["status"] != "free":
        raise RouteError(f"Source port {src_port_id} is already in use.")
    if dst["status"] != "free":
        raise RouteError(f"Destination port {dst_port_id} is already in use.")
    if src["type"] != dst["type"]:
        raise RouteError(f"Cable type mismatch: source is {src['type']}, destination is "
                         f"{dst['type']}. Both ends must be the same media.")

    cable_type = src["type"]
    src_rack, dst_rack = src["rack"], dst["rack"]
    if src_rack == dst_rack:
        raise RouteError("Both ports are in the same rack — this is an intra-rack patch, "
                         "no trunk routing required.")

    # Evaluate every candidate leg, then pick the best one — rather than
    # returning the first that happens to work. Without this the planner
    # would pour everything onto domain A and leave domain B idle.
    candidates, attempts = [], []
    for d in ([domain] if domain else ["A", "B"]):
        g = _build_graph(topology, cable_type, domain=d)
        if src_rack not in g or dst_rack not in g:
            attempts.append(f"domain {d}: no {cable_type} uplink with spare capacity")
            continue
        try:
            hops = nx.shortest_path(g, src_rack, dst_rack, weight="weight")
        except nx.NetworkXNoPath:
            attempts.append(f"domain {d}: no path with available capacity")
            continue

        segments, total = [], 0.0
        for a, b in zip(hops, hops[1:]):
            e = g.get_edge_data(a, b)
            total += e["length_m"]
            segments.append(_segment(e, a, b, cable_type))
        tightest = min(s["remaining_before"] for s in segments)
        # shortest run wins; on a tie, take the leg with more headroom left
        candidates.append(((round(total, 1), -tightest), d, hops, segments, total))

    if not candidates:
        raise RouteError(f"No valid {cable_type} route between {src_rack} and {dst_rack}. "
                         + "Tried: " + "; ".join(attempts))

    candidates.sort(key=lambda c: c[0])
    _, d, hops, segments, total = candidates[0]

    used = {src_port_id, dst_port_id}
    transit = []
    for rack_id in hops[1:-1]:
        pid = _pick_transit_port(topology, rack_id, cable_type, used)
        used.add(pid)
        transit.append({"rack": rack_id, "port": pid,
                        "location": describe_port(topology, pid)})

    return {
        "status": "ok",
        "cable_type": cable_type,
        "domain": d,
        "src_port": src_port_id,
        "dst_port": dst_port_id,
        "src_location": describe_port(topology, src_port_id),
        "dst_location": describe_port(topology, dst_port_id),
        "hop_racks": hops,
        "segments": segments,
        "transit_points": transit,
        "total_length_m": round(total, 1),
        "alternatives_considered": [c[1] for c in candidates],
    }


# --------------------------------------------------------------------------
# committing  (this is the function that will one day call the ITA API)
# --------------------------------------------------------------------------

def _route_from_hops(topology, hops, src_port_id, dst_port_id, cable_type, g,
                     reserved=None, reserved_ports=None):
    """Build a full route dict (segments, transit points, totals) from an
    already-computed rack path. Shared by resolve_route_options() below.

    `reserved` / `reserved_ports` carry what sibling options already claimed,
    so two alternatives for the same request never quote the same strand or
    the same cross-connect port."""
    segments, total = [], 0.0
    for a, b in zip(hops, hops[1:]):
        e = g.get_edge_data(a, b)
        total += e["length_m"]
        segments.append(_segment(e, a, b, cable_type, reserved))

    used = {src_port_id, dst_port_id} | set(reserved_ports or ())
    transit = []
    for rack_id in hops[1:-1]:
        pid = _pick_transit_port(topology, rack_id, cable_type, used)
        used.add(pid)
        transit.append({"rack": rack_id, "port": pid,
                        "location": describe_port(topology, pid)})

    domains_seen = {s["domain"] for s in segments}
    domain_label = next(iter(domains_seen)) if len(domains_seen) == 1 else "mixed"

    return {
        "status": "ok",
        "cable_type": cable_type,
        "domain": domain_label,
        "src_port": src_port_id,
        "dst_port": dst_port_id,
        "src_location": describe_port(topology, src_port_id),
        "dst_location": describe_port(topology, dst_port_id),
        "hop_racks": hops,
        "segments": segments,
        "transit_points": transit,
        "total_length_m": round(total, 1),
    }


def _next_best_fallback(g, src_rack, dst_rack, exclude_hop_lists):
    """Used when the graph has run out of fully edge-disjoint paths: fall
    back to the next-shortest path overall (may overlap), skipping anything
    already returned as an option. Returns None when there is no path at all.

    shortest_simple_paths is a GENERATOR: it does no work — and raises
    nothing — until it is iterated. Guarding only the call left
    NetworkXNoPath to escape from the first `for`, which surfaced as a
    traceback out of the planner whenever a pair genuinely had no route
    (copper between pods on different MDAs, say).
    """
    exclude = {tuple(h) for h in exclude_hop_lists}
    try:
        for hops in nx.shortest_simple_paths(g, src_rack, dst_rack, weight="weight"):
            if tuple(hops) not in exclude:
                return hops
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return None
    return None


def resolve_route_options(src_port_id, dst_port_id, domain=None, count=2, topology=None):
    """Propose up to `count` alternative physical routes for the same port
    pair, computed from the graph itself rather than from any naming
    convention (rack/rows named A/B, 1/2, FIRST/SEC, ...). The first option
    is the shortest path; each following option is the shortest path once
    the previous options' own trunk segments are removed from the graph —
    which guarantees zero shared segments whenever such a path physically
    exists. When it doesn't, we fall back to the next-best path and report
    exactly which segments it shares, so an engineer can judge (e.g. against
    independent power feeds) whether that overlap is actually a problem.

    If `domain` is given, this is a manual override: skip the automatic
    diversity search and return a single path forced onto that labeled leg,
    same as the old behavior — useful on sites where the domain tags are
    known to be reliable.
    """
    topology = topology if topology is not None else load_topology()

    if src_port_id == dst_port_id:
        # Until intra-rack patching was allowed, the same-cabinet check caught
        # this by accident. It has to be explicit now: a port cannot be its own
        # far end.
        raise RouteError(f"Source and destination are the same port ({src_port_id}).")

    src, dst = _port(topology, src_port_id), _port(topology, dst_port_id)
    if src["status"] != "free":
        raise RouteError(f"Source port {src_port_id} is already in use.")
    if dst["status"] != "free":
        raise RouteError(f"Destination port {dst_port_id} is already in use.")
    if src["type"] != dst["type"]:
        raise RouteError(f"Cable type mismatch: source is {src['type']}, destination is "
                         f"{dst['type']}. Both ends must be the same media.")

    cable_type = src["type"]
    src_rack, dst_rack = src["rack"], dst["rack"]
    if src_rack == dst_rack:
        route = intra_rack_route(topology, src_port_id, dst_port_id, cable_type)
        route.update(shared_segments=[], fully_disjoint_from_previous=True, option_index=1)
        return [route]

    g = _build_graph(topology, cable_type, domain=domain)
    if src_rack not in g or dst_rack not in g:
        where = f" in domain {domain}" if domain else ""
        raise RouteError(f"No {cable_type} uplink with spare capacity{where}.")

    if domain is not None:
        try:
            hops = nx.shortest_path(g, src_rack, dst_rack, weight="weight")
        except nx.NetworkXNoPath:
            raise RouteError(f"No path with available capacity in domain {domain}.")
        route = _route_from_hops(topology, hops, src_port_id, dst_port_id, cable_type, g)
        route["shared_segments"] = []
        route["fully_disjoint_from_previous"] = True
        route["option_index"] = 1
        return [route]

    options, seen_edge_sets = [], []
    working = g.copy()
    # options are alternatives for ONE request, and only one of them will ever
    # be committed — but they are shown (and printed into Work Orders) side by
    # side, so each has to name resources the others have not already named
    reserved, reserved_ports = {}, set()
    for i in range(count):
        try:
            hops = nx.shortest_path(working, src_rack, dst_rack, weight="weight")
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            hops = _next_best_fallback(g, src_rack, dst_rack, [o["hop_racks"] for o in options])
            if hops is None:
                break

        try:
            route = _route_from_hops(topology, hops, src_port_id, dst_port_id, cable_type, g,
                                     reserved=reserved, reserved_ports=reserved_ports)
        except RouteError:
            # The graph only carries trunks with spare capacity, but the options
            # already built are holding some of it: an earlier option can take
            # the last strand on a trunk this path needs. That means there is no
            # FURTHER alternative — it does not mean the alternatives already
            # found are invalid, so keep them instead of failing the request.
            if options:
                break
            raise
        reserved_ports.update(t["port"] for t in route["transit_points"])
        this_edges = {s["edge_id"] for s in route["segments"]}
        prev_edges = set().union(*seen_edge_sets) if seen_edge_sets else set()
        shared = sorted(this_edges & prev_edges)
        route["shared_segments"] = shared
        route["fully_disjoint_from_previous"] = not shared
        route["option_index"] = i + 1
        options.append(route)
        seen_edge_sets.append(this_edges)

        for a, b in zip(hops, hops[1:]):
            if working.has_edge(a, b):
                working.remove_edge(a, b)

    if not options:
        raise RouteError(f"No valid {cable_type} route between {src_rack} and {dst_rack}.")
    return options


def next_circuit_id(topology):
    """Next free CIR-#### id, continuing from whatever's already seeded."""
    nums = [int(cid.split("-", 1)[1]) for cid in topology.get("circuits", {})
            if cid.startswith("CIR-") and cid.split("-", 1)[1].isdigit()]
    return f"CIR-{(max(nums) + 1) if nums else 1:04d}"


def revalidate_route(topology, route):
    """A route computed earlier can go stale if something else consumed the
    same ports/capacity in the meantime. Re-check before committing so
    commit_route() never has to trust caller-supplied state blindly."""
    for port_id in (route["src_port"], route["dst_port"]):
        if topology["ports"][port_id]["status"] != "free":
            raise RouteError(f"Port '{port_id}' is no longer free — recompute the route.")
    for tp in route["transit_points"]:
        if topology["ports"][tp["port"]]["status"] != "free":
            raise RouteError(f"Transit port '{tp['port']}' is no longer free — recompute the route.")
    edges = {e["id"]: e for e in topology["edges"]}
    for seg in route["segments"]:
        edge = edges.get(seg["edge_id"])
        ct = edge["cable_types"].get(route["cable_type"]) if edge else None
        if not ct:
            raise RouteError(f"Trunk '{seg['edge_id']}' no longer carries {route['cable_type']} — recompute the route.")
        # the reservation is for one SPECIFIC strand, so checking spare
        # capacity is not enough — that exact strand has to still be free
        taken = ct.get("strands") or {}
        index = str(seg["strand_index"])
        if index in taken:
            raise RouteError(f"{describe_strand(seg, route['cable_type']).capitalize()} on trunk "
                             f"'{seg['edge_id']}' was patched by {taken[index]} in the meantime — "
                             "recompute the route.")


class DecommissionError(Exception):
    """The circuit cannot be released as recorded — usually because the map no
    longer matches what it claims to hold."""


def decommission_route(topology, circuit_id, force=False):
    """Take a circuit out: give its strands back to the trunks, free its ports,
    and forget it. The exact inverse of commit_route.

    Until this existed the tool could only ever add. That is not what a room
    does — cabling gets pulled — and it showed: the lab map only filled up, a
    wrong execution could not be undone, and pointed at the real ITA a link
    released THERE would have stayed occupied here forever.

    Nothing is freed on the circuit's say-so alone. Every strand and every port
    is checked to be held by THIS circuit before it is released, because the
    damage from releasing someone else's fibre is silent: the map would show a
    free strand that is carrying live traffic, and the next plan would hand it
    to a technician. A disagreement raises instead, naming what it found, and
    `force` is there for a genuinely corrupt record — it releases only the parts
    that do agree and reports the rest.
    """
    circuit = (topology.get("circuits") or {}).get(circuit_id)
    if not circuit:
        raise DecommissionError(f"No circuit {circuit_id} on the map.")

    edges = {e["id"]: e for e in topology["edges"]}
    ports = topology["ports"]
    conflicts, freed_strands, freed_ports = [], [], []

    # ---- check everything BEFORE touching anything ----
    for s in circuit.get("strands", []):
        edge = edges.get(s["edge_id"])
        if edge is None:
            conflicts.append(f"trunk {s['edge_id']} is no longer on the map")
            continue
        ct = edge["cable_types"].get(s["cable_type"])
        if ct is None:
            conflicts.append(f"trunk {s['edge_id']} has no {s['cable_type']} any more")
            continue
        holder = (ct.get("strands") or {}).get(str(s["strand_index"]))
        if holder is None:
            conflicts.append(f"{s['edge_id']} {s['cable_type']} strand "
                             f"#{s['strand_index']} is already free")
        elif holder != circuit_id:
            conflicts.append(f"{s['edge_id']} {s['cable_type']} strand "
                             f"#{s['strand_index']} is held by {holder}, not {circuit_id}")

    # A truncated circuit has no far end and does have an open end, so build
    # the list from what is actually there rather than from the usual shape.
    owned_ports = [p for p in (circuit.get("a_port"), circuit.get("b_port"),
                               circuit.get("open_end"))
                   if p] + list(circuit.get("transit_ports") or [])
    for pid in owned_ports:
        p = ports.get(pid)
        if p is None:
            conflicts.append(f"port {pid} is no longer on the map")
        elif p.get("circuit") != circuit_id:
            conflicts.append(f"port {pid} is held by {p.get('circuit') or 'nobody'}, "
                             f"not {circuit_id}")

    if conflicts and not force:
        raise DecommissionError(
            f"{circuit_id} does not match the map, so nothing was released: "
            + "; ".join(conflicts[:4])
            + (f" (+{len(conflicts) - 4} more)" if len(conflicts) > 4 else ""))

    # ---- release ----
    for s in circuit.get("strands", []):
        edge = edges.get(s["edge_id"])
        ct = edge["cable_types"].get(s["cable_type"]) if edge else None
        if ct is None:
            continue
        strands = ct.get("strands") or {}
        if strands.get(str(s["strand_index"])) != circuit_id:
            continue                      # someone else's; force must not steal it
        del strands[str(s["strand_index"])]
        ct["used"] = len(strands)
        freed_strands.append(f"{s['edge_id']}#{s['strand_index']}")

    for pid in owned_ports:
        p = ports.get(pid)
        if p is None or p.get("circuit") != circuit_id:
            continue
        p["status"] = "free"
        p["circuit"] = None
        p["role"] = None
        p["peer"] = None
        freed_ports.append(pid)

    del topology["circuits"][circuit_id]
    return {"circuit_id": circuit_id, "strands": freed_strands,
            "ports": freed_ports, "conflicts": conflicts,
            "a_port": circuit["a_port"], "b_port": circuit["b_port"]}


def open_end_of(topology, circuit):
    """Where a truncated circuit currently stops: the cross-connect port that
    the remaining trunk still lands on, waiting for a new final leg."""
    if not circuit.get("partial"):
        return None
    return circuit.get("open_end")


def truncate_route(topology, circuit_id):
    """Pull the LAST leg only, and leave the rest of the path in place.

    A route is a chain of trunk strands joined by cross-connects. When only the
    far end is wrong, tearing the whole thing out throws away the expensive
    part — the backbone strands already pulled between rooms — to change a
    patch lead. This frees the far endpoint and the final strand, and the
    cross-connect that fed it becomes the circuit's open end: still patched,
    still holding the trunk behind it, waiting to be sent somewhere else.

    The circuit is left explicitly marked `partial`, because a connection with
    one end loose is not carrying anything and must not be mistaken for one
    that is.
    """
    circuit = (topology.get("circuits") or {}).get(circuit_id)
    if not circuit:
        raise DecommissionError(f"No circuit {circuit_id} on the map.")
    if circuit.get("partial"):
        raise DecommissionError(
            f"{circuit_id} already ends in mid-air at "
            f"{describe_port(topology, circuit['open_end'])} — give it a "
            f"destination, or release it completely.")

    strands = circuit.get("strands") or []
    if len(strands) <= 1:
        raise DecommissionError(
            f"{circuit_id} is a single hop, so its last hop is the whole "
            f"connection — release it instead of truncating it.")

    last = strands[-1]
    edge = next((e for e in topology["edges"] if e["id"] == last["edge_id"]), None)
    ct = edge["cable_types"].get(last["cable_type"]) if edge else None
    holder = (ct.get("strands") or {}).get(str(last["strand_index"])) if ct else None
    if holder != circuit_id:
        raise DecommissionError(
            f"the last strand of {circuit_id} ({last['edge_id']} "
            f"#{last['strand_index']}) is held by {holder or 'nobody'}, so "
            f"nothing was changed.")

    # the cross-connect that joined the last two hops becomes the open end
    transit = list(circuit.get("transit_ports") or [])
    if not transit:
        raise DecommissionError(
            f"{circuit_id} has no cross-connect to stop at — release it instead.")
    new_end = transit[-1]

    b_port = circuit["b_port"]
    if topology["ports"].get(b_port, {}).get("circuit") != circuit_id:
        raise DecommissionError(
            f"port {b_port} is not held by {circuit_id}, so nothing was changed.")

    # ---- release just that leg ----
    del ct["strands"][str(last["strand_index"])]
    ct["used"] = len(ct["strands"])

    p = topology["ports"][b_port]
    p["status"], p["circuit"], p["role"], p["peer"] = "free", None, None, None

    end = topology["ports"][new_end]
    end["role"] = "open_end"
    end["peer"] = None

    a = topology["ports"][circuit["a_port"]]
    a["peer"] = None            # its far end no longer exists

    circuit["strands"] = strands[:-1]
    circuit["segment_ids"] = circuit["segment_ids"][:-1]
    circuit["transit_ports"] = transit[:-1]
    circuit["hop_racks"] = circuit["hop_racks"][:-1]
    circuit["total_length_m"] = round(
        circuit["total_length_m"] - (edge["length_m"] if edge else 0), 1)
    circuit["partial"] = True
    circuit["open_end"] = new_end
    circuit.pop("b_port", None)
    circuit["b_port"] = None

    return {"circuit_id": circuit_id, "freed_port": b_port,
            "freed_strand": f"{last['edge_id']}#{last['strand_index']}",
            "open_end": new_end,
            "open_end_location": describe_port(topology, new_end),
            "remaining_hops": len(circuit["strands"])}


def extend_route(topology, circuit_id, dst_port_id):
    """Give a truncated circuit a new final leg, to a different destination.

    Routed from the open end, so everything already pulled is kept and only
    what is missing is added.
    """
    circuit = (topology.get("circuits") or {}).get(circuit_id)
    if not circuit:
        raise RouteError(f"No circuit {circuit_id} on the map.")
    if not circuit.get("partial"):
        raise RouteError(f"{circuit_id} already has both ends; nothing to extend.")

    end_id = circuit["open_end"]
    end = topology["ports"][end_id]
    dst = _port(topology, dst_port_id)
    if dst["status"] != "free":
        raise RouteError(f"Destination port {dst_port_id} is already in use.")
    if dst["type"] != circuit["cable_type"]:
        raise RouteError(f"Cable type mismatch: this circuit is "
                         f"{circuit['cable_type']}, {dst_port_id} is {dst['type']}.")

    # The open end is occupied by this very circuit, and the router refuses a
    # used source. Free it for the search and put it back either way, so a
    # failed search cannot leave the map looking different.
    saved = (end["status"], end["circuit"], end["role"], end["peer"])
    end["status"], end["circuit"], end["role"], end["peer"] = "free", None, None, None
    try:
        if end["rack"] == dst["rack"]:
            tail = intra_rack_route(topology, end_id, dst_port_id, circuit["cable_type"])
        else:
            tail = resolve_route_options(end_id, dst_port_id, count=1,
                                         topology=topology)[0]
    finally:
        end["status"], end["circuit"], end["role"], end["peer"] = saved

    edges = {e["id"]: e for e in topology["edges"]}
    for seg in tail["segments"]:
        ct = edges[seg["edge_id"]]["cable_types"][circuit["cable_type"]]
        ct.setdefault("strands", {})[str(seg["strand_index"])] = circuit_id
        ct["used"] = len(ct["strands"])
        circuit["strands"].append({
            "edge_id": seg["edge_id"], "cable_type": circuit["cable_type"],
            "strand_index": seg["strand_index"],
            "from_port": seg["strand_port_from"], "to_port": seg["strand_port_to"]})
        circuit["segment_ids"].append(seg["edge_id"])

    # the old open end is a cross-connect again, now that something leads on
    end["role"] = "transit"
    end["peer"] = None
    circuit["transit_ports"].append(end_id)
    for tp in tail["transit_points"]:
        p = topology["ports"][tp["port"]]
        p["status"], p["circuit"], p["role"], p["peer"] = "used", circuit_id, "transit", None
        circuit["transit_ports"].append(tp["port"])

    dst["status"], dst["circuit"], dst["role"] = "used", circuit_id, "endpoint"
    dst["peer"] = circuit["a_port"]
    topology["ports"][circuit["a_port"]]["peer"] = dst_port_id

    circuit["b_port"] = dst_port_id
    circuit["hop_racks"] = circuit["hop_racks"] + tail["hop_racks"][1:]
    circuit["total_length_m"] = round(
        circuit["total_length_m"] + tail["total_length_m"], 1)
    # Dropped rather than set to False: a circuit with both ends again is a
    # whole circuit, and should be indistinguishable from one that was never
    # truncated — including byte-for-byte, which is what the round-trip test
    # compares.
    circuit.pop("partial", None)
    circuit.pop("open_end", None)
    return circuit


def commit_route(topology, route, circuit_id):
    """Apply an approved route: consume the ports, charge the trunks, and
    record the circuit. In production this is where the ITA write-back goes."""
    edges = {e["id"]: e for e in topology["edges"]}
    strand_log = []
    for seg in route["segments"]:
        edge = edges[seg["edge_id"]]
        ct = edge["cable_types"][route["cable_type"]]
        strands = ct.setdefault("strands", {})
        strands[str(seg["strand_index"])] = circuit_id
        ct["used"] = len(strands)          # kept in sync so capacity bars stay cheap
        strand_log.append({
            "edge_id": edge["id"], "cable_type": route["cable_type"],
            "strand_index": seg["strand_index"],
            "from_port": seg["strand_port_from"], "to_port": seg["strand_port_to"],
        })

    endpoints = (route["src_port"], route["dst_port"])
    for port_id in endpoints:
        p = topology["ports"][port_id]
        p["status"] = "used"
        p["circuit"] = circuit_id
        p["role"] = "endpoint"
        p["peer"] = endpoints[1] if port_id == endpoints[0] else endpoints[0]

    for tp in route["transit_points"]:
        p = topology["ports"][tp["port"]]
        p["status"] = "used"
        p["circuit"] = circuit_id
        p["role"] = "transit"
        p["peer"] = None

    topology.setdefault("circuits", {})[circuit_id] = {
        "id": circuit_id,
        "cable_type": route["cable_type"],
        "domain": route["domain"],
        "a_port": route["src_port"],
        "b_port": route["dst_port"],
        "hop_racks": route["hop_racks"],
        "segment_ids": [s["edge_id"] for s in route["segments"]],
        "strands": strand_log,
        "intra_rack": bool(route.get("intra_rack")),
        "transit_ports": [t["port"] for t in route["transit_points"]],
        "total_length_m": route["total_length_m"],
    }
    return topology["circuits"][circuit_id]
