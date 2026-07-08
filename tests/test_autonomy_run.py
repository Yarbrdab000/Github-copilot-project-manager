"""
Autonomy-addendum acceptance tests for `coord/coord.py` (AUTONOMY_SPEC §3.4, §6, §7.8-§7.9).

Covers Phase 4:
  - `coord run` — the thin loop wrapper around `tick`. All reconciliation logic lives
    in tick; `run` only sleeps, loops, and counts passes (bounded by `--max-ticks` /
    `--once`, or halted early by a fleet-wide STOP flag).
  - the `continue` boolean `coord checkpoint` now reports: true when the calling
    session holds an unfinished (`claimed`, not `done`) task.

Like the other autonomy test files, each test drives the real CLI as a subprocess
against a throwaway COORD_ROOT (pytest `tmp_path`), never the repo's own
`.coordination/`. The interpreter is invoked via `sys.executable` (this box has no
`python3` alias).
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


def _parse_json_objects(text: str) -> list[dict]:
    """`run` prints one `json.dumps(..., indent=2)` blob per tick pass, back-to-back
    with no separator. Peel them off one at a time with a streaming decoder."""
    decoder = json.JSONDecoder()
    objs = []
    idx = 0
    text = text.strip()
    while idx < len(text):
        obj, end = decoder.raw_decode(text, idx)
        objs.append(obj)
        idx = end
        while idx < len(text) and text[idx] in " \t\r\n":
            idx += 1
    return objs


# --- §7.8 `run --max-ticks N` performs exactly N tick passes ----------------
def test_run_max_ticks_performs_exactly_n_passes(coord):
    r = coord("run", "--interval", "0", "--max-ticks", "3")
    assert r.returncode == 0, r.stderr
    reports = _parse_json_objects(r.stdout)
    assert len(reports) == 3
    for rep in reports:
        assert set(rep) == {"reaped", "verified", "requeued", "spawned", "dispatched", "nudged", "failed", "awaiting_decision"}


# --- §7.8 `run --once` is exactly one pass -----------------------------------
def test_run_once_performs_exactly_one_pass(coord):
    r = coord("run", "--once", "--interval", "0")
    assert r.returncode == 0, r.stderr
    reports = _parse_json_objects(r.stdout)
    assert len(reports) == 1


# --- `run` halts early (before starting another pass) once a global STOP is set --
def test_run_stops_early_on_global_stop_flag(coord):
    stop = coord("stop")  # no --session => writes the GLOBAL flag
    assert stop.returncode == 0, stop.stderr

    r = coord("run", "--interval", "0", "--max-ticks", "5")
    assert r.returncode == 0, r.stderr
    reports = _parse_json_objects(r.stdout)
    assert len(reports) == 0


# --- §7.9 checkpoint continue:true while an unfinished claimed task is held --
def test_checkpoint_continue_true_while_task_claimed(coord):
    _register(coord, "w1")
    coord("add-task", "--id", "t1", "--desc", "d")
    coord("claim", "--session", "w1", "--task", "t1")

    cp = coord("checkpoint", "--session", "w1")
    assert cp.returncode == 0, cp.stderr
    out = json.loads(cp.stdout)
    assert out["continue"] is True


# --- §7.9 checkpoint continue:false once the claimed task is completed -----
def test_checkpoint_continue_false_after_task_done(coord):
    _register(coord, "w1")
    coord("add-task", "--id", "t1", "--desc", "d")
    coord("claim", "--session", "w1", "--task", "t1")
    coord("complete", "--session", "w1", "--task", "t1")

    cp = coord("checkpoint", "--session", "w1")
    assert cp.returncode == 0, cp.stderr
    out = json.loads(cp.stdout)
    assert out["continue"] is False


# --- §7.9 checkpoint continue:false with no claimed task at all ------------
def test_checkpoint_continue_false_with_no_claimed_task(coord):
    _register(coord, "w1")
    cp = coord("checkpoint", "--session", "w1")
    assert cp.returncode == 0, cp.stderr
    out = json.loads(cp.stdout)
    assert out["continue"] is False


# --- frontmatter of the edited skill/agent docs still parses ---------------
def _parse_frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} has no frontmatter delimiter"
    end = text.index("\n---", 4)
    fm_lines = text[4:end].splitlines()
    fm = {}
    for line in fm_lines:
        if not line.strip() or line.strip().startswith("#"):
            continue
        assert ":" in line, f"{path} frontmatter line not key:value -> {line!r}"
        k, _, v = line.partition(":")
        fm[k.strip()] = v.strip()
    return fm


def test_worker_skill_frontmatter_still_parses():
    path = Path(__file__).resolve().parent.parent / "skills" / "worker" / "SKILL.md"
    fm = _parse_frontmatter(path)
    assert fm  # non-empty: real frontmatter keys survived the edit


def test_editor_agent_frontmatter_still_parses():
    path = Path(__file__).resolve().parent.parent / "agents" / "editor.agent.md"
    fm = _parse_frontmatter(path)
    assert fm
