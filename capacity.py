#!/usr/bin/env python3
"""
capacity.py
===========
What is about to run out.

Every route consumes a strand on each trunk it crosses, and trunks are shared:
one that fills up stops being available to everyone, not just to whoever filled
it. Until this existed the only way to learn that a trunk was full was to plan
something across it and have the plan fail — which is the wrong end of the
problem, and exactly how a lab map got quietly consumed until routing broke.

TWO DIFFERENT THINGS RUN OUT
----------------------------
Trunk strands are the shared bottleneck: a full trunk between two rooms blocks
every future route between them. Device ports are local: a full patch panel
only blocks connections to that panel. Both are reported, separately, because
"we are out of capacity" means something different in each case.

A trunk is classified by what it joins, since that is what decides how much it
matters — a cross-room backbone at 90% is a site-wide problem, one cabinet's
uplink at 90% is a cabinet's problem.
"""

TRUNK_KINDS = {
    "rack_to_eor": "מסד → EOR",
    "eor_to_mda": "EOR → MDA",
    "backbone_room": "עמוד שדרה תוך-חדרי",
    "backbone_cross": "עמוד שדרה בין חדרים",
    "other": "אחר",
}

# Where "nearly full" begins. A trunk past this has room for a handful more
# connections and should be visible before someone plans across it.
WARN_PCT = 75
FULL_PCT = 100


def classify(topology, edge):
    """What kind of trunk this is, from the cabinets it joins."""
    racks, pods = topology["racks"], topology["pods"]
    a, b = racks.get(edge["from"]), racks.get(edge["to"])
    if not a or not b:
        return "other"
    a_mda, b_mda = pods[a["pod"]]["is_mda"], pods[b["pod"]]["is_mda"]

    if a_mda and b_mda:
        same_room = pods[a["pod"]]["room"] == pods[b["pod"]]["room"]
        return "backbone_room" if same_room else "backbone_cross"
    if a["pod"] == b["pod"]:
        return "rack_to_eor"
    if a_mda or b_mda:
        return "eor_to_mda"
    return "other"


def _trunk_rows(topology):
    for edge in topology["edges"]:
        kind = classify(topology, edge)
        for media, ct in edge["cable_types"].items():
            cap = ct["capacity"]
            used = len(ct.get("strands") or {})
            if not cap:
                continue
            yield {
                "id": edge["id"], "media": media, "kind": kind,
                "kind_he": TRUNK_KINDS[kind],
                "from": edge["from"], "to": edge["to"],
                "domain": edge["domain"], "length_m": edge["length_m"],
                "capacity": cap, "used": used, "remaining": cap - used,
                "pct": round(100 * used / cap, 1),
            }


def report(topology, tightest=25, racks=15):
    """Everything worth knowing about what is filling up.

    `tightest` and `racks` bound the two lists; the totals are always over
    everything, so a truncated list never makes the summary wrong.
    """
    rows = list(_trunk_rows(topology))

    by_media, by_kind = {}, {}
    for r in rows:
        for bucket, key in ((by_media, r["media"]), (by_kind, r["kind"])):
            b = bucket.setdefault(key, {"trunks": 0, "capacity": 0, "used": 0,
                                        "full": 0, "warn": 0})
            b["trunks"] += 1
            b["capacity"] += r["capacity"]
            b["used"] += r["used"]
            b["full"] += r["remaining"] == 0
            b["warn"] += 0 < r["remaining"] and r["pct"] >= WARN_PCT
    for b in list(by_media.values()) + list(by_kind.values()):
        b["remaining"] = b["capacity"] - b["used"]
        b["pct"] = round(100 * b["used"] / b["capacity"], 1) if b["capacity"] else 0.0
    for key, b in by_kind.items():
        b["kind"] = key
        b["kind_he"] = TRUNK_KINDS[key]

    # Ordered by how close to full, then by how little is left — a 96% trunk
    # with two strands spare matters more than a 96% trunk with twenty.
    hot = sorted(rows, key=lambda r: (-r["pct"], r["remaining"]))
    full = [r for r in rows if r["remaining"] == 0]

    # Device ports: a different supply, and it runs out locally rather than
    # site-wide, so it gets its own list instead of being mixed in.
    per_rack = {}
    for p in topology["ports"].values():
        b = per_rack.setdefault(p["rack"], {"rack": p["rack"], "total": 0, "used": 0})
        b["total"] += 1
        b["used"] += p["status"] == "used"
    for b in per_rack.values():
        b["free"] = b["total"] - b["used"]
        b["pct"] = round(100 * b["used"] / b["total"], 1) if b["total"] else 0.0
        b["pod"] = topology["racks"][b["rack"]]["pod"]
    tight_racks = sorted(per_rack.values(), key=lambda b: (-b["pct"], b["free"]))

    ports_total = sum(b["total"] for b in per_rack.values())
    ports_used = sum(b["used"] for b in per_rack.values())

    return {
        "warn_pct": WARN_PCT,
        "totals": {
            "trunks": len(rows),
            "capacity": sum(r["capacity"] for r in rows),
            "used": sum(r["used"] for r in rows),
            "remaining": sum(r["remaining"] for r in rows),
            "full": len(full),
            "warn": sum(1 for r in rows if 0 < r["remaining"] and r["pct"] >= WARN_PCT),
            "circuits": len(topology.get("circuits") or {}),
            "ports_total": ports_total,
            "ports_used": ports_used,
            "ports_free": ports_total - ports_used,
        },
        "by_media": by_media,
        "by_kind": sorted(by_kind.values(), key=lambda b: -b["pct"]),
        "tightest": hot[:tightest],
        "full": full[:tightest],
        "tight_racks": tight_racks[:racks],
    }
