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
# where ITA might keep a cabinet's place on its floor plan (field name made up),
# and a block keyed by ids, whose keys must not reach the screen either
TREE["rack1"]["floorPlacement"] = {"x": 12.5, "y": 3, "rotation": 90}
TREE["rack1"]["links"] = {"3f2a9c1e-77aa-4b1c-9d00-aa11bb22cc33": SECRET}
TREE["rack1"]["rackUnits"] = 42


def typed_back(out):
    """The lines between the bars: what the user types back by hand."""
    lines = out.splitlines()
    try:
        i = lines.index("TYPE BACK ONLY THESE LINES:")
    except ValueError:
        return []
    return lines[i + 1:next(j for j in range(i + 1, len(lines)) if set(lines[j]) == {"-"})]


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
                if (network == "dead" and "asset_id" in q) or (
                        network.startswith("drop:") and q.get("asset_id") == network[5:]):
                    self.close_connection = True
                    return              # hang up without a word
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
    def __init__(self, server, out, rack=None, site=None, id=None, limit=None, check=False):
        self.server, self.out = server, out
        self.rack, self.site, self.id, self.limit = rack, site, id, limit
        self.user, self.insecure, self.proxy, self.check = "u", False, False, check


def run_sync(network="ok", password="p", out=None, **kw):
    state = {"methods": [], "assets": [], "network": []}
    srv = make_server(state, network)
    out = out or Path(tempfile.mkdtemp(prefix="ita_sync_test_"))
    lines = []
    try:
        code = ita_sync.run(Args(f"http://127.0.0.1:{srv.server_port}", str(out), **kw),
                            ask_password=lambda _: password, log=lines.append)
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
        fields = next((l for l in out.splitlines() if l.startswith("FIELDS : rack ")), "")
        check("the field names a rack record carries are listed (numbers marked #)",
              "floorPlacement.x#" in fields and "floorPlacement.rotation#" in fields
              and "children[]" in fields and "name" in fields, out)
        check("...names only: no value, and no key that is an id",
              "12.5" not in fields and "3f2a9c1e" not in fields and "links.<id>" in fields, fields)
        check("no name, serial, address or id reaches the screen",
              SECRET not in out and "R-ONE" not in out and "rack1" not in out
              and "127.0.0.1" not in out, out)
        tb = typed_back(out)
        check("the sync ends with five short lines to type back",
              len(tb) == 5 and all(len(l) <= 120 for l in tb)
              and tb[0] == f"S1 done=yes net=ok assets={len(TREE)} devices=4"
              and tb[1] == "S2 ports=4 conn=4 noNet=0 http=200x4"
              and tb[2] == "S3 problems=0"
              and tb[3].startswith("S4 ") and "> rack" in tb[3], tb or out)
        check("...naming the rack's floor-plan and height fields, if it has them",
              len(tb) == 5
              and "pos=floorPlacement.rotation#,floorPlacement.x#,floorPlacement.y#" in tb[4]
              and "height=rackUnits#" in tb[4] and "42" not in tb[4], tb)
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
    tb = typed_back(out)
    check("...and the lines to type back say it first",
          len(tb) == 5 and tb[0].startswith("S1 done=NO net=refused")
          and tb[2] != "S3 problems=0", tb or out)

    code, out, state, cache = run_sync(id="row1")
    shutil.rmtree(cache, ignore_errors=True)
    check("--id starts anywhere in the tree",
          code == 0 and sorted(set(state["assets"])) == ["pp1", "rack1", "rack2", "row1",
                                                         "srv1", "sw1"], state["assets"])

    # a network hiccup on one device: the copy goes on, and says what it lacks
    code, out, state, cache = run_sync(network="drop:sw1", site="SITE-A")
    try:
        m = json.loads((cache / "SITE-A" / "manifest.json").read_text(encoding="utf-8"))
        check("one request that dies mid-copy is counted, not fatal",
              m["complete"] and m["network_missing"] == ["sw1"]
              and not (cache / "SITE-A" / "network" / "sw1.json").exists()
              and "PROBLEM: network-paths:" in out, out)
    finally:
        shutil.rmtree(cache, ignore_errors=True)

    # a fresh copy carries nothing the last one left behind
    cache = Path(tempfile.mkdtemp(prefix="ita_sync_test_"))
    (cache / "SITE-A" / "network").mkdir(parents=True)
    (cache / "SITE-A" / "network" / "ghost.json").write_text("{}", encoding="utf-8")
    code, out, state, cache = run_sync(site="SITE-A", out=cache)
    check("a new copy replaces the old one whole: no file from before survives",
          code == 0 and not (cache / "SITE-A" / "network" / "ghost.json").exists()
          and (cache / "SITE-A" / "network" / "sw1.json").exists(), out)

    # network-paths unreachable this time: a copy with no connections must not
    # replace one that has them
    code, out, state, cache = run_sync(network="deny", site="SITE-A", out=cache)
    check("a copy without connections never overwrites one that has them",
          code == 1 and (cache / "SITE-A" / "network" / "sw1.json").exists()
          and "ABORTED: network-paths was not reachable" in out, out)

    # the server stops answering: the good copy from before is left alone
    keep = ita_sync.MAX_IN_A_ROW
    ita_sync.MAX_IN_A_ROW = 2
    try:
        code, out, state, cache = run_sync(network="dead", site="SITE-A", out=cache)
    finally:
        ita_sync.MAX_IN_A_ROW = keep
    try:
        check("a server that goes silent aborts the copy without a traceback",
              "ABORTED: 2 requests in a row failed" in out, out)
        check("...and the last complete copy is untouched, the broken one set aside",
              (cache / "SITE-A" / "network" / "sw1.json").exists()
              and json.loads((cache / "SITE-A.partial" / "manifest.json")
                             .read_text(encoding="utf-8"))["complete"] is False, out)
    finally:
        shutil.rmtree(cache, ignore_errors=True)

    # a certificate the PC does not trust: never retried over http
    import ssl
    import urllib.error
    from ita_probe import Probe

    class CertFails(Probe):
        def __init__(self):
            super().__init__("https://ita.example")
            self.sent = []

        def get(self, url):
            self.sent.append(url.split("://")[0])
            if url.startswith("https://"):
                raise urllib.error.URLError(ssl.SSLError("[SSL: CERTIFICATE_VERIFY_FAILED]"))
            return 200, b"[]"

    probe = CertFails()
    sync = ita_sync.Sync(probe, None)
    sync.find_api("a")
    probe.locate_network_paths()
    check("a certificate error never falls back to http (the password would go in clear)",
          probe.sent and set(probe.sent) == {"https"} and sync.api is None, probe.sent)

    # --check: the one-minute login test for the first thing on Sunday
    code, out, state, cache = run_sync(check=True)
    shutil.rmtree(cache, ignore_errors=True)
    check("--check: a good login gets into the asset API and network-paths, copies nothing",
          code == 0 and "LOGIN  : ok" in out and "NETWORK: ok" in out
          and not state["assets"] and not state["network"], out)

    check("--check: one line to type back",
          typed_back(out) == ["C1 api=ok net=ok tls=ok"], out)

    code, out, state, cache = run_sync(check=True, password="wrong")
    shutil.rmtree(cache, ignore_errors=True)
    check("--check: a refused login is named as such, with what to try next",
          code == 1 and "refused this user name or password" in out
          and "DOMAIN\\name" in out, out)

    check("--check: ...a refused login in that one line too",
          typed_back(out) == ["C1 api=refused net=refused tls=ok"], out)

    code, out, state, cache = run_sync(check=True, network="deny")
    shutil.rmtree(cache, ignore_errors=True)
    check("--check: assets in but network-paths refusing shows as exactly that",
          code == 1 and "LOGIN  : ok" in out and "NETWORK: ok" not in out
          and "network-paths -> HTTP 401" in out, out)

    check("--check: ...and network-paths alone refusing",
          typed_back(out) == ["C1 api=ok net=refused tls=ok"], out)

    print(f"\n{sum(RESULTS)}/{len(RESULTS)} sync checks passed")
    raise SystemExit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
