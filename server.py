#!/usr/bin/env python3
"""
server.py
=========
Tiny local web server that puts the graphical interface in front of the
Python routing engine.

Deliberately uses only the Python standard library (http.server) — no Flask,
no npm, nothing extra to install on a closed network. Run it and open the
browser:

    python server.py

    -> http://127.0.0.1:8800

The browser is only a viewer. Every route calculation happens in
pathengine.py on the Python side, so there is exactly ONE implementation of
the routing rules — the same one that will later be pointed at the real ITA
API instead of at topology.json.

Endpoints
---------
  GET  /                       the UI
  GET  /api/topology           the whole map (racks, devices, ports, trunks)
  POST /api/route               {src, dst, domain?, count?} -> {status, options: [route+work_order, ...]}
  POST /api/execute            {route} -> commits a previously-proposed route,
                                consuming ports/trunk capacity and persisting
                                it to topology.json as a real circuit; also
                                returns a re-rendered Work Order stamped
                                EXECUTED with the new circuit id
  GET  /api/circuit?id=CIR-1   an existing circuit's path (for SHOW ROUTE on a used port)

  POST /api/bulk/plan?validate_only=1
                               raw .xlsx body -> preflight review only; checks
                               every row against the map and against the rest
                               of the sheet, plans nothing
  POST /api/bulk/plan          raw .xlsx body (+ preference query params)
                               -> preflight review AND a full plan, held
                               server-side under a plan_id
  POST /api/bulk/execute       {plan_id} -> commits every planned row, each
                               revalidated first; a stale row fails alone
  GET  /api/workorder?...      printable Work Order (HTML) for one route
"""

import copy
import io
import json
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import assistant
import bulkplan
import pathengine
import placement
import wo_html
import workorder
import xlsxreader
import zones

HERE = Path(__file__).parent
PORT = 8800
MAX_UPLOAD = 8 * 1024 * 1024        # a demand sheet is kilobytes; refuse anything wild

TOPOLOGY = pathengine.load_topology()

# ThreadingHTTPServer runs every request in its own thread, and committing is a
# read-modify-write across the shared topology: pick the next circuit id, take
# the strands, then rewrite the file. Two commits interleaving there could hand
# out the same id or write a half-updated map, so the whole sequence is done
# under one lock. Reads and route PROPOSALS stay outside it — they don't mutate,
# and revalidate_route re-checks under the lock before anything is written.
WRITE_LOCK = threading.Lock()

# Plans live server-side between plan and execute, so the browser never has to
# ship whole route objects back and never gets to rewrite one on the way.
PLANS = {}

# Proposed single routes, kept only so they can be re-rendered as a printable
# Work Order without the browser posting the whole route back. Capped, because
# a long session would otherwise accumulate every proposal ever made.
ROUTES = {}
MAX_CACHED_ROUTES = 200


def _cache_route(route):
    key = f"R{len(ROUTES) + 1:05d}"
    if len(ROUTES) >= MAX_CACHED_ROUTES:
        for old in list(ROUTES)[:len(ROUTES) - MAX_CACHED_ROUTES + 1]:
            ROUTES.pop(old, None)
    ROUTES[key] = route
    return key


