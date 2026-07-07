# Control-plane protocol

The **filesystem control plane** is how genuine peer sessions coordinate when native
hub-and-spoke isn't available (see [`architecture.md`](./architecture.md)). It is implemented
by the locked [`coord.py`](../coord/coord.py) — a stdlib-only Python 3.8+ CLI. This document
specifies the protocol and documents **every `coord` command**: its signature, behavior,
output, and exit code.

> Throughout, `coord` means `python coord/coord.py` (the quickstart sets up an alias; on
> systems whose Python 3 launcher is `python3`, use that). The control plane lives at
> `COORD_ROOT` (default `.coordination/`, resolved to an absolute path).

## Protocol overview

1. **One control plane per repo.** `coord init` creates it. It is safe to run repeatedly.
2. **Every session has an identity.** `coord register` records a session's `role`, `branch`,
   `worktree`, and `owned_paths`, and starts its heartbeat.
3. **Coordinate through declarative desired state.** The orchestrator (or any session) sets
   keys on a versioned `desired.json`; workers reconcile toward it at checkpoints. This is the
   primary channel — prefer it over messages.
4. **Work is a task board.** Tasks are added with optional dependencies, atomically claimed by
   exactly one session, and completed. The board is an append-only ledger.
5. **Shared resources use leases.** A lock has a TTL and is only steal-able once the holder's
   heartbeat is stale — no deadlock from a crashed session.
6. **Messages are a fallback channel with staleness.** Direct messages carry an `as_of`
   desired-state version and/or a TTL; anything outdated is filtered out at the checkpoint.
7. **The checkpoint is the coordination beat.** `coord checkpoint` heartbeats, checks
   stop-flags, surfaces fresh messages, and returns the current desired state — plus a
   `continue` flag telling the calling session whether it still has unfinished claimed work.
8. **Acceptance is coded, not claimed.** A task can carry a `--verify` command; `coord verify`
   (by hand) or `coord tick` (automatically) runs it, and only a passing exit code marks the
   task truly accepted — repeated failure requeues, then escalates.
9. **`tick`/`run` reconcile automatically, within human authorization.** `coord tick` is one
   pure reconciliation pass (reap, verify, dispatch, nudge, budgets, surface escalations);
   `coord run` is a thin bounded loop over it. Neither ever changes `authorized_phase` or
   approves a proposal — see [`architecture.md`](./architecture.md) §8.

## Invariants

- **Append-only ledgers.** `board/tasks.jsonl`, `board/events.jsonl`, and `inbox/*.jsonl` are
  only ever appended to. Current task state is the *fold* of the task ledger.
- **Atomic mutable writes.** `desired.json`, cursors, registry entries, and lock metadata are
  written to a temp file then `os.replace`d — never edited in place.
- **Atomic claims.** A claim is guarded by a per-task `lockdir` (created with `os.mkdir`, which
  is atomic), then re-checks task status *under the lock*. Two concurrent claims → exactly one
  winner.
- **Heartbeat-gated stealing.** A lease is reclaimable only when `now - acquired > ttl` **and**
  the holder's heartbeat is older than `HEARTBEAT_STALE_SEC` (300s).
- **Never hand-edit `.coordination/`.** Always go through `coord`, or you break these
  invariants.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. |
| `1` | Error (`coord: <message>` on stderr) — e.g. unmet deps, unclaimable task, held lock. |
| `3` | `checkpoint` found a stop-flag — the session must halt cleanly. |

---

## Command reference

### `coord init`
Create the control-plane directory tree and an empty `desired.json` (version 0). Idempotent.

```
$ coord init
initialized control plane at /abs/path/.coordination
```

### `coord register --session ID --role ROLE --branch BRANCH [--worktree DIR] [--paths GLOBS]`
Register a session's identity and start its heartbeat. `--worktree` defaults to the current
directory. `--paths` is a comma-separated list of globs the session is allowed to write; it
becomes the session's `owned_paths`, which the write-scope hook enforces.

```
$ coord register --session editor --role editor --branch feat/mapper --paths "src/mapper/**,tests/mapper/**"
registered editor as editor on branch 'feat/mapper' owning ['src/mapper/**', 'tests/mapper/**']
```

Writes `registry/<session>.json` (see [`registry.schema.json`](../coord/schema/registry.schema.json)).

### `coord heartbeat --session ID`
Update the session's `heartbeat` timestamp. `checkpoint` does this for you, so you rarely call
it directly.

```
$ coord heartbeat --session editor
heartbeat editor @ 2026-01-01T00:00:00Z
```

### `coord checkpoint --session ID`
**The one command every session runs at every checkpoint boundary.** It heartbeats, checks
stop-flags, partitions the inbox into fresh vs stale, and prints the current desired state.

