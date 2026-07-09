"""
Cockpit hook-enforcement tests for hooks/scripts/write_scope_guard.py
(COCKPIT_SPEC §3.2/§3.4, acceptance §7.9).

Two additive rules on top of the existing NAVIGATOR_SPEC/AUTONOMY_SPEC hook behavior:

  * A `navigator`-role session's bash allow-list grows to include `coord plan propose`,
    `coord plans`, `coord plan show`, and `coord cockpit` -- but NOT `coord plan approve`/
    `coord plan reject`.
  * `coord plan approve`/`coord plan reject` are ORCHESTRATOR-ONLY: any non-orchestrator role
    (editor, worker, navigator) is denied, mirroring the existing `add-task --verify` rule.

Same shape as tests/test_write_scope_guard.py, tests/test_navigator_hook.py, and
tests/test_autonomy_hook.py: the control plane is a directory of registry JSON, and the guard
is driven exactly as Copilot drives it -- JSON payload on stdin, decision JSON on stdout -- as
a subprocess with the current interpreter.
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
def nav(tmp_path):
    coord_root = tmp_path / "coordination"
    worktree = tmp_path / "wt"
    (worktree / "src").mkdir(parents=True)
    register(coord_root, "nav", worktree, ["**"], role="navigator")
    return {"root": coord_root, "wt": worktree}


@pytest.fixture
def editor(tmp_path):
    coord_root = tmp_path / "coordination"
    worktree = tmp_path / "wt"
    (worktree / "src").mkdir(parents=True)
    register(coord_root, "ed1", worktree, ["src/**"], role="editor")
    return {"root": coord_root, "wt": worktree}


@pytest.fixture
def worker(tmp_path):
    coord_root = tmp_path / "coordination"
    worktree = tmp_path / "wt"
    (worktree / "src").mkdir(parents=True)
    register(coord_root, "w1", worktree, ["src/**"], role="worker")
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


# --- §7.9: navigator's grown allow-list --------------------------------------------
@pytest.mark.parametrize("command", [
    "coord plan propose --file plan.json",
    "coord plans",
    "coord plan show --id 123",
    "coord cockpit",
    "coord cockpit --json",
])
def test_navigator_new_commands_allowed(nav, command):
    assert _bash(nav, command) == {"permissionDecision": "allow"}, command


@pytest.mark.parametrize("command", [
    "coord plan approve --id 123",
    "coord plan reject --id 123",
])
def test_navigator_plan_approve_reject_still_denied(nav, command):
    d = _bash(nav, command)
    assert d["permissionDecision"] == "deny", command


# --- §7.9: plan approve/reject are orchestrator-only ------------------------------
@pytest.mark.parametrize("command", [
    "coord plan approve --id 123",
    "coord plan reject --id 123",
])
def test_editor_plan_approve_reject_denied(editor, command):
    d = _bash(editor, command)
    assert d["permissionDecision"] == "deny", command
    assert "orchestrator" in d["permissionDecisionReason"]


@pytest.mark.parametrize("command", [
    "coord plan approve --id 123",
    "coord plan reject --id 123",
])
def test_worker_plan_approve_reject_denied(worker, command):
    d = _bash(worker, command)
    assert d["permissionDecision"] == "deny", command
    assert "orchestrator" in d["permissionDecisionReason"]


@pytest.mark.parametrize("command", [
    "coord plan approve --id 123",
    "coord plan reject --id 123",
])
def test_orchestrator_plan_approve_reject_allowed(orch, command):
    assert _bash(orch, command) == {"permissionDecision": "allow"}, command


# --- alias / spelled-out / compound-segment variants ------------------------------
def test_editor_plan_approve_spelled_out_denied(editor):
    d = _bash(editor, "python coord/coord.py plan approve --id 123")
    assert d["permissionDecision"] == "deny"


def test_editor_plan_reject_spelled_out_denied(editor):
    d = _bash(editor, "py coord/coord.py plan reject --id 123")
    assert d["permissionDecision"] == "deny"


def test_editor_plan_approve_in_compound_command_denied(editor):
    d = _bash(editor, "coord status && coord plan approve --id 123")
    assert d["permissionDecision"] == "deny"


def test_orchestrator_plan_approve_spelled_out_allowed(orch):
    d = _bash(orch, "python coord/coord.py plan approve --id 123")
    assert d == {"permissionDecision": "allow"}


# --- false-positive guard: 'plan approve' as prose text, not a real subcommand ----
def test_editor_add_task_mentioning_plan_approve_in_desc_not_falsely_denied(editor):
    d = _bash(editor, 'coord add-task --id t --desc "plan approve later"')
    assert d["permissionDecision"] == "allow"


# --- regressions: existing editor/worker behavior unchanged -----------------------
def test_editor_edit_within_owned_path_still_allowed(editor):
    payload = {"cwd": str(editor["wt"]), "toolName": "edit", "toolArgs": tool_args(path="src/app.py")}
    assert run_guard(payload, editor["root"]) == {"permissionDecision": "allow"}


def test_worker_git_push_still_denied(worker):
    d = _bash(worker, "git push origin feature")
    assert d["permissionDecision"] == "deny"


def test_worker_edit_within_owned_path_still_allowed(worker):
    payload = {"cwd": str(worker["wt"]), "toolName": "edit", "toolArgs": tool_args(path="src/app.py")}
    assert run_guard(payload, worker["root"]) == {"permissionDecision": "allow"}


def test_navigator_file_edit_still_denied(nav):
    inside = nav["wt"] / "src" / "app.py"
    payload = {"cwd": str(nav["wt"]), "toolName": "edit", "toolArgs": tool_args(path=str(inside))}
    d = run_guard(payload, nav["root"])
    assert d["permissionDecision"] == "deny"
    assert "navigator" in d["permissionDecisionReason"]


def test_navigator_state_approve_still_denied(nav):
    d = _bash(nav, "coord state approve --id 123")
    assert d["permissionDecision"] == "deny"


# --- frontmatter of the edited skills/agents still parses -------------------------
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


@pytest.mark.parametrize("path", [
    REPO / "skills" / "navigator" / "SKILL.md",
    REPO / "skills" / "orchestrator" / "SKILL.md",
    REPO / "agents" / "navigator.agent.md",
])
def test_edited_docs_frontmatter_still_parses(path):
    fm = _parse_frontmatter(path)
    assert fm  # non-empty: real frontmatter keys survived the edit
