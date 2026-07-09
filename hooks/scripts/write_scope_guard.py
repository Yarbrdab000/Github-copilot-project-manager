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
    branch, and redirects to absolute paths outside the worktree. Also denies a non-
    ``orchestrator`` role running ``coord add-task ... --verify ...`` (AUTONOMY_SPEC §4:
    acceptance gates must originate from the human-approved plan, never be self-injected
    by the code under verification), and a non-``orchestrator`` role running ``coord plan
    approve``/``coord plan reject`` (COCKPIT_SPEC §3.2/§3.4: a proposed plan becoming real
    fleet+tasks is the human-gated seam) — covers the alias and spelled-out invocation, and
    any segment of a compound (``&&``/``;``/``|``) command. Otherwise allow.
  * read tools (grep/glob/view/...): always **allow**.
  * human-prompt tools (``ask_user``): **deny** for any registered coordinated session
    (every role) and redirect to ``coord escalate`` -- such a modal blocks the session
    and the cockpit cannot clear it, stalling every dispatch queued behind it.
    Unregistered sessions fail **open**.

A ``navigator``-role session is special (NAVIGATOR_SPEC §4, extended by COCKPIT_SPEC §3.2):
all file-editing tools are denied outright, and ``bash`` is restricted to an allow-list —
``coord state propose/proposals/show``, ``coord plan propose/show/analyze/seams``, ``coord plans``,
``coord cockpit``, ``coord status``, ``coord tasks``, and read-only inspection (``git
status|log|diff|show``, ``cat``, ``ls``, ``grep``, ``find``) — with everything else
(incl. ``coord plan approve``/``coord plan reject`` and any output redirection) denied.

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

# Direct human-prompt tools: a coordinated session that opens one of these blocks on a
# modal the cockpit cannot clear, stalling every dispatch queued behind it. They are
# denied for any registered session (all roles) and redirected to the escalation channel.
PROMPT_TOOLS = {"ask_user"}
_PROMPT_DENY_REASON = (
    "coordinated sessions must not prompt the human directly -- a modal blocks this "
    "session and the cockpit cannot clear it, so every queued dispatch stalls behind it. "
    "Raise the question on the escalation channel instead: "
    "`coord escalate --session <id> --kind decision --body \"...\"`, then yield. The "
    "human answers in the cockpit and `coord resolve` delivers the decision to your next "
    "`coord checkpoint`."
)


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


def _check_bash(command: str, session_branch: str, worktree: str, role: str = ""):
    if not command:
        return "allow", None
    if role != "orchestrator":
        for seg in _SEG_SEP.split(command):
            if _segment_has_add_task_verify(seg):
                return "deny", ("only the orchestrator may attach a --verify acceptance gate; "
                                 "it must come from the approved plan")
            if _segment_has_plan_approve_or_reject(seg):
                return "deny", ("only the orchestrator may run 'coord plan approve'/'coord plan reject'; "
                                 "the human-gated plan approval is inviolate")
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


# --- navigator enforcement (NAVIGATOR_SPEC §4) -------------------------------
# "Conversation without authority" is a HARD property: a navigator-role session
# may only propose desired-state changes and read. Its bash is an ALLOW-LIST
# (deny by default); all file-editing tools are denied outright.
NAV_COORD_STATE_ALLOWED = {"propose", "proposals", "show"}
NAV_COORD_PLAN_ALLOWED = {"propose", "show", "analyze", "seams"}  # all read-only or propose-only, like show
NAV_COORD_TOP_ALLOWED = {"status", "tasks", "plans", "cockpit"}
NAV_READONLY_CMDS = {"cat", "ls", "grep", "find"}
NAV_GIT_READONLY = {"status", "log", "diff", "show"}
# shell separators that chain multiple commands; split so ANY denied segment denies
_SEG_SEP = re.compile(r"&&|\|\||;|\|")


def _strip_quoted(s: str) -> str:
    """Remove quoted spans so a quoted '>' in a proposal value isn't read as a redirect."""
    s = re.sub(r'"[^"]*"', "", s)
    s = re.sub(r"'[^']*'", "", s)
    return s


def _normalize_coord(seg: str) -> str:
    """Collapse a spelled-out `python|py|python3 <path>coord.py` invocation to the
    `coord` alias, so the allow-list can't be bypassed by not using the alias."""
    return re.sub(r"^\s*(?:python3?|py)\s+\S*coord\.py\b", "coord", seg.strip())


