#!/usr/bin/env python3
"""
bulkplan.py
===========
Plans a whole demand sheet at once instead of one connection at a time.

A real port-to-port request arrives as a spreadsheet with dozens of rows, and
routing each row in isolation gives a poor result: every row independently
picks the "best" trunk and the lowest free strand, so the batch piles onto
one trunk and the strands end up scattered. Planning the batch as a batch
lets us group related rows and lay them down side by side.

HOW IT WORKS
------------
Planning runs against a deep copy of the topology and genuinely commits each
route into that copy as it goes, so demand #2 sees the resources demand #1
consumed. Nothing touches the live topology until execute() replays the plan.
That also means a planned route can be revalidated on the way in — if the map
moved underneath the plan, the offending row fails loudly instead of silently
double-booking a strand.

PREFERENCES
-----------
  group_adjacent    route rows of the same group back-to-back, so they land
                    on consecutive strands and the same patch panel
  redundancy_split  alternate a group's rows across two physically disjoint
                    paths, so one cut can't take the whole group down
  load_balance      prefer the option with the most headroom on its tightest
                    trunk, rather than always the cheapest path
"""

import copy
from collections import Counter, OrderedDict, defaultdict

import pathengine
import xlsxreader

DEFAULT_PREFS = {
    "group_adjacent": True,
    "redundancy_split": False,
    "load_balance": False,
}


class BulkError(Exception):
    """The demand list itself is unusable (empty, malformed)."""


# --------------------------------------------------------------------------
# reading the demand list
# --------------------------------------------------------------------------

SRC_ALIASES = ("SRC_PORT", "SOURCE_PORT", "SRC", "SOURCE", "A_PORT", "FROM_PORT", "FROM")
DST_ALIASES = ("DST_PORT", "DEST_PORT", "DESTINATION_PORT", "DST", "DEST",
               "B_PORT", "TO_PORT", "TO")
GROUP_ALIASES = ("GROUP", "GROUP_ID", "BUNDLE", "SERVICE", "NETWORK", "VLAN")


def _pick(row, aliases):
    for a in aliases:
        v = row.get(a)
        if v:
            return v.strip()
    return ""


def read_demand_sheet(path_or_file):
    """Read a demand spreadsheet into demand records. Keeps the knowledge of
    which columns matter in one place — callers just hand over the file."""
    rows = xlsxreader.read_sheet(path_or_file,
                                 required_any=(SRC_ALIASES, DST_ALIASES))
    return demands_from_rows(rows)


def demands_from_rows(rows):
    """Turn spreadsheet dicts into demand records. Column naming varies wildly
    between the sheets people actually send, so several spellings are accepted
    (see *_ALIASES). Each row keeps its real Excel line number so every message
    can point back at the exact line in the user's file."""
    demands = []
    for i, row in enumerate(rows, start=2):
        src, dst = _pick(row, SRC_ALIASES), _pick(row, DST_ALIASES)
        if not src and not dst:
            continue
        demands.append({
            # the reader supplies the true line number; `i` is only a fallback
            # for callers that hand us plain dicts (tests, the CLI)
            "row": row.get(xlsxreader.ROW_KEY, i),
            "src": src,
            "dst": dst,
            "group": _pick(row, GROUP_ALIASES),
            # a half-filled row is a typo in the sheet, not a routing failure —
            # saying so here beats "Port '' does not exist" from deep inside
            "malformed": "" if (src and dst) else
                         ("no destination port in this row" if src
                          else "no source port in this row"),
        })
    if not demands:
        raise BulkError(
            "No demand rows found. Expected a source column (one of: "
            f"{', '.join(SRC_ALIASES[:4])}...) and a destination column "
            f"(one of: {', '.join(DST_ALIASES[:4])}...).")
    return demands


# --------------------------------------------------------------------------
# preflight validation
# --------------------------------------------------------------------------
# Checked BEFORE any routing happens, so a sheet with typos comes back as one
# list of problems to fix rather than as a half-planned run. Everything here is
# read-only and cheap — no graph search, no copying of the topology.

