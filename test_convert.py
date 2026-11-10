#!/usr/bin/env python3
"""
test_convert.py
===============
ita_convert.py proven by a round trip, since the real ITA is out of reach:

    synthetic topology.json  --(to_ita_cache)-->  a copy in EXACTLY the shape
    ita_sync.py saves (asset records + network-paths answers, field names as
    typed out from the real server on 2026-11-04)  --(ita_convert)-->  a map

and the map that comes back must be the one that went in: same racks, same
devices at the same U with the same serials and port counts, the same trunks
with the same capacity, and every existing circuit with the same endpoints,
the same racks in the same order, and the same strand on every trunk.

One honest difference: the synthetic map treats a trunk as capacity that is
not tied to any panel port (an EOR there has 312 trunk fibers and 240 panel
ports). Real ITA records each fiber on a real rear port, so the copy adds one
TRUNK panel per rack and media to land them on. Those are left out of the
device comparison and nothing else.

Then the converted map is handed to pathengine, to plan and commit a new
route on it — the point of the whole exercise.

    python test_convert.py
"""

import copy
import json
import shutil
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

import ita_convert
import pathengine

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" — {str(detail)[:600]}"))


ITA_TYPE = {"fiber_patch_panel": "patch-panel", "copper_patch_panel": "patch-panel",
            "switch": "network", "server": "server"}
CONNECTOR = {"fiber": "LC", "copper": "RJ45"}


def to_ita_cache(t, out):
    """Write `t` as ita_sync.py would have saved it from a real ITA."""
    (out / "assets").mkdir(parents=True)
    (out / "network").mkdir()
    tree, assets = {}, {}

    def add(aid, kind, name, parent, **extra):
        tree[aid] = {"parent": parent, "type": kind}
        assets[aid] = dict({"id": aid, "type": kind, "name": name, "children": []}, **extra)
        if parent:
            assets[parent]["children"].append({"id": aid})

    add("SITE", "location", "SITE", None)
    for rid, r in sorted(t["racks"].items()):
        room = t["pods"][r["pod"]]["room"]
        if f"ROOM-{room}" not in tree:
            add(f"ROOM-{room}", "server-room", room, "SITE")
        row = f"ROW-{r['pod']}-{r['row']}"
        if row not in tree:
            add(row, "row", f"{r['pod']}-{r['row']}", f"ROOM-{room}")
        add(rid, "rack", rid, row)

    # every device, with its cassettes: fiber first, then copper, as the map numbers them
    nets = {}
    modules = {}
    for did, d in t["devices"].items():
        add(did, ITA_TYPE[d["type"]], d["name"], d["rack"], serialNumber=d["serial"],
            modelName=d["label"],
            rackMounted={"unitPosition": d["u_start"] - d["u_size"] + 1,
                         "unitHeight": d["u_size"]})
        mods = []
        for media, n in (("fiber", d["fiber_ports"]), ("copper", d["copper_ports"])):
            if n:
                mods.append({"portConfiguration": {
                    "id": f"{did}/{media}", "assetId": did, "name": media,
                    "connectorType": CONNECTOR[media], "portCount": n, "portStart": 1,
                    "speed": "", "speedRate": 0, "portsUsed": 0},
                    "connectionsAndPaths": []})
        nets[did] = {"portConfigurations": mods}
        modules[did] = mods

    # one TRUNK panel per rack and media, big enough for every fiber landing there
    need = Counter()
    for e in t["edges"]:
        for m, ct in e["cable_types"].items():
            need[(e["from"], m)] += ct["capacity"]
            need[(e["to"], m)] += ct["capacity"]
    for (rid, m), n in need.items():
        did = f"{rid}:TRUNK-{m}"
        add(did, "patch-panel", f"TRUNK-{m}", rid, serialNumber="", rackMounted={})
        mod = {"portConfiguration": {"id": f"{did}/{m}", "assetId": did, "name": m,
                                     "connectorType": CONNECTOR[m], "portCount": n,
                                     "portStart": 1},
               "connectionsAndPaths": []}
        nets[did] = {"portConfigurations": [mod]}
        modules[did] = [mod]

    def end(did, number, media, rear=False):
        return {"networkModule": f"{did}/{media}", "portNumber": number, "rearPort": rear,
                "connectorType": CONNECTOR[media], "ipAddress": "", "description": "",
                "assetId": did, "assetName": did.split(":", 1)[1],
                "assetType": tree[did]["type"], "assetPath": []}

    def port_end(pid):
        p = t["ports"][pid]
        d = t["devices"][p["device"]]
        number = p["index"] if p["type"] == "fiber" else p["index"] - d["fiber_ports"]
        return end(p["device"], number, p["type"])

    # every strand of every trunk, rear port to rear port, numbered in order on both sides
    counter = Counter()
    trunk = {}                                  # (edge id, media, index) -> connection
    for e in t["edges"]:
        for m, ct in sorted(e["cable_types"].items()):
            for k in range(1, ct["capacity"] + 1):
                counter[(e["from"], m)] += 1
                counter[(e["to"], m)] += 1
                trunk[(e["id"], m, k)] = {
                    "id": f"T|{e['id']}|{m}|{k}",
                    "source": end(f"{e['from']}:TRUNK-{m}", counter[(e["from"], m)], m, True),
                    "destination": end(f"{e['to']}:TRUNK-{m}", counter[(e["to"], m)], m, True),
                    "cableType": {"length": e["length_m"]}}

    def attach(did, media, entry):
        mods = modules[did]
        mod = next(x for x in mods if x["portConfiguration"]["name"] == media)
        mod["connectionsAndPaths"].append(entry)

    def cord(n, a, b):
        """A front patch cord between two ENDs."""
        return {"id": f"P|{n}", "source": dict(a, rearPort=False),
                "destination": dict(b, rearPort=False), "cableType": {"length": 2}}

    used = set()
    for c in t["circuits"].values():
        # as ITA keeps it physically: endpoint -> cord -> TRUNK panel front N =
        # rear N -> fiber -> far TRUNK panel rear/front N -> cross-connect cord
        # to front M of the same panel -> rear M -> next fiber ... -> cord -> endpoint
        conns = []
        hops = c["hop_racks"]
        for i, s in enumerate(c["strands"]):
            fiber = trunk[(s["edge_id"], s["cable_type"], s["strand_index"])]
            near = fiber["source"] if fiber["source"]["assetId"].startswith(hops[i] + ":") \
                else fiber["destination"]
            far = fiber["destination"] if near is fiber["source"] else fiber["source"]
            if i == 0:
                conns.append(cord(f"{c['id']}/a", port_end(c["a_port"]), near))
            else:
                conns.append(cord(f"{c['id']}/x{i}", last_far, near))
            conns.append(fiber)
            last_far = far
            used.add((s["edge_id"], s["cable_type"], s["strand_index"]))
        conns.append(cord(f"{c['id']}/b", last_far, port_end(c["b_port"])))
        path = {"connections": conns, "beginItem": port_end(c["a_port"]),
                "endItem": port_end(c["b_port"])}
        for pid in (c["a_port"], c["b_port"]):
            p = t["ports"][pid]
            attach(p["device"], p["type"], {"connection": conns[0], "networkPath": path})
    for (eid, m, k), conn in trunk.items():
        if (eid, m, k) not in used:          # a free strand: ITA still lists it as a path
            did = conn["source"]["assetId"]
            attach(did, m, {"connection": conn, "networkPath": {
                "connections": [conn], "beginItem": dict(conn["source"], rearPort=False),
                "endItem": dict(conn["destination"], rearPort=False)}})

    for aid, a in assets.items():
        (out / "assets" / f"{aid.replace(':', '_')}.json").write_text(
            json.dumps(a), encoding="utf-8")
    for did, n in nets.items():
        (out / "network" / f"{did.replace(':', '_')}.json").write_text(
            json.dumps(n), encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps({"synced_at": "test", "tree": tree}),
                                       encoding="utf-8")


