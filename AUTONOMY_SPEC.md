# AUTONOMY_SPEC — addendum to `agent-coordination-skills`

**This extends the already-built repo (post-Navigator).** Do not rebuild existing files. Add
the new commands, data areas, and role edits described here as *surgical* additions to
`coord/coord.py`, `hooks/scripts/write_scope_guard.py`, and the worker skill/agent. All
existing tests must still pass afterward. The locked `reference/coord.reference.py` stays
untouched.

> **Prerequisite:** builds on the Navigator addendum (the `state propose/approve` seam and the
> stale-completion guard). Land the Navigator PR to `main` first and branch from it, **or**
> branch `feat/autonomy` off `feat/navigator`. Do not target `main` directly.

## 1. Why this addendum exists

The system today is **reconcilable but not self-driving.** A human still supplies the *tick*:
someone bumps `authorized_phase`, notices an idle worker and nudges it, re-runs the tests to
accept a task, and pushes/opens the PR. That makes the orchestrator a person, and the fleet a
set of sessions a person messages — the exact "glorified messaging" trap.

This addendum automates the **mechanics** and leaves the human only the **judgment**:

- The human talks to the **navigator** (design) and answers **decisions**.
- Everything mechanical — dispatch, liveness, acceptance, retry, integration prep — runs in a
  loop.
- The loop **never** self-advances a human gate: it does not bump `authorized_phase`, does not
  approve proposals, does not merge to `main`. Those remain human-gated (that invariant is the
  point; do not weaken it).

Guiding principle: **automate the mechanics; keep the human on judgment.**

### The honest runtime seam (read this before designing)

