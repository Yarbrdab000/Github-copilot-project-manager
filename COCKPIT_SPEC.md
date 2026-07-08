# COCKPIT_SPEC — addendum to `agent-coordination-skills`

**This extends the already-built repo (post-Autonomy).** Do not rebuild existing files. Add the
new commands, data areas, and role edits described here as *surgical* additions to
`coord/coord.py`, `hooks/scripts/write_scope_guard.py`, and the navigator/orchestrator
skill/agent. All existing tests must still pass afterward. The locked
`reference/coord.reference.py` stays untouched.

> **Prerequisite:** builds on the Navigator seam (`state propose/approve`) and the Autonomy loop
> (`tick`/`run`, `verify`, `escalate`). Land the Autonomy PR to `main` first and branch from it
> (`feat/cockpit` off `main`). Do not target `main` directly.

## 1. Why this addendum exists

After Autonomy, the fleet runs its own mechanics — but a human still has to **decide how many
sessions to spin up and what each one does**, then hand the orchestrator a hand-decomposed task
board. That planning is mechanical, not judgment: partitioning a goal into non-colliding units of
work and sizing the fleet is exactly the sort of thing the navigator should do *for* the human.

This addendum makes the **navigator the human's single cockpit**:

- The human states a goal in chat. The **navigator plans**: it decomposes the goal into a task
  DAG, assigns each task a non-overlapping owned-path partition, sizes the worker fleet, and
  **proposes the whole thing as one versioned plan**.
- The human **approves the plan once** (the single judgment act). Then the loop spawns the
  workers, dispatches, verifies, and retries autonomously until it is done or a decision/blocker
  surfaces.
- The navigator is also the human's **read surface**: `coord cockpit` answers "what is the fleet
  doing / what needs me?" and the navigator presents pending decisions and *drafts* the exact
  approve/resolve commands — but never executes them.

Guiding principle, unchanged from Autonomy: **automate the mechanics; keep the human on
judgment.** The navigator gains *planning* and *presentation*; it gains **no authority**. It
still cannot dispatch, spawn, approve, merge, or edit. "Conversation without authority" is
preserved and re-enforced by the hook.

### The honest runtime seam (unchanged, and it applies to spawning)

