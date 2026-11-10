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
# "rack", but not "rack-mount-pdu": a PDU mounted in a rack is a device
# (the first real run, 2026-11-09, counted two PDUs as racks)
RACK = re.compile(r"rack(?![-_ ]?mount)", re.I)
ROW = re.compile(r"^row$", re.I)
ROOM = re.compile(r"room", re.I)
CONTAINER = re.compile(r"location|site|building|floor|room|row|rack(?![-_ ]?mount)|cage|zone"
                       r"|folder|unsupported", re.I)
DEFAULT_RACK_U = 42
NOWHERE = "(outside the copy)"     # the stub rack of a device whose path names no rack


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
        # How ITA models the fiber plant, in connector types and counts only.
        # In the field the trunks are MPO into fan-out cassettes (2026-11-05);
        # whether ITA records that as one MPO link or as one link per fiber is
        # not known yet, and the two need different handling.
        self.shape = defaultdict(Counter)
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
        self.missing = {str(a) for a in missing}
        # for diagnose(): what the map dropped or guessed, kept out of the notes
        self.diag = Counter()
        self.dropped = set()          # (port pair, cables) of a second path between two ports
        self.topology = {"meta": {}, "pods": {}, "racks": {}, "devices": {},
                         "ports": {}, "edges": [], "circuits": {}}
        self.rack_of_asset = {}       # ITA rack uuid -> map rack id
        self.device_of_asset = {}     # ITA device uuid -> map device id
        self.modules = {}             # map device id -> its cassettes, in port order
        self.ends = {}                # ITA device uuid -> {(cassette, connector): numbers}
        self.outside = OrderedDict()  # ITA uuid of a device outside the copy -> one end of it
        self.path_racks = Counter()   # do paths name a copied device's rack by its tree id?
        self.stub_rack = {}           # outside device -> (map rack id, ITA rack id, by name only)
        self.device_id = {}           # ITA device uuid -> map id, where the name is shared

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
        named = defaultdict(list)
        for aid in racks:
            named[self._name(aid)].append(aid)
        position = Counter()
        for aid in racks:
            name = self._name(aid)
            rid = name if len(named[name]) == 1 else _apart(name, named[name])[aid]
            if len(named[name]) > 1:
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

    # -- every connection end, read before any port is made -------------------
    def _every_path(self):
        """(connections, networkPath) for every entry in every answer copied."""
        for body in self.network.values():
            for pc in body.get("portConfigurations") or []:
                for cp in pc.get("connectionsAndPaths") or []:
                    np = cp.get("networkPath") or {}
                    yield [c for c in [cp.get("connection")] + list(np.get("connections") or [])
                           if c], np

    def _every_end(self):
        """Each END the copy names: every one collect() and the two builders
        turn into a port (and the ends of a cable that is on no path, which
        get none)."""
        for conns, np in self._every_path():
            for c in conns:
                yield c.get("source") or {}
                yield c.get("destination") or {}
            if np.get("connections") and np.get("beginItem") and np.get("endItem"):
                yield np["beginItem"]
                yield np["endItem"]

    def gather_ends(self):
        """Which port numbers the copy names on each cassette of each device.
        Read first, so a cassette the device's own answer did not list —
        always so for a device outside the copy, or one ITA gave no answer
        for — gets a range of its own, sized once. Sized as the ends came,
        cassette B of such a panel landed on cassette A's ports, and one
        range could run into the next (code review, 2026-11-10)."""
        for end in self._every_end():
            aid = str(end.get("assetId"))
            self.ends.setdefault(aid, OrderedDict()).setdefault(
                (_module(end), end.get("connectorType")), set()).add(
                int(end.get("portNumber") or 0))
            rack, home = _path_rack(end), self._home(aid)
            if home is None:
                # a device outside the copy: keep the end whose path names its rack
                if aid not in self.outside or (rack and not _path_rack(self.outside[aid])):
                    self.outside[aid] = end
            elif rack and rack.get("id") is not None:
                self.path_racks["same" if str(rack["id"]) == home else "other"] += 1

    def _home(self, aid):
        """The copied rack (its ITA id) a copied device stands in; None for
        anything build_devices() does not add."""
        t = self.tree.get(aid)
        if t is None or CONTAINER.search(t["type"]):
            return None
        return self._ancestor(aid, RACK)

    def plan_names(self):
        """Names repeat on the real site — racks across rooms, devices within
        a rack — and the id ITA gives is what tells two of them apart. Decided
        here, before any device is made, so a map id never depends on the
        order things were read in. (First real runs: 8 devices of one name in
        a rack became ...#2#2#2 and were counted as 28, and a rack outside the
        copy joined whichever rack had its name.)

        A device outside the copy goes in the rack its path names. That is a
        copied rack only when the path gives a copied rack's id. Where nothing
        in the copy shows that paths name racks by the tree's ids, the name is
        all there is: the one copied rack of that name takes the device, and
        that is noted."""
        proven = self.path_racks["same"] > 0 and not self.path_racks["other"]
        copied = defaultdict(list)                  # rack name -> the copied racks of that name
        for aid in self.rack_of_asset:
            copied[self._name(aid)].append(aid)
        homes, outside = {}, defaultdict(set)       # rack name -> the outside racks of that name
        for aid, end in self.outside.items():
            rack = _path_rack(end)
            name = str(rack.get("name")).strip() if rack else NOWHERE
            pid = str(rack["id"]) if rack and rack.get("id") is not None else None
            if pid in self.rack_of_asset:
                self.stub_rack[aid] = (self.rack_of_asset[pid], pid, False)
            elif rack and not proven and len(copied.get(name, ())) == 1:
                self.stub_rack[aid] = (self.rack_of_asset[copied[name][0]], pid, True)
            else:
                homes[aid] = (name, pid)
                outside[name].add(pid)
        for aid, (name, pid) in homes.items():
            alone = len(outside[name]) == 1 and not copied.get(name)
            ids = {i for i in outside[name] if i} | set(copied.get(name, ()))
            # a rack whose path gives no id still gets a name of its own: the
            # bare name may be a copied rack's, and the device is not in it
            self.stub_rack[aid] = (name if alone else
                                   _apart(name, ids)[pid] if pid else f"{name}#noid",
                                   pid, False)
        twins = defaultdict(list)                   # (rack, name) -> the devices of that name there
        for aid in self.tree:
            if self._home(aid):
                twins[(self.rack_of_asset[self._home(aid)], self._name(aid))].append(aid)
        for aid, end in self.outside.items():
            twins[(self.stub_rack[aid][0], str(end.get("assetName") or aid).strip())].append(aid)
        for (rack, name), aids in twins.items():
            if len(aids) > 1:
                for aid, label in _apart(name, aids).items():
                    self.device_id[aid] = f"{rack}:{label}"

    def _unlisted(self, aid, mods, top):
        """The cassettes the copy's connections name on a device that its own
        answer did not list, each with a range past every listed port — never
        the bare port number, which is cassette 1's port of that number. Laid
        out in name order; numbered from 1, or from 0 where a port 0 is seen."""
        seen = {}
        for (module, connector), numbers in (self.ends.get(aid) or {}).items():
            if _listed(mods, module, connector)[0] is None:
                got = seen.setdefault(module or f"?{connector}",
                                      {"connector": connector, "numbers": set()})
                got["numbers"] |= numbers
        extras = []
        for key in sorted(seen, key=_natural):
            numbers, connector = seen[key]["numbers"], seen[key]["connector"]
            start = min(1, min(numbers))
            count = max(numbers) - start + 1
            extras.append({"keys": {key}, "offset": top, "count": count, "start": start,
                           "media": media_of(connector), "connector": connector,
                           "extra": True})
            top += count
        return extras

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
        did = self.device_id.get(aid) or f"{rack}:{name}"
        if aid in self.device_id:
            self.notes["devices sharing a name in one rack"] += 1
        while did in self.topology["devices"]:      # plan_names() saw them all: not reached
            did += "#2"
            self.notes["devices sharing a name in one rack"] += 1
        mods, offset, counts = [], 0, Counter()
        for pc in (net or {}).get("portConfigurations") or []:
            conf = pc.get("portConfiguration") or {}
            n = int(conf.get("portCount") or 0)
            media = media_of(conf.get("connectorType"))
            keys = {str(k) for k in (conf.get("id"), conf.get("name")) if k is not None}
            mods.append({"keys": keys, "offset": offset, "count": n,
                         "id": str(conf["id"]) if conf.get("id") is not None else None,
                         "conf": conf,
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
        elif aid in self.missing:
            # ITA gave no port list (404/501 on the first real runs): its port
            # count is unknown, not 0 — the screen shows "?", and since every
            # picker walks a device's ports by its counts, none is ever offered
            dev["no_port_data"] = True
        if dev["type"].endswith("patch_panel"):
            for m in mods:
                self.shape["panel ports by connector"][_kind(m["connector"])] += m["count"]
        self.topology["devices"][did] = dev
        self.device_of_asset[aid] = did
        for m in mods:
            for i in range(1, m["count"] + 1):
                self._add_port(dev, m["offset"] + i, m["media"], m["connector"])
        # cassettes only connections name: a port of theirs exists once named
        extras = self._unlisted(aid, mods, offset)
        if mods and extras and not external:
            self.notes["connection ends on an unknown cassette"] += len(extras)
        self.modules[did] = mods + extras
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
        m, how = self._module_for(did, end)
        if m is None:
            # gather_ends() saw every end, so this is not reached; an end from
            # anywhere else still gets a range of its own, past every known port
            mods = self.modules.setdefault(did, [])
            m = {"keys": {_module(end) or f"?{end.get('connectorType')}"},
                 "offset": max([x["offset"] + x["count"] for x in mods] + [0]),
                 "count": number - min(1, number) + 1, "start": min(1, number),
                 "media": media_of(end.get("connectorType")),
                 "connector": end.get("connectorType"), "extra": True}
            mods.append(m)
        index = m["offset"] + number - m["start"] + 1
        return self._add_port(dev, index, m["media"], m.get("connector"))

    def _where(self, end):
        """(map device, port index) an end names, or None — making nothing."""
        did = self.device_of_asset.get(str(end.get("assetId")))
        m = self._module_for(did, end)[0] if did else None
        if m is None:
            return None
        return did, m["offset"] + int(end.get("portNumber") or 0) - m["start"] + 1

    def _module_for(self, did, end):
        """The cassette of DID an end names, and how it was found: "id" or
        "name" (networkModule is that cassette's id or name), "media" (the
        only listed cassette with that connector), "single" (the device lists
        one), "extra" (a cassette only connections name), or None. --explain
        counts these: a guess is where a shift would start."""
        mods = self.modules.get(did) or []
        m, how = _listed([x for x in mods if not x.get("extra")], _module(end),
                         end.get("connectorType"))
        if m is None:
            key = _module(end) or f"?{end.get('connectorType')}"
            m = next((x for x in mods if x.get("extra") and key in x["keys"]), None)
            how = "extra" if m else None
        return m, how

    def _stub(self, end):
        """A device outside the copy, in the rack plan_names() found for it:
        a copied rack, or a stub rack named from the path ITA put on the end."""
        aid = str(end.get("assetId"))
        end = self.outside.get(aid, end)
        rid, uuid, by_name = self.stub_rack.get(aid) or (NOWHERE, None, False)
        if rid not in self.topology["racks"]:
            self.topology["racks"][rid] = {
                "id": rid, "uuid": uuid, "pod": None,
                "row": None, "room": None, "position": 0, "is_eor": False,
                "is_mda": False, "u_height": DEFAULT_RACK_U, "domain": None,
                "external": True}
            self.notes["racks outside the copy (stubs)"] += 1
        if by_name:
            self.notes["outside devices put in a copied rack by its name"] += 1
        self.notes["devices outside the copy (stubs)"] += 1
        return self._add_device(aid, rid,
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
                        if c and c.get("id") is not None and str(c["id"]) not in connections:
                            connections[str(c["id"])] = c
                            cable = c.get("cableType") or {}
                            if cable.get("isFanOut"):
                                self.shape["fan-out cables by outlets"][
                                    str(cable.get("fanOutOutletsCount"))] += 1
                    if np.get("connections") and np.get("beginItem") and np.get("endItem"):
                        key = tuple(sorted((self.port_of(np["beginItem"]),
                                            self.port_of(np["endItem"]))))
                        if key in paths and _cables(paths[key]) != _cables(np):
                            self.dropped.add((key, _cables(np)))    # only the first is kept
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
            ends = "-".join(sorted((_kind(a.get("connectorType")), _kind(b.get("connectorType")))))
            self.shape["trunk links by connector"][ends] += 1
            if MULTI.search(ends):
                # one ITA link that may carry 8/12/24 fibers: taken as ONE strand
                # until the real model is seen — said loudly, never silently
                self.notes["MPO/MTP trunk links, each counted as ONE strand for now"] += 1
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
            # a path ending on a panel's REAR marks that panel port taken,
            # though ITA takes a panel port only when its front is connected
            self.diag["endRear"] += sum(1 for x in (np["beginItem"], np["endItem"]) if _rear(x))
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
        self.gather_ends()
        self.plan_names()
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


MULTI = re.compile(r"MPO|MTP", re.I)


def _kind(connector):
    """A connector type as a short generic word for the SHAPE lines — LC, MPO,
    RJ45 — so nothing a site typed in free text reaches the screen."""
    c = str(connector or "")
    for word in ("MPO", "MTP", "LC", "SC", "ST", "FC", "E2000", "MU", "QSFP", "SFP", "RJ45"):
        if re.search(word.replace("RJ45", r"RJ-?45"), c, re.I):
            return word
    return "other" if c else "none"


def _rear(end):
    return bool(end.get("rearPort") or end.get("readPort"))


def _cables(np):
    return frozenset(str(c.get("id")) for c in np.get("connections") or [] if c)


def _path_rack(end):
    """The rack on the path ITA put on a connection end, or None."""
    return next((p for p in end.get("assetPath") or []
                 if isinstance(p, dict) and RACK.search(str(p.get("elementType", "")))), None)


def _apart(name, ids):
    """{id: NAME#<the id's first six characters>} — the whole id where those
    repeat — so things that share a name stay apart."""
    short = Counter(str(i)[:6] for i in ids)
    return {i: f"{name}#{i if short[str(i)[:6]] > 1 else str(i)[:6]}" for i in ids}


def _module(end):
    """The cassette an end names, as text; None when it names none."""
    return str(end.get("networkModule")) if end.get("networkModule") is not None else None


def _listed(mods, module, connector):
    """Which of MODS — the cassettes a device's own answer listed — an end
    names, and how: by "id" or "name", else the only one with that connector
    ("media"), else the only one ("single"). (None, None): none of them."""
    for m in mods:
        if module is not None and module in m["keys"]:
            return m, "id" if module == m.get("id") else "name"
    same = [m for m in mods if media_of(m["connector"]) == media_of(connector)]
    if len(same) == 1:
        return same[0], "media"
    if len(mods) == 1:
        return mods[0], "single"
    return None, None


def _natural(text):
    """Sort key: A2 before A10, B after A."""
    return [(0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"(\d+)", str(text)) if p]


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


def summary(topology, notes, shape=None):
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
    notes = _all_notes(topology, notes)
    for k, v in sorted(notes.items()):
        lines.append(f"NOTE   : {k}: {v}")
    for k in ("panel ports by connector", "trunk links by connector",
              "fan-out cables by outlets"):
        got = (shape or {}).get(k)
        lines.append(f"SHAPE  : {k}: " + (", ".join(f"{t}={n}" for t, n in got.most_common(8))
                                           if got else "none"))
    return lines


# -- --explain-u: one device, cassette by cassette ---------------------------
# The first real run (2026-11-09) showed the right port COUNT on a panel but
# the wrong ports taken, shifted, and one extra fiber on two panels. Before
# any fix, this shows what ITA said and what the map made of it, side by
# side, in port numbers only. Nothing here changes the map.

SM = re.compile(r"\b(OS ?[12]|SMF?|single[- ]?mode|9/125)\b", re.I)
MM = re.compile(r"\b(OM ?[1-5]|MMF?|multi[- ]?mode|50/125|62\.5/125)\b", re.I)
# a cassette name short enough to be a label (A, B, 1, M2) and nothing more
LABEL = re.compile(r"^[A-Za-z]{0,3}\d{0,3}$")


def _ranges(nums):
    """[1,2,3,5,7,8] -> '1-3,5,7-8'; '-' for none."""
    nums, out = sorted(set(nums)), []
    for n in nums:
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return ",".join(f"{a}" if a == b else f"{a}-{b}" for a, b in out) or "-"


def _names(dicts, depth=1):
    """Field names over several records, # = a number. Names only."""
    seen = set()

    def walk(d, prefix, lvl):
        for k, v in d.items():
            if isinstance(v, dict) and lvl < depth:
                walk(v, f"{prefix}{k}.", lvl + 1)
            else:
                seen.add(f"{prefix}{k}" + ("#" if isinstance(v, (int, float))
                                           and not isinstance(v, bool) else ""))
    for d in dicts:
        if isinstance(d, dict):
            walk(d, "", 0)
    return ",".join(sorted(seen)) or "none"


def _modes(where):
    """Where single/multi mode is written, as (field, SM count, MM count) —
    the field's name and a count, never the text."""
    hits = Counter()

    def walk(d, prefix):
        for k, v in d.items():
            if isinstance(v, dict):
                walk(v, f"{prefix}{k}.")
            elif isinstance(v, str):
                if SM.search(v):
                    hits[(prefix + k, "SM")] += 1
                if MM.search(v):
                    hits[(prefix + k, "MM")] += 1
    for prefix, d in where:
        if isinstance(d, dict):
            walk(d, prefix)
    return " ".join(f"{f}:{m}x{n}" for (f, m), n in sorted(hits.items())) or "none found"


def _in_rack(rack_id, name):
    """Does map rack RACK_ID stand for the rack the user NAMED? Two racks
    sharing a name get '#<uuid start>' in the map; the name matches both."""
    return name is None or rack_id == name or rack_id.startswith(name + "#")


def explain(conv, u, rack=None):
    """Lines for every copied device that covers rack unit U — in the rack
    named RACK only, when the copy holds more than one. The name is taken
    in, never printed."""
    topo = conv.topology
    lines = []
    if rack is not None and not any(_in_rack(r, rack) for r, v in topo["racks"].items()
                                    if not v.get("external")):
        return ["E0 no copied rack has that name (check the spelling, as in ITA)"]
    for aid, did in conv.device_of_asset.items():
        dev = topo["devices"][did]
        if dev.get("external") or dev["u_start"] is None or not _in_rack(dev["rack"], rack):
            continue
        if not dev["u_start"] - dev["u_size"] + 1 <= u <= dev["u_start"]:
            continue
        mods = [m for m in conv.modules.get(did, []) if not m.get("extra")]
        extra = [m for m in conv.modules.get(did, []) if m.get("extra")]
        # every end that names this device, from every answer in the copy
        ends, cables = [], []
        for body in conv.network.values():
            for pc in body.get("portConfigurations") or []:
                for cp in pc.get("connectionsAndPaths") or []:
                    np = cp.get("networkPath") or {}
                    for c in [cp.get("connection")] + list(np.get("connections") or []):
                        if not c:
                            continue
                        for e in (c.get("source") or {}, c.get("destination") or {}):
                            if str(e.get("assetId")) == aid:
                                ends.append((str(c.get("id")), e))
                                cables.append(c.get("cableType") or {})
        once = {}
        for cid, e in ends:                 # one connection is listed many times
            once[(cid, bool(_rear(e)), str(e.get("networkModule")), e.get("portNumber"))] = e
        how = Counter(conv._module_for(did, e)[1] or "none" for e in once.values())
        ports = [p for p in topo["ports"].values() if p["device"] == did]
        used = [p for p in ports if p["status"] == "used"]
        counted = [m["conf"]["portsUsed"] for m in mods
                   if isinstance(m["conf"].get("portsUsed"), (int, float))]
        conf_used = int(sum(counted)) if counted else "?"
        has_net = _safe(aid) in conv.network
        lines.append(f"E0 U{u} type={conv.tree[aid]['type']} net={'yes' if has_net else 'no'} "
                     f"cassettes={len(mods)} ports={len(ports)} ita_used={conf_used} "
                     f"map_used={len(used)} extra={sum(m['count'] for m in extra)}")
        # the cassette labels (A, B, 1) the ends name — short labels only
        named = sorted({_module(e) for e in once.values()
                        if _module(e) and LABEL.match(_module(e))}, key=_natural)
        lines.append(f"E1 match: " + " ".join(f"{k}={v}" for k, v in sorted(how.items()))
                     + (f" named={','.join(named)}" if named else ""))
        lines.append("E2 cassette fields: " + _names([m["conf"] for m in mods]))
        lines.append("E3 cable fields: " + _names(cables))
        lines.append("E4 SM/MM: " + _modes(
            [("record.", conv.assets.get(aid))]
            + [("cassette.", m["conf"]) for m in mods]
            + [("end.", e) for e in once.values()]
            + [("cable.", c) for c in cables]))
        for i, m in enumerate(mods + extra, 1):
            name = str(m["conf"].get("name") if m.get("conf") else "")
            label = name if name and LABEL.match(name) else f"M{i}"
            front, rear = [], []
            for e in once.values():
                if conv._module_for(did, e)[0] is m:
                    (rear if _rear(e) else front).append(int(e.get("portNumber") or 0))
            mine = [p["index"] - m["offset"] + m["start"] - 1 for p in used
                    if m["offset"] < p["index"] <= m["offset"] + m["count"]]
            conf = m.get("conf") or {}
            lines.append(
                f"{'X' if m.get('extra') else 'K'}{i} {label} start={m['start']} n={m['count']} "
                f"{_kind(m['connector'])} used={conf.get('portsUsed', '?')} "
                f"| front {_ranges(front)} | rear {_ranges(rear)} | map {_ranges(mine)}")
    if not lines:
        lines.append(f"E0 U{u}: no copied device covers this U "
                     "(devices with no U position are not listed)")
    # the rack's own numbers: which of them is its height in U is not known yet
    for r in topo["racks"].values():
        if not r.get("external") and _in_rack(r["id"], rack):
            rec = conv.assets.get(r.get("uuid"), {})
            nums = {k: v for k, v in rec.items()
                    if isinstance(v, (int, float)) and not isinstance(v, bool)
                    and re.search(r"height|unit", k, re.I)}
            lines.append("R0 rack numbers: " + (" ".join(f"{k}={v}" for k, v in sorted(nums.items()))
                                                or "none"))
    return lines


def _all_notes(topology, notes):
    notes = Counter(notes)
    no_u = sum(1 for d in topology["devices"].values()
               if d["u_start"] is None and not d.get("external"))
    if no_u:
        notes["devices with no U position"] += no_u
    return notes


# a short word for each NOTE, so the lines typed back stay short
SHORT = (("THE COPY IS INCOMPLETE", "INCOMPLETE"),
         ("devices whose ports/connections were not copied", "noNet"),
         ("racks sharing a name", "dupRack"),
         ("devices not in a rack", "noRack"),
         ("devices sharing a name", "dupDev"),
         ("ports with an unknown connector", "unkConn"),
         ("connection ends on an unknown cassette", "unkCassette"),
         ("racks outside the copy", "extRack"),
         ("devices outside the copy", "extDev"),
         ("outside devices put in a copied rack", "nameRack"),
         ("rear-to-rear links inside one rack", "rearRear"),
         ("MPO/MTP trunk links", "mpo"),
         ("paths whose cables do not chain", "noChain"),
         ("direct cables between racks", "direct"),
         ("ports on two different paths", "twoPaths"),
         ("devices with no U position", "noU"))


def _short(note):
    return next((w for start, w in SHORT if note.startswith(start)), note)


def type_back(topology, notes, shape=None):
    """The conversion in four lines, counts and connector words only."""
    racks = topology["racks"].values()
    edges = topology["edges"]
    shape = shape or {}

    def cap(m):
        return sum(e["cable_types"][m]["capacity"] for e in edges if m in e["cable_types"])

    def kinds(k):
        got = shape.get(k)
        return ",".join(f"{t}:{n}" for t, n in got.most_common(6)) if got else "none"
    notes = _all_notes(topology, notes)
    return [
        f"C1 racks={sum(1 for r in racks if not r.get('external'))} "
        f"devices={len(topology['devices'])} ports={len(topology['ports'])} "
        f"circuits={len(topology['circuits'])}",
        f"C2 trunks={len(edges)} fiber={cap('fiber')} copper={cap('copper')} used="
        + str(sum(len(e["cable_types"][m]["strands"]) for e in edges for m in e["cable_types"])),
        f"C3 panel={kinds('panel ports by connector')} trunk={kinds('trunk links by connector')} "
        f"fanout={kinds('fan-out cables by outlets')}",
        "C4 notes: " + (" ".join(f"{_short(k)}={v}" for k, v in sorted(notes.items())) or "none"),
    ]


def _count(counter, key=None, sep=":"):
    """{'a': 2, 'b': 1} -> 'a:2,b:1' (most first), 'none' when empty."""
    items = sorted(counter.items(), key=key or (lambda kv: (-kv[1], str(kv[0]))))
    return ",".join(f"{k}{sep}{n}" for k, n in items if n) or "none"


def _fit(line, width=120):
    return line if len(line) <= width else line[:width - 3] + "..."


def diagnose(conv):
    """Five more lines to type back (C5-C9), for what the first real runs
    left open: 8 racks where there are 7, ports taken in the wrong places
    on one panel, an extra fiber on two, and where single/multi mode is
    written. They say where ITA and the map part ways, so a fix follows the
    evidence. Counts, ITA type words and field names only; makes nothing."""
    topo, tree = conv.topology, conv.tree
    copied = {did: topo["devices"][did] for aid, did in conv.device_of_asset.items()
              if not topo["devices"][did].get("external")}
    panel = lambda did: copied[did]["type"].endswith("patch_panel")       # noqa: E731

    # C5: what was taken as a rack, and the rack's own numbers (which is U?)
    racks = list(conv.rack_of_asset)
    full = {d["rack"] for d in copied.values()}
    nums = defaultdict(Counter)
    for a in racks:
        for k, v in (conv.assets.get(a) or {}).items():
            if (isinstance(v, (int, float)) and not isinstance(v, bool)
                    and re.search(r"height|unit", k, re.I)):
                nums[k][f"{v:g}"] += 1
    c5 = (f"C5 racks types={_count(Counter(tree[a]['type'] for a in racks))} "
          f"empty={sum(1 for a in racks if conv.rack_of_asset[a] not in full)} num="
          + (";".join(f"{k}:" + ",".join(f"{v}x{n}" for v, n in sorted(c.items()))
                      for k, c in sorted(nums.items())) or "none"))

    # C6: devices ITA gave no ports for; devices that stand in another device
    nonet = [a for a, did in conv.device_of_asset.items()
             if topo["devices"][did].get("no_port_data")]
    inside = sum(1 for a, did in conv.device_of_asset.items()
                 if did in copied and copied[did]["u_start"] is None
                 and tree.get(tree.get(a, {}).get("parent") or "", {}).get("type")
                 and not CONTAINER.search(tree[tree[a]["parent"]]["type"]))
    c6 = (f"C6 noNet={len(nonet)} types={_count(Counter(tree[a]['type'] for a in nonet))} "
          f"seen={sum(1 for a in nonet if a in conv.ends)} inDevice={inside}")

    # C7: how each end of a copied device found its cassette
    once = {}
    for end in conv._every_end():
        did = conv.device_of_asset.get(str(end.get("assetId")))
        if did in copied:
            once[(did, _module(end), end.get("connectorType"),
                  int(end.get("portNumber") or 0), _rear(end))] = end
    how, out_range, guessed = Counter(), 0, []
    for (did, *_), end in once.items():
        m, h = conv._module_for(did, end)
        how[h or "none"] += 1
        n = int(end.get("portNumber") or 0)
        if m is not None and not m.get("extra") and not m["start"] <= n < m["start"] + m["count"]:
            out_range += 1                  # lands on the NEXT cassette's ports
        if h in ("media", "single") and LABEL.match(_module(end) or "-"):
            # names cassette A or B, which the device's own list does not have,
            # and was put on its only group: if the panel is A1-A6, B1-B6
            # (the user, 2026-11-10) and ports run 1-6, B5 was taken for 5
            guessed.append(n)
    listed = {did: [m for m in conv.modules.get(did, []) if not m.get("extra")]
              for did in copied}
    shapes = Counter(f"s{m['start']}n{m['count']}" for ms in listed.values() for m in ms)
    # out of order: only cassettes named like labels (A, B, A1, 2), whose
    # order a technician counts in; "SFP", "RJ45" or "fiber" say nothing of it
    names = [n for n in ([str(m["conf"].get("name") or "") for m in ms]
                         for ms in listed.values())
             if len(n) > 1 and all(x and LABEL.match(x) for x in n)]
    order = ("id", "name", "media", "single", "extra", "none")
    # the flags first and the shapes last, and only the four most common:
    # a full site has dozens of shapes, and a cut line lost the flags
    common = Counter(dict(shapes.most_common(4)))
    more = len(shapes) - len(common)
    c7 = (f"C7 match={_count(how, key=lambda kv: order.index(kv[0]))} outRange={out_range} "
          f"order={sum(1 for n in names if n != sorted(n, key=_natural))} "
          f"lblGuess={len(guessed)} lblMax={max(guessed, default=0)} "
          f"cas={_count(common, sep='x')}" + (f"+{more}" if more else ""))

    # C8: ports taken — in ITA, in the map, and on panels by the front-only rule
    counted = [m["conf"]["portsUsed"] for ms in listed.values() for m in ms
               if isinstance(m["conf"].get("portsUsed"), (int, float))
               and not isinstance(m["conf"].get("portsUsed"), bool)]
    used = {p for p, v in topo["ports"].items() if v["status"] == "used"}
    in_listed = sum(1 for did, ms in listed.items() for m in ms
                    for i in range(m["offset"] + 1, m["offset"] + m["count"] + 1)
                    if f"{did}:{i}" in used)
    # a cable in the front: the end of a CONNECTION, never a path's begin or
    # end item — those name the path's ends, and on a bare trunk fiber they
    # are the panel ports at either end with nothing plugged in front
    front = set()
    for conns, _ in conv._every_path():
        for c in conns:
            for end in (c.get("source") or {}, c.get("destination") or {}):
                did = conv.device_of_asset.get(str(end.get("assetId")))
                where = (conv._where(end) if did in copied and panel(did) and not _rear(end)
                         else None)
                if where:
                    front.add(f"{where[0]}:{where[1]}")
    taken = {p for p in used if topo["ports"][p]["device"] in copied
             and panel(topo["ports"][p]["device"])}
    c8 = (f"C8 used ita={int(sum(counted)) if counted else '?'} map={in_listed} "
          f"noFront={len(taken - front)} frontFree={len(front - used)} "
          f"endRear={conv.diag['endRear']} samePair={len(conv.dropped)}")

    # C9: where single/multi mode is written
    cables = {}
    for conns, _ in conv._every_path():
        for c in conns:
            cables[str(c.get("id"))] = c.get("cableType") or {}
    c9 = "C9 SM/MM: " + _modes(
        [("record.", conv.assets.get(a)) for a, did in conv.device_of_asset.items()
         if did in copied]
        + [("cassette.", m["conf"]) for ms in listed.values() for m in ms]
        + [("end.", e) for e in once.values()]
        + [("cable.", c) for c in cables.values()])
    return [_fit(x) for x in (c5, c6, c7, c8, c9)]


def block(lines):
    """The few lines to type back by hand; the full screen above stays."""
    bar = "-" * 44
    return [bar, "TYPE BACK ONLY THESE LINES:"] + list(lines) + [bar]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[3])
    ap.add_argument("--cache", required=True, help="one site's folder under ita_cache/")
    ap.add_argument("--out", help="where to write the map (default: <cache>/topology.json)")
    ap.add_argument("--unit-top", action="store_true",
                    help="ITA's unitPosition is the device's top U, not its bottom")
    ap.add_argument("--explain-u", type=int, metavar="U",
                    help="show one device, cassette by cassette: what ITA said and what the "
                         "map made of it (port numbers only). Writes nothing.")
    ap.add_argument("--explain-rack", metavar="NAME",
                    help="with --explain-u, when the copy holds several racks: the rack's "
                         "exact name, as in ITA")
    args = ap.parse_args()
    conv = Converter(args.cache, unit_top=args.unit_top)
    topology = conv.convert()
    if args.explain_u is not None:
        for line in block(explain(conv, args.explain_u, args.explain_rack)):
            print(line)
        return 0
    out = Path(args.out) if args.out else Path(args.cache) / "topology.json"
    out.write_text(json.dumps(topology, ensure_ascii=False), encoding="utf-8")
    for line in summary(topology, conv.notes, conv.shape):
        print(line)
    print(f"\nWritten next to the copy ({out.name}). It stays on this machine.\n")
    for line in block(type_back(topology, conv.notes, conv.shape) + diagnose(conv)):
        print(line)


if __name__ == "__main__":
    sys.exit(main())
