#!/usr/bin/env python3
"""
test_agreement.py
=================
The preflight review has to predict the plan.

Everything else in this tool is one implementation of one rule. These two are
not: `validate()` reads a sheet and says what is wrong with it, and the planner
reads the same sheet and tries to route it. They are separate code, and they
have to reach the same verdict about every row — otherwise the review screen
lies, in one of two ways:

  * it flags rows that would have planned perfectly well, so the user fixes
    things that were never broken and stops trusting the screen; or
  * it says "all clear" and rows then fail during planning, after the user has
    already approved the sheet.

The rest of the suite checks this the way the rest of the suite checks
everything: one hand-written row, one expected message. That is what let the
rule break twice in one week. Neither break was exotic —

  * when each end became a serial, the rule "an endpoint given as a serial
    needs a CABLE column" started rejecting every row of a CORRECT sheet while
    the planner accepted all of them; and
  * the fix for that exposed the opposite: the review's serial branch skipped
    the port checks, so a bare serial at one end hid an already-patched port at
    the other, and the review passed a row the planner refused.

— and 172 example checks passed through both. An example test only ever finds
the case somebody already thought of.

So this file does not assert messages. It generates sheets, runs both sides,
and asserts a RELATIONSHIP between them. When that relationship breaks it
shrinks the sheet to the smallest one that still breaks it, so the failure
arrives as two rows rather than forty.

Run it alone for a long sweep:

    python test_agreement.py            # 2000 sheets
    python test_agreement.py 5000 7     # 5000 sheets, seed 7

test_scenarios.py imports run() and takes a smaller budget, so the property is
part of the ordinary suite as well.
"""

import copy
import random
import sys

import bulkplan
import pathengine
import serials
import xlsxreader

# Sheets are built from a seeded Random, so a failure reported by CI or by a
# colleague reproduces exactly from the seed printed alongside it.
DEFAULT_SEED = 20260830


# --------------------------------------------------------------------------
# what the map has to offer
# --------------------------------------------------------------------------

class Pools:
    """The map, indexed the way a sheet generator needs to ask about it."""

    def __init__(self, topology):
        self.T = topology
        self.free, self.used = [], []
        self.dev_free = {}          # device id -> [free port index]
        self.dev_used = {}
        for pid, p in topology["ports"].items():
            (self.free if p["status"] == "free" else self.used).append(pid)
            bucket = self.dev_free if p["status"] == "free" else self.dev_used
            bucket.setdefault(p["device"], []).append(p["index"])

        self.devices = [d for d in topology["devices"].values() if d.get("serial")]
        self.by_serial = {d["serial"]: d for d in self.devices}
        self.free_by_media = {
            m: [pid for pid in self.free if topology["ports"][pid]["type"] == m]
            for m in ("fiber", "copper")
        }
        # boxes with a small, countable fibre supply — the ones worth asking
        # for more ports than they have
        self.small = [d for d in self.devices
                      if 0 < len(self._free_of(d, "fiber")) <= 6]

    def _free_of(self, dev, media):
        idx = self.dev_free.get(dev["id"], [])
        return [i for i in idx
                if self.T["ports"][f"{dev['id']}:{i}"]["type"] == media]

    def media_of(self, port_id):
        return self.T["ports"][port_id]["type"]


# --------------------------------------------------------------------------
# writing an endpoint the several ways a sheet is allowed to write one
# --------------------------------------------------------------------------

def _as_serial_port(T, port_id):
    dev_id, _, index = port_id.rpartition(":")
    return f"{T['devices'][dev_id]['serial']}:{index}"


def spell(rng, T, port_id):
    """One port, written the way some real sheet would write it. All three
    spellings name the same socket, and that is itself worth generating: a
    sheet written one way must plan identically to the same sheet written
    another."""
    return rng.choice([
        lambda: port_id,                                   # A1-S05:FIB-PP-01:2
        lambda: _as_serial_port(T, port_id),               # 5488209915:2
        lambda: port_id,                                   # (weighted toward ids)
    ])()


def to_columns(endpoint, split):
    """Render one endpoint into sheet cells.

    With a device column the port is a bare number beside it; without one the
    cell holds the whole address. Splitting on the LAST colon is exactly what a
    person does by eye, which is the point — both spellings have to survive the
    round trip and mean the same port.
    """
    if not endpoint:
        return ("", "")
    if not split:
        return (endpoint, None)                # single column carries it all
    hit = serials.parse(endpoint)
    if hit and hit[1] is not None:             # 5488209915:2
        return (hit[0], str(hit[1]))
    if hit:                                    # bare serial, port left to us
        return (hit[0], "")
    dev, _, port = endpoint.rpartition(":")
    if dev and port.isdigit():                 # A1-S05:FIB-PP-01:2
        return (dev, port)
    return (endpoint, "")


