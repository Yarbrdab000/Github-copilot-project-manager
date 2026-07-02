"""
Tests for hooks/scripts/write_scope_guard.py — the preToolUse write-scope hook.

Covers SPEC.md 7.6 (owned -> allow, out-of-owned -> deny, ../ traversal -> deny, read tool
-> allow) plus edge cases: absolute paths, alternate path keys, empty ownership, the bash
best-effort rules, the COORD_SESSION override, nested-cwd resolution, and fail-open behavior.

Self-contained: no conftest. The control plane is set up by writing registry JSON directly,
and the guard is driven exactly like Copilot drives it — JSON payload on stdin, decision JSON
on stdout — via a subprocess using the current interpreter.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GUARD = REPO / "hooks" / "scripts" / "write_scope_guard.py"


def run_guard(payload: dict, coord_root: Path, coord_session: str = None) -> dict:
    """Run the guard with `payload` as stdin JSON; return the parsed decision dict."""
    env = dict(os.environ)
    env["COORD_ROOT"] = str(coord_root)
    env.pop("COORD_SESSION", None)
    if coord_session is not None:
        env["COORD_SESSION"] = coord_session
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


def run_guard_raw(raw_stdin: str, coord_root: Path) -> dict:
    env = dict(os.environ)
    env["COORD_ROOT"] = str(coord_root)
    env.pop("COORD_SESSION", None)
    proc = subprocess.run(
        [sys.executable, str(GUARD)],
        input=raw_stdin,
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, f"guard exited {proc.returncode}; stderr={proc.stderr}"
    return json.loads(proc.stdout)


def register(coord_root: Path, session: str, worktree: Path, owned, branch="feature"):
    """Write a registry entry directly (isolates these tests from the coord CLI)."""
    reg_dir = coord_root / "registry"
    reg_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "session": session,
        "role": "editor",
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
def scene(tmp_path):
    """A coordinated 'editor' session owning src/** and README.md under a worktree."""
    coord_root = tmp_path / "coordination"
    worktree = tmp_path / "wt"
    (worktree / "src").mkdir(parents=True)
    register(coord_root, "editor", worktree, ["src/**", "README.md"], branch="feature")
    return {"root": coord_root, "wt": worktree}


# --- SPEC 7.6: the four required cases ------------------------------------------------

def test_write_to_owned_path_allowed(scene):
    payload = {"cwd": str(scene["wt"]), "toolName": "edit", "toolArgs": tool_args(path="src/app.py")}
    assert run_guard(payload, scene["root"]) == {"permissionDecision": "allow"}


def test_write_to_unowned_path_denied(scene):
    payload = {"cwd": str(scene["wt"]), "toolName": "edit", "toolArgs": tool_args(path="docs/guide.md")}
    decision = run_guard(payload, scene["root"])
    assert decision["permissionDecision"] == "deny"
    assert "owned paths" in decision["permissionDecisionReason"]


def test_parent_traversal_denied(scene):
    payload = {"cwd": str(scene["wt"]), "toolName": "create", "toolArgs": tool_args(path="../escape.py")}
    decision = run_guard(payload, scene["root"])
    assert decision["permissionDecision"] == "deny"
    assert "outside" in decision["permissionDecisionReason"]


def test_read_tool_allowed(scene):
    # A read tool is allowed even when it points outside the worktree entirely.
    payload = {"cwd": str(scene["wt"]), "toolName": "view", "toolArgs": tool_args(path="/etc/hosts")}
    assert run_guard(payload, scene["root"]) == {"permissionDecision": "allow"}


# --- write-tool edge cases ------------------------------------------------------------

def test_absolute_path_outside_denied(scene, tmp_path):
    outside = tmp_path / "elsewhere" / "x.py"
    payload = {"cwd": str(scene["wt"]), "toolName": "write", "toolArgs": tool_args(path=str(outside))}
    assert run_guard(payload, scene["root"])["permissionDecision"] == "deny"


def test_absolute_path_inside_owned_allowed(scene):
    inside = scene["wt"] / "src" / "deep" / "mod.py"
    payload = {"cwd": str(scene["wt"]), "toolName": "edit", "toolArgs": tool_args(path=str(inside))}
    assert run_guard(payload, scene["root"]) == {"permissionDecision": "allow"}


@pytest.mark.parametrize("key", ["path", "file_path", "filePath", "filename"])
def test_alternate_path_keys(scene, key):
    payload = {"cwd": str(scene["wt"]), "toolName": "create_file", "toolArgs": json.dumps({key: "src/new.py"})}
    assert run_guard(payload, scene["root"]) == {"permissionDecision": "allow"}


def test_exact_file_ownership(scene):
    ok = {"cwd": str(scene["wt"]), "toolName": "edit", "toolArgs": tool_args(path="README.md")}
    assert run_guard(ok, scene["root"]) == {"permissionDecision": "allow"}
    no = {"cwd": str(scene["wt"]), "toolName": "edit", "toolArgs": tool_args(path="CHANGELOG.md")}
    assert run_guard(no, scene["root"])["permissionDecision"] == "deny"


def test_empty_owned_paths_denies_all(tmp_path):
    coord_root = tmp_path / "coordination"
    wt = tmp_path / "wt"
    wt.mkdir()
    register(coord_root, "locked", wt, [], branch="feature")
    payload = {"cwd": str(wt), "toolName": "edit", "toolArgs": tool_args(path="anything.py")}
    assert run_guard(payload, coord_root)["permissionDecision"] == "deny"


# --- bash best-effort rules -----------------------------------------------------------

def test_bash_git_push_denied(scene):
    payload = {"cwd": str(scene["wt"]), "toolName": "bash", "toolArgs": tool_args(command="git push origin feature")}
    assert run_guard(payload, scene["root"])["permissionDecision"] == "deny"


def test_bash_checkout_other_branch_denied(scene):
    payload = {"cwd": str(scene["wt"]), "toolName": "bash", "toolArgs": tool_args(command="git checkout main")}
    assert run_guard(payload, scene["root"])["permissionDecision"] == "deny"


def test_bash_checkout_session_branch_allowed(scene):
    payload = {"cwd": str(scene["wt"]), "toolName": "bash", "toolArgs": tool_args(command="git checkout feature")}
    assert run_guard(payload, scene["root"]) == {"permissionDecision": "allow"}


def test_bash_redirect_outside_denied(scene, tmp_path):
    outside = tmp_path / "loot.txt"
    payload = {"cwd": str(scene["wt"]), "toolName": "bash", "toolArgs": tool_args(command=f"echo hi > {outside}")}
    assert run_guard(payload, scene["root"])["permissionDecision"] == "deny"


def test_bash_plain_command_allowed(scene):
    payload = {"cwd": str(scene["wt"]), "toolName": "bash", "toolArgs": tool_args(command="pytest -q && ls src")}
    assert run_guard(payload, scene["root"]) == {"permissionDecision": "allow"}


# --- session resolution ---------------------------------------------------------------

def test_coord_session_override_selects_session(tmp_path):
    coord_root = tmp_path / "coordination"
    wt_a = tmp_path / "a"
    wt_b = tmp_path / "b"
    (wt_a / "src").mkdir(parents=True)
    (wt_b / "lib").mkdir(parents=True)
    register(coord_root, "alpha", wt_a, ["src/**"])
    register(coord_root, "beta", wt_b, ["lib/**"])
    # cwd points at alpha's tree, but the override forces beta's scope -> src is not owned.
    payload = {"cwd": str(wt_a), "toolName": "edit", "toolArgs": tool_args(path="src/x.py")}
    assert run_guard(payload, coord_root, coord_session="beta")["permissionDecision"] == "deny"


def test_nested_cwd_resolves_session(scene):
    nested = scene["wt"] / "src"
    payload = {"cwd": str(nested), "toolName": "edit", "toolArgs": tool_args(path=str(scene["wt"] / "src" / "y.py"))}
    assert run_guard(payload, scene["root"]) == {"permissionDecision": "allow"}


# --- fail-open behavior ---------------------------------------------------------------

def test_failopen_on_bad_json(scene):
    assert run_guard_raw("this is not json", scene["root"]) == {"permissionDecision": "allow"}


def test_failopen_on_unresolved_session(tmp_path):
    coord_root = tmp_path / "coordination"
    (coord_root / "registry").mkdir(parents=True)  # empty registry
    payload = {"cwd": str(tmp_path / "unknown"), "toolName": "edit", "toolArgs": tool_args(path="src/x.py")}
    assert run_guard(payload, coord_root) == {"permissionDecision": "allow"}


def test_failopen_write_without_path(scene):
    payload = {"cwd": str(scene["wt"]), "toolName": "edit", "toolArgs": tool_args(mode="append")}
    assert run_guard(payload, scene["root"]) == {"permissionDecision": "allow"}
