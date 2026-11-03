#!/usr/bin/env python3
"""
test_probe.py
=============
ita_probe.py against a fake ITA, because the real one is on the far side of
the air gap and the probe gets one run there. The fake mirrors what the real
server showed on 2026-11-03: a Swagger page linking final.json, an asset tree,
and network-paths answering 400 "...arg0 must not be null" to a wrong
parameter name. How the real one logs in is unknown — so the fake offers one
method, and the checks prove the probe finds it rather than assumes it.

    python test_probe.py
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ita_probe

RESULTS = []
SECRET = "real-site-name-SECRET"     # must never reach the screen


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" — {detail}"))


SPEC = {
    "swagger": "2.0", "info": {"version": "8.1"}, "basePath": "/api/current",
    "paths": {
        "/assets": {"get": {}},
        "/assets/types": {"get": {}},
        "/assets/{id}": {"get": {"parameters": [
            {"name": "include", "in": "query", "type": "array",
             "items": {"enum": ["children", "custom_properties", "device_links"]}}]},
            "delete": {}},
        "/oauth/token/revoke": {"post": {}},
        "/token": {"post": {}},
        "/users/{id}/token": {"post": {}},
    },
}
TREE = {
    "room-1": {"id": "room-1", "type": "room", "name": SECRET,
               "children": ["rack-1"]},
    "rack-1": {"id": "rack-1", "type": "rack", "name": SECRET,
               "children": [{"id": "pp-1"}, {"id": "srv-1"}]},
    "pp-1": {"id": "pp-1", "type": "patchpanel", "name": SECRET},
    "srv-1": {"id": "srv-1", "type": "server", "name": SECRET},
}
PATHS = [{"portName": SECRET, "connections": [{"target": SECRET, "cableType": "FIBER"}]}]


def make_server(state, login="json"):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj, ctype="application/json"):
            body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self):
            return self.headers.get("Authorization") == "Bearer T0K"

        def do_GET(self):
            state["methods"].append("GET")
            path, _, query = self.path.partition("?")
            if path == "/api/current/docs/index.html":
                return self._send(200, b'<script>url: "./final.json"</script>', "text/html")
            if path == "/api/current/docs/final.json":
                return self._send(200, SPEC)
            if not self._authed():
                return self._send(401, {"error": "unauthorized"})
            if path == "/api/current/assets/types":
                return self._send(200, ["room", "rack"])
            if path == "/api/current/assets":
                return self._send(200, {"items": [TREE["room-1"]]})
            if path.startswith("/api/current/assets/"):
                aid = path.rsplit("/", 1)[1]
                state["include"].append(query)
                return self._send(200, TREE[aid]) if aid in TREE else self._send(404, {})
            if path == ita_probe.NETWORK_PATHS:
                if query.startswith("assetId="):
                    state["asked"].append(query.split("=", 1)[1])
                    return self._send(200, PATHS)
                return self._send(400, {"title": "invalid", "invalid-parameters": [
                    {"reason": "must not be null",
                     "property": "getPortConfigurationsWithConnectionsAndPath.arg0"}]})
            return self._send(404, {})

        def do_POST(self):
            state["methods"].append("POST " + self.path)
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if login == "json" and self.path == "/api/current/token":
                creds = json.loads(body.decode())
                if creds == {"username": "u", "password": "p"}:
                    return self._send(200, {"token": "T0K"})
            return self._send(401, {})

        def do_DELETE(self):
            state["methods"].append("DELETE")
            self._send(200, {})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Args:
    def __init__(self, server, asset=None):
        self.server, self.user, self.asset, self.insecure = server, "u", asset, False


def run_probe(login="json", asset=None, password="p"):
    state = {"methods": [], "asked": [], "include": []}
    srv = make_server(state, login)
    lines = []
    try:
        code = ita_probe.run(Args(f"http://127.0.0.1:{srv.server_port}", asset),
                             ask_password=lambda _: password, log=lines.append)
    finally:
        srv.shutdown()
    return code, "\n".join(lines), state


def main():
    code, out, state = run_probe()
    check("a full run succeeds against the fake", code == 0, out)
    check("it reads the spec link out of the Swagger page", "SPEC   : ok, version 8.1" in out, out)
    check("it logs in through the POST the spec lists, not a guessed one",
          "LOGIN  : ok (json)" in out
          and "POST /api/current/token" in state["methods"]
          and not any("revoke" in m or "/users/" in m for m in state["methods"]), state["methods"])
    check("it asks for children with the spec's own include parameter",
          any(q == "include=children" for q in state["include"]), state["include"])
    check("it walks the tree down to the patch panel and asks about THAT one",
          state["asked"] == ["pp-1"], state["asked"])
    check("it finds the parameter name the server actually answers to",
          "parameter 'assetId' -> HTTP 200" in out, out)
    check("it reports field names and types",
          "connections[].cableType: str" in out and "portName: str" in out, out)
    check("no real value reaches the screen", SECRET not in out and "pp-1" not in out, out)
    check("nothing but GET and the one login POST was sent",
          set(state["methods"]) <= {"GET", "POST /api/current/token"}, state["methods"])

    code, out, state = run_probe(asset="srv-1")
    check("--asset skips the walk and asks about the id given",
          code == 0 and state["asked"] == ["srv-1"], (code, state["asked"]))

    code, out, state = run_probe(password="wrong")
    check("a wrong password fails plainly, before any asset is touched",
          code == 1 and "LOGIN  : FAILED" in out and not state["asked"], out)

    # the guard itself: a write cannot leave, whoever asks for it
    p = ita_probe.Probe("http://127.0.0.1:9")
    refused = []
    for method in ("DELETE", "PUT", "PATCH", "POST"):
        try:
            p._request(method, "http://127.0.0.1:9/api/current/assets/x")
        except ita_probe.Refused:
            refused.append(method)
    check("every write is refused before it is sent", refused == ["DELETE", "PUT", "PATCH", "POST"],
          refused)

    print(f"\n{sum(RESULTS)}/{len(RESULTS)} probe checks passed")
    raise SystemExit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
