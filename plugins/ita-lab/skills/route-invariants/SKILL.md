---
name: route-invariants
description: The decisions in ITA Lab that look like tunable constants but are product rules — hop cost versus cable length, placement scoring, strand ownership, zone boundaries, and the rule that the bulk review must predict the plan. Use when touching pathengine.py, placement.py, zones.py, bulkplan.py, or when a score, weight, or validation rule needs changing.
---

# Constants that are decisions

Several numbers in this codebase are not tuning knobs. Changing them changes
what the product promises. Each one below has a reason, and the reason is worth
more than the number.

## Hops beat metres — `pathengine.py`

```python
HOP_COST = 10_000.0    # edge weight = HOP_COST + length_m
```

An explicit product decision: **fewer patch points matters more than shorter
cable.** The lengths in the synthetic topology were assigned randomly and carry
no real meaning, so optimising for them would be optimising for noise. Because
`HOP_COST` dwarfs any plausible metre difference, Dijkstra sorts by hop count
first and uses length only to break ties. Lower it and the tool starts
recommending longer paths through more racks. Do not touch it without saying
out loud that the priority is changing.

`INTRA_RACK_M = 3.0` and `intra_rack_route()` handle two ports in the same
rack: zero segments, zero transit, `domain: "local"`.

## A strand is owned, not counted — `pathengine.py`

Early versions modelled a trunk as `capacity` + `used`, a counter. It is now a
set of numbered strands, and each circuit **owns a specific one**:

- `strand_port_id(edge_id, cable_type, index, rack)` gives every strand a real
  port id — `A1-S05->A1-S01#F008@A1-S05`.
- **Media type is part of strand identity.** Fibre #1 and copper #1 on the same
  trunk are two different physical things and may both exist. An audit once
  reported 436 "duplicate" strands; the bug was in the audit, not the data.
- Storage is **sparse** — `strands: {index: circuit_id}` holds only what is
  taken, not an array the length of capacity.
- `_segment(..., reserved=None)` exists so that two route options computed side
  by side cannot offer the same strand.

Reverting any of this to a counter loses the ability to say *which* strand, and
that is the thing a technician needs.

## Placement scoring — `placement.py`

```
SAME_RACK=0  SAME_ROW=200  SAME_POD=260  SAME_MDA=500  SAME_ROOM=700  ELSEWHERE=1200
FULLNESS_WEIGHT=400 (cubed)     PREFER_BONUS=-10_000
```

- `eligible_racks()` excludes EOR and MDA racks outright. Equipment is not
  mounted there. Explicit user decision, not an oversight.
- Fullness is **cubed**, so a nearly-full rack is pushed away sharply rather
  than linearly.
- `PREFER_BONUS` is -10,000 because anything weaker lost to the same-rack
  bonus, and an explicit user preference got swallowed. It is sized to win.

## Zones are a hard boundary — `zones.py`

A device assigned to a colour must sit in a pod of that colour. Yellow is
*derived* (whatever is left), and the four MDA pods — A2, A6, D2, D6 — are
**neutral: they belong to no colour, not even yellow.**

`resolve()` accepts Hebrew and English, ignores case, and tolerates typos.
`correction()` reports what it fixed — **a correction is never silent.** An
unrecognised colour raises `ZoneError` rather than falling back to "anywhere":
a hard boundary must not decay into no boundary because of a typo.

## The review must predict the plan — `bulkplan.py`

The flow is `validate() → review → plan() → visual → execute()`, and everything
rests on one property: **what the review approved, the planner plans; what the
review rejected, the planner refuses.**

This is enforced structurally, not by discipline. `plan_devices()` runs
`validate_devices()` itself and refuses whatever it rejected, instead of the two
sides judging independently and drifting. It has broken twice; both times the
fix was to move a rule into the shared path.

**When adding a validation rule, add it in one place.** `test_agreement.py`
sweeps generated sheets asserting the two sides agree, and `test_scenarios.py`
runs that sweep — that guard is why the drift gets caught now.

Related: `_resolve_endpoints()` distinguishes a port claimed by an earlier row
of the same sheet (`already claimed by row 7 of this sheet`) from one already
patched in the map (`already patched by CIR-0036`). Two different problems with
two different fixes; do not collapse the messages.

## Traps already paid for

1. `shortest_simple_paths` is a generator — the `try` must wrap the
   **iteration**, or `NetworkXNoPath` escapes.
2. `u_start` is the **top** U of a device, and U numbering grows downward.
3. Device ids collided when built from `NEW-{serial[3:9]}`; use the full serial
   plus a guard counter.
4. Port-id rows in the Hebrew UI need `direction:ltr`, or the identifier renders
   reversed.
5. `assistant.py` classifies intents **specific before general** — "don't use
   D5" mentions a rack and was read as "where is D5". `unknown` is a legitimate
   answer; an early version let `?` trigger help and swallow every unfamiliar
   question, and the test passed only because it omitted the question mark.
