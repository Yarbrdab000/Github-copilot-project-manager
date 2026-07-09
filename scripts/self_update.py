#!/usr/bin/env python3
"""Offline, stdlib-only self-update helper for the agent-coordination-skills plugin.

This script performs the *filesystem* half of a self-update. Given a ``--source``
tree (a freshly fetched copy of the plugin at the desired version -- e.g. a shallow
clone of ``main``) and the ``--target`` install directory, it backs the target up,
mirrors the source over it, and verifies the result still runs.

It performs **no network I/O** and imports nothing outside the standard library.
The authenticated fetch of ``main`` is done by the caller (see
``skills/self-update/SKILL.md``) -- keeping this helper deterministic, offline, and
testable, in line with the repo's "no network in scripts or tests" guardrail.

Usage::

  python self_update.py --source <fetched-tree> --target <install-dir> [--dry-run]
                        [--backup-dir <dir>] [--no-backup] [--no-verify]

Exit codes: ``0`` on success (or dry-run); non-zero on a validation error or a
failed post-update verify (on a failed verify the target is auto-restored from the
backup so the install is never left broken).
"""
from __future__ import annotations

import argparse
import filecmp
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

MANIFEST = Path(".claude-plugin") / "plugin.json"
# VCS / build / runtime state that an update never copies, compares, or deletes.
EXCLUDE = {".git", ".pytest_cache", "__pycache__", ".coordination"}


def _excluded(rel: Path) -> bool:
    return any(part in EXCLUDE for part in rel.parts)


def _iter_files(root: Path):
    for p in root.rglob("*"):
        if p.is_file():
            rel = p.relative_to(root)
            if not _excluded(rel):
                yield rel


def _read_version(tree: Path) -> str:
    try:
        data = json.loads((tree / MANIFEST).read_text(encoding="utf-8"))
        return str(data.get("version", "?"))
    except Exception:
        return "?"


def _is_plugin_tree(tree: Path) -> bool:
    return (tree / MANIFEST).is_file() and (tree / "coord" / "coord.py").is_file()


def _plan(source: Path, target: Path):
    src = set(_iter_files(source))
    tgt = set(_iter_files(target))
    adds = sorted(src - tgt)
    removes = sorted(tgt - src)
    updates = sorted(
        rel for rel in (src & tgt)
        if not filecmp.cmp(source / rel, target / rel, shallow=False)
    )
    return adds, updates, removes


def _backup(target: Path, backup_dir: Path) -> None:
    for rel in _iter_files(target):
        dst = backup_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target / rel, dst)


def _apply(source: Path, target: Path, adds, updates, removes) -> None:
    for rel in list(adds) + list(updates):
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel, dst)
    for rel in removes:
        try:
            (target / rel).unlink()
        except FileNotFoundError:
            pass
    # Best-effort prune of directories left empty by removals.
    dirs = sorted((p for p in target.rglob("*") if p.is_dir()), reverse=True)
    for d in dirs:
        if _excluded(d.relative_to(target)):
            continue
        try:
            if not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass


def _restore(backup_dir: Path, target: Path) -> None:
    for rel in list(_iter_files(target)):
        try:
            (target / rel).unlink()
        except FileNotFoundError:
            pass
    for rel in _iter_files(backup_dir):
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup_dir / rel, dst)


def _verify(target: Path):
    coord = target / "coord" / "coord.py"
    try:
        r = subprocess.run(
            [sys.executable, str(coord), "--help"],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"could not run coord --help: {exc}"
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        return False, f"coord --help exited {r.returncode}"
    if "coord" not in out.lower():
        return False, "coord --help output did not look like coord"
    return True, "ok"


def _print_summary(header, from_v, to_v, adds, updates, removes) -> None:
    print(header)
    print(f"  version: {from_v} -> {to_v}")
    print(f"  add:    {len(adds)}")
    print(f"  update: {len(updates)}")
    print(f"  remove: {len(removes)}")
    for label, items in (("add", adds), ("update", updates), ("remove", removes)):
        for rel in items:
            print(f"    [{label}] {rel.as_posix()}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Offline self-update helper for agent-coordination-skills."
    )
    ap.add_argument("--source", required=True, type=Path,
                    help="fetched plugin tree at the target version")
    ap.add_argument("--target", required=True, type=Path,
                    help="installed plugin dir to update in place")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the planned changes and write nothing")
    ap.add_argument("--backup-dir", type=Path, default=None,
                    help="where to write the pre-update backup (default: a temp dir)")
    ap.add_argument("--no-backup", action="store_true",
                    help="skip the backup (not recommended)")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the post-update 'coord --help' check")
    a = ap.parse_args(argv)

    source, target = a.source.resolve(), a.target.resolve()
    if not _is_plugin_tree(source):
        print(f"error: --source {source} is not a plugin tree "
              f"(missing {MANIFEST.as_posix()} or coord/coord.py)", file=sys.stderr)
        return 2
    if not _is_plugin_tree(target):
        print(f"error: --target {target} is not a plugin tree "
              f"(missing {MANIFEST.as_posix()} or coord/coord.py)", file=sys.stderr)
        return 2

    from_v, to_v = _read_version(target), _read_version(source)
    adds, updates, removes = _plan(source, target)

    if a.dry_run:
        _print_summary("DRY RUN -- no changes written:", from_v, to_v, adds, updates, removes)
        return 0

    if not (adds or updates or removes):
        print(f"already up to date (version {from_v}); nothing to do.")
        return 0

    backup_dir = None
    if not a.no_backup:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        default_root = Path(tempfile.gettempdir()) / "agent-coordination-skills-backups"
        backup_dir = (a.backup_dir or default_root / f"{target.name}-{ts}").resolve()
        backup_dir.mkdir(parents=True, exist_ok=True)
        _backup(target, backup_dir)

    _apply(source, target, adds, updates, removes)

    if not a.no_verify:
        ok, msg = _verify(target)
        if not ok:
            if backup_dir is not None:
                _restore(backup_dir, target)
                print(f"verify failed ({msg}); target restored from backup at {backup_dir}",
                      file=sys.stderr)
            else:
                print(f"verify failed ({msg}); no backup to restore (ran with --no-backup)",
                      file=sys.stderr)
            return 1

    _print_summary("UPDATED:", from_v, to_v, adds, updates, removes)
    if backup_dir is not None:
        print(f"  backup: {backup_dir}")
    print("  restart your session so the reloaded plugin + manifest take effect.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
