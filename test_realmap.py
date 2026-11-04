#!/usr/bin/env python3
"""
test_realmap.py
===============
The whole application on a map converted from ITA — no pods, no rows, no EOR
or MDA marks, no A/B domains, because ITA has none of those (they are "just
known" on site). Every endpoint the interface uses is called through the real
HTTP server, the way the browser calls it, and must answer without falling
over: the code review (2026-11-04) found capacity, placement, zones and the
older router all assuming a pod and a domain on every cabinet.

The map is the small hand-built site from test_convert.py, run through
ita_convert.py exactly as a real copy would be.

    python test_realmap.py
"""

import copy
import json
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import ita_convert
import pathengine
import server
import store as storage
import test_convert

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" — {str(detail)[:700]}"))


def converted_map():
    tmp = Path(tempfile.mkdtemp(prefix="ita_realmap_"))
    try:
        test_convert.small_site(tmp / "s")
        m = ita_convert.Converter(tmp / "s").convert()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    pathengine.seed_circuit_seq(m)
    return m


class Client:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"

    def call(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                blob = r.read()
                return r.status, blob
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def json(self, method, path, body=None, raw=None):
        st, blob = self.call(method, path, body, raw)
        try:
            return st, json.loads(blob.decode("utf-8"))
        except ValueError:
            return st, blob.decode("utf-8", "replace")


def main():
    m = converted_map()
    tmp = Path(tempfile.mkdtemp(prefix="ita_realmap_store_"))
    keep = (server.TOPOLOGY, server.STORE)
    server.TOPOLOGY = m
    server.STORE = storage.open_store("json", path=tmp / "topology.json")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    c = Client(srv.server_port)

    def ok(name, method, path, body=None, raw=None, want=200):
        st, out = c.json(method, path, body, raw)
        check(name, st == want, (st, out))
        return out

    try:
        ok("GET /api/topology", "GET", "/api/topology")
        ok("GET /api/stats", "GET", "/api/stats")
        ok("GET /api/rack", "GET", "/api/rack?id=R2")
        ok("GET /api/zones", "GET", "/api/zones")
        ok("GET /api/capacity", "GET", "/api/capacity")
        ok("GET /api/search (device name)", "GET", "/api/search?q=SW")
        ok("GET /api/search (serial)", "GET", "/api/search?q=SNSW1")
        cid = next(iter(m["circuits"]))
        ok("GET /api/circuit", "GET", f"/api/circuit?id={cid}")
        st, blob = c.call("GET", f"/api/workorder?circuit={cid}")
        check("GET /api/workorder (existing circuit)", st == 200, (st, blob[:300]))
        st, sheet = c.call("GET", "/api/sample?kind=clean")
        check("GET /api/sample (clean sheet)", st == 200, (st, sheet[:300]))
        st, kit = c.call("GET", "/api/sample?kind=devices")
        check("GET /api/sample (sheet with new devices)", st == 200, (st, kit[:300]))

        routed = ok("POST /api/route", "POST", "/api/route",
                    {"src": "R1:SW:7", "dst": "R3:SRV:3"})
        key = (routed.get("options") or [{}])[0].get("route_key") if isinstance(routed, dict) else None
        check("...and it found a route", bool(key), routed)
        done = ok("POST /api/execute", "POST", "/api/execute", {"route_key": key})
        new = (done.get("circuit") or {}).get("id") if isinstance(done, dict) else None
        ok("POST /api/truncate", "POST", "/api/truncate", {"circuit_id": new})
        ok("POST /api/extend", "POST", "/api/extend", {"circuit_id": new, "dst": "R3:SRV:4"})
        ok("POST /api/decommission", "POST", "/api/decommission", {"circuit_id": new})

        if st == 200:
            plan = ok("POST /api/bulk/plan (clean sheet)", "POST", "/api/bulk/plan", raw=sheet)
            pid = plan.get("plan_id") if isinstance(plan, dict) else None
            ok("POST /api/assist", "POST", "/api/assist",
               {"plan_id": pid, "message": "מה המצב?"})
            ok("POST /api/bulk/execute", "POST", "/api/bulk/execute", {"plan_id": pid})
        ok("POST /api/bulk/plan (new devices)", "POST",
           "/api/bulk/plan?with_devices=1", raw=kit)
    finally:
        srv.shutdown()
        server.TOPOLOGY, server.STORE = keep
        shutil.rmtree(tmp, ignore_errors=True)

    content_checks()
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} real-map checks passed")
    raise SystemExit(0 if all(RESULTS) else 1)


