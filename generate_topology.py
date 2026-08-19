#!/usr/bin/env python3
"""
generate_topology.py  (v3 — 4 columns x 8 pods + MDA hierarchy)
=================================================================
Builds the synthetic "mini ITA" map we develop against, so the routing logic
can be proven out long before anyone touches the production system.

PHYSICAL LAYOUT MODELED
------------------------
  4 columns: A, B, C, D. 8 pod positions per column (1..8).
  Pod id = <COLUMN><POSITION>, e.g. A1, D5. 32 pods total.

  Two rooms:
      room AB = columns A, B
      room CD = columns C, D

  4 of the 32 pods are MDA ("Main Distribution Area") pods — dedicated
  cross-connect/core hubs, no compute. As specified: A2, A6, D2, D6 (the
  outer column of each room, at positions 2 and 6). The other 28 pods are
  regular compute pods.

  Every pod — MDA or regular — has the SAME physical shape: 2 rows (S and
  N), 10 rack positions per row, one EOR-style hub rack per row at opposite
  ends (row S -> position 01, row N -> position 10). Rack naming:
      <POD>-<ROW><NN>          e.g.  D5-N05
  Port/component addressing (human-readable, used in the UI and Work
  Order):
      <POD>-<ROW><NN>-U<UU>-P<PP>     e.g.  D5-N05-U20-P33

  REGULAR pod: 9 compute racks + 1 EOR rack per row (as in v2). The EOR
  aggregates its row's compute racks and uplinks OUT to this pod's assigned
  MDA — see below.

  MDA pod: no compute racks at all. Its S01/N10 positions are HUB racks
  (patch fields + core switches, sized generously so the trunk — not the
  panel — stays the binding constraint). The other 18 positions are filler
  racks (small reserved cross-connect capacity, not wired into the routing
  graph) — present so the pod still "looks like" a normal 2x10 pod on the
  map, per spec.

ASSIGNING REGULAR PODS TO AN MDA
---------------------------------
  Within a room, a regular pod uplinks to whichever MDA sits closest by
  position number: positions 1-4 -> the room's position-2 MDA, positions
  5-8 -> the room's position-6 MDA. Column doesn't matter — e.g. B3 (room
  AB, position 3) uplinks to A2 even though A2 is physically in column A.
  This gives each MDA exactly 7 served regular pods (3 from its own column
  + 4 from the other column in the room, or vice versa).

REDUNDANCY (A/B), now hierarchical
------------------------------------
  domain A = anything routed through a row-S hub (pod-local EOR, or MDA
             hub at S01)
  domain B = anything routed through a row-N hub (pod-local EOR, or MDA
             hub at N10)

  Chain for a regular pod's domain-A leg:
      compute rack -> pod's own S01 EOR -> assigned MDA's S01 hub
  Chain for domain-B is the mirror, via N10 hubs.

  MDA-to-MDA backbone (fiber only, no copper — same "no copper on long
  backbones" rule as v2):
    - within a room: the two MDAs' same-domain hubs connect directly
      (A2-S01<->A6-S01 for domain A, A2-N10<->A6-N10 for domain B; same
      pattern for D2/D6).
    - between rooms: ONE link per domain, so each domain still never
      shares a physical segment with the other: A2-S01<->D2-S01 carries
      domain A between rooms, A6-N10<->D6-N10 carries domain B. (Using the
      position-2 MDAs for the domain-A cross-room hop and the position-6
      MDAs for domain-B keeps the two inter-room paths on totally
      disjoint hub racks, not just disjoint trunks.)

  Every regular pod, in BOTH rows, still uplinks to its own row's EOR only
  (v2's "every compute rack uplinks to both EORs" assumption stays at the
  rack->pod-EOR layer; the NEW hierarchical hop is pod-EOR->MDA, one trunk
  per domain per pod).

Numbers below (CONFIG) are assumptions, same as v2 — easy to change.
"""

import random
from pathlib import Path

