"""
Work-routing addendum tests -- `coord plan seams`, the GENERATIVE half of work routing.

Where `plan analyze` critiques a plan a human already wrote, `plan seams` reads the
repository's own intra-repo import graph and SUGGESTS a partition into worker-owned path
clusters ("seams") that minimizes cross-worker coupling -- so each worker gets a vertical
slice it can build in its own worktree without waiting on another's output.

Same harness shape as tests/test_plan_analyze.py: a real subprocess against a throwaway
COORD_ROOT for the CLI, plus a direct module load for the pure functions. Fixture repos are
written under tmp_path (they are the *target* of analysis, distinct from the coordination
plane -- `plan seams` never reads the plane).
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
    spec = importlib.util.spec_from_file_location("coord_module_plan_seams", COORD_PY)
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


def _write(root: Path, rel: str, text: str = "") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# --- pure: module rollup ----------------------------------------------------

def test_module_graph_rolls_up_and_drops_intramodule():
    files = ["a/x.py", "a/y.py", "b/z.py"]
    edges = [("a/x.py", "a/y.py"), ("a/x.py", "b/z.py"), ("a/y.py", "b/z.py")]
    modules, weights = coord._module_graph(files, edges)
    assert modules == ["a", "b"]
    # a<->a edge is internal and dropped; the two a<->b file edges sum to weight 2
    assert weights == {("a", "b"): 2}


def test_module_graph_root_files_form_dot_module():
    modules, weights = coord._module_graph(["main.py", "pkg/x.py"], [("main.py", "pkg/x.py")])
    assert modules == [".", "pkg"]
    assert weights == {(".", "pkg"): 1}


# --- pure: agglomerative partition ------------------------------------------

def test_agglomerate_natural_components_when_k_is_none():
    # a-b-c are one connected component (a-b weight 3, b-c weight 1); d is isolated.
    clusters = coord._agglomerate(["a", "b", "c", "d"], {("a", "b"): 3, ("b", "c"): 1}, None)
    assert clusters == [["a", "b", "c"], ["d"]]


def test_agglomerate_cuts_the_weakest_edge_first():
    # asked for 3 seams from a 4-module graph: the b-c edge (weight 1) is weaker than
    # a-b (weight 3), so the cut lands there -- a and b stay together.
    clusters = coord._agglomerate(["a", "b", "c", "d"], {("a", "b"): 3, ("b", "c"): 1}, 3)
    assert clusters == [["a", "b"], ["c"], ["d"]]


def test_agglomerate_force_merges_smallest_below_component_count():
    # asking for FEWER seams than independent components forces independents together.
    clusters = coord._agglomerate(["a", "b", "c", "d"], {("a", "b"): 3, ("b", "c"): 1}, 1)
    assert clusters == [["a", "b", "c", "d"]]


def test_agglomerate_clamps_to_module_count():
    # a module is atomic: you cannot get more seams than there are modules.
    assert coord._agglomerate(["a", "b"], {}, 9) == [["a"], ["b"]]


def test_agglomerate_is_deterministic():
    mods = ["a", "b", "c", "d", "e"]
    w = {("a", "b"): 2, ("c", "d"): 2, ("b", "c"): 1}
    assert coord._agglomerate(mods, w, 3) == coord._agglomerate(mods, w, 3)


# --- I/O: import scanning / resolution --------------------------------------

def test_scan_resolves_python_absolute_and_relative_ignores_external(tmp_path):
    _write(tmp_path, "pkg/__init__.py")
    _write(tmp_path, "pkg/core.py", "X = 1\n")
    _write(tmp_path, "pkg/util.py", "from pkg.core import X\nimport os\nfrom . import core\n")
    files, edges = coord._scan_repo_graph(str(tmp_path))
    assert ("pkg/core.py", "pkg/util.py") in edges          # absolute dotted import
    assert ("pkg/__init__.py", "pkg/util.py") in edges       # `from . import core`
    assert len(edges) == 2                                    # `import os` (stdlib) adds nothing


def test_scan_resolves_relative_js_and_ignores_bare(tmp_path):
    _write(tmp_path, "ui/app.jsx", "import W from './widget'\nimport React from 'react'\n")
    _write(tmp_path, "ui/widget.jsx", "export default 1\n")
    _, edges = coord._scan_repo_graph(str(tmp_path))
    assert edges == [("ui/app.jsx", "ui/widget.jsx")]        # './widget' resolves; 'react' ignored


def test_scan_resolves_ts_require_and_dynamic_import(tmp_path):
    _write(tmp_path, "ui/widget.ts", "export const W = 1\n")
    _write(tmp_path, "ui/page.ts", "const w = require('./widget')\nimport('./nope')\n")
    _, edges = coord._scan_repo_graph(str(tmp_path))
    assert edges == [("ui/page.ts", "ui/widget.ts")]         # missing './nope' resolves to nothing


def test_scan_skips_denylisted_dirs(tmp_path):
    _write(tmp_path, "src/a.py", "X = 1\n")
    _write(tmp_path, "node_modules/pkg/index.js", "export default 1\n")
    _write(tmp_path, ".venv/lib/thing.py", "Y = 1\n")
    files, _ = coord._scan_repo_graph(str(tmp_path))
    assert files == ["src/a.py"]


# --- pure: full seam report -------------------------------------------------

def test_plan_seams_reports_cross_cluster_edges(tmp_path):
    _write(tmp_path, "api/models.py", "ID = 1\n")
    _write(tmp_path, "ui/view.py", "from api.models import ID\n")
    files, edges = coord._scan_repo_graph(str(tmp_path))
    r = coord._plan_seams(files, edges, target_k=2)
    assert r["natural_seams"] == 1          # api and ui are coupled -> one component
    assert r["workers"] == 2                # but we asked for 2 seams, so the edge crosses
    assert r["cross_cluster_edge_count"] == 1
    assert r["cross_cluster_edge_weight"] == 1
    e = r["cross_cluster_edges"][0]
    assert {e["a"], e["b"]} == {"api", "ui"}


def test_plan_seams_owned_paths_are_plan_ready_globs(tmp_path):
    _write(tmp_path, "src/api/x.py", "X = 1\n")
    _write(tmp_path, "root.py", "Y = 1\n")
    files, edges = coord._scan_repo_graph(str(tmp_path))
    r = coord._plan_seams(files, edges)
    paths = sorted(p for c in r["clusters"] for p in c["owned_paths"])
    assert "src/api/**" in paths
    assert "*" in paths                     # a root-level file becomes the '*' seam


def test_plan_seams_clamp_emits_a_note(tmp_path):
    _write(tmp_path, "a/f.py", "A = 1\n")
    _write(tmp_path, "b/f.py", "B = 1\n")
    files, edges = coord._scan_repo_graph(str(tmp_path))
    r = coord._plan_seams(files, edges, target_k=9)
    assert r["workers"] == 2
    assert any("only 2 modules" in n for n in r["notes"])


# --- CLI integration --------------------------------------------------------

def _seams_json(cli, root, *extra):
    r = cli("plan", "seams", "--root", str(root), "--json", *extra)
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(r.stdout)


def test_cli_plan_seams_json(cli, tmp_path):
    repo = tmp_path / "repo"
    _write(repo, "api/models.py", "ID = 1\n")
    _write(repo, "api/routes.py", "from api.models import ID\n")   # intra-module (api)
    _write(repo, "ui/view.py", "V = 1\n")
    r = _seams_json(cli, repo)
    assert r["file_count"] == 3
    assert r["module_count"] == 2
    assert r["natural_seams"] == 2                # {api}, {ui} -- no cross-module coupling
    assert r["cross_cluster_edge_weight"] == 0


def test_cli_plan_seams_workers_forces_k(cli, tmp_path):
    repo = tmp_path / "repo"
    _write(repo, "a/f.py", "A = 1\n")
    _write(repo, "b/f.py", "B = 1\n")
    _write(repo, "c/f.py", "C = 1\n")
    r = _seams_json(cli, repo, "--workers", "2")
    assert r["workers"] == 2
    assert len(r["clusters"]) == 2


def test_cli_plan_seams_human_output(cli, tmp_path):
    repo = tmp_path / "repo"
    _write(repo, "api/m.py", "ID = 1\n")
    _write(repo, "ui/v.py", "from api.m import ID\n")
    r = cli("plan", "seams", "--root", str(repo), "--workers", "2")
    assert r.returncode == 0, r.stderr
    assert "natural_seams" in r.stdout
    assert "cross-seam coupling:" in r.stdout
    assert "seam-1" in r.stdout


def test_cli_plan_seams_is_read_only(cli, tmp_path):
    repo = tmp_path / "repo"
    _write(repo, "a/f.py", "A = 1\n")
    cli("plan", "seams", "--root", str(repo))
    # seams must never write a plan to the ledger (it is a read-only suggester)
    assert cli("plans").stdout.strip() == "(no pending plans)"


def test_plan_seams_runs_without_an_initialized_plane(tmp_path):
    # seams reads only --root, never the coordination plane, so it works with no `init`.
    repo = tmp_path / "repo"
    _write(repo, "a/f.py", "A = 1\n")
    env = dict(os.environ)
    env["COORD_ROOT"] = str(tmp_path / "never-initialized")
    r = subprocess.run(
        [sys.executable, str(COORD_PY), "plan", "seams", "--root", str(repo), "--json"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["file_count"] == 1


def test_cli_plan_seams_is_deterministic(cli, tmp_path):
    repo = tmp_path / "repo"
    for i in range(4):
        _write(repo, f"m{i}/f.py", "X = 1\n")
    a = _seams_json(cli, repo, "--workers", "2")
    b = _seams_json(cli, repo, "--workers", "2")
    assert a == b
