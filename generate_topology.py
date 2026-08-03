#!/usr/bin/env python3
"""
generate_topology.py  (v2 — rows + 42U + devices)
==================================================
Builds the synthetic "mini ITA" map we develop against, so the routing logic
can be proven out long before anyone touches the production system.

PHYSICAL LAYOUT MODELED
-----------------------
  2 pods: A and B.
  Each pod has two rows: S and N, 10 rack positions each.
  Rack naming: <POD>-<ROW><NN>   e.g.  A-S01, A-S07, B-N04
  One EOR per row, at opposite ends of the pod (as you specified):
      row S -> the EOR sits at position 01   (A-S01, B-S01)
      row N -> the EOR sits at position 10   (A-N10, B-N10)
  So each row is 1 EOR + 9 compute racks; 18 compute racks per pod.

  Every rack is 42U and holds real, U-positioned devices. Ports belong to
  devices, not to racks — that is what lets the UI say "U42, FIB-PP-01,
  port 12" the way ITA does.

REDUNDANCY (A/B)
----------------
  domain A  = anything routed through the row-S EOR
  domain B  = anything routed through the row-N EOR
  Every compute rack — in BOTH rows — has an uplink trunk to each of the two
  EORs. That is the assumption I made so a redundant pair of connections can
  be routed with genuinely zero shared physical segments. If in reality a
  rack only uplinks to its own row's EOR, delete the second uplink in
  build_edges() and the engine still works (you just lose per-rack A/B).

  Between pods, only EOR-to-EOR, fiber only, domain-consistent:
      A-S01 <-> B-S01   (domain A backbone)
      A-N10 <-> B-N10   (domain B backbone)
  There is deliberately no copper between pods, which is why a cross-pod
  copper request correctly fails instead of silently inventing a path.

Numbers below are my assumptions — all in the CONFIG block, easy to change.
"""

import random
from pathlib import Path

from pathengine import resolve_path, commit_route, save_topology, RouteError

random.seed(42)  # same map every run

# ---------------------------------------------------------------- CONFIG --
PODS = ("A", "B")
ROWS = ("S", "N")
POSITIONS_PER_ROW = 10
EOR_POSITION = {"S": 1, "N": 10}      # EORs at opposite ends of the pod
RACK_U = 42

UPLINK_FIBER = 12                      # strands per rack->EOR trunk
UPLINK_COPPER = 24
BACKBONE_FIBER = 144                   # strands per inter-pod EOR<->EOR trunk
BACKBONE_COPPER = 0                    # no copper between pods, by design

RACK_PITCH_M = 2.5                     # distance between adjacent rack positions
CROSS_ROW_M = 15.0                     # extra distance to reach the other row
SLACK_M = 5.0                          # patch slack at each end

TARGET_CIRCUITS = 260                  # pre-existing connections to seed

OUT_PATH = Path(__file__).parent / "data" / "topology.json"


# ------------------------------------------------------------- BUILDING ---

def rack_id(pod, row, pos):
    return f"{pod}-{row}{pos:02d}"


def is_eor(row, pos):
    return EOR_POSITION[row] == pos


def add_device(topology, rack, name, dev_type, u_start, u_size, fiber, copper, label):
    dev_id = f"{rack}:{name}"
    topology["devices"][dev_id] = {
        "id": dev_id, "rack": rack, "name": name, "type": dev_type,
        "u_start": u_start, "u_size": u_size, "label": label,
        "fiber_ports": fiber, "copper_ports": copper,
    }
    for i in range(1, fiber + 1):
        pid = f"{dev_id}:{i}"
        topology["ports"][pid] = {"id": pid, "rack": rack, "device": dev_id, "index": i,
                                  "type": "fiber", "status": "free",
                                  "peer": None, "circuit": None, "role": None}
    for i in range(fiber + 1, fiber + copper + 1):
        pid = f"{dev_id}:{i}"
        topology["ports"][pid] = {"id": pid, "rack": rack, "device": dev_id, "index": i,
                                  "type": "copper", "status": "free",
                                  "peer": None, "circuit": None, "role": None}
    return dev_id


def populate_compute_rack(topology, rack):
    """Typical compute cabinet: patch panels and a ToR at the top, servers below."""
    add_device(topology, rack, "FIB-PP-01", "fiber_patch_panel", 42, 1, 24, 0,
               "Fiber Patch Panel 24x LC")
    add_device(topology, rack, "CU-PP-01", "copper_patch_panel", 41, 1, 0, 24,
               "Copper Patch Panel 24x RJ45")
    add_device(topology, rack, "TOR-SW-01", "switch", 40, 1, 4, 48,
               "Top-of-Rack Switch 48x RJ45 + 4x SFP+")
    u = 37
    for n in range(1, random.randint(4, 10) + 1):
        add_device(topology, rack, f"SRV-{n:02d}", "server", u, 2, 1, 1,
                   "Rack Server 2U")
        u -= 2


def populate_eor_rack(topology, rack):
    """EOR cabinet: a large high-density cross-connect field plus the
    aggregation switches. The field is deliberately sized a little above the
    total uplink capacity feeding into it (18 racks x 12 fiber = 216 strands,
    x 24 copper = 432 pairs), so the binding constraint on a route is the
    TRUNK — the way it is in a real room — and not the panel."""
    u = 42
    for n in range(1, 6):                                   # 5 x 48 = 240 fiber
        add_device(topology, rack, f"FIB-PP-{n:02d}", "fiber_patch_panel", u, 1, 48, 0,
                   "HD Fiber Patch Panel 48x LC")
        u -= 1
    u -= 1
    for n in range(1, 10):                                  # 9 x 48 = 432 copper
        add_device(topology, rack, f"CU-PP-{n:02d}", "copper_patch_panel", u, 1, 0, 48,
                   "HD Copper Patch Panel 48x RJ45")
        u -= 1
    u -= 1
    for n in range(1, 3):
        add_device(topology, rack, f"EOR-SW-{n:02d}", "switch", u, 2, 8, 48,
                   "EOR Aggregation Switch 48x RJ45 + 8x SFP+")
        u -= 3


