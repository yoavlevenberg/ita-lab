#!/usr/bin/env python3
"""
ita_probe.py
============
Finds out, on the closed network, what the real ITA server will give us — and
reports only the SHAPE of it, never the data.

    python ita_probe.py --server <address>
    python ita_probe.py --server <address> --asset <uuid>     skip the search
    python ita_probe.py --server <address> --insecure         self-signed TLS

WHY THIS EXISTS
---------------
Almost nothing comes back out of the closed network, so the usual loop — send
me the API docs, I write the client — does not work. This runs there instead,
learns the API from the server itself, and prints a summary short enough to
type out by hand: which calls work, and the field NAMES they return.

What is known going in (2026-11-03, from the server's own Swagger page):
  * the documented API (/api/current, v8.1) has rooms, racks and devices
    (PatchPanel, network, server ...) but NO ports and NO connections;
  * an undocumented internal endpoint, /api/networks/internal/rest/network-paths,
    answers with 400 "getPortConfigurationsWithConnectionsAndPath.arg0 must not
    be null" — so it exists, wants one parameter, and is exactly what we need.
Everything else — how to log in, the parameter's name — is discovered here.

READ-ONLY, ENFORCED
-------------------
Every request goes through _request(), which refuses anything but GET, with
one exception: a POST to a login URL this script itself found in the spec.
Nothing can create, change or delete, whatever a later edit does elsewhere.

NOTHING REAL ON SCREEN
----------------------
The screen gets field names, types and counts. Ids, names, addresses and
values never reach it. Raw responses are saved under probe_out/ for later use
on this side of the gap — they stay on this machine.
"""

import argparse
import getpass
import json
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

OUT = Path(__file__).parent / "probe_out"
NETWORK_PATHS = "/api/networks/internal/rest/network-paths"
# tried in this order; the server's 400 names no parameter, only "arg0"
PARAM_NAMES = ("asset_id", "assetId", "asset", "id", "assetIds", "asset_ids")
MAX_GETS = 400          # the asset walk stops here, found or not
TIMEOUT = 30


class Refused(Exception):
    """A request this script must never send."""