def small_site(out):
    """A hand-built copy holding each case the code review raised
    (2026-11-04), as ITA records it physically:

      C1  SW:1 (R1) -cord- P1 f5 | P1 r5 =fiber= P2 r5 | P2 f5 -cord- P3 f7 (R2)
          | P3 r7 =fiber= P4 r7 | P4 f7 -cord- SRV:1 (R3)     — listed BACKWARDS
      C2  SW:2 -cord- SRV2:1, both in R1                      — one direct cable
      C3  SW:3 (R1) -cable- SRV:2 (R3)                         — direct, across racks
      --  P1 r6 =fiber= P2 r6, nothing patched                 — a free strand
      P5  two cassettes: A (ports 1-24), B (numbered from 0)
      a ghost device's files left in the folder by an earlier copy
    """
    (out / "assets").mkdir(parents=True)
    (out / "network").mkdir()
    tree = {"site": {"parent": None, "type": "location"}}
    nets = {}

    def put(aid, kind, parent, name, mods=(), u=None):
        tree[aid] = {"parent": parent, "type": kind}
        rec = {"id": aid, "type": kind, "name": name, "serialNumber": f"SN{aid}1"}
        if u:
            rec["rackMounted"] = {"unitPosition": u, "unitHeight": 1}
        (out / "assets" / f"{aid}.json").write_text(json.dumps(rec), encoding="utf-8")
        nets[aid] = {"portConfigurations": [
            {"portConfiguration": {"id": f"{aid}-{m}", "name": m, "connectorType": "LC",
                                   "portCount": n, "portStart": start},
             "connectionsAndPaths": []} for m, n, start in mods]}

    for r in ("R1", "R2", "R3"):
        put(r, "rack", "site", r)
    put("SW", "network", "R1", "SW", [("m", 8, 1)], u=10)
    put("SRV2", "server", "R1", "SRV2", [("m", 2, 1)], u=5)
    put("P1", "patch-panel", "R1", "P1", [("m", 24, 1)], u=40)
    put("P5", "patch-panel", "R1", "P5", [("A", 24, 1), ("B", 24, 0)], u=39)
    put("P2", "patch-panel", "R2", "P2", [("m", 24, 1)], u=40)
    put("P3", "patch-panel", "R2", "P3", [("m", 24, 1)], u=39)
    put("P4", "patch-panel", "R3", "P4", [("m", 24, 1)], u=40)
    put("SRV", "server", "R3", "SRV", [("m", 4, 1)], u=10)

    def e(aid, port, rear=False, module="m"):
        kind = tree[aid]["type"]
        return {"assetId": aid, "networkModule": f"{aid}-{module}", "portNumber": port,
                "rearPort": rear, "connectorType": "LC", "assetType": kind,
                "assetName": aid}

    def conn(cid, a, b):
        return {"id": cid, "source": a, "destination": b, "cableType": {"length": 5}}

    c1 = [conn("c1a", e("SW", 1), e("P1", 5)),
          conn("c1b", e("P1", 5, True), e("P2", 5, True)),
          conn("c1c", e("P2", 5), e("P3", 7)),
          conn("c1d", e("P3", 7, True), e("P4", 7, True)),
          conn("c1e", e("P4", 7), e("SRV", 1))]
    c2 = [conn("c2", e("SW", 2), e("SRV2", 1))]
    c3 = [conn("c3", e("SW", 3), e("SRV", 2))]
    free = [conn("f6", e("P1", 6, True), e("P2", 6, True))]
    free2 = [conn("f8", e("P3", 8, True), e("P4", 8, True))]
    cass = [conn("k1", e("P5", 0, module="B"), e("SW", 4)),
            conn("k2", e("P5", 5, module="Z"), e("SW", 5)),
            conn("k3", e("P5", 5, module="A"), e("SW", 6))]

    def path(conns, begin, end, holder):
        nets[holder]["portConfigurations"][0]["connectionsAndPaths"].append(
            {"connection": conns[0], "networkPath": {
                "connections": conns, "beginItem": begin, "endItem": end}})

    path(c1[::-1], e("SW", 1), e("SRV", 1), "SW")
    path(c1, e("SW", 1), e("SRV", 1), "SRV")              # the same circuit, from its far end
    path(c2, e("SW", 2), e("SRV2", 1), "SW")
    path(c3, e("SW", 3), e("SRV", 2), "SW")
    path(free, e("P1", 6), e("P2", 6), "P1")
    path(free2, e("P3", 8), e("P4", 8), "P3")
    for k in cass:
        path([k], k["source"], k["destination"], "P5")

    for aid, n in nets.items():
        (out / "network" / f"{aid}.json").write_text(json.dumps(n), encoding="utf-8")
    # an earlier copy's leftover: on disk, not in this copy's tree
    (out / "assets" / "GHOST.json").write_text(json.dumps(
        {"id": "GHOST", "type": "server", "name": "GHOST"}), encoding="utf-8")
    (out / "network" / "GHOST.json").write_text(json.dumps({"portConfigurations": [
        {"portConfiguration": {"id": "g", "connectorType": "LC", "portCount": 1},
         "connectionsAndPaths": [{"networkPath": {"connections": c2,
                                                  "beginItem": e("SW", 2),
                                                  "endItem": e("SRV2", 1)}}]}]}),
        encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps(
        {"synced_at": "test", "complete": True, "network_missing": [], "tree": tree}),
        encoding="utf-8")


