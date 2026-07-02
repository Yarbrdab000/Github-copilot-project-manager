# Coordinator Build Kit

A drop-in kit that turns a GitHub Copilot agent into the **builder** of the
`agent-coordination-skills` repo. You commit these files to an empty repo, paste the kickoff
prompt, and Copilot ships the whole thing — staying on-spec because the kit constrains it.

## What's in here

| File | Role |
|---|---|
| `SPEC.md` | **Source of truth.** Architecture, file manifest, component contracts, acceptance criteria. |
| `BUILD_PLAN.md` | Phased, checkpointed task plan the agent executes one phase at a time. |
| `KICKOFF.md` | The prompt you paste to start the build session. |
| `.github/copilot-instructions.md` | Standing guardrails the agent always reads (anti-drift, verify-don't-assert). |
| `reference/coord.reference.py` | **Tested, locked** control-plane implementation the agent must use verbatim. |
| `reference/ACCEPTANCE.md` | The exact scenarios (with real transcripts) the build must reproduce as tests. |

## How to use it

1. Create the empty GitHub repo and connect it to Copilot.
2. Commit the contents of this kit to the repo root (keep the `.github/` and `reference/`
   paths as-is).
3. Open a Copilot agent session with edit + bash tools on the repo.
4. Paste `KICKOFF.md` into the session.
5. The agent builds **Phase 0**, stops, and reports. Review, then tell it to continue. Repeat
   through Phase 7. It opens a PR at the end — you review and merge.

## Why it's shaped this way

The whole point of the repo being built is keeping agents on track, so the kit practices what
it preaches:

- **A spec the agent can't wander from.** `SPEC.md` + `copilot-instructions.md` pin scope so
  the agent builds what you asked for, not an over-engineered cousin of it.
- **A locked, pre-tested core.** The concurrency-critical control plane is already written and
  verified; the agent builds the shell around it instead of free-styling the hard part (which
  is where a naive build reintroduces subtle bugs).
- **Phase-by-phase checkpoints.** The agent stops and reports between phases instead of doing
  one long run-to-completion where it drifts and you find out too late.
- **Verify-don't-assert.** The agent must run tests and paste real output before claiming a
  phase is done.

## After the build

Once merged, `agent-coordination-skills` is itself the thing you use to coordinate real
parallel Copilot work (e.g. your Power BI formatting research session + your migration-tool
session). Native hub-and-spoke first; the filesystem control plane for genuine peer sessions.
