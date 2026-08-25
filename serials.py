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

# A port is addressed as <serial>:<port>, e.g. 4827193056:12. Bare digits mean
# "this device, any suitable port"; with the suffix, that exact port.
_SERIAL_RE = re.compile(r"^\s*(\d{4,})\s*(?::\s*(\d+)\s*)?$")


class SerialError(Exception):
    """Two devices ended up with the same serial, or one was not found."""


def serial_for(device_id):
    """Deterministic, opaque, stable across rebuilds."""
    digest = hashlib.sha256(device_id.encode("utf-8")).digest()
    return str(FIRST + int.from_bytes(digest[:8], "big") % SPAN)


def index(topology):
    """serial -> device id, for resolving what a demand sheet refers to."""
    return {d["serial"]: d["id"] for d in topology["devices"].values() if d.get("serial")}


def parse(text):
    """Read an endpoint written as a serial.

    Returns (serial, port_index) — port_index is None when the cell named only
    the device and left the choice of port to the planner. Returns None when
    the text is not a serial at all, which is how a port id like
    'A1-S05:FIB-PP-01:2' is told apart from '4827193056:12'.
    """
    # a stray "SN" / "S/N" prefix is tolerated here rather than only in
    # normalise(), so every caller agrees on what counts as a serial
    raw = re.sub(r"^\s*(?:s[/\-]?n)\s*[:\-]?\s*", "", str(text or ""), flags=re.I)
    m = _SERIAL_RE.match(raw)
    if not m:
        return None
    return (m.group(1), int(m.group(2)) if m.group(2) else None)


def normalise(text):
    """The serial alone, without any port suffix."""
    hit = parse(text)
    return hit[0] if hit else str(text or "").strip()


def looks_like(text):
    """Is this cell naming a device by serial, rather than a port id?"""
    return parse(text) is not None
