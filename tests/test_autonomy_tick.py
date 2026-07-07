"""
Autonomy-addendum acceptance tests for `coord/coord.py` (AUTONOMY_SPEC §3.3, §7.1-§7.4, §7.6-§7.7).

Covers Phase 3 — the keystone `coord tick`: ONE deterministic reconciliation pass that
reaps dead sessions, runs coded acceptance gates, requeues/escalates on repeated
verify failure, and surfaces open escalations — all strictly WITHIN the current human
authorization (it never touches `authorized_phase`, never approves/rejects a proposal,
and never performs a git write).

Like the other autonomy test files, each test drives the real CLI as a subprocess
against a throwaway COORD_ROOT (pytest `tmp_path`), never the repo's own
`.coordination/`. The interpreter is invoked via `sys.executable` (this box has no
`python3` alias). Verify commands are simulated with
`python -c "import sys; sys.exit(0|1)"` per AUTONOMY_SPEC §7.
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


def _register(coord, session, role="worker", branch=None):
    branch = branch or f"feat/{session}"
    r = coord("register", "--session", session, "--role", role, "--branch", branch)
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


def _inbox(coord, session):
    path = coord.root / "inbox" / f"{session}.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


PASS_CMD = f'"{sys.executable}" -c "import sys; sys.exit(0)"'
FAIL_CMD = f'"{sys.executable}" -c "import sys; sys.exit(1)"'


# --- §7.1 tick reaps a dead session's claimed task back to open ------------
def test_tick_reaps_stale_claimed_task_to_open(coord):
    coord("add-task", "--id", "dead-owner-task", "--desc", "d")
    # claim with a session that is never registered -> _heartbeat_stale() treats it as dead
    r = coord("claim", "--session", "ghost-worker", "--task", "dead-owner-task")
    assert r.returncode == 0, r.stderr
    assert _fold_task(coord, "dead-owner-task")["status"] == "claimed"

    tick = coord("tick")
    assert tick.returncode == 0, tick.stderr
    report = json.loads(tick.stdout)
    assert any(e["type"] == "task" and e["id"] == "dead-owner-task" and e["holder"] == "ghost-worker"
               for e in report["reaped"])

    assert _fold_task(coord, "dead-owner-task")["status"] == "open"


# --- gap fix: tick re-dispatches a task reaped from a dead worker -----------
def test_tick_redispatches_reaped_task_to_live_worker(coord):
    _register(coord, "w1")  # live, idle worker -> should receive the re-dispatch
    coord("add-task", "--id", "orphan-task", "--desc", "d")
    # dead1 is never registered -> _heartbeat_stale(dead1) is True, simulating a dead claimant
    r = coord("claim", "--session", "dead1", "--task", "orphan-task")
    assert r.returncode == 0, r.stderr

    tick = coord("tick")
    assert tick.returncode == 0, tick.stderr
    report = json.loads(tick.stdout)

    # the reap step folds the task back to 'open', but the reference fold's
    # `is not None` guard means the stale claimed_by="dead1" lingers in the fold
    # (see AUTONOMY_SPEC gap) -- dispatch must still treat this task as dispatchable.
    assert any(d["task"] == "orphan-task" and d["to"] == "w1" for d in report["dispatched"]), report["dispatched"]
    assert _fold_task(coord, "orphan-task")["status"] == "open"


# --- regression guard: a live failing-verify claimant's requeue is NOT re-dispatched --
def test_tick_does_not_dispatch_task_requeued_from_live_failing_verify(coord):
    _register(coord, "w-idle")  # live, idle worker present as a dispatch candidate
    _register(coord, "worker-fail2")
    coord("add-task", "--id", "gate-fail2", "--desc", "d", "--verify", FAIL_CMD, "--max-attempts", "5")
    coord("claim", "--session", "worker-fail2", "--task", "gate-fail2")
    coord("complete", "--session", "worker-fail2", "--task", "gate-fail2")

    tick = coord("tick")
    assert tick.returncode == 0, tick.stderr
    report = json.loads(tick.stdout)

    # this task is handled by the notify-claimant retry path (requeued), not dispatch --
    # dispatching it to a second worker would double-work it.
    assert any(r["task"] == "gate-fail2" for r in report["requeued"])
    assert not any(d["task"] == "gate-fail2" for d in report["dispatched"])


# --- §7.2 tick runs a passing verify on a done task -> stays done, gains verified --
def test_tick_runs_passing_verify_and_marks_verified(coord):
    _register(coord, "worker-pass")
    coord("add-task", "--id", "gate-pass", "--desc", "d", "--verify", PASS_CMD)
    coord("claim", "--session", "worker-pass", "--task", "gate-pass")
    coord("complete", "--session", "worker-pass", "--task", "gate-pass")

    tick = coord("tick")
    assert tick.returncode == 0, tick.stderr
    report = json.loads(tick.stdout)
    assert "gate-pass" in report["verified"]

    t = _fold_task(coord, "gate-pass")
    assert t["status"] == "done"
    assert t["verified"] is True


# --- §7.3 tick runs a failing verify -> requeued, attempts++, claimant notified ----
def test_tick_runs_failing_verify_requeues_and_notifies(coord):
    _register(coord, "worker-fail")
    coord("add-task", "--id", "gate-fail", "--desc", "d", "--verify", FAIL_CMD, "--max-attempts", "5")
    coord("claim", "--session", "worker-fail", "--task", "gate-fail")
    coord("complete", "--session", "worker-fail", "--task", "gate-fail")

    tick = coord("tick")
    assert tick.returncode == 0, tick.stderr
    report = json.loads(tick.stdout)
    requeued_ids = [r["task"] for r in report["requeued"]]
    assert "gate-fail" in requeued_ids

    t = _fold_task(coord, "gate-fail")
    assert t["status"] == "open"
    assert t["attempts"] == 1
    assert t["verified"] is False

    msgs = _inbox(coord, "worker-fail")
    assert any("gate-fail" in m["body"] and "failed verify" in m["body"] for m in msgs)


# --- §7.4 after max_attempts failing verifies -> failed + blocker escalation ------
def test_tick_marks_failed_and_escalates_after_max_attempts(coord):
    _register(coord, "worker-maxfail")
    coord("add-task", "--id", "gate-maxfail", "--desc", "d", "--verify", FAIL_CMD, "--max-attempts", "1")
    coord("claim", "--session", "worker-maxfail", "--task", "gate-maxfail")
    coord("complete", "--session", "worker-maxfail", "--task", "gate-maxfail")

    tick = coord("tick")
    assert tick.returncode == 0, tick.stderr
    report = json.loads(tick.stdout)
    failed_ids = [f["task"] for f in report["failed"]]
    assert "gate-maxfail" in failed_ids

    t = _fold_task(coord, "gate-maxfail")
    assert t["status"] == "failed"
    assert t["attempts"] == 1

    escalations = coord("escalations", "--json")
    escs = json.loads(escalations.stdout)
    assert any(e["kind"] == "blocker" and e.get("task") == "gate-maxfail" and e["status"] == "open"
               for e in escs)


# --- §7.6 tick reports an open decision escalation under awaiting_decision --------
def test_tick_surfaces_open_decision_escalation(coord):
    r = coord("escalate", "--session", "nav", "--kind", "decision", "--body", "pick a palette")
    assert r.returncode == 0, r.stderr

    tick = coord("tick")
    assert tick.returncode == 0, tick.stderr
    report = json.loads(tick.stdout)
    assert any(e["kind"] == "decision" and e["body"] == "pick a palette" for e in report["awaiting_decision"])


# --- §7.7 invariant: tick never touches version / authorized_phase ---------------
def test_tick_invariant_leaves_version_and_authorized_phase_unchanged(coord):
    # establish authorized_phase via desired state, matching how the orchestrator gates phases
    coord("state", "set", "--session", "orch", "--key", "authorized_phase", "--value", "2")
    before = json.loads(coord("state", "show").stdout)

    # give tick real work to do: a dead claim to reap, a passing verify, a failing verify,
    # and an open escalation to surface -- so this is a "working" tick, not a no-op.
    coord("add-task", "--id", "dead-task", "--desc", "d")
    coord("claim", "--session", "ghost", "--task", "dead-task")

    _register(coord, "worker-ok")
    coord("add-task", "--id", "gate-ok", "--desc", "d", "--verify", PASS_CMD)
    coord("claim", "--session", "worker-ok", "--task", "gate-ok")
    coord("complete", "--session", "worker-ok", "--task", "gate-ok")

    _register(coord, "worker-bad")
    coord("add-task", "--id", "gate-bad", "--desc", "d", "--verify", FAIL_CMD, "--max-attempts", "1")
    coord("claim", "--session", "worker-bad", "--task", "gate-bad")
    coord("complete", "--session", "worker-bad", "--task", "gate-bad")

    coord("escalate", "--session", "nav", "--kind", "fork", "--body", "two valid approaches")

    tick = coord("tick")
    assert tick.returncode == 0, tick.stderr
    report = json.loads(tick.stdout)
    # sanity: this tick actually did work across every step
    assert report["reaped"]
    assert report["verified"] or report["requeued"] or report["failed"]
    assert report["awaiting_decision"]

    after = json.loads(coord("state", "show").stdout)
    assert after["version"] == before["version"]
    assert after["desired"].get("authorized_phase") == before["desired"].get("authorized_phase") == 2