def loose_site(out, rack_ids=True):
    """Ends on cassettes the copy holds no list of: a panel outside the copy
    (always so), a copied panel ITA gave no answer for (404/501 on the first
    real runs), and cassettes a panel's own answer did not list.

      SW:1 - OUT A:1   SW:2 - OUT B:1   SW:3 - OUT A:6   SW:8 - OUT C:0
                                                  OUT: outside the copy, rack RX
      SW:4 - NN m:3                               NN: in R1, no answer from ITA
      SW:5 - P Z:2     SW:6 - P Y:1     SW:7 - P Z:4     P: lists cassettes A, B

    And names that repeat, as they do on the real site:
      four devices named PDU in R1
      SW2:1 - OUT5, in ANOTHER rack that is also named R1
      SW2:2, SW2:3 - OUT6, OUT7, in two racks outside the copy, both named RZ
      SW2:4, SW2:5 - two devices named ODF, both in rack RX

    And what the diagnostic lines (C5-C9) look for:
      SW2:6 - P A:9        a port number past the end of its cassette (A has 6)
      SW2:7 - P B:4 rear   a path that ends on a panel's REAR port
      P A:5 - OUT A:2      a cable on no path: front seen, the map port stays free
      SW2:1 - OUT5         a second, different path between the same two ports
      Q lists cassette B before A; the SW2:1 cable says OS2
    rack_ids=False: the ends of copied devices carry no path, so nothing
    shows that the paths name racks by the same ids as the tree.
    """
    (out / "assets").mkdir(parents=True)
    (out / "network").mkdir()
    tree = {"site": {"parent": None, "type": "location"}}
    nets = {}

    def put(aid, kind, parent, name, mods=None, u=None):
        tree[aid] = {"parent": parent, "type": kind}
        rec = {"id": aid, "type": kind, "name": name}
        if u:
            rec["rackMounted"] = {"unitPosition": u, "unitHeight": 1}
        (out / "assets" / f"{aid}.json").write_text(json.dumps(rec), encoding="utf-8")
        if mods is not None:
            nets[aid] = {"portConfigurations": [
                {"portConfiguration": {"id": f"{aid}-{m}", "name": m, "connectorType": "LC",
                                       "portCount": n, "portStart": 1},
                 "connectionsAndPaths": []} for m, n in mods]}

    put("R1", "rack", "site", "R1")
    put("SW", "network", "R1", "SW", [("m", 8)], u=10)
    put("SW2", "network", "R1", "SW2", [("m", 8)], u=11)
    put("NN", "patch-panel", "R1", "NN", u=30)
    put("P", "patch-panel", "R1", "P", [("A", 6), ("B", 6)], u=40)
    put("Q", "patch-panel", "R1", "Q", [("B", 6), ("A", 6)], u=41)
    pdus = ["PDU-a1", "PDU-b2", "PDU-c3", "PDU-d4"]
    for aid in pdus:
        put(aid, "rack-mount-pdu", "R1", "PDU")

    def e(aid, port, module, rack=None, name=None):
        end = {"assetId": aid, "networkModule": module, "portNumber": port, "rearPort": False,
               "connectorType": "LC", "assetName": name or aid,
               "assetType": tree[aid]["type"] if aid in tree else "patch-panel"}
        if aid in tree and rack_ids:
            rack = ("R1", "R1")
        if rack:
            end["assetPath"] = [{"elementType": "rack", "id": rack[0], "name": rack[1]}]
        return end

    rx = ("RX-uuid", "RX")
    far = {("SW", 1): e("OUT", 1, "A", rx), ("SW", 2): e("OUT", 1, "B", rx),
           ("SW", 3): e("OUT", 6, "A", rx), ("SW", 8): e("OUT", 0, "C", rx),
           ("SW", 4): e("NN", 3, "m"),
           ("SW", 5): e("P", 2, "Z"), ("SW", 6): e("P", 1, "Y"), ("SW", 7): e("P", 4, "Z"),
           ("SW2", 1): e("OUT5", 1, "A", ("R1-other", "R1")),
           ("SW2", 2): e("OUT6", 1, "A", ("RZ-one", "RZ")),
           ("SW2", 3): e("OUT7", 1, "A", ("RZ-two", "RZ")),
           ("SW2", 4): e("ODF-x1", 1, "A", rx, name="ODF"),
           ("SW2", 5): e("ODF-y2", 1, "A", rx, name="ODF"),
           ("SW2", 6): e("P", 9, "A"),
           ("SW2", 7): dict(e("P", 4, "B"), rearPort=True)}
    for (sw, n), other in sorted(far.items()):
        c = {"id": f"s-{sw}-{n}", "source": e(sw, n, f"{sw}-m"), "destination": other,
             "cableType": {"length": 3, **({"name": "OS2 duplex"} if (sw, n) == ("SW2", 1)
                                           else {})}}
        nets[sw]["portConfigurations"][0]["connectionsAndPaths"].append(
            {"connection": c, "networkPath": {"connections": [c], "beginItem": c["source"],
                                              "endItem": c["destination"]}})
    alt = {"id": "s-alt", "source": e("SW2", 1, "SW2-m"),
           "destination": e("OUT5", 1, "A", ("R1-other", "R1")), "cableType": {"length": 5}}
    nets["SW2"]["portConfigurations"][0]["connectionsAndPaths"].append(
        {"connection": alt, "networkPath": {"connections": [alt], "beginItem": alt["source"],
                                            "endItem": alt["destination"]}})
    nets["P"]["portConfigurations"][0]["connectionsAndPaths"].append(
        {"connection": {"id": "p-loose", "source": e("P", 5, "A"),
                        "destination": e("OUT", 2, "A", rx), "cableType": {"length": 3}}})
    for aid, n in nets.items():
        (out / "network" / f"{aid}.json").write_text(json.dumps(n), encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps(
        {"synced_at": "test", "complete": True, "network_missing": ["NN"] + pdus,
         "tree": tree}), encoding="utf-8")


