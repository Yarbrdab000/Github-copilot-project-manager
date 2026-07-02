---
name: coordination-protocol
description: "Shared control-plane protocol every coordinated Copilot session loads: session identity, the checkpoint ritual, declarative desired-state reconciliation, atomic task claims, and lease locks."
---

# Coordination protocol

You are one of several Copilot sessions working the same repository in parallel. You
coordinate through a filesystem **control plane** driven by the `coord` CLI — not by talking
to the other sessions directly. Invoke it as `coord` (an alias for `python coord/coord.py`;
the quickstart sets this up).

Read this skill before doing anything else, then load your role skill (`worker` or
`orchestrator`).

## Core principles

- **Coordinate through declarative shared state**, reconciled at checkpoints — never through
  imperative messages dropped in a queue that goes stale.
- **Ledgers are append-only** and mutable state is written atomically. Never hand-edit files
  under `.coordination/`; go through `coord`.
- **You have an identity**: a session id (`$ID`), a role, a branch, and a set of **owned
  paths**. You may only write inside your owned paths — a `preToolUse` hook enforces this, so
  don't try to work around it.
- **Everyone heartbeats.** A session that goes silent is treated as dead and its work becomes
  reclaimable. Running `coord checkpoint` heartbeats for you.

## The checkpoint ritual (run this at every checkpoint boundary)

At every checkpoint boundary run `coord checkpoint --session $ID`; if `stop` is non-empty,
halt; act only on returned `messages`; reconcile behavior to returned `desired`.

Concretely, `coord checkpoint --session $ID` prints this shape:

```json
{
  "session": "$ID",
  "time": "2026-01-01T00:00:00Z",
  "stop": [],
  "desired_version": 0,
  "desired": {},
  "messages": [],
  "stale_messages_skipped": 0
}
```

Then:

1. **`stop`** — if this list is non-empty, **halt cleanly now**. (The command also exits with
   status `3` so a wrapper can enforce the halt.) Do not start new work.
2. **`messages`** — act only on these. They are the fresh, non-stale messages addressed to
   you. Anything counted in `stale_messages_skipped` was intentionally dropped (TTL-expired or
   written against an older `desired_version`) — do not go hunting for it.
3. **`desired` / `desired_version`** — reconcile your behavior to this declared desired state.
   If it changed since you last looked, adjust what you are doing to match it.

**When to checkpoint:** before you start a unit of work, after you finish one, before you
finish the session, and periodically during any long-running work.

## Working with tasks

- `coord tasks` — list the task board.
- `coord claim --session $ID --task <id>` — atomically claim a task. Fails if it is already
  claimed or has unmet dependencies; exactly one session can win a claim.
- `coord complete --session $ID --task <id> --status done|failed` — record the outcome.

## Shared resources: lease locks

For anything only one session may touch at a time (a shared config file, a migration step):

- `coord lock acquire --session $ID --resource <name> --ttl <sec>`
- `coord lock release --session $ID --resource <name>`

Locks are **leases**: they expire after their TTL and are only stealable once the holder's
heartbeat is provably stale, so a crashed session never deadlocks the fleet.

## Messaging (use sparingly)

Prefer changing `desired` state over sending messages. When you must message directly:

- `coord send --from $ID --to <session> --body "<text>" --as-of <desired_version> --ttl <sec>`
- Passing `--as-of` ties the message to a desired-state version so it auto-goes-stale once the
  world moves on — that is exactly why `checkpoint` can safely skip outdated messages.
