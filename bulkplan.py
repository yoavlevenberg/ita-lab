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
import re
from collections import Counter, OrderedDict, defaultdict

import fuzzy
import pathengine
import placement
import serials
import xlsxreader
import zones

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

SRC_ALIASES = ("SRC_PORT", "SOURCE_PORT", "SRC", "SOURCE", "A_PORT", "FROM_PORT", "FROM",
               "SRC_SERIAL", "A_SERIAL")
DST_ALIASES = ("DST_PORT", "DEST_PORT", "DESTINATION_PORT", "DST", "DEST",
               "B_PORT", "TO_PORT", "TO", "DST_SERIAL", "B_SERIAL")

# ---- the four-column form: device and port in separate cells --------------
# A sheet may name each end as one cell (`4827193056:12`) or as two — the box
# in its own column and the port number beside it, left blank to mean "any
# suitable port on that box". The two columns are easier to fill in from an
# inventory export, where the serial and the port never lived in one field.
#
# The device column decides how the port column is read: with it present,
# SRC_PORT holds a bare port NUMBER; without it, SRC_PORT holds a whole
# endpoint. That keeps a sheet written in either form unambiguous.
SRC_DEV_ALIASES = ("SRC_DEVICE", "SOURCE_DEVICE", "SRC_SN", "SOURCE_SN",
                   "A_DEVICE", "FROM_DEVICE", "A_SN", "FROM_SN",
                   "SRC_ASSET", "רכיב_מקור", "מקור", "סיריאל_מקור")
DST_DEV_ALIASES = ("DST_DEVICE", "DEST_DEVICE", "DESTINATION_DEVICE",
                   "DST_SN", "DEST_SN", "B_DEVICE", "TO_DEVICE", "B_SN", "TO_SN",
                   "DST_ASSET", "רכיב_יעד", "יעד", "סיריאל_יעד")
GROUP_ALIASES = ("GROUP", "GROUP_ID", "BUNDLE", "SERVICE", "NETWORK", "VLAN")
CABLE_ALIASES = ("CABLE", "CABLE_TYPE", "MEDIA", "TYPE", "CONNECTION_TYPE", "LINK_TYPE")
# an optional free-text name for the connection itself, carried through to the
# results table and the Work Order so a link can be recognised by what it is
# for rather than only by its port ids
LABEL_ALIASES = ("LABEL", "NAME", "DESCRIPTION", "DESC", "COMMENT", "NOTE",
                 "תיאור", "שם", "הערה")

# ---- the Devices tab: what is arriving, not what it connects to -----------
DEV_SERIAL_ALIASES = ("SERIAL", "SERIAL_NUMBER", "SN", "S_N", "ASSET", "ASSET_TAG")
DEV_TYPE_ALIASES = ("TYPE", "DEVICE_TYPE", "KIND", "CATEGORY", "ROLE")
DEV_USIZE_ALIASES = ("U_SIZE", "U", "USIZE", "HEIGHT", "RU", "SIZE_U", "UNITS")
DEV_FIBER_ALIASES = ("FIBER", "FIBER_PORTS", "FIBRE", "FIBRE_PORTS", "SFP", "OPTICAL_PORTS")
DEV_COPPER_ALIASES = ("COPPER", "COPPER_PORTS", "RJ45", "ETHERNET_PORTS", "UTP")
DEV_LABEL_ALIASES = ("LABEL", "MODEL", "DESCRIPTION", "NAME", "PRODUCT")
# the colour zone a device belongs to — a hard boundary on where it may go
DEV_ZONE_ALIASES = ("ZONE", "GROUP", "COLOR", "COLOUR", "AREA", "קבוצה", "צבע")

# what TYPE strings people actually write, mapped to the model's vocabulary
TYPE_SYNONYMS = {
    "switch": "switch", "sw": "switch", "tor": "switch", "leaf": "switch",
    "spine": "switch", "core": "switch", "router": "switch",
    "server": "server", "srv": "server", "host": "server", "node": "server",
    "compute": "server", "storage": "server", "appliance": "server",
    "fiber_patch_panel": "fiber_patch_panel", "fiber_panel": "fiber_patch_panel",
    "fibre_panel": "fiber_patch_panel", "fpp": "fiber_patch_panel",
    "copper_patch_panel": "copper_patch_panel", "copper_panel": "copper_patch_panel",
    "cpp": "copper_patch_panel", "patch_panel": "copper_patch_panel",
}


def _pick(row, aliases):
    """Read a column by any of its accepted names, tolerating a typo in the
    header.

    Exact spellings win outright. Only if none matched do we look for a
    mistyped header — `SRC_PROT`, `COPER` — and then only when the match is
    unambiguous, so a sheet with both `TYPE` and a mistyped `TYP` cannot have
    one silently read as the other.
    """
    for a in aliases:
        v = row.get(a)
        if v:
            return v.strip()

    # Every alias in the group names the SAME column, so map them all to one
    # value: being torn between SOURCE_PORT and SRC_PORT is not an ambiguity
    # to refuse, it is the same answer twice.
    same = dict.fromkeys(aliases, "hit")
    for key, value in row.items():
        if key == xlsxreader.ROW_KEY or not value:
            continue
        # Accept an exact-after-normalisation hit as well as a corrected one.
        # Filtering to corrections only looked like an optimisation — the
        # direct loop above already tried the literal spellings — but it threw
        # away headers that differ only in spacing or case, like "U SIZE" for
        # "USIZE", which the loop above never sees.
        if fuzzy.match(key, same):
            return str(value).strip()
    return ""


