#!/usr/bin/env python3
"""
patchcords.py
=============
Which patch cords a technician has to bring for a route.

A trunk's length is ITA's business: it is already in the floor. What the
technician carries is the short cords inside each cabinet — device port to
panel at the start and the end, panel to panel at every cross-connect in the
middle. Their length follows from how far apart the two ports sit in the
cabinet: a panel at U40 and a switch at U1 is a 3 m cord (2026-11-04, from the
field: "if the patch is at U40 and the switch at U1 you need a 3 m fiber").

    need  = U gap x one rack unit + SLACK_M
    bring = the shortest stock length that is at least `need`

The three numbers below are the site's to change.
"""

from collections import Counter

U_M = 0.04445        # one rack unit, in metres
SLACK_M = 0.35       # out to the cable manager and back in — 30-40 cm, from the
                     # field (2026-11-05); U40 to U1 -> 3 m, panels 1U apart -> 1 m
STOCK_M = (1, 2, 3, 5, 10)   # the cord lengths kept on the shelf


def _u(topology, port_id):
    dev = topology["devices"].get(topology["ports"][port_id]["device"]) or {}
    return dev.get("u_start")


def _where(topology, port_id):
    p = topology["ports"][port_id]
    dev = topology["devices"].get(p["device"]) or {}
    u = dev.get("u_start")
    return f"U{u if u is not None else '?'}-P{p['index']} ({dev.get('name', p['device'])})"


def cord(topology, a, b):
    """The cord joining ports `a` and `b` of one cabinet, or None when the two
    are not both real ports in the same cabinet (the synthetic map's trunk
    ends are not ports, so it has no cord to measure there)."""
    ports = topology["ports"]
    if a not in ports or b not in ports or ports[a]["rack"] != ports[b]["rack"]:
        return None
    ua, ub = _u(topology, a), _u(topology, b)
    out = {"rack": ports[a]["rack"], "from_port": a, "to_port": b,
           "from": _where(topology, a), "to": _where(topology, b),
           "cable_type": ports[a]["type"],
           "connectors": [c for c in (ports[a].get("connector"), ports[b].get("connector")) if c],
           "gap_u": None, "need_m": None, "stock_m": None}
    if ua is None or ub is None:
        return out                       # ITA has no U for one end: measure on site
    gap = abs(ua - ub)
    need = round(gap * U_M + SLACK_M, 2)
    out.update(gap_u=gap, need_m=need,
               stock_m=next((s for s in STOCK_M if s >= need), None))
    return out


def cords(topology, route):
    """Every cord the route needs, in the order the technician meets them."""
    src, dst = route.get("src_port"), route.get("dst_port")
    if route.get("intra_rack"):
        pairs = [(src, dst)]
    else:
        segs = route.get("segments") or []
        pairs = []
        if segs and segs[0].get("physical"):
            pairs.append((src, segs[0]["strand_port_from"]))
        for tp in route.get("transit_points") or []:
            if tp.get("out_port"):
                pairs.append((tp["port"], tp["out_port"]))
        if segs and segs[-1].get("physical") and dst:
            pairs.append((segs[-1]["strand_port_to"], dst))
    found = (cord(topology, a, b) for a, b in pairs if a and b)
    return [c for c in found if c]


def kit(cord_list):
    """'2 x 3 m fiber' lines: what to take off the shelf."""
    count = Counter()
    for c in cord_list:
        # sorted by length; a cord with no U or past the longest stock goes last
        order = c["stock_m"] if c["stock_m"] else float("inf")
        size = f"{c['stock_m']} m" if c["stock_m"] else (
            "measure on site" if c["need_m"] is None else f"over {STOCK_M[-1]} m")
        plug = f" {'-'.join(c['connectors'])}" if len(c["connectors"]) == 2 else ""
        count[(order, size, c["cable_type"] + plug)] += 1
    return [f"{n} x {size} {kind}" for (_, size, kind), n in sorted(count.items())]


def describe(c):
    """One line per cord for a work order."""
    if c["need_m"] is None:
        size = "U unknown - measure on site"
    elif c["stock_m"] is None:
        size = f"~{c['need_m']} m - longer than any stock cord"
    else:
        size = f"{c['gap_u']}U apart, ~{c['need_m']} m -> {c['stock_m']} m cord"
    return f"{c['rack']}: {c['from']} <-> {c['to']}   {size}"
