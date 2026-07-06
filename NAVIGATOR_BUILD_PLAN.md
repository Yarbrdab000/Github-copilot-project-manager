# NAVIGATOR_BUILD_PLAN — phased, checkpointed

Extends the built repo. Work on a new branch (e.g. `feat/navigator`); do not touch `main`.
One phase at a time; run checks, commit, **stop and report** between phases. Existing tests
must stay green at every phase.

Read `NAVIGATOR_SPEC.md` and the existing `coord/coord.py` before starting.

---

## Phase 0 — Baseline  (no deps)
- New branch off the current build. Run `pytest -q` and confirm the existing suite is green
  **before** changing anything. Paste output.
- **Checkpoint & report.**

## Phase 1 — coord: proposals + hardening  (deps: 0)
- Add `state propose | proposals | approve | reject` to `coord/coord.py` per SPEC §2, plus the
  proposals dir in `cmd_init`.
- Apply the SPEC §3 hardening: `claimed_at_version` in `cmd_claim`; the stale-completion guard
  in `cmd_complete`.
- **Do not** alter unrelated logic (the claim mutex / lease code stays exactly as-is).
- **DoD:** existing suite still green; manual smoke of propose→proposals→approve→reject works.
- **Checkpoint & report.**

## Phase 2 — coord tests  (deps: 1)
- Add tests for SPEC §7.1–§7.6 (proposal lifecycle, version-bump-only-on-approve, invalidation
  requeue + claimant notification, stale-completion refusal).
- **DoD:** `pytest -q` green including new tests; paste output.
- **Checkpoint & report.**

## Phase 3 — navigator hook enforcement + test  (deps: 0, but land after 1)
- Extend `hooks/scripts/write_scope_guard.py` per SPEC §4 (navigator role → propose/read
  allow-list on bash, deny all edits). Keep worker write-scoping untouched.
- Add a test for SPEC §7.7 by piping crafted payloads with a navigator-role session.
- **DoD:** hook tests green (old + new); paste output.
- **Checkpoint & report.**

## Phase 4 — role files  (deps: 1,3)
- `agents/navigator.agent.md` and `skills/navigator/SKILL.md` per SPEC §5.
- **DoD:** frontmatter parses; tool list matches SPEC; skill states the propose-only discipline
  and the `--invalidates` obligation.
- **Checkpoint & report.**

## Phase 5 — docs  (deps: all)
- Update `docs/architecture.md` and `docs/protocol.md` per SPEC §6. Add a short navigator
  walkthrough to `docs/quickstart.md`.
- **DoD:** a copy-paste propose→approve walkthrough in quickstart runs clean (paste the run).
- **Checkpoint & report.**

## Phase 6 — Final gate  (deps: all)
- Full suite green; every SPEC §7 item has a passing test; existing behavior unchanged.
- Open a PR summarizing additions + pasted acceptance results. **Do not self-merge.**
- **Checkpoint & report — done.**