def _has_column(row, aliases):
    """Does the sheet HAVE this column, whatever this row happens to hold?

    Asked of the header, not the value: a device column changes how the port
    column beside it is read, and a blank cell in row 2 must not flip that
    meaning for the whole file.
    """
    if any(a in row for a in aliases):
        return True
    same = dict.fromkeys(aliases, "hit")
    return any(key != xlsxreader.ROW_KEY and fuzzy.match(key, same) for key in row)


P2P_SHEET_NAMES = ("P2P", "DEMANDS", "PORT_TO_PORT", "CONNECTIONS", "LINKS")
DEVICES_SHEET_NAMES = ("DEVICES", "NEW_DEVICES", "EQUIPMENT", "HARDWARE")


def _find_sheet(path_or_file, candidates):
    """Locate a tab by any of its accepted names — case, spacing and an
    obvious typo in the tab name all forgiven ('Devicez', 'P2p ')."""
    actual = xlsxreader.sheet_names(path_or_file)
    have = {xlsxreader._norm(n): n for n in actual}
    for want in candidates:
        if want in have:
            return have[want]
    same = dict.fromkeys(candidates, "hit")     # all name the same tab
    for name in actual:
        if fuzzy.match(name, same):
            return name
    return None


def read_demand_sheet(path_or_file):
    """Read the port-to-port tab into demand records. Keeps the knowledge of
    which columns matter in one place — callers just hand over the file.

    A single-tab workbook is read as-is, so sheets written before the Devices
    tab existed keep working.
    """
    sheet = _find_sheet(path_or_file, P2P_SHEET_NAMES)
    rows = xlsxreader.read_sheet(path_or_file, sheet=sheet,
                                 required_any=(SRC_ALIASES, DST_ALIASES))
    return demands_from_rows(rows)


def read_device_sheet(path_or_file):
    """Read the Devices tab: the spec of kit that has not been installed yet.

    Returns [] when there is no such tab, so the caller can tell "no new
    devices" apart from "a broken Devices tab".
    """
    sheet = _find_sheet(path_or_file, DEVICES_SHEET_NAMES)
    if sheet is None:
        return []
    rows = xlsxreader.read_sheet(path_or_file, sheet=sheet,
                                 required_any=(DEV_SERIAL_ALIASES,))
    return devices_from_rows(rows)


def _int(value, default=0):
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def devices_from_rows(rows):
    """Turn Devices-tab rows into device specs. Nothing is resolved or placed
    here — this only says what each box IS."""
    out = []
    for i, row in enumerate(rows, start=2):
        serial = _pick(row, DEV_SERIAL_ALIASES)
        if not serial:
            continue
        raw_type = _pick(row, DEV_TYPE_ALIASES)
        # 'swich', 'serrver' — corrected when unambiguous, left alone otherwise
        # so validate_devices can report it against this row
        hit = fuzzy.match(raw_type, TYPE_SYNONYMS) if raw_type else None
        out.append({
            "row": row.get(xlsxreader.ROW_KEY, i),
            "serial": serials.normalise(serial),
            "raw_type": raw_type,
            "type": hit[0] if hit else (raw_type.strip().lower() or "server"),
            "u_size": _int(_pick(row, DEV_USIZE_ALIASES), 0),
            "fiber_ports": _int(_pick(row, DEV_FIBER_ALIASES), 0),
            "copper_ports": _int(_pick(row, DEV_COPPER_ALIASES), 0),
            "label": _pick(row, DEV_LABEL_ALIASES) or raw_type or "New device",
            # kept raw here; resolving it can fail, and that belongs in the
            # review where it can be reported against its row
            "raw_zone": _pick(row, DEV_ZONE_ALIASES),
        })
    return out


def _endpoint(row, dev_aliases, port_aliases, split):
    """One end of a connection, however the sheet chose to write it.

    `split` is decided once per sheet, not per row: a file either has device
    columns or it does not. Deciding per row would make a blank device cell
    silently change what the port cell means.
    """
    if not split:
        return _pick(row, port_aliases)
    device = _pick(row, dev_aliases).strip()
    port = _pick(row, port_aliases).strip()
    if not device:
        # a port with no box beside it names nothing; let the caller report the
        # row as half-filled rather than inventing an endpoint from the number
        return ""
    if not port:
        return device
    # An end already carrying its own port wins: someone filled the device
    # column the old way, and honouring it beats gluing on a second port.
    #
    # Testing that with "does it contain a colon" was wrong, and quietly so:
    # every device id on this map has one (A1-S05:FIB-PP-01), so copying an id
    # out of the UI and typing the port beside it — the obvious thing to do —
    # dropped the port column and reported the DEVICE as a port that does not
    # exist. Only a serial can carry its port in one cell, so only a serial is
    # asked.
    parsed = serials.parse(device)
    if parsed and parsed[1] is not None:
        return device
    return f"{device}:{port}"


