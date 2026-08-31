#!/usr/bin/env python3
"""
store.py
========
Where the map lives, and how a change is written back to it.

This is the seam. Everything above it — routing, planning, placement, the
review, the interface — works on a topology dict and does not care where that
dict came from. Everything below it is "how do we read and write the real
world". Today the real world is `data/topology.json`; tomorrow it is the ITA
API, and the intended change is to write one more class in this file and
change one line in server.py.

WHY THIS EXISTS RATHER THAN JUST CALLING save_topology()
--------------------------------------------------------
Because the two are not the same operation, and only one of them is what ITA
offers. A file store can be handed the whole map and told "write this". An API
is told "create this circuit", "delete that one" — one call per change, each
of which can fail on its own. Code that says `commit_route(); save_topology()`
has already decided it is talking to a file, and every place that says it is a
place that would have to be found and rewritten later.

So callers say what they DID, not how to store it:

    store.commit_route(topology, route, "CIR-1001")
    store.release_route(topology, "CIR-1001")

and the store decides whether that means rewriting a file or calling an API.

THE LINE THAT MATTERS
---------------------
Planning routes a whole sheet against a private COPY of the map, committing
each row into that copy so row 2 sees what row 1 took. Those commits are a
simulation and must never reach ITA — nothing is being installed. They keep
calling pathengine directly, and that is deliberate: the store is only for
changes to the real map.

Getting that backwards would mean a preview quietly patching a live data
centre, so the two are kept visibly different rather than distinguished by a
flag someone can forget to pass.
"""

import contextlib

import bulkplan
import pathengine


class Store:
    """The operations the world above this line needs. A backend implements
    each one as whatever its storage actually supports."""

    name = "abstract"

    # ---- reading -----------------------------------------------------------
    def load(self):
        raise NotImplementedError

    # ---- changes to the real map -------------------------------------------
    def commit_route(self, topology, route, circuit_id):
        raise NotImplementedError

    def release_route(self, topology, circuit_id, force=False):
        raise NotImplementedError

    def truncate_route(self, topology, circuit_id):
        raise NotImplementedError

    def extend_route(self, topology, circuit_id, dst_port_id):
        raise NotImplementedError

    def place_device(self, topology, spec, site):
        raise NotImplementedError

    # ---- grouping ----------------------------------------------------------
    @contextlib.contextmanager
    def batch(self, topology):
        """Several changes that should be written back once.

        A ten-row sheet is ten circuits; a file store rewrites the file once at
        the end rather than ten times, and an API store still makes ten calls
        because that is what an API takes. Either way the caller writes the
        same code.
        """
        raise NotImplementedError


class JsonFileStore(Store):
    """The map as a single JSON file — the prototype's world.

    Every change is applied in memory and the file is rewritten. That is fine
    for a file and wrong for an API, which is the whole reason for the seam.
    """

    name = "json-file"

    def __init__(self, path=None):
        self.path = path or pathengine.TOPOLOGY_PATH
        self._depth = 0          # nesting level of batch()
        self._dirty = False

    def load(self):
        return pathengine.load_topology(self.path)

    # -- the changes ---------------------------------------------------------
    def commit_route(self, topology, route, circuit_id):
        circuit = pathengine.commit_route(topology, route, circuit_id)
        self._changed(topology)
        return circuit

    def release_route(self, topology, circuit_id, force=False):
        rec = pathengine.decommission_route(topology, circuit_id, force=force)
        self._changed(topology)
        return rec

    def truncate_route(self, topology, circuit_id):
        rec = pathengine.truncate_route(topology, circuit_id)
        self._changed(topology)
        return rec

    def extend_route(self, topology, circuit_id, dst_port_id):
        circuit = pathengine.extend_route(topology, circuit_id, dst_port_id)
        self._changed(topology)
        return circuit

    def place_device(self, topology, spec, site):
        dev_id = bulkplan.materialise(topology, spec, site)
        self._changed(topology)
        return dev_id

    # -- writing back --------------------------------------------------------
    def _changed(self, topology):
        self._dirty = True
        if self._depth == 0:
            self.flush(topology)

    def flush(self, topology):
        if self._dirty:
            pathengine.save_topology(topology, self.path)
            self._dirty = False

    @contextlib.contextmanager
    def batch(self, topology):
        self._depth += 1
        try:
            yield self
        finally:
            self._depth -= 1
            if self._depth == 0:
                # Written even when the block raised: the changes that DID
                # happen are already in the map in memory, and a file that
                # disagrees with memory is worse than one that is merely
                # further ahead than the caller expected.
                self.flush(topology)


class ItaStore(Store):
    """The real thing — not implemented, deliberately.

    Left here as the shape the integration has to fill, so the work is "write
    these six methods" rather than "find everywhere the prototype assumed a
    file". Each method says what ITA has to be able to do; when a call is
    added, the pathengine line above it stays exactly as it is, because the
    in-memory map must keep matching what was just asked of ITA — that mirror
    is what the router plans against.

    Notes for whoever fills this in:

      * load() is the big unknown. If ITA can export the whole inventory, this
        is one call and everything else works unchanged. If it can only be
        queried piecemeal, the mirror has to be built up and kept fresh, and
        THAT is the real integration project — not these methods.

      * every method must be safe to retry. A commit that timed out may or may
        not have happened; the next attempt has to be able to find out rather
        than blindly making a second circuit.

      * a failure must leave the mirror matching ITA. If the API call fails,
        the in-memory change has to be undone, or the router will plan against
        strands nobody actually has.
    """

    name = "ita-api"

    def __init__(self, client):
        self.client = client

    def load(self):
        raise NotImplementedError(
            "ITA inventory export -> the same shape as data/topology.json "
            "(racks, devices, ports, edges, circuits, pods, meta)")

    def commit_route(self, topology, route, circuit_id):
        raise NotImplementedError(
            "create one circuit in ITA: both endpoint ports, every trunk "
            "strand it takes, and every cross-connect port along the way — "
            "then apply pathengine.commit_route to the mirror")

    def release_route(self, topology, circuit_id, force=False):
        raise NotImplementedError(
            "delete the circuit in ITA and free its strands and ports — then "
            "apply pathengine.decommission_route to the mirror")

    def truncate_route(self, topology, circuit_id):
        raise NotImplementedError(
            "free the circuit's LAST strand and its far endpoint, leaving the "
            "rest of the path in place — then apply pathengine.truncate_route")

    def extend_route(self, topology, circuit_id, dst_port_id):
        raise NotImplementedError(
            "add a final leg from the circuit's open end to dst_port_id — "
            "then apply pathengine.extend_route to the mirror")

    def place_device(self, topology, spec, site):
        raise NotImplementedError(
            "create the device in ITA at site['rack'] / site['u_start'] with "
            "its ports — then apply bulkplan.materialise to the mirror")

    @contextlib.contextmanager
    def batch(self, topology):
        # An API has no "write it all at once": each change was already sent.
        # Grouping exists so callers can be written the same way for both.
        yield self


def open_store(kind="json", **kw):
    """The one line that changes when ITA arrives."""
    if kind == "json":
        return JsonFileStore(**kw)
    if kind == "ita":
        return ItaStore(**kw)
    raise ValueError(f"unknown store {kind!r}")
