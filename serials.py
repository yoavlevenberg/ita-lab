#!/usr/bin/env python3
"""
serials.py
==========
Every device carries a unique serial number, the way real hardware does — it
is how a demand sheet refers to a box that is either already racked or still
in a delivery crate.

Serials are derived from the device id by a stable hash, so:

  * regenerating the topology produces exactly the same serials, and a demand
    sheet written last week still resolves today;
  * they are opaque, like a real vendor serial, rather than a second name for
    the location — a device that gets moved keeps its serial.

Collisions are checked at build time rather than assumed away: 6,312 devices
against 32^10 possible strings will not collide, but "will not" is not the
same as "did not".
"""

import hashlib
import re

# Serials are plain numbers, the way an asset tag or an inventory export gives
# them. Ten digits: wide enough that 6,312 devices collide with probability
# around one in a million, and uniqueness is asserted at build time anyway.
LENGTH = 10
FIRST = 10 ** (LENGTH - 1)          # no leading zero, so the number survives
SPAN = 9 * FIRST                    # a round-trip through a spreadsheet cell

# What counts as a serial. The synthetic map derives ten digits, but real ITA
# serials are the vendor's own and mostly alphanumeric (FDO2134X0AB, 7XJ2K3).
# So: four or more digits, OR 5-24 letters and digits with at least one digit.
# No hyphen, no colon, no space — which is what keeps a rack (A1-S05), a device
# name (SRV-01), a port id (A1-S05:FIB-PP-01:2) and an ordinary word apart
# from a serial. Compared case-insensitively: canonical() upper-cases.
SERIAL = re.compile(r"\d{4,}|(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{5,24}")

# A port is addressed as <serial>:<port>, e.g. 4827193056:12 or FDO2134X0AB:12.
# The serial alone means "this device, any suitable port"; with the suffix,
# that exact port.
_SERIAL_RE = re.compile(r"^\s*(" + SERIAL.pattern + r")\s*(?::\s*(\d+)\s*)?$")

# "SN 123", "S/N: 123", "sn-123" — the label is dropped only when a separator
# follows it, because a real serial may itself begin with SN (SN12345ABC)
_LABEL_RE = re.compile(r"^\s*s[/\-]?n(?:\s*[:\-]\s*|\s+)", re.I)


class SerialError(Exception):
    """Two devices ended up with the same serial, or one was not found."""


def serial_for(device_id):
    """Deterministic, opaque, stable across rebuilds."""
    digest = hashlib.sha256(device_id.encode("utf-8")).digest()
    return str(FIRST + int.from_bytes(digest[:8], "big") % SPAN)


def canonical(serial):
    return str(serial or "").strip().upper()


def is_serial(text):
    """Is this, exactly, a serial (no port suffix, no label)?"""
    return bool(SERIAL.fullmatch(str(text or "").strip()))


def index(topology):
    """serial -> device id, for resolving what a demand sheet refers to.

    Only usable serials go in. Real data has blanks, "N/A", and two boxes
    sharing a serial now and then; a duplicate is left OUT rather than letting
    the last one silently win — a sheet that names it then fails as unknown,
    and duplicates() says why."""
    seen, dup = {}, set()
    for d in topology["devices"].values():
        s = canonical(d.get("serial"))
        if not is_serial(s):
            continue
        if s in seen:
            dup.add(s)
        seen[s] = d["id"]
    return {s: i for s, i in seen.items() if s not in dup}


def duplicates(topology):
    """serial -> [device ids], for every serial more than one device carries."""
    by = {}
    for d in topology["devices"].values():
        s = canonical(d.get("serial"))
        if is_serial(s):
            by.setdefault(s, []).append(d["id"])
    return {s: ids for s, ids in by.items() if len(ids) > 1}


def parse(text):
    """Read an endpoint written as a serial.

    Returns (serial, port_index) — port_index is None when the cell named only
    the device and left the choice of port to the planner. Returns None when
    the text is not a serial at all, which is how a port id like
    'A1-S05:FIB-PP-01:2' is told apart from '4827193056:12'.
    """
    # a stray "SN" / "S/N" prefix is tolerated here rather than only in
    # normalise(), so every caller agrees on what counts as a serial
    raw = _LABEL_RE.sub("", str(text or ""))
    m = _SERIAL_RE.match(raw)
    if not m:
        return None
    return (canonical(m.group(1)), int(m.group(2)) if m.group(2) else None)


def normalise(text):
    """The serial alone, without any port suffix."""
    hit = parse(text)
    return hit[0] if hit else str(text or "").strip()


def looks_like(text):
    """Is this cell naming a device by serial, rather than a port id?"""
    return parse(text) is not None
