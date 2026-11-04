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
    python ita_sync.py --server https://<ip> --check                  only test the login
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
import shutil
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


class SyncAborted(Exception):
    """The server stopped answering mid-copy; what was copied is kept aside."""


MAX_IN_A_ROW = 10       # this many failed requests in a row: the server is gone


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
            if any("certificate not trusted" in n for n in notes):
                break           # no http retry: it would send the password in clear
            for base in API_BASES:
                url = f"{root}{base}/assets/search?{urllib.parse.urlencode({'q': probe_query})}"
                try:
                    st, raw = self.p.get(url)
                except (urllib.error.URLError, OSError) as e:
                    notes.append(f"{root.split('://')[0]} {base}/assets/search -> {_why(e)}")
                    if "certificate" in _why(e):
                        break
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

    # -- one request, never fatal ----------------------------------------------
    def _get(self, url, what, failed):
        """GET that survives the network: one retry, then counted as a
        failure instead of thrown. A long site copy must not die on the
        3,000th request. Returns (status, body) or (None, None)."""
        for attempt in (1, 2):
            try:
                st, raw = self.p.get(url)
                self.in_a_row = 0
                return st, raw
            except (urllib.error.URLError, OSError) as e:
                reason = _why(e)
        failed[f"{what}: {reason}"] += 1
        self.in_a_row += 1
        if self.in_a_row >= MAX_IN_A_ROW:
            raise SyncAborted(f"{MAX_IN_A_ROW} requests in a row failed ({reason})")
        return None, None

    # -- the walk ------------------------------------------------------------
    def walk(self, start_id, limit=None):
        """Breadth-first from start_id: every asset record, then connections
        for each device. Returns the manifest.

        Everything goes into <out>.partial/ first, and replaces <out> only when
        the walk finished: a copy cut short never mixes with the last good
        one, and a fresh copy never carries files a previous one left behind
        (a device removed in ITA would otherwise come back from its old file)."""
        stage = self.out.with_name(self.out.name + ".partial")
        if stage.exists():
            shutil.rmtree(stage)
        (stage / "assets").mkdir(parents=True)
        (stage / "network").mkdir(parents=True)
        kinds, parent_of, kind_of = Counter(), {}, {}
        failed = Counter()
        devices, missing = [], []
        connections = ports = 0
        net_codes = Counter()
        self.in_a_row = 0
        aborted = None
        try:
            queue, seen = [(start_id, None)], set()
            while queue:
                aid, parent = queue.pop(0)
                if not aid or aid in seen:
                    continue
                if limit and len(seen) >= limit:
                    failed["stopped at --limit"] += 1
                    break
                seen.add(aid)
                st, raw = self._get(
                    f"{self.api}/assets/{urllib.parse.quote(str(aid))}?include=children",
                    "asset", failed)
                body = _json(raw) if st == 200 else None
                if not isinstance(body, dict):
                    if st is not None:
                        failed[f"asset HTTP {st}"] += 1
                    continue
                _write(stage / "assets" / f"{_safe(aid)}.json", body)
                kind = _kind(body)
                kinds[kind] += 1
                kind_of[aid], parent_of[aid] = kind, parent
                if not CONTAINER.search(kind):
                    devices.append(aid)
                for child in _children(body):
                    cid = child.get("id") if isinstance(child, dict) else child
                    queue.append((cid, aid))

            for aid in devices:
                if not self.network_url:
                    missing.append(aid)
                    continue
                st, raw = self._get(
                    f"{self.network_url}?{urllib.parse.urlencode({'asset_id': aid})}",
                    "network-paths", failed)
                if st is not None:
                    net_codes[st] += 1
                body = _json(raw) if st == 200 else None
                if not isinstance(body, dict):
                    missing.append(aid)
                    if st in (401, 403) and net_codes[st] == 1 and len(net_codes) == 1:
                        failed[f"network-paths HTTP {st} — refuses this login"] += 1
                        missing.extend(d for d in devices if d not in missing)
                        break       # every other device would say the same
                    continue
                _write(stage / "network" / f"{_safe(aid)}.json", body)
                for pc in body.get("portConfigurations") or []:
                    ports += 1
                    connections += len(pc.get("connectionsAndPaths") or [])
        except SyncAborted as e:
            aborted = str(e)
        except KeyboardInterrupt:
            aborted = "stopped by Ctrl+C"
        finally:
            manifest = {
                "synced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "complete": aborted is None,
                "aborted": aborted,
                "start": start_id,
                "api": self.api,
                "network_paths": self.network_url,
                "assets": len(kind_of),
                "kinds": dict(kinds),
                "devices": len(devices),
                # devices whose ports and connections are NOT in the copy —
                # the converter must not read them as "no ports, nothing patched"
                "network_missing": missing,
                "network_http": {str(k): v for k, v in net_codes.items()},
                "port_configurations": ports,
                "connections": connections,
                "problems": dict(failed),
                "tree": {a: {"parent": parent_of[a], "type": kind_of[a]} for a in kind_of},
            }
            _write(stage / "manifest.json", manifest)
        # a copy without connections must not replace one that has them
        if aborted is None and not self.network_url:
            aborted = "network-paths was not reachable, so this copy has no connections"
            manifest.update(complete=False, aborted=aborted)
            _write(stage / "manifest.json", manifest)
        if aborted is None:
            if self.out.exists():
                shutil.rmtree(self.out)
            stage.rename(self.out)
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