A filesystem control plane can *record* an intention ("spawn a worker `w-api` owning
`src/api/**`", "dispatch task X to `w-api`") but **creating or waking an OS-level session is a
runtime capability, not a filesystem one.** So this addendum keeps the same split the Autonomy
spec established:

- **`coord` is pure control logic.** `plan approve` folds a plan into tasks + a declared fleet;
  `tick` folds ledgers and **emits structured directives** (`spawn`, `dispatch`) into an
  append-only ledger. Deterministic, offline, fully unit-testable.
- **A thin runtime adapter** (outside `coord`) consumes those directives and calls the host's
  real "create/wake a session" capability (`create_session` in this app). It is documented here
  and shipped as an **offline, dry-run reference** — it makes no network calls and is not coupled
  to any specific host.

Do not try to make `coord` spawn sessions. `coord` decides *what* the fleet should be and emits
directives; the adapter makes it so.

## 2. Constraints (do not violate)

- **Additive only.** Extend `coord/coord.py`, `hooks/scripts/write_scope_guard.py`, the navigator
  and orchestrator skill/agent, and `docs/`. Do not modify existing command behavior. Every
  currently-passing test must still pass.
- **`reference/coord.reference.py` is locked** — zero diff. If you think it has a bug, write a
  failing test and raise it.
- **The human gate is inviolate.** `plan approve`, `state approve`, `authorized_phase`, and merge
  stay human-gated. `tick`/`run`/the adapter MUST NOT approve a plan, bump `authorized_phase`,
  approve a proposal, or do any git write. (This is the Autonomy invariant, re-asserted for
  plans.)
- **Navigator has no authority.** The navigator may *propose* a plan and *read* the cockpit. It
  may not `plan approve`/`plan reject`, dispatch, spawn, `state set`, claim, complete, merge,
  push, or edit. Enforced by the hook (defense-in-depth on top of the missing `edit` tool).
- **Non-overlapping partitions.** A plan whose declared workers have overlapping `owned_paths`
  MUST be rejected at propose time — overlapping owners would collide under the write-scope hook.
- **Offline, stdlib-only, deterministic.** Python 3.8+; `pytest` is the only dev dependency. No
  network in any script or test.

## 3. New commands and data

`coord` = `python coord/coord.py`. All new state lives under `$COORD_ROOT/.coordination/`.

### 3.1 Fleet spec in `desired.json`

`desired.json` gains an optional `fleet` object (absent on legacy planes; treat missing as
empty):

```json
"fleet": {
  "max_concurrent": 3,
  "workers": [
    {"id": "w-api", "owned_paths": ["src/api/**", "tests/api/**"]},
    {"id": "w-ui",  "owned_paths": ["src/ui/**",  "tests/ui/**"]}
  ]
}
```

- `max_concurrent` (int ≥ 1) — the hard cap on simultaneously-live fleet workers. The guardrail
  that makes autonomous spawning safe.
- `workers[]` — the declared fleet: a stable `id` and a list of `owned_paths` globs. Partitions
  MUST be pairwise non-overlapping.

### 3.2 Plans (`coord plan …`)

A **plan** is a structured proposal that stages a fleet + a task DAG atomically, approved as one
unit. Plans live in an append-only ledger `.coordination/plans.jsonl`; the pending/approved
projection is derived by fold (same pattern as proposals).

A plan document:

```json
{
  "id": "<epoch-ns>",
  "as_of": 4,
  "note": "build feature X",
  "fleet": { "max_concurrent": 3, "workers": [ {"id":"w-api","owned_paths":["src/api/**"]}, ... ] },
  "tasks": [
    {"id":"api-model","desc":"…","owned_by":"w-api","deps":[],          "verify":"pytest tests/api -q","max_attempts":3},
    {"id":"api-routes","desc":"…","owned_by":"w-api","deps":["api-model"],"verify":"pytest tests/api -q","max_attempts":3}
  ]
}
```

- **`coord plan propose --file <plan.json>`** (also accept the doc on stdin) — validate and write
  a **pending** plan. Validation (all must hold, else non-zero exit + message):
  - worker `id`s unique; `max_concurrent` ≥ 1;
  - every declared worker's `owned_paths` non-empty; **workers pairwise non-overlapping** (§3.3);
  - every `task.owned_by` references a declared worker;
  - every `task.deps` entry references another task **in the same plan**;
  - every `task.id` unique and not already on the live board;
  - `verify` present (may be an explicit `null` to opt out, mirroring `add-task`).

  On success: append pending plan, print its id + a `current → proposed` summary. **Does NOT bump
  `desired.version`.** Allowed for the navigator.
- **`coord plans`** — list pending plans (id, as_of, note, worker count, task count).
- **`coord plan show --id <pid>`** — print the full `current → proposed` diff (fleet + task DAG).
- **`coord plan approve --id <pid>`** — apply the plan (human/orchestrator only; navigator
  DENIED by hook). Effects, atomically:
  - create each task (status `open`, carrying `owned_by`, `deps`, `verify`, `max_attempts`) via
    the existing task fold;
  - set `desired.fleet` to the plan's fleet;
  - **bump `desired.version`**; mark the plan `approved`;
  - emit initial `spawn` directives (§3.4) for the declared workers, capped at `max_concurrent`.
- **`coord plan reject --id <pid>`** — mark `rejected`; **version + fleet unchanged**.

### 3.3 Owned-path overlap check

Two `owned_paths` **sets** overlap if any glob in one can match a path the other's glob can also
match. Use a conservative, deterministic check (reuse the hook's glob normalization): normalize
each glob to a path prefix (strip trailing `/**`, `/*`); two globs overlap if either normalized
prefix is a prefix of the other (e.g. `src/**` vs `src/api/**` overlap; `src/api/**` vs
`src/ui/**` do not; identical globs overlap). Reject a plan if any pair of *distinct* workers
overlaps.

### 3.4 Directives ledger (`spawn` / `dispatch`)

An append-only `.coordination/directives.jsonl`. `tick` (and `plan approve`) append structured
directives that the runtime adapter consumes:

```json
{"kind":"spawn",   "worker":"w-api","owned_paths":["src/api/**"],"as_of":5,"ts":"…"}
{"kind":"dispatch","worker":"w-api","task":"api-model","as_of":5,"ts":"…"}
```

`coord` only ever **appends** directives and reads `desired.json`; it never spawns/wakes anything
itself.

### 3.5 `coord tick` — fleet awareness (extends the Autonomy tick)

`tick` keeps all Autonomy behavior (reap → verify → requeue/escalate → dispatch → surface). It
gains a **spawn step**, run before dispatch:

1. Read `desired.fleet`. Compute `live` = declared workers that are registered **and**
   heartbeat-fresh. Compute `missing` = declared workers with an open/assigned task that are not
   live.
2. Emit a `spawn` directive for each `missing` worker, **but never let (live + spawned-this-tick)
   exceed `max_concurrent`.**
3. If `missing` demand exceeds the cap (more workers want to run than `max_concurrent` allows),
   do **not** over-spawn: open a single `decision` escalation
   (`kind=decision, reason="fleet at cap; raise max_concurrent or wait"`) so the human can decide.
   Dispatch continues normally for the workers that are live.

`tick`'s printed summary gains `spawned` and (existing) `awaiting_decision`. The **invariant is
unchanged and still tested**: `tick` never changes `desired.version` or `authorized_phase`, never
approves/rejects a plan or proposal, never does a git write.

### 3.6 `coord cockpit` — the navigator's read surface

`coord cockpit [--json]` — a single read-only aggregate (no writes, no heartbeat side effects
beyond the standard read). Navigator-allowed. Returns:

- `desired`: `version`, `authorized_phase`, `fleet` (max_concurrent, declared worker ids);
- `tasks`: counts by status + the list grouped `open / claimed / done / failed`;
- `workers`: each live/declared worker with liveness (`fresh|stale`, heartbeat age) and the task
  it holds;
- `decisions`: open `decision` escalations (what needs the human) — **separated from** `blockers`
  (open `blocker` escalations);
- `pending`: pending plan ids + pending proposal ids (things awaiting the human's approval);
- `capacity`: `live` / `max_concurrent` utilization and any `spawn` directives not yet consumed.

This is what the navigator reads to answer the human and to draft the exact approve/resolve
commands.

## 4. Hook edits (`hooks/scripts/write_scope_guard.py`)

Surgical, additive; preserve fail-open on parse errors and the existing role handling.

- **Navigator allow-list grows.** A `navigator`-role session may additionally run
  `coord plan propose`, `coord plans`, `coord plan show`, and `coord cockpit` (all propose/read).
  It remains **denied** `coord plan approve`, `coord plan reject`, and everything already denied
  (dispatch, spawn, `state set/approve/reject`, claim, complete, add-task, lock, send, stop,
  merge, push, edits).
- **`plan approve`/`plan reject` are orchestrator-only.** For any **non-orchestrator** role
  (editor, worker, navigator), deny a `bash` command whose normalized form is
  `coord plan approve …` or `coord plan reject …` (same segment-split/quote-stripping the
  `add-task --verify` rule uses). Applying a plan bumps the version and defines the fleet — that
  is an authority action reserved for the orchestrator/human.
- Existing worker/editor write-scoping and the `add-task --verify` rule are unchanged.

## 5. File manifest (exactly this — additive edits + new files)

Edit (surgical, additive):
- `coord/coord.py` — fleet spec handling; `plan propose/plans/show/approve/reject`; overlap
  check; directives ledger; `tick` spawn step; `cockpit`.
- `coord/schema/task.schema.json` — add optional `owned_by` (string) to a task; add a
  `plan.schema.json` sibling if the repo validates schemas (only if a schema dir already exists).
- `hooks/scripts/write_scope_guard.py` — navigator plan/cockpit allow-list + non-orch
  `plan approve/reject` deny.
- `agents/navigator.agent.md`, `skills/navigator/SKILL.md` — planner + cockpit capabilities.
- `skills/orchestrator/SKILL.md` — applying approved plans + consuming spawn directives via the
  adapter.
- `docs/architecture.md`, `docs/protocol.md`, `docs/quickstart.md` — architecture §9, protocol
  entries for the new commands, quickstart Walkthrough E.

New files:
- `COCKPIT_SPEC.md`, `COCKPIT_BUILD_PLAN.md`, `COCKPIT_KICKOFF.md` — land at repo root (like the
  prior addendum specs).
- `runtime/adapter.reference.py` — the offline, dry-run runtime adapter reference (§6). Stdlib
  only; reads a directives ledger and prints the `create_session`/wake actions it *would* take;
  `--dry-run` makes no calls.
- `tests/test_cockpit_plan.py` — plan propose/validate/approve/reject (§7.1–§7.5).
- `tests/test_cockpit_tick.py` — spawn directives + cap + over-cap escalation + invariant
  (§7.6–§7.7).
- `tests/test_cockpit_view.py` — `cockpit --json` aggregate (§7.8).
- `tests/test_cockpit_hook.py` — hook allow/deny matrix (§7.9).
- `tests/test_cockpit_adapter.py` — adapter dry-run over a fixture ledger, offline (§7.10).

Do not add anything outside this manifest.

## 6. Role edits + the runtime adapter reference

**`skills/navigator/SKILL.md` / `agents/navigator.agent.md`** gain two capabilities, framed so the
"no authority" discipline stays first:

- **Planner.** Turn a human goal into a `coord plan propose` document: decompose into a task DAG
  (ids, deps, per-task `verify`), assign each task an `owned_by` worker, compute a **non-overlapping
  owned-path partition**, and size the fleet to ≤ `max_concurrent`. The plan is a *proposal* — a
  request, not an act.
- **Cockpit.** Read `coord cockpit` to answer "what is the fleet doing / what needs you," and when
  a plan or decision is pending, **present it and draft the exact `coord plan approve …` /
  `coord state approve …` / `coord resolve …` command for the human to run** — the navigator never
  runs it (the hook denies it).

Re-assert: the navigator still cannot approve/dispatch/spawn/merge/edit.

**`skills/orchestrator/SKILL.md`** gains: apply human-approved plans (`plan approve`), and note
that spawning/waking workers from `spawn`/`dispatch` directives is done by the runtime adapter
(the orchestrator/host), never by `coord` itself.

**`runtime/adapter.reference.py`** — a thin, offline reference:
- reads new lines from `.coordination/directives.jsonl`;
- for a `spawn` directive, prints the intended "create a `<role=editor>` session for worker
  `<id>` owning `<paths>`" action; for `dispatch`, prints the intended "wake `<worker>` with task
  `<task>`" action;
- `--dry-run` (default) only prints; a host wires the real `create_session`/wake in the marked
  hand-off function. **No network, no app import** — the hand-off is a single clearly-marked
  stub.

## 7. Acceptance criteria (new tests; existing suite stays green)

All tests offline, stdlib-only, deterministic.

1. `plan propose` on a valid plan writes a **pending** plan and **does not** bump
   `desired.version`.
2. `plan propose` on a plan whose two workers have **overlapping** `owned_paths` (e.g. `src/**`
   and `src/api/**`) is **rejected** (non-zero exit, no plan written).
3. `plans` / `plan show` list the pending plan with a `current → proposed` view (fleet + task
   count/DAG).
4. `plan approve` **creates every task** (with `owned_by`/`deps`/`verify`/`max_attempts`), sets
   `desired.fleet`, **bumps `desired.version`**, and marks the plan `approved`.
5. `plan reject` leaves `desired.version` **and** `desired.fleet` unchanged and marks the plan
   `rejected`.
6. After an approved plan, `tick` emits a `spawn` directive for each declared-but-not-live worker,
   **never exceeding `max_concurrent`** live+spawned.
7. When demand exceeds the cap, `tick` opens exactly one `decision` escalation instead of
   over-spawning; **invariant:** across that `tick`, `desired.version` and `authorized_phase` are
   unchanged and no plan/proposal is approved.
8. `cockpit --json` returns the aggregate: tasks-by-status, workers with liveness, `decisions`
   separated from `blockers`, and `pending` plan/proposal ids.
9. Hook matrix: **navigator** — `coord plan propose …` and `coord cockpit` **allowed**;
   `coord plan approve …` / `coord plan reject …` **denied**. **editor** — `coord plan approve …`
   **denied**. **orchestrator** — `coord plan approve …` **allowed**. Existing navigator file-edit
   deny and worker `git push` deny **unchanged**.
10. `runtime/adapter.reference.py --dry-run` over a fixture `directives.jsonl` prints exactly the
    intended spawn/dispatch actions (one line each, in order) and makes **no** network calls
    (assert offline: patch/deny socket).
