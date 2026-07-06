"""
Navigator-addendum acceptance tests for `coord/coord.py` (NAVIGATOR_SPEC §7.1–§7.6).

These cover the proposal lifecycle and the stale-completion guard that make
"conversation without authority" safe:
  §7.1  propose writes a PENDING proposal and does NOT bump the live version.
  §7.2  proposals lists the pending one with current -> proposed values.
  §7.3  approve bumps the version, applies the value, marks the proposal applied.
  §7.4  approve --invalidates T requeues T (folded status -> open) AND drops a
        FRESH (as_of == new version) message into the prior claimant's inbox.
  §7.5  after that approve, the prior claimant's `complete T` is refused (non-zero).
  §7.6  reject leaves the version unchanged and marks the proposal rejected.

Like tests/test_coord.py, each test drives the real CLI as a subprocess against a
throwaway COORD_ROOT (pytest `tmp_path`), never the repo's own `.coordination/`.
The interpreter is invoked via `sys.executable` (this box has no `python3` alias).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

COORD_PY = Path(__file__).resolve().parent.parent / "coord" / "coord.py"


@pytest.fixture
def coord(tmp_path):
    """Return a `run(*args)` callable bound to an initialized, isolated plane."""
    root = tmp_path / "coordination"

    def run(*args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["COORD_ROOT"] = str(root)
        return subprocess.run(
            [sys.executable, str(COORD_PY), *args],
            capture_output=True,
            text=True,
            env=env,
        )

    run.root = root.resolve()  # coord.py resolves COORD_ROOT; read artifacts from the same place
    init = run("init")
    assert init.returncode == 0, init.stderr
    return run


def _register(coord, session, role="worker", branch=None):
    branch = branch or f"feat/{session}"
    r = coord("register", "--session", session, "--role", role, "--branch", branch)
    assert r.returncode == 0, r.stderr
    return r


def _pid_from(stdout: str) -> str:
    """Extract the proposal id printed by `state propose`."""
    m = re.search(r"proposed (\d+):", stdout)
    assert m, f"no proposal id found in: {stdout!r}"
    return m.group(1)


def _version(coord) -> int:
    return json.loads(coord("state", "show").stdout)["version"]


def _fold_status(coord, task_id: str):
    """Fold board/tasks.jsonl the way _fold_tasks does and return task_id's status."""
    lines = (coord.root / "board" / "tasks.jsonl").read_text(encoding="utf-8").splitlines()
    status = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        ev = json.loads(line)
        if ev.get("id") == task_id and ev.get("status") is not None:
            status = ev["status"]
    return status


# --- §7.1 propose is pending and does not bump the version -----------------
def test_propose_writes_pending_and_does_not_bump_version(coord):
    assert _version(coord) == 0
    r = coord("state", "propose", "--session", "nav", "--key", "target_palette",
              "--value", '"v2"', "--note", "switch palette")
    assert r.returncode == 0, r.stderr
    # the live desired-state version must be unchanged
    assert _version(coord) == 0
    # exactly one pending proposal was written, with the expected shape
    props = list((coord.root / "state" / "proposals").glob("*.json"))
    assert len(props) == 1, props
    prop = json.loads(props[0].read_text(encoding="utf-8"))
    assert prop["status"] == "pending"
    assert prop["from"] == "nav"
    assert prop["key"] == "target_palette"
    assert prop["value"] == "v2"          # parsed as JSON, not the literal string '"v2"'
    assert prop["base_version"] == 0
    assert prop["note"] == "switch palette"


# --- §7.2 proposals lists current -> proposed ------------------------------
def test_proposals_lists_pending_with_current_and_proposed(coord):
    # establish a current value so the diff has a non-null left side
    coord("state", "set", "--session", "orch", "--key", "target_palette", "--value", '"v1"')
    r = coord("state", "propose", "--session", "nav", "--key", "target_palette", "--value", '"v2"')
    pid = _pid_from(r.stdout)

    listing = coord("state", "proposals")
    assert listing.returncode == 0, listing.stderr
    out = listing.stdout
    assert pid in out
    assert "from=nav" in out
    assert "target_palette" in out
    # current ("v1") -> proposed ("v2")
    assert '"v1"' in out and '"v2"' in out


