"""
Work-routing addendum tests -- CONTRACT-AWARE `coord plan scaffold`.

`plan scaffold` already turns a repo's seam partition into a valid plan document. When the
partition is naturally decoupled (or under-split by asking for FEWER workers than natural
seams) there is no coupling between seams, so tasks stay dep-free and maximally parallel.

The sharp case these tests pin down is the FORCED cut: asking for MORE workers than there
are naturally-decoupled seams (`--workers N`) splits a coupled component along its weakest
edge. That edge is a real shared interface between two workers. Contract-aware scaffold
turns it into an explicit contracts-first wave-0: one UNOWNED "contract" prelude task per
coupled seam pair, with both coupled seams' impl tasks depending on it. Multiple module
edges crossing the same seam pair collapse to a single contract.

The load-bearing guarantees under test: (1) a natural/under-split partition is unchanged
(no contracts, empty deps); (2) a forced cut surfaces one contract per coupled seam pair,
unowned, with both seams waiting on it; (3) the resulting doc still passes `_plan_validate`
and round-trips `scaffold | analyze | propose` -- and analyze sees the contracts as
prelude_candidates with ZERO cross_worker_deps (an unowned boundary object is not a
worker-to-worker leak).

Same harness shape as tests/test_plan_scaffold.py: a real subprocess for the CLI, a direct
module load for the pure functions, fixture repos under tmp_path.
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
    spec = importlib.util.spec_from_file_location("coord_module_plan_contracts", COORD_PY)
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


def _contracts(doc: dict) -> list:
    return [t for t in doc["tasks"] if t["id"].startswith("contract-")]


def _impl(doc: dict, seam: str) -> dict:
    return next(t for t in doc["tasks"] if t["id"] == f"{seam}-impl")


def _write(root: Path, rel: str, text: str = "") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _coupled_chain_repo(root: Path) -> None:
    """a -> b -> c: a coupled 3-module chain (natural = ONE component). Forcing --workers
    3 splits it and leaves two cross-seam edges (a<->b, b<->c)."""
    _write(root, "a/x.py", "import b.s\n")
    _write(root, "a/y.py", "import b.s\n")
    _write(root, "b/s.py", "import c.z\n")
    _write(root, "c/z.py", "value = 1\n")


def _decl(root: Path, spec: dict) -> str:
    p = root / "graph.json"
    p.write_text(json.dumps(spec), encoding="utf-8")
    return str(p)


# --- pure: no forced cut -> no contracts (behavior unchanged) ----------------

def test_natural_partition_emits_no_contracts():
    # Two independent coupled packages -> 2 natural seams, no edges between them.
    files = ["alpha/core.py", "alpha/util.py", "beta/service.py", "beta/model.py"]
    edges = [("alpha/core.py", "alpha/util.py"), ("beta/service.py", "beta/model.py")]
    doc = coord._plan_scaffold(files, edges)
    assert _contracts(doc) == []
    assert all(t["deps"] == [] for t in doc["tasks"])


def test_under_split_force_merge_emits_no_contracts():
    # Three INDEPENDENT modules forced to 2 workers -> merges the smallest (they share no
    # edges), so no cross-seam coupling is created and no contract is emitted.
    files = ["a/x.py", "b/y.py", "c/z.py"]
    edges = []
    doc = coord._plan_scaffold(files, edges, target_k=2)
    assert len(doc["fleet"]["workers"]) == 2
    assert _contracts(doc) == []
    assert all(t["deps"] == [] for t in doc["tasks"])


# --- pure: a forced cut surfaces the coupling as unowned contracts -----------

def test_forced_cut_emits_one_contract_per_coupled_seam_pair():
    files = ["a/x.py", "b/y.py"]
    edges = [("a/x.py", "b/y.py")]           # one coupled component; natural = 1 seam
    natural = coord._plan_scaffold(files, edges)
    assert len(natural["fleet"]["workers"]) == 1 and _contracts(natural) == []
    doc = coord._plan_scaffold(files, edges, target_k=2)   # force the split
    assert len(doc["fleet"]["workers"]) == 2
    assert len(_contracts(doc)) == 1


def test_contract_prelude_is_unowned():
    files = ["a/x.py", "b/y.py"]
    edges = [("a/x.py", "b/y.py")]
    doc = coord._plan_scaffold(files, edges, target_k=2)
    (contract,) = _contracts(doc)
    assert contract["owned_by"] is None


def test_contract_prelude_has_verify_key_present():
    files = ["a/x.py", "b/y.py"]
    edges = [("a/x.py", "b/y.py")]
    doc = coord._plan_scaffold(files, edges, target_k=2)
    (contract,) = _contracts(doc)
    assert "verify" in contract and contract["verify"] is None


def test_both_coupled_seams_depend_on_the_contract():
    files = ["a/x.py", "b/y.py"]
    edges = [("a/x.py", "b/y.py")]
    doc = coord._plan_scaffold(files, edges, target_k=2)
    (contract,) = _contracts(doc)
    cid = contract["id"]
    assert cid in _impl(doc, "seam-1")["deps"]
    assert cid in _impl(doc, "seam-2")["deps"]


def test_multiple_crossing_edges_collapse_to_one_contract():
    # Two distinct file edges both cross the a<->b seam boundary -> ONE shared contract,
    # its weight the sum of the crossing edges.
    files = ["a/x.py", "a/y.py", "b/s.py", "b/t.py"]
    edges = [("a/x.py", "b/s.py"), ("a/y.py", "b/t.py")]
    doc = coord._plan_scaffold(files, edges, target_k=2)
    contracts = _contracts(doc)
    assert len(contracts) == 1
    assert "weight 2" in contracts[0]["desc"]


def test_three_way_split_emits_pairwise_contracts():
    # A triangle a-b-c (each pair coupled) forced into 3 seams -> 3 pairwise contracts.
    files = ["a/x.py", "b/y.py", "c/z.py"]
    edges = [("a/x.py", "b/y.py"), ("b/y.py", "c/z.py"), ("a/x.py", "c/z.py")]
    doc = coord._plan_scaffold(files, edges, target_k=3)
    assert len(doc["fleet"]["workers"]) == 3
    assert len(_contracts(doc)) == 3
    # every impl waits on exactly the two contracts touching its seam
    for seam in ("seam-1", "seam-2", "seam-3"):
        assert len(_impl(doc, seam)["deps"]) == 2


def test_hub_split_hub_impl_waits_on_every_spoke_contract():
    # Hub h coupled to three spokes; a/b/c share no edges with each other. Forcing 4 seams
    # gives 3 contracts (h<->a, h<->b, h<->c). The hub's impl waits on all three; each
    # spoke waits on exactly one.
    files = ["h/m.py", "a/x.py", "b/y.py", "c/z.py"]
    edges = [("h/m.py", "a/x.py"), ("h/m.py", "b/y.py"), ("h/m.py", "c/z.py")]
    doc = coord._plan_scaffold(files, edges, target_k=4)
    assert len(_contracts(doc)) == 3
    owner_of_hub = next(w["id"] for w in doc["fleet"]["workers"]
                        if w["owned_paths"] == ["h/**"])
    hub_deps = _impl(doc, owner_of_hub)["deps"]
    assert len(hub_deps) == 3
    spoke_dep_counts = sorted(
        len(_impl(doc, w["id"])["deps"]) for w in doc["fleet"]["workers"]
        if w["owned_paths"] != ["h/**"]
    )
    assert spoke_dep_counts == [1, 1, 1]


def test_scaffold_with_contracts_passes_plan_validate():
    files = ["a/x.py", "b/y.py", "c/z.py"]
    edges = [("a/x.py", "b/y.py"), ("b/y.py", "c/z.py")]
    doc = coord._plan_scaffold(files, edges, target_k=3)
    assert _contracts(doc)                       # contracts were emitted
    assert coord._plan_validate(doc, {}) == []   # and the doc is still valid


def test_contract_ids_do_not_collide_with_impl_ids():
    files = ["a/x.py", "b/y.py", "c/z.py"]
    edges = [("a/x.py", "b/y.py"), ("b/y.py", "c/z.py")]
    doc = coord._plan_scaffold(files, edges, target_k=3)
    ids = [t["id"] for t in doc["tasks"]]
    assert len(ids) == len(set(ids))


def test_contracts_are_deterministic():
    files = ["a/x.py", "b/y.py", "c/z.py"]
    edges = [("a/x.py", "b/y.py"), ("b/y.py", "c/z.py")]
    a = coord._plan_scaffold(files, edges, target_k=3)
    b = coord._plan_scaffold(files, edges, target_k=3)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_contract_desc_names_both_seams_and_says_assign_owner():
    files = ["a/x.py", "b/y.py"]
    edges = [("a/x.py", "b/y.py")]
    doc = coord._plan_scaffold(files, edges, target_k=2)
    (contract,) = _contracts(doc)
    assert "seam-1" in contract["desc"] and "seam-2" in contract["desc"]
    assert "owned_by" in contract["desc"]


# --- CLI: forced cut over a scanned repo ------------------------------------

def test_cli_forced_cut_emits_contracts(cli, tmp_path):
    repo = tmp_path / "repo"
    _coupled_chain_repo(repo)
    r = cli("plan", "scaffold", "--root", str(repo), "--workers", "3")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert len(doc["fleet"]["workers"]) == 3
    assert len(_contracts(doc)) == 2               # a<->b and b<->c
    assert coord._plan_validate(doc, {}) == []


def test_cli_scaffold_contracts_pipe_into_analyze(cli, tmp_path):
    repo = tmp_path / "repo"
    _coupled_chain_repo(repo)
    scaffolded = cli("plan", "scaffold", "--root", str(repo), "--workers", "3")
    assert scaffolded.returncode == 0, scaffolded.stderr
    analyzed = cli("plan", "analyze", "--json", stdin=scaffolded.stdout)
    assert analyzed.returncode == 0, analyzed.stderr
    report = json.loads(analyzed.stdout)
    assert report["errors"] == []                         # a valid plan
    assert report["cross_worker_deps"] == []              # unowned contract != worker leak
    prelude_ids = {p["task"] for p in report["prelude_candidates"]}
    assert prelude_ids == {"contract-seam-1-seam-2", "contract-seam-2-seam-3"}
    assert report["waves"][0] == ["contract-seam-1-seam-2", "contract-seam-2-seam-3"]


def test_cli_scaffold_contracts_pipe_into_propose_accepted(cli, tmp_path):
    repo = tmp_path / "repo"
    _coupled_chain_repo(repo)
    scaffolded = cli("plan", "scaffold", "--root", str(repo), "--workers", "3")
    assert scaffolded.returncode == 0, scaffolded.stderr
    proposed = cli("plan", "propose", stdin=scaffolded.stdout)
    assert proposed.returncode == 0, proposed.stderr


# --- CLI: forced cut over a greenfield DECLARED graph (closes the loop) ------

def test_cli_graph_forced_cut_emits_contracts(cli, tmp_path):
    # A declared hub: store is shared by api, expiry, web. Natural = ONE coupled seam;
    # forcing --workers 4 splits it and surfaces the store contracts automatically.
    path = _decl(tmp_path, {
        "modules": ["src/api", "src/store", "src/expiry", "src/web"],
        "edges": [["src/api", "src/store"], ["src/expiry", "src/store"], ["src/web", "src/api"]],
    })
    r = cli("plan", "scaffold", "--graph", path, "--workers", "4")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert len(doc["fleet"]["workers"]) == 4
    assert len(_contracts(doc)) == 3               # api<->store, expiry<->store, web<->api
    assert coord._plan_validate(doc, {}) == []