def demands_from_rows(rows):
    """Turn spreadsheet dicts into demand records. Column naming varies wildly
    between the sheets people actually send, so several spellings are accepted
    (see *_ALIASES). Each row keeps its real Excel line number so every message
    can point back at the exact line in the user's file."""
    # Which form is this sheet written in? Ask the header once, so the answer
    # is the same for every row.
    probe = rows[0] if rows else {}
    split = _has_column(probe, SRC_DEV_ALIASES) or _has_column(probe, DST_DEV_ALIASES)

    demands = []
    for i, row in enumerate(rows, start=2):
        src = _endpoint(row, SRC_DEV_ALIASES, SRC_ALIASES, split)
        dst = _endpoint(row, DST_DEV_ALIASES, DST_ALIASES, split)
        if not src and not dst:
            continue
        demands.append({
            # the reader supplies the true line number; `i` is only a fallback
            # for callers that hand us plain dicts (tests, the CLI)
            "row": row.get(xlsxreader.ROW_KEY, i),
            "src": src,
            "dst": dst,
            "group": _pick(row, GROUP_ALIASES),
            "label": _pick(row, LABEL_ALIASES),
            # only needed when an endpoint is given as a serial: the device has
            # both fibre and copper ports, so the sheet has to say which
            "cable": _pick(row, CABLE_ALIASES).strip().lower(),
            # a half-filled row is a typo in the sheet, not a routing failure —
            # saying so here beats "Port '' does not exist" from deep inside
            "malformed": "" if (src and dst) else
                         ("no destination port in this row" if src
                          else "no source port in this row"),
        })
    if not demands:
        raise BulkError(
            "No demand rows found. Expected either a device column per end "
            f"({SRC_DEV_ALIASES[0]} / {DST_DEV_ALIASES[0]}) with an optional "
            f"{SRC_ALIASES[0]} / {DST_ALIASES[0]} beside it, or a single "
            f"column per end holding the whole endpoint ({SRC_ALIASES[0]} / "
            f"{DST_ALIASES[0]}).")
    return demands


# --------------------------------------------------------------------------
# resolving what an endpoint refers to
# --------------------------------------------------------------------------
# A P2P cell holds either a port id (A1-S05:FIB-PP-01:2) or a device serial
# (SN-7QK2M4XP01). A serial says "this box" without saying which of its ports,
# so the planner picks a free one of the right media — the same way it already
# picks cross-connect ports along a route.

CABLE_SYNONYMS = {"fibre": "fiber", "optic": "fiber", "optical": "fiber",
                  "rj45": "copper", "utp": "copper", "eth": "copper"}


def _norm_cable(text):
    c = (text or "").strip().lower()
    return CABLE_SYNONYMS.get(c, c)


def unknown_port_message(topology, port_id):
    """Why a port id names nothing. Shared by the review and the planner so the
    two cannot drift into describing the same fault differently.

    "port does not exist" is true but unhelpful when the mistake is in the
    device half of the cell — a name without its cabinet, or a serial with a
    digit wrong. Say which half is unrecognised.
    """
    dev_part = port_id.rpartition(":")[0]
    if dev_part and dev_part not in topology["devices"]:
        return (f"{port_id} — no device '{dev_part}' on the map (name a device by "
                f"its serial, or by its full id including the cabinet, e.g. "
                f"A1-S05:FIB-PP-01)")
    return f"{port_id} — that device has no such port"


def port_id_for(topology, serial_index, value):
    """The single socket an endpoint names, or None when it names none — a bare
    serial, an unknown serial, or a port that is not on the map."""
    if not value:
        return None
    hit = serials.parse(value)
    if hit:
        if hit[1] is None:
            return None
        dev_id = serial_index.get(hit[0])
        port = f"{dev_id}:{hit[1]}" if dev_id else None
        return port if port in topology["ports"] else None
    return value if value in topology["ports"] else None


def looks_like_serial(text):
    """Serials are plain numbers, so this is what tells '4827193056:12' apart
    from a port id like 'A1-S05:FIB-PP-01:2'."""
    return serials.looks_like(text)


def free_port_on_device(topology, device_id, cable_type, exclude=()):
    """Lowest-numbered free port of the right media on a device."""
    best = None
    for pid, p in topology["ports"].items():
        if (p["device"] == device_id and p["status"] == "free"
                and p["type"] == cable_type and pid not in exclude):
            if best is None or p["index"] < topology["ports"][best]["index"]:
                best = pid
    return best


# --------------------------------------------------------------------------
# preflight validation
# --------------------------------------------------------------------------
# Checked BEFORE any routing happens, so a sheet with typos comes back as one
# list of problems to fix rather than as a half-planned run. Everything here is
# read-only and cheap — no graph search, no copying of the topology.

VALID_TYPES = ("switch", "server", "fiber_patch_panel", "copper_patch_panel")


