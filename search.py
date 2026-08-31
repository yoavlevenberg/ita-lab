#!/usr/bin/env python3
"""
search.py
=========
Finding one thing among 640 cabinets, 6,312 devices and 120,256 ports.

Until this existed the only way to reach anything was to click down through the
map — farm, pod, cabinet, device, port. That is fine when you are exploring and
useless when you already know what you are looking for, which is most of the
time: a serial off a delivery note, a port id out of a demand sheet, a circuit
id from a Work Order.

WHAT COUNTS AS A MATCH
----------------------
Everything the rest of the tool lets you write is accepted here, because
someone who has a string in their hand should not have to know which kind of
string it is:

    4827193056              a serial
    4827193056:12           that device's port 12
    A1-S05:FIB-PP-01:2      a port id
    A1-S05:FIB-PP-01        a device id
    A1-S05                  a cabinet
    A1                      a pod
    CIR-0123                a circuit
    FIB-PP-01               a device name, wherever it appears
    patch panel             words from a device's label

ORDERING
--------
Exact identifiers first, then names, then a typo-tolerant pass — and the
typo pass runs ONLY when the literal reading found nothing, so a real id is
never outranked by a fuzzy guess at something else. Every result says how it
was matched, so a correction is visible rather than silent, the same rule the
sheet reader follows.
"""

import re

import fuzzy
import serials

RACK_RE = re.compile(r"^[A-D]\d{1,2}-[SN]\d{1,2}$", re.I)
POD_RE = re.compile(r"^[A-D]\d{1,2}$", re.I)
CIRCUIT_RE = re.compile(r"^CIR[-_]?0*(\d+)$", re.I)

MAX_RESULTS = 25


def _port_hit(topology, port_id, why):
    p = topology["ports"][port_id]
    dev = topology["devices"][p["device"]]
    state = ("פנוי" if p["status"] == "free"
             else f"תפוס · {p['circuit']}" if p.get("circuit") else "תפוס")
    return {
        "kind": "port", "id": port_id, "why": why,
        "title": f"{p['rack']}-U{dev['u_start']}-P{p['index']}",
        "sub": f"{dev['name']} · {'סיב' if p['type'] == 'fiber' else 'נחושת'} · {state}",
        "rack": p["rack"], "pod": topology["racks"][p["rack"]]["pod"],
        "device": p["device"], "port": port_id,
        "status": p["status"], "circuit": p.get("circuit"),
    }


def _device_hit(topology, dev, why, free=None):
    if free is None:
        total = dev["fiber_ports"] + dev["copper_ports"]
        free = sum(1 for i in range(1, total + 1)
                   if topology["ports"].get(f"{dev['id']}:{i}", {}).get("status") == "free")
    total = dev["fiber_ports"] + dev["copper_ports"]
    return {
        "kind": "device", "id": dev["id"], "why": why,
        "title": f"{dev['rack']} · {dev['name']}",
        "sub": f"{dev['label']} · U{dev['u_start']} · {free}/{total} פורטים פנויים"
               + (f" · {dev['serial']}" if dev.get("serial") else ""),
        "rack": dev["rack"], "pod": topology["racks"][dev["rack"]]["pod"],
        "device": dev["id"], "port": None,
    }


def _rack_hit(topology, rack, why):
    devices = sum(1 for d in topology["devices"].values() if d["rack"] == rack["id"])
    return {
        "kind": "rack", "id": rack["id"], "why": why,
        "title": rack["id"],
        "sub": (f"פוד {rack['pod']} · שדרה {rack['row']} · עמדה {rack['position']}"
                + (" · EOR" if rack.get("is_eor") else "")
                + (" · MDA" if rack.get("is_mda") else "")
                + f" · {devices} רכיבים"),
        "rack": rack["id"], "pod": rack["pod"], "device": None, "port": None,
    }


def _pod_hit(topology, pod, why):
    return {
        "kind": "pod", "id": pod["id"], "why": why,
        "title": f"פוד {pod['id']}",
        "sub": (f"חדר {pod['room']}"
                + (" · MDA" if pod.get("is_mda") else f" · MDA {pod.get('mda')}")),
        "rack": None, "pod": pod["id"], "device": None, "port": None,
    }


def _circuit_hit(topology, circuit, why):
    hops = len(circuit.get("strands") or [])
    return {
        "kind": "circuit", "id": circuit["id"], "why": why,
        "title": circuit["id"],
        "sub": (("קטום · " if circuit.get("partial") else "")
                + f"{'סיב' if circuit['cable_type'] == 'fiber' else 'נחושת'}"
                + f" · {hops} {'קפיצה' if hops == 1 else 'קפיצות'}"
                + f" · {circuit['a_port']} ⇄ {circuit.get('b_port') or '—'}"),
        "rack": topology["ports"][circuit["a_port"]]["rack"],
        "pod": topology["racks"][topology["ports"][circuit["a_port"]]["rack"]]["pod"],
        "device": None, "port": circuit["a_port"], "circuit": circuit["id"],
    }


