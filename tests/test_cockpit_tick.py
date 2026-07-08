"""
Cockpit addendum tests — `tick`'s fleet spawn step + concurrency cap
(COCKPIT_SPEC.md §3.5, acceptance §7.6-§7.7).

Each test drives the real CLI as a subprocess against a throwaway COORD_ROOT (pytest
`tmp_path`), never the repo's own `.coordination/`, mirroring the fixture pattern used
in tests/test_autonomy_tick.py.
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

    run.root = root.resolve()
    init = run("init")
    assert init.returncode == 0, init.stderr
    return run


def _register(coord, session, role="worker", branch=None):
    branch = branch or f"feat/{session}"
    r = coord("register", "--session", session, "--role", role, "--branch", branch)
    assert r.returncode == 0, r.stderr
    return r


def _read_jsonl_file(path: Path):
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _directives(coord):
    return _read_jsonl_file(coord.root / "state" / "directives.jsonl")


def _spawn_directives(coord):
    return [d for d in _directives(coord) if d.get("kind") == "spawn"]


def _propose_and_approve_plan(coord, doc, tmp_path, name="plan.json"):
    plan_file = tmp_path / name
    plan_file.write_text(json.dumps(doc), encoding="utf-8")
    proposed = coord("plan", "propose", "--file", str(plan_file))
    assert proposed.returncode == 0, proposed.stdout + proposed.stderr
    pid = proposed.stdout.split()[2]
    approved = coord("plan", "approve", "--id", pid)
    assert approved.returncode == 0, approved.stdout + approved.stderr
    return pid


def _plan_doc(max_concurrent, worker_ids, task_per_worker=True):
    workers = [{"id": wid, "owned_paths": [f"src/{wid}/**"]} for wid in worker_ids]
    tasks = []
    if task_per_worker:
        for wid in worker_ids:
            tasks.append({"id": f"{wid}-task", "desc": "d", "owned_by": wid, "deps": [], "verify": None})
    return {"note": "n", "fleet": {"max_concurrent": max_concurrent, "workers": workers}, "tasks": tasks}


def _escalations(coord):
    out = []
    esc_dir = coord.root / "escalations"
    if not esc_dir.exists():
        return out
    for p in esc_dir.glob("*.json"):
        out.append(json.loads(p.read_text(encoding="utf-8")))
    return out


# --- §7.6: spawn emitted for each declared-but-not-live worker, capped -------------
def test_tick_emits_spawn_directive_for_each_missing_worker_up_to_cap(coord, tmp_path):
    # 3 declared workers, none registered/live, all own an open task, cap = 3 -> spawn all 3.
    doc = _plan_doc(max_concurrent=3, worker_ids=["w-a", "w-b", "w-c"])
    _propose_and_approve_plan(coord, doc, tmp_path)

    # `plan approve` itself emits an initial capped batch (COCKPIT_SPEC §3.4) -- clear the
    # ledger's slate isn't possible (append-only), so assert on the state AFTER approve's
    # own directives, which is exactly what `tick`'s idempotency step must respect too.
    after_approve = _spawn_directives(coord)
    assert {d["worker"] for d in after_approve} == {"w-a", "w-b", "w-c"}
    assert len(after_approve) == 3

    tick = coord("tick")
    assert tick.returncode == 0, tick.stderr
    report = json.loads(tick.stdout)
    # nothing new to spawn: all 3 already have an in-flight directive from approve.
    assert report["spawned"] == []
    assert len(_spawn_directives(coord)) == 3


def test_tick_spawns_missing_workers_never_exceeding_cap(coord, tmp_path):
    # 3 declared workers, cap = 3, but approve only spawns up to its OWN cap of 2 so we can
    # see tick's spawn step pick up the remaining demand under the (still) global cap.
    doc = _plan_doc(max_concurrent=2, worker_ids=["w-a", "w-b", "w-c"])
    _propose_and_approve_plan(coord, doc, tmp_path)

    after_approve = _spawn_directives(coord)
    assert len(after_approve) == 2  # capped at max_concurrent=2 by approve itself

    tick = coord("tick")
    assert tick.returncode == 0, tick.stderr
    report = json.loads(tick.stdout)
    # w-c is missing and not live, but live(0)+in_flight(2) already == max_concurrent(2):
    # no more capacity -> tick must NOT spawn it, and must escalate instead.
    assert report["spawned"] == []
    assert len(_spawn_directives(coord)) == 2
    assert any(e["kind"] == "decision" for e in _escalations(coord))


def test_tick_dispatch_still_works_for_a_live_worker_with_spawn_step_present(coord, tmp_path):
    # A live, registered worker with a claimable task must still get dispatched even
    # though the spawn step now runs first.
    doc = _plan_doc(max_concurrent=3, worker_ids=["w-live"])
    _propose_and_approve_plan(coord, doc, tmp_path)
    _register(coord, "w-live")

    tick = coord("tick")
    assert tick.returncode == 0, tick.stderr
    report = json.loads(tick.stdout)
    # w-live is live -> not "missing" -> no spawn directive for it.
    assert report["spawned"] == []
    assert any(d["task"] == "w-live-task" and d["to"] == "w-live" for d in report["dispatched"])


# --- §7.7: demand exceeds cap -> exactly one decision escalation, no over-spawn, invariant ---
def test_tick_over_cap_demand_opens_exactly_one_decision_escalation(coord, tmp_path):
    doc = _plan_doc(max_concurrent=1, worker_ids=["w-a", "w-b", "w-c"])
    # propose without approve's own spawn emission interfering: use max_concurrent=1 so
    # approve itself only spawns 1, leaving 2 workers genuinely missing under the cap.
    _propose_and_approve_plan(coord, doc, tmp_path)
    assert len(_spawn_directives(coord)) == 1

    coord("state", "set", "--session", "orch", "--key", "authorized_phase", "--value", "3")
    before = json.loads(coord("state", "show").stdout)

    tick = coord("tick")
    assert tick.returncode == 0, tick.stderr
    report = json.loads(tick.stdout)

    assert report["spawned"] == []
    assert len(_spawn_directives(coord)) == 1  # no over-spawn

    decisions = [e for e in _escalations(coord) if e["kind"] == "decision"
                 and e["body"] == "fleet at cap; raise max_concurrent or wait"]
    assert len(decisions) == 1  # exactly one, not one per over-cap worker

    after = json.loads(coord("state", "show").stdout)
    assert after["version"] == before["version"]
    assert after["desired"].get("authorized_phase") == before["desired"].get("authorized_phase") == 3


# --- over-cap decision escalation must be deduped across repeated ticks (no spam) --------
def test_tick_over_cap_decision_deduped_across_repeated_ticks(coord, tmp_path):
    doc = _plan_doc(max_concurrent=1, worker_ids=["w-a", "w-b", "w-c"])
    _propose_and_approve_plan(coord, doc, tmp_path)
    assert len(_spawn_directives(coord)) == 1

    coord("state", "set", "--session", "orch", "--key", "authorized_phase", "--value", "3")
    before = json.loads(coord("state", "show").stdout)

    for _ in range(3):
        tick = coord("tick")
        assert tick.returncode == 0, tick.stderr

    assert len(_spawn_directives(coord)) == 1  # still no over-spawn after 3 ticks

    decisions = [e for e in _escalations(coord) if e["kind"] == "decision"
                 and e["status"] == "open"
                 and e["body"] == "fleet at cap; raise max_concurrent or wait"]
    assert len(decisions) == 1  # deduped, not re-opened on every tick

    after = json.loads(coord("state", "show").stdout)
    assert after["version"] == before["version"]
    assert after["desired"].get("authorized_phase") == before["desired"].get("authorized_phase") == 3


# --- idempotency: a second tick with the same still-not-live workers doesn't duplicate ---
def test_tick_does_not_duplicate_spawn_directives_across_ticks(coord, tmp_path):
    doc = _plan_doc(max_concurrent=5, worker_ids=["w-a", "w-b"])
    _propose_and_approve_plan(coord, doc, tmp_path)
    after_approve = len(_spawn_directives(coord))
    assert after_approve == 2

    tick1 = coord("tick")
    assert tick1.returncode == 0, tick1.stderr
    assert json.loads(tick1.stdout)["spawned"] == []
    assert len(_spawn_directives(coord)) == after_approve

    tick2 = coord("tick")
    assert tick2.returncode == 0, tick2.stderr
    assert json.loads(tick2.stdout)["spawned"] == []
    assert len(_spawn_directives(coord)) == after_approve  # still stable, no duplicates


# --- no fleet declared: spawn step is a no-op, doesn't crash on legacy planes ---
def test_tick_with_no_fleet_declared_does_not_crash_and_spawns_nothing(coord):
    coord("add-task", "--id", "plain-task", "--desc", "d")
    tick = coord("tick")
    assert tick.returncode == 0, tick.stderr
    report = json.loads(tick.stdout)
    assert report["spawned"] == []
    assert _spawn_directives(coord) == []