```
$ coord checkpoint --session editor
{
  "session": "editor",
  "time": "2026-01-01T00:00:00Z",
  "stop": [],
  "desired_version": 2,
  "desired": { "target_palette": "v3" },
  "messages": [ ... fresh messages addressed to editor ... ],
  "stale_messages_skipped": 1,
  "continue": false
}
```

Then: if `stop` is non-empty, **halt now** (the command also exits `3`); act only on
`messages`; reconcile your behavior to `desired`. Note `checkpoint` does **not** advance the
inbox cursor — use `inbox` to consume messages. See the ritual in
[`skills/coordination-protocol/SKILL.md`](../skills/coordination-protocol/SKILL.md).

`continue` is a machine-readable self-continue signal (AUTONOMY_SPEC §6): **true** when the
calling session currently holds a `claimed` task whose folded status is not yet `done` —
i.e. there is unfinished work in flight and the session should keep going without waiting to
be re-prompted. **false** once the task is `done` (or there is no claimed task at all):

```
$ coord checkpoint --session w1     # w1 holds an unfinished claimed task
{ ..., "continue": true }

$ coord complete --session w1 --task ship-thing
$ coord checkpoint --session w1     # task is now done
{ ..., "continue": false }
```

### `coord state show` / `coord state set --key KEY --value JSON [--session ID]`
Read or update the versioned declarative desired state. `set` parses `--value` as JSON when it
can (so `--value '"v3"'` stores the string `v3`, `--value '42'` stores a number), falling back
to a raw string. Each `set` **bumps `version` monotonically** and is serialized behind an
internal lock so the version stays consistent under concurrency.

```
$ coord state set --session orch --key target_palette --value '"v3"'
desired.target_palette set; state version -> 2
$ coord state show
{ "version": 2, "updated": "2026-01-01T00:00:00Z", "desired": { "target_palette": "v3" } }
```

See [`desired-state.schema.json`](../coord/schema/desired-state.schema.json).

### `coord state propose --key KEY --value JSON [--invalidates CSV] [--note TEXT] [--session ID]`
Write a **pending** proposal to amend `desired[KEY]` — a navigator's only lever on the fleet.
It records the proposed value (parsed as JSON like `set`, with any `--invalidates` task ids and
free-text `--note`) under `state/proposals/<pid>.json` and prints the proposal id, but **does
not bump the live version** — nothing propagates until a human approves.

```
$ coord state propose --session nav --key target_palette --value '"v3"' --invalidates write-mapper --note "v3 changes the mapper contract"
proposed 1783358576540003700: desired.target_palette: "v2" -> "v3" (pending; version unchanged at 1)
  invalidates: ['write-mapper']
```

### `coord state proposals`
List every **pending** proposal with its `current -> proposed` diff, origin, and any
`invalidates`/`note`. Applied and rejected proposals are omitted.

```
$ coord state proposals
  1783358576540003700  from=nav  target_palette: "v2" -> "v3"  invalidates=write-mapper  note=v3 changes the mapper contract
```

### `coord state approve --id PID [--session ID]`
Apply a pending proposal — the human-gated act that actually moves the fleet. Under the same
`__state__` lock `state set` uses, it applies the value, **bumps the version**, marks the
proposal `applied`, and for each `--invalidates` task folds it back to `open` and drops a fresh
message (`as_of` = the new version) into its current claimant's inbox. Fails (exit 1) if the
proposal doesn't exist or isn't `pending`.

```
$ coord state approve --session orch --id 1783358576540003700
approved 1783358576540003700: desired.target_palette applied; state version -> 2
  requeued: [{'task': 'write-mapper', 'notified': 'w1'}]
```

The `navigator` role **cannot** run this — the write-scope hook denies `coord state approve`
for a navigator session, so a proposal is only ever approved by a human/orchestrator.

### `coord state reject --id PID [--reason TEXT] [--session ID]`
Mark a pending proposal `rejected`, recording an optional `--reason`. **Leaves the version
unchanged** — nothing propagates. Fails (exit 1) if the proposal doesn't exist or isn't
`pending`.

```
$ coord state reject --session orch --id 1783358611618012800 --reason "staying on v3"
rejected 1783358611618012800 (version unchanged)
```

### `coord add-task --id ID [--desc TEXT] [--deps CSV] [--verify CMD] [--max-attempts N]`
Append a new open task to the board. `--deps` is a comma-separated list of task ids that must
be `done` before this task can be claimed. `--verify` attaches a coded acceptance gate — a
shell command that must exit `0` for the task to be truly accepted (see `coord verify` and
`coord tick`, below); `--max-attempts` bounds how many failing verifies are tolerated before the
task is marked `failed` and escalated (default from `desired.max_attempts_default`, else `1`).

