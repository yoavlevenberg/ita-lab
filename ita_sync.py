#!/usr/bin/env python3
"""
ita_sync.py
===========
Copies one rack, or one whole site, out of the real ITA into a local folder —
read-only, and as raw as ITA gave it. ITA Lab will read the copy instead of
asking ITA on every click: faster, no load on ITA, and one folder per site
when more sites come in.

    python ita_sync.py --server https://<ip> --rack "<rack name>"     try one rack first
    python ita_sync.py --server https://<ip> --site "<folder name>"   a whole site
    python ita_sync.py --server https://<ip> --id <uuid>              start from any asset
        --insecure   accept the server's self-signed certificate
        --proxy      go through Windows' proxy setting (default: direct)

WHAT IS KNOWN ABOUT THE SERVER (2026-11-04, see CONTEXT.md)
-----------------------------------------------------------
  * login is HTTP Basic on every request (an old curl script that worked);
  * assets:       GET /api/v8.1/assets/<uuid>, /api/v8.1/assets/search?q=<name>
  * connections:  GET https://<ip>/api/networks/internal/rest/network-paths?asset_id=<uuid>
                  — https only, one call per device, the full path through
                  every patch panel included;
  * the tree is location > location > location > server-room > row > rack >
    (unsupported) > device. How rows map to pods is NOT known yet — the
    STRUCTURE lines below are there to answer that without showing a name.

WHAT LANDS ON DISK, WHAT REACHES THE SCREEN
-------------------------------------------
Disk (ita_cache/, git-ignored, stays on the closed network): every asset
record and every network-paths answer, untouched, plus manifest.json.
Screen: counts and type names only — never a name, id, serial or address.
Every request goes through ita_probe.Probe._request, which refuses anything
but GET (and the one login POST the probe may find; this tool sends none).
"""

import argparse
import getpass
import json
import re
import sys
import time
import urllib.error
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path

from ita_probe import Probe, _assets_in, _children, _json, _root, _why

CACHE = Path(__file__).parent / "ita_cache"
API_BASES = ("/api/v8.1", "/api/current")
# an asset whose type matches this holds other assets; anything else is a
# device and gets its connections fetched
CONTAINER = re.compile(r"location|site|building|floor|room|row|rack|cage|zone|folder|unsupported",
                       re.I)


class SyncError(Exception):
    """Something the person running this has to act on; the message says what."""


def _other_scheme(root):
    scheme, host = root.split("://", 1)
    return ("http" if scheme == "https" else "https") + "://" + host


def _kind(asset):
    return str(asset.get("type") or asset.get("elementType") or "?")


def _slug(text):
    return re.sub(r"[^\w.-]+", "_", text.strip(), flags=re.UNICODE).strip("_") or "site"