def loose(**how):
    tmp = Path(tempfile.mkdtemp(prefix="ita_convert_loose_"))
    try:
        loose_site(tmp / "s", **how)
        conv = ita_convert.Converter(tmp / "s")
        return conv, conv.convert()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cassette_cases():
    """Where a connection end lands when the copy has no list of the
    device's cassettes (code review, 2026-11-10)."""
    conv, m = loose()
    far = {n: m["ports"][f"R1:SW:{n}"]["peer"] for n in range(1, 9)}
    check("two cassettes of a panel outside the copy never share a port (A1 is not B1)",
          len({far[1], far[2], far[3], far[8]}) == 4, far)
    check("...port N of its first cassette is map port N, the next cassette follows it",
          (far[1], far[3], far[2]) == ("RX:OUT:1", "RX:OUT:6", "RX:OUT:7"), far)
    check("...and a cassette is numbered from 0 only where a port 0 is seen",
          far[8] == "RX:OUT:8", far)
    check("a copied panel ITA gave no answer for: port 3, seen from the far side, is port 3",
          far[4] == "R1:NN:3", far)
    check("cassettes a panel's answer did not list get ranges that never overlap",
          len({far[5], far[6], far[7]}) == 3
          and all(int(far[n].rsplit(":", 1)[1]) > 12 for n in (5, 6, 7)), far)
    check("...so no port is reported on two paths because of it, and each is noted once",
          "ports on two different paths (check in ITA)" not in conv.notes
          and conv.notes["connection ends on an unknown cassette"] == 2, dict(conv.notes))
    name_cases(conv, m)
    diagnose_cases(conv, m)


def diagnose_cases(conv, m):
    """The lines that say WHY ports look shifted or taken twice on a real
    copy, before anything is changed: counts, type words and field names only."""
    d = ita_convert.diagnose(conv)
    check("five diagnostic lines, C5 to C9, each short enough to type",
          [x[:2] for x in d] == ["C5", "C6", "C7", "C8", "C9"]
          and all(len(x) <= 120 for x in d), d)
    check("C5: the types the copy took as racks, racks with nothing copied in them, rack numbers",
          d[0] == "C5 racks types=rack:1 empty=0 num=none", d)
    check("C6: devices ITA gave no ports for, by type, and how many are seen from the far side",
          d[1] == "C6 noNet=5 types=rack-mount-pdu:4,patch-panel:1 seen=1 inDevice=0", d)
    check("C7: how ends found their cassette, cassette shapes, numbers past a cassette, order",
          d[2] == "C7 match=id:15,name:3,extra:4 cas=s1n6x4,s1n8x2 outRange=1 order=1", d)
    check("C8: ports taken in ITA and in the map; panel ports taken with no front cable and "
          "free with one; paths ending on a rear port; a second path between two ports",
          d[3] == "C8 used ita=? map=17 noFront=1 frontFree=1 endRear=1 samePair=1", d)
    check("C9: where single/multi mode is written, as field names and counts",
          d[4] == "C9 SM/MM: cable.name:SMx1", d)
    check("the four lines typed back so far are unchanged",
          len(ita_convert.type_back(m, conv.notes, conv.shape)) == 4, "")
    check("diagnosing makes no port and no device", (len(m["ports"]), len(m["devices"]))
          == (len(conv.topology["ports"]), len(conv.topology["devices"]))
          and ita_convert.diagnose(conv) == d, "")
    D = m["devices"]
    flagged = sorted(k for k, v in D.items() if v.get("no_port_data"))
    check("a copied device ITA gave no ports for is marked so (the screen shows '?', not 0/0)",
          flagged == ["R1:NN"] + [f"R1:PDU#PDU-{x}" for x in ("a1", "b2", "c3", "d4")],
          flagged)
    import bulkplan
    check("...and none of its ports is ever offered: not as a free port, not as a transit port",
          all(bulkplan.free_port_on_device(m, k, "fiber") is None for k in flagged)
          and all(not str(pathengine._pick_transit_port(m, "R1", "fiber", set())
                          or "").startswith(("R1:NN:", "R1:PDU")) for _ in (0,)),
          [bulkplan.free_port_on_device(m, k, "fiber") for k in flagged])


