#!/usr/bin/env bash
# Three things that have to be true before any work on this repo starts, and
# that a fresh container gets wrong by default:
#
#   1. networkx is importable  — the only third-party dependency; without it
#      nothing imports and every test fails for a reason that is not the code.
#   2. the checkout is current — the repo is worked on from several machines,
#      so `git status` alone is not enough to know what is here.
#   3. data/topology.json is real data — uncommitted changes to it are executed
#      circuits that only exist in this container.
#
# Never fails the session: everything here is a report, not a gate.

set -uo pipefail

cd "${CLAUDE_PROJECT_DIR:-$PWD}" 2>/dev/null || exit 0
[ -f pathengine.py ] || exit 0   # not the ITA Lab checkout; say nothing

notes=()

# --- 1. the one dependency -------------------------------------------------
if ! python3 -c "import networkx" >/dev/null 2>&1; then
  if pip install --quiet --timeout 60 --retries 3 networkx >/dev/null 2>&1 \
     || pip install --quiet --break-system-packages --timeout 60 --retries 3 networkx >/dev/null 2>&1; then
    notes+=("installed networkx (the only dependency; it was missing)")
  else
    notes+=("networkx is MISSING and could not be installed — nothing will import until it is")
  fi
fi

# --- 2. current checkout ---------------------------------------------------
branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)" || exit 0
export GIT_TERMINAL_PROMPT=0
if git fetch --quiet origin "$branch" 2>/dev/null; then
  behind="$(git rev-list --count "HEAD..origin/$branch" 2>/dev/null || echo 0)"
  if [ "${behind:-0}" -gt 0 ]; then
    if [ -z "$(git status --porcelain)" ] && git merge --ff-only "origin/$branch" >/dev/null 2>&1; then
      notes+=("fast-forwarded $branch by $behind commit(s) from origin")
    else
      notes+=("$branch is $behind commit(s) behind origin and could not fast-forward — pull before editing")
    fi
  fi
elif git ls-remote --exit-code --heads origin >/dev/null 2>&1; then
  notes+=("$branch is not on origin yet — nothing to pull, push it when the work is ready")
else
  notes+=("could not reach origin; working from the local checkout of $branch")
fi

# --- 3. topology.json is data, not a build artifact ------------------------
if [ -n "$(git status --porcelain -- data/topology.json 2>/dev/null)" ]; then
  notes+=("data/topology.json has UNCOMMITTED changes — those are executed circuits that exist only here. Commit them before running generate_topology.py, which would overwrite them.")
fi

if [ ${#notes[@]} -gt 0 ]; then
  echo "ITA Lab:"
  for n in "${notes[@]}"; do echo "  - $n"; done
fi
exit 0