def validate(topology, demands):
    """Check every row against the map and against the rest of the sheet.

    Returns one issue record per problem row plus a summary. A row can only
    hold one fault: the first thing wrong with it is the thing to fix, and
    listing three consequences of one typo just makes the report harder to read.
    """
    ports = topology["ports"]
    issues, seen_pairs = [], {}

    # A physical port can host exactly one connection, so the same port must
    # not appear twice anywhere in the sheet. Only a whole-sheet check finds
    # this — routing row by row would notice only on the second row, by which
    # point the first is already planned.
    #
    # The clash is reported on the LATER row, naming the earlier one. Flagging
    # both would double-count: the planner will happily place the first row and
    # fail only the second, so blaming both would make this review disagree
    # with the plan that follows it.
    claimed_by = {}

    def fault(d, kind, message):
        issues.append({"row": d["row"], "kind": kind, "message": message,
                       "src": d["src"], "dst": d["dst"]})

    for d in sorted(demands, key=lambda x: x["row"]):
        if d.get("malformed"):
            fault(d, "incomplete", d["malformed"])
            continue

        src, dst = d["src"], d["dst"]

        if src == dst:
            fault(d, "same_port", "source and destination are the same port")
            continue

        missing = [p for p in (src, dst) if p not in ports]
        if missing:
            fault(d, "unknown_port",
                  f"port does not exist on the map: {', '.join(missing)}")
            continue

        a, b = ports[src], ports[dst]

        busy = [f"{p} (held by {ports[p]['circuit']})"
                for p in (src, dst) if ports[p]["status"] != "free"]
        if busy:
            fault(d, "port_in_use", f"already patched: {'; '.join(busy)}")
            continue

        if a["type"] != b["type"]:
            fault(d, "cable_mismatch",
                  f"cable type mismatch: source is {a['type']}, destination is {b['type']}")
            continue

        if a["rack"] == b["rack"]:
            fault(d, "same_rack",
                  f"both ports are in {a['rack']} — an intra-rack patch needs no trunk routing")
            continue

        clash = sorted({claimed_by[p] for p in (src, dst) if p in claimed_by})
        if clash:
            fault(d, "duplicate_port",
                  "a port on this row is already taken by row "
                  + ", ".join(str(r) for r in clash))
            continue

        pair = tuple(sorted((src, dst)))
        if pair in seen_pairs:
            fault(d, "duplicate_row",
                  f"this exact connection is already requested on row {seen_pairs[pair]}")
            continue
        seen_pairs[pair] = d["row"]
        claimed_by[src] = claimed_by[dst] = d["row"]

    bad_rows = {i["row"] for i in issues}
    return {
        "total": len(demands),
        "ok": len(demands) - len(bad_rows),
        "problems": len(bad_rows),
        "issues": sorted(issues, key=lambda i: i["row"]),
        "by_kind": dict(Counter(i["kind"] for i in issues)),
    }


# --------------------------------------------------------------------------
# grouping
# --------------------------------------------------------------------------

def _group_key(topology, d):
    """An explicit group column wins. Otherwise rows that run between the same
    pair of racks are treated as one bundle — that is what 'probably the same
    network' looks like in a demand sheet."""
    if d["group"]:
        return f"col:{d['group']}"
    ports = topology["ports"]
    a, b = ports.get(d["src"]), ports.get(d["dst"])
    if not a or not b:
        return "unresolved"
    return f"{a['rack']}=>{b['rack']}"


def _order_demands(topology, demands, group_adjacent):
    """Group members must be routed consecutively for adjacency to happen:
    each commit takes the next free strand, so back-to-back rows naturally
    land on consecutive strands of the same trunk and the same panel."""
    for d in demands:
        d["group_key"] = _group_key(topology, d)
    if not group_adjacent:
        return list(demands)
    buckets = OrderedDict()
    for d in demands:
        buckets.setdefault(d["group_key"], []).append(d)
    ordered = []
    for members in buckets.values():
        ordered.extend(members)
    return ordered


# --------------------------------------------------------------------------
# choosing between candidate routes
# --------------------------------------------------------------------------

def _headroom(route):
    """Spare strands on the route's tightest trunk — the thing that actually
    runs out first."""
    return min((s["remaining_before"] for s in route["segments"]), default=0)