from pathengine import resolve_path, commit_route, save_topology, RouteError

random.seed(42)  # same map every run

# ---------------------------------------------------------------- CONFIG --
COLUMNS = ("A", "B", "C", "D")
PODS_PER_COLUMN = 8
ROOM_OF_COLUMN = {"A": "AB", "B": "AB", "C": "CD", "D": "CD"}
MDA_COLUMN_OF_ROOM = {"AB": "A", "CD": "D"}   # which column hosts that room's MDAs
MDA_POSITIONS = (2, 6)                        # pod-number positions that are MDA hubs

ROWS = ("S", "N")
POSITIONS_PER_ROW = 10
EOR_POSITION = {"S": 1, "N": 10}      # hub racks at opposite ends of every pod
RACK_U = 42

UPLINK_FIBER = 12                      # strands per compute-rack -> pod-EOR trunk
UPLINK_COPPER = 24
EOR_TO_MDA_FIBER = 96                  # strands per pod-EOR -> assigned-MDA trunk
EOR_TO_MDA_COPPER = 60
MDA_BACKBONE_FIBER = 192               # strands per room-internal MDA<->MDA trunk
CROSS_ROOM_FIBER = 288                 # strands per AB<->CD room backbone (the single
                                        # chokepoint for all inter-room traffic in one
                                        # domain, so it gets extra headroom)
MDA_BACKBONE_COPPER = 0                # no copper on MDA backbones, by design

RACK_PITCH_M = 2.5                     # distance between adjacent rack positions
CROSS_ROW_M = 15.0                     # extra distance to reach the other row's EOR
SLACK_M = 5.0                          # patch slack at each end
POD_PITCH_M = 30.0                     # distance between adjacent pod positions in a column
COLUMN_GAP_M = 20.0                    # extra distance when the EOR's column != the MDA's column
ROOM_GAP_M = 300.0                     # extra distance for the AB<->CD room backbone

TARGET_CIRCUITS = 1000                 # pre-existing connections to seed

OUT_PATH = Path(__file__).parent / "data" / "topology.json"


# ------------------------------------------------------------- BUILDING ---

def pod_id(col, num):
    return f"{col}{num}"


def is_mda_pod(col, num):
    room = ROOM_OF_COLUMN[col]
    return col == MDA_COLUMN_OF_ROOM[room] and num in MDA_POSITIONS


def assign_mda(col, num):
    """Which MDA pod a regular pod's hubs uplink to."""
    room = ROOM_OF_COLUMN[col]
    mda_col = MDA_COLUMN_OF_ROOM[room]
    mda_num = 2 if num <= 4 else 6
    return pod_id(mda_col, mda_num)


def rack_id(pod, row, pos):
    return f"{pod}-{row}{pos:02d}"


def is_eor_position(row, pos):
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


def populate_pod_eor_rack(topology, rack):
    """Pod-level EOR: aggregates the 9 compute racks in its row and uplinks
    out to the pod's assigned MDA. Sized above the total uplink capacity
    feeding into it (9 racks x 12 fiber = 108 strands, x 24 copper = 216
    pairs) so the TRUNK stays the binding constraint, not the panel."""
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


def populate_mda_hub_rack(topology, rack):
    """MDA hub: the S01/N10 position of an MDA pod. Lands every served pod's
    EOR uplink for this domain (up to 7 pods x 96 strands = 672) plus, on the
    two hub racks that also carry the room-internal (192) AND the cross-room
    (288) backbone. Sized comfortably above that worst case
    (~1152 fiber / ~420 copper)."""
    u = 42
    for n in range(1, 26):                                  # 25 x 48 = 1200 fiber
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
        add_device(topology, rack, f"CORE-SW-{n:02d}", "switch", u, 2, 16, 32,
                   "MDA Core Switch 32x RJ45 + 16x SFP+")
        u -= 3


