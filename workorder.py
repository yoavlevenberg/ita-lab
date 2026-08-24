#!/usr/bin/env python3
"""
workorder.py  (v3)
==================
Turns a resolved route into a Work Order a field technician can follow
without knowing anything about ITA or about this tool. Every step names the
rack, the U position, the device, the port number and the exact strand.

Two renderings of the same content:

  render()            plain text — fast to read in a terminal, easy to paste
                      into a ticket, and what the CLI writes to disk.
  wo_html.render()    a printable sheet with a sign-off block (see wo_html.py).
"""

from datetime import datetime


def render(route, order_id=None, circuit_id=None):
    order_id = order_id or f"WO-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    L = []
    L.append("=" * 68)
    L.append(f"  WORK ORDER  {order_id}")
    L.append("=" * 68)
    L.append(f"Cable type    : {route['cable_type'].upper()}")
    L.append(f"Redundancy    : domain {route['domain']}")
    L.append(f"Total length  : ~{route['total_length_m']} m")
    L.append(f"From          : {route['src_location']}")
    L.append(f"To            : {route['dst_location']}")
    if route.get("option_index"):
        L.append(f"Route option  : #{route['option_index']}")
        if route.get("shared_segments"):
            L.append(f"WARNING       : shares {len(route['shared_segments'])} trunk segment(s) "
                     f"with an earlier option: {', '.join(route['shared_segments'])}")
        else:
            L.append("Diversity     : fully disjoint from every earlier option (no shared trunk segments)")
    L.append("")
    L.append("STEPS")
    L.append("-" * 68)

    if route.get("intra_rack"):
        # nothing to pull and nothing to cross-connect — one patch lead inside
        # a single cabinet
        L.append(f"1. Run a {route['cable_type']} patch lead inside {route['hop_racks'][0]}:")
        L.append(f"     from {route['src_location']}")
        L.append(f"     to   {route['dst_location']}")
        L.append("")
        L.append("No trunk segments are used — both ends are in the same cabinet.")
        L.append("")
        if circuit_id:
            L.append(f"STATUS: EXECUTED — committed as {circuit_id}, written to topology.json.")
        else:
            L.append("STATUS: PROPOSED — not yet written to ITA.")
            L.append("        Requires network engineer approval before commit.")
        L.append("=" * 68)
        return "\n".join(L)

    n = 1
    L.append(f"{n}. Terminate the {route['cable_type']} cable at the source:")
    L.append(f"     {route['src_location']}")
    n += 1

    strand_word = "fiber strand" if route["cable_type"] == "fiber" else "copper pair"
    for i, seg in enumerate(route["segments"]):
        L.append(f"{n}. Patch {strand_word} #{seg['strand_index']} of trunk '{seg['edge_id']}'  "
                 f"({seg['from_rack']} -> {seg['to_rack']}, ~{seg['length_m']} m, domain {seg['domain']}).")
        L.append(f"     near end : {seg['strand_port_from']}")
        L.append(f"     far  end : {seg['strand_port_to']}")
        n += 1
        if i < len(route["transit_points"]):
            tp = route["transit_points"][i]
            L.append(f"{n}. Cross-connect at:")
            L.append(f"     {tp['location']}")
            n += 1

    L.append(f"{n}. Terminate at the destination:")
    L.append(f"     {route['dst_location']}")

    L.append("")
    L.append("PATH")
    L.append("-" * 68)
    L.append("  " + "  ->  ".join(route["hop_racks"]))
    L.append("")
    L.append("STRANDS RESERVED BY THIS JOB")
    L.append("-" * 68)
    for seg in route["segments"]:
        L.append(f"  {seg['edge_id']:<24} strand #{seg['strand_index']:<4} "
                 f"({seg['used_before']}/{seg['capacity']} strands used before this job, "
                 f"{seg['remaining_before']} free)")
    L.append("")
    if circuit_id:
        L.append(f"STATUS: EXECUTED — committed as {circuit_id}, written to topology.json.")
    else:
        L.append("STATUS: PROPOSED — not yet written to ITA.")
        L.append("        Requires network engineer approval before commit.")
    L.append("=" * 68)
    return "\n".join(L)