def _choose(options, prefs, seat):
    """Which of the candidate routes this row should take.

    `seat` is the row's index within its group, so redundancy_split can
    alternate members across the two disjoint options."""
    if len(options) == 1:
        return options[0], None
    if prefs["redundancy_split"]:
        pick = options[seat % len(options)]
        return pick, f"redundancy split — leg {seat % len(options) + 1}"
    if prefs["load_balance"]:
        pick = max(options, key=_headroom)
        if pick is not options[0]:
            return pick, f"load balance — {_headroom(pick)} free on its tightest trunk"
        return pick, None
    return options[0], None


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------

def plan(topology, demands, prefs=None, already_isolated=False):
    """Route every demand against a working copy, in an order chosen by the
    preferences. Returns one result per demand plus a summary. Never mutates
    the topology it is given.

    Set `already_isolated` when the caller has handed us a private snapshot
    that nobody else can touch — then we skip the defensive copy instead of
    paying for a second one.
    """
    prefs = {**DEFAULT_PREFS, **(prefs or {})}
    work = topology if already_isolated else copy.deepcopy(topology)

    ordered = _order_demands(work, demands, prefs["group_adjacent"])
    need_options = prefs["redundancy_split"] or prefs["load_balance"]

    seats = Counter()
    results, planned = [], 0
    for d in ordered:
        seat = seats[d["group_key"]]
        seats[d["group_key"]] += 1

        base = {"row": d["row"], "src": d["src"], "dst": d["dst"],
                "group": d["group_key"], "seat": seat + 1}
        if d.get("malformed"):
            results.append({**base, "status": "failed", "reason": d["malformed"]})
            continue
        try:
            options = pathengine.resolve_route_options(
                d["src"], d["dst"], count=2 if need_options else 1, topology=work)
        except pathengine.RouteError as e:
            results.append({**base, "status": "failed", "reason": str(e)})
            continue

        route, note = _choose(options, prefs, seat)
        provisional = f"PLAN-{planned + 1:04d}"
        try:
            pathengine.commit_route(work, route, provisional)
        except (KeyError, pathengine.RouteError) as e:
            results.append({**base, "status": "failed", "reason": f"could not reserve: {e}"})
            continue

        planned += 1
        results.append({**base, "status": "ok", "note": note, "route": route,
                        "hops": len(route["segments"]),
                        "cable_type": route["cable_type"],
                        "domain": route["domain"],
                        "total_length_m": route["total_length_m"],
                        "strands": [{"edge_id": s["edge_id"], "index": s["strand_index"]}
                                    for s in route["segments"]]})

    results.sort(key=lambda r: r["row"])
    return {"prefs": prefs, "results": results, "summary": _summary(results)}


def _summary(results):
    ok = [r for r in results if r["status"] == "ok"]
    failed = [r for r in results if r["status"] != "ok"]
    trunks = Counter(s["edge_id"] for r in ok for s in r["strands"])
    groups = defaultdict(list)
    for r in ok:
        groups[r["group"]].append(r["row"])
    return {
        "total": len(results),
        "planned": len(ok),
        "failed": len(failed),
        "groups": len(groups),
        "hops_avg": round(sum(r["hops"] for r in ok) / len(ok), 2) if ok else 0,
        "total_length_m": round(sum(r["total_length_m"] for r in ok), 1),
        "busiest_trunks": trunks.most_common(5),
        "failures": [{"row": r["row"], "reason": r["reason"]} for r in failed],
    }


# --------------------------------------------------------------------------
# executing
# --------------------------------------------------------------------------

def execute(topology, results):
    """Commit an approved plan to the live topology, in the same order it was
    planned. Each route is revalidated first, so anything consumed since the
    plan was made is reported instead of quietly overwriting someone's work.

    Rows are committed independently: one stale row does not roll back the
    rows that already succeeded — it is reported and the rest continue, which
    matches how a batch of patching actually gets done.
    """
    committed, failed = [], []
    for r in sorted((r for r in results if r.get("status") == "ok"),
                    key=lambda r: r["row"]):
        route = r["route"]
        try:
            pathengine.revalidate_route(topology, route)
        except pathengine.RouteError as e:
            failed.append({"row": r["row"], "reason": str(e)})
            continue
        cid = pathengine.next_circuit_id(topology)
        circuit = pathengine.commit_route(topology, route, cid)
        committed.append({"row": r["row"], "circuit": circuit})
    return {"committed": committed, "failed": failed}