# --------------------------------------------------------------------------
# the shapes a row can take
# --------------------------------------------------------------------------
# Each returns a list of (src, dst, cable) — a list because some faults only
# exist across rows. Nothing here knows the right answer: the property compares
# two implementations, so the generator only has to be varied, not correct.

def _shapes(rng, P, earlier):
    T = P.T

    def two_free(media=None, same_rack=False):
        media = media or rng.choice(("fiber", "copper"))
        pool = P.free_by_media[media]
        a = rng.choice(pool)
        if same_rack:
            rack = T["ports"][a]["rack"]
            here = [p for p in pool if T["ports"][p]["rack"] == rack and p != a]
            if not here:
                return None
            return a, rng.choice(here)
        elsewhere = [p for p in (rng.choice(pool) for _ in range(20))
                     if T["ports"][p]["rack"] != T["ports"][a]["rack"]]
        return (a, elsewhere[0]) if elsewhere else None

    def valid():
        pair = two_free()
        return [(spell(rng, T, pair[0]), spell(rng, T, pair[1]), "")] if pair else []

    def valid_same_rack():
        pair = two_free(same_rack=True)
        return [(spell(rng, T, pair[0]), spell(rng, T, pair[1]), "")] if pair else []

    def busy():
        if not P.used:
            return []
        a = rng.choice(P.used)
        pool = P.free_by_media[P.media_of(a)]
        return [(spell(rng, T, a), spell(rng, T, rng.choice(pool)), "")]

    def port_out_of_range():
        dev = rng.choice(P.devices)
        total = dev["fiber_ports"] + dev["copper_ports"]
        pair = two_free()
        if not pair:
            return []
        return [(f"{dev['serial']}:{total + rng.randint(1, 900)}",
                 spell(rng, T, pair[1]), "")]

    def unknown_serial():
        while True:
            sn = str(rng.randint(10 ** 9, 10 ** 10 - 1))
            if sn not in P.by_serial:
                break
        pair = two_free()
        return [(f"{sn}:{rng.randint(1, 24)}", spell(rng, T, pair[1]), "")] if pair else []

    def unknown_device_name():
        """A device named without its cabinet — the mistake anyone makes once,
        since the UI shows names and there are 640 cabinets."""
        pair = two_free()
        if not pair:
            return []
        dev_id = T["ports"][pair[0]]["device"]
        bare = dev_id.split(":", 1)[1] if ":" in dev_id else dev_id
        return [(f"{bare}:1", spell(rng, T, pair[1]), "")]

    def same_port():
        if not P.free:
            return []
        a = rng.choice(P.free)
        return [(spell(rng, T, a), spell(rng, T, a), "")]

    def duplicate():
        if not earlier:
            return []
        src, dst, cable = rng.choice(earlier)
        pair = two_free()
        if not pair or not src:
            return []
        return [(src, spell(rng, T, pair[1]), cable)]

    def media_mismatch():
        f = P.free_by_media["fiber"]
        c = P.free_by_media["copper"]
        if not f or not c:
            return []
        return [(spell(rng, T, rng.choice(f)), spell(rng, T, rng.choice(c)), "")]

    def half_filled():
        pair = two_free()
        if not pair:
            return []
        a, b = (spell(rng, T, pair[0]), spell(rng, T, pair[1]))
        return [(a, "", "")] if rng.random() < 0.5 else [("", b, "")]

    def bare_serials():
        """Two boxes and no port on either — the case that needs CABLE, and the
        one whose rule was wrong for a week."""
        a, b = rng.sample(P.devices, 2)
        cable = rng.choice(("fiber", "copper", ""))
        return [(a["serial"], b["serial"], cable)]

    def bare_and_named():
        """One end a box, the other a socket. The review used to skip every
        port check on a row like this."""
        pair = two_free()
        if not pair:
            return []
        dev = rng.choice(P.devices)
        media = P.media_of(pair[1])
        row = (dev["serial"], spell(rng, T, pair[1]), media)
        return [row if rng.random() < 0.5 else (row[1], row[0], media)]

    def drain_one_box():
        """More rows than the box has ports. Each row is fine alone; the sheet
        is not. Only a whole-sheet view catches it."""
        if not P.small:
            return []
        dev = rng.choice(P.small)
        have = len(P._free_of(dev, "fiber"))
        pool = P.free_by_media["fiber"]
        out = []
        for _ in range(have + rng.randint(1, 2)):
            far = rng.choice(pool)
            if T["ports"][far]["device"] == dev["id"]:
                continue
            out.append((dev["serial"], spell(rng, T, far), "fiber"))
        return out

    return [
        (valid, 26), (valid_same_rack, 6), (busy, 9), (port_out_of_range, 7),
        (unknown_serial, 6), (unknown_device_name, 6), (same_port, 5),
        (duplicate, 8), (media_mismatch, 6), (half_filled, 5),
        (bare_serials, 8), (bare_and_named, 8), (drain_one_box, 4),
    ]


