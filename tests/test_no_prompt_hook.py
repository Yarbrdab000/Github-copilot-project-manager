"""
"No direct prompts" hook tests for hooks/scripts/write_scope_guard.py.

A coordinated session must never block the fleet on a human-prompt modal (e.g. the
`ask_user` tool): the cockpit cannot clear such a modal, and every queued dispatch
stalls behind it. The preToolUse guard therefore DENIES the ask_user tool for every
registered coordinated role and redirects to the escalation channel. An unregistered
session (nothing in the registry maps to its cwd) and every other tool are unaffected —
the guard fails open there, exactly as it does for reads.

Same subprocess harness as tests/test_cockpit_hook.py: JSON payload on stdin, decision
JSON on stdout, driven with the current interpreter.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GUARD = REPO / "hooks" / "scripts" / "write_scope_guard.py"


def run_guard(payload: dict, coord_root: Path) -> dict:
    env = dict(os.environ)
    env["COORD_ROOT"] = str(coord_root)
    env.pop("COORD_SESSION", None)
    proc = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, f"guard exited {proc.returncode}; stderr={proc.stderr}"
    assert proc.stdout.strip(), f"guard produced no stdout; stderr={proc.stderr}"
    return json.loads(proc.stdout)


def register(coord_root: Path, session: str, worktree: Path, owned, role, branch="feature"):
    reg_dir = coord_root / "registry"
    reg_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "session": session,
        "role": role,
        "branch": branch,
        "worktree": str(worktree),
        "owned_paths": list(owned),
        "registered": "2024-01-01T00:00:00Z",
        "heartbeat": 0,
        "heartbeat_iso": "2024-01-01T00:00:00Z",
    }
    (reg_dir / f"{session}.json").write_text(json.dumps(entry, indent=2), encoding="utf-8")
    return entry


def payload(cwd: Path, tool: str = "ask_user", **args) -> dict:
    """toolArgs is a JSON *string* in the real payload."""
    return {"cwd": str(cwd), "toolName": tool, "toolArgs": json.dumps(args)}


# --- ask_user is denied for EVERY registered coordinated role ----------------
@pytest.mark.parametrize("role", ["worker", "editor", "researcher", "navigator", "orchestrator"])
def test_ask_user_denied_for_every_coordinated_role(tmp_path, role):
    wt = tmp_path / "wt"
    wt.mkdir()
    register(tmp_path / "coordination", "s1", wt, ["**"], role=role)

    out = run_guard(payload(wt, question="warm or cool palette?"), tmp_path / "coordination")
    assert out["permissionDecision"] == "deny", out
    assert "escalate" in out["permissionDecisionReason"].lower(), out


# --- an unregistered session fails open (the guard only governs the fleet) ----
def test_ask_user_allowed_for_unregistered_session(tmp_path):
    (tmp_path / "coordination" / "registry").mkdir(parents=True)
    out = run_guard(payload(tmp_path / "stray", question="x"), tmp_path / "coordination")
    assert out["permissionDecision"] == "allow", out


# --- other (read) tools are still allowed for a coordinated session ----------
def test_other_read_tools_still_allowed_for_coordinated_role(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    register(tmp_path / "coordination", "s1", wt, ["**"], role="worker")
    for tool in ("grep", "view", "glob"):
        out = run_guard(payload(wt, tool=tool, pattern="x"), tmp_path / "coordination")
        assert out["permissionDecision"] == "allow", (tool, out)
