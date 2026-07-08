# COCKPIT_KICKOFF — paste into the build worker session

You are building the **Cockpit addendum** to `agent-coordination-skills`. Authoritative inputs,
in priority order: `.github/copilot-instructions.md` (guardrails), `COCKPIT_SPEC.md` (what to
build), `COCKPIT_BUILD_PLAN.md` (the order). Read all three fully before writing anything.

Non-negotiables:
- This is an **additive** addendum on top of the Autonomy build. Do not modify existing command
  behavior. Every currently-passing test must still pass.
- `reference/coord.reference.py` is **locked** — zero diff. If you think it has a bug, write a
  failing test and raise it; do not edit it.
- Build **exactly** the file manifest in COCKPIT_SPEC §5. Do not add scope, frameworks, or
  abstractions that aren't specified. If anything is ambiguous, **stop and ask** rather than
  guessing.
- The human gate is inviolate: `plan approve`, `state approve`, `authorized_phase`, and merge stay
  human-gated. `tick`/`run`/the adapter must never approve a plan, bump `authorized_phase`,
  approve a proposal, or do a git write. The navigator has **no authority** (propose + read only)
  — enforce it in the hook.
- Work **one `COCKPIT_BUILD_PLAN.md` phase at a time**. After each phase: run that phase's checks,
  paste the real output, commit, and **stop and report**. Do not start the next phase until the
  human bumps `authorized_phase` on the control plane.
- Verify, don't assert. Actually run `python -m pytest -q` and paste output; actually run the
  quickstart walkthrough in a scratch `COORD_ROOT`; actually drive the hook with stdin payloads.

Branch `feat/cockpit` off `main` (post-Autonomy). Environment note: this repo runs on Windows —
use `python` (not `python3`), and set `COORD_ROOT` inline in each shell since every shell is a
fresh process.

Start now with **Phase 0** only: confirm the inherited suite is green (record the count), land
the three kit docs at the repo root, commit, and **stop and report** with the tree and the
baseline test count before continuing to Phase 1.
