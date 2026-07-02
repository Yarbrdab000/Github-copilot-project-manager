# Architecture

This document expands the design in [`SPEC.md`](../SPEC.md) §2–3. It explains the two
coordination modes, when to use each, the design principles the control plane enforces, and
how each classic multi-agent failure mode is mechanically prevented.

## 1. The problem

Running several GitHub Copilot agent sessions against **one repository** at the same time is
attractive — more work in parallel — but naively it breaks in predictable ways: sessions
wander off task, lose the plan, overwrite each other's branches, fail to coordinate, and act
on stale instructions. This repo is a drop-in layer that removes those failure modes.

The guiding idea: **invariants live in tooling, not in prose.** Instructions *teach* the
protocol; the `coord` CLI, the hooks, and git *enforce* it. A model under context pressure can
skip a paragraph; it cannot skip a `preToolUse` hook that denies an out-of-scope write.

## 2. Two coordination modes

### Native mode (preferred): hub-and-spoke

One **orchestrator** agent decomposes the work and delegates scoped slices to **worker**
sub-agents. Each worker runs in an isolated context and reports back through Copilot's native
sub-agent lifecycle. Coordination is **vertical** (parent ↔ child), so there is no lateral
peer-messaging problem to solve at all — the parent holds the plan and the exit criteria, and
children start fresh with only their slice.

```mermaid
flowchart TD
    O[Orchestrator agent<br/>holds the durable plan] -->|delegates scoped task| R[Researcher<br/>read-only]
    O -->|delegates scoped task| E1[Editor<br/>write-scoped to path A]
    O -->|delegates scoped task| E2[Editor<br/>write-scoped to path B]
    R -.->|reports findings| O
    E1 -.->|reports result| O
    E2 -.->|reports result| O
```

Prefer this mode. It is simpler and has fewer moving parts because the runtime already gives
you preemption (the parent controls the children) and delivery guarantees (lifecycle events).

### Fallback mode: filesystem control plane

Some work is genuinely done by **long-running peer sessions** that are *not* in one
parent/child tree — e.g. two separate Copilot windows a human opened, each a top-level agent.
Peers have **no preemption** (you cannot interrupt another top-level session) and **no
delivery guarantees** (there is no message bus). For that case, sessions coordinate through a
shared on-disk **control plane** under `.coordination/`, driven by the `coord` CLI, which they
read at defined **checkpoints**.

```mermaid
flowchart LR
    subgraph FS[".coordination/ (shared on disk)"]
        DS[state/desired.json<br/>versioned desired state]
        BOARD[board/tasks.jsonl<br/>append-only task ledger]
        INBOX[inbox/&lt;session&gt;.jsonl<br/>per-recipient messages]
        LOCKS[locks/*.lockdir<br/>TTL leases]
        REG[registry/&lt;session&gt;.json<br/>identity + heartbeat]
    end
    S1[Peer session A] -->|coord checkpoint| FS
    S2[Peer session B] -->|coord checkpoint| FS
    FS --> S1
    FS --> S2
```

This is the mode the locked [`coord.py`](../coord/coord.py) implements. It is a *fallback*
because it re-creates, on the filesystem, the preemption and delivery properties that
hub-and-spoke gets for free.

### Choosing a mode

| Situation | Mode |
|---|---|
| One driver decomposing work into scoped slices | **Native** hub-and-spoke (orchestrator + workers) |
| Sub-agents that live and die inside one parent run | **Native** |
| Two+ independent, long-lived sessions a human started separately | **Fallback** control plane |
| Peers that must survive across many turns and re-sync periodically | **Fallback** |

When in doubt, start native. Reach for the control plane only when you have true peers.

## 3. Design principles (do not violate)

These come from `SPEC.md` §2 and are baked into `coord.py`:

1. **Declarative state over imperative messages.** The primary channel is a versioned
   `desired.json` that sessions *reconcile toward* at checkpoints — not commands dropped in a
   queue. "Current target is X" read fresh never goes stale the way a queued "do X" does.