def _route_of_circuit(circuit_id):
    """Rebuild a route-shaped dict from a committed circuit, so an already
    installed connection can be reprinted. A circuit records what was done;
    the printable sheet wants it in the same shape a proposal has."""
    circuit = TOPOLOGY.get("circuits", {}).get(circuit_id)
    if not circuit:
        return None
    edges = {e["id"]: e for e in TOPOLOGY["edges"]}
    ct_name = circuit["cable_type"]

    segments = []
    for s in circuit.get("strands", []):
        edge = edges.get(s["edge_id"])
        if not edge:
            continue
        ct = edge["cable_types"][ct_name]
        used = len(ct.get("strands") or {})
        segments.append({
            "edge_id": s["edge_id"], "domain": edge["domain"],
            "from_rack": edge["from"], "to_rack": edge["to"],
            "length_m": edge["length_m"], "capacity": ct["capacity"],
            # this circuit is already in place, so the honest figure is the
            # occupancy as it stands now, not a "before" we no longer know
            "used_before": used, "remaining_before": ct["capacity"] - used,
            "strand_index": s["strand_index"],
            "strand_port_from": s["from_port"], "strand_port_to": s["to_port"],
        })

    return {
        "cable_type": ct_name, "domain": circuit["domain"],
        "src_port": circuit["a_port"], "dst_port": circuit["b_port"],
        "src_location": pathengine.describe_port(TOPOLOGY, circuit["a_port"]),
        "dst_location": pathengine.describe_port(TOPOLOGY, circuit["b_port"]),
        "hop_racks": circuit["hop_racks"], "segments": segments,
        "transit_points": [{"port": p, "rack": TOPOLOGY["ports"][p]["rack"],
                            "location": pathengine.describe_port(TOPOLOGY, p)}
                           for p in circuit.get("transit_ports", [])],
        "total_length_m": circuit["total_length_m"],
    }


