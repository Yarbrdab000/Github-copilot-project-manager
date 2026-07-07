"""
Autonomy-addendum acceptance tests for `coord/coord.py` (AUTONOMY_SPEC §2, §3.2, §7.5).

Covers Phase 2 — the escalation channel (the human/navigator interface):
  * `escalate` writes an open escalation with the right `kind`/`from`/`task`, and
    records `as_of` = the current desired-state version.
  * `escalations` lists it while it is open.
  * `resolve` flips it to `resolved` with the note, and it drops off the open list.
  * an invalid `--kind` is rejected.

Like tests/test_coord.py and friends, each test drives the real CLI as a subprocess
against a throwaway COORD_ROOT (pytest `tmp_path`), never the repo's own
`.coordination/`. The interpreter is invoked via `sys.executable` (this box has no
`python3` alias).
"""
from __future__ import annotations

import json
import os
import re
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


def _eid_from(stdout: str) -> str:
    m = re.search(r"escalated (\d+):", stdout)
    assert m, f"no escalation id found in: {stdout!r}"
    return m.group(1)


def _version(coord) -> int:
    return json.loads(coord("state", "show").stdout)["version"]


def _escalation_file(coord, eid: str) -> dict:
    path = coord.root / "escalations" / f"{eid}.json"
    assert path.exists(), f"no escalation file for {eid}"
    return json.loads(path.read_text(encoding="utf-8"))


# --- escalate writes an open escalation with as_of = current version -------
def test_escalate_writes_open_escalation_with_as_of(coord):
    r = coord("escalate", "--session", "worker1", "--kind", "blocker",
               "--body", "stuck on flaky test", "--task", "gate-1")
    assert r.returncode == 0, r.stderr
    eid = _eid_from(r.stdout)

    esc = _escalation_file(coord, eid)
    assert esc["from"] == "worker1"
    assert esc["kind"] == "blocker"
    assert esc["task"] == "gate-1"
    assert esc["body"] == "stuck on flaky test"
    assert esc["status"] == "open"
    assert esc["as_of"] == _version(coord)
    assert esc["resolved_note"] is None


def test_escalate_without_task_records_null_task(coord):
    r = coord("escalate", "--session", "nav", "--kind", "decision", "--body", "which palette?")
    assert r.returncode == 0, r.stderr
    eid = _eid_from(r.stdout)
    esc = _escalation_file(coord, eid)
    assert esc["task"] is None


# --- an invalid --kind is rejected ------------------------------------------
def test_escalate_rejects_invalid_kind(coord):
    r = coord("escalate", "--session", "worker1", "--kind", "bogus", "--body", "x")
    assert r.returncode != 0


# --- escalations lists it while open ----------------------------------------
def test_escalations_lists_open_escalation(coord):
    r = coord("escalate", "--session", "worker1", "--kind", "fork", "--body", "two valid approaches")
    eid = _eid_from(r.stdout)

    listing = coord("escalations")
    assert listing.returncode == 0, listing.stderr
    assert eid in listing.stdout
    assert "worker1" in listing.stdout
    assert "two valid approaches" in listing.stdout

    listing_json = coord("escalations", "--json")
    assert listing_json.returncode == 0, listing_json.stderr
    items = json.loads(listing_json.stdout)
    assert any(e["eid"] == eid for e in items)


def test_escalations_empty_when_none_open(coord):
    r = coord("escalations")
    assert r.returncode == 0, r.stderr
    assert "no open escalations" in r.stdout.lower()


# --- resolve flips status and drops off the open list -----------------------
def test_resolve_marks_resolved_and_drops_from_open_list(coord):
    r = coord("escalate", "--session", "worker1", "--kind", "blocker", "--body", "need input")
    eid = _eid_from(r.stdout)

    res = coord("resolve", "--id", eid, "--note", "approved via state propose")
    assert res.returncode == 0, res.stderr

    esc = _escalation_file(coord, eid)
    assert esc["status"] == "resolved"
    assert esc["resolved_note"] == "approved via state propose"

    listing = coord("escalations")
    assert eid not in listing.stdout
    assert "no open escalations" in listing.stdout.lower()


def test_resolve_unknown_id_errors(coord):
    r = coord("resolve", "--id", "does-not-exist")
    assert r.returncode != 0
