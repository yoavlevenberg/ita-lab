#!/usr/bin/env python3
"""
ita_probe.py
============
Finds out, on the closed network, what the real ITA server will give us — and
reports only the SHAPE of it, never the data.

    python ita_probe.py --server <address>
    python ita_probe.py --server <address> --asset <uuid>     skip the search
    python ita_probe.py --server <address> --insecure         self-signed TLS
    python ita_probe.py --server <address> --proxy            via Windows' proxy
    python ita_probe.py --server <address> --spec final.json  spec saved from Chrome

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
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

OUT = Path(__file__).parent / "probe_out"
# Where the internal endpoint lives is not certain: the 400 was seen once
# (2026-11-03), but on 2026-11-04 the first of these gave 404 in Chrome. The
# right one answers a bare GET with 400 "...must not be null".
# Confirmed 2026-11-04 from F12 in ITA's own Network window:
#   GET https://<ip>/api/networks/internal/rest/network-paths?asset_id=<uuid>
NETWORK_PATH_CANDIDATES = ("/api/networks/internal/rest/network-paths",
                           "/api/v8.1/networks/internal/rest/network-paths",
                           "/api/current/networks/internal/rest/network-paths",
                           "/networks/internal/rest/network-paths")
# tried in this order; the server's 400 names no parameter, only "arg0"
PARAM_NAMES = ("asset_id", "assetId", "asset", "id", "assetIds", "asset_ids",
               "sourceAssetId")    # the last from an internal AI's guess, 2026-11-04
# Paths nobody has confirmed (an internal AI's guesses, 2026-11-04; the spec
# does not list them). Bare GETs — only the HTTP code is reported.
GUESSES = ("/api/current/connections", "/api/current/ports",
           "/api/current/connections/network-paths")
MAX_GETS = 400          # the asset walk stops here, found or not
TIMEOUT = 30


class Refused(Exception):
    """A request this script must never send."""


class Probe:
    def __init__(self, root, insecure=False, log=print, proxy=False):
        self.root = root.rstrip("/")
        ctx = ssl._create_unverified_context() if insecure else None
        # An internal server is reached directly; Windows' proxy setting is
        # for the outside world and may not know the way in. --proxy flips it.
        handlers = [urllib.request.HTTPSHandler(context=ctx)]
        if not proxy:
            handlers.append(urllib.request.ProxyHandler({}))
        self.opener = urllib.request.build_opener(*handlers)
        self.log = log
        self.auth_urls = set()      # the only URLs a POST may go to
        self.headers = {"Accept": "application/json"}
        self.gets = 0
        self.last = ""              # one safe line on the last answer
        self.attempts = []          # what each spec URL answered, for the report

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
            with self.opener.open(req, timeout=TIMEOUT) as r:
                self.last = _meta(url, r.status, r.headers, r.geturl())
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            self.last = _meta(url, e.code, e.headers, url)
            return e.code, e.read()

    def get(self, path_or_url):
        url = path_or_url if "://" in path_or_url else self.root + path_or_url
        return self._request("GET", url)

    # -- 1. the spec ---------------------------------------------------------
    def fetch_spec(self):
        """The Swagger page links its spec (final.json); read the link out of
        the page rather than guess it, then fall back to the likely places."""
        tried = [self.root + p for p in ("/api/current/docs/index.html",
                                        "/api/current/docs/final.json",
                                        "/api/current/final.json",
                                        "/api/current/swagger.json",
                                        "/api/v8.1/docs/index.html",
                                        "/api/v8.1/docs/final.json")]
        seen = set()
        while tried:
            url = tried.pop(0)
            if url in seen:
                continue
            seen.add(url)
            try:
                st, raw = self.get(url)
            except (urllib.error.URLError, OSError) as e:
                self.attempts.append(f"{_path(url)} -> {_why(e)}")
                if _is_cert_error(e) or _is_timeout(e):
                    raise           # every other URL on this host would do the same
                continue
            note = self.last
            if st == 200 and url.endswith(".html"):
                links = re.findall(r"""["']([^"'\s]+\.json)["']""", raw.decode("utf-8", "replace"))
                tried[:0] = [urllib.parse.urljoin(url, m) for m in links]
                self.attempts.append(f"{_path(url)} -> {note}, {len(links)} .json links")
                continue
            if st == 200:
                try:
                    return json.loads(raw.decode("utf-8")), url
                except ValueError:
                    note += ", not JSON"
            self.attempts.append(f"{_path(url)} -> {note}")
        return None, None

    # -- 2. logging in -------------------------------------------------------
    def login_candidates(self, spec):
        """How to log in. Basic first, whatever the spec says: it is what
        worked against this server before (a curl script, 2026-11-04), and it
        sends nothing but GETs. Then what the spec offers — an oauth2 password
        flow under securityDefinitions, or a POST about tokens or logging in."""
        base = self.root + spec.get("basePath", "/api/current").rstrip("/")
        out = [("basic", None)]
        for d in (spec.get("securityDefinitions") or {}).values():
            if d.get("type") == "oauth2" and d.get("tokenUrl"):
                out.append(("oauth", urllib.parse.urljoin(base + "/", d["tokenUrl"])))
        for path, ops in (spec.get("paths") or {}).items():
            low = path.lower()
            if ("post" in ops and "{" not in path
                    and re.search(r"token|login|auth", low)
                    and not re.search(r"revoke|logout|refresh|password|ldap|saml", low)):
                out.append(("json", base + path))
        return out

    def login(self, spec, user, password):
        check = self._cheap_get_path(spec)
        for kind, url in self.login_candidates(spec):
            try:
                if self._try_login(kind, url, user, password, check):
                    return kind
            except (urllib.error.URLError, OSError) as e:
                self.attempts.append(f"login {kind} {_path(url or check)} -> {_why(e)}")
            self.headers.pop("Authorization", None)
        return None

    def use_basic(self, user, password):
        import base64
        pair = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.headers["Authorization"] = "Basic " + pair

    def _try_login(self, kind, url, user, password, check):
        """One login method; True if a cheap GET then works. Notes what each
        step got back, so a failure says more than FAILED."""
        if url:
            self.auth_urls.add(url)
        if kind == "basic":
            self.use_basic(user, password)
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
            self.attempts.append(f"login {kind} {_path(url)} -> {self.last}"
                                 + ("" if token or st not in (200, 201) else ", no token in reply"))
            if not token:
                return False
            self.headers["Authorization"] = "Bearer " + token
        st, _ = self.get(check)
        self.attempts.append(f"login {kind} check {_path(check)} -> {self.last}")
        return st == 200

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
    def locate_network_paths(self):
        """Which candidate is the endpoint: the one whose bare GET complains
        about the missing argument. Both schemes: on the real server the
        documented API answers on http, but this one only on https (Chrome's
        F12, 2026-11-04) — http gets a different web server and a 404.
        Returns (full URL or None, notes)."""
        scheme, host = self.root.split("://", 1)
        other = ("http" if scheme == "https" else "https") + "://" + host
        notes = []
        for path in NETWORK_PATH_CANDIDATES:
            for root in (self.root, other):
                url = root + path
                try:
                    st, raw = self.get(url)
                except (urllib.error.URLError, OSError) as e:
                    notes.append(f"{root.split('://')[0]} {path} -> {_why(e)}")
                    continue
                notes.append(f"{root.split('://')[0]} {path} -> {self.last}")
                if st == 400 and b"must not be null" in raw:
                    return url, notes
        return None, notes

    def network_paths(self, path, asset_id):
        """Try each likely parameter name. A 400 that still says the argument
        is null, or a 404, means the name was wrong; anything else means it
        was heard."""
        tried = []
        for name in PARAM_NAMES:
            q = urllib.parse.urlencode({name: asset_id})
            st, raw = self.get(f"{path}?{q}")
            tried.append((name, st))
            if st == 404 or (st == 400 and b"must not be null" in raw):
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
    """Scheme and host only: a pasted Swagger URL would otherwise keep its
    path, and every request would land under it."""
    s = server.strip()
    u = urllib.parse.urlsplit(s if "://" in s else "https://" + s)
    return f"{u.scheme}://{u.netloc}"


def _path(url):
    """A URL without its host — the host is the one internal detail here."""
    u = urllib.parse.urlsplit(url)
    return u.path + ("?" + u.query if u.query else "")


def _meta(url, status, headers, final_url):
    """One line on what came back, safe to type out: the code, the content
    type, the login scheme asked for, and where a redirect went (path only)."""
    out = f"HTTP {status}"
    ctype = (headers.get("Content-Type") or "").split(";")[0].strip()
    if ctype:
        out += f", {ctype}"
    auth = headers.get("WWW-Authenticate")
    if auth:
        out += f", wants {auth.split()[0]} login"
    if final_url and _path(final_url) != _path(url):
        out += f", redirected to {_path(final_url)}"
    return out


def _reason(e):
    return getattr(e, "reason", e)


def _is_cert_error(e):
    return "CERTIFICATE_VERIFY_FAILED" in str(_reason(e))


def _is_timeout(e):
    return isinstance(e, socket.timeout) or isinstance(_reason(e), socket.timeout)


def _why(e):
    """What went wrong, without the address in it."""
    r = _reason(e)
    if _is_cert_error(e):
        return "certificate not trusted"
    if isinstance(r, ssl.SSLError):
        return "TLS failed (" + str(getattr(r, "reason", None) or type(r).__name__) + ")"
    if _is_timeout(e):
        return "no answer (timeout)"
    if isinstance(r, socket.gaierror):
        return "name not found (DNS)"
    if isinstance(r, ConnectionRefusedError):
        return "connection refused"
    if getattr(r, "winerror", None):
        return f"network error (WinError {r.winerror})"
    return type(r).__name__


def run(args, ask_password=getpass.getpass, ask_user=input, log=print):
    OUT.mkdir(exist_ok=True)
    report = []

    def say(line=""):
        log(line)
        report.append(line)

    root = _root(args.server)
    roots = [root]
    if "://" not in args.server:
        roots.append("http://" + root.split("://", 1)[1])

    # Asked up front: the docs themselves may want the login (Chrome only
    # showed them because it was already signed in).
    user = args.user or ask_user("ITA user name: ")
    password = ask_password("ITA password (not shown, not saved): ")

    probe = spec = spec_url = None
    cert_error = False
    if getattr(args, "spec", None):
        # Saved from Chrome: the docs may sit behind a login Chrome already
        # has. The server's address still comes from --server; its scheme
        # from there, else the spec's own `schemes`, else https.
        try:
            spec = json.loads(Path(args.spec).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            say(f"SPEC   : cannot read {args.spec} ({type(e).__name__})")
            return 2
        if "://" not in args.server and spec.get("schemes"):
            root = spec["schemes"][0] + "://" + root.split("://", 1)[1]
        probe = Probe(root, insecure=args.insecure, log=log,
                      proxy=getattr(args, "proxy", False))
        spec_url = args.spec
        roots = []
    for root in roots:
        scheme = root.split("://", 1)[0]
        p = Probe(root, insecure=args.insecure, log=log, proxy=getattr(args, "proxy", False))
        s = None
        for with_login in (False, True):
            if with_login:
                p.use_basic(user, password)
            p.attempts = []
            try:
                s, u = p.fetch_spec()
            except (urllib.error.URLError, OSError) as e:
                cert_error = cert_error or _is_cert_error(e)
                for a in p.attempts:
                    say(f"TRY    : {scheme} {a}")
                break               # the host itself failed; a login won't help
            for a in p.attempts:
                say(f"TRY    : {scheme}{' +login' if with_login else ''} {a}")
            if s:
                break
        p.headers.pop("Authorization", None)
        if s:
            probe, spec, spec_url = p, s, u
            break
    if not spec:
        if cert_error:
            say("TLS    : the server's certificate is not trusted here.")
            say("         run again with --insecure (it is an internal server)")
        else:
            say("SPEC   : not found. Type out the TRY lines above.")
            say("         If Chrome opens the spec, save it (Ctrl+S) as")
            say("         final.json next to this script and add --spec final.json")
        _save(report)
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

    before = len(probe.attempts)
    how = probe.login(spec, user, password)
    if not how:
        say("LOGIN  : FAILED with every method the spec offers")
        tried = [k for k, _ in probe.login_candidates(spec)]
        say(f"         tried: {', '.join(tried) or 'nothing found in the spec'}")
        for a in probe.attempts[before:]:
            say(f"TRY    : {a}")
        _save(report)
        return 1
    say(f"LOGIN  : ok ({how})")
    for g in GUESSES:
        try:
            st, raw = probe.get(g)
            n = _json(raw)
            size = f", {len(n)} items" if st == 200 and isinstance(n, list) else ""
            say(f"GUESS  : {g} -> {probe.last}{size}")
        except (urllib.error.URLError, OSError) as e:
            say(f"GUESS  : {g} -> {_why(e)}")

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

    path, notes = probe.locate_network_paths()
    for n in notes:
        say(f"PATH   : {n}")
    if not path:
        say("NETWORK: no candidate path answered 'must not be null' —")
        say("         find the real one with F12 > Network in ITA's own UI")
        _save(report)
        return 1
    name, st, raw, tried = probe.network_paths(path, asset)
    if name is None:
        say("NETWORK: the server ignored every parameter name: "
            + ", ".join(f"{n}={c}" for n, c in tried))
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
    ap.add_argument("--spec", metavar="FILE",
                    help="the spec saved from Chrome, when the server will not hand it over")
    ap.add_argument("--proxy", action="store_true",
                    help="go through Windows' proxy setting (default: direct)")
    sys.exit(run(ap.parse_args()))


if __name__ == "__main__":
    main()
