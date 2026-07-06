"""
Autonomy-addendum acceptance tests for `coord/coord.py` (AUTONOMY_SPEC §2, §3.1).

Covers Phase 1 — coded acceptance gates:
  * `add-task --verify ... --max-attempts ...` persists the fields on the event.
  * `coord verify --task <id>` runs the verify command in the claimant's registered
    worktree: rc==0 records `verified: true` and leaves the folded status alone;
    rc!=0 records `verified: false`, increments `attempts`, and exits non-zero.
  * A task with no `verify` set trivially verifies (records `verified: true`, exit 0).

Like tests/test_coord.py and tests/test_navigator_coord.py, each test drives the real
CLI as a subprocess against a throwaway COORD_ROOT (pytest `tmp_path`), never the
repo's own `.coordination/`. The interpreter is invoked via `sys.executable` (this box
has no `python3` alias).
"""
from __future__ import annotations

import json
import os
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


def _register(coord, session, role="worker", branch=None, worktree=None):
    branch = branch or f"feat/{session}"
    args = ["register", "--session", session, "--role", role, "--branch", branch]
    if worktree:
        args += ["--worktree", str(worktree)]
    r = coord(*args)
    assert r.returncode == 0, r.stderr
    return r


def _fold_task(coord, task_id: str) -> dict:
    """Fold board/tasks.jsonl the way _fold_tasks does and return task_id's folded dict."""
    lines = (coord.root / "board" / "tasks.jsonl").read_text(encoding="utf-8").splitlines()
    t = {"id": task_id, "status": "open", "deps": [], "claimed_by": None, "attempts": 0, "verified": False}
    seen = False
    for line in lines:
        line = line.strip()
        if not line:
            continue
        ev = json.loads(line)
        if ev.get("id") != task_id:
            continue
        seen = True
        for k in ("desc", "deps", "status", "claimed_by", "claimed_at_version",
                  "verify", "max_attempts", "attempts", "verified"):
            if k in ev and ev[k] is not None:
                t[k] = ev[k]
    assert seen, f"no events found for task '{task_id}'"
    return t


def _add_verified_claimed_task(coord, task_id, verify_cmd, session="worker", worktree=None):
    _register(coord, session, worktree=worktree)
    r = coord("add-task", "--id", task_id, "--desc", "d", "--verify", verify_cmd)
    assert r.returncode == 0, r.stderr
    r = coord("claim", "--session", session, "--task", task_id)
    assert r.returncode == 0, r.stderr
    r = coord("complete", "--session", session, "--task", task_id)
    assert r.returncode == 0, r.stderr
    return session


# --- add-task persists --verify / --max-attempts ---------------------------
def test_add_task_persists_verify_and_max_attempts(coord):
    r = coord("add-task", "--id", "gate-1", "--desc", "d",
              "--verify", f"{sys.executable} -c \"import sys; sys.exit(0)\"", "--max-attempts", "3")
    assert r.returncode == 0, r.stderr
    t = _fold_task(coord, "gate-1")
    assert t["verify"] == f"{sys.executable} -c \"import sys; sys.exit(0)\""
    assert t["max_attempts"] == 3


def test_add_task_without_verify_leaves_fields_absent(coord):
    r = coord("add-task", "--id", "no-gate")
    assert r.returncode == 0, r.stderr
    t = _fold_task(coord, "no-gate")
    assert "verify" not in t
    assert "max_attempts" not in t


# --- passing verify records verified: true, status unchanged ---------------
def test_verify_passing_records_verified_true_and_preserves_status(coord, tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    verify_cmd = f'"{sys.executable}" -c "import sys; sys.exit(0)"'
    _add_verified_claimed_task(coord, "gate-pass", verify_cmd, session="worker-pass", worktree=worktree)

    before = _fold_task(coord, "gate-pass")
    assert before["status"] == "done"
    assert before["verified"] is False

    r = coord("verify", "--task", "gate-pass")
    assert r.returncode == 0, r.stderr

    after = _fold_task(coord, "gate-pass")
    assert after["verified"] is True
    assert after["status"] == "done"  # verify event doesn't touch status
    assert after["attempts"] == 0


# --- failing verify increments attempts and exits non-zero -----------------
def test_verify_failing_increments_attempts_and_exits_nonzero(coord, tmp_path):
    worktree = tmp_path / "wt2"
    worktree.mkdir()
    verify_cmd = f'"{sys.executable}" -c "import sys; sys.exit(1)"'
    _add_verified_claimed_task(coord, "gate-fail", verify_cmd, session="worker-fail", worktree=worktree)

    r = coord("verify", "--task", "gate-fail")
    assert r.returncode != 0

    after = _fold_task(coord, "gate-fail")
    assert after["verified"] is False
    assert after["attempts"] == 1
    assert after["status"] == "done"  # verify event doesn't touch status

    # a second failing run increments attempts again
    r2 = coord("verify", "--task", "gate-fail")
    assert r2.returncode != 0
    after2 = _fold_task(coord, "gate-fail")
    assert after2["attempts"] == 2


# --- no verify set trivially verifies ---------------------------------------
def test_verify_with_no_verify_command_trivially_passes(coord):
    _register(coord, "worker-trivial")
    coord("add-task", "--id", "gate-trivial", "--desc", "no gate")
    coord("claim", "--session", "worker-trivial", "--task", "gate-trivial")
    coord("complete", "--session", "worker-trivial", "--task", "gate-trivial")

    r = coord("verify", "--task", "gate-trivial")
    assert r.returncode == 0, r.stderr

    after = _fold_task(coord, "gate-trivial")
    assert after["verified"] is True


def test_verify_json_output(coord, tmp_path):
    worktree = tmp_path / "wt3"
    worktree.mkdir()
    verify_cmd = f'"{sys.executable}" -c "import sys; sys.exit(0)"'
    _add_verified_claimed_task(coord, "gate-json", verify_cmd, session="worker-json", worktree=worktree)

    r = coord("verify", "--task", "gate-json", "--json")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["task"] == "gate-json"
    assert out["verified"] is True