def validate_devices(topology, new_devices, demands=()):
    """Check the Devices tab before anything is sited.

    Placement is the expensive step and it mutates a working copy, so every
    problem that can be seen from the spec alone is reported first.
    """
    issues, notes = [], []
    known = serials.index(topology)
    seen = {}

    def fault(d, kind, message):
        issues.append({"row": d["row"], "kind": kind, "message": message,
                       "serial": d.get("serial", "")})

    for d in sorted(new_devices, key=lambda x: x["row"]):
        serial = d["serial"]

        if not serial:
            fault(d, "no_serial", "this row has no serial number")
            continue
        if serial in seen:
            fault(d, "duplicate_serial",
                  f"serial {serial} is already declared on row {seen[serial]}")
            continue
        if serial in known:
            dev = topology["devices"][known[serial]]
            fault(d, "serial_exists",
                  f"serial {serial} already belongs to {dev['label']} in {dev['rack']} "
                  "— it is installed, not new")
            continue
        seen[serial] = d["row"]

        if not 1 <= d["u_size"] <= placement.RACK_U:
            fault(d, "bad_u_size",
                  f"U_SIZE must be between 1 and {placement.RACK_U} (got '{d['u_size']}')")
            continue
        if d["fiber_ports"] + d["copper_ports"] <= 0:
            fault(d, "no_ports",
                  "this device declares no ports, so nothing can be cabled to it "
                  "(set FIBER and/or COPPER)")
            continue
        if d["type"] not in VALID_TYPES:
            fault(d, "unknown_type",
                  f"unrecognised TYPE '{d['raw_type']}' — use one of: "
                  + ", ".join(VALID_TYPES))
            continue

        try:
            zone = zones.resolve(d.get("raw_zone"))
        except zones.ZoneError as e:
            fault(d, "unknown_zone", str(e))
            continue
        d["zone"] = zone
        # a typo that was corrected is reported, never applied behind the
        # user's back — this is the hardest boundary in the system
        note = zones.correction(d.get("raw_zone"))
        if note:
            notes.append({"row": d["row"], "message": note})

        # is there anywhere at all it could go? inside its zone, if it has one
        if not any(placement.positions_for(topology, r, d["u_size"])
                   for r in placement.eligible_racks(topology, zone=zone)):
            where = (f"in the {zone} zone (pods "
                     f"{', '.join(zones.installable_pods(topology, zone))})"
                     if zone else "anywhere")
            fault(d, "no_space",
                  f"no cabinet {where} has {d['u_size']}U of contiguous free space")
            continue

        # how many links does the sheet ask of it, and can its ports carry them?
        wanted = Counter()
        for dem in demands:
            ends = [dem.get("src", ""), dem.get("dst", "")]
            if not any(looks_like_serial(e) and serials.normalise(e) == serial for e in ends):
                continue
            cable = (dem.get("cable") or "").strip().lower()
            wanted[{"fibre": "fiber"}.get(cable, cable)] += 1
        over = [f"{n} {k} link(s) but only "
                f"{d['fiber_ports'] if k == 'fiber' else d['copper_ports']} {k} port(s)"
                for k, n in wanted.items()
                if k in ("fiber", "copper")
                and n > (d["fiber_ports"] if k == "fiber" else d["copper_ports"])]
        if over:
            fault(d, "not_enough_ports",
                  "the P2P tab asks for " + "; ".join(over))
            continue

    # a mutual dependency can only be seen across rows
    links = [(serials.normalise(x.get("src", "")), serials.normalise(x.get("dst", "")))
             for x in demands
             if looks_like_serial(x.get("src", "")) and looks_like_serial(x.get("dst", ""))]
    _, cycles = placement.order_by_dependency(new_devices, links)
    for serial in cycles:
        spec = next((d for d in new_devices if d["serial"] == serial), None)
        if spec and not any(i["row"] == spec["row"] for i in issues):
            fault(spec, "dependency_cycle",
                  "this device and another new device each wait for the other to be "
                  "placed — one of them must connect to something already installed")

    bad = {i["row"] for i in issues}
    return {
        "total": len(new_devices),
        "ok": len(new_devices) - len(bad),
        "problems": len(bad),
        "issues": sorted(issues, key=lambda i: i["row"]),
        # spellings that were understood only by correcting them; shown so a
        # correction is never something the user finds out about later
        "corrections": sorted(notes, key=lambda n: n["row"]),
        "by_kind": dict(Counter(i["kind"] for i in issues)),
    }