```
$ coord add-task --id write-mapper --desc "build the field mapper" --deps research-formatting
added task write-mapper

$ coord add-task --id ship-thing --desc "ship the thing" --verify "pytest -q tests/thing" --max-attempts 2
added task ship-thing
```

A `--verify` acceptance gate may only be attached by the `orchestrator` role — the write-scope
hook denies `add-task ... --verify` for any other role, so a gate always comes from the
human-approved plan, never from the code being verified (see
[`hooks/README.md`](../hooks/README.md)).

### `coord tasks`
List the current state of every task (the fold of the ledger).

```
$ coord tasks
  [      open] research-formatting                    investigate source formatting
  [   claimed] write-mapper       <- editor deps=research-formatting  build the field mapper
```

### `coord claim --session ID --task ID`
Atomically claim an open task. Fails (exit 1) if the task doesn't exist, isn't `open`, or has
unmet dependencies. Guarded by a per-task lockdir with a re-check under the lock, so exactly
one session wins a contested claim.

```
$ coord claim --session researcher --task research-formatting
researcher claimed research-formatting

$ coord claim --session editor --task write-mapper
coord: task 'write-mapper' blocked on unmet deps: ['research-formatting']    # exit 1
```

On a successful claim it also records the current desired-state version as the task's
`claimed_at_version`, so the fleet can tell whether a task was claimed against a now-superseded
plan (see `state approve --invalidates`, which requeues such tasks).

### `coord complete --session ID --task ID [--status done|failed|open]`
Record a task outcome (default `done`). `--status open` re-opens a task for someone else.

```
$ coord complete --session researcher --task research-formatting --status done
task research-formatting -> done
```

A **stale-completion guard** protects against completing work the plan has moved past:
`complete` refuses (exit 1) unless the task is still `claimed` by the *calling* session. If the
task was requeued out from under the worker — e.g. by an approved proposal that
`--invalidates`d it — its folded status is no longer `claimed` by that session, so a worker
that kept going cannot mark stale work done; it must re-claim first.

```
$ coord complete --session w1 --task write-mapper --status done
coord: cannot complete 'write-mapper': it is 'open' (claimed_by=w1), not claimed by 'w1' — it may have been requeued/invalidated; re-claim before completing   # exit 1
```

### `coord verify --task ID [--json]`
Run a task's coded acceptance gate (its `--verify` command) in its claimant's registered
worktree, right now, on demand. A task with no `--verify` set verifies **trivially** (always
`verified: true`). On pass, appends `{verified: true}` to the ledger and exits `0`; on fail,
appends `{verified: false, attempts: <current+1>}` and exits non-zero. This is the same check
`coord tick` runs automatically for every `done` task — `verify` lets you run it by hand.

```
$ coord verify --task write-mapper
task 'write-mapper' verified (rc=0)

$ coord verify --task ship-thing
coord: task 'ship-thing' failed verify (rc=1)   # exit 1
```

### `coord lock acquire --session ID --resource NAME [--ttl SEC]` / `coord lock release ...`
Acquire or release a **lease** on a shared resource (default TTL 120s). `acquire` fails (exit
1) if the resource is held by a live holder with a valid lease. A `/` in `NAME` is flattened to
`__`. Release only succeeds for the holder.

```
$ coord lock acquire --session worker1 --resource shared/theme.json --ttl 60
lock 'shared/theme.json' acquired by worker1 (ttl 60s)

$ coord lock acquire --session worker2 --resource shared/theme.json --ttl 60
coord: lock 'shared/theme.json' is held (holder alive or lease valid)         # exit 1

$ coord lock release --session worker1 --resource shared/theme.json
lock 'shared/theme.json' released
```

A held lease is reclaimed by `reap` (below) once its TTL expires **and** the holder is stale.

### `coord send --from ID --to ID --body TEXT [--as-of VERSION] [--ttl SEC]`
Queue a direct message to another session's inbox. `--as-of` ties the message to a
desired-state version so it auto-goes-stale once the world moves past it; `--ttl` sets a
wall-clock expiry. Prefer changing `desired` state over messaging.

```
$ coord send --from orch --to editor --body "now use v3" --as-of 2
queued message 1735689600000000000 -> editor
```

See [`message.schema.json`](../coord/schema/message.schema.json).

### `coord inbox --session ID`
Show fresh messages for a session and **advance its cursor** past everything surfaced (fresh +
stale), so they aren't shown again. Reports a count of stale messages skipped.

```
$ coord inbox --session editor
{ "fresh": [ {"body": "now use v3", "as_of": 2, ... } ], "stale_skipped": 1 }
```

