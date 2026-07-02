# ACCEPTANCE — scenarios the build must reproduce

These scenarios **already pass** against `coord.reference.py`. The build's `tests/` must
encode them so they stay passing. Transcripts below are real runs of the reference
implementation, provided as the behavioral contract (your test assertions must match).

Run everything against a throwaway `COORD_ROOT` (e.g. a `tmp_path` fixture) — never the
repo's own `.coordination/`.

---

## 1. Dependency blocking
Claiming a task whose deps aren't `done` must fail.
```
$ coord add-task --id write-mapper --deps research-formatting
$ coord claim --session editor --task write-mapper
coord: task 'write-mapper' blocked on unmet deps: ['research-formatting']   # exit 1
```

## 2. Atomic claim (exactly one winner)
Two sessions claiming the same open task → one succeeds, the other is rejected. The claim is
guarded by a per-task lockdir, then re-checks status under the lock.
```
$ coord claim --session researcher --task research-formatting
researcher claimed research-formatting
```

## 3. Lease deny, then heartbeat-gated steal + reap
A held lease denies others. It becomes reclaimable **only** after the TTL expires **and** the
holder's heartbeat is stale. `reap` reclaims it; a live session can then re-acquire.
```
$ coord lock acquire --session worker1 --resource shared/theme.json --ttl 1
lock 'shared/theme.json' acquired by worker1 (ttl 1s)
# ... worker1 heartbeat goes stale, TTL elapses ...
$ coord reap
{ "reaped_locks": [["shared__theme.json","worker1"]], "requeued_tasks": [] }
$ coord lock acquire --session worker2 --resource shared/theme.json --ttl 60
lock 'shared/theme.json' acquired by worker2 (ttl 60s)
```
Note: lock names with `/` are flattened to `__` so they stay flat under `locks/` and remain
visible to `status`/`reap`. Your tests must cover a slashed resource name specifically — this
is where a naive reimplementation breaks.

## 4. Staleness filter (the core anti-stale-message behavior)
A message tagged with an `as_of` desired-state version older than the current version is
**skipped** by `checkpoint`; the current message is surfaced.
```
$ coord state set --session orch --key target_palette --value '"v2"'   # version -> 1
$ coord send --from orch --to editor --body "use palette v1" --as-of 1
$ coord state set --session orch --key target_palette --value '"v3"'   # version -> 2 (v1 msg now stale)
$ coord send --from orch --to editor --body "now use v3" --as-of 2
$ coord checkpoint --session editor
{ ... "messages": [ {"body":"now use v3","as_of":2, ...} ], "stale_messages_skipped": 1 }
```

## 5. Stop-flag halt
A global `STOP` (or `STOP-<session>`) makes `checkpoint` exit **3** so a wrapper/agent halts.
```
$ coord stop
$ coord checkpoint --session editor   # prints state with "stop":["GLOBAL"], exits 3
```

## 6. Write-scope hook (Phase 3 component)
Pipe a crafted `preToolUse` payload to `hooks/scripts/write_scope_guard.py`:
- write to an **owned** path → `{"permissionDecision":"allow"}`
- write to an **out-of-owned** path → `{"permissionDecision":"deny", ...}`
- a `../` traversal outside the worktree → deny
- a read tool (grep/glob/view) → allow
Payload shape (note `toolArgs` is a JSON string):
```
{"cwd":"/repo/wt","toolName":"edit","toolArgs":"{\"path\":\"src/x.py\"}"}
```