# --- §7.3 approve bumps version, applies value, marks applied --------------
def test_approve_bumps_version_applies_value_and_marks_applied(coord):
    r = coord("state", "propose", "--session", "nav", "--key", "target_palette", "--value", '"v2"')
    pid = _pid_from(r.stdout)
    assert _version(coord) == 0  # still 0 before approval

    ap = coord("state", "approve", "--id", pid, "--session", "human")
    assert ap.returncode == 0, ap.stderr

    st = json.loads(coord("state", "show").stdout)
    assert st["version"] == 1
    assert st["desired"]["target_palette"] == "v2"

    prop = json.loads((coord.root / "state" / "proposals" / f"{pid}.json").read_text(encoding="utf-8"))
    assert prop["status"] == "applied"


# --- §7.4 approve --invalidates requeues + notifies the claimant -----------
def test_approve_invalidate_requeues_task_and_notifies_claimant_freshly(coord):
    _register(coord, "w1", role="editor")
    coord("add-task", "--id", "build-thing", "--desc", "build it")
    assert coord("claim", "--session", "w1", "--task", "build-thing").returncode == 0
    assert _fold_status(coord, "build-thing") == "claimed"

    r = coord("state", "propose", "--session", "nav", "--key", "target_palette",
              "--value", '"v2"', "--invalidates", "build-thing")
    pid = _pid_from(r.stdout)
    ap = coord("state", "approve", "--id", pid, "--session", "human")
    assert ap.returncode == 0, ap.stderr

    new_version = json.loads(coord("state", "show").stdout)["version"]
    assert new_version == 1

    # (a) the invalidated task is folded back to open (requeued)
    assert _fold_status(coord, "build-thing") == "open"

    # (b) a FRESH message (as_of == new version) landed in the claimant's inbox:
    #     checkpoint surfaces it as fresh, not stale-skipped.
    cp = json.loads(coord("checkpoint", "--session", "w1").stdout)
    assert cp["desired_version"] == new_version
    assert cp["stale_messages_skipped"] == 0
    assert len(cp["messages"]) == 1
    msg = cp["messages"][0]
    assert msg["as_of"] == new_version
    assert "build-thing" in msg["body"] and "invalidated" in msg["body"]


# --- §7.5 stale completion is refused after invalidation -------------------
def test_stale_completion_is_refused_after_invalidation(coord):
    _register(coord, "w1", role="editor")
    coord("add-task", "--id", "build-thing")
    assert coord("claim", "--session", "w1", "--task", "build-thing").returncode == 0

    r = coord("state", "propose", "--session", "nav", "--key", "target",
              "--value", '"v2"', "--invalidates", "build-thing")
    pid = _pid_from(r.stdout)
    assert coord("state", "approve", "--id", pid, "--session", "human").returncode == 0

    # the worker kept going and tries to complete an invalidated task -> refused
    done = coord("complete", "--session", "w1", "--task", "build-thing")
    assert done.returncode != 0, done.stdout + done.stderr
    assert "cannot complete" in done.stderr
    # and it was NOT marked done
    assert _fold_status(coord, "build-thing") == "open"


# --- §7.6 reject leaves the version unchanged ------------------------------
def test_reject_leaves_version_unchanged_and_marks_rejected(coord):
    coord("state", "set", "--session", "orch", "--key", "target", "--value", '"v1"')
    base = _version(coord)

    r = coord("state", "propose", "--session", "nav", "--key", "target", "--value", '"v2"')
    pid = _pid_from(r.stdout)

    rej = coord("state", "reject", "--id", pid, "--reason", "not now")
    assert rej.returncode == 0, rej.stderr

    assert _version(coord) == base  # unchanged

    prop = json.loads((coord.root / "state" / "proposals" / f"{pid}.json").read_text(encoding="utf-8"))
    assert prop["status"] == "rejected"
    assert prop.get("reason") == "not now"
    # no longer surfaced as pending
    assert "(no pending proposals)" in coord("state", "proposals").stdout
