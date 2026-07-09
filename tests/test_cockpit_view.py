"""
Cockpit addendum tests — `coord cockpit [--json]` read-only aggregate view
(COCKPIT_SPEC.md §3.6, acceptance §7.8).

Drives the real CLI as a subprocess against a throwaway COORD_ROOT (pytest `tmp_path`),
never the repo's own `.coordination/`, mirroring the fixture pattern used in
tests/test_cockpit_tick.py.
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


def _board_event_count(coord):
    return len(_read_jsonl_file(coord.root / "board" / "events.jsonl")) + \
        len(_read_jsonl_file(coord.root / "board" / "tasks.jsonl"))


def _escalation_count(coord):
    esc_dir = coord.root / "escalations"
    return len(list(esc_dir.glob("*.json"))) if esc_dir.exists() else 0


def _propose_and_approve_plan(coord, doc, tmp_path, name="plan.json"):
    plan_file = tmp_path / name
    plan_file.write_text(json.dumps(doc), encoding="utf-8")
    proposed = coord("plan", "propose", "--file", str(plan_file))
    assert proposed.returncode == 0, proposed.stdout + proposed.stderr
    pid = proposed.stdout.split()[2]
    approved = coord("plan", "approve", "--id", pid)
    assert approved.returncode == 0, approved.stdout + approved.stderr
    return pid


def _propose_plan(coord, doc, tmp_path, name="plan2.json"):
    plan_file = tmp_path / name
    plan_file.write_text(json.dumps(doc), encoding="utf-8")
    proposed = coord("plan", "propose", "--file", str(plan_file))
    assert proposed.returncode == 0, proposed.stdout + proposed.stderr
    return proposed.stdout.split()[2]


def _plan_doc(max_concurrent, worker_ids, task_per_worker=True):
    workers = [{"id": wid, "owned_paths": [f"src/{wid}/**"]} for wid in worker_ids]
    tasks = []
    if task_per_worker:
        for wid in worker_ids:
            tasks.append({"id": f"{wid}-task", "desc": "d", "owned_by": wid, "deps": [], "verify": None})
    return {"note": "n", "fleet": {"max_concurrent": max_concurrent, "workers": workers}, "tasks": tasks}


def _build_scenario(coord, tmp_path):
    """Approve a 2-worker plan; register+claim with w-a (live); leave w-b unregistered
    (stale/absent); mark w-b's task done; open one decision + one blocker escalation;
    leave a pending proposal and a second pending plan."""
    doc = _plan_doc(max_concurrent=2, worker_ids=["w-a", "w-b"])
    _propose_and_approve_plan(coord, doc, tmp_path)

    _register(coord, "w-a")
    claim = coord("claim", "--session", "w-a", "--task", "w-a-task")
    assert claim.returncode == 0, claim.stdout + claim.stderr

    done = coord("claim", "--session", "w-b", "--task", "w-b-task")
    assert done.returncode == 0, done.stdout + done.stderr
    done = coord("complete", "--session", "w-b", "--task", "w-b-task", "--status", "done")
    assert done.returncode == 0, done.stdout + done.stderr

    dec = coord("escalate", "--session", "orch", "--kind", "decision", "--body", "pick a lane")
    assert dec.returncode == 0, dec.stdout + dec.stderr
    blk = coord("escalate", "--session", "orch", "--kind", "blocker", "--body", "stuck on X")
    assert blk.returncode == 0, blk.stdout + blk.stderr

    prop = coord("state", "propose", "--session", "orch", "--key", "note", "--value", '"hi"')
    assert prop.returncode == 0, prop.stdout + prop.stderr

    doc2 = _plan_doc(max_concurrent=1, worker_ids=["w-c"])
    pending_pid = _propose_plan(coord, doc2, tmp_path)

    return pending_pid


# --- §7.8: full aggregate shape + read-only proof ----------------------------------
def test_cockpit_json_aggregate_shape(coord, tmp_path):
    pending_plan_id = _build_scenario(coord, tmp_path)

    before_version = json.loads(coord("state", "show").stdout)["version"]
    before_events = _board_event_count(coord)
    before_escs = _escalation_count(coord)

    r = coord("cockpit", "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    view = json.loads(r.stdout)

    after_version = json.loads(coord("state", "show").stdout)["version"]
    after_events = _board_event_count(coord)
    after_escs = _escalation_count(coord)

    # --- read-only proof: cockpit must not mutate anything ---
    assert after_version == before_version
    assert after_events == before_events
    assert after_escs == before_escs

    # --- tasks: counts + grouped lists ---
    counts = view["tasks"]["counts"]
    assert counts.get("claimed") == 1
    assert counts.get("done") == 1
    by_status = view["tasks"]["by_status"]
    assert by_status["claimed"] == ["w-a-task"]
    assert by_status["done"] == ["w-b-task"]

    # --- workers: liveness + held task ---
    workers = {w["id"]: w for w in view["workers"]}
    assert workers["w-a"]["liveness"] == "fresh"
    assert workers["w-a"]["task"] == "w-a-task"
    assert workers["w-b"]["liveness"] == "stale"
    assert workers["w-b"]["task"] is None  # its task is done, not claimed

    # --- decisions vs blockers: separated ---
    dec_bodies = [e["body"] for e in view["decisions"]]
    blk_bodies = [e["body"] for e in view["blockers"]]
    assert "pick a lane" in dec_bodies
    assert "pick a lane" not in blk_bodies
    assert "stuck on X" in blk_bodies
    assert "stuck on X" not in dec_bodies

    # --- pending: plan + proposal ids ---
    assert pending_plan_id in view["pending"]["plans"]
    assert len(view["pending"]["proposals"]) == 1

    # --- capacity: live/max_concurrent + unconsumed spawn directives ---
    assert view["capacity"]["live"] == 1
    assert view["capacity"]["max_concurrent"] == 2

    # --- desired: version/authorized_phase/fleet ---
    assert view["desired"]["version"] == before_version
    assert set(view["desired"]["fleet"]["workers"]) == {"w-a", "w-b"}


# --- cockpit (no --json) still exits 0 and produces readable text -----------------
def test_cockpit_text_mode_exits_zero(coord, tmp_path):
    _build_scenario(coord, tmp_path)
    r = coord("cockpit")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "desired:" in r.stdout
    assert "tasks:" in r.stdout


# --- empty plane: cockpit must not crash with no fleet/tasks/escalations/plans -----
def test_cockpit_on_empty_plane_does_not_crash(coord):
    r = coord("cockpit", "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    view = json.loads(r.stdout)
    assert view["desired"]["fleet"]["workers"] == []
    assert view["workers"] == []
    assert view["decisions"] == []
    assert view["blockers"] == []
    assert view["pending"]["plans"] == []
    assert view["pending"]["proposals"] == []
    assert view["capacity"]["live"] == 0