class Sync:
    def __init__(self, probe, out, log=print):
        self.p = probe
        self.out = out
        self.log = log
        self.api = None             # e.g. https://host/api/v8.1, found by find_api
        self.network_url = None     # the network-paths URL, found by find_network

    # -- where the documented API answers -----------------------------------
    def find_api(self, probe_query):
        """The first base that answers a search with 200. Both schemes: the
        old curl script used http, Chrome uses https."""
        notes = []
        for root in (self.p.root, _other_scheme(self.p.root)):
            for base in API_BASES:
                url = f"{root}{base}/assets/search?{urllib.parse.urlencode({'q': probe_query})}"
                try:
                    st, raw = self.p.get(url)
                except (urllib.error.URLError, OSError) as e:
                    notes.append(f"{root.split('://')[0]} {base}/assets/search -> {_why(e)}")
                    continue
                notes.append(f"{root.split('://')[0]} {base}/assets/search -> {self.p.last}")
                if st == 200:
                    self.api = root + base
                    return _json(raw), notes
        return None, notes

    # -- the starting asset --------------------------------------------------
    def find_start(self, name, want):
        """Exactly one asset named `name` whose type matches `want`."""
        body, notes = self.find_api(name)
        if self.api is None:
            raise SyncError("the asset API did not answer:\n" +
                            "\n".join("         " + n for n in notes))
        hits = [a for a in _assets_in(body)
                if str(a.get("name", "")).strip().lower() == name.strip().lower()]
        typed = [a for a in hits if re.search(want, _kind(a), re.I)]
        pick = typed or hits
        if len(pick) == 1 and pick[0].get("id"):
            return pick[0]["id"]
        if not pick:
            kinds = Counter(_kind(a) for a in _assets_in(body))
            raise SyncError(f"no asset with exactly that name. The search returned "
                            f"{sum(kinds.values())} results, types: "
                            + (", ".join(f"{k}={v}" for k, v in kinds.most_common()) or "none")
                            + "\n         check the spelling, or open it in ITA and use --id <uuid>")
        raise SyncError(f"{len(pick)} assets share that name — open the right one in ITA "
                        "and rerun with --id <uuid> (from the address bar)")

    # -- the network-paths endpoint ------------------------------------------
    def find_network(self):
        url, notes = self.p.locate_network_paths()
        self.network_url = url
        return notes

    # -- the walk ------------------------------------------------------------
    def walk(self, start_id, limit=None):
        """Breadth-first from start_id: every asset record, then connections
        for each device. Returns the manifest."""
        (self.out / "assets").mkdir(parents=True, exist_ok=True)
        (self.out / "network").mkdir(parents=True, exist_ok=True)
        kinds, parent_of, kind_of = Counter(), {}, {}
        failed = Counter()
        devices = []
        queue, seen = [(start_id, None)], set()
        while queue:
            aid, parent = queue.pop(0)
            if not aid or aid in seen:
                continue
            if limit and len(seen) >= limit:
                failed["stopped at --limit"] += 1
                break
            seen.add(aid)
            st, raw = self.p.get(f"{self.api}/assets/{urllib.parse.quote(str(aid))}?include=children")
            body = _json(raw) if st == 200 else None
            if not isinstance(body, dict):
                failed[f"asset HTTP {st}"] += 1
                continue
            _write(self.out / "assets" / f"{_safe(aid)}.json", body)
            kind = _kind(body)
            kinds[kind] += 1
            kind_of[aid], parent_of[aid] = kind, parent
            if not CONTAINER.search(kind):
                devices.append(aid)
            for child in _children(body):
                cid = child.get("id") if isinstance(child, dict) else child
                queue.append((cid, aid))

        connections = ports = 0
        net_codes = Counter()
        for aid in devices:
            if not self.network_url:
                break
            st, raw = self.p.get(f"{self.network_url}?{urllib.parse.urlencode({'asset_id': aid})}")
            net_codes[st] += 1
            body = _json(raw) if st == 200 else None
            if body is None:
                if st in (401, 403) and net_codes[st] == 1 and len(net_codes) == 1:
                    failed[f"network-paths HTTP {st} — refuses this login"] += 1
                    break       # every other device would say the same
                continue
            _write(self.out / "network" / f"{_safe(aid)}.json", body)
            for pc in (body.get("portConfigurations") or []) if isinstance(body, dict) else []:
                ports += 1
                connections += len(pc.get("connectionsAndPaths") or [])

        manifest = {
            "synced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "start": start_id,
            "api": self.api,
            "network_paths": self.network_url,
            "assets": len(kind_of),
            "kinds": dict(kinds),
            "devices": len(devices),
            "network_http": {str(k): v for k, v in net_codes.items()},
            "port_configurations": ports,
            "connections": connections,
            "problems": dict(failed),
            "tree": {a: {"parent": parent_of[a], "type": kind_of[a]} for a in kind_of},
        }
        _write(self.out / "manifest.json", manifest)
        return manifest


def structure(manifest):
    """How the tree is built, in type names and counts only — enough to tell
    whether a row is one side of a pod, a whole pod, or a single rack, and
    how many racks stand outside any row."""
    tree = manifest["tree"]
    kids = defaultdict(list)
    for a, t in tree.items():
        if t["parent"]:
            kids[t["parent"]].append(a)

    def chain(a):
        out = []
        while a:
            out.append(tree[a]["type"])
            a = tree[a]["parent"]
        return " > ".join(reversed(out))

    lines = []
    rack_chains = Counter(chain(a) for a, t in tree.items() if re.search("rack", t["type"], re.I))
    for c, n in rack_chains.most_common(6):
        lines.append(f"{n} racks at: {c}")
    rows = [a for a, t in tree.items() if re.fullmatch(r"row", t["type"], re.I)]
    if rows:
        per_row = Counter(sum(1 for k in kids[r] if re.search("rack", tree[k]["type"], re.I))
                          for r in rows)
        lines.append(f"{len(rows)} rows, racks per row: "
                     + ", ".join(f"{k} racks x{v}" for k, v in sorted(per_row.items())))
        parents = Counter(tree[tree[r]["parent"]]["type"] if tree[r]["parent"] else "(start)"
                          for r in rows)
        lines.append("rows sit under: " + ", ".join(f"{k}={v}" for k, v in parents.most_common()))
    racks = [a for a, t in tree.items() if re.search("rack", t["type"], re.I)]
    loose = sum(1 for a in racks
                if tree[a]["parent"] and not re.fullmatch(r"row", tree[tree[a]["parent"]]["type"], re.I))
    if racks:
        lines.append(f"racks not inside a row: {loose} of {len(racks)}")
    return lines


