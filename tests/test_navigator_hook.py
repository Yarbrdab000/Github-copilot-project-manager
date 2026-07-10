"""
Navigator-enforcement tests for hooks/scripts/write_scope_guard.py (NAVIGATOR_SPEC §7.7).

Makes "conversation without authority" a HARD property: for a navigator-role
session the hook must DENY every file edit and everything on bash except a
propose/read allow-list, while leaving normal worker behavior unchanged.

Self-contained (no conftest), same shape as tests/test_write_scope_guard.py: the
control plane is a directory of registry JSON, and the guard is driven exactly as
Copilot drives it — JSON payload on stdin, decision JSON on stdout — as a
subprocess with the current interpreter.
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


def register(coord_root: Path, session: str, worktree: Path, owned, role, branch="feat/navigator"):
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


def tool_args(**kw) -> str:
    """toolArgs is a JSON *string* in the real payload."""
    return json.dumps(kw)


@pytest.fixture
def nav(tmp_path):
    """A coordinated 'navigator' session owning everything under a worktree."""
    coord_root = tmp_path / "coordination"
    worktree = tmp_path / "wt"
    (worktree / "src").mkdir(parents=True)
    register(coord_root, "nav", worktree, ["**"], role="navigator")
    return {"root": coord_root, "wt": worktree}


def _bash(nav, command: str) -> dict:
    payload = {"cwd": str(nav["wt"]), "toolName": "bash", "toolArgs": tool_args(command=command)}
    return run_guard(payload, nav["root"])


# --- §7.7: navigator bash denials ------------------------------------------
@pytest.mark.parametrize("command", [
    "coord claim --session nav --task build-thing",
    "coord state set --session nav --key k --value v",
    "coord state approve --id 123",
    "coord state reject --id 123",
    "coord complete --session nav --task t",
    "coord add-task --id t",
    "coord lock acquire --session nav --resource r",
    "coord send --from nav --to orch --body hi",
    "coord stop",
    "git push origin feat/navigator",
    "git merge main",
    "git checkout main",
    "git commit -m x",
])
def test_navigator_bash_denied(nav, command):
    d = _bash(nav, command)
    assert d["permissionDecision"] == "deny", command


# --- §7.7: navigator bash allows -------------------------------------------
@pytest.mark.parametrize("command", [
    "coord state propose --session nav --key target --value v2 --invalidates a,b",
    "coord state proposals",
    "coord state show",
    "coord status",
    "coord tasks",
    "coord plan analyze --file plan.json",
    "coord plan analyze --file plan.json --json",
    "coord plan seams --root . --workers 3",
    "coord plan seams --root . --json",
    "coord plan scaffold --root .",
    "coord plan scaffold --root . --workers 3 --max-concurrent 2",
    "git status",
    "git log --oneline -5",
    "git diff",
    "cat NAVIGATOR_SPEC.md",
    "ls src",
    "grep -n propose coord/coord.py",
])
def test_navigator_bash_allowed(nav, command):
    d = _bash(nav, command)
    assert d == {"permissionDecision": "allow"}, command


# --- §7.7: navigator file-edit tools are denied regardless of path ---------
@pytest.mark.parametrize("tool", ["edit", "create", "str_replace", "write", "create_file", "apply_patch"])
def test_navigator_file_edit_denied(nav, tool):
    inside = nav["wt"] / "src" / "app.py"
    payload = {"cwd": str(nav["wt"]), "toolName": tool, "toolArgs": tool_args(path=str(inside))}
    d = run_guard(payload, nav["root"])
    assert d["permissionDecision"] == "deny"
    assert "navigator" in d["permissionDecisionReason"]


# --- normalization: spelled-out `python coord/coord.py` can't bypass -------
def test_navigator_spelled_out_state_set_denied(nav):
    assert _bash(nav, "python coord/coord.py state set --key k --value v")["permissionDecision"] == "deny"
    assert _bash(nav, "py coord/coord.py claim --task t")["permissionDecision"] == "deny"


def test_navigator_spelled_out_propose_allowed(nav):
    assert _bash(nav, "python coord/coord.py state propose --key k --value v") == {"permissionDecision": "allow"}


# --- compound commands: any denied segment denies the whole ----------------
def test_navigator_compound_denies_if_any_segment_denied(nav):
    assert _bash(nav, "coord state show && git push")["permissionDecision"] == "deny"
    assert _bash(nav, "git status; coord claim --task t")["permissionDecision"] == "deny"
    # all-allowed compound is allowed
    assert _bash(nav, "coord state show && git status") == {"permissionDecision": "allow"}


# --- redirection to a file is denied regardless of target ------------------
def test_navigator_redirect_denied(nav):
    assert _bash(nav, "coord state proposals > loot.txt")["permissionDecision"] == "deny"
    assert _bash(nav, "coord tasks >> out.log")["permissionDecision"] == "deny"


# --- non-navigator behavior is unchanged: the SAME edit is allowed ---------
def test_worker_role_edit_still_allowed(tmp_path):
    coord_root = tmp_path / "coordination"
    worktree = tmp_path / "wt"
    (worktree / "src").mkdir(parents=True)
    register(coord_root, "editor", worktree, ["src/**"], role="editor", branch="feat/x")
    inside = worktree / "src" / "app.py"
    payload = {"cwd": str(worktree), "toolName": "edit", "toolArgs": tool_args(path=str(inside))}
    assert run_guard(payload, coord_root) == {"permissionDecision": "allow"}
    # ...and a worker may run coord claim (which a navigator may not)
    claim = {"cwd": str(worktree), "toolName": "bash",
             "toolArgs": tool_args(command="coord claim --session editor --task t")}
    assert run_guard(claim, coord_root) == {"permissionDecision": "allow"}
