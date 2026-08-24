#!/usr/bin/env python3
"""
wo_html.py
==========
The printable rendering of a Work Order — the sheet a technician carries onto
the floor. Same facts as workorder.render(), laid out for paper.

Self-contained by design: no external CSS, fonts or images, so it prints
identically on a machine with no network — the same constraint that rules the
rest of this tool.
"""

from datetime import datetime
from html import escape as e

CSS = """
:root{ --ink:#111; --muted:#666; --line:#c9c9c9; --tint:#f4f6f8; --accent:#14507d; }
*{ box-sizing:border-box }
body{ margin:0; padding:26px 30px; color:var(--ink); background:#fff;
      font:13px/1.55 "Segoe UI",system-ui,-apple-system,Arial,sans-serif; }
h1{ font-size:19px; margin:0 0 2px; letter-spacing:.2px }
h2{ font-size:12px; text-transform:uppercase; letter-spacing:.9px; color:var(--accent);
    margin:22px 0 8px; padding-bottom:4px; border-bottom:1.5px solid var(--accent); }
.mono{ font-family:Consolas,"Courier New",monospace }
header{ display:flex; align-items:flex-start; gap:16px;
        border-bottom:2.5px solid var(--accent); padding-bottom:12px; }
header .id{ margin-left:auto; text-align:right; font-size:11.5px; color:var(--muted) }
header .id b{ display:block; font-size:15px; color:var(--ink); font-family:Consolas,monospace }
.sub{ font-size:11.5px; color:var(--muted) }
.badge{ display:inline-block; padding:3px 12px; border-radius:3px; font-size:11px;
        font-weight:700; letter-spacing:.5px; }
.badge.proposed{ background:#fff3cd; color:#7a5b00; border:1px solid #e0c356 }
.badge.executed{ background:#e2f4e6; color:#14602a; border:1px solid #7ec48f }
.warn{ margin-top:10px; background:#fff3cd; border:1px solid #e0c356; color:#7a5b00;
       border-radius:4px; padding:8px 11px; font-size:11.5px }
table{ width:100%; border-collapse:collapse; font-size:12px }
th,td{ text-align:left; padding:6px 9px; border-bottom:1px solid var(--line); vertical-align:top }
th{ background:var(--tint); font-size:10.5px; text-transform:uppercase;
    letter-spacing:.6px; color:var(--muted); font-weight:700 }
.facts{ display:grid; grid-template-columns:repeat(2,1fr); gap:0 26px; margin-top:12px }
.facts div{ display:flex; gap:10px; padding:5px 0;
            border-bottom:1px dotted var(--line); font-size:12px }
.facts .k{ color:var(--muted); min-width:118px; flex:none }
.facts .v{ font-weight:600 }
ol.steps{ margin:0; padding-left:20px }
ol.steps li{ margin-bottom:11px; padding-left:3px }
ol.steps .what{ font-weight:600 }
.ends{ margin-top:4px; border-left:2.5px solid var(--accent); padding:4px 0 4px 10px;
       background:var(--tint); font-size:11px }
.ends span{ display:block; font-family:Consolas,monospace }
.ends i{ font-style:normal; color:var(--muted); display:inline-block; width:62px }
.strand{ display:inline-block; border:1px solid var(--accent); color:var(--accent);
         border-radius:3px; padding:0 7px; font-family:Consolas,monospace;
         font-size:11px; font-weight:700; }
.path{ font-family:Consolas,monospace; font-size:11.5px; background:var(--tint);
       border:1px solid var(--line); border-radius:4px; padding:9px 11px; word-spacing:2px }
.signoff{ display:grid; grid-template-columns:repeat(3,1fr); gap:22px; margin-top:16px }
.signoff div{ border-top:1px solid var(--ink); padding-top:5px;
              font-size:10.5px; color:var(--muted) }
footer{ margin-top:26px; padding-top:9px; border-top:1px solid var(--line);
        font-size:10px; color:var(--muted); display:flex }
footer span:last-child{ margin-left:auto }
.noprint{ margin-bottom:16px }
.noprint button{ font:inherit; padding:7px 16px; border-radius:5px; cursor:pointer;
                 border:1px solid var(--accent); background:var(--accent);
                 color:#fff; font-weight:600 }
@media print{
  body{ padding:0 }
  h2{ break-after:avoid }
  ol.steps li, tr{ break-inside:avoid }
  .noprint{ display:none }
}
"""