def _safe(aid):
    return re.sub(r"[^\w.-]", "_", str(aid))


def _write(path, obj):
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")


def run(args, ask_password=getpass.getpass, ask_user=input, log=print):
    say = log
    if bool(args.rack) + bool(args.site) + bool(args.id) != 1:
        say("give exactly one of --rack, --site or --id")
        return 2
    user = args.user or ask_user("ITA user name: ")
    password = ask_password("ITA password (not shown, not saved): ")

    probe = Probe(_root(args.server), insecure=args.insecure, log=log,
                  proxy=getattr(args, "proxy", False))
    probe.use_basic(user, password)
    label = args.rack or args.site or args.id
    out = Path(args.out) / _slug(label)
    sync = Sync(probe, out, log)
    say("ITA sync — read-only. The screen gets counts and type names, never a value.")
    say("")
    try:
        if args.id:
            _, notes = sync.find_api("a")
            if sync.api is None:
                raise SyncError("the asset API did not answer:\n" +
                                "\n".join("         " + n for n in notes))
            start = args.id
        else:
            start = sync.find_start(args.rack or args.site,
                                    "rack" if args.rack else "location|site|folder|room")
    except SyncError as e:
        say(f"START  : {e}")
        return 1
    except (urllib.error.URLError, OSError) as e:
        say(f"START  : {_why(e)}" + ("  (try --insecure)" if "certificate" in _why(e) else ""))
        return 1
    say(f"API    : {sync.api.split('://')[0]} {urllib.parse.urlsplit(sync.api).path}")
    say("START  : found")

    notes = sync.find_network()
    for n in notes:
        say(f"PATH   : {n}")
    refused = None
    if not sync.network_url:
        # A 401/403 means the path is there and only the login was turned
        # away (Chrome gets in with its own session) — not "not found".
        refused = next((n for n in notes if re.search(r"-> HTTP 40[13]\b", n)), None)
        say("NETWORK: network-paths is there but refuses this login — assets only this time"
            if refused else "NETWORK: network-paths not found — assets only this time")

    m = sync.walk(start, limit=args.limit)
    say(f"ASSETS : {m['assets']} — " + ", ".join(f"{k}={v}" for k, v in
                                                  Counter(m["kinds"]).most_common(10)))
    say(f"DEVICES: {m['devices']}, network-paths answers: " +
        (", ".join(f"HTTP {k} x{v}" for k, v in m["network_http"].items()) or "none"))
    say(f"PORTS  : {m['port_configurations']} port groups, {m['connections']} connected ports")
    if refused:
        m["problems"]["network-paths refuses this login"] = 1
    for p, n in m["problems"].items():
        say(f"PROBLEM: {p} (x{n})")
    for line in structure(m):
        say(f"STRUCT : {line}")
    say("")
    say(f"Saved under ita_cache/ ({m['assets']} asset files). It stays on this machine.")
    say("Type out the lines above.")
    return 0 if not m["problems"] else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[3])
    ap.add_argument("--server", required=True, help="the ITA server's address, as in Chrome")
    ap.add_argument("--rack", help="one rack, by its exact name — the first test")
    ap.add_argument("--site", help="a whole site, by its folder's exact name")
    ap.add_argument("--id", help="start from this asset's uuid instead of a name")
    ap.add_argument("--user", help="ITA user name (asked for if left out)")
    ap.add_argument("--out", default=str(CACHE), help="where the copy goes (default ita_cache/)")
    ap.add_argument("--limit", type=int, help="stop after this many assets")
    ap.add_argument("--insecure", action="store_true",
                    help="accept the server's self-signed certificate")
    ap.add_argument("--proxy", action="store_true",
                    help="go through Windows' proxy setting (default: direct)")
    sys.exit(run(ap.parse_args()))


if __name__ == "__main__":
    main()
