#!/usr/bin/env python3
"""
adapter.reference.py — the OFFLINE reference for the filesystem-to-runtime seam
(COCKPIT_SPEC §6).

`coord` only ever EMITS directives (append-only lines in
`state/directives.jsonl` under COORD_ROOT) — it never spawns a session or wakes
a worker itself. Turning a directive into a real running session/wake-up is a
host concern: a real adapter (not this file) watches the ledger and calls into
whatever actually creates sessions in that environment (e.g. this app's
`create_session`, a CI runner, a shell that starts a Copilot CLI process, ...).

This file is a THIN, INERT reference implementation of that seam:
  * it reads the directives ledger in order,
  * it prints, one line per directive, the *intended* action a real host
    would take,
  * it never performs that action itself — `--dry-run` is the default and
    only mode, and the one hand-off point (`_perform`) that a real host would
    replace is a deliberately-unimplemented stub here.

Stdlib only (argparse/json/pathlib/os/sys). No network. No import of any
app/runtime module — this file must stay inert and safe to run anywhere.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT_ENV = "COORD_ROOT"
DEFAULT_DIR = ".coordination"


def root() -> Path:
    """Resolve COORD_ROOT the same way coord.py does (env var, else default)."""
    return Path(os.environ.get(ROOT_ENV, DEFAULT_DIR)).resolve()


def directives_path() -> Path:
    return root() / "state" / "directives.jsonl"


def read_directives(path: Path):
    """Read directive lines from the ledger, in ledger order. Tolerate a
    missing file (no directives emitted yet) by yielding nothing."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def describe(directive: dict) -> str:
    """Render the one-line, deterministic description of the action a real
    host would take for this directive. Never performs the action."""
    kind = directive.get("kind")
    if kind == "spawn":
        worker = directive.get("worker")
        owned = directive.get("owned_paths", [])
        return f"create session role=editor worker={worker} owning={owned}"
    if kind == "dispatch":
        worker = directive.get("worker")
        task = directive.get("task")
        return f"wake worker={worker} task={task}"
    return f"skip {kind}"


def _perform(action: str) -> None:
    """HOST WIRES real create_session/wake HERE.

    This is the one hand-off point a real adapter replaces with an actual
    call into its runtime (e.g. spawning a session, waking a worker process).
    The reference implementation never calls this in --dry-run mode, and even
    if invoked directly it deliberately does nothing but signal that it is
    unimplemented — there is no host in this offline reference.
    """
    raise NotImplementedError(
        "adapter.reference.py has no host wired in; a real adapter replaces "
        "_perform() with an actual create_session/wake call. Got action: "
        f"{action!r}"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline reference for the coord directives -> runtime seam."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Only print intended actions; never perform them (default, and "
        "the only mode this reference implementation supports).",
    )
    args = parser.parse_args(argv)

    for directive in read_directives(directives_path()):
        action = describe(directive)
        print(action)
        if not args.dry_run:
            # A real host would call _perform(action) here. The reference
            # implementation always runs --dry-run (default=True above), so
            # this branch is unreachable via the CLI as shipped.
            _perform(action)

    return 0


if __name__ == "__main__":
    sys.exit(main())
