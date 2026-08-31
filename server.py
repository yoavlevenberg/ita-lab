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
  POST /api/bulk/row           {plan_id, row, count} -> full detail for one
                               planned row plus alternatives for it, computed
                               against the rest of the plan
  POST /api/bulk/choose        {plan_id, row, option} -> makes one of those
                               alternatives the row's plan
  POST /api/bulk/execute       {plan_id} -> commits every planned row, each
                               revalidated first; a stale row fails alone
  POST /api/truncate           {circuit_id | port} -> pulls only the LAST hop,
                               leaving the rest of the path and a loose end
  POST /api/extend             {circuit_id, dst} -> gives a truncated
                               connection a new far end from its loose end
  POST /api/decommission       {circuit_id | port | plan_id | device | rack}
                               -> releases connections: one, or every one on a
                               device/rack, or a whole executed plan. Strands
                               and ports go back only if that circuit really
                               holds them
  GET  /api/workorder?...      printable Work Order (HTML) for one route
  GET  /api/sample?kind=...    a demand sheet (.xlsx) built against the live
                               map, so its ports are free right now
  GET  /api/search?q=...       find a port, device, cabinet, pod or circuit by
                               serial, id, name or label — typo-tolerant
  GET  /api/capacity           what is filling up: trunk strands by kind and
                               the tightest individual trunks and cabinets
  GET  /api/topology[?full=1]  the map WITHOUT its 120,256 ports (1.9MB rather
                               than 26MB), plus ready-made port counts per
                               cabinet and pod; ?full=1 for everything
  GET  /api/rack?id=A1-S05     that cabinet's ports, fetched when it is opened
  GET  /api/stats              the port counts alone — the single source of
                               truth for the numbers the interface shows