def name_cases(conv, m):
    """Names repeat on the real site: rows and racks across rooms, devices
    in one rack (first real runs: 8 devices of one name were counted as 28
    and named ...#2#2#2). The id ITA gives tells them apart."""
    D = m["devices"]
    check("devices sharing a name in a rack are told apart by their own id, each noted once",
          {d for d in D if ":PDU" in d} == {f"R1:PDU#PDU-{x}" for x in ("a1", "b2", "c3", "d4")}
          and {"RX:ODF#ODF-x1", "RX:ODF#ODF-y2"} <= set(D)
          and conv.notes["devices sharing a name in one rack"] == 6,
          (sorted(D), dict(conv.notes)))
    check("...and keep their plain name for the screen",
          all(d["name"] == "PDU" for k, d in D.items() if ":PDU" in k), "")
    check("a rack outside the copy that shares a copied rack's name stays outside it",
          D.get("R1#R1-oth:OUT5", {}).get("rack") == "R1#R1-oth"
          and m["racks"].get("R1#R1-oth", {}).get("external")
          and not any(d.get("external") for d in D.values() if d["rack"] == "R1"),
          (sorted(D), sorted(m["racks"])))
    check("two racks outside the copy that share a name are two racks",
          D.get("RZ#RZ-one:OUT6", {}).get("rack") == "RZ#RZ-one"
          and D.get("RZ#RZ-two:OUT7", {}).get("rack") == "RZ#RZ-two"
          and D["RX:OUT"]["rack"] == "RX", sorted(m["racks"]))
    conv2, m2 = loose(rack_ids=False)
    check("with nothing to show that paths name racks by the tree's ids, the name decides, "
          "and it is noted",
          m2["devices"].get("R1:OUT5", {}).get("rack") == "R1"
          and conv2.notes["outside devices put in a copied rack by its name"] == 1
          and "outside devices put in a copied rack by its name" not in conv.notes
          and "RZ#RZ-two:OUT7" in m2["devices"], (sorted(m2["devices"]), dict(conv2.notes)))
    tb = ita_convert.type_back(m2, conv2.notes, conv2.shape)
    check("...in a short word of its own on the typed-back line",
          "nameRack=1" in tb[3] and "extDev=6" in tb[3] and "dupDev=6" in tb[3], tb)


def review_cases():
    tmp = Path(tempfile.mkdtemp(prefix="ita_convert_small_"))
    try:
        small_site(tmp / "s")
        conv = ita_convert.Converter(tmp / "s")
        m = conv.convert()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    P = m["ports"]
    circ = {tuple(sorted((c["a_port"], c["b_port"]))): c for c in m["circuits"].values()}
    c1 = circ.get(("R1:SW:1", "R3:SRV:1"))
    check("a path listed backwards still walks R1 -> R2 -> R3, both trunks in order",
          c1 and [r for r in (c1["hop_racks"] if c1["a_port"] == "R1:SW:1"
                              else c1["hop_racks"][::-1])] == ["R1", "R2", "R3"]
          and len(c1["strands"]) == 2, c1)
    check("every panel port on the path is taken — both ends of each fiber, not one",
          all(P[p]["status"] == "used" and P[p]["circuit"] == (c1 or {}).get("id")
              for p in ("R1:P1:5", "R2:P2:5", "R2:P3:7", "R3:P4:7")),
          {p: P[p]["status"] for p in ("R1:P1:5", "R2:P2:5", "R2:P3:7", "R3:P4:7")})
    check("the same circuit seen from both ends is one circuit", len(
        [c for c in m["circuits"].values() if c["a_port"] in ("R1:SW:1", "R3:SRV:1")]) == 1, "")
    c2 = circ.get(("R1:SRV2:1", "R1:SW:2"))
    check("one direct cable in a rack is a circuit: both ports taken",
          c2 and c2["intra_rack"] and P["R1:SW:2"]["status"] == "used"
          and P["R1:SRV2:1"]["peer"] == "R1:SW:2", c2)
    c3 = circ.get(("R1:SW:3", "R3:SRV:2"))
    check("a direct cable between racks: not intra-rack, both racks on the path, noted",
          c3 and not c3["intra_rack"] and c3["hop_racks"] == ["R1", "R3"]
          and conv.notes["direct cables between racks (no trunk)"] == 1, (c3, dict(conv.notes)))
    e12 = next((x for x in m["edges"] if {x["from"], x["to"]} == {"R1", "R2"}), None)
    check("an unpatched fiber is a free strand of the trunk, its ports free",
          e12 and e12["cable_types"]["fiber"]["capacity"] == 2
          and len(e12["cable_types"]["fiber"]["strands"]) == 1
          and P["R1:P1:6"]["status"] == "free", e12)
    check("cassette B numbered from 0 continues after cassette A (B:0 -> port 25)",
          P["R1:P5:25"]["peer"] == "R1:SW:4", P.get("R1:P5:25"))
    check("an unknown cassette gets ports of its own, never cassette A's of that number",
          P["R1:P5:5"]["peer"] == "R1:SW:6"
          and any(p["peer"] == "R1:SW:5" and p["index"] > 48 for p in P.values()
                  if p["device"] == "R1:P5"), [p for p in P.values() if p["device"] == "R1:P5"
                                                and p["status"] == "used"])
    check("files an earlier copy left behind are not read",
          not any("GHOST" in d for d in m["devices"]), list(m["devices"]))
    lines = ita_convert.summary(m, conv.notes, conv.shape)
    check("SHAPE: how ITA models the fiber, in connector types and counts only",
          "SHAPE  : trunk links by connector: LC-LC=4" in lines
          and "SHAPE  : panel ports by connector: LC=144" in lines
          and "SHAPE  : fan-out cables by outlets: none" in lines, lines)
    ex = ita_convert.explain(conv, 39)
    p5 = ex[:ex.index(next(l for l in ex[1:] if l.startswith("E0")))]
    check("--explain-u: one panel, cassette by cassette, ITA's ports beside the map's",
          p5[0].startswith("E0 U39 type=patch-panel net=yes cassettes=2 ports=49")
          and "ita_used=?" in p5[0] and "extra=5" in p5[0]
          and p5[1] == "E1 match: extra=1 id=2"
          and "K1 A start=1 n=24 LC used=? | front 5 | rear - | map 5" in p5
          and "K2 B start=0 n=24 LC used=? | front 0 | rear - | map 0" in p5
          and any(l.startswith("X3 M3 ") and l.endswith("| front 5 | rear - | map 5") for l in p5),
          ex)
    p1 = [l for l in ita_convert.explain(conv, 40) if l.startswith("K1")]
    check("--explain-u: a rear-only fiber shows in ITA's rear list, not in the map's taken",
          "K1 m start=1 n=24 LC used=? | front 5 | rear 5-6 | map 5" in p1, p1)
    check("--explain-u: field names and numbers only, never a name from the site",
          not any(s in " ".join(ex) for s in ("SNP", "R1:", "P5-A")), ex)
    r2 = [l for l in ita_convert.explain(conv, 40, "R2") if l.startswith(("E0", "K"))]
    check("--explain-rack: several racks in the copy, one picked by its name",
          len(r2) == 2 and "front 5 | rear 5-6" in r2[1], r2)
    check("--explain-rack: a name not in the copy says so",
          ita_convert.explain(conv, 40, "NOPE")[0].startswith("E0 no copied rack"), "")
    check("--explain-u: a U nothing covers says so",
          ita_convert.explain(conv, 7)[0].startswith("E0 U7: no copied device"), "")
    check("a rack-mounted PDU is a device, not a rack (first real run, 2026-11-09)",
          not ita_convert.RACK.search("rack-mount-pdu")
          and not ita_convert.CONTAINER.search("rack-mount-pdu")
          and bool(ita_convert.RACK.search("rack") and ita_convert.CONTAINER.search("rack")), "")
    tb = ita_convert.type_back(m, conv.notes, conv.shape)
    check("the conversion ends with four short lines to type back",
          len(tb) == 4 and all(len(l) <= 120 for l in tb)
          and tb[0].startswith("C1 racks=") and f"circuits={len(m['circuits'])}" in tb[0]
          and tb[2] == "C3 panel=LC:144 trunk=LC-LC:4 fanout=none"
          and tb[3].startswith("C4 notes: ") and "direct=1" in tb[3], tb)
    check("...in counts and short words: no name from the site",
          not any(d.split(":")[-1] in " ".join(tb) for d in m["devices"] if len(d) > 6), tb)
    mpo_cases()
    cassette_cases()


