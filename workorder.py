#!/usr/bin/env python3
"""
workorder.py  (v2)
==================
Turns a resolved route into a Work Order a field technician can follow
without knowing anything about ITA or about this tool. Every step names the
rack, the U position, the device and the port number.

Plain text for now — deliberately, so we can iterate fast. Once the routing
logic is signed off, this is the natural place to swap in a proper printable
Word template.
"""

from datetime import datetime


def render(route, order_id=None):
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
    L.append("")
    L.append("STEPS")
    L.append("-" * 68)

    n = 1
    L.append(f"{n}. Terminate the {route['cable_type']} cable at the source:")
    L.append(f"     {route['src_location']}")
    n += 1

    for i, seg in enumerate(route["segments"]):
        L.append(f"{n}. Pull cable {seg['from_rack']} -> {seg['to_rack']}  "
                 f"(~{seg['length_m']} m, trunk '{seg['edge_id']}', domain {seg['domain']}).")
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
    L.append("TRUNK CAPACITY BEFORE THIS JOB (planner reference)")
    L.append("-" * 68)
    for seg in route["segments"]:
        L.append(f"  {seg['edge_id']:<24} {seg['used_before']:>3}/{seg['capacity']:<3} used, "
                 f"{seg['remaining_before']:>3} free")
    L.append("")
    L.append("STATUS: PROPOSED — not yet written to ITA.")
    L.append("        Requires network engineer approval before commit.")
    L.append("=" * 68)
    return "\n".join(L)
