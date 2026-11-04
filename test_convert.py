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

    used = set()
    for c in t["circuits"].values():
        conns = []
        for s in c["strands"]:
            conns.append(trunk[(s["edge_id"], s["cable_type"], s["strand_index"])])
            used.add((s["edge_id"], s["cable_type"], s["strand_index"]))
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


def main():
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
