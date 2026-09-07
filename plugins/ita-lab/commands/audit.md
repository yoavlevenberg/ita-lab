---
description: Audit ITA Lab against the invariants that have broken before.
allowed-tools: Bash, Read, Grep, Glob
---

Run an audit pass. This project has had three of them, and each one found
something that the test suite was structurally unable to see. Work through the
list below, check each claim **against the code and the data**, and report what
you actually verified — an audit that reports "all good" without evidence is
worth nothing.

Load the `route-invariants` and `closed-network` skills first; they carry the
reasoning behind each item.

**Dependencies.** Collect every import across `*.py` by parsing, not grepping
(docstrings contain prose that looks like imports). Anything outside the
standard library other than `networkx` is a finding.

**Strand integrity.** Load `data/topology.json` and check that no strand index
is owned by two circuits — remembering that **media type is part of strand
identity**, so fibre #1 and copper #1 on one trunk are two different strands.
An earlier audit reported 436 duplicates and the bug was in the audit.

**Review predicts plan.** Confirm `plan_devices()` still calls
`validate_devices()` itself rather than re-judging in parallel, and that
`test_agreement.py` still runs from `test_scenarios.py`. This rule has broken
twice.

**Zones.** Confirm the four MDA pods (A2, A6, D2, D6) resolve to neutral and not
to yellow, that an unknown colour raises `ZoneError` rather than widening to
"anywhere", and that `correction()` still reports what it fixed.

**Product constants.** Report the current values of `HOP_COST`, `INTRA_RACK_M`,
`PREFER_BONUS`, `FULLNESS_WEIGHT` and the proximity ladder, and check them
against `git log -p` for a change nobody announced.

**Serials.** Re-check the two properties `serials.py` claims: no port id parses
as a serial, and no serial collides with a device name.

**Topology consistency.** Every circuit references ports that exist and are
marked used; every used port belongs to a circuit; no device overhangs its rack
(`u_start` is the **top** U and numbering grows downward).

Then run `python3 test_scenarios.py` and report the count.

Write findings as: what you checked, how, and what you found. Separate a
confirmed defect from a suspicion. Fix only what is clearly a defect, and raise
anything that touches a documented decision instead of changing it.
