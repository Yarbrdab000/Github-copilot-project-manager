"""
Control-plane acceptance tests for `coord/coord.py` (SPEC.md §7.1–§7.5).

Each test drives the real CLI as a subprocess against a throwaway COORD_ROOT
(pytest `tmp_path`), never the repo's own `.coordination/`. Behaviors asserted
here are the ones transcribed in `reference/ACCEPTANCE.md`.

We invoke the interpreter via `sys.executable` for portability (this repo's dev
box has no `python3` alias, only `python`).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
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

    init = run("init")
    assert init.returncode == 0, init.stderr
    return run


def _register(coord, session, role="worker", branch=None):
    branch = branch or f"feat/{session}"
    r = coord("register", "--session", session, "--role", role, "--branch", branch)
    assert r.returncode == 0, r.stderr
    return r


# --- §7.1 Dependency blocking ---------------------------------------------
def test_claim_with_unmet_dependency_fails(coord):
    coord("add-task", "--id", "write-mapper", "--deps", "research-formatting")
    r = coord("claim", "--session", "editor", "--task", "write-mapper")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "blocked on unmet deps" in r.stderr
    assert "research-formatting" in r.stderr


def test_claim_succeeds_once_dependency_is_done(coord):
    coord("add-task", "--id", "research-formatting")
    coord("add-task", "--id", "write-mapper", "--deps", "research-formatting")
    # dep still open -> blocked
    assert coord("claim", "--session", "editor", "--task", "write-mapper").returncode == 1
    # mark dep done -> now claimable
    assert coord("claim", "--session", "researcher", "--task", "research-formatting").returncode == 0
    assert coord("complete", "--session", "researcher", "--task", "research-formatting").returncode == 0
    ok = coord("claim", "--session", "editor", "--task", "write-mapper")
    assert ok.returncode == 0, ok.stderr
    assert "editor claimed write-mapper" in ok.stdout


# --- §7.2 Atomic claim (exactly one winner) -------------------------------
def test_two_concurrent_claims_yield_exactly_one_winner(coord):
    coord("add-task", "--id", "research-formatting")
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [
            ex.submit(coord, "claim", "--session", s, "--task", "research-formatting")
            for s in ("researcher", "editor")
        ]
        results = [f.result() for f in futs]

    winners = [r for r in results if r.returncode == 0]
    losers = [r for r in results if r.returncode != 0]
    assert len(winners) == 1, [(r.returncode, r.stdout, r.stderr) for r in results]
    assert len(losers) == 1
    assert "claimed research-formatting" in winners[0].stdout

    # A subsequent claim of the now-claimed task is also rejected.
    again = coord("claim", "--session", "third", "--task", "research-formatting")
    assert again.returncode == 1
    assert "not claimable" in again.stderr


# --- §7.3 Lease deny, then heartbeat-gated steal + reap -------------------
def test_lease_denies_then_reap_steals_and_reacquires(coord):
    res = "shared/theme.json"  # slashed name -> must flatten to shared__theme.json

    # (a) a valid lease denies another session
    assert coord("lock", "acquire", "--session", "worker1", "--resource", res, "--ttl", "60").returncode == 0
    denied = coord("lock", "acquire", "--session", "worker2", "--resource", res, "--ttl", "60")
    assert denied.returncode == 1
    assert "held" in denied.stderr.lower()

    # re-take with a 1s lease by an *unregistered* holder (heartbeat provably stale)
    assert coord("lock", "release", "--session", "worker1", "--resource", res).returncode == 0
    assert coord("lock", "acquire", "--session", "worker1", "--resource", res, "--ttl", "1").returncode == 0
    time.sleep(1.2)  # let the TTL elapse

    # (b) reap reclaims the expired + stale lease; slash was flattened to __
    reap = coord("reap")
    assert reap.returncode == 0, reap.stderr
    data = json.loads(reap.stdout)
    reaped_names = [name for name, _holder in data["reaped_locks"]]
    assert "shared__theme.json" in reaped_names
    assert ["shared__theme.json", "worker1"] in data["reaped_locks"]

    # (c) a live session can now acquire it
    got = coord("lock", "acquire", "--session", "worker2", "--resource", res, "--ttl", "60")
    assert got.returncode == 0, got.stderr


def test_expired_lease_with_live_holder_is_not_stolen(coord):
    """Steal requires TTL expiry AND a stale heartbeat — not just expiry."""
    res = "shared/theme.json"
    _register(coord, "worker1")  # fresh heartbeat => NOT stale
    assert coord("lock", "acquire", "--session", "worker1", "--resource", res, "--ttl", "1").returncode == 0
    time.sleep(1.2)  # TTL elapsed, but worker1's heartbeat is seconds old

    # holder still alive -> another session is denied even though the lease expired
    denied = coord("lock", "acquire", "--session", "worker2", "--resource", res, "--ttl", "60")
    assert denied.returncode == 1
    assert "held" in denied.stderr.lower()

    # ...and reap must NOT reclaim it
    reap = coord("reap")
    assert reap.returncode == 0, reap.stderr
    reaped_names = [name for name, _holder in json.loads(reap.stdout)["reaped_locks"]]
    assert "shared__theme.json" not in reaped_names


# --- §7.4 Staleness filter -------------------------------------------------
def test_checkpoint_skips_stale_message_and_surfaces_current(coord):
    _register(coord, "editor")
    # version -> 1, then a message pinned to as_of=1
    coord("state", "set", "--session", "orch", "--key", "target_palette", "--value", '"v2"')
    coord("send", "--from", "orch", "--to", "editor", "--body", "use palette v1", "--as-of", "1")
    # version -> 2 (the as_of=1 message is now stale), then a current message
    coord("state", "set", "--session", "orch", "--key", "target_palette", "--value", '"v3"')
    coord("send", "--from", "orch", "--to", "editor", "--body", "now use v3", "--as-of", "2")

    cp = coord("checkpoint", "--session", "editor")
    assert cp.returncode == 0, cp.stderr
    out = json.loads(cp.stdout)
    assert out["desired_version"] == 2
    assert out["stale_messages_skipped"] == 1
    assert [m["body"] for m in out["messages"]] == ["now use v3"]
    assert out["messages"][0]["as_of"] == 2


# --- §7.5 Stop-flag halt ---------------------------------------------------
def test_global_stop_makes_checkpoint_exit_3(coord):
    _register(coord, "editor")
    assert coord("stop").returncode == 0
    cp = coord("checkpoint", "--session", "editor")
    assert cp.returncode == 3, (cp.returncode, cp.stdout, cp.stderr)
    out = json.loads(cp.stdout)  # state is still printed before the halt
    assert out["stop"] == ["GLOBAL"]


def test_session_stop_flag_halts_only_that_session(coord):
    _register(coord, "editor")
    _register(coord, "researcher")
    assert coord("stop", "--session", "editor").returncode == 0

    halted = coord("checkpoint", "--session", "editor")
    assert halted.returncode == 3
    assert json.loads(halted.stdout)["stop"] == ["editor"]

    running = coord("checkpoint", "--session", "researcher")
    assert running.returncode == 0
    assert json.loads(running.stdout)["stop"] == []
