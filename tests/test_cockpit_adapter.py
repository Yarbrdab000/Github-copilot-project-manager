"""
Runtime adapter reference tests (COCKPIT_SPEC §6, acceptance §7.10).

runtime/adapter.reference.py is a thin, offline, stdlib-only reference for the
filesystem-to-runtime seam: it reads directives from `state/directives.jsonl`
under COORD_ROOT and, in `--dry-run` (default and only real mode here), prints
one line per directive describing the intended action -- it never performs
that action. These tests drive the real CLI as a subprocess (mirroring the
pattern used for the hook tests) and assert:
  * the printed lines match the fixture's directives, in ledger order,
  * the run is fully offline -- no socket is ever opened.
"""
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ADAPTER = REPO / "runtime" / "adapter.reference.py"


def _write_directives(coord_root: Path, directives: list) -> Path:
    path = coord_root / "state" / "directives.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for d in directives:
            f.write(json.dumps(d) + "\n")
    return path


def _run_adapter(coord_root: Path, extra_args=None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["COORD_ROOT"] = str(coord_root)
    cmd = [sys.executable, str(ADAPTER)] + (extra_args or [])
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def test_adapter_prints_intended_actions_in_ledger_order(tmp_path):
    coord_root = tmp_path / "coordination"
    directives = [
        {"kind": "spawn", "worker": "w1", "owned_paths": ["src/w1/**"], "as_of": 1, "ts": "2024-01-01T00:00:00Z"},
        {"kind": "dispatch", "worker": "w1", "task": "build-frontend", "ts": "2024-01-01T00:00:01Z"},
        {"kind": "spawn", "worker": "w2", "owned_paths": ["src/w2/**"], "as_of": 1, "ts": "2024-01-01T00:00:02Z"},
    ]
    _write_directives(coord_root, directives)

    proc = _run_adapter(coord_root, ["--dry-run"])

    assert proc.returncode == 0, proc.stderr
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    assert lines == [
        "create session role=editor worker=w1 owning=['src/w1/**']",
        "wake worker=w1 task=build-frontend",
        "create session role=editor worker=w2 owning=['src/w2/**']",
    ]


def test_adapter_default_mode_is_dry_run(tmp_path):
    """--dry-run is default; running with no flags must behave identically."""
    coord_root = tmp_path / "coordination"
    _write_directives(coord_root, [
        {"kind": "spawn", "worker": "w1", "owned_paths": ["src/w1/**"]},
    ])

    proc = _run_adapter(coord_root, [])

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "create session role=editor worker=w1 owning=['src/w1/**']"


def test_adapter_unknown_directive_kind_is_skipped_deterministically(tmp_path):
    coord_root = tmp_path / "coordination"
    _write_directives(coord_root, [
        {"kind": "mystery", "foo": "bar"},
        {"kind": "spawn", "worker": "w1", "owned_paths": []},
    ])

    proc = _run_adapter(coord_root, ["--dry-run"])

    assert proc.returncode == 0, proc.stderr
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    assert lines == [
        "skip mystery",
        "create session role=editor worker=w1 owning=[]",
    ]


def test_adapter_missing_directives_file_yields_no_output(tmp_path):
    coord_root = tmp_path / "coordination"  # never written
    proc = _run_adapter(coord_root, ["--dry-run"])
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""


def test_adapter_never_performs_actions_in_dry_run(tmp_path):
    """The reference's hand-off stub (_perform) must never be reached in
    --dry-run: if it were, it raises NotImplementedError and the subprocess
    would exit non-zero."""
    coord_root = tmp_path / "coordination"
    _write_directives(coord_root, [
        {"kind": "spawn", "worker": "w1", "owned_paths": ["src/w1/**"]},
        {"kind": "dispatch", "worker": "w1", "task": "t"},
    ])
    proc = _run_adapter(coord_root, ["--dry-run"])
    assert proc.returncode == 0, proc.stderr
    assert "NotImplementedError" not in proc.stderr


# --- offline proof: no socket is ever opened --------------------------------
def test_adapter_is_fully_offline(tmp_path, monkeypatch):
    coord_root = tmp_path / "coordination"
    _write_directives(coord_root, [
        {"kind": "spawn", "worker": "w1", "owned_paths": ["src/w1/**"]},
        {"kind": "dispatch", "worker": "w1", "task": "build-frontend"},
    ])

    def _no_sockets(*a, **k):
        raise AssertionError("adapter.reference.py attempted to open a socket")

    monkeypatch.setattr(socket, "socket", _no_sockets)

    # Run the module in-process (so the monkeypatched socket.socket applies)
    # by importing it directly rather than via subprocess.
    import importlib.util

    spec = importlib.util.spec_from_file_location("adapter_reference", ADAPTER)
    module = importlib.util.module_from_spec(spec)
    env_backup = os.environ.get("COORD_ROOT")
    os.environ["COORD_ROOT"] = str(coord_root)
    try:
        spec.loader.exec_module(module)
        rc = module.main(["--dry-run"])
    finally:
        if env_backup is None:
            os.environ.pop("COORD_ROOT", None)
        else:
            os.environ["COORD_ROOT"] = env_backup

    assert rc == 0
    # If socket.socket had been called, _no_sockets would have raised above.