_ID_LIKE = re.compile(r"^[0-9a-f-]{8,}$|^\d+$", re.I)


def field_names(folder, manifest, kinds=("rack", "server-room", "room", "row")):
    """Which fields the records of racks and rooms carry — NAMES only, never a
    value. ITA draws each site as a floor plan with the cabinets placed on it
    (2026-11-05, from the user), so a rack record may hold its position; this
    is how to find the field without showing a single name or number from the
    site. A key that looks like an id is not printed."""
    tree = manifest["tree"]
    lines = []
    for kind in kinds:
        ids = [a for a, t in tree.items() if str(t["type"]).lower() == kind][:20]
        seen = Counter()
        for aid in ids:
            try:
                rec = json.loads((folder / "assets" / f"{_safe(aid)}.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue

            def walk(obj, prefix, depth):
                for k, v in obj.items():
                    if _ID_LIKE.match(str(k)):
                        k = "<id>"
                    name = f"{prefix}{k}"
                    if isinstance(v, dict) and depth < 2:
                        walk(v, name + ".", depth + 1)
                    else:
                        tag = ("#" if isinstance(v, (int, float)) and not isinstance(v, bool)
                               else "[]" if isinstance(v, list) else "")
                        seen[name + tag] += 1
            walk(rec if isinstance(rec, dict) else {}, "", 0)
        if seen:
            lines.append(f"{kind} ({len(ids)} read): " + ", ".join(sorted(seen)))
    return lines


def _safe(aid):
    return re.sub(r"[^\w.-]", "_", str(aid))


def _write(path, obj):
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")


def check_login(sync, say):
    """Does this user and password get in — to the asset API, and to
    network-paths? No asset is needed: network-paths checks the login before
    it checks its argument, so 'must not be null' means the login passed."""
    try:
        _, notes = sync.find_api("a")
    except (urllib.error.URLError, OSError) as e:
        say(f"LOGIN  : {_why(e)}" + ("  (try --insecure)" if "certificate" in _why(e) else ""))
        return 1
    for n in notes:
        say(f"LOGIN  : {n}")
    if sync.api:
        say("LOGIN  : ok — the asset API accepts this user")
    elif any("-> HTTP 401" in n for n in notes):
        say("LOGIN  : the server refused this user name or password.")
        say("         If it is a domain user, try it as DOMAIN\\name or name@domain,")
        say("         or the user the old curl script used (DCIM_USER), if that was a different one.")
    net_notes = sync.find_network()
    for n in net_notes:
        say(f"PATH   : {n}")
    if sync.network_url:
        say("NETWORK: ok — network-paths accepts this login too")
    if any("certificate not trusted" in n for n in notes + net_notes):
        say("TLS    : the server's certificate is not trusted here — run again with --insecure")
    return 0 if sync.api and sync.network_url else 1


def run(args, ask_password=getpass.getpass, ask_user=input, log=print):
    say = log
    check = getattr(args, "check", False)
    if not check and bool(args.rack) + bool(args.site) + bool(args.id) != 1:
        say("give exactly one of --rack, --site or --id (or --check)")
        return 2
    user = (args.user or ask_user("ITA user name: ")).strip()
    password = ask_password("ITA password (not shown, not saved): ")

    probe = Probe(_root(args.server), insecure=args.insecure, log=log,
                  proxy=getattr(args, "proxy", False))
    probe.use_basic(user, password)
    if check:
        say("ITA login check — read-only, nothing is copied.")
        say("")
        return check_login(Sync(probe, None, log), say)
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
        if any("certificate not trusted" in n for n in notes):
            say("NETWORK: network-paths is on https and its certificate is not trusted here —")
            say("         run again with --insecure (assets only this time)")
        else:
            say("NETWORK: network-paths is there but refuses this login — assets only this time"
                if refused else "NETWORK: network-paths not found — assets only this time")

    m = sync.walk(start, limit=args.limit)
    if not m["complete"]:
        say(f"ABORTED: {m['aborted']}")
        say("         the previous copy (if any) is untouched; this one is kept aside")
        say("         as .partial and is NOT used — run the same command again")
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
    # field names only (# = a number, [] = a list) — looking for where ITA keeps
    # a cabinet's place on its floor plan
    folder = sync.out if m["complete"] else sync.out.with_name(sync.out.name + ".partial")
    for line in field_names(folder, m):
        say(f"FIELDS : {line}")
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
    ap.add_argument("--check", action="store_true",
                    help="only test the user and password, copy nothing")
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
