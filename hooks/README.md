# Coordination hooks

These hooks make the coordination protocol **enforced by the platform**, not just described
in prose the model can skip. They are wired by [`.github/hooks/coordination.json`](../.github/hooks/coordination.json)
(`version: 1`), which Copilot loads automatically for the repo.

| Event | Script | What it does |
| --- | --- | --- |
| `sessionStart` | [`scripts/session_register.sh`](scripts/session_register.sh) | Registers the session in the control plane (`coord register`) and writes a first heartbeat. |
| `preToolUse` | [`scripts/write_scope_guard.py`](scripts/write_scope_guard.py) | Denies any file write outside the session's owned paths, plus `git push` / off-branch checkouts / stray redirects — and denies direct human-prompt tools (`ask_user`), redirecting the session to `coord escalate`. |

## How the write-scope guard works

Copilot delivers each hook its payload as **JSON on stdin**. For tool hooks that includes
`toolName` and `toolArgs`, where **`toolArgs` is itself a JSON string** and must be parsed
before you can read fields like `path` or `command`.

The guard:

1. Resolves the acting session by matching the payload `cwd` against each registry entry's
   `worktree` (or uses `COORD_SESSION` if set), and reads that session's `owned_paths`.
2. For write tools (`edit`, `create`, `str_replace`, `write`, `create_file`, `apply_patch`)
   it extracts the target path (`path` / `file_path` / `filePath` / `filename`), resolves it
   against `cwd`, and **denies** the call if it escapes the worktree or matches none of the
   `owned_paths` globs.
3. For `bash` it best-effort **denies** `git push`, a checkout/switch to a branch other than
   the session's, and redirects to absolute paths outside the worktree.
4. For direct human-prompt tools (`ask_user`) it **denies** the call for any resolved session
   (every role) and points it at `coord escalate` — a blocking modal would stall the whole
   fleet, and the cockpit the human watches cannot clear it.
5. It **allows** every read tool.

It replies with `{"permissionDecision":"allow"}` or
`{"permissionDecision":"deny","permissionDecisionReason":"..."}` and exits 0.

**Fail-open by design.** Real scope violations fail *closed* (deny), but anything the guard
cannot evaluate — an unparseable payload, an unresolved session, a missing path, an
unexpected error — fails *open* (allow, logged to stderr) so a broken hook never wedges every
tool call. This matches Copilot's own timeout = fail-open stance.

**Honest seam on the prompt rule.** The guard denies `ask_user` whenever Copilot routes it
through `preToolUse`. If a runtime treats `ask_user` as an internal interaction that does *not*
fire a tool hook, the guard never sees it — so the load-bearing guarantee is the **discipline**
(every role's skill says "never prompt directly; `coord escalate` and yield") plus the
**round-trip** (`coord resolve` delivers the human's answer back as a checkpoint message). The
hook is defense-in-depth on top of those two.

## Install / enable

The hooks live in the repo, so cloning it is enough for Copilot to pick them up. To make a
session *coordinated*, export its identity before launching Copilot so `session_register.sh`
runs meaningfully:

```bash
export COORD_ROOT="$PWD/.coordination"   # shared control-plane location
export COORD_SESSION=editor              # this session's id
export COORD_ROLE=editor
export COORD_PATHS='src/**,tests/**'     # globs this session may write
```

If `COORD_SESSION` is unset the `sessionStart` hook is a no-op and the guard fails open, so
the hooks are inert for non-coordinated sessions.

**Requirements.** The guard is Python 3.8+ (stdlib only), invoked as `python3` on
POSIX and `python` on Windows. `session_register.sh` is Bash; on Windows it runs under Git
Bash (bundled with Git for Windows). If Bash is unavailable you can register a session by
hand with `python coord/coord.py register --session ... --role ... --branch ... --paths ...`.