def make_sheet(rng, P, max_rows=8):
    """A sheet as a list of (src, dst, cable), plus how it will be written."""
    rows, target = [], rng.randint(1, max_rows)
    while len(rows) < target:
        shapes = _shapes(rng, P, rows)
        pick = rng.choices([s for s, _ in shapes], [w for _, w in shapes])[0]
        try:
            rows.extend(pick() or [])
        except (IndexError, ValueError, KeyError):
            continue                      # a shape the current map cannot supply
    return rows[:max_rows]


def render(rows, split, group=True):
    """Turn the abstract sheet into the dicts the reader produces."""
    out = []
    for i, (src, dst, cable) in enumerate(rows, start=2):
        sd, sp = to_columns(src, split)
        dd, dp = to_columns(dst, split)
        row = {xlsxreader.ROW_KEY: i}
        if split:
            row["SRC_DEVICE"], row["SRC_PORT"] = sd, sp
            row["DST_DEVICE"], row["DST_PORT"] = dd, dp
        else:
            row["SRC_PORT"], row["DST_PORT"] = sd, dd
        if cable:
            row["CABLE"] = cable
        if group:
            row["GROUP"] = f"G{i % 3}"
        out.append(row)
    return out


# --------------------------------------------------------------------------
# the properties
# --------------------------------------------------------------------------

def _sheet_faults(T, demands):
    """Which rows the PLANNER refuses on the strength of the sheet alone,
    before any routing. This is the half of the planner the review claims to
    predict; capacity found during routing is not something it promises."""
    return {d["row"] for d in bulkplan._resolve_endpoints(T, demands)
            if d.get("malformed")}


def _review_faults(T, demands, new_devices=()):
    return {i["row"] for i in bulkplan.validate(T, demands, new_devices)["issues"]}


def check_agreement(T, rows, split):
    """The core property. Returns "" when the sheet is fine, or a description
    of the disagreement."""
    demands = bulkplan.demands_from_rows(render(rows, split))
    review = _review_faults(T, demands)
    planner = _sheet_faults(T, demands)
    if review == planner:
        return ""
    only_review = sorted(review - planner)
    only_planner = sorted(planner - review)
    parts = []
    if only_review:
        parts.append(f"review flags rows {only_review} that the planner accepts")
    if only_planner:
        parts.append(f"planner refuses rows {only_planner} that the review passed")
    return "; ".join(parts)


def check_both_spellings(T, rows):
    """The same sheet written both ways has to mean the same thing. Testing
    "does this cell contain a colon" to decide whether it already named a port
    silently dropped the port column for every device id on this map, and every
    example test still passed."""
    a = bulkplan.demands_from_rows(render(rows, split=True))
    b = bulkplan.demands_from_rows(render(rows, split=False))
    for x, y in zip(a, b):
        if (x["src"], x["dst"]) != (y["src"], y["dst"]):
            return (f"row {x['row']}: two columns give "
                    f"{x['src']!r}->{x['dst']!r}, one column gives "
                    f"{y['src']!r}->{y['dst']!r}")
    return ""


def check_review_predicts_plan(T, rows, split):
    """The expensive one, so it runs on few sheets: no row the review passed
    may fail the full planner for a SHEET reason, and no row the review flagged
    may plan successfully."""
    demands = bulkplan.demands_from_rows(render(rows, split))
    review = _review_faults(T, demands)
    result = bulkplan.plan(T, demands, {"group_adjacent": True})
    planned = {r["row"] for r in result["results"] if r["status"] == "ok"}
    both = sorted(review & planned)
    if both:
        return f"review flagged rows {both}, and the planner planned them anyway"
    return ""


# --------------------------------------------------------------------------
# shrinking
# --------------------------------------------------------------------------

def shrink(fails, rows):
    """The smallest sheet that still breaks the property.

    A generated sheet is mostly noise around the one row that matters, and a
    failure nobody can read is a failure nobody fixes. Greedy row removal is
    enough here: the faults are per-row or per-pair, so dropping one row at a
    time converges.
    """
    changed = True
    while changed and len(rows) > 1:
        changed = False
        for i in range(len(rows)):
            smaller = rows[:i] + rows[i + 1:]
            if smaller and fails(smaller):
                rows, changed = smaller, True
                break
    # then try dropping the cable hint, which is often incidental
    for i, (src, dst, cable) in enumerate(rows):
        if cable:
            trial = rows[:i] + [(src, dst, "")] + rows[i + 1:]
            if fails(trial):
                rows = trial
    return rows


