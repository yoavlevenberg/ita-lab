#!/usr/bin/env python3
"""
test_sync.py
============
ita_sync.py against a fake ITA built from what the real one showed on
2026-11-04: Basic login everywhere, /api/v8.1/assets with children, a search
by name, and network-paths answering with the exact shape typed out from
Chrome (portConfigurations > connectionsAndPaths > connection / networkPath).

The fake site mixes the layouts the real ones have: two rows of different
length, and one rack standing on its own outside any row.

    python test_sync.py
"""

import base64
import json
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import ita_sync

RESULTS = []
SECRET = "SECRET-NAME"


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" — {detail}"))


def asset(aid, kind, name, kids=()):
    return {"id": aid, "type": kind, "name": name, "serialNumber": SECRET,
            "children": [{"id": k} for k in kids]}


TREE = {a["id"]: a for a in [
    asset("site", "location", "SITE-A", ["room"]),
    asset("room", "server-room", SECRET, ["row1", "row2", "rack4"]),
    asset("row1", "row", SECRET, ["rack1", "rack2"]),
    asset("row2", "row", SECRET, ["rack3"]),
    asset("rack1", "rack", "R-ONE", ["pp1", "sw1"]),
    asset("rack2", "rack", "DUP", ["srv1"]),
    asset("rack3", "rack", "DUP"),
    asset("rack4", "rack", SECRET, ["pp2"]),          # outside any row
    asset("pp1", "patch-panel", SECRET),
    asset("sw1", "network", SECRET),
    asset("srv1", "server", SECRET),
    asset("pp2", "patch-panel", SECRET),
]}


def end(aid, port):
    return {"networkModule": "m1", "portNumber": port, "rearPort": False,
            "connectorType": "LC", "ipAddress": SECRET, "description": SECRET,
            "assetId": aid, "assetName": SECRET, "assetType": "patch-panel",
            "assetPath": [{"id": "site", "name": SECRET, "permission": "R",
                           "elementType": "location"}]}


CABLE = {"name": SECRET, "length": 3, "speed": "10G", "speedRate": 10, "color": "blue",
         "startConnector": "LC", "endConnector": "LC", "isFanOut": False,
         "fanOutOutletsCount": 0, "fanOutOutletNumber": 0}


def paths_for(aid):
    link = {"id": "c1", "source": end(aid, 1), "destination": end("pp2", 1), "cableType": CABLE}
    return {"portConfigurations": [{
        "portConfiguration": {"id": "pc1", "assetId": aid, "name": SECRET,
                              "connectorType": "LC", "portCount": 24, "portStart": 1,
                              "speed": "10G", "speedRate": 10, "portsUsed": 1},
        "connectionsAndPaths": [{
            "connection": link,
            "networkPath": {"connections": [link], "beginItem": end(aid, 1),
                            "endItem": end("pp2", 1)}}]}]}


NETWORK = "/api/networks/internal/rest/network-paths"
BASIC = "Basic " + base64.b64encode(b"u:p").decode()


def make_server(state, network="ok"):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            state["methods"].append("GET")
            path, _, query = self.path.partition("?")
            q = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
            if self.headers.get("Authorization") != BASIC:
                return self._send(401, {"error": "login"})
            if path == "/api/v8.1/assets/search":
                want = q.get("q", "").replace("%20", " ")
                return self._send(200, [{"id": a["id"], "name": a["name"], "type": a["type"]}
                                        for a in TREE.values() if a["name"] == want])
            if path.startswith("/api/v8.1/assets/"):
                aid = path.rsplit("/", 1)[1]
                state["assets"].append(aid)
                return self._send(200, TREE[aid]) if aid in TREE else self._send(404, {})
            if path == NETWORK:
                if network == "deny":
                    return self._send(401, {"error": "session required"})
                if "asset_id" not in q:
                    return self._send(400, {"invalid-parameters": [{
                        "reason": "must not be null",
                        "property": "getPortConfigurationsWithConnectionsAndPath.arg0"}]})
                state["network"].append(q["asset_id"])
                return self._send(200, paths_for(q["asset_id"]))
            return self._send(404, {})

        def do_POST(self):
            state["methods"].append("POST")
            self._send(405, {})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Args:
    def __init__(self, server, out, rack=None, site=None, id=None, limit=None):
        self.server, self.out = server, out
        self.rack, self.site, self.id, self.limit = rack, site, id, limit
        self.user, self.insecure, self.proxy = "u", False, False


