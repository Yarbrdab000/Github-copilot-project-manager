# KICKOFF — paste this into a fresh Copilot agent session in the empty repo

> Prereqs: the six kit files are committed to the repo root (`SPEC.md`, `BUILD_PLAN.md`,
> `KICKOFF.md`, `.github/copilot-instructions.md`, `reference/coord.reference.py`,
> `reference/ACCEPTANCE.md`). Run this from a session with edit + bash tools in the repo.

---

You are building this repository. Your authoritative inputs are, in priority order:
`.github/copilot-instructions.md` (guardrails), `SPEC.md` (what to build), and
`BUILD_PLAN.md` (the order to build it). Read all three fully before writing anything.

Non-negotiables:
- `reference/coord.reference.py` is a **tested, locked** control-plane implementation. Copy
  it to `coord/coord.py` **verbatim**. Do not rewrite it. If you think it has a bug, write a
  failing test and raise it — don't silently change it.
- Build **exactly** the file manifest in SPEC §5. Do not add scope, extra frameworks, or
  abstractions that aren't specified. If anything is ambiguous, **stop and ask me** rather
  than guessing.
- Work **one `BUILD_PLAN.md` phase at a time**. After each phase: run that phase's checks,
  paste the real output, commit, and **stop and report** before starting the next phase. Do
  not run the whole plan in one uninterrupted pass.
- Verify, don't assert. Actually run `pytest -q` and paste output; actually validate hook
  JSON and schemas. Never claim a check passes without having run it.

Start now with **Phase 0** only. Confirm the scaffold, prove `python3 coord/coord.py --help`
works, then stop and show me the tree before continuing to Phase 1.

---

### Optional: build it as a fleet instead of a single session

If you want parallelism, run this as an orchestrator with scoped sub-agents rather than one
session. Phases 1, 2, and 3 have no dependencies on each other and can be built concurrently:

- a **test-writer** sub-agent (scoped to `tests/**`) for Phase 1,
- a **schema** sub-agent (scoped to `coord/schema/**`) for Phase 2,
- a **hooks** sub-agent (scoped to `hooks/**` + `.github/hooks/**`) for Phase 3.

Give each an isolated worktree, let them report back on completion, then you integrate and
proceed to the dependent phases (4→5→6→7) sequentially. This is the repo dogfooding its own
pattern — but single-session is completely fine for a build this size; only shard if you want
the practice.
