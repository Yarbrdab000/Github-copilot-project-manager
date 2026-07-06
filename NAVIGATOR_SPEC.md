# NAVIGATOR_SPEC — addendum to `agent-coordination-skills`

**This extends the already-built repo.** Do not rebuild existing files. Add the new files
and make the *surgical* edits to `coord/coord.py` and `hooks/scripts/write_scope_guard.py`
described here. All existing tests must still pass afterward.

## 1. Why this role exists

The current system has a **reconciliation** loop (the orchestrator: dispatch, reap,
integrate — must keep ticking) and **workers**. It has no seat for **deliberation** — a
design partner the human can riff with — so today the only way to make design changes is to
stop the orchestrator, which collapses the fleet.

The fix is a third role with an inverted authority model:

- **Orchestrator** = authority without conversation (acts; doesn't deliberate).
- **Navigator** = conversation without authority (deliberates with the human; cannot act).
- **Seam between them = `desired.json`.** The navigator influences the fleet *only* by
  amending the versioned contract every worker already reconciles against at checkpoints.
  Design changes propagate on the next checkpoint; the orchestrator never pauses.

The navigator's single lever is a **proposal** to `desired.json`, which a human (or the
orchestrator on explicit human say-so) must **approve** before it becomes a live version.
That approval is the highest-leverage write in the system and is deliberately human-gated.

## 2. New `coord` commands (add to `coord/coord.py`)

Follow the existing style: append-only where possible, atomic writes, reuse `_acquire_raw`/
`_release_raw`, `_append`, `_atomic_write`, `_read_json`, `iso()`, `now()`.

Add a proposals area under the control plane: `.coordination/state/proposals/<pid>.json`
(create the dir in `cmd_init`). A proposal:
```json
{ "pid": "<time_ns>", "from": "<session>", "key": "...", "value": <any>,
  "invalidates": ["taskA","taskB"], "note": "...", "status": "pending",
  "created": "<iso>", "base_version": <int> }
```

- `state propose --session <id> --key <k> --value <v> [--invalidates a,b] [--note "..."]`
  Writes a **pending** proposal. **Does NOT bump the live version.** Print the pid and a diff
  preview: current `desired[key]` → proposed value. `value` parses as JSON, else literal
  (match `cmd_state`'s existing behavior).
- `state proposals` — list pending proposals: pid, from, key, current→proposed, invalidates,
  note.
- `state approve --id <pid> [--session <id>]` — under the `__state__` lock (same pattern as
  `state set`): apply `value` to `desired[key]`, bump `version`, set `updated`, write an
  `events.jsonl` `state_approved` record. Then **for each task in `invalidates`**: append a
  `{status:"open", claimed_by:null}` task event (requeue) **and** send a message to that
  task's *current claimant* via the existing inbox mechanism with `as_of` = the **new**
  version and body naming the task + version (so it lands as a *fresh* message at the
  claimant's next checkpoint, telling it to stop and re-claim). Mark the proposal
  `status:"applied"`. Print the new version and what was requeued.
- `state reject --id <pid> [--reason "..."]` — mark proposal `rejected`; **no** version
  change; record an event.

## 3. Surgical hardening to existing commands

- **`cmd_claim`**: when appending the `claimed` event, also record
  `claimed_at_version` = current `desired.version`. (Enables detecting silently-stale work.)
- **`cmd_complete`**: **guard against stale completion.** Before appending, fold tasks; if the
  task's current status is not `claimed` **or** `claimed_by` != this session (i.e. it was
  requeued/invalidated out from under the worker), refuse with a clear message and non-zero
  exit — do **not** mark it done. This is what makes invalidation safe: a worker that kept
  going on an invalidated task cannot complete it stale.

## 4. Navigator enforcement (extend `hooks/scripts/write_scope_guard.py`)

Make "conversation without authority" a **hard** property, not a prompt promise. When the
acting session's registry `role` is `navigator`:
- On `bash` tool calls, parse the command and **allow only**: `coord state propose`,
  `coord state proposals`, `coord state show`, `coord status`, `coord tasks`, and read-only
  inspection (`git status|log|diff|show`, `cat`, `ls`, `grep`, `find`). **Deny everything
  else** — explicitly `coord state set`, `state approve`, `state reject`, `claim`, `complete`,
  `add-task`, `lock`, `send`, `stop`, and any `git push|merge|checkout|switch|commit`, and any
  output redirection to files.
- Deny all file-editing tools (`edit`, `create`, `str_replace`, `write`, `create_file`,
  `apply_patch`) for a navigator regardless of path.
- Allow read tools.
Keep the existing per-worker write-scoping behavior unchanged for non-navigator roles. Keep
the fail-open-on-parse-error stance.

## 5. New role files

- `agents/navigator.agent.md` — tools limited to `read`, `search`, and `bash` (bash is
  constrained by the hook above to the propose/read allow-list). Description: the human's
  design partner; influences the fleet only via `desired.json` proposals; cannot dispatch,
  merge, edit, or approve. Loads `skills/coordination-protocol` + `skills/navigator`.
- `skills/navigator/SKILL.md` — YAML frontmatter + body. Core discipline, stated plainly:
  *You never act on the fleet directly. Your only lever is `coord state propose`. When a
  design change would invalidate work in flight, you MUST pass `--invalidates` naming those
  tasks so approval requeues them instead of letting them finish stale. You do not approve
  your own proposals — a human does.*

## 6. Docs

- `docs/architecture.md`: add the three-role symmetry (authority-without-conversation /
  conversation-without-authority / seam = `desired.json`) and the propose→approve→propagate
  flow, including how invalidation becomes a re-plan event rather than a stomp.
- `docs/protocol.md`: document the four new `state` subcommands, the `cmd_complete` guard, and
  `claimed_at_version`.

## 7. Acceptance criteria (new tests; existing suite must stay green)

Add to `tests/`:
1. `propose` writes a pending proposal and **does not** change `state show` version.
2. `proposals` lists the pending one with the current→proposed values.
3. `approve` bumps the version, applies the value, and marks the proposal `applied`.
4. `approve` with `--invalidates T` requeues T (folded status → `open`) **and** drops a fresh
   message (`as_of` = new version) into the prior claimant's inbox.
5. After that approve, the prior claimant's `complete T` is **refused** (stale-completion
   guard), non-zero exit.
6. `reject` leaves the version unchanged and marks the proposal `rejected`.
7. Hook: for a `navigator`-role session, `bash` running `coord claim ...`, `coord state set
   ...`, and `git push` are **denied**; `coord state propose ...`, `coord state show`, and
   `git status` are **allowed**; any file-edit tool is **denied**.