def run_sync(network="ok", **kw):
    state = {"methods": [], "assets": [], "network": []}
    srv = make_server(state, network)
    out = Path(tempfile.mkdtemp(prefix="ita_sync_test_"))
    lines = []
    try:
        code = ita_sync.run(Args(f"http://127.0.0.1:{srv.server_port}", str(out), **kw),
                            ask_password=lambda _: "p", log=lines.append)
    finally:
        srv.shutdown()
    return code, "\n".join(lines), state, out


def main():
    code, out, state, cache = run_sync(site="SITE-A")
    site = cache / "SITE-A"
    try:
        check("a whole site syncs", code == 0, out)
        check("every asset in the tree is copied, raw",
              len(list((site / "assets").glob("*.json"))) == len(TREE)
              and json.loads((site / "assets" / "rack1.json").read_text(encoding="utf-8"))
              == TREE["rack1"], out)
        check("connections are fetched for devices only, never for containers",
              sorted(state["network"]) == ["pp1", "pp2", "srv1", "sw1"], state["network"])
        check("each network-paths answer is kept exactly as ITA sent it",
              json.loads((site / "network" / "sw1.json").read_text(encoding="utf-8"))
              == paths_for("sw1"), out)
        check("the counts reach the screen",
              f"ASSETS : {len(TREE)}" in out and "DEVICES: 4" in out
              and "PORTS  : 4 port groups, 4 connected ports" in out, out)
        check("rows of different length are told apart",
              "2 rows, racks per row: 1 racks x1, 2 racks x1" in out, out)
        check("a rack outside any row is counted, not lost",
              "racks not inside a row: 1 of 4" in out, out)
        check("the tree's shape is shown in type names",
              "location > server-room > row > rack" in out, out)
        check("no name, serial, address or id reaches the screen",
              SECRET not in out and "R-ONE" not in out and "rack1" not in out
              and "127.0.0.1" not in out, out)
        check("nothing but GET was sent", set(state["methods"]) == {"GET"}, state["methods"])
        m = json.loads((site / "manifest.json").read_text(encoding="utf-8"))
        check("the manifest records the tree for the next step",
              m["tree"]["rack4"] == {"parent": "room", "type": "rack"}, m.get("tree"))
    finally:
        shutil.rmtree(cache, ignore_errors=True)

    code, out, state, cache = run_sync(rack="R-ONE")
    shutil.rmtree(cache, ignore_errors=True)
    check("--rack copies that rack and what is in it, nothing else",
          code == 0 and sorted(set(state["assets"])) == ["pp1", "rack1", "sw1"],
          state["assets"])

    code, out, state, cache = run_sync(rack="DUP")
    shutil.rmtree(cache, ignore_errors=True)
    check("two racks with one name: stops and asks for --id",
          code == 1 and "--id" in out and not state["network"], out)

    code, out, state, cache = run_sync(rack="NO-SUCH")
    shutil.rmtree(cache, ignore_errors=True)
    check("a name that is not there: says so, nothing walked",
          code == 1 and "no asset with exactly that name" in out and not state["assets"], out)

    code, out, state, cache = run_sync(network="deny", site="SITE-A")
    shutil.rmtree(cache, ignore_errors=True)
    check("network-paths refusing the login: said plainly, assets still saved",
          code == 1 and "refuses this login" in out and f"ASSETS : {len(TREE)}" in out, out)

    code, out, state, cache = run_sync(id="row1")
    shutil.rmtree(cache, ignore_errors=True)
    check("--id starts anywhere in the tree",
          code == 0 and sorted(set(state["assets"])) == ["pp1", "rack1", "rack2", "row1",
                                                         "srv1", "sw1"], state["assets"])

    print(f"\n{sum(RESULTS)}/{len(RESULTS)} sync checks passed")
    raise SystemExit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