def validate(topology, demands, new_devices=()):
    """Check every row against the map and against the rest of the sheet.

    Returns one issue record per problem row plus a summary. A row can only
    hold one fault: the first thing wrong with it is the thing to fix, and
    listing three consequences of one typo just makes the report harder to read.

    `new_devices` are the specs from the Devices tab. A row may name one of
    them by serial even though it is not installed yet — that is the whole
    point of declaring it — so those serials count as resolvable here.
    """
    ports = topology["ports"]
    known_serials = serials.index(topology)
    specs_by_serial = {d["serial"]: d for d in new_devices}
    new_serials = set(specs_by_serial)
    issues, seen_pairs = [], {}

    def as_port(end):
        """`<serial>:<port>` names one socket exactly. Return the port id it
        stands for, so every check below applies to it the same way it applies
        to a port written out in full. None when the end names no single
        socket: a bare serial, or a box the Devices tab has not racked yet."""
        hit = serials.parse(end)
        if not hit or hit[1] is None:
            return None
        dev_id = known_serials.get(hit[0])
        return f"{dev_id}:{hit[1]}" if dev_id else None

    # How many free ports of each medium a box still has. Kept as a running
    # pool rather than read fresh per row, so several rows asking the same box
    # for "any port" are counted against one supply — which is what the planner
    # does, and the only way this review can predict it.
    pool = {}

    def free_left(serial, media):
        key = (serial, media)
        if key not in pool:
            spec = specs_by_serial.get(serial)
            if spec:                       # declared but not racked: all free
                pool[key] = spec["fiber_ports"] if media == "fiber" else spec["copper_ports"]
            else:
                dev_id = known_serials.get(serial)
                dev = topology["devices"][dev_id]
                pool[key] = sum(
                    1 for i in range(1, dev["fiber_ports"] + dev["copper_ports"] + 1)
                    if ports[f"{dev_id}:{i}"]["type"] == media
                    and ports[f"{dev_id}:{i}"]["status"] == "free")
        return pool[key]

    def take(serial, media):
        pool[(serial, media)] = free_left(serial, media) - 1

    def declared_media(end):
        """The medium of a port on a box that is declared but not yet racked.
        materialise() lays fibre ports out first and copper after, so a
        declared port number already implies its medium — which is why such a
        row does not need a CABLE column either."""
        hit = serials.parse(end)
        if not hit or hit[1] is None:
            return None
        spec = specs_by_serial.get(hit[0])
        if not spec or not 1 <= hit[1] <= spec["fiber_ports"] + spec["copper_ports"]:
            return None
        return "fiber" if hit[1] <= spec["fiber_ports"] else "copper"

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
        # Resolve named sockets before comparing, so the same port written two
        # ways — as a port id and as <serial>:<port> — is still caught as one.
        src, dst = as_port(src) or src, as_port(dst) or dst

        if src == dst:
            fault(d, "same_port", "source and destination are the same port")
            continue

        # Whatever is still a serial names a BOX rather than one socket: either
        # a bare serial, or a port on kit the Devices tab has not racked yet.
        # Which port a bare serial lands on is decided during planning, so the
        # checks below — port free, port exists on the map — cannot apply.
        serial_ends = [e for e in (src, dst) if looks_like_serial(e)]
        if serial_ends:
            unresolved = [e for e in serial_ends
                          if serials.normalise(e) not in new_serials
                          and serials.normalise(e) not in known_serials]
            if unresolved:
                fault(d, "unknown_serial",
                      "no device has serial " + ", ".join(serials.normalise(e)
                                                          for e in unresolved)
                      + " — it is neither installed nor declared on the Devices tab")
                continue

            # A declared port number still has to exist on the box the Devices
            # tab describes, the same way the planner insists it does.
            over = []
            for e in serial_ends:
                sn, idx = serials.parse(e)
                spec = specs_by_serial.get(sn)
                if idx is not None and spec:
                    total = spec["fiber_ports"] + spec["copper_ports"]
                    if not 1 <= idx <= total:
                        over.append(f"{sn} has no port {idx} — it declares {total}")
            if over:
                fault(d, "unknown_port", "; ".join(over))
                continue

            # Only a BARE serial leaves the medium open. `<serial>:<port>`
            # names a socket, and a socket already has a medium — demanding a
            # CABLE column for it would reject rows the planner accepts.
            bare = [e for e in serial_ends if serials.parse(e)[1] is None]
            cable = _norm_cable(d.get("cable"))
            media = [declared_media(e) or (ports[e]["type"] if e in ports else None)
                     for e in (src, dst)]
            # A named socket at EITHER end settles the medium for the row, so a
            # CABLE column is only needed when neither end names one. Asking for
            # it anyway rejected rows the planner goes on to route happily.
            if bare and cable not in ("fiber", "copper") and not any(media):
                fault(d, "no_cable_type",
                      "this row names a device by serial without a port, so it must "
                      "also say which media to use — add a CABLE column with "
                      "'fiber' or 'copper', or name the port as <serial>:<port>")
                continue

            # Both media knowable? Then a mismatch is knowable too, and saying
            # so now beats letting the router discover it after placement.
            if all(media) and media[0] != media[1]:
                fault(d, "cable_mismatch",
                      f"cable type mismatch: source is {media[0]}, destination is {media[1]}")
                continue
            if cable in ("fiber", "copper") and any(m and m != cable for m in media):
                fault(d, "cable_mismatch",
                      f"the row asks for {cable}, but a named port is "
                      f"{next(m for m in media if m and m != cable)}")
                continue

            # Whatever on this row DID name one socket still deserves the port
            # checks. A bare serial at the other end is no reason to stop
            # noticing that this end is already patched, or that an earlier row
            # asked for it too.
            busy = [f"{p} (held by {ports[p]['circuit']})"
                    for p in (src, dst) if p in ports and ports[p]["status"] != "free"]
            if busy:
                fault(d, "port_in_use", f"already patched: {'; '.join(busy)}")
                continue
            sockets = [e for e in (src, dst)
                       if e in ports or (serials.parse(e) or (None, None))[1] is not None]
            clash = sorted({claimed_by[p] for p in sockets if p in claimed_by})
            if clash:
                fault(d, "duplicate_port",
                      "a port on this row is already taken by row "
                      + ", ".join(str(r) for r in clash))
                continue
            # A bare serial says "any suitable port on this box". Whether one
            # is left is knowable now, and a sheet that asks a 4-port switch
            # for five uplinks should hear about it here, not row by row.
            #
            # Checked BEFORE anything is claimed, and rolled back if it fails:
            # a row that ends up faulted is never planned, so it consumes
            # nothing, and a later row asking for the same port is not clashing
            # with it. Claiming first made this flag rows that were fine.
            wanted = cable or next((m for m in media if m), "")
            spent, full = [], ""
            for e in serial_ends:
                sn, idx = serials.parse(e)
                media_e = declared_media(e) or (ports[e]["type"] if e in ports else wanted)
                if not media_e:
                    continue
                if idx is None and free_left(sn, media_e) <= 0:
                    full = f"device {sn} has no free {media_e} port left"
                    break
                take(sn, media_e)
                spent.append((sn, media_e))
            if full:
                for sn, m in spent:              # give the row's ports back
                    pool[(sn, m)] = free_left(sn, m) + 1
                fault(d, "device_full", full)
                continue

            for p in sockets:
                claimed_by[p] = d["row"]
            # anything further depends on placement, which has not happened yet
            continue

        missing = [p for p in (src, dst) if p not in ports]
        if missing:
            # "port does not exist" is true but unhelpful when the real mistake
            # is in the device half of the cell — a name instead of a full id,
            # or a serial with a digit wrong. Say which half is unrecognised.
            fault(d, "unknown_port",
                  "; ".join(unknown_port_message(topology, p) for p in missing))
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

        # Two ports in one cabinet used to be rejected; it is now the cheapest
        # possible connection (a single patch lead, no trunk consumed) and is
        # exactly what the placement planner aims for.

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
# placing new devices
# --------------------------------------------------------------------------