class Handler(BaseHTTPRequestHandler):

    def _send(self, code, payload, content_type="application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # Every handler runs behind this. Without it an unexpected exception —
    # malformed JSON in a field, a shape the client should not have sent —
    # escapes through BaseHTTPRequestHandler and simply drops the connection,
    # which reaches the browser as "Failed to fetch" with nothing to act on.
    # Two such crashes were found by probing rather than by use, which is
    # exactly the kind that reaches a user first.
    def _guard(self, fn, *args):
        try:
            return fn(*args)
        except Exception as e:
            traceback.print_exc()
            try:
                self._send(500, {"error": f"{type(e).__name__}: {e}"})
            except Exception:
                pass          # client already gone; nothing useful left to do

    def do_GET(self):
        return self._guard(self._get)

    def do_POST(self):
        return self._guard(self._post)

    def _get(self):
        url = urlparse(self.path)

        if url.path in ("/", "/index.html"):
            html = (HERE / "ui.html").read_bytes()
            return self._send(200, html, "text/html; charset=utf-8")

        if url.path == "/api/topology":
            return self._send(200, TOPOLOGY)

        if url.path == "/api/zones":
            return self._send(200, {
                "pods": zones.summary(TOPOLOGY),
                "colours": zones.COLOURS,
                "audit": zones.audit(TOPOLOGY),
            })

        if url.path == "/api/workorder":
            # Printable sheet for a route the browser is already showing, or
            # for a circuit that is already committed.
            q = parse_qs(url.query)
            cid = q.get("circuit", [None])[0]
            key = q.get("plan", [None])[0]
            row = q.get("row", [None])[0]

            route, circuit_id = None, None
            if cid:
                route, circuit_id = _route_of_circuit(cid), cid
            elif q.get("route", [None])[0]:
                route = ROUTES.get(q["route"][0])
            elif key and row:
                plan = PLANS.get(key)
                if plan:
                    hit = next((r for r in plan["results"]
                                if str(r.get("row")) == row and r.get("route")), None)
                    if hit:
                        route = hit["route"]
                        circuit_id = hit.get("circuit_id")
            if not route:
                return self._send(404, {"error": "no such route to print"})
            html = wo_html.render(route, circuit_id=circuit_id)
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

        if url.path == "/api/circuit":
            cid = parse_qs(url.query).get("id", [None])[0]
            circuit = TOPOLOGY.get("circuits", {}).get(cid)
            if not circuit:
                return self._send(404, {"error": f"circuit {cid} not found"})
            enriched = dict(circuit)
            enriched["a_location"] = pathengine.describe_port(TOPOLOGY, circuit["a_port"])
            enriched["b_location"] = pathengine.describe_port(TOPOLOGY, circuit["b_port"])
            return self._send(200, enriched)

        return self._send(404, {"error": "not found"})

    def _post(self):
        url = urlparse(self.path)

        if url.path == "/api/route":
            length = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "invalid JSON"})

            src, dst = req.get("src"), req.get("dst")
            domain = req.get("domain") or None
            count = req.get("count") or 2
            if not src or not dst:
                return self._send(400, {"error": "src and dst are required"})

            try:
                options = pathengine.resolve_route_options(
                    src, dst, domain=domain, count=count, topology=TOPOLOGY)
            except pathengine.RouteError as e:
                return self._send(200, {"status": "failed", "reason": str(e)})

            for route in options:
                route["work_order"] = workorder.render(route)
                route["route_key"] = _cache_route(route)
            return self._send(200, {"status": "ok", "options": options})

        if url.path == "/api/execute":
            length = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "invalid JSON"})

            route = req.get("route")
            if not route:
                return self._send(400, {"error": "route is required"})

            with WRITE_LOCK:
                try:
                    pathengine.revalidate_route(TOPOLOGY, route)
                except pathengine.RouteError as e:
                    return self._send(409, {"error": str(e)})

                circuit_id = pathengine.next_circuit_id(TOPOLOGY)
                circuit = pathengine.commit_route(TOPOLOGY, route, circuit_id)
                pathengine.save_topology(TOPOLOGY)
            work_order = workorder.render(route, circuit_id=circuit_id)
            return self._send(200, {"status": "ok", "circuit": circuit, "work_order": work_order})

        if url.path == "/api/bulk/plan":
            return self._bulk_plan(url)

        if url.path == "/api/bulk/execute":
            return self._bulk_execute()

        if url.path == "/api/assist":
            return self._assist()

        return self._send(404, {"error": "not found"})

    def _assist(self):
        """One turn of the offline assistant. Nothing leaves this machine: the
        answers are read out of the plan that is already in memory."""
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid JSON"})

        # a plan id arrives from the browser and need not be a string, let
        # alone a hashable one — coerce before it reaches a dict lookup
        plan_id = req.get("plan_id")
        stored = PLANS.get(str(plan_id) if plan_id is not None else None) or {}
        # the browser holds a trimmed copy; the assistant needs the full one,
        # including the ranked alternatives that were never sent down
        context = {
            "results": stored.get("results", []),
            "summary": stored.get("summary", {}),
            "siting": (stored.get("siting") or {}).get("placements", []),
            "review": req.get("review"),
            "device_review": req.get("device_review"),
        }
        out = assistant.respond(req.get("message", ""), plan=context,
                                topology=TOPOLOGY,
                                constraints=req.get("constraints"))
        return self._send(200, out)

    # ------------------------------------------------------------- bulk ---

    def _bulk_plan(self, url):
        """Body is the raw .xlsx; preferences ride along as query params, so
        there is no multipart parsing to get wrong."""
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return self._send(400, {"error": "no file received"})
        if length > MAX_UPLOAD:
            return self._send(413, {"error": f"file is larger than {MAX_UPLOAD // 1024 // 1024} MB"})

        blob = self.rfile.read(length)
        q = parse_qs(url.query)
        prefs = {k: q.get(k, ["0"])[0] in ("1", "true", "on")
                 for k in ("group_adjacent", "redundancy_split", "load_balance")}

        # Instructions the planner was given in the chat ("not in D5"), carried
        # as a query param so a plan is reproducible from its URL alone.
        constraints = {}
        for key in placement.EMPTY_CONSTRAINTS:
            raw = q.get(key, [""])[0]
            if raw:
                constraints[key] = {v for v in raw.split(",") if v}

        # Adding hardware is a bigger step than patching, so the Devices tab is
        # only read when the user has said the file contains new equipment.
        want_devices = q.get("with_devices", ["0"])[0] in ("1", "true", "on")
        try:
            demands = bulkplan.read_demand_sheet(io.BytesIO(blob))
            new_devices = (bulkplan.read_device_sheet(io.BytesIO(blob))
                           if want_devices else [])
        except (xlsxreader.XlsxError, bulkplan.BulkError) as e:
            return self._send(200, {"status": "failed", "reason": str(e)})

        # Preflight first: a sheet with typos comes back as one list of things
        # to fix, instead of a half-planned run the user has to unpick.
        review = bulkplan.validate(TOPOLOGY, demands, new_devices)
        device_review = (bulkplan.validate_devices(TOPOLOGY, new_devices, demands)
                         if new_devices else None)
        if q.get("validate_only", ["0"])[0] in ("1", "true", "on"):
            has_tab = bulkplan._find_sheet(io.BytesIO(blob),
                                           bulkplan.DEVICES_SHEET_NAMES) is not None
            return self._send(200, {"status": "ok", "stage": "review",
                                    "review": review,
                                    "device_review": device_review,
                                    "has_devices_tab": has_tab,
                                    "devices_read": len(new_devices),
                                    "prefs": prefs})

        # Snapshot under the lock: deep-copying a dict that another thread is
        # committing into can raise "dictionary changed size during iteration".
        # The planning itself then runs outside the lock, on a private copy.
        with WRITE_LOCK:
            snapshot = copy.deepcopy(TOPOLOGY)
        result = bulkplan.plan(snapshot, demands, prefs, already_isolated=True,
                               new_devices=new_devices,
                               constraints=constraints)
        result["new_devices"] = new_devices

        plan_id = f"PLAN-{len(PLANS) + 1:04d}"
        PLANS[plan_id] = result
        # the routes themselves stay server-side; the browser gets the summary
        # it needs to render the table and decide whether to commit
        siting = result.get("siting")
        return self._send(200, {
            "status": "ok", "stage": "plan", "plan_id": plan_id,
            "prefs": result["prefs"], "summary": result["summary"],
            "review": review, "device_review": device_review,
            # the ranked alternatives are large and only the chosen spot is
            # shown, so they stay server-side with the plan
            "siting": [{k: v for k, v in p.items() if k != "options"}
                       for p in siting["placements"]] if siting else None,
            "results": [{k: v for k, v in r.items() if k != "route"}
                        for r in result["results"]],
        })

    def _bulk_execute(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid JSON"})

        plan = PLANS.get(req.get("plan_id"))
        if not plan:
            return self._send(404, {"error": "that plan is no longer available — re-upload the sheet"})

        with WRITE_LOCK:
            pending = [r for r in plan["results"] if r.get("status") == "ok"]
            if not pending:
                done = sum(1 for r in plan["results"] if r.get("status") == "committed")
                return self._send(409, {"error": f"nothing left to commit in this plan "
                                                 f"({done} row(s) already committed)."})

            # rack the new kit before cabling it — the routes reference ports
            # that only exist once the device does
            racked = bulkplan.execute_devices(TOPOLOGY, plan.get("siting"),
                                              plan.get("new_devices", []))
            outcome = bulkplan.execute(TOPOLOGY, plan["results"])
            outcome["devices"] = racked
            # mark before releasing the lock, so a second click on the same plan
            # cannot slip in and commit the same rows twice
            committed_rows = {c["row"] for c in outcome["committed"]}
            for r in plan["results"]:
                if r.get("row") in committed_rows:
                    r["status"] = "committed"
            if outcome["committed"]:
                pathengine.save_topology(TOPOLOGY)
        return self._send(200, {"status": "ok", **outcome})

    def log_message(self, fmt, *args):
        pass  # keep the console clean


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print("=" * 58)
    print("  ITA Lab — physical layer route planner")
    print("=" * 58)
    print(f"  Loaded : {len(TOPOLOGY['racks'])} racks, {len(TOPOLOGY['ports'])} ports, "
          f"{len(TOPOLOGY.get('circuits', {}))} circuits")
    print(f"  Open   : {url}")
    print("  Stop   : Ctrl+C")
    print("=" * 58)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