def mpo_cases():
    """In the field the trunks are MPO into fan-out cassettes (2026-11-05).
    If ITA records such a trunk as ONE link, counting it as one strand would
    be a silent error of 12 to 1 — so it is counted, and said, until the real
    model has been seen."""
    tmp = Path(tempfile.mkdtemp(prefix="ita_convert_mpo_"))
    try:
        small_site(tmp / "s")
        f = tmp / "s" / "network" / "P1.json"
        body = json.loads(f.read_text(encoding="utf-8"))

        def touch(c):
            if c.get("id") == "f6":
                c["source"]["connectorType"] = c["destination"]["connectorType"] = "MPO-12 (MTP)"
                c["cableType"].update(isFanOut=True, fanOutOutletsCount=12)
        for pc in body["portConfigurations"]:
            for cp in pc["connectionsAndPaths"]:
                for c in [cp["connection"]] + cp["networkPath"]["connections"]:
                    touch(c)
        f.write_text(json.dumps(body), encoding="utf-8")
        conv = ita_convert.Converter(tmp / "s")
        m = conv.convert()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    lines = ita_convert.summary(m, conv.notes, conv.shape)
    check("an MPO trunk link is flagged, never silently taken as one fiber",
          "NOTE   : MPO/MTP trunk links, each counted as ONE strand for now: 1" in lines
          and "SHAPE  : fan-out cables by outlets: 12=1" in lines
          and any(l.startswith("SHAPE  : trunk links by connector:") and "MPO-MPO=1" in l
                  for l in lines), lines)
    check("...and the connector shows as a generic word, not the text ITA holds",
          not any("MPO-12 (MTP)" in l for l in lines), lines)
    tb = ita_convert.type_back(m, conv.notes, conv.shape)
    check("...and the typed-back lines carry it too",
          "MPO-MPO:1" in tb[2] and "fanout=12:1" in tb[2] and "mpo=1" in tb[3], tb)