def make_edge(edge_id, a, b, domain, fiber, copper, length_m):
    cable_types = {}
    if fiber:
        cable_types["fiber"] = {"capacity": fiber, "used": 0}
    if copper:
        cable_types["copper"] = {"capacity": copper, "used": 0}
    return {"id": edge_id, "from": a, "to": b, "domain": domain,
            "cable_types": cable_types, "length_m": round(length_m, 1)}


def build_edges(topology):
    edges = []
    for pod in PODS:
        eor_s = rack_id(pod, "S", EOR_POSITION["S"])   # domain A
        eor_n = rack_id(pod, "N", EOR_POSITION["N"])   # domain B
        for row in ROWS:
            for pos in range(1, POSITIONS_PER_ROW + 1):
                if is_eor(row, pos):
                    continue
                rid = rack_id(pod, row, pos)
                # distance to the row-S EOR (position 1 of row S)
                d_s = SLACK_M + abs(pos - EOR_POSITION["S"]) * RACK_PITCH_M
                d_n = SLACK_M + abs(pos - EOR_POSITION["N"]) * RACK_PITCH_M
                if row == "N":
                    d_s += CROSS_ROW_M
                else:
                    d_n += CROSS_ROW_M
                edges.append(make_edge(f"{rid}->{eor_s}", rid, eor_s, "A",
                                       UPLINK_FIBER, UPLINK_COPPER, d_s))
                edges.append(make_edge(f"{rid}->{eor_n}", rid, eor_n, "B",
                                       UPLINK_FIBER, UPLINK_COPPER, d_n))

    edges.append(make_edge("A-S01<->B-S01", rack_id("A", "S", 1), rack_id("B", "S", 1),
                           "A", BACKBONE_FIBER, BACKBONE_COPPER, 210.0))
    edges.append(make_edge("A-N10<->B-N10", rack_id("A", "N", 10), rack_id("B", "N", 10),
                           "B", BACKBONE_FIBER, BACKBONE_COPPER, 210.0))
    return edges


def build_skeleton():
    topology = {
        "meta": {
            "description": "Synthetic sandbox 'mini ITA' — 2 pods x 2 rows (S/N) x 10 positions, 42U racks",
            "seed": 42, "rack_u": RACK_U,
        },
        "pods": {}, "racks": {}, "devices": {}, "ports": {}, "edges": [], "circuits": {},
    }

    for pod in PODS:
        topology["pods"][pod] = {"rows": {}}
        for row in ROWS:
            members = []
            for pos in range(1, POSITIONS_PER_ROW + 1):
                rid = rack_id(pod, row, pos)
                eor = is_eor(row, pos)
                topology["racks"][rid] = {
                    "id": rid, "pod": pod, "row": row, "position": pos,
                    "is_eor": eor, "u_height": RACK_U,
                    "domain": ("A" if row == "S" else "B") if eor else None,
                }
                members.append(rid)
                if eor:
                    populate_eor_rack(topology, rid)
                else:
                    populate_compute_rack(topology, rid)
            topology["pods"][pod]["rows"][row] = {
                "racks": members,
                "eor": rack_id(pod, row, EOR_POSITION[row]),
            }

    topology["edges"] = build_edges(topology)
    return topology


# ------------------------------------------------- SEEDING REAL CIRCUITS --

def seed_circuits(topology, target=TARGET_CIRCUITS):
    """Create the 'already installed' connections by genuinely routing them
    through the engine and committing them. This keeps the dataset perfectly
    self-consistent: trunk usage always equals the number of circuits crossing
    it, and every used port really does have a far end to display."""
    compute_racks = [r for r, m in topology["racks"].items() if not m["is_eor"]]
    made = 0
    attempts = 0
    while made < target and attempts < target * 20:
        attempts += 1
        a_rack, b_rack = random.sample(compute_racks, 2)
        cable_type = "fiber" if random.random() < 0.55 else "copper"

        a_port = _random_free_port(topology, a_rack, cable_type)
        b_port = _random_free_port(topology, b_rack, cable_type)
        if not a_port or not b_port:
            continue
        try:
            route = resolve_path(a_port, b_port, topology=topology)
        except RouteError:
            continue
        made += 1
        commit_route(topology, route, f"CIR-{made:04d}")
    return made


def _random_free_port(topology, rack, cable_type):
    pool = [pid for pid, p in topology["ports"].items()
            if p["rack"] == rack and p["type"] == cable_type and p["status"] == "free"]
    return random.choice(pool) if pool else None


# ------------------------------------------------------------------ MAIN --

def build():
    topology = build_skeleton()
    n = seed_circuits(topology)
    save_topology(topology, OUT_PATH)

    used_ports = sum(1 for p in topology["ports"].values() if p["status"] == "used")
    print(f"Wrote {OUT_PATH}")
    print(f"  racks   : {len(topology['racks'])} ({len(PODS)} pods x {len(ROWS)} rows x {POSITIONS_PER_ROW})")
    print(f"  devices : {len(topology['devices'])}")
    print(f"  ports   : {len(topology['ports'])}  ({used_ports} in use)")
    print(f"  trunks  : {len(topology['edges'])}")
    print(f"  circuits: {n} pre-existing connections")


if __name__ == "__main__":
    build()
