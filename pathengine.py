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
from pathlib import Path

import networkx as nx

TOPOLOGY_PATH = Path(__file__).parent / "data" / "topology.json"

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
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(topology, indent=1, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _port(topology, port_id):
    p = topology["ports"].get(port_id)
    if p is None:
        raise RouteError(f"Port '{port_id}' does not exist.")
    return p


def _edge_key(a, b):
    return "|".join(sorted((a, b)))


def _build_graph(topology, cable_type, domain=None):
    """Graph of only the segments that can actually carry this request:
    right cable type, spare capacity, and matching redundancy domain."""
    g = nx.Graph()
    g.add_nodes_from(topology["racks"].keys())
    for edge in topology["edges"]:
        ct = edge["cable_types"].get(cable_type)
        if not ct:
            continue                                    # trunk doesn't carry this media at all
        if ct["capacity"] - ct["used"] <= 0:
            continue                                    # trunk is full
        if domain is not None and edge["domain"] != domain:
            continue                                    # wrong redundancy leg
        g.add_edge(edge["from"], edge["to"],
                   id=edge["id"], domain=edge["domain"], length_m=edge["length_m"],
                   capacity=ct["capacity"], used=ct["used"],
                   remaining=ct["capacity"] - ct["used"])
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
            hops = nx.shortest_path(g, src_rack, dst_rack, weight="length_m")
        except nx.NetworkXNoPath:
            attempts.append(f"domain {d}: no path with available capacity")
            continue

        segments, total = [], 0.0
        for a, b in zip(hops, hops[1:]):
            e = g.get_edge_data(a, b)
            total += e["length_m"]
            segments.append({
                "edge_id": e["id"], "from_rack": a, "to_rack": b, "domain": e["domain"],
                "length_m": e["length_m"], "capacity": e["capacity"],
                "used_before": e["used"], "remaining_before": e["remaining"],
            })
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
        if not ct or ct["used"] >= ct["capacity"]:
            raise RouteError(f"Trunk '{seg['edge_id']}' no longer has spare capacity — recompute the route.")


def commit_route(topology, route, circuit_id):
    """Apply an approved route: consume the ports, charge the trunks, and
    record the circuit. In production this is where the ITA write-back goes."""
    for seg in route["segments"]:
        for edge in topology["edges"]:
            if edge["id"] == seg["edge_id"]:
                edge["cable_types"][route["cable_type"]]["used"] += 1
                break

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
        "transit_ports": [t["port"] for t in route["transit_points"]],
        "total_length_m": route["total_length_m"],
    }
    return topology["circuits"][circuit_id]
