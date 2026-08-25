#!/usr/bin/env python3
"""
zones.py
========
The site divides its pods into colour zones, and equipment belonging to a zone
must be installed inside that zone — never mixed into another one.

This is a HARD boundary, stronger than any of the placement scores. A device
that names a zone will be racked inside it or not racked at all; the planner
does not get to trade the zone away for a shorter cable, because the zone is
an operational decision (tenancy, security, environment) and cable length is
an optimisation.

Yellow is defined as "everything not named above" rather than listed out, so
adding a pod to a colour automatically removes it from yellow and the zones
always cover the estate exactly.

MDA pods are NEUTRAL — they belong to no colour at all, not even yellow. They
hold no equipment, only cross-connect capacity, so giving them a tenancy
colour would describe something that never happens. Every zone therefore
contains only pods that can actually receive a device.
"""

# Pod membership, as given by the site.
NAMED_ZONES = {
    "green": ("A3", "A4", "A5", "B3", "C3", "D3", "D4", "D5"),
    "blue": ("A1", "B1", "B2", "C1", "C2", "D1"),
    "white": ("A7", "B7", "C7", "D7"),
}
DEFAULT_ZONE = "yellow"          # whatever is left over, MDA pods excepted
NEUTRAL = None                   # what an MDA pod's zone is

# What people actually type in the sheet.
ALIASES = {
    "green": "green", "ירוק": "green", "ירוקה": "green", "grn": "green", "g": "green",
    "blue": "blue", "כחול": "blue", "כחולה": "blue", "blu": "blue", "b": "blue",
    "white": "white", "לבן": "white", "לבנה": "white", "wht": "white", "w": "white",
    "yellow": "yellow", "צהוב": "yellow", "צהובה": "yellow", "ylw": "yellow", "y": "yellow",
}

# Rendered in the UI so a pod's colour is visible on the map itself.
COLOURS = {
    "green": "#3fb950",
    "blue": "#58a6ff",
    "white": "#d8dee6",
    "yellow": "#d29922",
}

ALL = tuple(NAMED_ZONES) + (DEFAULT_ZONE,)


class ZoneError(Exception):
    """An unrecognised zone name, or one with nowhere to put anything."""


def resolve(text):
    """Turn whatever the sheet says into a zone name, or None if it is blank.

    Raises ZoneError on something that looks like a zone but is not one, so a
    typo becomes a message about that row rather than a silently ignored
    boundary.
    """
    if not text:
        return None
    key = str(text).strip().lower()
    if key in ALIASES:
        return ALIASES[key]
    raise ZoneError(
        f"'{text}' is not a zone. Use one of: "
        + ", ".join(f"{z} ({heb})" for z, heb in
                    (("green", "ירוק"), ("blue", "כחול"),
                     ("white", "לבן"), ("yellow", "צהוב"))))


def is_neutral(topology, pod):
    """MDA pods carry no colour: they hold cross-connect capacity, not kit."""
    return bool(topology.get("pods", {}).get(pod, {}).get("is_mda"))


def zone_of_pod(topology, pod):
    """The pod's colour, or None if it is neutral."""
    if is_neutral(topology, pod):
        return NEUTRAL
    for name, pods in NAMED_ZONES.items():
        if pod in pods:
            return name
    return DEFAULT_ZONE


def pods_in(topology, zone):
    """Every pod of a zone that exists on this map. Neutral pods are in no
    zone, so they appear in none of these lists."""
    known = [p for p in topology.get("pods", ()) if not is_neutral(topology, p)]
    if zone == DEFAULT_ZONE:
        named = {p for pods in NAMED_ZONES.values() for p in pods}
        return sorted(p for p in known if p not in named)
    return sorted(p for p in NAMED_ZONES.get(zone, ()) if p in known)


def installable_pods(topology, zone):
    """Pods of a zone that can receive equipment.

    Identical to pods_in() now that neutral pods are excluded from zones in
    the first place; kept as its own name because callers are asking a
    different question, and the two could diverge again if some other kind of
    pod ever became off-limits.
    """
    return pods_in(topology, zone)


def racks_in(topology, zone):
    """Rack ids a device of this zone may be installed in."""
    allowed = set(installable_pods(topology, zone))
    return [rid for rid, meta in topology["racks"].items() if meta["pod"] in allowed]


def audit(topology):
    """What each zone actually contains, plus anything the colour map claims
    that the map does not have. A zone naming a pod that does not exist is a
    typo in the configuration, and silence about it would mean a device could
    be turned away from a zone that looked bigger than it was."""
    known = set(topology.get("pods", ()))
    out = []
    for zone in ALL:
        pods = pods_in(topology, zone)
        declared = set(NAMED_ZONES.get(zone, ()))
        missing = sorted(declared - known)
        neutral = sorted(p for p in declared if is_neutral(topology, p))
        notes = []
        if missing:
            notes.append(f"{', '.join(missing)} do not exist on this map")
        if neutral:
            notes.append(f"{', '.join(neutral)} are MDA pods and carry no colour — "
                         "they are ignored here")
        out.append({"zone": zone, "pods": pods, "installable_pods": pods,
                    "note": " · ".join(notes)})

    out.append({"zone": "(neutral)",
                "pods": sorted(p for p in known if is_neutral(topology, p)),
                "installable_pods": [],
                "note": "MDA pods — cross-connect only, never hold equipment"})
    return out


def summary(topology):
    """pod -> zone (None for neutral), for painting the map."""
    return {pod: zone_of_pod(topology, pod) for pod in topology.get("pods", ())}
