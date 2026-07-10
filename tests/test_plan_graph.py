"""
Work-routing addendum tests -- `coord plan seams`/`plan scaffold --graph`, the GREENFIELD
front of work routing.

`seams` and `scaffold` normally read coupling from code that already exists (`--root`). But a
brand-new project has nothing to scan. So the navigator reasons the prose goal into an
*intended* module graph -- the components it will build and the dependencies between them --
and declares it as JSON; `--graph` feeds that declaration through the SAME isolation engine
(`_module_graph` -> `_agglomerate` -> nested-merge -> owned-path globs -> `_plan_validate`).
The language understanding lives in the navigator; the deterministic partition lives in coord.

The load-bearing guarantee: a declared graph produces the same well-formed, isolation-safe
partition a scanned repo does -- `plan scaffold --graph g.json | plan analyze` round-trips with
zero errors, and nested declared modules (`src` + `src/api`) never emit overlapping owned_paths.

Same harness shape as tests/test_plan_scaffold.py.
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
    spec = importlib.util.spec_from_file_location("coord_module_plan_graph", COORD_PY)
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


def _decl(root: Path, spec: dict) -> str:
    p = root / "graph.json"
    p.write_text(json.dumps(spec), encoding="utf-8")
    return str(p)


# --- pure: declared-module normalization ------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("src/api", "src/api"),
    ("./src/api/", "src/api"),
    ("src//api", "src/api"),
    ("src\\api", "src/api"),
    ("  src/api  ", "src/api"),
    (".", "."),
    ("", "."),
])
def test_normalize_decl_module(raw, expected):
    assert coord._normalize_decl_module(raw) == expected


@pytest.mark.parametrize("bad", ["/abs/path", "..", "../escape", "/"])
def test_normalize_decl_module_rejects_escapes(bad):
    with pytest.raises(SystemExit):
        coord._normalize_decl_module(bad)


# --- pure: declared graph -> synthetic (files, edges) rolls up as declared --

def test_load_declared_graph_rolls_up_to_declared_modules_and_weights():
    files, edges = coord._load_declared_graph(
        {"modules": ["a", "b", "c"], "edges": [["a", "b"], ["b", "c"]]})
    modules, weights = coord._module_graph(files, edges)
    assert modules == ["a", "b", "c"]
    assert weights == {("a", "b"): 1, ("b", "c"): 1}


def test_load_declared_graph_edge_weight_is_honored():
    files, edges = coord._load_declared_graph(
        {"modules": ["a", "b"], "edges": [["a", "b", 4]]})
    _, weights = coord._module_graph(files, edges)
    assert weights == {("a", "b"): 4}


def test_load_declared_graph_dedups_modules():
    files, edges = coord._load_declared_graph({"modules": ["a", "a", "b"]})
    modules, _ = coord._module_graph(files, edges)
    assert modules == ["a", "b"]


def test_load_declared_graph_drops_self_edges():
    files, edges = coord._load_declared_graph(
        {"modules": ["a", "b"], "edges": [["a", "a"], ["a", "b"]]})
    _, weights = coord._module_graph(files, edges)
    assert weights == {("a", "b"): 1}  # a<->a dropped


@pytest.mark.parametrize("spec", [
    [],                                             # not a dict
    {"modules": "src"},                             # modules not a list
    {"modules": []},                                # empty modules
    {"modules": [""]},                              # blank module
    {"modules": [123]},                             # non-string module
    {"modules": ["a"], "edges": [["a", "ghost"]]},  # edge to undeclared module
    {"modules": ["a", "b"], "edges": [["a"]]},      # short edge
    {"modules": ["a", "b"], "edges": "nope"},       # edges not a list
    {"modules": ["a", "b"], "edges": [["a", "b", 0]]},      # weight < 1
    {"modules": ["a", "b"], "edges": [["a", "b", "x"]]},    # non-int weight
    {"modules": ["a", "b"], "edges": [["a", "b", True]]},   # bool weight
])
def test_load_declared_graph_rejects_malformed(spec):
    with pytest.raises(SystemExit):
        coord._load_declared_graph(spec)


# --- pure: scaffold from a declared graph is a VALID plan -------------------

def test_scaffold_from_declared_graph_passes_validate():
    files, edges = coord._load_declared_graph({
        "modules": ["src/auth", "src/api", "src/ui", "src/billing"],
        "edges": [["src/api", "src/auth"], ["src/ui", "src/api"]],
    })
    doc = coord._plan_scaffold(files, edges)
    assert coord._plan_validate(doc, {}) == []
    # billing shares no edges -> its own seam (free parallelism); auth/api/ui chain merges.
    assert len(doc["fleet"]["workers"]) == 2


def test_scaffold_from_declared_nested_modules_do_not_overlap():
    files, edges = coord._load_declared_graph({"modules": ["src", "src/api"], "edges": []})
    doc = coord._plan_scaffold(files, edges)
    assert coord._plan_validate(doc, {}) == []
    assert len(doc["fleet"]["workers"]) == 1
    assert doc["fleet"]["workers"][0]["owned_paths"] == ["src/**"]


# --- CLI: --graph from a file and from stdin --------------------------------

def test_cli_scaffold_graph_file_emits_valid_plan(cli, tmp_path):
    path = _decl(tmp_path, {
        "modules": ["src/auth", "src/api", "src/billing"],
        "edges": [["src/api", "src/auth"]],
    })
    r = cli("plan", "scaffold", "--graph", path)
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert coord._plan_validate(doc, {}) == []
    owned = sorted(p for w in doc["fleet"]["workers"] for p in w["owned_paths"])
    assert owned == ["src/api/**", "src/auth/**", "src/billing/**"]


def test_cli_scaffold_graph_stdin(cli):
    spec = '{"modules":["a","b"],"edges":[]}'
    r = cli("plan", "scaffold", "--graph", "-", stdin=spec)
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert {w["id"] for w in doc["fleet"]["workers"]} == {"seam-1", "seam-2"}


def test_cli_seams_graph_reports_declared_source(cli, tmp_path):
    path = _decl(tmp_path, {"modules": ["a", "b"], "edges": [["a", "b"]]})
    r = cli("plan", "seams", "--graph", path)
    assert r.returncode == 0, r.stderr
    assert "source=declared graph" in r.stdout
    assert "modules=2" in r.stdout


def test_cli_scaffold_graph_pipes_into_analyze_zero_errors(cli, tmp_path):
    path = _decl(tmp_path, {
        "modules": ["svc/orders", "svc/payments", "svc/notify"],
        "edges": [["svc/payments", "svc/orders"]],
    })
    scaffolded = cli("plan", "scaffold", "--graph", path)
    assert scaffolded.returncode == 0, scaffolded.stderr
    analyzed = cli("plan", "analyze", "--json", stdin=scaffolded.stdout)
    assert analyzed.returncode == 0, analyzed.stderr
    report = json.loads(analyzed.stdout)
    assert report["errors"] == []
    assert report["cross_worker_deps"] == []
    assert len(report["waves"]) == 1  # notify + (orders/payments) all parallel to start


def test_cli_scaffold_graph_workers_reduces_seams(cli, tmp_path):
    # three independent components -> 3 seams by default, forced to 2 with --workers.
    path = _decl(tmp_path, {"modules": ["a", "b", "c"], "edges": []})
    assert len(json.loads(cli("plan", "scaffold", "--graph", path).stdout)["fleet"]["workers"]) == 3
    r2 = cli("plan", "scaffold", "--graph", path, "--workers", "2")
    assert len(json.loads(r2.stdout)["fleet"]["workers"]) == 2


def test_cli_scaffold_graph_is_read_only(tmp_path):
    # no init, non-existent plane -- declared scaffold must not create it.
    path = _decl(tmp_path, {"modules": ["a", "b"], "edges": []})
    plane = tmp_path / "empty_plane"
    env = dict(os.environ)
    env["COORD_ROOT"] = str(plane)
    r = subprocess.run(
        [sys.executable, str(COORD_PY), "plan", "scaffold", "--graph", path],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["fleet"]["workers"]
    assert not plane.exists()


def test_cli_graph_bad_json_errors(cli):
    r = cli("plan", "scaffold", "--graph", "-", stdin="{not json")
    assert r.returncode == 1
    assert "not valid JSON" in r.stderr


def test_cli_graph_unknown_edge_endpoint_errors(cli, tmp_path):
    path = _decl(tmp_path, {"modules": ["a"], "edges": [["a", "ghost"]]})
    r = cli("plan", "scaffold", "--graph", path)
    assert r.returncode == 1
    assert "undeclared module" in r.stderr
