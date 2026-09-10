#!/usr/bin/env python3
"""
unpack.py
=========
Applies a recovered transfer to the working directory, on the closed network.

Run it the SAME way every time — first install and every later update:

    python _stage/unpack.py --target .

Two procedures would be one too many. Whoever types these commands during
update #3 will type what the README says; if that ever said "extract straight
over the top", the far side's only copy of its own state would be gone. Using
one path also means this file gets exercised on the very first install, instead
of making its debut under pressure months later.

WHAT THIS PROTECTS
------------------
By the second transfer the far side holds work that exists nowhere else. There
is no git there and no way to send anything back out, so a plain extract is
unrecoverable. This is generate_topology's --force lesson again, with the safety
net removed:

    code (*.py, ui.html, docs)   overwritten - that is the point of an update
    networkx/                    written once, skipped if already present
    data/zones.json              NEVER touched; seeded from zones.default.json
                                 only when it does not exist yet
    data/topology.json           NEVER touched; never even ships

Nothing under data/ is written without --force-data. A warning in a README is
not a safeguard; a check in code is.
"""

import argparse
import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path

STAGE = Path(__file__).parent
DEFAULT_SUFFIX = ".default.json"


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply(target, force_data=False, dry_run=False):
    manifest_path = STAGE / "MANIFEST.json"
    if not manifest_path.exists():
        raise SystemExit(f"{manifest_path} is missing — the recovery step did "
                         f"not produce a complete payload. Re-run the bootstrap.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    target = Path(target).resolve()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = target / f"_backup_{stamp}"

    # PASS 1 — check the whole payload before touching anything.
    #
    # Validating as we go and refusing at the end would already have written
    # every file up to the damaged one, leaving the far side half-updated while
    # the message claimed nothing had happened. There is no git there to undo
    # it. So the entire payload is proved first, and only then is a single byte
    # written.
    corrupt = []
    for name, want in sorted(manifest.items()):
        src = STAGE / name
        if not src.exists():
            corrupt.append(f"{name} (missing from the payload)")
        elif _sha(src) != want:
            corrupt.append(f"{name} (arrived damaged)")

    if corrupt:
        print("REFUSED — the payload does not match its own manifest:")
        for c in corrupt:
            print(f"  {c}")
        raise SystemExit("nothing was written. Re-transfer the workbook.")

    # PASS 2 — apply it.
    added, changed, same, skipped = [], [], [], []

    for name, want in sorted(manifest.items()):
        src = STAGE / name
        dst = target / name

        # data/ is theirs, not ours. The one exception is seeding the zone map
        # the very first time, from the copy that ships under a different name.
        if name.startswith("data/") and not force_data:
            if name.endswith(DEFAULT_SUFFIX):
                real = dst.with_name(name.split("/")[-1].replace(DEFAULT_SUFFIX, ".json"))
                if real.exists():
                    skipped.append(f"{real.relative_to(target).as_posix()} (yours — left alone)")
                    continue
                dst = real          # first install: seed it
            else:
                skipped.append(f"{name} (under data/ — needs --force-data)")
                continue

        if dst.exists():
            if _sha(dst) == want:
                same.append(name)
                continue
            changed.append(name)
            if not dry_run:
                keep = backup / dst.relative_to(target)
                keep.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, keep)
        else:
            added.append(name)

        if not dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    verb = "would apply" if dry_run else "applied"
    print(f"{verb} {len(added) + len(changed)} file(s) to {target}")
    for label, group in (("new", added), ("updated", changed)):
        for n in group:
            print(f"  {label:>8}  {n}")
    for n in skipped:
        print(f"  {'kept':>8}  {n}")
    print(f"  {'unchanged':>8}  {len(same)} file(s)")
    if changed and not dry_run:
        print(f"\nreplaced files backed up in {backup.name}/")

    if not dry_run:
        print("\nnext:")
        if not (target / "data" / "topology.json").exists():
            print("  python generate_topology.py     # first transfer only, ~1 min")
        else:
            print("  (topology.json already exists — do NOT re-run "
                  "generate_topology.py, it would erase it)")
        print("  python test_scenarios.py        # the acceptance gate")
        print("  python server.py")
    return added, changed, same, skipped


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Apply a recovered ITA Lab transfer.")
    ap.add_argument("--target", default=".", help="where the project lives (default: .)")
    ap.add_argument("--force-data", action="store_true",
                    help="also write files under data/ — this can destroy "
                         "executed circuits and a tuned zone map")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change and write nothing")
    args = ap.parse_args()
    apply(args.target, force_data=args.force_data, dry_run=args.dry_run)
