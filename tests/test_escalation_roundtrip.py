"""
Escalation round-trip tests (the "no direct prompts" change).

`coord escalate --kind decision` is meant to replace a direct `ask_user` prompt: a
coordinated session raises a question, the human answers it in the cockpit, and the
answer must come *back* to the asking session. For that loop to close, `coord resolve`
has to deliver the human's note to the session that raised the escalation as a normal
checkpoint message, tied to the current desired-state version so `coord checkpoint`
surfaces it as fresh.

Same subprocess-against-a-throwaway-COORD_ROOT harness as
tests/test_autonomy_escalation.py (this box has no `python3` alias, so `sys.executable`).
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

    run.root = root.resolve()
    init = run("init")
    assert init.returncode == 0, init.stderr
    return run


def _eid_from(stdout: str) -> str:
    m = re.search(r"escalated (\d+):", stdout)
    assert m, f"no escalation id found in: {stdout!r}"
    return m.group(1)


def _version(coord) -> int:
    return json.loads(coord("state", "show").stdout)["version"]


def _register(coord, session: str) -> None:
    """Register `session` so `coord checkpoint` (which requires a registered session)
    can surface its inbox — the real path a worker uses to receive the answer."""
    r = coord("register", "--session", session, "--role", "editor",
              "--branch", "feature", "--paths", "**")
    assert r.returncode == 0, r.stderr


def _messages(coord, session: str):
    """The fresh messages the session would see at its next checkpoint boundary."""
    r = coord("checkpoint", "--session", session)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)["messages"]


def _inbox_lines(coord, session: str):
    """Raw queued messages for `session`, read straight off disk (works even for an
    unregistered pseudo-session like `tick`)."""
    path = coord.root / "inbox" / f"{session}.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# --- resolve delivers the human's answer back to the asking session ----------
def test_resolve_delivers_answer_to_asking_session(coord):
    _register(coord, "w1")
    r = coord("escalate", "--session", "w1", "--kind", "decision",
              "--body", "which palette, warm or cool?")
    eid = _eid_from(r.stdout)

    # Nothing is waiting for w1 until the human resolves the escalation.
    assert _messages(coord, "w1") == []

    res = coord("resolve", "--id", eid, "--note", "use the warm palette")
    assert res.returncode == 0, res.stderr

    msgs = _messages(coord, "w1")
    assert len(msgs) == 1, msgs
    body = msgs[0]["body"]
    assert eid in body, body
    assert "use the warm palette" in body, body


# --- the answer is tied to the current version, so checkpoint sees it fresh ---
def test_resolve_answer_is_fresh_at_current_version(coord):
    _register(coord, "w1")
    r = coord("escalate", "--session", "w1", "--kind", "decision", "--body", "q")
    eid = _eid_from(r.stdout)

    res = coord("resolve", "--id", eid, "--note", "a")
    assert res.returncode == 0, res.stderr

    msgs = _messages(coord, "w1")
    assert msgs, "answer was not delivered"
    assert msgs[0]["as_of"] == _version(coord)


# --- resolving a tick-authored escalation queues no message ------------------
def test_resolve_tick_escalation_sends_no_message(coord):
    # `tick` raises blocker escalations from the pseudo-session "tick"; there is no
    # session by that name to receive an answer, so resolving one must send nothing.
    r = coord("escalate", "--session", "tick", "--kind", "blocker", "--body", "verify failed")
    eid = _eid_from(r.stdout)

    res = coord("resolve", "--id", eid, "--note", "looked into it")
    assert res.returncode == 0, res.stderr

    assert _inbox_lines(coord, "tick") == []