2. **Deterministic core, probabilistic shell.** Coordination invariants live in the CLI,
   hooks, and git — never in prose the model can skip. Instructions teach; tools enforce.
3. **Append-only ledgers.** The task board and inboxes are JSONL **appends**, never
   read-modify-write on a shared file, so concurrent writers can't clobber each other. Mutable
   state (`desired.json`, cursors, lock metadata) is written atomically via temp file +
   `os.replace`.
4. **Leases, not locks.** Every lock has a TTL and is steal-able **only** when the lease has
   expired **and** the holder's heartbeat is provably stale. A crashed session cannot deadlock
   the fleet.
5. **Small units + frequent checkpoints.** Because sessions can't be preempted, work is broken
   into small units with a `coord checkpoint` beat between each, which is where a session
   picks up stop-flags, fresh messages, and desired-state changes.

## 4. The five failure modes → the mechanism that fixes each

| Failure mode | Fix (in this repo) |
|---|---|
| Agents don't stop / wander off task | Scoped tools per agent (read-only workers can't edit) + `preToolUse` write-scope hook + orchestrator holds exit criteria; `stop`/`STOP` flags halt a session at its next checkpoint (exit 3). |
| Lose context | Orchestrator holds the durable plan; workers start fresh with only their slice; per-agent skills preloaded at startup. |
| Overwrite each other's branches | Worktree-per-worker (native) + write-scoped tools + `preToolUse` hook rejecting writes outside a session's `owned_paths`. |
| Don't coordinate | Hub-and-spoke via the orchestrator + native lifecycle events; no lateral messaging to get wrong. |
| Append-only queue / stale messages | Native `steering` for live redirect; fallback: per-recipient inboxes with `as_of` + TTL **staleness filtering**, surfaced only at checkpoints. |

## 5. Control-plane layout

`coord init` creates this tree under `COORD_ROOT` (default `.coordination/`):

```
.coordination/
  registry/<session>.json          # identity: role, branch, worktree, owned_paths, heartbeat
  inbox/<session>.jsonl            # append-only per-recipient messages
  cursor/<session>.json            # how far this session has consumed its inbox
  locks/<name>.lockdir/meta.json   # a lease: holder, acquired, ttl (dir = atomic mkdir)
  state/desired.json               # versioned declarative desired state
  board/tasks.jsonl                # append-only task ledger (event-sourced)
  board/events.jsonl               # append-only audit log of coordination events
  control/STOP, control/STOP-<session>   # halt flags
  log/                             # reserved
```

Key encodings:

- **Lock names with `/` are flattened to `__`** (`shared/theme.json` → `shared__theme.json`)
  so every lockdir lives directly under `locks/` and stays visible to `status`/`reap`. This
  also blocks path traversal in a resource name.
- **Tasks are event-sourced.** `board/tasks.jsonl` is a log of task events; the current state
  of a task is the fold of its events. Claims and completions are appends, never rewrites.
- **Mutable files are atomic.** `desired.json`, cursors, registry entries, and lock metadata
  are written to a temp file and `os.replace`d into place (atomic on POSIX and Windows).

See [`protocol.md`](./protocol.md) for the full command surface and the exact on-disk shapes,
and the JSON Schemas under [`../coord/schema/`](../coord/schema/) for the validated record
formats.

## 6. Failure semantics

- **A dead session** (no heartbeat for `HEARTBEAT_STALE_SEC` = 300s) is treated as gone. Its
  leases become steal-able once their TTL also expires, and `coord reap` requeues its claimed
  tasks so the fleet doesn't wedge.
- **A broken hook fails open.** If the `preToolUse` guard can't parse its payload or resolve
  the acting session, it *allows* the tool and logs to stderr — a bug in the guard must never
  block every tool call. It fails **closed** only on a genuine, well-formed scope violation.
- **A stale message is dropped, not delivered.** `checkpoint` and `inbox` skip messages whose
  TTL has expired or whose `as_of` is older than the current desired-state version, and report
  only a count of what was skipped.