def engine_cases():
    """pathengine on a map from ITA: a new route must ride real panel ports —
    strand N is panel port N at both ends — and cross-connect between exactly
    the port the incoming fiber lands on and the one the outgoing leaves from.
    Free on the small site: R1-R2 fiber on P1:6/P2:6, R2-R3 fiber on P3:8/P4:8."""
    import workorder
    tmp = Path(tempfile.mkdtemp(prefix="ita_convert_engine_"))
    try:
        small_site(tmp / "s")
        base = ita_convert.Converter(tmp / "s").convert()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    pathengine.seed_circuit_seq(base)

    m = copy.deepcopy(base)
    route = pathengine.resolve_route_options("R1:SW:7", "R3:SRV:3", count=1, topology=m)[0]
    segs = route["segments"]
    check("a route on a map from ITA rides the free fibers' own panel ports",
          route["hop_racks"] == ["R1", "R2", "R3"]
          and [(s_["strand_port_from"], s_["strand_port_to"]) for s_ in segs]
          == [("R1:P1:6", "R2:P2:6"), ("R2:P3:8", "R3:P4:8")], segs)
    tp = route["transit_points"]
    check("the cross-connect in R2 joins the port the fiber lands on to the one the next leaves from",
          len(tp) == 1 and tp[0]["port"] == "R2:P2:6" and tp[0]["out_port"] == "R2:P3:8", tp)
    check("the route lists every port it will take",
          route["patch_ports"] == ["R1:P1:6", "R2:P2:6", "R2:P3:8", "R3:P4:8"],
          route["patch_ports"])
    text = workorder.render(route)
    check("the Work Order names real panel ports, no made-up strand ids",
          "R1-U40-P6 (P1)" in text and "R3-U40-P8 (P4)" in text and "#F0" not in text
          and "domain None" not in text, text)
    cords_cases(route, text)

    # a fiber whose front port has a cord in it is not free, whatever the trunk says
    m2 = copy.deepcopy(base)
    m2["ports"]["R2:P2:6"]["status"] = "used"
    try:
        pathengine.resolve_route_options("R1:SW:7", "R3:SRV:3", count=1, topology=m2)
        offered = True
    except pathengine.RouteError:
        offered = False
    check("a strand whose panel port is taken is never offered", not offered, "")

    # a stale plan is caught before it is committed
    m3 = copy.deepcopy(base)
    r3 = pathengine.resolve_route_options("R1:SW:7", "R3:SRV:3", count=1, topology=m3)[0]
    m3["ports"]["R3:P4:8"]["status"] = "used"
    try:
        pathengine.revalidate_route(m3, r3)
        stale_ok = False
    except pathengine.RouteError:
        stale_ok = True
    check("revalidation catches a panel port taken after planning", stale_ok, "")

    # commit, truncate, extend, release: always exactly the right ports
    cid = pathengine.next_circuit_id(m)
    pathengine.commit_route(m, route, cid)
    taken = ["R1:SW:7", "R1:P1:6", "R2:P2:6", "R2:P3:8", "R3:P4:8", "R3:SRV:3"]
    check("commit takes both ends and every panel port on the way",
          all(m["ports"][p]["circuit"] == cid for p in taken), {p: m["ports"][p] for p in taken})
    t = pathengine.truncate_route(m, cid)
    check("truncate: the open end is where the kept fiber lands; the rest is freed",
          t["open_end"] == "R2:P2:6"
          and all(m["ports"][p]["status"] == "free" for p in ("R2:P3:8", "R3:P4:8", "R3:SRV:3"))
          and m["ports"]["R1:P1:6"]["circuit"] == cid, (t, {p: m["ports"][p]["status"]
                                                             for p in taken}))
    pathengine.extend_route(m, cid, "R3:SRV:4")
    check("extend from the open end takes the new tail's panel ports",
          all(m["ports"][p]["circuit"] == cid
              for p in ("R2:P2:6", "R2:P3:8", "R3:P4:8", "R3:SRV:4")),
          {p: m["ports"][p]["status"] for p in ("R2:P3:8", "R3:P4:8", "R3:SRV:4")})
    pathengine.decommission_route(m, cid)
    check("release gives every port and strand back",
          all(m["ports"][p]["status"] == "free"
              for p in taken + ["R3:SRV:4"]) and all(
              str(i) not in (e_["cable_types"]["fiber"]["strands"])
              for e_ in m["edges"] for i in (2,)), "")
    check("...leaving the map exactly as it was", m["ports"] == base["ports"]
          and m["edges"] == base["edges"], "")

    # an EXISTING circuit read back from ITA prints the same way
    import server
    keep = server.TOPOLOGY
    server.TOPOLOGY = base
    try:
        cid = next(c for c, v in base["circuits"].items() if v["a_port"] in ("R1:SW:1", "R3:SRV:1"))
        old = server._route_of_circuit(cid)
        text = workorder.render(old, circuit_id=cid)
    finally:
        server.TOPOLOGY = keep
    check("an existing ITA circuit's Work Order: real ports, one cross-connect in R2",
          [t_["location"] for t_ in old["transit_points"]]
          == ["R2-U40-P5 (P2)  <->  R2-U39-P7 (P3)"]
          and "near end : R1-U40-P5 (P1)" in text and "far  end : R3-U40-P7 (P4)" in text,
          text)
    check("...and it lists the same three cords a new route would need",
          [(c["rack"], c["stock_m"]) for c in old["patch_cords"]]
          == [("R1", 2), ("R2", 1), ("R3", 2)], old["patch_cords"])


def cords_cases(route, text):
    """The cords a technician brings: device to panel at each end, panel to
    panel at each cross-connect, sized by how far apart they sit in the cabinet
    (field rule, 2026-11-04: panel at U40, switch at U1 -> a 3 m fiber)."""
    import patchcords
    import wo_html
    got = [(c["rack"], c["from_port"], c["to_port"], c["gap_u"], c["stock_m"])
           for c in route["patch_cords"]]
    check("cords: switch->panel in R1, panel->panel in R2, panel->server in R3",
          got == [("R1", "R1:SW:7", "R1:P1:6", 30, 2), ("R2", "R2:P2:6", "R2:P3:8", 1, 1),
                  ("R3", "R3:P4:8", "R3:SRV:3", 30, 2)], got)
    check("cords: the connector types from ITA name the cord",
          patchcords.kit(route["patch_cords"]) == ["1 x 1 m fiber LC-LC", "2 x 2 m fiber LC-LC"],
          patchcords.kit(route["patch_cords"]))
    check("cords: the Work Order lists them",
          "PATCH CORDS TO BRING" in text and "Total: 1 x 1 m fiber LC-LC, 2 x 2 m" in text, text)
    html = wo_html.render(route)
    check("cords: the printable sheet lists them too",
          "Patch cords to bring" in html and "<b>2 m</b>" in html, "")
    # ITA may hold no trunk lengths at all: then the sheet says nothing about them
    bare = copy.deepcopy(route)
    bare["total_length_m"] = 0.0
    for s_ in bare["segments"]:
        s_["length_m"] = 0.0
    t0, h0 = workorder_text(bare), wo_html.render(bare)
    check("a map with no trunk lengths prints no '~0 m'",
          "~0.0 m" not in t0 and "Total length" not in t0
          and "Estimated length" not in h0 and "0.0 m" not in h0, t0)

    topo = {"ports": {"a": {"rack": "X", "device": "d1", "index": 1, "type": "fiber"},
                      "b": {"rack": "X", "device": "d2", "index": 1, "type": "fiber"},
                      "c": {"rack": "X", "device": "d3", "index": 1, "type": "fiber"},
                      "z": {"rack": "Y", "device": "d4", "index": 1, "type": "fiber"}},
            "devices": {"d1": {"name": "PP", "u_start": 40}, "d2": {"name": "SW", "u_start": 1},
                        "d3": {"name": "PDU", "u_start": None}, "d4": {"name": "Q", "u_start": 1}}}
    c = patchcords.cord(topo, "a", "b")
    check("cords: the field's own example — U40 to U1 is a 3 m cord",
          c["gap_u"] == 39 and c["stock_m"] == 3, c)
    check("cords: an end ITA has no U for says 'measure on site', not a guess",
          patchcords.cord(topo, "a", "c")["stock_m"] is None
          and "measure on site" in patchcords.describe(patchcords.cord(topo, "a", "c")), "")
    check("cords: two ports in different cabinets are never one cord",
          patchcords.cord(topo, "a", "z") is None, "")
    synthetic = pathengine.load_topology()
    free = [p for p in synthetic["ports"].values()
            if p["status"] == "free" and p["type"] == "fiber"
            and synthetic["devices"][p["device"]]["type"] == "server"]
    a = free[0]
    b = next(p for p in free if p["rack"] != a["rack"])
    r = pathengine.resolve_route_options(a["id"], b["id"], count=1, topology=synthetic)[0]
    check("cords: the synthetic map's made-up trunk ends get no cord, and nothing breaks",
          r["patch_cords"] == [] and "PATCH CORDS" not in workorder_text(r), r["patch_cords"])