A filesystem control plane can *record* an intention ("dispatch task X to worker w", "continue
your task"), but **waking a fully-idle agent session is a runtime capability, not a filesystem
one.** So this addendum splits cleanly:

- **`coord tick` is pure control logic**: it folds the ledgers and emits *directives* + effects
  (requeue, verify, escalate, dispatch-message). It is deterministic and fully unit-testable.
- **Delivery of a wake/continue directive to an OS-level idle session is a thin runtime
  adapter** outside `coord` (the `run` wrapper, or whatever host can re-prompt a session). The
  spec defines the directive shape so any host can implement delivery; it does **not** pretend
  the filesystem can wake a process.

To minimize reliance on external waking, the **worker becomes self-continuing** (§6): a *live*
worker never yields mid-task, so the common case needs no wake at all.

## 2. New data areas (create dirs in `cmd_init`)

Reuse existing helpers: `now()`, `iso()`, `_atomic_write`, `_append`, `_read_json`,
`_fold_tasks()`, `_heartbeat_stale()`, `HEARTBEAT_STALE_SEC`, and the `cmd_reap` logic.

- `.coordination/escalations/<eid>.json` — a decision/blocker raised to the human/navigator:
  ```json
  { "eid": "<time_ns>", "from": "<session>", "kind": "decision|blocker|fork",
    "task": "<task id | null>", "body": "...", "status": "open|resolved",
    "created": "<iso>", "as_of": <version>, "resolved_note": null }
  ```
- Extend the **task add-task event** (fold in `_fold_tasks`) with optional fields:
  - `verify`: a shell command string — the machine-checkable acceptance gate.
  - `max_attempts`: int (default from desired-state budget; see §5).
  And fold two counters from new task events: `attempts` (int, incremented on a failed verify)
  and `verified` (bool, set true by a passing verify). Update `coord/schema/task.schema.json`
  additively so the schema test stays green.

## 3. New `coord` commands

### 3.1 Coded acceptance gates
- `add-task ... [--verify "CMD"] [--max-attempts N]` — extend the existing parser; persist
  `verify`/`max_attempts` on the add-task event.
- `verify --task <id> [--json]` — resolve the task's current claimant (or last claimant) and
  its registry `worktree`; run `verify` there with `subprocess` (inherit env; capture rc). On
  rc==0 append a `verified` event (status stays `done`); on rc!=0 append a `verify_failed`
  event (increments `attempts`) and exit non-zero. No `verify` set ⇒ treat as trivially passing
  (record `verified`). This is the seam that lets acceptance need no human.

### 3.2 Escalations (the human/navigator interface)
- `escalate --session <id> --kind decision|blocker|fork --body "..." [--task <id>]` — write an
  `open` escalation with `as_of` = current desired version.
- `escalations [--json]` — list `open` escalations.
- `resolve --id <eid> [--note "..."]` — mark an escalation `resolved`. (A human/navigator
  action; a decision is typically resolved by an approved `state propose`, then `resolve`.)

### 3.3 The keystone: `tick`
- `tick [--json]` — perform **one** deterministic pass and print a JSON effects report. In
  order, each step atomic / under the same lock discipline the touched command already uses:
  1. **Reap** dead sessions (reuse `cmd_reap`): requeue their `claimed` tasks, release their
     expired locks held while stale.
  2. **Verify** acceptance: for every task whose folded status is `done`, has a `verify`, and
     has no `verified` event yet, run §3.1 `verify`.
     - pass ⇒ leave `done`, now `verified`.
     - fail & `attempts < max_attempts` ⇒ requeue (append `{status:"open", claimed_by:null}`)
       and drop a message to the last claimant (`as_of` = current version) naming the failure.
     - fail & `attempts >= max_attempts` ⇒ append `{status:"failed"}` and open a `blocker`
       escalation.
  3. **Dispatch** (advisory): for each `ready` task (open, all deps `done`) with no claimant,
     pick an idle registered worker whose `owned_paths` match the task and record a `dispatch`
     directive — a message to that worker (`as_of` = current version) telling it to claim the
     task. (Delivery/wake is the runtime adapter's job; see §1.)
  4. **Stall nudge** (advisory): a worker holding a `claimed` task whose heartbeat is aging (but
     not yet reap-stale) gets a `continue` directive message. (Same delivery caveat.)
  5. **Budgets:** enforce §5 — tasks past `max_attempts` or a deadline ⇒ `failed` + escalate;
     if `max_parallel` or a global time budget is exceeded, `cmd_stop` the fleet.
  6. **Surface:** include any `open` escalations as `awaiting_decision` in the report.
  - **Invariant (assert in tests):** `tick` MUST NOT change `authorized_phase`, approve/reject a
    proposal, or perform a git write. It reconciles *within* the current human authorization.
  - Report shape: `{ "reaped": [...], "verified": [...], "requeued": [...], "dispatched": [...],
    "nudged": [...], "failed": [...], "awaiting_decision": [...] }`.

### 3.4 The loop wrapper (kept minimal, the only non-pure command)
- `run [--interval SEC] [--max-ticks N] [--once]` — call `tick` in a loop, sleeping `interval`
  between passes, stopping after `max_ticks` (or on a global `stop`). `--max-ticks`/`--once`
  make it bounded and testable. Keep this thin: all logic lives in `tick`.

## 4. Hook hardening (trust for unattended code-exec)

`verify` runs arbitrary commands under the orchestrator. Therefore, in
`hooks/scripts/write_scope_guard.py`, keep the existing role rules and add: a **worker** (editor
role) may not create/alter a task's `verify` field — deny a `bash` `coord add-task ... --verify`
(and the spelled-out `python coord/coord.py add-task ... --verify`) for a non-orchestrator role.
Rationale: acceptance gates must originate from the human-approved plan (orchestrator/navigator),
never be injected by the code being verified. Keep the fail-open-on-parse stance and leave all
existing worker/navigator behavior unchanged otherwise.

## 5. Budgets & rails (desired-state keys)

The orchestrator reads these from `desired.json` (set by the human/plan; defaults if absent):
- `max_parallel` — max concurrently-`claimed` tasks `tick` will dispatch toward.
- `max_attempts_default` — default per-task retry cap.
- `time_budget_sec` / task `deadline` — wall-clock rails; breach ⇒ `failed` + escalate, and a
  global breach ⇒ `stop`.
`tick` enforces these; they are the difference between "autonomous" and "thrashing unsupervised".

## 6. Worker self-continue (skill/agent edit)

Attack the real-world friction directly: today a live worker often *yields a turn before
finishing a phase*. Edit `skills/worker/SKILL.md` (and note it in `agents/editor.agent.md`) so
the worker loop is: after each `checkpoint`, **if you hold a `claimed` task that is not `done`
and you are not blocked, immediately continue the next unit — do not yield.** Yield only when the
task is `done`, when `stop` is set, or when you must `escalate` (raise a `blocker`/`fork` and
stop). Additionally, `cmd_checkpoint`'s output gains a `continue` boolean: true when the calling
session holds an unfinished `claimed` task — a machine-readable "keep going" signal a host can
also act on.

## 7. Acceptance criteria (new tests; existing suite stays green)

All tests offline, stdlib-only, deterministic. Use `verify` commands like
`python -c "import sys; sys.exit(0)"` / `sys.exit(1)` to simulate pass/fail.

1. `tick` reaps a dead (heartbeat-stale) session's `claimed` task → task folds back to `open`.
2. `tick` runs a passing `verify` on a `done` task → task stays `done` and gains a `verified`
   event.
3. `tick` runs a failing `verify` → task requeued to `open`, `attempts` incremented, and a
   message lands in the prior claimant's inbox.
4. After `max_attempts` failing verifies, `tick` marks the task `failed` **and** opens a
   `blocker` escalation.
5. `escalate` → `escalations` lists it `open` → `resolve` marks it `resolved` (and it drops off
   the open list).
6. `tick` with an `open` `decision` escalation reports it under `awaiting_decision`.
7. **Invariant:** `tick` never changes `authorized_phase` and never approves/rejects a proposal
   (assert desired `version`/`authorized_phase` unchanged across a `tick` that does other work).
8. `run --max-ticks N` performs exactly N passes and exits (bounded loop).
9. `checkpoint` returns `continue: true` for a session holding an unfinished `claimed` task and
   `continue: false` otherwise.
10. Hook: for an **editor** role, a `bash` `coord add-task ... --verify "..."` is **denied**;
    for the **orchestrator** role it is **allowed**; existing worker/navigator hook behavior is
    unchanged.
