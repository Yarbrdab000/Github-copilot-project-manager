# COCKPIT_BUILD_PLAN — build order for the Cockpit addendum

Build **one phase at a time**. After each phase: run that phase's checks, paste real output,
commit, then **stop and report** and await the human's `authorized_phase` bump before the next.
The locked `reference/coord.reference.py` stays zero-diff throughout. Branch `feat/cockpit` off
`main` (post-Autonomy).

Convention: `coord` = `python coord/coord.py`. Each phase's tests are offline/stdlib-only.

## Phase 0 — baseline + land kit
- Confirm the inherited suite is green on `main` (`python -m pytest -q`), record the count.
- Copy `COCKPIT_SPEC.md`, `COCKPIT_BUILD_PLAN.md`, `COCKPIT_KICKOFF.md` to the repo root; commit.
- **Check:** inherited suite still green (unchanged count); `git diff main -- reference/` empty.

## Phase 1 — fleet spec + non-overlap partition check
- Add `fleet` handling to `desired.json` reads (missing → empty; never crash on legacy planes).
- Implement the deterministic owned-path overlap check (§3.3) as a pure helper.
- Extend `coord/schema/task.schema.json` with optional `owned_by`; add `plan.schema.json` **only
  if** the repo already validates schemas.
- **Tests (new `tests/test_cockpit_plan.py`, partial):** overlap helper — `src/**` vs
  `src/api/**` overlap; `src/api/**` vs `src/ui/**` do not; identical overlap.
- **Check:** new helper tests pass; full suite green; reference 0-diff.

## Phase 2 — `plan propose` / `plans` / `plan show`
- Implement the plans ledger + fold; `plan propose` with full validation (§3.2); `plans`;
  `plan show`.
- **Tests:** §7.1 (pending, no version bump), §7.2 (overlap rejected, non-zero, nothing written),
  §7.3 (list current→proposed).
- **Check:** those tests pass; `plan propose` does **not** bump `desired.version` (assert);
  full suite green; reference 0-diff.

## Phase 3 — `plan approve` / `plan reject` (KEYSTONE)
- `plan approve`: create tasks (owned_by/deps/verify/max_attempts), set `desired.fleet`, bump
  version, mark approved, emit initial capped `spawn` directives. `plan reject`: mark rejected,
  no version/fleet change.
- **Tests:** §7.4 (approve creates tasks + sets fleet + bumps version + marks applied), §7.5
  (reject leaves version+fleet unchanged).
- **Check:** those pass; approve is atomic (either all tasks land or none); full suite green;
  reference 0-diff. **Stop — this is the human-gated seam; report before continuing.**

## Phase 4 — `tick` spawn step + concurrency cap
- Add the spawn step to `tick` (§3.5): emit `spawn` for declared-but-not-live workers, capped at
  `max_concurrent`; over-cap → one `decision` escalation. Add `spawned` to the summary. Reuse the
  existing `_tick_once` plumbing so `run` inherits it.
- **Tests (`tests/test_cockpit_tick.py`):** §7.6 (spawn emitted, capped), §7.7 (over-cap →
  decision escalation; invariant: version/authorized_phase unchanged, no approve).
- **Check:** those pass; the Autonomy invariant test still passes; full suite green; reference
  0-diff.

## Phase 5 — `coord cockpit`
- Implement the read-only aggregate (§3.6), `--json` and human-readable.
- **Tests (`tests/test_cockpit_view.py`):** §7.8 (tasks-by-status; workers+liveness; decisions vs
  blockers; pending plan/proposal ids).
- **Check:** those pass; `cockpit` performs no writes (assert ledger mtimes unchanged across a
  call); full suite green; reference 0-diff.

## Phase 6 — hook rules + role edits + docs
- Hook (§4): navigator allow `plan propose/plans/show` + `cockpit`; non-orchestrator deny
  `plan approve/reject`.
- Navigator skill/agent: planner + cockpit capabilities (discipline-first). Orchestrator skill:
  apply approved plans + adapter note.
- Docs: architecture §9 (the cockpit seam + a propose-plan→approve→spawn diagram), protocol
  entries for the new commands (with real pasted output), quickstart **Walkthrough E**
  (navigator proposes a 2-worker plan → human approves → tick spawns), runnable in a scratch
  `COORD_ROOT`.
- **Tests (`tests/test_cockpit_hook.py`):** §7.9 full allow/deny matrix; assert existing
  navigator/worker hook behavior unchanged. Validate frontmatter still parses.
- **Check:** hook tests pass; run Walkthrough E in a scratch plane and paste real output; full
  suite green; reference 0-diff.

## Phase 7 — runtime adapter reference + FINAL GATE + PR
- `runtime/adapter.reference.py` (§6): offline, `--dry-run` default, single marked hand-off stub.
- **Tests (`tests/test_cockpit_adapter.py`):** §7.10 (dry-run prints intended actions in order;
  assert no network — deny `socket`).
- **Final gate (verification only, no code commit):**
  - `python -m pytest -q` → full suite green; paste the tail.
  - `pytest tests/test_cockpit_*.py -v` → map every §7.1–§7.10 to a named passing test; paste.
  - Inherited unchanged: `pytest tests/test_coord.py tests/test_write_scope_guard.py
    tests/test_navigator_*.py tests/test_autonomy_*.py -q`.
  - `git diff main..HEAD -- reference/coord.reference.py` → empty.
  - **Push `feat/cockpit`; open a PR to `main` with the §7 map. NO self-merge.**

## Definition of done
Every §7 item has a named passing test with pasted output; Walkthrough E runs clean; the file
manifest is complete and nothing outside it was added; `reference/` is byte-identical; the PR is
open (not merged).