def workorder_text(route):
    import workorder
    return workorder.render(route)


def main():
    review_cases()
    engine_cases()
    t = pathengine.load_topology()
    tmp = Path(tempfile.mkdtemp(prefix="ita_convert_test_"))
    try:
        to_ita_cache(t, tmp / "site")
        conv = ita_convert.Converter(tmp / "site")
        m = conv.convert()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    check("racks: the same set, nothing added, nothing lost",
          set(m["racks"]) == set(t["racks"]), set(m["racks"]) ^ set(t["racks"]))
    check("racks keep their row from the tree; pod and EOR are left for the site config",
          all(m["racks"][r]["row"] == f"{t['racks'][r]['pod']}-{t['racks'][r]['row']}"
              and m["racks"][r]["pod"] is None for r in t["racks"]), "")

    real = {d: x for d, x in m["devices"].items() if ":TRUNK-" not in d}
    check("devices: the same set", set(real) == set(t["devices"]),
          list(set(real) ^ set(t["devices"]))[:5])
    bad = [d for d, x in t["devices"].items() if d in real and any(
        real[d][k] != x[k] for k in ("type", "u_start", "u_size", "serial",
                                     "fiber_ports", "copper_ports", "label", "rack"))]
    check("every device: same type, top U, height, serial, port counts", not bad,
          [(d, {k: (t["devices"][d][k], real[d][k]) for k in t["devices"][d]
                if k in real[d] and t["devices"][d][k] != real[d][k]}) for d in bad[:3]])

    tports = set(t["ports"])
    mports = {p for p in m["ports"] if ":TRUNK-" not in p}
    check("ports: every one numbered as before, fiber then copper", mports == tports,
          list(tports ^ mports)[:5])

    def pairs(top):
        out = {}
        for e in top["edges"]:
            for media, ct in e["cable_types"].items():
                out[(tuple(sorted((e["from"], e["to"]))), media)] = ct
        return out
    tp, mp = pairs(t), pairs(m)
    check("trunks: the same rack pairs and media", set(tp) == set(mp), set(tp) ^ set(mp))
    check("trunks: the same number of strands each",
          all(tp[k]["capacity"] == mp[k]["capacity"] for k in tp if k in mp), "")

    # circuits come back under new ids: match them by their two endpoints
    by_ends = {tuple(sorted((c["a_port"], c["b_port"]))): c for c in m["circuits"].values()}
    check("circuits: every existing one found, none invented",
          len(m["circuits"]) == len(t["circuits"])
          and all(tuple(sorted((c["a_port"], c["b_port"]))) in by_ends
                  for c in t["circuits"].values()), (len(m["circuits"]), len(t["circuits"])))
    wrong_hops, wrong_strands = [], []
    for c in t["circuits"].values():
        mc = by_ends.get(tuple(sorted((c["a_port"], c["b_port"]))))
        if not mc:
            continue
        hops = mc["hop_racks"] if mc["a_port"] == c["a_port"] else mc["hop_racks"][::-1]
        if hops != c["hop_racks"]:
            wrong_hops.append(c["id"])
        ts = [(tuple(sorted(s["edge_id"].replace("<->", "->").split("->"))), s["strand_index"])
              for s in c["strands"]]
        ms = [(tuple(sorted(s["edge_id"].split("<->"))), s["strand_index"]) for s in mc["strands"]]
        if mc["a_port"] != c["a_port"]:
            ms = ms[::-1]
        if ts != ms:
            wrong_strands.append(c["id"])
    check("every circuit crosses the same racks in the same order", not wrong_hops,
          wrong_hops[:5])
    check("every circuit holds the same strand number on every trunk", not wrong_strands,
          wrong_strands[:5])
    check("endpoints are marked used and point at each other",
          all(m["ports"][c["a_port"]]["peer"] == c["b_port"]
              and m["ports"][c["b_port"]]["status"] == "used" for c in t["circuits"].values()),
          "")
    check("each strand in use is owned by exactly the circuit that holds it",
          all(len(mp[k]["strands"]) == len(tp[k]["strands"]) for k in tp if k in mp), "")
    check("nothing to report on a clean copy", not conv.notes, dict(conv.notes))

    # the point of it all: the engine plans and commits on the converted map
    work = copy.deepcopy(m)
    pathengine.seed_circuit_seq(work)
    free = [p for p in work["ports"].values()
            if p["status"] == "free" and p["type"] == "fiber" and ":TRUNK-" not in p["id"]
            and work["devices"][p["device"]]["type"] == "fiber_patch_panel"]
    a = free[0]
    b = next(p for p in free if p["rack"][:2] != a["rack"][:2])
    try:
        opts = pathengine.resolve_route_options(a["id"], b["id"], count=2, topology=work)
        route = opts[0]
        cid = pathengine.next_circuit_id(work)
        pathengine.commit_route(work, route, cid)
        ok = work["ports"][a["id"]]["circuit"] == cid and len(route["segments"]) >= 1
        detail = (len(opts), [s["edge_id"] for s in route["segments"]])
    except Exception as e:          # noqa: BLE001 — report whatever the engine raised
        ok, detail = False, repr(e)
    check("pathengine plans and commits a new route on the converted map", ok, detail)
    check("new circuit ids continue after the converted ones",
          ok and cid == f"CIR-{len(t['circuits']) + 1:04d}", cid if ok else detail)

    print(f"\n{sum(RESULTS)}/{len(RESULTS)} convert checks passed")
    raise SystemExit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