"""

import contextlib
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
import capacity
import make_sample_sheet
import pathengine
import placement
import search as sitesearch
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
#
# Capped, because they were not: every sheet ever uploaded stayed for the life
# of the process, each holding full route objects, and one of them now also
# holds a working copy of the map. A day of use grew without limit.
PLANS = {}
MAX_PLANS = 20            # whole plans kept addressable
MAX_PLAN_STATES = 3       # working maps kept — these are the 32MB ones

# Bumped by anything that changes the live map. A plan's working copy is built
# from the map, so it is only trustworthy while this has not moved.
MAP_VERSION = 0


def _map_changed():
    global MAP_VERSION
    MAP_VERSION += 1


_STATE_SEQ = 0


def _next_state_seq():
    global _STATE_SEQ
    _STATE_SEQ += 1
    return _STATE_SEQ


def _trim_plan_states(keep=None):
    """Keep only the few most recently USED working maps. A plan without one is
    not lost — it rebuilds on next use — but a plan holding one costs 32MB.

    Ordered by last use, not by when the sheet was uploaded: ordering by upload
    threw away the map belonging to the plan being looked at, whenever that
    plan was not the newest one.
    """
    holders = [p for p in PLANS.values() if p.get("_state") and p is not keep]
    holders.sort(key=lambda p: p["_state"]["seq"])
    budget = MAX_PLAN_STATES - (1 if keep is not None else 0)
    for stale in holders[:max(0, len(holders) - budget)]:
        stale.pop("_state", None)


def _remember_plan(plan_id, plan):
    PLANS[plan_id] = plan
    for old in list(PLANS)[:-MAX_PLANS]:
        PLANS.pop(old, None)

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

    # A truncated circuit has no far end; it stops at its open end. Reading one
    # used to crash here, because every circuit was assumed to have both.
    far = circuit.get("b_port") or circuit.get("open_end")
    return {
        "cable_type": ct_name, "domain": circuit["domain"],
        "src_port": circuit["a_port"], "dst_port": far,
        "src_location": pathengine.describe_port(TOPOLOGY, circuit["a_port"]),
        "dst_location": (pathengine.describe_port(TOPOLOGY, far) if far
                         else "— אין קצה, המסלול קטום"),
        "partial": bool(circuit.get("partial")),
        "hop_racks": circuit["hop_racks"], "segments": segments,
        "transit_points": [{"port": p, "rack": TOPOLOGY["ports"][p]["rack"],
                            "location": pathengine.describe_port(TOPOLOGY, p)}
                           for p in circuit.get("transit_ports", [])],
        "total_length_m": circuit["total_length_m"],
        # a truncated connection has to be able to say where it stops, or the
        # page can only report that something is missing
        "partial": bool(circuit.get("partial")),
        "open_end": circuit.get("open_end"),
        "open_end_location": (pathengine.describe_port(TOPOLOGY, circuit["open_end"])
                              if circuit.get("open_end") else None),
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
            # Ports are 23.3MB of the map's 32.5MB, and the browser looks at
            # one cabinet's worth at a time. They are fetched per cabinet
            # instead (/api/rack), and the counts the interface needs come
            # ready-made — computed from the map the router itself uses, so
            # what is on screen cannot drift from what the router sees.
            #
            # ?full=1 still returns everything, for anything that genuinely
            # wants the whole map.
            if parse_qs(url.query).get("full", ["0"])[0] in ("1", "true", "on"):
                return self._send(200, TOPOLOGY)
            slim = {k: v for k, v in TOPOLOGY.items()
                    if k not in ("ports", "circuits")}
            slim["stats"] = capacity.port_stats(TOPOLOGY)
            slim["slim"] = True
            return self._send(200, slim)

        if url.path == "/api/stats":
            # The one source of truth for the counts on screen. The browser
            # used to add and subtract them itself after every commit and
            # release, which is arithmetic that can only drift — and drift
            # silently, since nothing compares it to anything.
            return self._send(200, capacity.port_stats(TOPOLOGY))

        if url.path == "/api/rack":
            rid = parse_qs(url.query).get("id", [None])[0]
            if rid not in TOPOLOGY["racks"]:
                return self._send(404, {"error": f"no cabinet {rid} on the map"})
            # Addressed through the cabinet's own devices rather than by
            # sweeping the map: a port id is <device>:<index>, so this costs
            # the ~190 ports the cabinet has instead of all 120,256.
            ports = {}
            for d in TOPOLOGY["devices"].values():
                if d["rack"] != rid:
                    continue
                for i in range(1, d["fiber_ports"] + d["copper_ports"] + 1):
                    p = TOPOLOGY["ports"].get(f"{d['id']}:{i}")
                    if p is None:
                        continue
                    # A patched port's far end is almost always in a DIFFERENT
                    # cabinet, and the page holds one cabinet at a time — so it
                    # is described here rather than looked up there. Without
                    # this the panel would fall back to printing a raw port id.
                    if p.get("peer"):
                        p = {**p, "peer_location":
                             pathengine.describe_port(TOPOLOGY, p["peer"])}
                    ports[p["id"]] = p
            return self._send(200, {"rack": rid, "ports": ports})

        if url.path == "/api/zones":
            return self._send(200, {
                "pods": zones.summary(TOPOLOGY),
                "colours": zones.COLOURS,
                "audit": zones.audit(TOPOLOGY),
            })

        if url.path == "/api/sample":
            return self._sample(url)

        if url.path == "/api/capacity":
            return self._send(200, capacity.report(TOPOLOGY))

        if url.path == "/api/search":
            q = parse_qs(url.query)
            return self._send(200, sitesearch.search(
                TOPOLOGY, q.get("q", [""])[0],
                limit=max(1, min(int(q.get("limit", ["25"])[0]), 50))))

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
            # a truncated connection has no far end, and has to be able to say
            # where it does stop — otherwise the page can only report that
            # something is missing
            enriched["b_location"] = (pathengine.describe_port(TOPOLOGY, circuit["b_port"])
                                      if circuit.get("b_port") else "— אין קצה, המסלול קטום")
            enriched["open_end_location"] = (
                pathengine.describe_port(TOPOLOGY, circuit["open_end"])
                if circuit.get("open_end") else None)
            # Ready-to-draw stops, the same shape a planned row carries. The
            # browser holds one cabinet's ports at a time and a circuit crosses
            # several, so it cannot look these up for itself.
            route = _route_of_circuit(cid)
            enriched["jump"] = bulkplan._jump_chain(TOPOLOGY, route) if route else []
            enriched["transit_locations"] = [
                pathengine.describe_port(TOPOLOGY, p)
                for p in circuit.get("transit_ports", [])]
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
                if req.get("direct"):
                    # "is there a single cable I can patch here" is a different
                    # question from "how do I get there", and a six-hop answer
                    # to it is wrong rather than merely long
                    options = [pathengine.direct_route(TOPOLOGY, src, dst)]
                else:
                    options = pathengine.resolve_route_options(
                        src, dst, domain=domain, count=count, topology=TOPOLOGY)
            except pathengine.RouteError as e:
                return self._send(200, {"status": "failed", "reason": str(e)})

            for route in options:
                route["work_order"] = workorder.render(route)
                route["route_key"] = _cache_route(route)
                # ready-to-draw stops: the browser holds one cabinet's ports at
                # a time, and a route crosses several
                route["jump"] = bulkplan._jump_chain(TOPOLOGY, route)
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
                _map_changed()
            work_order = workorder.render(route, circuit_id=circuit_id)
            return self._send(200, {"status": "ok", "circuit": circuit, "work_order": work_order})

        if url.path == "/api/bulk/plan":
            return self._bulk_plan(url)

        if url.path == "/api/truncate":
            return self._truncate()

        if url.path == "/api/extend":
            return self._extend()

        if url.path == "/api/decommission":
            return self._decommission()

        if url.path == "/api/bulk/row":
            return self._bulk_row()

        if url.path == "/api/bulk/choose":
            return self._bulk_choose()

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
        _remember_plan(plan_id, result)
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
            # the full route object stays server-side; "jump" is the small
            # ready-to-draw form the diagram needs
            "results": [{k: v for k, v in r.items() if k != "route"}
                        for r in result["results"]],
        })

    def _sample(self, url):
        """A demand sheet filled with ports that are free RIGHT NOW.

        A checked-in example stops being valid the moment a plan is executed,
        which is exactly when someone reaches for it — so the file is built on
        request, against the live map, and never stored.
        """
        kind = (parse_qs(url.query).get("kind", ["clean"])[0] or "clean").lower()
        builders = {
            "clean":   (lambda: {"P2P": make_sample_sheet.build_clean(TOPOLOGY)},
                        "sample_demand.xlsx"),
            "devices": (lambda: make_sample_sheet.build_new_devices(TOPOLOGY),
                        "sample_demand_new_devices.xlsx"),
            "errors":  (lambda: {"P2P": make_sample_sheet.build_broken(TOPOLOGY)},
                        "sample_demand_with_errors.xlsx"),
        }
        if kind not in builders:
            return self._send(404, {"error": f"no sample called '{kind}'"})

        build, filename = builders[kind]
        buf = io.BytesIO()
        with WRITE_LOCK:            # reads the map while a commit may be writing it
            tabs = build()
        make_sample_sheet.write_xlsx(buf, tabs)

        body = buf.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.openxmlformats-"
                                         "officedocument.spreadsheetml.sheet")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    @contextlib.contextmanager
    def _plan_state(self, plan, exclude_row=None):
        """The plan's working map with one row lifted out, for as long as you
        hold it. That is the state the row was routed in, and the state any
        alternative for it has to fit into — an alternative that collided with
        a sibling row of the same sheet would be no alternative at all.

        A context manager because the map is now shared and mutated rather
        than copied: the row is released on the way in and put back on the way
        out, under the plan's own lock so two requests cannot interleave.
        """
        with plan.setdefault("_lock", threading.Lock()):
            work = self._plan_base(plan)
            cid = f"PLAN-{exclude_row:04d}" if exclude_row is not None else None
            if cid and cid in (work.get("circuits") or {}):
                pathengine.decommission_route(work, cid)
            try:
                yield work
            finally:
                # put back whatever the row holds NOW — _bulk_choose may have
                # swapped its route while we were inside
                row = next((r for r in plan["results"]
                            if r["row"] == exclude_row), None)
                if cid and row and row.get("status") == "ok" and row.get("route"):
                    try:
                        pathengine.commit_route(work, row["route"], cid)
                    except (KeyError, pathengine.RouteError):
                        # rather than leave a map that quietly disagrees with
                        # the plan, throw it away and rebuild on next use
                        plan.pop("_state", None)

    def _plan_base(self, plan):
        """This plan's map, with every pending row applied."""
        # Built once per plan and kept with it. Every pending row is committed
        # into it as PLAN-xxxx, so the map already looks the way the plan
        # intends; a request that needs one row left out releases just that
        # row and puts it back afterwards.
        #
        # This used to deep-copy the whole 32MB map on every call, half a
        # second each, so opening a row or asking for another option paid it
        # every single time. Releasing one circuit is a handful of dictionary
        # edits — which only became possible once connections could be
        # released at all.
        #
        # Dropped whenever the live map moves underneath it: an alternative
        # computed against a stale map is worse than a slow one.
        cached = plan.get("_state")
        if cached is not None and cached["version"] == MAP_VERSION:
            cached["seq"] = _next_state_seq()      # touched, so keep it longer
            return cached["map"]

        with WRITE_LOCK:
            base = copy.deepcopy(TOPOLOGY)

        siting = plan.get("siting")
        if siting:
            specs = {d["serial"]: d for d in plan.get("new_devices", [])}
            known = set(bulkplan.serials.index(base))
            for site in siting["placements"]:
                if site["status"] == "ok" and site["serial"] not in known:
                    bulkplan.materialise(base, specs[site["serial"]], site)

        for r in plan["results"]:
            if r.get("status") != "ok" or not r.get("route"):
                continue
            try:
                pathengine.commit_route(base, r["route"], f"PLAN-{r['row']:04d}")
            except (KeyError, pathengine.RouteError):
                pass

        plan["_state"] = {"version": MAP_VERSION, "map": base,
                          "seq": _next_state_seq()}
        _trim_plan_states(keep=plan)
        # returned from the local, not re-read from the plan: a bug in trimming
        # should never be able to turn this into a KeyError at the call site
        return base

    def _decommission(self):
        """Release connections. One circuit, or every circuit on a device, a
        rack, or a whole executed plan.

        The map used to be append-only, so a wrong execution could not be taken
        back and the lab filled up with test circuits until routing failed.
        """
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid JSON"})

        with WRITE_LOCK:
            wanted = self._circuits_named_by(req)
            if isinstance(wanted, dict):        # an error the helper built
                return self._send(wanted.pop("code", 400), wanted)
            if not wanted:
                return self._send(404, {"error": "nothing to release — "
                                                 "no matching connection"})

            # "Are you sure?" with no detail is not a question. A dry run lets
            # the browser name what is about to go before it asks.
            if req.get("dry_run"):
                return self._send(200, {"status": "ok", "would_release": [
                    f"{cid}  {pathengine.describe_port(TOPOLOGY, circuits[cid]['a_port'])}"
                    f"  ⇄  {pathengine.describe_port(TOPOLOGY, circuits[cid]['b_port'])}"
                    for cid in wanted
                    for circuits in [TOPOLOGY["circuits"]]]})

            released, refused = [], []
            for cid in wanted:
                try:
                    released.append(pathengine.decommission_route(
                        TOPOLOGY, cid, force=bool(req.get("force"))))
                except pathengine.DecommissionError as e:
                    refused.append({"circuit_id": cid, "reason": str(e)})
            if released:
                pathengine.save_topology(TOPOLOGY)
                _map_changed()

        # a plan whose circuits are gone must not still offer to reopen them
        for plan in PLANS.values():
            for r in plan.get("results", []):
                if r.get("circuit_id") in {x["circuit_id"] for x in released}:
                    r["status"] = "ok"
                    r.pop("circuit_id", None)

        return self._send(200, {"status": "ok", "released": released,
                                "refused": refused,
                                "freed_ports": sorted({p for r in released
                                                       for p in r["ports"]})})

    def _truncate(self):
        """Pull only the last leg of a connection, keeping the rest of the path.

        The expensive part of a route is the backbone strands already pulled
        between rooms; when only the far end is wrong, that should not be torn
        out to change a patch lead.
        """
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid JSON"})

        cid = req.get("circuit_id")
        if not cid and req.get("port"):
            p = TOPOLOGY["ports"].get(req["port"])
            if p is None:
                return self._send(404, {"error": f"no port {req['port']} on the map"})
            cid = p.get("circuit")
        if not cid:
            return self._send(400, {"error": "circuit_id or port is required"})

        with WRITE_LOCK:
            try:
                rec = pathengine.truncate_route(TOPOLOGY, cid)
            except pathengine.DecommissionError as e:
                return self._send(409, {"error": str(e)})
            pathengine.save_topology(TOPOLOGY)
            _map_changed()
        return self._send(200, {"status": "ok", **rec,
                                "circuit": TOPOLOGY["circuits"][cid]})

    def _extend(self):
        """Give a truncated connection a new far end."""
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid JSON"})

        cid, dst = req.get("circuit_id"), req.get("dst")
        if not cid or not dst:
            return self._send(400, {"error": "circuit_id and dst are required"})

        with WRITE_LOCK:
            try:
                circuit = pathengine.extend_route(TOPOLOGY, cid, dst)
            except (pathengine.RouteError, KeyError) as e:
                return self._send(409, {"error": str(e)})
            pathengine.save_topology(TOPOLOGY)
            _map_changed()
        return self._send(200, {"status": "ok", "circuit": circuit,
                                "work_order": workorder.render(
                                    _route_of_circuit(cid), circuit_id=cid)})

    def _circuits_named_by(self, req):
        """Which circuits a release request is asking about. One id, or every
        circuit on a port's device, in a rack, or committed by a plan — the
        difference between 'unplug this' and 'clear all of this'."""
        circuits = TOPOLOGY.get("circuits") or {}

        if req.get("circuit_id"):
            cid = req["circuit_id"]
            return [cid] if cid in circuits else {
                "code": 404, "error": f"no circuit {cid} on the map"}

        if req.get("port"):
            p = TOPOLOGY["ports"].get(req["port"])
            if p is None:
                return {"code": 404, "error": f"no port {req['port']} on the map"}
            if not p.get("circuit"):
                return {"code": 409, "error": f"port {req['port']} is not patched"}
            return [p["circuit"]]

        if req.get("plan_id"):
            plan = PLANS.get(req["plan_id"])
            if not plan:
                return {"code": 404, "error": "that plan is no longer available"}
            return [r["circuit_id"] for r in plan["results"]
                    if r.get("circuit_id") in circuits]

        # everything on one device, or one rack — "remove all" at the scope the
        # user is actually looking at
        scope = req.get("device") or req.get("rack")
        if scope:
            key = "device" if req.get("device") else "rack"
            here = {p["circuit"] for p in TOPOLOGY["ports"].values()
                    if p.get(key) == scope and p.get("circuit")}
            return sorted(c for c in here if c in circuits)

        return {"code": 400, "error": "say what to release: circuit_id, port, "
                                      "plan_id, device or rack"}

    def _bulk_row(self):
        """Full detail for one planned row, and alternatives for it — the same
        thing a single port-to-port lookup shows, for a row that came from a
        sheet."""
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid JSON"})

        plan = PLANS.get(req.get("plan_id"))
        if not plan:
            return self._send(404, {"error": "that plan is no longer available — re-upload the sheet"})
        try:
            row = int(req.get("row"))
        except (TypeError, ValueError):
            return self._send(400, {"error": "row is required"})
        count = max(1, min(int(req.get("count") or 2), 8))

        result = next((r for r in plan["results"] if r["row"] == row), None)
        if not result:
            return self._send(404, {"error": f"row {row} is not in this plan"})
        if not result.get("route"):
            return self._send(409, {"error": result.get("reason")
                                    or f"row {row} was never planned"})

        chosen = result["route"]
        # A committed row is history: show what was actually installed rather
        # than offering choices that can no longer be taken.
        if result.get("status") == "committed":
            chosen = dict(chosen)
            chosen["work_order"] = workorder.render(
                chosen, circuit_id=result.get("circuit_id"))
            chosen["option_index"] = 1
            chosen.setdefault("shared_segments", [])
            return self._send(200, {"status": "ok", "row": row, "locked": True,
                                    "chosen": 0, "options": [chosen]})

        with self._plan_state(plan, exclude_row=row) as work:
            try:
                options = pathengine.resolve_route_options(
                    chosen["src_port"], chosen["dst_port"], count=count, topology=work)
            except pathengine.RouteError as e:
                return self._send(200, {"status": "failed", "reason": str(e)})

        # Which of these is the one the plan is currently holding? Compare the
        # physical path, not object identity — these were computed against the
        # plan's own map and share nothing with the stored route.
        fingerprint = [(s["edge_id"], s["strand_index"]) for s in chosen["segments"]]
        current = next((i for i, o in enumerate(options)
                        if [(s["edge_id"], s["strand_index"]) for s in o["segments"]]
                        == fingerprint), None)
        if current is None:
            # the plan's own route is always an option, even if the router no
            # longer ranks it in the top `count`
            keep = dict(chosen)
            keep["option_index"] = 0
            keep.setdefault("shared_segments", [])
            options.insert(0, keep)
            current = 0

        for route in options:
            route["work_order"] = workorder.render(route)
        return self._send(200, {"status": "ok", "row": row, "locked": False,
                                "chosen": current, "options": options,
                                "more": len(options) >= count})

    def _bulk_choose(self):
        """Swap in one of the alternatives as the row's plan. The plan is what
        execute reads, so this is the only place a choice becomes real."""
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid JSON"})

        plan = PLANS.get(req.get("plan_id"))
        if not plan:
            return self._send(404, {"error": "that plan is no longer available — re-upload the sheet"})
        try:
            row, pick = int(req.get("row")), int(req.get("option"))
        except (TypeError, ValueError):
            return self._send(400, {"error": "row and option are required"})

        result = next((r for r in plan["results"] if r["row"] == row), None)
        if not result or not result.get("route"):
            return self._send(404, {"error": f"row {row} is not a planned row"})
        if result.get("status") == "committed":
            return self._send(409, {"error": f"row {row} is already installed as "
                                             f"{result.get('circuit_id')}"})

        with self._plan_state(plan, exclude_row=row) as work:
            try:
                options = pathengine.resolve_route_options(
                    result["route"]["src_port"], result["route"]["dst_port"],
                    count=max(pick + 1, 2), topology=work)
            except pathengine.RouteError as e:
                return self._send(200, {"status": "failed", "reason": str(e)})
            if not 0 <= pick < len(options):
                return self._send(409, {"error": "that option is no longer available — "
                                                 "reopen the row"})

            route = options[pick]
            # Prove it still fits before it becomes the plan: reserving it on
            # the same state it was computed against is the difference between
            # an option and a promise. Released again straight away so the
            # context manager can put back whichever route the row ends up
            # holding, without it being committed twice.
            try:
                pathengine.commit_route(work, route, f"PLAN-{row:04d}")
            except (KeyError, pathengine.RouteError) as e:
                return self._send(200, {"status": "failed",
                                        "reason": f"could not reserve: {e}"})
            jump = bulkplan._jump_chain(work, route)
            pathengine.decommission_route(work, f"PLAN-{row:04d}")
            result["route"] = route

        result["jump"] = jump
        result["hops"] = len(route["segments"])
        result["cable_type"] = route["cable_type"]
        result["domain"] = route["domain"]
        result["total_length_m"] = route["total_length_m"]
        result["strands"] = [{"edge_id": s["edge_id"], "index": s["strand_index"]}
                             for s in route["segments"]]
        result["note"] = "נבחר ידנית"
        plan["summary"] = bulkplan._summary(plan["results"])
        if plan.get("siting"):
            plan["summary"]["devices_placed"] = plan["siting"]["ok"]
            plan["summary"]["devices_failed"] = plan["siting"]["failed"]

        return self._send(200, {"status": "ok", "row": row,
                                "summary": plan["summary"],
                                "result": {k: v for k, v in result.items() if k != "route"}})

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
            committed_rows = {c["row"]: c["circuit"]["id"] for c in outcome["committed"]}
            for r in plan["results"]:
                if r.get("row") in committed_rows:
                    r["status"] = "committed"
                    # remember which circuit it became: reopening the row later
                    # must show what was installed, not a proposal
                    r["circuit_id"] = committed_rows[r["row"]]
            if outcome["committed"]:
                pathengine.save_topology(TOPOLOGY)
                _map_changed()
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
