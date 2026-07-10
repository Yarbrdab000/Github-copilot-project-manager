"""
Work-routing addendum tests -- `coord plan scaffold`, the bridge from `plan seams` to
`plan propose`.

`plan seams` tells you WHERE a repo's isolated boundaries are; `plan scaffold` turns that
partition into a VALID, ready-to-edit plan document: a fleet wired straight from the seams
and one placeholder task per seam with empty deps (maximally parallel, zero coupling to
start). The navigator then fills in task descriptions and the contracts-first deps, runs
`plan analyze`, and proposes.

The load-bearing guarantee under test: a scaffolded doc always passes `_plan_validate`, so
`coord plan scaffold --root . | coord plan analyze` (and, after editing, `| coord plan
propose`) round-trips cleanly. The sharp edge is nested modules -- an uncoupled `src/core.py`
(module `src`) and `src/api/routes.py` (module `src/api`) must never land in DIFFERENT workers
(overlapping owned_paths is an illegal plan); `_merge_nested_clusters` unions them.

Same harness shape as tests/test_plan_seams.py: a real subprocess for the CLI, a direct module
load for the pure functions, fixture repos under tmp_path.
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
    spec = importlib.util.spec_from_file_location("coord_module_plan_scaffold", COORD_PY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


coord = _load_coord_module()


@pytest.fixture
def cli(tmp_path):
    """Return a `run(*args, stdin=...)` callable bound to an initialized, isolated plane."""
    root = tmp_path / "coordination"

    def run(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["COORD_ROOT"] = str(root)
        return subprocess.run(
            [sys.executable, str(COORD_PY), *args],
            capture_output=True,
            text=True,
            input=stdin,
            env=env,
        )

    run.root = root.resolve()
    init = run("init")
    assert init.returncode == 0, init.stderr
    return run


def _write(root: Path, rel: str, text: str = "") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _two_package_repo(root: Path) -> None:
    """Two top-level packages with intra-package imports but no cross imports -> two
    disjoint components -> two seams."""
    _write(root, "alpha/__init__.py", "")
    _write(root, "alpha/core.py", "from alpha import util\n")
    _write(root, "alpha/util.py", "VALUE = 1\n")
    _write(root, "beta/__init__.py", "")
    _write(root, "beta/service.py", "from beta import model\n")
    _write(root, "beta/model.py", "NAME = 'b'\n")


# --- pure: nested-cluster merge (the sharp edge) ----------------------------

def test_merge_nested_clusters_unions_parent_and_child():
    # 'src' and 'src/api' would yield owned_paths src/** and src/api/** -> illegal overlap.
    assert coord._merge_nested_clusters([["src"], ["src/api"]]) == [["src", "src/api"]]


def test_merge_nested_clusters_keeps_siblings_separate():
    assert coord._merge_nested_clusters([["a"], ["b"]]) == [["a"], ["b"]]


def test_merge_nested_clusters_root_never_merges():
    # root module '.' -> owned_path '*', which overlaps nothing but another '*'.
    assert coord._merge_nested_clusters([["."], ["src/api"]]) == [["."], ["src/api"]]


def test_merge_nested_clusters_is_transitive():
    # src, src/api, src/api/v2 collapse to a single worker under union-find.
    merged = coord._merge_nested_clusters([["src"], ["src/api"], ["src/api/v2"]])
    assert merged == [["src", "src/api", "src/api/v2"]]


# --- pure: intra-worker owned-path collapse ---------------------------------

def test_collapse_owned_paths_drops_covered_descendants_and_dedups():
    got = coord._collapse_owned_paths(["src/api/**", "src/api/v2/**", "src/api/**"])
    assert got == ["src/api/**"]


def test_collapse_owned_paths_keeps_disjoint_siblings():
    assert coord._collapse_owned_paths(["a/**", "b/**"]) == ["a/**", "b/**"]


def test_collapse_owned_paths_star_coexists_with_dirs():
    # root ownership '*' does not swallow a subtree and vice versa.
    assert coord._collapse_owned_paths(["*", "src/**"]) == ["*", "src/**"]


# --- pure: scaffold produces a VALID plan document --------------------------

def test_scaffold_doc_passes_plan_validate():
    files = ["alpha/core.py", "alpha/util.py", "beta/service.py", "beta/model.py"]
    edges = [("alpha/core.py", "alpha/util.py"), ("beta/service.py", "beta/model.py")]
    doc = coord._plan_scaffold(files, edges)
    assert coord._plan_validate(doc, {}) == []


def test_scaffold_nested_uncoupled_modules_do_not_overlap():
    # src/ and src/api/ have direct files but NO import between them -> separate components.
    # Without the nested-merge this would emit src/** and src/api/** in different workers.
    files = ["src/__init__.py", "src/core.py", "src/api/__init__.py", "src/api/routes.py"]
    edges = []  # zero coupling
    doc = coord._plan_scaffold(files, edges)
    assert coord._plan_validate(doc, {}) == []
    # merged into exactly one worker owning the collapsed ancestor
    assert len(doc["fleet"]["workers"]) == 1
    assert doc["fleet"]["workers"][0]["owned_paths"] == ["src/**"]


def test_scaffold_one_task_per_seam_owner_wired_verify_present():
    files = ["alpha/core.py", "alpha/util.py", "beta/service.py", "beta/model.py"]
    edges = [("alpha/core.py", "alpha/util.py"), ("beta/service.py", "beta/model.py")]
    doc = coord._plan_scaffold(files, edges)
    worker_ids = {w["id"] for w in doc["fleet"]["workers"]}
    assert len(doc["tasks"]) == len(worker_ids)
    for t in doc["tasks"]:
        assert t["owned_by"] in worker_ids          # owner references a declared worker
        assert t["owned_by"] + "-impl" == t["id"]   # task id derived from its seam
        assert t["deps"] == []                       # zero coupling to start
        assert "verify" in t and t["verify"] is None  # verify key present (opt-out via null)


def test_scaffold_max_concurrent_defaults_to_worker_count():
    files = ["alpha/core.py", "beta/service.py"]
    edges = []
    doc = coord._plan_scaffold(files, edges)
    assert doc["fleet"]["max_concurrent"] == len(doc["fleet"]["workers"])


def test_scaffold_max_concurrent_override_is_honored():
    files = ["alpha/core.py", "beta/service.py"]
    edges = []
    doc = coord._plan_scaffold(files, edges, max_concurrent=1)
    assert doc["fleet"]["max_concurrent"] == 1
    assert coord._plan_validate(doc, {}) == []


def test_scaffold_workers_target_reduces_seam_count():
    # Three uncoupled modules -> 3 seams by default; target_k=2 forces a merge to 2.
    files = ["a/x.py", "b/y.py", "c/z.py"]
    edges = []
    assert len(coord._plan_scaffold(files, edges)["fleet"]["workers"]) == 3
    assert len(coord._plan_scaffold(files, edges, target_k=2)["fleet"]["workers"]) == 2


def test_scaffold_is_deterministic():
    files = ["alpha/core.py", "alpha/util.py", "beta/service.py", "beta/model.py"]
    edges = [("alpha/core.py", "alpha/util.py"), ("beta/service.py", "beta/model.py")]
    a = coord._plan_scaffold(files, edges)
    b = coord._plan_scaffold(files, edges)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# --- CLI: scaffold emits a plan doc, read-only ------------------------------

def test_cli_scaffold_emits_valid_plan_json(cli, tmp_path):
    repo = tmp_path / "repo"
    _two_package_repo(repo)
    r = cli("plan", "scaffold", "--root", str(repo))
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert set(doc) == {"note", "fleet", "tasks"}
    assert {w["id"] for w in doc["fleet"]["workers"]} == {"seam-1", "seam-2"}
    assert coord._plan_validate(doc, {}) == []


def test_cli_scaffold_is_read_only(tmp_path):
    # scaffold needs NO init and must not write to the plane -- run against a bare,
    # non-initialized COORD_ROOT and assert nothing is created there.
    repo = tmp_path / "repo"
    _two_package_repo(repo)
    plane = tmp_path / "empty_plane"
    env = dict(os.environ)
    env["COORD_ROOT"] = str(plane)
    r = subprocess.run(
        [sys.executable, str(COORD_PY), "plan", "scaffold", "--root", str(repo)],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["fleet"]["workers"]  # produced output
    assert not plane.exists()  # wrote nothing to the coordination plane


# --- CLI: the round-trips (the whole point) ---------------------------------

def test_cli_scaffold_pipes_into_analyze_with_zero_errors(cli, tmp_path):
    repo = tmp_path / "repo"
    _two_package_repo(repo)
    scaffolded = cli("plan", "scaffold", "--root", str(repo))
    assert scaffolded.returncode == 0, scaffolded.stderr
    analyzed = cli("plan", "analyze", "--json", stdin=scaffolded.stdout)
    assert analyzed.returncode == 0, analyzed.stderr
    report = json.loads(analyzed.stdout)
    assert report["errors"] == []             # would-be propose errors: none
    assert report["cross_worker_deps"] == []  # zero coupling by construction
    assert len(report["waves"]) == 1          # maximally parallel (one wave)


def test_cli_scaffold_pipes_into_propose_accepted(cli, tmp_path):
    repo = tmp_path / "repo"
    _two_package_repo(repo)
    scaffolded = cli("plan", "scaffold", "--root", str(repo))
    assert scaffolded.returncode == 0, scaffolded.stderr
    proposed = cli("plan", "propose", stdin=scaffolded.stdout)
    assert proposed.returncode == 0, proposed.stderr
    assert "pending" in proposed.stdout
    # a plan is now on the pending ledger
    plans = cli("plans")
    assert plans.returncode == 0, plans.stderr
    assert "tasks=2" in plans.stdout
