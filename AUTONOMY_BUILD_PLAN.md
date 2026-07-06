# AUTONOMY_BUILD_PLAN — phased, checkpointed

Build the AUTONOMY_SPEC as an **extension** of the post-Navigator repo. One phase at a time:
run that phase's checks, paste real output, commit, then **stop and report**. Do not run the
whole plan in one pass. The existing suite (77 after Navigator) must stay green at every phase.
`reference/coord.reference.py` stays byte-for-byte untouched.

**Branch:** `feat/autonomy`, off `feat/navigator` (or off `main` once the Navigator PR merges).
Never target `main` directly; never self-merge.

**Dependency order:** gates → escalation → **tick** (keystone) → run + self-continue → docs → gate.

---

## Phase 0 — Baseline + land the kit
- Create `feat/autonomy` from the agreed base. Land the build kit at the repo root
  (`AUTONOMY_SPEC.md`, `AUTONOMY_BUILD_PLAN.md`, `AUTONOMY_KICKOFF.md`) so the worker builds
  against in-tree inputs (as the Navigator build did with its kit).
- Run `pytest -q` and confirm the inherited suite is green **before any code change**. Paste
  output. Commit `chore(autonomy): land build kit + confirm baseline`. Stop + report.

## Phase 1 — Coded acceptance gates  *(SPEC §2, §3.1, §7.2–§7.3 partial)*
- Extend `add-task` with `--verify` / `--max-attempts`; persist on the event.
- Add `cmd_verify` (`coord verify`): resolve claimant worktree, run the command via
  `subprocess`, append `verified` / `verify_failed` (increment `attempts`), exit code mirrors rc.
- Fold `attempts` / `verified` in `_fold_tasks`. Update `coord/schema/task.schema.json`
  additively.
- **Tests:** passing verify records `verified`; failing verify increments `attempts` + non-zero
  exit; no-verify task trivially verifies. Full suite still green.
- Commit `feat(autonomy): coded acceptance gates`. Stop + report.

## Phase 2 — Escalation channel  *(SPEC §2, §3.2, §7.5)*
- Create `.coordination/escalations/` in `cmd_init`.
- Add `cmd_escalate` / `cmd_escalations` / `cmd_resolve`.
- **Tests:** escalate → listed open → resolve → drops off open list; `as_of` recorded.
- Commit `feat(autonomy): escalation channel`. Stop + report.

## Phase 3 — The keystone: `coord tick`  *(SPEC §3.3, §7.1, §7.3, §7.4, §7.6, §7.7)*
- Add `cmd_tick`: reap → verify → requeue/escalate on fail → advisory dispatch → stall nudge →
  budget enforcement → surface `awaiting_decision`. Reuse `cmd_reap` logic; do not duplicate it.
- **Hard invariant:** `tick` must not touch `authorized_phase`, approve/reject proposals, or do
  any git write.
- **Tests:** reap requeues (§7.1); fail→requeue+message (§7.3); max_attempts→failed+blocker
  escalation (§7.4); open decision escalation surfaces as `awaiting_decision` (§7.6); invariant
  test asserts `version`/`authorized_phase` unchanged across a working `tick` (§7.7).
- Commit `feat(autonomy): reconciliation tick`. Stop + report.

## Phase 4 — Loop wrapper + worker self-continue  *(SPEC §3.4, §6, §7.8, §7.9)*
- Add `cmd_run` (`--interval`, `--max-ticks`, `--once`) — thin loop over `tick`.
- Add `continue` boolean to `cmd_checkpoint` output.
- Edit `skills/worker/SKILL.md` (+ note in `agents/editor.agent.md`) with the self-continue
  directive.
- **Tests:** `run --max-ticks N` performs exactly N passes (§7.8); `checkpoint` returns
  `continue` correctly (§7.9). Frontmatter still parses.
- Commit `feat(autonomy): run loop + worker self-continue`. Stop + report.

## Phase 5 — Hook hardening + docs  *(SPEC §4, §5, §7.10)*
- `write_scope_guard.py`: deny a non-orchestrator `coord add-task ... --verify` (bash +
  spelled-out); keep all other behavior identical, fail-open on parse.
- Docs (surgical, preserve voice/numbering): `architecture.md` — the autonomy loop + the runtime
  seam; `protocol.md` — new command reference entries with real output; `quickstart.md` —
  "Walkthrough D — autonomous run to an escalation" (must run clean in a **scratch** COORD_ROOT).
- **Tests:** hook denies editor `--verify`, allows orchestrator (§7.10). Full suite green.
- Commit `feat(autonomy): hook guard + docs`. Stop + report.

## Phase 6 — Final gate + PR
- Verification only (no code commit): full suite green; map every SPEC §7 item to a named
  passing test with pasted `-v` output; prove the inherited suite is unchanged; confirm
  `reference/coord.reference.py` diff is empty.
- Push `feat/autonomy`; open a PR summarizing additions + the §7 acceptance map. **No
  self-merge.**

---

### Checks to paste at each phase
- `python -m pytest -q` (full suite tail).
- Targeted `-v` for the phase's new tests.
- `git diff --name-only <base>..HEAD` (scope proof).
- `git diff <base>..HEAD -- reference/coord.reference.py` (must be empty).
