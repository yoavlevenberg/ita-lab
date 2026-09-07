---
description: Run the ITA Lab test suite and report the result honestly.
allowed-tools: Bash, Read, Grep
---

Verify the working tree.

1. Make sure `networkx` imports. If not, `pip install networkx` — it is the only
   dependency and its absence makes every failure meaningless.
2. Run `python3 test_scenarios.py`. It takes roughly 35 seconds and exits
   non-zero on failure. The last line is `N/N checks passed`.
3. If anything failed, show the failing `[FAIL]` lines verbatim and diagnose
   before proposing a fix. Two possibilities are worth separating:
   - a real regression, or
   - a **fixture that ran out** — a test that hardcoded a port id and lost it to
     a real execution. That is a bug in the test, and the fix is to make the
     fixture search the topology for a device in the state it needs, never to
     restore the port by hand.
4. Report the exact count. Do not round, do not say "tests pass" without the
   number, and do not describe a failure as flaky — this suite is deterministic
   (`seed=42`).

If `$ARGUMENTS` is given, treat it as extra scope: run `python3 $ARGUMENTS`
as well and report it alongside (`test_agreement.py` takes a `--sheets` sweep
for a longer run).