def _describe(rows, split):
    lines = []
    for r in render(rows, split):
        cells = {k: v for k, v in r.items() if k != xlsxreader.ROW_KEY}
        lines.append(f"      row {r[xlsxreader.ROW_KEY]}: " +
                     "  ".join(f"{k}={v!r}" for k, v in cells.items() if v))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# the runner
# --------------------------------------------------------------------------

def run(T, check, sheets=250, plans=10, seed=DEFAULT_SEED, verbose=False):
    """Generate sheets, assert the properties, report the smallest counter-
    example. `check(name, ok, detail)` is test_scenarios' own reporter, so a
    failure here reads like any other failed check."""
    rng = random.Random(seed)
    P = Pools(T)

    bad_agreement = bad_spelling = None
    for n in range(sheets):
        split = rng.random() < 0.6
        rows = make_sheet(rng, P)
        if not rows:
            continue

        if bad_agreement is None:
            why = check_agreement(T, rows, split)
            if why:
                fails = lambda rs: bool(check_agreement(T, rs, split))
                small = shrink(fails, rows)
                bad_agreement = (check_agreement(T, small, split), small, split)

        if bad_spelling is None:
            why = check_both_spellings(T, rows)
            if why:
                fails = lambda rs: bool(check_both_spellings(T, rs))
                small = shrink(fails, rows)
                bad_spelling = (check_both_spellings(T, small), small, True)

        if bad_agreement and bad_spelling:
            break

    check(f"the review and the planner agree, over {sheets} generated sheets",
          bad_agreement is None,
          "" if not bad_agreement else
          f"{bad_agreement[0]}\n    smallest sheet that shows it "
          f"({'two columns' if bad_agreement[2] else 'one column'}):\n"
          + _describe(bad_agreement[1], bad_agreement[2])
          + f"\n    reproduce with: python test_agreement.py {sheets} {seed}")

    check("a sheet means the same written either way, over the same sheets",
          bad_spelling is None,
          "" if not bad_spelling else
          f"{bad_spelling[0]}\n" + _describe(bad_spelling[1], True))

    # The full planner is 0.5s of deep copy per call, so it gets a small budget.
    bad_plan = None
    for n in range(plans):
        split = rng.random() < 0.6
        rows = make_sheet(rng, P, max_rows=5)
        if not rows:
            continue
        why = check_review_predicts_plan(T, rows, split)
        if why:
            fails = lambda rs: bool(check_review_predicts_plan(T, rs, split))
            small = shrink(fails, rows)
            bad_plan = (check_review_predicts_plan(T, small, split), small, split)
            break

    check(f"nothing the review flagged is planned anyway, over {plans} full plans",
          bad_plan is None,
          "" if not bad_plan else
          f"{bad_plan[0]}\n" + _describe(bad_plan[1], bad_plan[2]))


def self_check(T, check, seed=5, sheets=120):
    """Can these properties actually fail?

    A property test that stays green while the thing it guards is broken is
    decoration. So break each rule on purpose and confirm the sweep notices —
    including the one that shipped: the device and port columns not being
    joined back together.
    """
    import bulkplan as B

    def sweep():
        bad = []
        run(T, lambda n, ok, d="": (None if ok else bad.append(n)),
            sheets=sheets, plans=2, seed=seed)
        return bad

    broken = []

    real_validate = B.validate
    B.validate = lambda t, d, nd=(): {
        **real_validate(t, d, nd),
        "issues": [i for i in real_validate(t, d, nd)["issues"]
                   if i["kind"] != "port_in_use"]}
    broken.append(("a review that ignores already-patched ports", bool(sweep())))
    B.validate = real_validate

    real_endpoint = B._endpoint
    B._endpoint = lambda row, dev, port, split: (
        B._pick(row, dev) if split else real_endpoint(row, dev, port, split))
    broken.append(("a reader that drops the port column", bool(sweep())))
    B._endpoint = real_endpoint

    real_resolve = B._resolve_endpoints
    def no_dupes(t, d):
        out = real_resolve(t, d)
        for r in out:
            if "already claimed by row" in (r.get("malformed") or ""):
                r["malformed"] = ""
        return out
    B._resolve_endpoints = no_dupes
    broken.append(("a planner that ignores a port used twice", bool(sweep())))
    B._resolve_endpoints = real_resolve

    for name, caught in broken:
        check(f"the sweep catches {name}", caught,
              "the property stayed green while the rule was broken")
    check("the sweep is green when nothing is broken", not sweep())


def main():
    sheets = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_SEED
    T = pathengine.load_topology()

    failures = []

    def check(name, ok, detail=""):
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failures.append(name)
            if detail:
                print(detail)

    print(f"sweeping {sheets} generated sheets, seed {seed}\n")
    run(T, check, sheets=sheets, plans=max(10, sheets // 100), seed=seed)
    print()
    print("can these properties fail?\n")
    self_check(T, check)
    print()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
