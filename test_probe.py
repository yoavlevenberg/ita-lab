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

import base64
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
# not the first candidate, so the probe has to find it
REAL_NETWORK_PATHS = "/api/v8.1/networks/internal/rest/network-paths"
PATHS = [{"portName": SECRET, "connections": [{"target": SECRET, "cableType": "FIBER"}]}]


def make_server(state, login="json", docs="open"):
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
            got = self.headers.get("Authorization")
            if login == "basic":
                return got == "Basic " + base64.b64encode(b"u:p").decode()
            return got == "Bearer T0K"

        def do_GET(self):
            state["methods"].append("GET")
            path, _, query = self.path.partition("?")
            if docs == "basic" and path.startswith("/api/current/docs/") and not self._authed():
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="ita"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if docs == "login" and path.startswith("/api/current/docs/"):
                self.send_response(302)
                self.send_header("Location", "/login?next=docs")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path == "/login":
                return self._send(200, b"<html>sign in</html>", "text/html")
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
            if path == REAL_NETWORK_PATHS:
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
    def __init__(self, server, asset=None, spec=None):
        self.server, self.user, self.asset, self.insecure = server, "u", asset, False
        self.spec = spec


def run_probe(login="json", asset=None, password="p", docs="open", suffix="", spec=None):
    state = {"methods": [], "asked": [], "include": []}
    srv = make_server(state, login, docs)
    lines = []
    try:
        code = ita_probe.run(Args(f"http://127.0.0.1:{srv.server_port}{suffix}", asset, spec),
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
    check("it finds which candidate path is the endpoint",
          "PATH   : /api/networks/internal/rest/network-paths -> HTTP 404" in out
          and f"PATH   : {REAL_NETWORK_PATHS} -> HTTP 400" in out, out)
    check("it finds the parameter name the server actually answers to",
          "parameter 'assetId' -> HTTP 200" in out, out)
    check("it reports field names and types",
          "connections[].cableType: str" in out and "portName: str" in out, out)
    check("the unconfirmed guesses are tried with GET and reported by code",
          "GUESS  : /api/current/connections -> HTTP 404" in out, out)
    check("no real value reaches the screen", SECRET not in out and "pp-1" not in out, out)
    check("nothing but GET and the one login POST was sent",
          set(state["methods"]) <= {"GET", "POST /api/current/token"}, state["methods"])

    code, out, state = run_probe(asset="srv-1")
    check("--asset skips the walk and asks about the id given",
          code == 0 and state["asked"] == ["srv-1"], (code, state["asked"]))

    code, out, state = run_probe(password="wrong")
    check("a wrong password fails plainly, before any asset is touched",
          code == 1 and "LOGIN  : FAILED" in out and not state["asked"], out)

    # what the real server turned out to want (an old curl script, 2026-11-04):
    # Basic on every request, the docs included
    code, out, state = run_probe(login="basic", docs="basic")
    check("Basic everywhere, docs too: finds the spec, logs in, sends no POST",
          code == 0 and "SPEC   : ok" in out and "LOGIN  : ok (basic)" in out
          and state["asked"] == ["pp-1"]
          and not any(m.startswith("POST") for m in state["methods"]), out)

    code, out, state = run_probe(suffix="/api/current/docs/index.html#/assets")
    check("a Swagger URL pasted whole as --server still works",
          code == 0 and "SPEC   : ok" in out, out)

    code, out, state = run_probe(docs="login")
    check("docs behind a login page: says where each try went, no host",
          code == 2 and "redirected to /login" in out and "127.0.0.1" not in out
          and "TRY    : http /api/current/docs/final.json" in out, out)

    saved = ita_probe.OUT / "_test_saved_spec.json"
    ita_probe.OUT.mkdir(exist_ok=True)
    saved.write_text(json.dumps(SPEC), encoding="utf-8-sig")    # Chrome's Ctrl+S may add a BOM
    try:
        code, out, state = run_probe(docs="login", spec=str(saved))
    finally:
        saved.unlink()
    check("docs behind a login + spec saved from Chrome: the whole run works",
          code == 0 and "LOGIN  : ok (json)" in out and state["asked"] == ["pp-1"], out)

    code, out, state = run_probe(password="wrong")
    check("a failed login says what each attempt got back",
          "TRY    : login json /api/current/token -> HTTP 401" in out
          and "127.0.0.1" not in out, out)

    lines = []
    code = ita_probe.run(Args("http://127.0.0.1:9"), ask_password=lambda _: "p",
                         log=lines.append)
    out = "\n".join(lines)
    check("an address nobody answers on: says so instead of 'not found' alone",
          code == 2 and "connection refused" in out and "127.0.0.1" not in out, out)

    check("the server address is cut down to scheme and host",
          ita_probe._root("10.1.2.3:8443/api/current/docs/index.html") == "https://10.1.2.3:8443"
          and ita_probe._root("http://ita/x/y") == "http://ita", ita_probe._root("http://ita/x/y"))

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