class Probe:
    def __init__(self, root, insecure=False, log=print):
        self.root = root.rstrip("/")
        self.ctx = ssl._create_unverified_context() if insecure else None
        self.log = log
        self.auth_urls = set()      # the only URLs a POST may go to
        self.headers = {"Accept": "application/json"}
        self.gets = 0

    # -- the one door every request goes through ----------------------------
    def _request(self, method, url, body=None, headers=None):
        if method != "GET" and not (method == "POST" and url in self.auth_urls):
            raise Refused(f"{method} {url} — this tool only reads")
        h = dict(self.headers)
        h.update(headers or {})
        req = urllib.request.Request(url, data=body, headers=h, method=method)
        if method == "GET":
            self.gets += 1
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=self.ctx) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def get(self, path_or_url):
        url = path_or_url if "://" in path_or_url else self.root + path_or_url
        return self._request("GET", url)

    # -- 1. the spec ---------------------------------------------------------
    def fetch_spec(self):
        """The Swagger page links its spec (final.json); read the link out of
        the page rather than guess it, then fall back to the likely places."""
        tried = []
        st, page = self.get("/api/current/docs/index.html")
        if st == 200:
            for m in re.findall(r"""["']([^"'\s]+\.json)["']""", page.decode("utf-8", "replace")):
                tried.append(urllib.parse.urljoin(self.root + "/api/current/docs/", m))
        tried += [self.root + p for p in ("/api/current/docs/final.json",
                                         "/api/current/final.json",
                                         "/api/current/swagger.json")]
        for url in dict.fromkeys(tried):
            st, raw = self.get(url)
            if st == 200:
                try:
                    return json.loads(raw.decode("utf-8")), url
                except ValueError:
                    continue
        return None, None

    # -- 2. logging in -------------------------------------------------------
    def login_candidates(self, spec):
        """Where the spec says a login goes. Swagger 2.0 puts an oauth2
        password flow under securityDefinitions; otherwise look for a POST
        whose path is about tokens or logging in."""
        base = self.root + spec.get("basePath", "/api/current").rstrip("/")
        out = []
        for d in (spec.get("securityDefinitions") or {}).values():
            if d.get("type") == "oauth2" and d.get("tokenUrl"):
                out.append(("oauth", urllib.parse.urljoin(base + "/", d["tokenUrl"])))
        for path, ops in (spec.get("paths") or {}).items():
            low = path.lower()
            if ("post" in ops and "{" not in path
                    and re.search(r"token|login|auth", low)
                    and not re.search(r"revoke|logout|refresh|password|ldap|saml", low)):
                out.append(("json", base + path))
        if any(d.get("type") == "basic"
               for d in (spec.get("securityDefinitions") or {}).values()):
            out.append(("basic", None))
        return out

    def login(self, spec, user, password):
        check = self._cheap_get_path(spec)
        for kind, url in self.login_candidates(spec):
            if url:
                self.auth_urls.add(url)
            if kind == "basic":
                import base64
                pair = base64.b64encode(f"{user}:{password}".encode()).decode()
                self.headers["Authorization"] = "Basic " + pair
            else:
                if kind == "oauth":
                    body = urllib.parse.urlencode({"grant_type": "password",
                                                   "username": user,
                                                   "password": password}).encode()
                    ctype = "application/x-www-form-urlencoded"
                else:
                    body = json.dumps({"username": user, "password": password}).encode()
                    ctype = "application/json"
                st, raw = self._request("POST", url, body, {"Content-Type": ctype})
                token = _find_token(_json(raw)) if st in (200, 201) else None
                if not token:
                    continue
                self.headers["Authorization"] = "Bearer " + token
            st, _ = self.get(check)
            if st == 200:
                return kind
            self.headers.pop("Authorization", None)
        return None

    def _cheap_get_path(self, spec):
        """A GET with no path parameters, to prove a login worked."""
        base = spec.get("basePath", "/api/current").rstrip("/")
        paths = spec.get("paths") or {}
        for want in ("/assets/types", "/assets"):
            if want in paths and "get" in paths[want]:
                return base + want
        for path, ops in sorted(paths.items()):
            if "get" in ops and "{" not in path:
                return base + path
        return base + "/assets"

    # -- 3. one real asset to ask about --------------------------------------
    def find_asset(self, spec):
        """Walk the asset tree breadth-first, counting types, until a patch
        panel turns up. Returns (id or None, type counts)."""
        base = spec.get("basePath", "/api/current").rstrip("/")
        include = _include_param(spec, "/assets/{id}")
        suffix = f"?{include}=children" if include else ""
        types, seen, queue = Counter(), set(), []
        fallback = None

        st, raw = self.get(base + "/assets")
        queue.extend(a.get("id") for a in (_assets_in(_json(raw)) if st == 200 else []))
        while queue and self.gets < MAX_GETS:
            aid = queue.pop(0)
            if not aid or aid in seen:
                continue
            seen.add(aid)
            # a parent may list its children as bare ids, so the type is only
            # known once the asset's own record has been fetched
            st, raw = self.get(f"{base}/assets/{urllib.parse.quote(str(aid))}{suffix}")
            body = _json(raw) if st == 200 else None
            if not isinstance(body, dict):
                continue
            kind = str(body.get("type", "?"))
            types[kind] += 1
            if "patch" in kind.lower():
                return aid, types
            if fallback is None and re.search(r"network|switch|server", kind, re.I):
                fallback = aid
            for child in _children(body):
                queue.append(child.get("id") if isinstance(child, dict) else child)
        return fallback, types

    # -- 4. the endpoint we actually need ------------------------------------
    def network_paths(self, asset_id):
        """Try each likely parameter name. A 400 that still says the argument
        is null means the name was wrong; anything else means it was heard."""
        tried = []
        for name in PARAM_NAMES:
            q = urllib.parse.urlencode({name: asset_id})
            st, raw = self.get(f"{NETWORK_PATHS}?{q}")
            text = raw.decode("utf-8", "replace")
            tried.append((name, st))
            if st == 400 and "must not be null" in text:
                continue
            return name, st, raw, tried
        return None, None, None, tried


# -- helpers that never print a value ----------------------------------------

def _json(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, AttributeError):
        return None


def _find_token(obj):
    if isinstance(obj, dict):
        for k in ("access_token", "accessToken", "token", "jwt", "id_token"):
            if isinstance(obj.get(k), str) and obj[k]:
                return obj[k]
        for v in obj.values():
            t = _find_token(v)
            if t:
                return t
    return None


def _include_param(spec, path):
    """The query parameter on GET <path> that accepts 'children', if any."""
    op = ((spec.get("paths") or {}).get(path) or {}).get("get") or {}
    for p in op.get("parameters", []):
        enum = p.get("enum") or (p.get("items") or {}).get("enum") or []
        if "children" in [str(e).lower() for e in enum]:
            return p.get("name")
    return None


def _assets_in(body):
    """An asset list may come bare or wrapped in a page object."""
    if isinstance(body, list):
        return [a for a in body if isinstance(a, dict)]
    if isinstance(body, dict):
        for k in ("items", "content", "assets", "results", "data"):
            if isinstance(body.get(k), list):
                return [a for a in body[k] if isinstance(a, dict)]
        if "id" in body:
            return [body]
    return []


def _children(body):
    if isinstance(body, dict):
        kids = body.get("children")
        if isinstance(kids, list):
            return kids
    return []


