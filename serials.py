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

PREFIX = "SN-"
LENGTH = 10

# Crockford-style alphabet: no I, L, O or U, so a serial read off a label or
# dictated over the phone cannot be transcribed ambiguously.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class SerialError(Exception):
    """Two devices ended up with the same serial, or one was not found."""


def serial_for(device_id):
    """Deterministic, opaque, stable across rebuilds."""
    digest = hashlib.sha256(device_id.encode("utf-8")).digest()
    n = int.from_bytes(digest[:8], "big")
    out = []
    for _ in range(LENGTH):
        n, r = divmod(n, len(ALPHABET))
        out.append(ALPHABET[r])
    return PREFIX + "".join(out)


def assign_all(topology):
    """Stamp a serial on every device and verify uniqueness."""
    seen = {}
    for dev_id, dev in topology["devices"].items():
        s = serial_for(dev_id)
        if s in seen:
            raise SerialError(f"serial {s} generated for both {seen[s]} and {dev_id}")
        seen[s] = dev_id
        dev["serial"] = s
    return len(seen)


def index(topology):
    """serial -> device id, for resolving what a demand sheet refers to."""
    return {d["serial"]: d["id"] for d in topology["devices"].values() if d.get("serial")}


def normalise(text):
    """Accept a serial typed loosely — lower case, spaces, a missing prefix —
    because these get copied off labels and out of inventory exports."""
    t = "".join(str(text).split()).upper().replace("_", "-")
    if t and not t.startswith(PREFIX):
        bare = t[3:] if t[:3].rstrip("-") in ("SN", "S/N") else t
        t = PREFIX + bare.lstrip("-")
    return t
