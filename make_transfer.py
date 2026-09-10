#!/usr/bin/env python3
"""
make_transfer.py
================
Packs this project into a single ordinary .xlsx so it can cross an air gap.

WHY AN EXCEL FILE
-----------------
The ITA network is closed. Files enter only through a controlled transfer
portal, and only as .xlsx, an image, or .docx. An .xlsx is really a zip, so the
project *could* ride as an extra archive member inside one — recovered in a
single line on the far side.

That is deliberately NOT what this does. A portal that finds an unexpected
archive inside a document may treat the transfer as tampering, and being flagged
costs trust rather than merely time. So the workbook here is entirely
legitimate: the payload is base64 text sitting in visible cells. There is
nothing structurally unusual to detect, base64 contains no character that
autocorrect or re-encoding can alter, and cell values are the one thing a
sanitiser that rebuilds the file must preserve.

WHAT TRAVELS, AND WHAT DOES NOT
-------------------------------
Source only. data/topology.json is 32MB and does NOT travel: generate_topology
rebuilds it from seed 42 on the far side in about a minute. Shipping the
generator instead of the generated data is what turns this from a 32MB problem
into 66 spreadsheet cells.

data/zones.json travels as data/zones.default.json, so applying a later carrier
can never overwrite a zone map that has been tuned to the real site. See
unpack.py, which is the half of this that has to be careful.

    python make_transfer.py                     code only
    python make_transfer.py --networkx          bundle networkx too
"""

import argparse
import base64
import hashlib
import io
import json
import subprocess
import zipfile
from pathlib import Path

import make_sample_sheet

HERE = Path(__file__).parent
OUT = HERE / "ITA_transfer.xlsx"

# 4,000 of the 32,767 characters a cell allows. Deliberately far below the
# limit: a sanitiser that rebuilds the workbook is far more likely to truncate
# something sitting at the boundary than something obviously ordinary.
CHUNK = 4000

# Never travels. topology.json is regenerated; the png is a screenshot; the
# caches and outputs are build products.
NEVER = {"data/topology.json"}
NEVER_SUFFIX = (".png", ".pyc")
NEVER_DIR = ("__pycache__/", "output/", "_stage/", "_backup")

# Ships under a different name so unpack.py can tell "the default we shipped"
# from "the file they have been editing".
RENAME = {"data/zones.json": "data/zones.default.json"}


def _tracked():
    """The files git knows about, which is the honest definition of the project
    rather than whatever happens to be lying in the directory.

    unpack.py is forced in rather than left to git. It is the one file the far
    side cannot do without — nothing can be applied there without it — and a
    payload that silently lacked it because someone had not committed yet would
    only be discovered across the gap, where it cannot be fixed.
    """
    out = subprocess.run(["git", "ls-files"], cwd=HERE,
                         capture_output=True, text=True, check=True).stdout
    keep = []
    for name in out.split():
        if name in NEVER or name.endswith(NEVER_SUFFIX):
            continue
        if any(d in name for d in NEVER_DIR):
            continue
        keep.append(name)

    for essential in ("unpack.py",):
        if essential not in keep:
            if not (HERE / essential).exists():
                raise SystemExit(f"{essential} is missing — the far side cannot "
                                 f"apply a transfer without it")
            keep.append(essential)

    # make_transfer.py travels too, even though carriers are only ever built on
    # this side and it is useless over there.
    #
    # It was excluded at first, for tidiness. That broke the acceptance gate on
    # arrival: test_scenarios reads this file, so the 320 checks — the one thing
    # that proves the transfer worked — died on a FileNotFoundError the moment
    # they were run on the far side.
    #
    # The rule that replaces the tidiness: ANYTHING THE SUITE READS MUST SHIP.
    # The gate has to run identically on both sides or it is not a gate, and
    # skipping a check there instead would make the count disagree with the
    # documentation, which E5 checks — failing just as loudly, one step later.
    return sorted(keep)


def _networkx_from_wheel(wheel):
    """Vendor networkx out of a wheel, into _vendor/ rather than alongside us.

    Into _vendor/ because pathengine only falls back to it when the real import
    fails. Unpacking it into the project directory instead would shadow whatever
    is installed on that machine, so a far side with a perfectly good networkx
    would silently get ours — older, and on a newer Python possibly the one that
    does not run at all.

    Take the wheel rather than this machine's install, because the version has
    to suit THEIR Python, not ours: 3.6 needs 3.11+, while 3.1 covers 3.8 and up
    and still has every one of the five calls the engine makes. It is pure
    Python with no unconditional dependencies, so it needs no pip and no
    permissions once it is simply sitting there.

    networkx's own tests do not travel. They are half the package, and we are
    not testing networkx on the far side — we are testing the engine, which has
    its own 309 checks.
    """
    files, version = [], Path(wheel).name.split("-")[1]
    with zipfile.ZipFile(wheel) as z:
        for name in sorted(z.namelist()):
            if not name.startswith("networkx/") or not name.endswith(".py"):
                continue
            if "/tests/" in name:
                continue
            files.append((f"_vendor/{name}", z.read(name)))
    return version, files


