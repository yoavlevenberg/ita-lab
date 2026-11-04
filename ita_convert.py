#!/usr/bin/env python3
"""
ita_convert.py
==============
Turns the local copy that ita_sync.py made (ita_cache/<site>/) into the map
ITA Lab plans on — the same topology.json shape pathengine already routes
over. Nothing here talks to ITA; it reads files and writes one.

    python ita_convert.py --cache ita_cache/<site>                 -> <cache>/topology.json
    python ita_convert.py --cache ita_cache/<site> --out my.json
    python ita_convert.py --cache ita_cache/<site> --unit-top      see UNIT POSITION

WHAT ITA GIVES, WHAT THE MAP WANTS
----------------------------------
  ITA asset record                     map
  rack (by name)                       racks[<name>]        row/room from the tree;
                                                            pod, EOR, MDA, domain left
                                                            empty — ITA does not have
                                                            them ("just known", per site
                                                            config, a later step)
  device under a rack                  devices[<rack>:<name>]
  rackMounted.unitPosition/Height      u_start (top U) / u_size
  serialNumber                         serial
  network-paths portConfigurations     ports, one per (cassette, number), numbered
                                       straight through in the order ITA lists them
  connection rear <-> rear between     one strand of the trunk between those two
    panels in two racks                racks — ITA records every fiber as its own
                                       connection (user, 2026-11-04), and front port
                                       N is rear port N is the far panel's port N
  networkPath with a patch cord in it, an existing circuit: endpoints, the racks
    or more than one trunk             it crosses, the strand it holds on each trunk

A networkPath that is ONE trunk fiber and nothing else is not a circuit: it is
an unpatched strand, which ITA still reports as a "path" from panel to panel.

UNIT POSITION
-------------
The map counts a device from its TOP U (u_start 42, 1U = U42). ITA's
unitPosition is taken as the BOTTOM U, the common convention; if devices show
up shifted by their own height minus one, the server says the top, and
--unit-top flips it. To be confirmed on the first real copy.

A PARTIAL COPY
--------------
A one-rack copy refers to panels in racks it did not copy. Those come in as
stubs, named from the path ITA attached to every connection end, marked
"external": true — enough to show where the trunks go, and counted on screen.

Screen: counts only. The output file holds real names and stays where the
cache is, on the closed network (ita_cache/ is git-ignored).
"""

import argparse
import json
import re
import sys
import time
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path

FIBER = re.compile(r"\b(LC|SC|ST|FC|MPO|MTP|E2000|MU|SFP|QSFP|fib(er|re)|optic)", re.I)
COPPER = re.compile(r"\b(RJ-?45|RJ-?11|8P8C|CAT ?\d|copper|UTP|STP|110)", re.I)
RACK = re.compile(r"rack", re.I)
ROW = re.compile(r"^row$", re.I)
ROOM = re.compile(r"room", re.I)
CONTAINER = re.compile(r"location|site|building|floor|room|row|rack|cage|zone|folder|unsupported",
                       re.I)
DEFAULT_RACK_U = 42


def media_of(connector):
    c = str(connector or "")
    if FIBER.search(c):
        return "fiber"
    if COPPER.search(c):
        return "copper"
    return "other"


def device_type(kind, media):
    k = str(kind or "").lower()
    if "patch" in k:
        return "copper_patch_panel" if media == "copper" else "fiber_patch_panel"
    if re.search(r"network|switch|router", k):
        return "switch"
    if "server" in k:
        return "server"
    return k or "other"


