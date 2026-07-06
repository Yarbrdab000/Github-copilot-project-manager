# NAVIGATOR_KICKOFF — paste into a Copilot agent session on the built repo

> Prereqs: `NAVIGATOR_SPEC.md` and `NAVIGATOR_BUILD_PLAN.md` are committed to the repo (root
> is fine). Run from a session with edit + bash tools. The base repo (`coord/coord.py`, tests,
> hook, agents, skills, docs) is already built and green.

---

You are extending this already-built repository. Authoritative inputs, in priority order:
`.github/copilot-instructions.md` (existing guardrails — they still apply),
`NAVIGATOR_SPEC.md` (what to add), `NAVIGATOR_BUILD_PLAN.md` (the order). Read all three, and
read the existing `coord/coord.py`, before writing anything.

Non-negotiables:
- This is an **extension, not a rebuild.** Do not recreate or rewrite existing files. Make
  only the additions and the surgical edits `NAVIGATOR_SPEC.md` §2–§4 specify. The claim mutex
  and lease code in `coord.py` must remain byte-for-byte as-is except where §3 names.
- **The existing test suite must stay green at every phase.** Run `pytest -q` at Phase 0
  before changing anything and after each subsequent phase; paste real output. If a change
  breaks an existing test, stop and raise it rather than editing the test to pass.
- Work on a new branch (`feat/navigator`), **not `main`**. One `NAVIGATOR_BUILD_PLAN.md` phase
  at a time; run that phase's checks, commit, and **stop and report** before the next. No
  single uninterrupted pass.
- Verify, don't assert. Actually run the tests and the smoke walkthroughs; never claim a check
  passes without pasted output.
- Preserve the design invariant this feature exists to protect: the navigator can influence
  the fleet **only** by proposing a `desired.json` change that a human approves. If any part of
  your implementation would let the navigator dispatch, merge, edit, or self-approve, that's a
  bug — stop and raise it.

Start now with **Phase 0** only: create the branch, run the existing suite, confirm it's
green, and report before touching Phase 1.
