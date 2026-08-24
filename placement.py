#!/usr/bin/env python3
"""
placement.py
============
Answers the question a demand sheet cannot: a new switch or server is
arriving — which cabinet does it go in, and at which U?

The sheet declares WHAT is coming (Devices tab: serial, type, size, ports)
and WHAT it must talk to (P2P tab, referring to devices by serial). Placement
sits between the two: it reads the connections asked for, then picks a
position that makes those connections cheap.

WHAT MAKES A POSITION GOOD
--------------------------
Three things, combined into one score:

  proximity   Same cabinet as the thing it talks to is best by a wide margin —
              one patch lead, no trunk, no cross-connect. Then the same row,
              the same pod, the same MDA, the same room.
  room to grow A cabinet that is nearly full is a poor home for new kit: the
              next device has nowhere to go, and neither does a replacement.
  fit         Among positions in one cabinet, prefer the tightest gap that
              still fits, so a 1U box does not sit in the middle of the only
              4U hole left.

RACKS THAT ARE NOT CANDIDATES
-----------------------------
Equipment is never installed in an EOR cabinet or anywhere inside an MDA pod.
Those are cross-connect real estate — patch fields and aggregation switches,
built once and thereafter only patched into. This is a hard exclusion rather
than a low score: proposing such a position at all would be proposing
something the site does not do.
"""

from collections import defaultdict

RACK_U = 42

# Distance rings, cheapest first. The numbers are relative weights, not metres
# — what matters is the ordering and the size of the gaps between rings.
#
# The jump from SAME_RACK to SAME_ROW is deliberately huge. Staying in the
# cabinet means one patch lead: no trunk strand at either end, no cross-connect
# port, no cable pulled through a tray. Leaving the cabinet costs all of that
# even for the very next rack along, so a half-full home cabinet must still
# beat an empty neighbour.
SAME_RACK = 0
SAME_ROW = 200
SAME_POD = 260
SAME_MDA = 500
SAME_ROOM = 700
ELSEWHERE = 1200

# Filling a cabinet from 20% to 50% is unremarkable; from 85% to 95% is a
# problem, because the next device and any replacement have nowhere to go.
# Cubing the ratio keeps the penalty near zero until a cabinet is genuinely
# tight, then makes it bite hard.
FULLNESS_WEIGHT = 400

class PlacementError(Exception):
    """No position exists for this device, and the message says why."""


# --------------------------------------------------------------------------
# free space
# --------------------------------------------------------------------------

def occupancy(topology, rack_id):
    """Set of U numbers already taken in a cabinet.

    A device sits at u_start and grows DOWNWARD: a 2U box at u_start=37
    occupies 37 and 36. Getting this backwards silently double-books space,
    so it is computed in exactly one place.
    """
    taken = set()
    for dev in topology["devices"].values():
        if dev["rack"] != rack_id:
            continue
        top = dev["u_start"]
        for u in range(top - dev["u_size"] + 1, top + 1):
            taken.add(u)
    return taken


def free_gaps(topology, rack_id, extra_taken=()):
    """Contiguous runs of free U, as (top_u, height), largest U first.

    `extra_taken` lets a caller reserve space for devices it has already
    placed in this same planning run but not yet committed.
    """
    taken = occupancy(topology, rack_id) | set(extra_taken)
    gaps, run_top, run_len = [], None, 0
    for u in range(RACK_U, 0, -1):
        if u in taken:
            if run_len:
                gaps.append((run_top, run_len))
            run_top, run_len = None, 0
        else:
            if run_len == 0:
                run_top = u
            run_len += 1
    if run_len:
        gaps.append((run_top, run_len))
    return gaps


def positions_for(topology, rack_id, u_size, extra_taken=()):
    """Every legal top-U where a device of this height fits, plus how much
    slack the gap it lands in would have left over."""
    out = []
    for top, height in free_gaps(topology, rack_id, extra_taken):
        if height < u_size:
            continue
        # only offer the top of each gap: any lower position in the same gap
        # is equivalent electrically and just fragments the space
        out.append({"u_start": top, "gap_height": height, "slack": height - u_size})
    return out


# --------------------------------------------------------------------------
# distance
# --------------------------------------------------------------------------

def _rack_meta(topology, rack_id):
    return topology["racks"].get(rack_id)


def distance_rank(topology, rack_a, rack_b):
    """How far apart two cabinets are, in the rings described at the top."""
    if rack_a == rack_b:
        return SAME_RACK
    ma, mb = _rack_meta(topology, rack_a), _rack_meta(topology, rack_b)
    if not ma or not mb:
        return ELSEWHERE
    if ma["pod"] == mb["pod"]:
        return SAME_ROW if ma["row"] == mb["row"] else SAME_POD

    pods = topology["pods"]
    pa, pb = pods.get(ma["pod"], {}), pods.get(mb["pod"], {})
    mda_a = pa.get("mda") or ma["pod"]
    mda_b = pb.get("mda") or mb["pod"]
    if mda_a == mda_b:
        return SAME_MDA
    if pa.get("room") and pa.get("room") == pb.get("room"):
        return SAME_ROOM
    return ELSEWHERE


