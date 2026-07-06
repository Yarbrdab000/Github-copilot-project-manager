# AUTONOMY_KICKOFF — paste into a fresh Copilot agent session

> Prereqs: the addendum kit is present (`AUTONOMY_SPEC.md`, `AUTONOMY_BUILD_PLAN.md`, this
> file, `README.md`) and the repo already contains the built, Navigator-extended
> `agent-coordination-skills` (locked `reference/coord.reference.py`, `coord/coord.py`, the
> hooks, skills, agents, docs, and the passing test suite). Run from a session with edit + bash
> tools in the repo.

---

You are extending this repository, not rebuilding it. Authoritative inputs, in priority order:
`.github/copilot-instructions.md` (guardrails), `AUTONOMY_SPEC.md` (what to add), and
`AUTONOMY_BUILD_PLAN.md` (the order). Read all three fully before writing anything.

Non-negotiables:
- `reference/coord.reference.py` is a **locked** control-plane reference. Do not modify it. New
  behavior (`tick`, `verify`, `escalate`, `run`) is **new tested code** in `coord/coord.py` and
  friends — exactly how the Navigator addendum added code without touching the locked reference.
- This is an **extension**. Do not rebuild existing files, invent new frameworks, or add scope
  beyond `AUTONOMY_SPEC.md`. The existing suite must stay green at every phase. If anything is
  ambiguous, **stop and ask** rather than guessing.
- Respect the **runtime seam** (SPEC §1): `coord tick` is pure, deterministic control logic that
  emits directives + effects; it must NOT try to wake OS processes, and it must NEVER change
  `authorized_phase`, approve/reject proposals, or perform git writes. Those stay human-gated.
- Work **one `AUTONOMY_BUILD_PLAN.md` phase at a time**. After each phase: run that phase's
  checks, paste the **real** output, commit, and **stop and report** before the next phase. Do
  not run the whole plan in one pass.
- Verify, don't assert. Actually run `pytest -q` and paste it; actually run `tick`/`verify` and
  paste their JSON. Never claim a check passes without having run it.
- Offline, stdlib-only, no secrets, no network. Simulate verify pass/fail with
  `python -c "import sys; sys.exit(0|1)"`.
- Branch `feat/autonomy` off `feat/navigator` (or off `main` once the Navigator PR merges).
  Never target `main` directly; never self-merge — the final PR is for human review.

Start now with **Phase 0** only: create the branch, run the inherited suite, prove it is green,
paste the output, and **stop** and show me before Phase 1.

---
