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
  POST /api/route              {src, dst, domain?} -> proposed route + work order
  POST /api/execute            {route} -> commits a previously-proposed route,
                                consuming ports/trunk capacity and persisting
                                it to topology.json as a real circuit; also
                                returns a re-rendered Work Order stamped
                                EXECUTED with the new circuit id
  GET  /api/circuit?id=CIR-1   an existing circuit's path (for SHOW ROUTE on a used port)
"""

import json
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import pathengine
import workorder

HERE = Path(__file__).parent
PORT = 8800

TOPOLOGY = pathengine.load_topology()


class Handler(BaseHTTPRequestHandler):

    def _send(self, code, payload, content_type="application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)

        if url.path in ("/", "/index.html"):
            html = (HERE / "ui.html").read_bytes()
            return self._send(200, html, "text/html; charset=utf-8")

        if url.path == "/api/topology":
            return self._send(200, TOPOLOGY)

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

    def do_POST(self):
        url = urlparse(self.path)

        if url.path == "/api/route":
            length = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "invalid JSON"})

            src, dst = req.get("src"), req.get("dst")
            domain = req.get("domain") or None
            if not src or not dst:
                return self._send(400, {"error": "src and dst are required"})

            try:
                route = pathengine.resolve_path(src, dst, domain=domain, topology=TOPOLOGY)
            except pathengine.RouteError as e:
                return self._send(200, {"status": "failed", "reason": str(e)})

            route["work_order"] = workorder.render(route)
            return self._send(200, route)

        if url.path == "/api/execute":
            length = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "invalid JSON"})

            route = req.get("route")
            if not route:
                return self._send(400, {"error": "route is required"})

            try:
                pathengine.revalidate_route(TOPOLOGY, route)
            except pathengine.RouteError as e:
                return self._send(409, {"error": str(e)})

            circuit_id = pathengine.next_circuit_id(TOPOLOGY)
            circuit = pathengine.commit_route(TOPOLOGY, route, circuit_id)
            pathengine.save_topology(TOPOLOGY)
            work_order = workorder.render(route, circuit_id=circuit_id)
            return self._send(200, {"status": "ok", "circuit": circuit, "work_order": work_order})

        return self._send(404, {"error": "not found"})

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