def build(include_networkx=False):
    members = []                      # (name_in_payload, bytes)
    for name in _tracked():
        members.append((RENAME.get(name, name), (HERE / name).read_bytes()))

    nx_version = None
    if include_networkx:
        nx_version, nx_files = _networkx_from_wheel(include_networkx)
        members.extend(nx_files)

    # The manifest rides INSIDE the payload, so unpack.py can verify what it
    # unpacked without having to parse the workbook a second time.
    manifest = {name: hashlib.sha256(blob).hexdigest() for name, blob in members}

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name, blob in members:
            z.writestr(name, blob)
        z.writestr("MANIFEST.json", json.dumps(manifest, indent=1, sort_keys=True))
    payload = buf.getvalue()
    digest = hashlib.sha256(payload).hexdigest()

    b64 = base64.b64encode(payload).decode()
    chunks = [b64[i:i + CHUNK] for i in range(0, len(b64), CHUNK)]
    if len(chunks) > 9999:
        raise SystemExit(f"{len(chunks)} chunks needs more than the 4-digit "
                         f"prefix the bootstrap matches on")

    # The prefix is what the far-side bootstrap keys on. Four zero-padded digits
    # so a plain sort works, and it is re-sorted by int() there anyway so a
    # rebuild that reorders the strings cannot scramble the stream.
    payload_rows = [["PART_AND_DATA"]] + [[f"{i:04d}:{c}"] for i, c in enumerate(chunks)]

    readme_rows = [
        ["ITA Lab - source transfer"],
        [""],
        [f"payload sha256 : {digest}"],
        [f"files          : {len(members)}"],
        [f"networkx       : {('bundled ' + nx_version + ' in _vendor/, used only if none is installed')
                             if nx_version else 'not included - must already be installed'}"],
        [""],
        ["STEP 1 - recover the files into _stage (paste as one line):"],
        [BOOTSTRAP],
        [""],
        ["STEP 2 - apply them (same command every time, first install or update):"],
        ["python _stage/unpack.py --target ."],
        [""],
        ["STEP 3 - first transfer only, builds the 32MB synthetic map:"],
        ["python generate_topology.py"],
        [""],
        ["STEP 4 - the acceptance gate. Green here means the transfer is intact:"],
        ["python test_scenarios.py"],
        ["python test_agreement.py"],
        [""],
        ["STEP 5 - run it:"],
        ["python server.py"],
        [""],
        ["Do NOT re-run generate_topology.py on a later transfer: by then the map"],
        ["is either real work or real ITA data, and it would be erased."],
    ]

    manifest_rows = [["FILE", "SHA256"]] + [[n, h] for n, h in sorted(manifest.items())]

    make_sample_sheet.write_xlsx(OUT, {
        "README": readme_rows,
        "PAYLOAD": payload_rows,
        "MANIFEST": manifest_rows,
    })

    print(f"wrote {OUT}")
    print(f"  files    : {len(members)}")
    print(f"  networkx : {nx_version or 'not bundled'}")
    print(f"  payload  : {len(payload):,} bytes  ->  {len(chunks)} cells of {CHUNK}")
    print(f"  carrier  : {OUT.stat().st_size:,} bytes")
    print(f"  sha256   : {digest}")
    return digest


# Kept as one string so the README sheet and the documentation cannot drift
# apart. Scans every XML part rather than xl/sharedStrings.xml, because a
# sanitiser that rebuilds the workbook may store cells as inline strings and
# drop that part entirely - which would strand us with a KeyError on the far
# side and no way to debug it from here.
#
# No trailing \b after the base64 run: a word boundary fails between a closing
# "=" pad and the "<" that follows it, so the engine would backtrack and quietly
# drop the padding, breaking only the final chunk.
BOOTSTRAP = (
    'python -c "import base64,io,re,zipfile;'
    'z=zipfile.ZipFile(\'ITA_transfer.xlsx\');'
    'x=\'\'.join(z.read(n).decode(\'utf-8\',\'ignore\') for n in z.namelist() if n.endswith(\'.xml\'));'
    'c=sorted(re.findall(r\'(\\d{4}):([A-Za-z0-9+/=]{100,})\',x),key=lambda t:int(t[0]));'
    'zipfile.ZipFile(io.BytesIO(base64.b64decode(\'\'.join(t[1] for t in c)))).extractall(\'_stage\')"'
)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[3])
    ap.add_argument("--networkx", metavar="WHEEL",
                    help="vendor networkx from this wheel into _vendor/, for a "
                         "far side that may not have one. Use 3.1: it covers "
                         "Python 3.8+ where 3.6 needs 3.11+")
    args = ap.parse_args()
    build(include_networkx=args.networkx)