def eligible_racks(topology):
    """Cabinets a new device may be installed in.

    Equipment is not racked in EOR cabinets or anywhere in an MDA pod. Those
    are cross-connect real estate: patch fields and aggregation switches that
    the site builds once and then only patches into. So this is a hard
    exclusion, not a low score — offering such a position at all would be
    proposing something the site does not do.

    That leaves the compute cabinets: 504 of the 640 racks here.
    """
    return [rid for rid, meta in topology["racks"].items()
            if not meta.get("is_eor") and not meta.get("is_mda")]


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

def _rack_fullness(topology, rack_id):
    """0.0 empty .. 1.0 full, by U."""
    return len(occupancy(topology, rack_id)) / RACK_U


def _port_headroom(topology, rack_id, cable_type):
    free = sum(1 for p in topology["ports"].values()
               if p["rack"] == rack_id and p["status"] == "free"
               and (not cable_type or p["type"] == cable_type))
    return free


def rank_positions(topology, device, neighbours, extra_taken=None, limit=5):
    """Score every position a device could take and return the best few.

    `device`     the declared spec: type, u_size, ports
    `neighbours` rack ids this device must reach (from the P2P rows that name
                 its serial). Empty means "no connections asked for", and then
                 only space and headroom matter.
    `extra_taken` {rack_id: set(u)} already spoken for by earlier placements
                 in this same run.
    """
    extra_taken = extra_taken or {}
    u_size = device["u_size"]

    scored = []
    for rack_id in eligible_racks(topology):
        spots = positions_for(topology, rack_id, u_size,
                              extra_taken.get(rack_id, ()))
        if not spots:
            continue

        if neighbours:
            # a device usually talks to more than one thing; the position has
            # to be good for ALL of them, so take the total, not the best hop
            proximity = sum(distance_rank(topology, rack_id, n) for n in neighbours)
            proximity /= len(neighbours)
        else:
            proximity = 0.0

        fullness = _rack_fullness(topology, rack_id)

        best = min(spots, key=lambda s: s["slack"])
        # tightest fit first, so small kit does not eat the last big gap
        fit_penalty = best["slack"] * 0.5

        score = proximity + (fullness ** 3) * FULLNESS_WEIGHT + fit_penalty
        scored.append({
            "rack": rack_id,
            "u_start": best["u_start"],
            "u_end": best["u_start"] - u_size + 1,
            "score": round(score, 1),
            "proximity": round(proximity, 1),
            "rack_fullness_pct": round(fullness * 100),
            "gap_height": best["gap_height"],
            "pod": topology["racks"][rack_id]["pod"],
            "reason": _reason(topology, rack_id, neighbours, fullness),
            "alternatives_in_rack": len(spots),
        })

    if not scored:
        raise PlacementError(
            f"no cabinet has {u_size}U of contiguous free space for "
            f"{device.get('serial', 'this device')}")

    scored.sort(key=lambda s: (s["score"], s["rack"]))
    return scored[:limit]


def _reason(topology, rack_id, neighbours, fullness):
    bits = []
    if neighbours:
        if any(n == rack_id for n in neighbours):
            bits.append("same cabinet as what it connects to — a patch lead, no trunk")
        else:
            ring = min(distance_rank(topology, rack_id, n) for n in neighbours)
            bits.append({
                SAME_ROW: "same row as what it connects to",
                SAME_POD: "same pod as what it connects to",
                SAME_MDA: "shares an MDA with what it connects to",
                SAME_ROOM: "same room as what it connects to",
            }.get(ring, "reachable, but not close to what it connects to"))
    bits.append(f"cabinet {round(fullness * 100)}% full")
    return " · ".join(bits)


# --------------------------------------------------------------------------
# ordering a batch
# --------------------------------------------------------------------------

def order_by_dependency(new_devices, links):
    """New kit must be placed before anything that connects to it, or its
    position is unknown when the dependent device is scored.

    `links` is a list of (serial_a, serial_b) from the P2P rows. Returns the
    serials in a safe order, and any that sit in a dependency cycle.
    """
    serials = {d["serial"] for d in new_devices}
    deps = defaultdict(set)
    for a, b in links:
        if a in serials and b in serials and a != b:
            # neither can be placed first with certainty, so treat it as a
            # mutual dependency and let the cycle check report it
            deps[a].add(b)

    ordered, placed = [], set()
    # Kahn's algorithm, but tolerant: a device whose dependencies are all
    # EXISTING kit has no entries here and comes out first.
    pending = sorted(serials)
    while pending:
        ready = [s for s in pending if not (deps[s] - placed)]
        if not ready:
            break                       # everything left is in a cycle
        for s in ready:
            ordered.append(s)
            placed.add(s)
        pending = [s for s in pending if s not in placed]

    return ordered, sorted(pending)
