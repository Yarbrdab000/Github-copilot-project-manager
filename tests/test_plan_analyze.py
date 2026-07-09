"""
Work-routing addendum tests — plan acyclicity enforcement + `coord plan analyze`.

Two concerns, mirroring tests/test_cockpit_plan.py's harness (a real subprocess against a
throwaway COORD_ROOT, plus direct module load for pure functions):

  A. Acyclicity: a dependency CYCLE in a proposed plan passes every existing structural
     check (each dep references a real plan task) but then deadlocks forever at claim time
     — no task in the cycle can ever be claimed. `plan propose`/`plan approve` must reject
     it up front.

  B. `coord plan analyze`: a read-only shape analysis (waves, peak parallel width, critical
     path, cross-worker deps, prelude candidates) a navigator runs BEFORE proposing to see
     whether the plan parallelizes and whether the workers are actually isolated.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

COORD_PY = Path(__file__).resolve().parent.parent / "coord" / "coord.py"


def _load_coord_module():
    spec = importlib.util.spec_from_file_location("coord_module_plan_analyze", COORD_PY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


coord = _load_coord_module()


@pytest.fixture
def cli(tmp_path):
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


def _worktree_fleet():
    return {
        "max_concurrent": 3,
        "workers": [
            {"id": "w-api", "owned_paths": ["src/api/**", "tests/api/**"]},
            {"id": "w-ui", "owned_paths": ["src/ui/**", "tests/ui/**"]},
        ],
    }


def _propose(cli, doc, tmp_path, name="plan.json"):
    plan_file = tmp_path / name
    plan_file.write_text(json.dumps(doc), encoding="utf-8")
    return cli("plan", "propose", "--file", str(plan_file))


# --- A. acyclicity enforcement ----------------------------------------------

def _cyclic_plan_doc():
    # t1 -> t2 -> t1: every dep references a real plan task, so it passes the existing
    # reference-integrity check, yet it can never be scheduled.
    return {
        "note": "cyclic",
        "fleet": _worktree_fleet(),
        "tasks": [
            {"id": "t1", "desc": "a", "owned_by": "w-api", "deps": ["t2"], "verify": None},
            {"id": "t2", "desc": "b", "owned_by": "w-api", "deps": ["t1"], "verify": None},
        ],
    }


def test_plan_propose_rejects_dependency_cycle(cli, tmp_path):
    r = _propose(cli, _cyclic_plan_doc(), tmp_path)
    assert r.returncode != 0, r.stdout
    assert "cycle" in r.stderr.lower()
    assert cli("plans").stdout.strip() == "(no pending plans)"


def test_plan_propose_rejects_self_dependency(cli, tmp_path):
    doc = _cyclic_plan_doc()
    doc["tasks"] = [
        {"id": "t1", "desc": "a", "owned_by": "w-api", "deps": ["t1"], "verify": None},
    ]
    r = _propose(cli, doc, tmp_path)
    assert r.returncode != 0, r.stdout
    assert "cycle" in r.stderr.lower()


def test_plan_propose_accepts_valid_dag(cli, tmp_path):
    # A real DAG (t2 depends on t1) must still be accepted — guard against false positives.
    doc = {
        "note": "dag",
        "fleet": _worktree_fleet(),
        "tasks": [
            {"id": "t1", "desc": "a", "owned_by": "w-api", "deps": [], "verify": None},
            {"id": "t2", "desc": "b", "owned_by": "w-api", "deps": ["t1"], "verify": None},
        ],
    }
    r = _propose(cli, doc, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr


# --- toposort helper (pure) -------------------------------------------------

def test_toposort_waves_diamond_layers_correctly():
    ids = ["a", "b", "c", "d"]
    deps = {"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]}
    waves, cyclic = coord._toposort_waves(ids, deps)
    assert waves == [["a"], ["b", "c"], ["d"]]
    assert cyclic == []


def test_toposort_waves_detects_cycle():
    waves, cyclic = coord._toposort_waves(
        ["x", "y", "z"], {"x": ["z"], "y": ["x"], "z": ["y"]}
    )
    assert cyclic == ["x", "y", "z"]


def test_toposort_waves_ignores_deps_outside_the_id_set():
    waves, cyclic = coord._toposort_waves(["a"], {"a": ["not-in-set"]})
    assert waves == [["a"]]
    assert cyclic == []


# --- B. `coord plan analyze` ------------------------------------------------

def _diamond_doc():
    # a -> {b, c} -> d, split across two workers so some deps cross the worker boundary.
    return {
        "note": "diamond",
        "fleet": {
            "max_concurrent": 2,
            "workers": [
                {"id": "w-api", "owned_paths": ["src/api/**"]},
                {"id": "w-ui", "owned_paths": ["src/ui/**"]},
            ],
        },
        "tasks": [
            {"id": "a", "desc": "base", "owned_by": "w-api", "deps": [], "verify": None},
            {"id": "b", "desc": "b", "owned_by": "w-api", "deps": ["a"], "verify": None},
            {"id": "c", "desc": "c", "owned_by": "w-ui", "deps": ["a"], "verify": None},
            {"id": "d", "desc": "d", "owned_by": "w-ui", "deps": ["b", "c"], "verify": None},
        ],
    }


def _analyze_json(cli, doc, tmp_path, name="an.json"):
    f = tmp_path / name
    f.write_text(json.dumps(doc), encoding="utf-8")
    r = cli("plan", "analyze", "--file", str(f), "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(r.stdout)


def test_plan_analyze_reports_waves_and_width(cli, tmp_path):
    a = _analyze_json(cli, _diamond_doc(), tmp_path)
    assert a["waves"] == [["a"], ["b", "c"], ["d"]]
    assert a["peak_parallel_width"] == 2
    assert a["critical_path_length"] == 3
    assert a["critical_path"][0] == "a"
    assert a["critical_path"][-1] == "d"
    assert a["task_count"] == 4


def test_plan_analyze_flags_cross_worker_deps(cli, tmp_path):
    a = _analyze_json(cli, _diamond_doc(), tmp_path)
    edges = {(c["task"], c["dep"]) for c in a["cross_worker_deps"]}
    assert ("c", "a") in edges       # w-ui depends on w-api output
    assert ("d", "b") in edges       # w-ui depends on w-api output
    assert ("b", "a") not in edges   # same worker (w-api) — not cross
    assert a["cross_worker_dep_count"] == 2


def test_plan_analyze_prelude_candidates(cli, tmp_path):
    a = _analyze_json(cli, _diamond_doc(), tmp_path)
    prelude = {p["task"]: p["dependents"] for p in a["prelude_candidates"]}
    assert prelude == {"a": 2}       # a is depended on by both b and c


def test_plan_analyze_human_readable_output(cli, tmp_path):
    f = tmp_path / "an.json"
    f.write_text(json.dumps(_diamond_doc()), encoding="utf-8")
    r = cli("plan", "analyze", "--file", str(f))
    assert r.returncode == 0, r.stderr
    assert "peak_parallel_width=2" in r.stdout
    assert "critical path:" in r.stdout
    assert "cross-worker deps: 2" in r.stdout


def test_plan_analyze_reports_cycle_without_writing(cli, tmp_path):
    f = tmp_path / "cyc.json"
    f.write_text(json.dumps(_cyclic_plan_doc()), encoding="utf-8")
    r = cli("plan", "analyze", "--file", str(f), "--json")
    assert r.returncode == 0, r.stderr   # analyze is read-only and robust to a bad plan
    a = json.loads(r.stdout)
    assert set(a["cyclic_tasks"]) == {"t1", "t2"}
    assert any("cycle" in e.lower() for e in a["errors"])
    # analyze must never write a plan to the ledger
    assert cli("plans").stdout.strip() == "(no pending plans)"


def test_plan_analyze_reads_from_stdin(cli):
    # parity with `plan propose`: the document may arrive on stdin instead of --file.
    env = dict(os.environ)
    env["COORD_ROOT"] = str(cli.root)
    r = subprocess.run(
        [sys.executable, str(COORD_PY), "plan", "analyze", "--json"],
        input=json.dumps(_diamond_doc()),
        capture_output=True,
        text=True,
        env=env,
    )
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["peak_parallel_width"] == 2