def _neighbour_racks(topology, serial, demands, serial_index, placed):
    """Which cabinets a not-yet-installed device has to reach, taken from the
    P2P rows that name it. This is the whole reason placement can be automatic:
    the sheet already says what the box talks to."""
    racks = []
    for d in demands:
        ends = [d["src"], d["dst"]]
        if not any(looks_like_serial(e) and serials.normalise(e) == serial for e in ends):
            continue
        for other in ends:
            if looks_like_serial(other) and serials.normalise(other) == serial:
                continue
            if other in topology["ports"]:
                racks.append(topology["ports"][other]["rack"])
            elif looks_like_serial(other):
                o = serials.normalise(other)
                if o in placed:                       # another new box, already sited
                    racks.append(placed[o]["rack"])
                elif o in serial_index:
                    racks.append(topology["devices"][serial_index[o]]["rack"])
    return racks


def plan_devices(topology, new_devices, demands, limit=4, constraints=None):
    """Choose a cabinet and U position for every new device.

    Devices are sited in dependency order so that a box hanging off another new
    box is placed once its neighbour's location is known. Space taken by an
    earlier placement is reserved before the next one is scored, so two new
    devices never get handed the same U.
    """
    serial_index = serials.index(topology)
    by_serial = {d["serial"]: d for d in new_devices}

    # One source of truth for what a usable spec is. Re-checking here rather
    # than trusting the caller to have validated first keeps the review and the
    # plan in agreement BY CONSTRUCTION: a row the review rejects can never
    # come out of the planner as sited.
    #
    # This matters most for a mistyped zone. Swallowing the ZoneError and
    # carrying on with no zone turned the hardest boundary in the system into
    # "anywhere" on a typo — silently, and in the direction that does damage.
    rejected = {i["row"]: i for i in validate_devices(topology, new_devices, demands)["issues"]}

    links = []
    for d in demands:
        if looks_like_serial(d["src"]) and looks_like_serial(d["dst"]):
            links.append((serials.normalise(d["src"]), serials.normalise(d["dst"])))
    order, cycles = placement.order_by_dependency(new_devices, links)

    placed, results, reserved = {}, [], defaultdict(set)
    for serial in order:
        spec = by_serial[serial]
        base = {"row": spec["row"], "serial": serial, "type": spec["type"],
                "u_size": spec["u_size"], "label": spec["label"],
                "zone": spec.get("zone")}

        fault = rejected.get(spec["row"])
        if fault:
            results.append({**base, "status": "failed", "reason": fault["message"]})
            continue

        # validate_devices resolved and stamped this on the way through; a
        # device that got past it has a zone that is either valid or absent
        spec.setdefault("zone", None)
        base["zone"] = spec["zone"]

        neighbours = _neighbour_racks(topology, serial, demands, serial_index, placed)
        try:
            options = placement.rank_positions(topology, spec, neighbours,
                                               extra_taken=reserved, limit=limit,
                                               constraints=constraints)
        except placement.PlacementError as e:
            results.append({**base, "status": "failed", "reason": str(e)})
            continue

        best = options[0]
        placed[serial] = best
        for u in range(best["u_end"], best["u_start"] + 1):
            reserved[best["rack"]].add(u)

        results.append({**base, "status": "ok", "rack": best["rack"],
                        "u_start": best["u_start"], "u_end": best["u_end"],
                        "reason": best["reason"], "score": best["score"],
                        "connects_to": sorted(set(neighbours)),
                        "options": options})

    for serial in cycles:
        spec = by_serial[serial]
        results.append({"row": spec["row"], "serial": serial, "type": spec["type"],
                        "u_size": spec["u_size"], "label": spec["label"],
                        "status": "failed",
                        "reason": "circular dependency — this device and another new "
                                  "device each wait for the other to be placed"})

    results.sort(key=lambda r: r["row"])
    return {"placements": results, "placed": placed,
            "ok": sum(1 for r in results if r["status"] == "ok"),
            "failed": sum(1 for r in results if r["status"] != "ok")}