def populate_mda_filler_rack(topology, rack):
    """Non-hub position inside an MDA pod: reserved cross-connect capacity,
    physically present (so the pod still looks like a normal 2x10 pod) but
    not wired into the routing graph."""
    add_device(topology, rack, "FIB-PP-01", "fiber_patch_panel", 42, 1, 48, 0,
               "Fiber Patch Panel 48x LC (reserved)")
    add_device(topology, rack, "CU-PP-01", "copper_patch_panel", 41, 1, 0, 24,
               "Copper Patch Panel 24x RJ45 (reserved)")


def make_edge(edge_id, a, b, domain, fiber, copper, length_m):
    cable_types = {}
    if fiber:
        cable_types["fiber"] = {"capacity": fiber, "used": 0}
    if copper:
        cable_types["copper"] = {"capacity": copper, "used": 0}
    return {"id": edge_id, "from": a, "to": b, "domain": domain,
            "cable_types": cable_types, "length_m": round(length_m, 1)}


def build_edges(topology, pods):
    edges = []

    # 1. compute rack -> pod EOR, inside every regular pod. Every compute
    #    rack, in BOTH rows, uplinks to BOTH the row-S and row-N EOR (v2's
    #    dual-homing assumption) so a redundant pair can be routed with
    #    zero shared physical segments without having to relocate racks.
    for pid, meta in pods.items():
        if meta["is_mda"]:
            continue
        eor_s = rack_id(pid, "S", EOR_POSITION["S"])
        eor_n = rack_id(pid, "N", EOR_POSITION["N"])
        for row in ROWS:
            for pos in range(1, POSITIONS_PER_ROW + 1):
                if is_eor_position(row, pos):
                    continue
                rid = rack_id(pid, row, pos)
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

    # 2. pod EOR -> assigned MDA hub, one trunk per domain per regular pod.
    for pid, meta in pods.items():
        if meta["is_mda"]:
            continue
        mda = meta["mda"]
        for row in ROWS:
            eor_rack = rack_id(pid, row, EOR_POSITION[row])
            hub_rack = rack_id(mda, row, EOR_POSITION[row])
            d = (SLACK_M + abs(meta["number"] - pods[mda]["number"]) * POD_PITCH_M
                 + (0 if meta["column"] == pods[mda]["column"] else COLUMN_GAP_M))
            edges.append(make_edge(f"{eor_rack}->{hub_rack}", eor_rack, hub_rack,
                                   "A" if row == "S" else "B",
                                   EOR_TO_MDA_FIBER, EOR_TO_MDA_COPPER, d))

    # 3. MDA <-> MDA backbone: within-room (same domain, same position pair)
    #    and one cross-room link per domain.
    for room, mda_col in MDA_COLUMN_OF_ROOM.items():
        m2, m6 = pod_id(mda_col, 2), pod_id(mda_col, 6)
        for row in ROWS:
            hub2, hub6 = rack_id(m2, row, EOR_POSITION[row]), rack_id(m6, row, EOR_POSITION[row])
            d = SLACK_M + abs(2 - 6) * POD_PITCH_M
            edges.append(make_edge(f"{hub2}<->{hub6}", hub2, hub6,
                                   "A" if row == "S" else "B",
                                   MDA_BACKBONE_FIBER, MDA_BACKBONE_COPPER, d))

    a_col, d_col = MDA_COLUMN_OF_ROOM["AB"], MDA_COLUMN_OF_ROOM["CD"]
    d_cross = SLACK_M + ROOM_GAP_M
    hubA2 = rack_id(pod_id(a_col, 2), "S", EOR_POSITION["S"])
    hubD2 = rack_id(pod_id(d_col, 2), "S", EOR_POSITION["S"])
    edges.append(make_edge(f"{hubA2}<->{hubD2}", hubA2, hubD2, "A",
                           CROSS_ROOM_FIBER, MDA_BACKBONE_COPPER, d_cross))
    hubA6 = rack_id(pod_id(a_col, 6), "N", EOR_POSITION["N"])
    hubD6 = rack_id(pod_id(d_col, 6), "N", EOR_POSITION["N"])
    edges.append(make_edge(f"{hubA6}<->{hubD6}", hubA6, hubD6, "B",
                           CROSS_ROOM_FIBER, MDA_BACKBONE_COPPER, d_cross))

    return edges


