"""Offline tests for scripts/self_update.py.

Everything runs against fixture ``source`` / ``target`` plugin trees built under
``tmp_path`` -- no network, no reliance on the real install. The helper is loaded
by path so the test does not depend on ``scripts`` being importable as a package.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "self_update.py"

_spec = importlib.util.spec_from_file_location("self_update", SCRIPT)
self_update = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(self_update)


# A minimal, runnable coord.py: exits 0 on `--help` and prints something with
# "coord" in it, so the helper's post-update verify passes.
COORD_OK = (
    "import argparse\n"
    "def cmd_cockpit():\n"
    "    pass\n"
    "def main():\n"
    "    argparse.ArgumentParser(prog='coord').parse_args()\n"
    "if __name__ == '__main__':\n"
    "    main()\n"
)
# A runnable coord.py that always exits non-zero -> verify fails.
COORD_BROKEN = "import sys\nsys.exit(7)\n"


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _make_plugin(root: Path, version: str, coord_body: str = COORD_OK,
                 extra: dict | None = None) -> None:
    _write(root, ".claude-plugin/plugin.json", json.dumps({"version": version}))
    _write(root, "coord/coord.py", coord_body)
    for rel, text in (extra or {}).items():
        _write(root, rel, text)


def _files(root: Path) -> set:
    return {p.relative_to(root).as_posix()
            for p in root.rglob("*") if p.is_file()}


def _version(root: Path) -> str:
    return json.loads((root / ".claude-plugin/plugin.json").read_text())["version"]


# --- dry-run --------------------------------------------------------------------
def test_dry_run_writes_nothing(tmp_path, capsys):
    source = tmp_path / "src"
    target = tmp_path / "tgt"
    _make_plugin(source, "0.2.0", extra={"skills/new/SKILL.md": "new"})
    _make_plugin(target, "0.1.0", extra={"skills/old/SKILL.md": "old"})
    before = _files(target)

    rc = self_update.main(["--source", str(source), "--target", str(target), "--dry-run"])

    assert rc == 0
    assert _files(target) == before          # nothing added or removed
    assert _version(target) == "0.1.0"       # manifest untouched
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "0.1.0 -> 0.2.0" in out
    assert "[add] skills/new/SKILL.md" in out
    assert "[remove] skills/old/SKILL.md" in out
    # no backup was created anywhere under tmp
    assert not (tmp_path / "backup").exists()


# --- real apply -----------------------------------------------------------------
def test_apply_backs_up_and_mirrors(tmp_path):
    source = tmp_path / "src"
    target = tmp_path / "tgt"
    backup = tmp_path / "backup"
    # source: bumped version, a NEW file, a CHANGED file; no stale file
    _make_plugin(source, "0.2.0", extra={
        "skills/new/SKILL.md": "brand new",
        "docs/keep.md": "changed upstream",
    })
    # target: old version, MISSING the new file, DIFFERENT keep.md, has a STALE file
    _make_plugin(target, "0.1.0", extra={
        "docs/keep.md": "old local",
        "skills/removed/SKILL.md": "should be deleted",
    })

    rc = self_update.main([
        "--source", str(source), "--target", str(target),
        "--backup-dir", str(backup),
    ])

    assert rc == 0
    # target now mirrors source
    assert _version(target) == "0.2.0"
    assert (target / "skills/new/SKILL.md").read_text() == "brand new"
    assert (target / "docs/keep.md").read_text() == "changed upstream"
    assert not (target / "skills/removed/SKILL.md").exists()   # stale file pruned
    # backup captured the ORIGINAL target
    assert _version(backup) == "0.1.0"
    assert (backup / "docs/keep.md").read_text() == "old local"
    assert (backup / "skills/removed/SKILL.md").exists()


# --- failed verify auto-restores ------------------------------------------------
def test_verify_failure_auto_restores(tmp_path, capsys):
    source = tmp_path / "src"
    target = tmp_path / "tgt"
    backup = tmp_path / "backup"
    # source ships a coord.py that fails `--help`
    _make_plugin(source, "0.2.0", coord_body=COORD_BROKEN,
                 extra={"skills/new/SKILL.md": "new"})
    _make_plugin(target, "0.1.0", extra={"skills/old/SKILL.md": "old"})
    before = _files(target)

    rc = self_update.main([
        "--source", str(source), "--target", str(target),
        "--backup-dir", str(backup),
    ])

    assert rc == 1
    # target rolled back exactly to its pre-update state
    assert _files(target) == before
    assert _version(target) == "0.1.0"
    assert (target / "coord/coord.py").read_text() == COORD_OK
    assert not (target / "skills/new/SKILL.md").exists()
    err = capsys.readouterr().err
    assert "verify failed" in err
    assert "restored" in err


# --- excludes are honored on both sides -----------------------------------------
def test_excludes_git_and_caches(tmp_path):
    source = tmp_path / "src"
    target = tmp_path / "tgt"
    backup = tmp_path / "backup"
    _make_plugin(source, "0.2.0", extra={
        ".git/config": "[core]",
        ".pytest_cache/v/x": "cache",
        "coord/__pycache__/coord.cpython.pyc": "bytecode",
    })
    _make_plugin(target, "0.1.0", extra={".git/HEAD": "ref: refs/heads/main"})

    rc = self_update.main([
        "--source", str(source), "--target", str(target),
        "--backup-dir", str(backup),
    ])

    assert rc == 0
    # source's VCS/cache junk is never copied into target
    assert not (target / ".git/config").exists()
    assert not (target / ".pytest_cache").exists()
    assert not (target / "coord/__pycache__").exists()
    # target's own .git is never deleted by the mirror
    assert (target / ".git/HEAD").exists()
    # and it is never captured in the backup either
    assert not (backup / ".git").exists()


# --- no-op when already current -------------------------------------------------
def test_up_to_date_is_noop(tmp_path, capsys):
    source = tmp_path / "src"
    target = tmp_path / "tgt"
    _make_plugin(source, "0.2.0", extra={"docs/x.md": "same"})
    _make_plugin(target, "0.2.0", extra={"docs/x.md": "same"})

    rc = self_update.main([
        "--source", str(source), "--target", str(target),
        "--backup-dir", str(tmp_path / "backup"),
    ])

    assert rc == 0
    assert "already up to date" in capsys.readouterr().out
    assert not (tmp_path / "backup").exists()   # bailed before making a backup


# --- guards against pointing at a non-plugin tree -------------------------------
def test_non_plugin_source_errors_and_touches_nothing(tmp_path, capsys):
    source = tmp_path / "src"           # empty -> not a plugin tree
    source.mkdir()
    target = tmp_path / "tgt"
    _make_plugin(target, "0.1.0")
    before = _files(target)

    rc = self_update.main(["--source", str(source), "--target", str(target)])

    assert rc == 2
    assert _files(target) == before
    assert "not a plugin tree" in capsys.readouterr().err