def _steps(route):
    """The same sequence render() writes, as (title, near, far, strand) rows."""
    kind = "fiber strand" if route["cable_type"] == "fiber" else "copper pair"
    out = [(f"Terminate the {route['cable_type']} cable at the source",
            route["src_location"], None, None)]
    for i, seg in enumerate(route["segments"]):
        out.append((
            f"Patch {kind} #{seg['strand_index']} of trunk {seg['edge_id']} "
            f"({seg['from_rack']} → {seg['to_rack']}, ~{seg['length_m']} m)",
            seg["strand_port_from"], seg["strand_port_to"],
            f"#{seg['strand_index']}"))
        if i < len(route["transit_points"]):
            tp = route["transit_points"][i]
            out.append(("Cross-connect at", tp["location"], None, None))
    out.append(("Terminate at the destination", route["dst_location"], None, None))
    return out


def render(route, order_id=None, circuit_id=None):
    order_id = order_id or f"WO-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    status = (f'<span class="badge executed">EXECUTED &mdash; {e(circuit_id)}</span>'
              if circuit_id else
              '<span class="badge proposed">PROPOSED &mdash; needs approval</span>')

    facts = [
        ("Cable type", route["cable_type"].upper()),
        ("Redundancy leg", f"domain {route['domain']}"),
        ("Hops", f"{len(route['segments'])} trunk segment(s)"),
        ("Estimated length", f"~{route['total_length_m']} m"),
        ("Source port", route["src_location"]),
        ("Destination port", route["dst_location"]),
    ]
    if route.get("option_index"):
        facts.append(("Route option", f"#{route['option_index']}"))

    notes = []
    if route.get("shared_segments"):
        notes.append("This option shares {} trunk segment(s) with another proposed "
                     "option: {}. The two are NOT physically independent."
                     .format(len(route["shared_segments"]),
                             ", ".join(route["shared_segments"])))
    if route.get("domain") == "mixed":
        notes.append("This route changes row part-way through, so it does not stay "
                     "within a single redundancy leg.")
    warn = "".join(f'<div class="warn">&#9888; {e(n)}</div>' for n in notes)

    steps = []
    for title, near, far, strand in _steps(route):
        ends = f'<span><i>near end</i>{e(near)}</span>'
        if far:
            ends += f'<span><i>far end</i>{e(far)}</span>'
        tag = f' <span class="strand">{e(strand)}</span>' if strand else ""
        steps.append(f'<li><span class="what">{e(title)}</span>{tag}'
                     f'<div class="ends">{ends}</div></li>')

    rows = "".join(
        f"<tr><td class='mono'>{e(s['edge_id'])}</td>"
        f"<td class='mono'>#{s['strand_index']}</td>"
        f"<td>{s['length_m']} m</td><td>domain {e(s['domain'])}</td>"
        f"<td>{s['used_before']}/{s['capacity']} used &middot; "
        f"{s['remaining_before']} free</td></tr>"
        for s in route["segments"])

    # an installed circuit has no meaningful "before"; say what is true now
    strand_heading = ("Strands used by this connection" if circuit_id
                      else "Strands reserved by this job")
    occupancy_heading = "Occupancy now" if circuit_id else "Occupancy before this job"

    fact_html = "".join(
        f"<div><span class='k'>{e(k)}</span><span class='v mono'>{e(str(v))}</span></div>"
        for k, v in facts)
    path_html = " &nbsp;&rarr;&nbsp; ".join(e(r) for r in route["hop_racks"])

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{e(order_id)} &mdash; Work Order</title><style>{CSS}</style></head><body>
<div class="noprint"><button onclick="window.print()">Print / Save as PDF</button></div>

<header>
  <div>
    <h1>Physical Layer Work Order</h1>
    <div class="sub">ITA Lab &mdash; standalone prototype, not connected to live ITA</div>
    <div style="margin-top:9px">{status}</div>
  </div>
  <div class="id"><b>{e(order_id)}</b>{datetime.now().strftime('%Y-%m-%d %H:%M')}</div>
</header>
{warn}

<h2>Connection</h2>
<div class="facts">{fact_html}</div>

<h2>Path</h2>
<div class="path">{path_html}</div>

<h2>Steps</h2>
<ol class="steps">{''.join(steps)}</ol>

<h2>{strand_heading}</h2>
<table><thead><tr><th>Trunk</th><th>Strand</th><th>Length</th>
<th>Leg</th><th>{occupancy_heading}</th></tr></thead>
<tbody>{rows}</tbody></table>

<h2>Sign-off</h2>
<div class="signoff">
  <div>Planned by / date</div>
  <div>Approved by (network engineer) / date</div>
  <div>Installed by / date</div>
</div>

<footer><span class="mono">{e(order_id)}</span>
<span>ITA Lab &mdash; physical layer route planner</span></footer>
</body></html>"""