def build_skeleton():
    topology = {
        "meta": {
            "description": "Synthetic sandbox 'mini ITA' — 4 columns x 8 pods, "
                            "4 of which are dedicated MDA hubs, 42U racks",
            "seed": 42, "rack_u": RACK_U,
            "columns": list(COLUMNS), "pods_per_column": PODS_PER_COLUMN,
            "rooms": {"AB": ["A", "B"], "CD": ["C", "D"]},
        },
        "pods": {}, "racks": {}, "devices": {}, "ports": {}, "edges": [], "circuits": {},
    }

    # -- pod registry (column/position/room/MDA bookkeeping) --
    pods = {}
    for col in COLUMNS:
        for num in range(1, PODS_PER_COLUMN + 1):
            pid = pod_id(col, num)
            mda = is_mda_pod(col, num)
            pods[pid] = {
                "id": pid, "column": col, "number": num,
                "room": ROOM_OF_COLUMN[col], "is_mda": mda,
                "mda": None if mda else assign_mda(col, num),
            }
    for pid, meta in pods.items():
        if meta["is_mda"]:
            meta["serves"] = sorted(p for p, m in pods.items() if m.get("mda") == pid)

    # -- racks + devices --
    for pid, meta in pods.items():
        topology["pods"][pid] = {
            "id": pid, "column": meta["column"], "number": meta["number"],
            "room": meta["room"], "is_mda": meta["is_mda"],
            "mda": meta["mda"], "serves": meta.get("serves"),
            "rows": {},
        }
        for row in ROWS:
            members = []
            for pos in range(1, POSITIONS_PER_ROW + 1):
                rid = rack_id(pid, row, pos)
                hub = is_eor_position(row, pos)
                topology["racks"][rid] = {
                    "id": rid, "pod": pid, "row": row, "position": pos,
                    "is_eor": hub, "is_mda": meta["is_mda"], "u_height": RACK_U,
                    "domain": ("A" if row == "S" else "B") if hub else None,
                }
                members.append(rid)
                if meta["is_mda"]:
                    populate_mda_hub_rack(topology, rid) if hub else populate_mda_filler_rack(topology, rid)
                else:
                    populate_pod_eor_rack(topology, rid) if hub else populate_compute_rack(topology, rid)
            topology["pods"][pid]["rows"][row] = {
                "racks": members, "eor": rack_id(pid, row, EOR_POSITION[row]),
            }

    topology["edges"] = build_edges(topology, pods)
    return topology


# ------------------------------------------------- SEEDING REAL CIRCUITS --

def seed_circuits(topology, target=TARGET_CIRCUITS):
    """Create the 'already installed' connections by genuinely routing them
    through the engine and committing them. This keeps the dataset perfectly
    self-consistent: trunk usage always equals the number of circuits crossing
    it, and every used port really does have a far end to display."""
    compute_racks = [r for r, m in topology["racks"].items()
                     if not m["is_eor"] and not m["is_mda"]]
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
    mda_pods = [p for p, m in topology["pods"].items() if m["is_mda"]]
    print(f"Wrote {OUT_PATH}")
    print(f"  pods    : {len(topology['pods'])} ({len(COLUMNS)} columns x {PODS_PER_COLUMN}), "
          f"MDA: {', '.join(mda_pods)}")
    print(f"  racks   : {len(topology['racks'])}")
    print(f"  devices : {len(topology['devices'])}")
    print(f"  ports   : {len(topology['ports'])}  ({used_ports} in use)")
    print(f"  trunks  : {len(topology['edges'])}")
    print(f"  circuits: {n} pre-existing connections")


if __name__ == "__main__":
    build()
