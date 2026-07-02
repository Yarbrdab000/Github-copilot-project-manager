#!/usr/bin/env bash
# session_register.sh — Copilot `sessionStart` hook.
#
# Registers this session in the coordination control plane and writes a first heartbeat so
# the write-scope guard can resolve it and so `coord reap` can tell live sessions from dead
# ones. Reads (and ignores) the sessionStart payload on stdin; configuration comes from env.
#
# Coordination is OPT-IN: if COORD_SESSION is unset this hook is a no-op, so sessions that
# are not part of a coordinated run are unaffected. Registration is best-effort — a failure
# here must never block the session from starting.
#
#   COORD_SESSION   required; the session id (e.g. "editor", "researcher")
#   COORD_ROLE      optional; defaults to "worker"
#   COORD_BRANCH    optional; defaults to the current git branch
#   COORD_WORKTREE  optional; defaults to $PWD
#   COORD_PATHS     optional; comma-separated globs this session may write
#   COORD_ROOT      optional; control-plane location (see coord/coord.py; default .coordination)

cat >/dev/null 2>&1 || true   # drain the stdin payload (unused)

if [ -z "${COORD_SESSION:-}" ]; then
  exit 0
fi

ROLE="${COORD_ROLE:-worker}"
BRANCH="${COORD_BRANCH:-$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)}"
WORKTREE="${COORD_WORKTREE:-$PWD}"
PATHS="${COORD_PATHS:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
COORD="${REPO_ROOT}/coord/coord.py"

PY="python3"
command -v python3 >/dev/null 2>&1 || PY="python"

"$PY" "$COORD" register \
  --session "$COORD_SESSION" \
  --role "$ROLE" \
  --branch "$BRANCH" \
  --worktree "$WORKTREE" \
  ${PATHS:+--paths "$PATHS"} || true
"$PY" "$COORD" heartbeat --session "$COORD_SESSION" || true

exit 0
