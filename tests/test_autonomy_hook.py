"""
Acceptance-gate hardening tests for hooks/scripts/write_scope_guard.py (AUTONOMY_SPEC §4, §7.10).

Enforces "acceptance gates must come from the approved plan": only an `orchestrator`-role
session may run `coord add-task ... --verify ...`. An `editor`/`worker` session attaching
its own `--verify` (or the orchestrator role's proposal being bypassed via the spelled-out
`python coord/coord.py` form, or hidden inside a compound `&&`/`;`/`|` command) must be
denied. A plain `add-task` with no `--verify` is unaffected.

Same shape as tests/test_write_scope_guard.py and tests/test_navigator_hook.py: the control
plane is a directory of registry JSON, and the guard is driven exactly as Copilot drives it —
JSON payload on stdin, decision JSON on stdout — as a subprocess with the current interpreter.
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


def tool_args(**kw) -> str:
    """toolArgs is a JSON *string* in the real payload."""
    return json.dumps(kw)


@pytest.fixture
def editor(tmp_path):
    coord_root = tmp_path / "coordination"
    worktree = tmp_path / "wt"
    (worktree / "src").mkdir(parents=True)
    register(coord_root, "ed1", worktree, ["src/**"], role="editor")
    return {"root": coord_root, "wt": worktree}


@pytest.fixture
def orch(tmp_path):
    coord_root = tmp_path / "coordination"
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True)
    register(coord_root, "orch1", worktree, ["**"], role="orchestrator")
    return {"root": coord_root, "wt": worktree}


def _bash(scene, command: str) -> dict:
    payload = {"cwd": str(scene["wt"]), "toolName": "bash", "toolArgs": tool_args(command=command)}
    return run_guard(payload, scene["root"])


# --- §7.10: non-orchestrator --verify denials -------------------------------
def test_editor_add_task_with_verify_alias_denied(editor):
    decision = _bash(editor, 'coord add-task --id x --verify "pytest -q"')
    assert decision["permissionDecision"] == "deny"
    assert "orchestrator" in decision["permissionDecisionReason"]


def test_editor_add_task_with_verify_spelled_out_denied(editor):
    decision = _bash(editor, 'python coord/coord.py add-task --id x --verify "pytest -q"')
    assert decision["permissionDecision"] == "deny"


def test_editor_add_task_with_verify_in_compound_command_denied(editor):
    decision = _bash(editor, 'coord status && coord add-task --id x --verify "pytest"')
    assert decision["permissionDecision"] == "deny"


def test_editor_add_task_without_verify_still_allowed(editor):
    decision = _bash(editor, "coord add-task --id x")
    assert decision["permissionDecision"] == "allow"


def test_editor_add_task_verify_inside_quoted_desc_not_falsely_denied(editor):
    # a literal '--verify' inside the task's own quoted --desc string must NOT trip the
    # guard -- only a real --verify flag token (outside quotes) should deny.
    decision = _bash(editor, 'coord add-task --id x --desc "mentions --verify in prose"')
    assert decision["permissionDecision"] == "allow"


def test_orchestrator_add_task_with_verify_allowed(orch):
    decision = _bash(orch, 'coord add-task --id x --verify "pytest -q"')
    assert decision["permissionDecision"] == "allow"


# --- regression guards: existing behavior unchanged -------------------------
def test_navigator_add_task_still_denied(tmp_path):
    coord_root = tmp_path / "coordination"
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True)
    register(coord_root, "nav", worktree, ["**"], role="navigator")
    scene = {"root": coord_root, "wt": worktree}
    decision = _bash(scene, "coord add-task --id t")
    assert decision["permissionDecision"] == "deny"


def test_worker_edit_within_owned_path_still_allowed(editor):
    payload = {"cwd": str(editor["wt"]), "toolName": "edit", "toolArgs": tool_args(path="src/app.py")}
    assert run_guard(payload, editor["root"]) == {"permissionDecision": "allow"}


def test_worker_git_push_still_denied(editor):
    decision = _bash(editor, "git push origin feature")
    assert decision["permissionDecision"] == "deny"