def content_checks():
    """Beyond 'it answers': on a map with no pods the answers are right."""
    import io
    import bulkplan
    import layout
    import make_sample_sheet as mss
    import placement
    import zones

    m = converted_map()

    def plan_sheet(tabs, topology):
        buf = io.BytesIO()
        mss.write_xlsx(buf, tabs)
        blob = buf.getvalue()
        demands = bulkplan.read_demand_sheet(io.BytesIO(blob))
        kit = (bulkplan.read_device_sheet(io.BytesIO(blob))
               if "Devices" in tabs else [])
        return bulkplan.plan(copy.deepcopy(topology), demands, None, new_devices=kit)

    try:
        old = pathengine.resolve_path("R1:SW:7", "R3:SRV:3", topology=m)
        old_ok = old["hop_racks"] == ["R1", "R2", "R3"] and old["domain"] is None
    except pathengine.RouteError as e:
        old, old_ok = str(e), False
    check("the CLI's router finds a path on a map with no A/B domains", old_ok, old)

    clean = plan_sheet({"P2P": mss.build_clean(m)}, m)
    check("a sample sheet built on the converted map plans in full",
          clean["summary"]["total"] > 0 and clean["summary"]["failed"] == 0, clean["summary"])
    kit = plan_sheet(mss.build_new_devices(m), m)
    check("new equipment is placed and wired on a site with no pods",
          kit["summary"]["devices_placed"] == 4 and kit["summary"]["failed"] == 0,
          kit["summary"])
    sited = [s_ for s_ in kit["siting"]["placements"] if s_.get("rack")]
    check("...never in a stub cabinet outside the copy",
          sited and not any(m["racks"][s_["rack"]].get("external") for s_ in sited), sited)

    # a zone named by CABINET, as a site without pods has to name it
    keep = zones.NAMED_ZONES
    zones.NAMED_ZONES = {"green": ("R2",)}
    try:
        spec = {"serial": "X1", "type": "server", "u_size": 1, "fiber_ports": 2,
                "copper_ports": 0, "zone": "green"}
        spots = placement.rank_positions(m, spec, ["R1"], limit=10)
        check("a zone named by cabinet: green kit goes only into R2",
              spots and {s_["rack"] for s_ in spots} == {"R2"}
              and all(s_["zone"] == "green" for s_ in spots), spots)
        check("...and every other cabinet is the default colour",
              zones.zone_of_rack(m, "R1") == "yellow", zones.zone_of_rack(m, "R1"))
        check("...and the audit does not call R2 missing",
              not any("R2" in z["note"] for z in zones.audit(m)), zones.audit(m))
    finally:
        zones.NAMED_ZONES = keep

    # an ITA-shaped site: rooms and named rows, a lone cabinet, a tall cabinet,
    # a device with no U — and no pods anywhere
    racks = {
        "A01": {"id": "A01", "pod": None, "row": "ROW-A", "room": "HALL-1", "u_height": 48},
        "A02": {"id": "A02", "pod": None, "row": "ROW-A", "room": "HALL-1", "u_height": 42},
        "B01": {"id": "B01", "pod": None, "row": "ROW-B", "room": "HALL-1", "u_height": 42},
        "L01": {"id": "L01", "pod": None, "row": None, "room": "HALL-1", "u_height": 42},
        "X01": {"id": "X01", "pod": None, "row": "ROW-X", "room": "HALL-2", "u_height": 42},
    }
    t = {"pods": {}, "racks": racks, "devices": {
        "A01:PDU": {"id": "A01:PDU", "rack": "A01", "u_start": None, "u_size": 1},
        "A01:SW": {"id": "A01:SW", "rack": "A01", "u_start": 48, "u_size": 1}}, "ports": {}}
    rank = lambda a, b: placement.distance_rank(t, a, b)          # noqa: E731
    check("proximity on a site from ITA: same row < same room < another room",
          rank("A01", "A02") == placement.SAME_ROW and rank("A01", "B01") == placement.SAME_ROOM
          and rank("A01", "L01") == placement.SAME_ROOM and rank("A01", "X01") == placement.ELSEWHERE,
          [rank("A01", x) for x in ("A02", "B01", "L01", "X01")])
    check("two cabinets with no pod are not 'the same pod'",
          rank("B01", "X01") == placement.ELSEWHERE, rank("B01", "X01"))
    check("a 48U cabinet offers 48U; a device with no U takes none",
          placement.free_gaps(t, "A01") == [(47, 47)], placement.free_gaps(t, "A01"))
    check("a cabinet's description shows only what the map has",
          layout.describe(t, "A01") == "שדרה ROW-A · חדר HALL-1", layout.describe(t, "A01"))


if __name__ == "__main__":
    main()