def materialise(topology, spec, site):
    """Create a placed device (and its ports) in a topology, so the router can
    treat it exactly like any other box. Used on the working copy while
    planning, and again on the live map at execute time."""
    rack = site["rack"]
    # The device id must be unique, and a truncated serial is not: SN-NEWSRV01
    # and SN-NEWSRV02 share their first characters, and the second box would
    # silently overwrite the first. Use the whole serial, and still guard
    # against a clash with something already in the rack.
    name = f"NEW-{spec['serial']}"
    dev_id = f"{rack}:{name}"
    suffix = 2
    while dev_id in topology["devices"]:
        dev_id = f"{rack}:{name}-{suffix}"
        suffix += 1
    topology["devices"][dev_id] = {
        "id": dev_id, "rack": rack, "name": name, "type": spec["type"],
        "u_start": site["u_start"], "u_size": spec["u_size"],
        "label": spec["label"], "serial": spec["serial"],
        "fiber_ports": spec["fiber_ports"], "copper_ports": spec["copper_ports"],
        "zone": spec.get("zone"),
        "installed_by_plan": True,
    }
    for i in range(1, spec["fiber_ports"] + 1):
        pid = f"{dev_id}:{i}"
        topology["ports"][pid] = {"id": pid, "rack": rack, "device": dev_id, "index": i,
                                  "type": "fiber", "status": "free",
                                  "peer": None, "circuit": None, "role": None}
    start = spec["fiber_ports"] + 1
    for i in range(start, start + spec["copper_ports"]):
        pid = f"{dev_id}:{i}"
        topology["ports"][pid] = {"id": pid, "rack": rack, "device": dev_id, "index": i,
                                  "type": "copper", "status": "free",
                                  "peer": None, "circuit": None, "role": None}
    return dev_id


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

def _resolve_endpoints(topology, demands):
    """Turn every endpoint into a concrete port on the map, and refuse the row
    here — where the fault is about the SHEET — rather than letting the router
    discover it three layers down.

    Ports chosen here are remembered as the rows are walked, because two rows
    naming the same device must land on two different ports of it: the whole
    point of a switch is that it has many.

    Every check applies whichever way the port was written. It used to skip any
    endpoint that was not a serial, so `5488209915:4` was checked for existing,
    being free and matching the media, while `A1-S05:FIB-PP-01:4` — the same
    socket — was checked for none of them and failed later with a rougher
    message. Two spellings of one port have to behave identically.
    """
    index = serials.index(topology)
    ports = topology["ports"]
    taken, out = {}, []          # port -> the row that claimed it

    for d in demands:
        d = dict(d)
        declared = _norm_cable(d.get("cable"))
        # A row claims its ports only if the WHOLE row resolves. A row that
        # ends up malformed is never planned, so it consumes nothing, and a
        # later row asking for the same port is not clashing with anything —
        # holding the claim made this refuse rows the review passed.
        claims = {}

        # What medium is this row? A named socket answers it, whichever END it
        # is on — so settle it before resolving anything. Reading it as we went
        # made the same row behave differently depending on which side happened
        # to be the open one.
        media = declared
        if media not in ("fiber", "copper"):
            for side in ("src", "dst"):
                pid = port_id_for(topology, index, d[side])
                if pid:
                    media = ports[pid]["type"]
                    break

        for side in ("src", "dst"):
            value = d[side]
            if not value:
                continue

            serial = wanted = None
            if looks_like_serial(value):
                serial, wanted = serials.parse(value)
                dev_id = index.get(serial)
                if not dev_id:
                    d["malformed"] = f"serial {serial} is not a device on the map"
                    break

                if wanted is None:
                    # Naming only the box leaves the port open, and a box has
                    # both kinds of socket — so something has to say which.
                    if media not in ("fiber", "copper"):
                        d["malformed"] = (
                            f"row refers to device {serial} by serial but does not "
                            "say which cable type to use (add a CABLE column, or "
                            f"write {serial}:<port> to name the port itself)")
                        break
                    port = free_port_on_device(topology, dev_id, media,
                                               {**taken, **claims})
                    if not port:
                        dev = topology["devices"][dev_id]
                        d["malformed"] = (f"device {serial} ({dev['label']}) has no free "
                                          f"{media} port left")
                        break
                    claims[port] = d["row"]
                    d[side] = port
                    d.setdefault("resolved", {})[side] = {"serial": serial, "port": port,
                                                          "device": dev_id}
                    continue

                port = f"{dev_id}:{wanted}"
                shown = f"port {serial}:{wanted}"
            else:
                port = value
                shown = f"port {value}"

            # ---- the same checks, however the socket was spelled ----
            p = ports.get(port)
            if p is None:
                if serial:
                    dev = topology["devices"][index[serial]]
                    d["malformed"] = (f"device {serial} ({dev['label']}) has no port "
                                      f"{wanted} — it has "
                                      f"{dev['fiber_ports'] + dev['copper_ports']}")
                else:
                    d["malformed"] = unknown_port_message(topology, port)
                break

            if port in taken or port in claims:
                # Claimed by an earlier row of THIS sheet, which is a different
                # problem from one already patched on the map — and the row
                # number is what the user needs to go and fix. When the
                # claiming row is this one, the row is patching a port to
                # itself; say that rather than pointing back at the line the
                # user is already reading.
                owner = claims.get(port, taken.get(port))
                d["malformed"] = (
                    "source and destination are the same port"
                    if owner == d["row"] else
                    f"{shown} is already claimed by row {owner} of this sheet")
                break

            if p["status"] != "free":
                d["malformed"] = (f"{shown} is already patched"
                                  + (f" by {p['circuit']}" if p.get("circuit") else ""))
                break

            if declared and p["type"] != declared:
                d["malformed"] = (f"{shown} is {p['type']}, but the row asks "
                                  f"for {declared}")
                break
            if media and p["type"] != media:
                d["malformed"] = (f"cable type mismatch: this row has a {media} end "
                                  f"and a {p['type']} end")
                break

            media = media or p["type"]
            claims[port] = d["row"]
            d[side] = port
            if serial:
                d.setdefault("resolved", {})[side] = {"serial": serial, "port": port,
                                                      "device": index[serial]}

        if not d.get("malformed"):
            taken.update(claims)
        out.append(d)
    return out