def shape(obj, prefix="", out=None):
    """Every field path with its type, lists merged: 'ports[].name: str'.
    The values themselves are never looked at beyond their type."""
    out = set() if out is None else out
    if isinstance(obj, dict):
        if not obj:
            out.add(f"{prefix or '.'}: {{}}")
        for k, v in obj.items():
            shape(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(obj, list):
        if not obj:
            out.add(f"{prefix}[]: empty")
        for v in obj[:50]:
            shape(v, prefix + "[]", out)
    else:
        name = {bool: "bool", int: "int", float: "num", str: "str",
                type(None): "null"}.get(type(obj), type(obj).__name__)
        out.add(f"{prefix or '.'}: {name}")
    return sorted(out)


def _spec_summary(spec):
    paths = spec.get("paths") or {}
    gets = sum(1 for ops in paths.values() if "get" in ops)
    hits = sorted(p for p in paths if re.search(r"port|connect|cable|network|link",
                                                p, re.I))
    return len(paths), gets, hits


def _root(server):
    s = server.strip().rstrip("/")
    return s if "://" in s else "https://" + s


def run(args, ask_password=getpass.getpass, ask_user=input, log=print):
    OUT.mkdir(exist_ok=True)
    report = []

    def say(line=""):
        log(line)
        report.append(line)

    roots = [_root(args.server)]
    if "://" not in args.server:
        roots.append("http://" + args.server.strip().rstrip("/"))

    probe = spec = spec_url = None
    for root in roots:
        p = Probe(root, insecure=args.insecure, log=log)
        try:
            s, u = p.fetch_spec()
        except ssl.SSLError:
            say("TLS: the server's certificate is not trusted here.")
            say("     run again with --insecure (it is an internal server)")
            return 2
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, ssl.SSLError):
                say("TLS: the server's certificate is not trusted here.")
                say("     run again with --insecure (it is an internal server)")
                return 2
            continue
        if s:
            probe, spec, spec_url = p, s, u
            break
    if not spec:
        say("SPEC   : not found — is the address right? Can Chrome open")
        say("         <address>/api/current/docs/index.html from this PC?")
        return 2

    (OUT / "final.json").write_text(json.dumps(spec, indent=1), encoding="utf-8")
    n_paths, n_gets, hits = _spec_summary(spec)
    say("ITA probe — read-only. Values are never shown, only field names.")
    say("")
    say(f"SPEC   : ok, version {spec.get('info', {}).get('version', '?')}, "
        f"{n_paths} paths ({n_gets} GET) -> probe_out/final.json")
    say(f"SPEC   : paths about ports/connections/network: {len(hits)}")
    for h in hits[:12]:
        say(f"         {h}")

    user = args.user or ask_user("ITA user name: ")
    password = ask_password("ITA password (not shown, not saved): ")
    how = probe.login(spec, user, password)
    if not how:
        say("LOGIN  : FAILED with every method the spec offers")
        tried = [k for k, _ in probe.login_candidates(spec)]
        say(f"         tried: {', '.join(tried) or 'nothing found in the spec'}")
        _save(report)
        return 1
    say(f"LOGIN  : ok ({how})")

    if args.asset:
        asset, types = args.asset, Counter()
        say("ASSET  : using the id given with --asset")
    else:
        asset, types = probe.find_asset(spec)
        say(f"ASSETS : walked {sum(types.values())} assets, "
            f"{len(types)} types: " + ", ".join(f"{k}={v}" for k, v in types.most_common(8)))
        say("ASSET  : found one to ask about" if asset else
            "ASSET  : no patch panel / network device found — rerun with --asset <uuid>")
    if not asset:
        _save(report)
        return 1

    name, st, raw, tried = probe.network_paths(asset)
    if name is None:
        say("NETWORK: the server ignored every parameter name: "
            + ", ".join(n for n, _ in tried))
        _save(report)
        return 1
    say(f"NETWORK: parameter '{name}' -> HTTP {st}")
    body = _json(raw)
    if st == 200 and body is not None:
        (OUT / "network_paths_sample.json").write_text(
            json.dumps(body, indent=1), encoding="utf-8")
        fields = shape(body)
        say(f"NETWORK: {len(fields)} fields (raw saved to probe_out/, stays here):")
        for f in fields:
            say("         " + f)
    else:
        say("NETWORK: no JSON came back — note the HTTP code above")
    _save(report)
    say("")
    say("Type out the lines above. Everything is also in probe_out/report.txt")
    return 0


def _save(report):
    (OUT / "report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[3])
    ap.add_argument("--server", required=True,
                    help="the ITA server's address, as typed in Chrome")
    ap.add_argument("--user", help="ITA user name (asked for if left out)")
    ap.add_argument("--asset", help="a device's uuid, to skip the search")
    ap.add_argument("--insecure", action="store_true",
                    help="accept the server's self-signed certificate")
    sys.exit(run(ap.parse_args()))


if __name__ == "__main__":
    main()