class Converter:
    def __init__(self, cache, unit_top=False):
        self.cache = Path(cache)
        self.unit_top = unit_top
        self.manifest = json.loads((self.cache / "manifest.json").read_text(encoding="utf-8"))
        self.tree = self.manifest["tree"]
        self.notes = Counter()
        # only what this copy's own tree lists — a file some earlier copy
        # left in the folder must not come back as a device or a circuit
        self.assets, self.network = {}, {}
        for aid in self.tree:
            f = self.cache / "assets" / f"{_safe(aid)}.json"
            if f.exists():
                self.assets[aid] = json.loads(f.read_text(encoding="utf-8"))
            f = self.cache / "network" / f"{_safe(aid)}.json"
            if f.exists():
                self.network[_safe(aid)] = json.loads(f.read_text(encoding="utf-8"))
        if self.manifest.get("complete") is False:
            self.notes["THE COPY IS INCOMPLETE: " + str(self.manifest.get("aborted"))] += 1
        missing = self.manifest.get("network_missing") or []
        if missing:
            self.notes["devices whose ports/connections were not copied"] += len(missing)
        self.topology = {"meta": {}, "pods": {}, "racks": {}, "devices": {},
                         "ports": {}, "edges": [], "circuits": {}}
        self.rack_of_asset = {}       # ITA rack uuid -> map rack id
        self.device_of_asset = {}     # ITA device uuid -> map device id
        self.modules = {}             # map device id -> [(keys, offset, count, media)]

    # -- the tree ------------------------------------------------------------
    def _ancestor(self, aid, pattern):
        a = self.tree.get(aid, {}).get("parent")
        while a:
            if pattern.search(self.tree[a]["type"]):
                return a
            a = self.tree[a].get("parent")
        return None

    def _name(self, aid):
        return str(self.assets.get(aid, {}).get("name") or aid).strip()

    def build_racks(self):
        racks = [a for a, t in self.tree.items() if RACK.search(t["type"])]
        names = Counter(self._name(a) for a in racks)
        position = Counter()
        for aid in racks:
            name = self._name(aid)
            rid = name if names[name] == 1 else f"{name}#{aid[:6]}"
            if names[name] > 1:
                self.notes["racks sharing a name (uuid added)"] += 1
            parent = self.tree[aid].get("parent")
            row = self._name(parent) if parent and ROW.search(self.tree[parent]["type"]) else None
            room = self._ancestor(aid, ROOM)
            position[parent] += 1
            self.topology["racks"][rid] = {
                "id": rid, "uuid": aid,
                "pod": None, "row": row, "room": self._name(room) if room else None,
                "position": position[parent],
                "is_eor": False, "is_mda": False,
                "u_height": _rack_units(self.assets.get(aid, {})),
                "domain": None,
            }
            self.rack_of_asset[aid] = rid

    # -- devices and their ports ---------------------------------------------
    def build_devices(self):
        for aid, t in self.tree.items():
            if CONTAINER.search(t["type"]):
                continue
            rack = self._ancestor(aid, RACK)
            if rack is None:
                self.notes["devices not in a rack (skipped)"] += 1
                continue
            self._add_device(aid, self.rack_of_asset[rack], self.assets.get(aid, {}),
                             self.network.get(_safe(aid)))

    def _add_device(self, aid, rack, record, net, external=False):
        name = str(record.get("name") or aid).strip()
        did = f"{rack}:{name}"
        while did in self.topology["devices"]:
            did += "#2"
            self.notes["devices sharing a name in one rack"] += 1
        mods, offset, counts = [], 0, Counter()
        for pc in (net or {}).get("portConfigurations") or []:
            conf = pc.get("portConfiguration") or {}
            n = int(conf.get("portCount") or 0)
            media = media_of(conf.get("connectorType"))
            keys = {str(k) for k in (conf.get("id"), conf.get("name")) if k is not None}
            mods.append({"keys": keys, "offset": offset, "count": n,
                         "start": int(conf["portStart"]) if conf.get("portStart") is not None
                         else 1, "media": media,
                         "connector": conf.get("connectorType")})
            counts[media] += n
            offset += n
        if counts["other"]:
            self.notes["ports with an unknown connector type"] += counts["other"]
        mounted = record.get("rackMounted") or {}
        size = int(mounted.get("unitHeight") or 1)
        pos = mounted.get("unitPosition")
        top = None
        if isinstance(pos, (int, float)):
            top = int(pos) if self.unit_top else int(pos) + size - 1
        major = "copper" if counts["copper"] > counts["fiber"] else "fiber"
        dev = {
            "id": did, "uuid": aid, "rack": rack, "name": name,
            "type": device_type(record.get("type") or record.get("assetType"), major),
            "u_start": top, "u_size": size,
            "label": str(record.get("modelName") or record.get("description") or ""),
            "fiber_ports": counts["fiber"], "copper_ports": counts["copper"],
            "serial": str(record.get("serialNumber") or ""),
        }
        if external:
            dev["external"] = True
        self.topology["devices"][did] = dev
        self.device_of_asset[aid] = did
        self.modules[did] = mods
        for m in mods:
            for i in range(1, m["count"] + 1):
                self._add_port(dev, m["offset"] + i, m["media"], m["connector"])
        return did

    def _add_port(self, dev, index, media, connector=None):
        pid = f"{dev['id']}:{index}"
        if pid not in self.topology["ports"]:
            self.topology["ports"][pid] = {
                "id": pid, "rack": dev["rack"], "device": dev["id"], "index": index,
                "type": media, "status": "free", "peer": None, "circuit": None, "role": None}
            if connector:
                # the plug a patch cord needs at this end (patchcords.py)
                self.topology["ports"][pid]["connector"] = str(connector)
        return pid

    # -- one connection end -> one map port -----------------------------------
    def port_of(self, end):
        """The map port an END names. Ports are numbered straight through the
        device's cassettes, so (cassette, number) -> offset + number."""
        aid = str(end.get("assetId"))
        did = self.device_of_asset.get(aid) or self._stub(end)
        dev = self.topology["devices"][did]
        number = int(end.get("portNumber") or 0)
        mods = self.modules.get(did) or []
        module = str(end.get("networkModule")) if end.get("networkModule") is not None else None
        m = next((m for m in mods if module in m["keys"]), None)
        if m is None:
            same = [m for m in mods if media_of(m["connector"]) == media_of(end.get("connectorType"))]
            m = same[0] if len(same) == 1 else (mods[0] if len(mods) == 1 else None)
        if m is None:
            # A cassette the device's own record did not list (always so for a
            # stub). It gets a number range of its own past every known port —
            # never the bare port number, which is cassette 1's port of that number.
            if mods and not dev.get("external"):
                self.notes["connection ends on an unknown cassette"] += 1
            top = max([x["offset"] + x["count"] for x in mods] + [0])
            m = {"keys": {module or f"?{end.get('connectorType')}"}, "offset": top,
                 "count": 0, "start": 0, "media": media_of(end.get("connectorType")),
                 "connector": end.get("connectorType"), "extra": True}
            self.modules.setdefault(did, []).append(m)
        index = m["offset"] + number - m["start"] + 1
        if m.get("extra"):
            m["count"] = max(m["count"], number - m["start"] + 1)
        return self._add_port(dev, index, m["media"], m.get("connector"))

    def _stub(self, end):
        """A device outside the copy: placed in a stub rack named from the
        path ITA put on the connection end."""
        path = end.get("assetPath") or []
        rack = next((p for p in path if RACK.search(str(p.get("elementType", "")))), None)
        rname = str(rack.get("name") if rack else "(outside the copy)").strip()
        rid = self.rack_of_asset.get(str(rack.get("id"))) if rack else None
        if rid is None:
            rid = rname
            if rid not in self.topology["racks"]:
                self.topology["racks"][rid] = {
                    "id": rid, "uuid": rack.get("id") if rack else None, "pod": None,
                    "row": None, "room": None, "position": 0, "is_eor": False,
                    "is_mda": False, "u_height": DEFAULT_RACK_U, "domain": None,
                    "external": True}
                self.notes["racks outside the copy (stubs)"] += 1
            if rack:
                self.rack_of_asset[str(rack.get("id"))] = rid
        self.notes["devices outside the copy (stubs)"] += 1
        return self._add_device(str(end.get("assetId")), rid,
                                {"name": end.get("assetName"), "type": end.get("assetType")},
                                None, external=True)

    # -- connections -> trunks, paths -> circuits ------------------------------
    def _is_trunk(self, conn):
        s, d = conn.get("source") or {}, conn.get("destination") or {}
        return bool(_rear(s) and _rear(d)
                    and "patch" in str(s.get("assetType", "")).lower()
                    and "patch" in str(d.get("assetType", "")).lower())

    def _rack_of_end(self, end):
        return self.topology["ports"][self.port_of(end)]["rack"]

    def collect(self):
        connections, paths = OrderedDict(), OrderedDict()
        for body in self.network.values():
            for pc in body.get("portConfigurations") or []:
                for cp in pc.get("connectionsAndPaths") or []:
                    np = cp.get("networkPath") or {}
                    for c in [cp.get("connection")] + list(np.get("connections") or []):
                        if c and c.get("id") is not None:
                            connections.setdefault(str(c["id"]), c)
                    if np.get("connections") and np.get("beginItem") and np.get("endItem"):
                        key = tuple(sorted((self.port_of(np["beginItem"]),
                                            self.port_of(np["endItem"]))))
                        paths.setdefault(key, np)
        return connections, paths

    def build_trunks(self, connections):
        """Every rear-to-rear fiber between panels in two racks is a strand.
        Strands of one rack pair are numbered by (panel, port) on the side
        whose rack sorts first — a stable order, and the order a technician
        counts them in."""
        groups = defaultdict(list)
        self.strand_of = {}           # connection id -> (edge id, media, index)
        for cid, c in connections.items():
            if not self._is_trunk(c):
                continue
            a, b = c["source"], c["destination"]
            ra, rb = self._rack_of_end(a), self._rack_of_end(b)
            if ra == rb:
                self.notes["rear-to-rear links inside one rack (ignored)"] += 1
                continue
            if rb < ra:
                a, b, ra, rb = b, a, rb, ra
            media = self.topology["ports"][self.port_of(a)]["type"]
            groups[(ra, rb, media)].append((self.port_of(a), self.port_of(b), cid, c))
        edges = {}
        for (ra, rb, media), strands in sorted(groups.items()):
            eid = f"{ra}<->{rb}"
            edge = edges.get(eid)
            if edge is None:
                edge = edges[eid] = {"id": eid, "from": ra, "to": rb, "domain": None,
                                     "cable_types": {}, "length_m": 0.0, "strand_ports": {}}
            strands.sort(key=lambda s: _port_sort(s[0]))
            edge["cable_types"][media] = {"capacity": len(strands), "strands": {}}
            edge["strand_ports"][media] = {}
            for i, (pa, pb, cid, c) in enumerate(strands, 1):
                edge["strand_ports"][media][str(i)] = {ra: pa, rb: pb}
                self.strand_of[cid] = (eid, media, i)
                length = (c.get("cableType") or {}).get("length")
                if isinstance(length, (int, float)) and length > edge["length_m"]:
                    edge["length_m"] = float(length)
        self.topology["edges"] = list(edges.values())
        self.edges = edges

    def build_circuits(self, paths):
        """Every networkPath that is more than one unpatched trunk fiber is a
        circuit. The cables are followed end to end from beginItem — device to
        device, whatever order ITA listed them in — so the racks come out in
        the order a technician walks them, and EVERY port on the way is marked
        taken: the patch-cord ports, and both panel ports of every trunk fiber,
        because front port N is rear port N (code review, 2026-11-04)."""
        n = 0
        owner = {}
        for key, np in sorted(paths.items()):
            conns = [c for c in np.get("connections") or [] if c]
            if not conns:
                continue
            if len(conns) == 1 and str(conns[0].get("id")) in self.strand_of:
                continue                            # one trunk fiber, nothing patched to it
            steps = self._chain(np["beginItem"], conns)
            if steps is None:
                self.notes["paths whose cables do not chain (taken as listed)"] += 1
                steps = [(c.get("source") or {}, c.get("destination") or {}, c) for c in conns]
            n += 1
            cid = f"CIR-{n:04d}"
            a_port, b_port = self.port_of(np["beginItem"]), self.port_of(np["endItem"])
            rack = lambda pid: self.topology["ports"][pid]["rack"]      # noqa: E731
            hops = [rack(a_port)]
            strands, segments, on_path = [], [], []
            for frm, to, c in steps:
                pf, pt = self.port_of(frm), self.port_of(to)
                on_path += [pf, pt]
                if rack(pt) != hops[-1]:
                    hops.append(rack(pt))
                hit = self.strand_of.get(str(c.get("id")))
                if hit is None:
                    if rack(pf) != rack(pt):
                        self.notes["direct cables between racks (no trunk)"] += 1
                    continue
                eid, media, idx = hit
                edge = self.edges[eid]
                edge["cable_types"][media]["strands"][str(idx)] = cid
                segments.append(eid)
                strands.append({"edge_id": eid, "cable_type": media, "strand_index": idx,
                                "from_port": pf, "to_port": pt})
            transit = []
            for pid in on_path:
                if pid not in (a_port, b_port) and pid not in transit:
                    transit.append(pid)
            media = strands[0]["cable_type"] if strands else self.topology["ports"][a_port]["type"]
            self.topology["circuits"][cid] = {
                "id": cid, "cable_type": media, "domain": None,
                "a_port": a_port, "b_port": b_port,
                "hop_racks": hops, "segment_ids": segments, "strands": strands,
                "intra_rack": rack(a_port) == rack(b_port), "transit_ports": transit,
                "total_length_m": sum(self.edges[s]["length_m"] for s in segments),
            }
            for pid in [a_port, b_port] + transit:
                if owner.get(pid, cid) != cid:
                    self.notes["ports on two different paths (check in ITA)"] += 1
                owner[pid] = cid
            for pid, peer in ((a_port, b_port), (b_port, a_port)):
                self.topology["ports"][pid].update(status="used", peer=peer, circuit=cid,
                                                   role="endpoint")
            for pid in transit:
                self.topology["ports"][pid].update(status="used", circuit=cid, role="transit")

    def _chain(self, begin, conns):
        """The connections as steps (from END, to END, connection), walked
        from `begin`: each step leaves the device the last one reached. A
        panel counts as one device front and rear, so a patch cord into
        front 5 continues on the trunk out of rear 5. Tries the order ITA
        gave, then the reverse; None if neither chains."""
        for order in (conns, conns[::-1]):
            here, steps = str(begin.get("assetId")), []
            for c in order:
                s, d = c.get("source") or {}, c.get("destination") or {}
                if str(s.get("assetId")) == here:
                    steps.append((s, d, c))
                    here = str(d.get("assetId"))
                elif str(d.get("assetId")) == here:
                    steps.append((d, s, c))
                    here = str(s.get("assetId"))
                else:
                    break
            else:
                return steps
        return None

    def convert(self):
        self.build_racks()
        self.build_devices()
        connections, paths = self.collect()
        self.build_trunks(connections)
        self.build_circuits(paths)
        rack_u = Counter(r["u_height"] for r in self.topology["racks"].values())
        self.topology["meta"] = {
            "description": "Converted from ITA by ita_convert.py",
            "source": "ita", "synced_at": self.manifest.get("synced_at"),
            "converted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "rack_u": rack_u.most_common(1)[0][0] if rack_u else DEFAULT_RACK_U,
            "unit_position": "top" if self.unit_top else "bottom",
        }
        return self.topology