def _jump_chain(topology, route):
    """The route as a list of stops the browser can draw directly.

    One entry per cabinet the cable passes through, carrying the U position and
    port it lands on. Sent ready-made because the browser's copy of the map is
    the one it downloaded at load time, which does not contain the devices this
    very plan is proposing to install.
    """
    ports = [route["src_port"]] + [t["port"] for t in route["transit_points"]] \
            + [route["dst_port"]]
    racks = list(route["hop_racks"])
    # An intra-rack patch has ONE cabinet but TWO ends. Zipping them would
    # drop the far end and draw a diagram with a single stop, which is exactly
    # the case where seeing both ends matters most.
    if len(racks) == 1 and len(ports) == 2:
        racks = racks * 2

    stops = []
    for rack, port_id in zip(racks, ports):
        p = topology["ports"].get(port_id)
        dev = topology["devices"].get(p["device"]) if p else None
        stops.append({
            "rack": rack,
            "u": dev["u_start"] if dev else None,
            "u_size": dev["u_size"] if dev else 1,
            "port": p["index"] if p else None,
            "device": dev["name"] if dev else "",
            "is_new": bool(dev and dev.get("installed_by_plan")),
        })
    return stops


def plan(topology, demands, prefs=None, already_isolated=False, new_devices=(),
         constraints=None):
    """Route every demand against a working copy, in an order chosen by the
    preferences. Returns one result per demand plus a summary. Never mutates
    the topology it is given.

    Set `already_isolated` when the caller has handed us a private snapshot
    that nobody else can touch — then we skip the defensive copy instead of
    paying for a second one.

    `new_devices` are specs from the Devices tab. They are sited and created in
    the working copy FIRST, so by the time the P2P rows are routed the new kit
    is indistinguishable from equipment that was already racked — which is
    exactly how the sheet is written.
    """
    prefs = {**DEFAULT_PREFS, **(prefs or {})}
    work = topology if already_isolated else copy.deepcopy(topology)

    siting = None
    if new_devices:
        siting = plan_devices(work, list(new_devices), demands,
                              constraints=constraints)
        specs = {d["serial"]: d for d in new_devices}
        for site in siting["placements"]:
            if site["status"] == "ok":
                site["device_id"] = materialise(work, specs[site["serial"]], site)

    # serials only become resolvable once the new kit exists in `work`
    demands = _resolve_endpoints(work, demands)

    ordered = _order_demands(work, demands, prefs["group_adjacent"])
    need_options = prefs["redundancy_split"] or prefs["load_balance"]

    seats = Counter()
    results, planned = [], 0
    for d in ordered:
        seat = seats[d["group_key"]]
        seats[d["group_key"]] += 1

        base = {"row": d["row"], "src": d["src"], "dst": d["dst"],
                "group": d["group_key"], "label": d.get("label", ""),
                "seat": seat + 1}
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
                        # enough to draw the cable-jump diagram in the browser
                        # WITHOUT it having to look ports up in its own copy of
                        # the map — which it cannot do for a device this plan
                        # is about to create
                        "jump": _jump_chain(work, route),
                        "hops": len(route["segments"]),
                        "cable_type": route["cable_type"],
                        "domain": route["domain"],
                        "total_length_m": route["total_length_m"],
                        "strands": [{"edge_id": s["edge_id"], "index": s["strand_index"]}
                                    for s in route["segments"]]})

    results.sort(key=lambda r: r["row"])
    out = {"prefs": prefs, "results": results, "summary": _summary(results)}
    if siting is not None:
        out["siting"] = siting
        out["summary"]["devices_placed"] = siting["ok"]
        out["summary"]["devices_failed"] = siting["failed"]
    return out


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

def execute_devices(topology, siting, new_devices):
    """Create the sited devices on the LIVE map, before their cabling is
    committed. Idempotent by serial: a plan committed twice does not rack the
    same box a second time."""
    if not siting:
        return {"installed": [], "skipped": []}
    specs = {d["serial"]: d for d in new_devices}
    known = serials.index(topology)
    installed, skipped = [], []

    for site in siting["placements"]:
        if site["status"] != "ok":
            continue
        if site["serial"] in known:
            skipped.append({"serial": site["serial"],
                            "reason": "already installed"})
            continue
        # the position was chosen against a working copy; make sure the live
        # map still has that space before claiming it
        free = {u for top, height in placement.free_gaps(topology, site["rack"])
                for u in range(top - height + 1, top + 1)}
        needed = set(range(site["u_end"], site["u_start"] + 1))
        if not needed <= free:
            skipped.append({"serial": site["serial"],
                            "reason": f"{site['rack']} U{site['u_end']}-U{site['u_start']} "
                                      "was taken since the plan was made"})
            continue
        spec = specs[site["serial"]]
        dev_id = materialise(topology, spec, site)
        # Ship the device record and its ports, not just where the box went.
        # Whoever asked for this execution is holding a copy of the map that
        # predates the device, and the cabling committed a moment later
        # references ports that copy has never heard of.
        n_ports = spec["fiber_ports"] + spec["copper_ports"]
        installed.append({"serial": site["serial"], "device_id": dev_id,
                          "rack": site["rack"], "u_start": site["u_start"],
                          "u_end": site["u_end"],
                          "device": topology["devices"][dev_id],
                          "ports": [topology["ports"][f"{dev_id}:{i}"]
                                    for i in range(1, n_ports + 1)]})
    return {"installed": installed, "skipped": skipped}


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
