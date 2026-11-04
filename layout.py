#!/usr/bin/env python3
"""
layout.py
=========
Where a cabinet is — asked the same way whatever kind of site the map
describes.

The synthetic map is a classic layout: every cabinet sits in a pod of two
facing rows, with an EOR at the end of each and an MDA serving the pod. A map
converted from ITA has none of that spelled out (ITA knows rooms, rows and
racks; pods, EOR and MDA are "just known" on site), and a real site mixes
classic pods with plain rows and cabinets that stand on their own. So every
question about a cabinet's surroundings goes through here, and each answer is
None when the map simply does not say — never a KeyError, and never a guess.

    pod_of / row_of / room_of   the groups a cabinet belongs to, or None
    is_eor / is_mda             cross-connect real estate: no equipment goes in
    mda_of                      the MDA that serves the cabinet's pod, if known
    units_of                    every name a cabinet can be addressed by in
                                configuration (zones, constraints): its own id,
                                its row, its pod, its room

A row is identified by (pod, row) on the synthetic map — row "S" exists in
every pod — and by its own name on a map from ITA, where rows are named.
"""


def _rack(topology, rack_id):
    return (topology.get("racks") or {}).get(rack_id) or {}


def _pod(topology, rack_id):
    return (topology.get("pods") or {}).get(_rack(topology, rack_id).get("pod")) or {}


def pod_of(topology, rack_id):
    return _rack(topology, rack_id).get("pod")


def row_of(topology, rack_id):
    """A hashable identity for the cabinet's row, or None if it is in none."""
    r = _rack(topology, rack_id)
    if r.get("row") is None:
        return None
    return (r["pod"], r["row"]) if r.get("pod") is not None else r["row"]


def row_name(topology, rack_id):
    """The row as a person would name it: 'A1-S' on the synthetic map, the
    ITA row's own name on a converted one."""
    r = _rack(topology, rack_id)
    if r.get("row") is None:
        return None
    return f"{r['pod']}-{r['row']}" if r.get("pod") is not None else str(r["row"])


def room_of(topology, rack_id):
    return _rack(topology, rack_id).get("room") or _pod(topology, rack_id).get("room")


def is_mda(topology, rack_id):
    return bool(_rack(topology, rack_id).get("is_mda") or _pod(topology, rack_id).get("is_mda"))


def is_eor(topology, rack_id):
    return bool(_rack(topology, rack_id).get("is_eor"))


def mda_of(topology, rack_id):
    """The MDA serving this cabinet: its pod's MDA, the pod itself when it IS
    an MDA pod, or None when the map does not say."""
    pod = _pod(topology, rack_id)
    return pod.get("mda") or (pod_of(topology, rack_id) if pod.get("is_mda") else None)


def units_of(topology, rack_id):
    """Every name a cabinet answers to in configuration, most specific first."""
    out = [rack_id]
    for name in (row_name(topology, rack_id), pod_of(topology, rack_id),
                 room_of(topology, rack_id)):
        if name is not None and name not in out:
            out.append(name)
    return out


def all_units(topology):
    """Every rack, row, pod and room name on the map."""
    names = set(topology.get("pods") or ())
    for rid in topology.get("racks") or ():
        names.update(units_of(topology, rid))
    return names


def describe(topology, rack_id):
    """'pod A1 · row S · position 5 · EOR' — only the parts the map has."""
    r = _rack(topology, rack_id)
    parts = []
    if r.get("pod") is not None:
        parts.append(f"פוד {r['pod']}")
    if r.get("row") is not None:
        parts.append(f"שדרה {r['row']}")
    if r.get("room") is not None and r.get("pod") is None:
        parts.append(f"חדר {r['room']}")
    if r.get("position"):
        parts.append(f"עמדה {r['position']}")
    if r.get("is_eor"):
        parts.append("EOR")
    if is_mda(topology, rack_id):
        parts.append("MDA")
    if r.get("external"):
        parts.append("מחוץ לעותק")
    return " · ".join(parts)