def search(topology, query, limit=MAX_RESULTS):
    """Everything matching `query`, best first.

    Returns {"query", "results", "correction"} — `correction` is set when the
    literal reading found nothing and a typo-tolerant pass was used instead, so
    the interface can say "showing results for X" rather than pretending the
    user typed it.
    """
    q = (query or "").strip()
    if not q:
        return {"query": q, "results": [], "correction": None}

    out, seen = [], set()

    def add(hit, score):
        """`score` is how directly this was asked for — 0 is "you typed its
        id". Sorting by kind alone put a cabinet below the thirteen devices
        inside it, which is not what someone who typed the cabinet's name
        wanted."""
        key = (hit["kind"], hit["id"])
        if key not in seen:
            seen.add(key)
            hit["_score"] = score
            out.append(hit)

    ports, devices, racks = topology["ports"], topology["devices"], topology["racks"]
    upper = q.upper()

    # ---- 1. a serial, with or without a port on it -------------------------
    parsed = serials.parse(q)
    if parsed:
        serial, index = parsed
        dev = next((d for d in devices.values() if d.get("serial") == serial), None)
        if dev:
            if index is not None:
                pid = f"{dev['id']}:{index}"
                if pid in ports:
                    add(_port_hit(topology, pid, f"פורט {index} של סיריאל {serial}"), 0)
                else:
                    total = dev["fiber_ports"] + dev["copper_ports"]
                    add({**_device_hit(topology, dev, f"סיריאל {serial}"),
                         "note": f"אין פורט {index} — לרכיב יש {total}"}, 0)
            else:
                add(_device_hit(topology, dev, f"סיריאל {serial}"), 0)

    # ---- 2. exact identifiers ---------------------------------------------
    if upper in ports:
        add(_port_hit(topology, upper, "מזהה פורט"), 0)
    if upper in devices:
        add(_device_hit(topology, devices[upper], "מזהה רכיב"), 0)
    if upper in racks:
        add(_rack_hit(topology, racks[upper], "מזהה מסד"), 0)
    if upper in topology["pods"]:
        add(_pod_hit(topology, topology["pods"][upper], "מזהה פוד"), 0)

    circuits = topology.get("circuits") or {}
    m = CIRCUIT_RE.match(q)
    if m:
        for cid in (upper, f"CIR-{int(m.group(1)):04d}"):
            if cid in circuits:
                add(_circuit_hit(topology, circuits[cid], "מזהה חיבור"), 0)

    # ---- 3. everything in one cabinet, when a cabinet was named ------------
    if RACK_RE.match(q) and upper in racks:
        for d in devices.values():
            if d["rack"] == upper:
                add(_device_hit(topology, d, f"במסד {upper}"), 1)

    # ---- 4. a device name or label, anywhere ------------------------------
    if len(q) >= 2:
        for d in devices.values():
            if len(out) >= limit * 3:
                break
            if upper in d["name"].upper():
                add(_device_hit(topology, d, f"שם רכיב מכיל '{q}'"), 2)
            elif upper in (d.get("label") or "").upper():
                add(_device_hit(topology, d, f"תיאור מכיל '{q}'"), 3)

    # ---- 5. only now, a typo-tolerant pass --------------------------------
    # Last, and only when nothing literal matched: a real identifier must never
    # be pushed down the list by a guess at something else.
    correction = None
    if not out:
        candidates = {}
        for name in {d["name"] for d in devices.values()}:
            candidates[name] = ("name", name)
        for rid in racks:
            candidates[rid] = ("rack", rid)
        for pid in topology["pods"]:
            candidates[pid] = ("pod", pid)
        hit = fuzzy.match(q, candidates, lenient=True)
        if hit:
            kind, value = hit[0]
            correction = value
            if kind == "rack":
                add(_rack_hit(topology, racks[value], f"נקרא כ-{value}"), 4)
                for d in devices.values():
                    if d["rack"] == value:
                        add(_device_hit(topology, d, f"במסד {value}"), 5)
            elif kind == "pod":
                add(_pod_hit(topology, topology["pods"][value], f"נקרא כ-{value}"), 4)
            else:
                for d in devices.values():
                    if d["name"] == value:
                        add(_device_hit(topology, d, f"נקרא כ-{value}"), 4)
                        if len(out) >= limit:
                            break

    rank = {"port": 0, "circuit": 1, "rack": 2, "device": 3, "pod": 4}
    out.sort(key=lambda h: (h["_score"], rank.get(h["kind"], 9), h["id"]))
    for h in out:
        h.pop("_score", None)
    return {"query": q, "results": out[:limit], "correction": correction,
            "truncated": len(out) > limit}
