#!/usr/bin/env python3
"""
write_scope_guard.py — Copilot ``preToolUse`` hook.

Enforces per-session write scope for parallel coordinated agent sessions. Reads the hook
payload as JSON on stdin (fields: ``cwd``, ``toolName``, ``toolArgs`` — where ``toolArgs``
is *itself a JSON string* and must be parsed). Resolves the acting session from the
coordination registry by matching ``cwd`` against each session's ``worktree`` (override
with the ``COORD_SESSION`` env var), then:

  * write tools (edit/create/str_replace/write/create_file/apply_patch): **deny** if the
    target path escapes the worktree or matches none of the session's ``owned_paths`` globs.
  * ``bash``: best-effort **deny** of ``git push``, branch switches away from the session
    branch, and redirects to absolute paths outside the worktree. Otherwise allow.
  * read tools (grep/glob/view/...): always **allow**.

Emits ``{"permissionDecision":"allow"}`` or
``{"permissionDecision":"deny","permissionDecisionReason":"..."}`` on stdout and exits 0.

Real scope violations fail **closed** (deny). Anything the hook cannot evaluate — an
unparseable payload, an unresolved session, a missing path arg, or an unexpected error —
fails **open** (allow, logged to stderr) so a broken hook never wedges every tool call.

Stdlib only. Python 3.8+.
"""
from __future__ import annotations
import fnmatch
import json
import os
import re
import sys

WRITE_TOOLS = {"edit", "create", "str_replace", "write", "create_file", "apply_patch"}
PATH_KEYS = ("path", "file_path", "filePath", "filename")
ROOT_ENV = "COORD_ROOT"
DEFAULT_DIR = ".coordination"


def _log(msg: str) -> None:
    print(f"write_scope_guard: {msg}", file=sys.stderr)


def _emit(decision: str, reason=None):
    out = {"permissionDecision": decision}
    if reason:
        out["permissionDecisionReason"] = reason
    sys.stdout.write(json.dumps(out))
    sys.exit(0)


def _allow():
    _emit("allow")


def _coord_root() -> str:
    return os.path.abspath(os.environ.get(ROOT_ENV, DEFAULT_DIR))


def _registry_dir() -> str:
    return os.path.join(_coord_root(), "registry")


def _load_registry():
    entries = []
    try:
        names = os.listdir(_registry_dir())
    except OSError:
        return entries
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(_registry_dir(), n), encoding="utf-8") as f:
                entries.append(json.load(f))
        except (OSError, ValueError):
            continue
    return entries


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(p)))


def _is_within(child: str, parent: str) -> bool:
    """True if ``child`` is ``parent`` itself or nested beneath it (after normalizing)."""
    child_n, parent_n = _norm(child), _norm(parent)
    if child_n == parent_n:
        return True
    try:
        return os.path.commonpath([child_n, parent_n]) == parent_n
    except ValueError:
        return False  # e.g. different drives on Windows → not within


def _resolve_session(cwd: str):
    """Return the registry entry for the acting session, or None if it can't be resolved."""
    override = os.environ.get("COORD_SESSION")
    if override:
        path = os.path.join(_registry_dir(), f"{override}.json")
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None
    if not cwd:
        return None
    best, best_len = None, -1
    for e in _load_registry():
        wt = e.get("worktree")
        if wt and _is_within(cwd, wt):
            length = len(_norm(wt))
            if length > best_len:  # most-specific (longest) worktree wins
                best, best_len = e, length
    return best


def _owned_match(rel_posix: str, owned_paths) -> bool:
    for g in owned_paths or []:
        g = str(g).replace("\\", "/").rstrip("/")
        if not g:
            continue
        if (rel_posix == g
                or rel_posix.startswith(g + "/")
                or fnmatch.fnmatch(rel_posix, g)
                or fnmatch.fnmatch(rel_posix, g + "/*")):
            return True
    return False


def _check_write(target_path: str, cwd: str, worktree: str, owned_paths):
    target = target_path if os.path.isabs(target_path) else os.path.join(cwd, target_path)
    if not _is_within(target, worktree):
        return "deny", f"'{target_path}' resolves outside the session worktree ({worktree})"
    rel = os.path.relpath(_norm(target), _norm(worktree)).replace(os.sep, "/")
    if _owned_match(rel, owned_paths):
        return "allow", None
    return "deny", f"'{rel}' is not within this session's owned paths ({list(owned_paths)})"


def _check_bash(command: str, session_branch: str, worktree: str):
    if not command:
        return "allow", None
    if re.search(r"\bgit\s+push\b", command):
        return "deny", "git push is not permitted from a coordinated worker session"
    m = re.search(r"\bgit\s+(checkout|switch)\b(.*)", command)
    if m:
        tokens = m.group(2).split()
        branch = None
        if "--" not in tokens:  # `--` means the rest are paths, not a branch switch
            i = 0
            while i < len(tokens):
                t = tokens[i]
                if t in ("-b", "-B", "-c", "-C", "-t", "--track"):
                    branch = tokens[i + 1] if i + 1 < len(tokens) else None
                    break
                if t.startswith("-"):
                    i += 1
                    continue
                branch = t
                break
        # best-effort: only treat a bare name (no `/`, no `.`) as a branch to switch to
        if branch and branch != session_branch and "/" not in branch and not branch.startswith("."):
            return "deny", f"git {m.group(1)} to '{branch}' leaves the session branch '{session_branch}'"
    for rm in re.finditer(r">>?\s*(\"[^\"]+\"|'[^']+'|[^\s|&;<>]+)", command):
        tgt = rm.group(1).strip("\"'")
        if os.path.isabs(tgt) and not _is_within(tgt, worktree):
            return "deny", f"redirect to '{tgt}' writes outside the session worktree"
    return "allow", None


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        _log("could not parse hook payload as JSON; allowing (fail-open)")
        _allow()
    if not isinstance(payload, dict):
        _log("payload is not a JSON object; allowing (fail-open)")
        _allow()

    tool = str(payload.get("toolName") or "").strip().lower()
    cwd = payload.get("cwd") or ""

    # Read tools (and anything that is not a write tool or bash) → always allow.
    if tool not in WRITE_TOOLS and tool != "bash":
        _allow()

    args_raw = payload.get("toolArgs")
    args = {}
    if isinstance(args_raw, str):
        try:
            args = json.loads(args_raw)
        except ValueError:
            args = {}
    elif isinstance(args_raw, dict):
        args = args_raw
    if not isinstance(args, dict):
        args = {}

    session = _resolve_session(cwd)
    if not session:
        _log(f"no coordinated session resolved for cwd={cwd!r}; allowing (fail-open)")
        _allow()

    worktree = session.get("worktree") or cwd
    owned = session.get("owned_paths") or []
    branch = session.get("branch") or ""

    if tool == "bash":
        command = args.get("command") or args.get("cmd") or ""
        _emit(*_check_bash(str(command), branch, worktree))

    target = None
    for k in PATH_KEYS:
        v = args.get(k)
        if isinstance(v, str) and v:
            target = v
            break
    if not target:
        _log(f"write tool {tool!r} had no recognizable path arg; allowing (fail-open)")
        _allow()

    _emit(*_check_write(target, cwd, worktree, owned))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # never wedge tool calls on an unexpected error
        _log(f"unexpected error ({e!r}); allowing (fail-open)")
        try:
            sys.stdout.write(json.dumps({"permissionDecision": "allow"}))
        except Exception:
            pass
        sys.exit(0)