def _rear(end):
    return bool(end.get("rearPort") or end.get("readPort"))


def _rack_units(record):
    for k in ("unitCount", "numberOfUnits", "rackUnits", "uHeight", "units"):
        v = record.get(k)
        if isinstance(v, int) and 1 <= v <= 60:
            return v
    return DEFAULT_RACK_U


def _port_sort(pid):
    dev, _, idx = pid.rpartition(":")
    return dev, int(idx) if idx.isdigit() else 0


def _safe(aid):
    return re.sub(r"[^\w.-]", "_", str(aid))


def summary(topology, notes):
    racks = topology["racks"].values()
    edges = topology["edges"]
    lines = [
        f"RACKS  : {sum(1 for r in racks if not r.get('external'))} "
        f"(+{sum(1 for r in racks if r.get('external'))} outside the copy), "
        f"{sum(1 for r in racks if r.get('row'))} in a row",
        f"DEVICES: {len(topology['devices'])} — " + ", ".join(
            f"{k}={v}" for k, v in Counter(d["type"] for d in topology["devices"].values())
            .most_common(6)),
        f"PORTS  : {len(topology['ports'])} — " + ", ".join(
            f"{k}={v}" for k, v in Counter(p["type"] for p in topology["ports"].values())
            .most_common()),
        f"TRUNKS : {len(edges)} rack pairs, "
        + ", ".join(f"{m}={sum(e['cable_types'][m]['capacity'] for e in edges if m in e['cable_types'])} strands"
                    for m in ("fiber", "copper")),
        f"CIRCUIT: {len(topology['circuits'])} existing, "
        f"{sum(len(e['cable_types'][m]['strands']) for e in edges for m in e['cable_types'])} strands in use",
    ]
    no_u = sum(1 for d in topology["devices"].values()
               if d["u_start"] is None and not d.get("external"))
    if no_u:
        notes = Counter(notes)
        notes["devices with no U position"] += no_u
    for k, v in sorted(notes.items()):
        lines.append(f"NOTE   : {k}: {v}")
    return lines


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[3])
    ap.add_argument("--cache", required=True, help="one site's folder under ita_cache/")
    ap.add_argument("--out", help="where to write the map (default: <cache>/topology.json)")
    ap.add_argument("--unit-top", action="store_true",
                    help="ITA's unitPosition is the device's top U, not its bottom")
    args = ap.parse_args()
    conv = Converter(args.cache, unit_top=args.unit_top)
    topology = conv.convert()
    out = Path(args.out) if args.out else Path(args.cache) / "topology.json"
    out.write_text(json.dumps(topology, ensure_ascii=False), encoding="utf-8")
    for line in summary(topology, conv.notes):
        print(line)
    print(f"\nWritten next to the copy ({out.name}). It stays on this machine.")


if __name__ == "__main__":
    sys.exit(main())
