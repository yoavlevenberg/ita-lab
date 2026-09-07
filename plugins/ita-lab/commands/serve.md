---
description: Start the ITA Lab server and check the API without screenshots.
allowed-tools: Bash, Read
---

Bring the app up and confirm it actually works. Browser screenshot tooling has
timed out repeatedly on this project, so verification goes through the API and
the served HTML instead — that is the reliable path, not a fallback.

1. Ensure `networkx` is importable.
2. Start `python3 server.py` in the background. It binds `127.0.0.1:8800` and
   calls `webbrowser.open`, which is harmless where there is no browser.
3. Wait for the port to accept a connection, then exercise the API with
   `curl`, checking the shape of what comes back, not just the status code:
   - `GET /api/topology` — pods, racks, devices
   - `GET /api/zones` — the four colours, with the MDA pods neutral
   - `POST /api/route` — a source and destination port that are actually free
     (find them from the topology; **do not hardcode a port id**, they go stale
     the moment a plan is executed)
   - `POST /api/assist` — a question the offline assistant should classify
4. Fetch `/` and confirm the HTML is served whole.
5. Stop the server when done and report what you exercised.

Anything that writes — `/api/execute`, `/api/bulk/execute` — changes
`data/topology.json` for real. Do not call it to "check the endpoint"; only run
it when executing is the actual task, and commit the result.

If `$ARGUMENTS` names a specific endpoint or scenario, focus there.