def _segment_has_add_task_verify(seg: str) -> bool:
    """True if this segment invokes `coord add-task` (alias or spelled-out
    `python coord/coord.py add-task`) with a `--verify` flag. Quoted spans are
    stripped first so a literal '--verify' INSIDE the task's own quoted command/desc
    string is not falsely matched, but a real --verify flag token is (AUTONOMY_SPEC §4:
    acceptance gates must come from the approved plan, never be self-injected)."""
    stripped = _strip_quoted(seg)
    norm = _normalize_coord(stripped)
    tokens = norm.split()
    if len(tokens) < 2 or tokens[0] != "coord" or tokens[1] != "add-task":
        return False
    return any(t == "--verify" or t.startswith("--verify=") for t in tokens[2:])


def _segment_has_plan_approve_or_reject(seg: str) -> bool:
    """True if this segment invokes `coord plan approve` or `coord plan reject` (alias
    or spelled-out). COCKPIT_SPEC §3.2/§3.4: a proposed plan becoming real fleet+tasks
    is the human-gated seam -- only the orchestrator role may run it."""
    stripped = _strip_quoted(seg)
    norm = _normalize_coord(stripped)
    tokens = norm.split()
    if len(tokens) < 3 or tokens[0] != "coord" or tokens[1] != "plan":
        return False
    return tokens[2] in ("approve", "reject")


def _nav_segment_allowed(seg: str) -> bool:
    seg = seg.strip()
    if not seg:
        return True  # inert (e.g. an empty split around a trailing separator)
    if ">" in _strip_quoted(seg):
        return False  # any output redirection to a file is denied, regardless of target
    seg = _normalize_coord(seg)
    tokens = seg.split()
    if not tokens:
        return True
    head = tokens[0]
    if head == "coord":
        if len(tokens) >= 2 and tokens[1] in NAV_COORD_TOP_ALLOWED:
            return True
        if len(tokens) >= 3 and tokens[1] == "state" and tokens[2] in NAV_COORD_STATE_ALLOWED:
            return True
        if len(tokens) >= 3 and tokens[1] == "plan" and tokens[2] in NAV_COORD_PLAN_ALLOWED:
            return True
        return False  # coord state set/approve/reject, plan approve/reject, claim, complete, add-task, lock, send, stop, ...
    if head == "git":
        return len(tokens) >= 2 and tokens[1] in NAV_GIT_READONLY  # push/merge/checkout/switch/commit denied
    if head in NAV_READONLY_CMDS:
        return True
    return False


def _check_navigator_bash(command: str):
    if not command or not command.strip():
        return "allow", None
    for seg in _SEG_SEP.split(command):
        if not _nav_segment_allowed(seg):
            return "deny", (
                "navigator role may only run 'coord state propose/proposals/show', "
                "'coord plan propose/show/analyze/seams', 'coord plans', 'coord cockpit', "
                "'coord status', 'coord tasks', and read-only inspection "
                "(git status|log|diff|show, cat, ls, grep, find) — denied: " + seg.strip()
            )
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

    # Direct human-prompt tools (e.g. ask_user): deny for any registered coordinated
    # session (every role) and redirect to the escalation channel; an unregistered
    # session fails open. This runs before the read-tool allow-through below, since
    # ask_user is otherwise an ordinary non-write, non-bash tool.
    if tool in PROMPT_TOOLS:
        if _resolve_session(cwd):
            _emit("deny", _PROMPT_DENY_REASON)
        _log(f"prompt tool {tool!r} from an unresolved session (cwd={cwd!r}); allowing (fail-open)")
        _allow()

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
    role = session.get("role") or ""

    # Navigator: conversation without authority. Read tools were already allowed
    # above; here we deny ALL file edits and constrain bash to a propose/read
    # allow-list. This runs BEFORE the normal worker logic and always _emit()s,
    # so navigator sessions never reach the per-worker write-scoping path.
    if role == "navigator":
        if tool in WRITE_TOOLS:
            _emit("deny", "navigator role may not edit files; its only lever is 'coord state propose'")
        command = args.get("command") or args.get("cmd") or ""
        _emit(*_check_navigator_bash(str(command)))

    if tool == "bash":
        command = args.get("command") or args.get("cmd") or ""
        _emit(*_check_bash(str(command), branch, worktree, role))

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