A message is **stale** if its TTL has expired **or** its `as_of` is older than the current
desired-state version. This is the core anti-stale-message behavior.

### `coord stop [--session ID]` / `coord resume [--session ID]`
Write or clear a halt flag. With `--session`, targets `STOP-<session>` (only that session
halts); without, targets the global `STOP` (every session halts at its next checkpoint).

```
$ coord stop
wrote STOP
$ coord resume
cleared STOP
```

A session sees the flag at its next `checkpoint`, which exits `3`.

### `coord status`
Print a human-readable snapshot: the control-plane root, active stop-flags, every registered
session (ALIVE/STALE by heartbeat age), every held lock, and the task board.

```
$ coord status
control plane: /abs/path/.coordination
stop flags: none
sessions:
  editor           editor         ALIVE  hb 3s ago  branch=feat/mapper
locks:
  shared__theme.json       holder=worker1 age=5s
tasks:
  [      open] research-formatting ...
```

### `coord reap`
Orchestrator hygiene: release leases held by dead sessions (TTL expired **and** heartbeat
stale) and requeue tasks claimed by dead sessions, so a crashed worker never wedges the fleet.
Prints what it reclaimed.

```
$ coord reap
{
  "reaped_locks": [ ["shared__theme.json", "worker1"] ],
  "requeued_tasks": [ ["write-mapper", "editor"] ]
}
```

### `coord escalate --session ID --kind decision|blocker|fork --body TEXT [--task ID]`
Raise a human-facing escalation — the seam a stuck worker or an automated `tick` pass uses to
stop and ask, instead of guessing. Records the current desired-state version as `as_of` and
writes `state/escalations/<eid>.json` with `status: "open"`.

```
$ coord escalate --session w1 --kind decision --body "which palette should v4 target?"
escalated 1783452715807409000: [decision] from=w1 task=None - which palette should v4 target?
```

### `coord escalations [--json]`
List every **open** escalation, most-recent first.

```
$ coord escalations
  [decision] 1783452715807409000 from=w1 task=None  which palette should v4 target?
```

### `coord resolve --id EID [--note TEXT]`
Close an escalation: sets `status: "resolved"` and records an optional `--note`. It then drops
off the `escalations` list. Fails (exit 1) on an unknown id.

```
$ coord resolve --id 1783452715807409000 --note "picked v3, staying put"
resolved 1783452715807409000

$ coord escalations
(no open escalations)
```

### `coord tick [--json]`
**The keystone reconciliation pass** (AUTONOMY_SPEC §3.3): one deterministic sweep that reaps
dead sessions, runs coded acceptance gates on `done` tasks, requeues-or-escalates on repeated
verify failure, advisory-dispatches ready work to idle workers, advisory-nudges a claimant whose
heartbeat is aging, enforces budgets, and surfaces open escalations. Always prints JSON and
exits `0`. See [`architecture.md`](./architecture.md) §8 for the full step order and the hard
invariant it upholds (never touches `authorized_phase`, never approves/rejects a proposal,
never performs a git operation).

```
$ coord tick
{
  "reaped": [],
  "verified": [],
  "requeued": [],
  "dispatched": [],
  "nudged": [],
  "failed": [
    { "task": "ship-thing", "attempts": 1, "escalation": "1783452744369564200" }
  ],
  "awaiting_decision": [
    { "eid": "1783452744369564200", "from": "tick", "kind": "blocker", "task": "ship-thing", ... }
  ]
}
```

### `coord run [--interval SEC] [--max-ticks N] [--once]`
The thin, bounded loop wrapper around `tick` (AUTONOMY_SPEC §3.4) — **all** reconciliation logic
lives in `tick`; `run` only sleeps `--interval` seconds between passes and counts them. Stops
after `--max-ticks` passes (`--once` is `--max-ticks 1`), or immediately once a fleet-wide
`STOP` is set — before starting another pass. Prints one JSON report per pass, in the same shape
as `coord tick`.

```
$ coord run --once --interval 0
{ "reaped": [], "verified": [], "requeued": [], "dispatched": [], "nudged": [], "failed": [], "awaiting_decision": [] }
```

---

## The checkpoint ritual

Every coordinated session runs this at each checkpoint boundary — before starting a unit of
work, after finishing one, before ending the session, and periodically during long work:

1. Run `coord checkpoint --session $ID`.
2. If `stop` is non-empty (or the command exits `3`), **halt cleanly**. Don't start new work.
3. Act **only** on `messages`. Anything in `stale_messages_skipped` was intentionally dropped.
4. Reconcile your behavior to `desired` / `desired_version`. If it changed, adjust.

This ritual is stated verbatim in
[`skills/coordination-protocol/SKILL.md`](../skills/coordination-protocol/SKILL.md), which
every agent loads.
