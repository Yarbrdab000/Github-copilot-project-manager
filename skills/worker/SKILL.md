---
name: worker
description: "Bootstrap into the control plane, claim a task, work in small units within your owned paths, checkpoint frequently to stay reconciled, and finish by recording task status and reporting back."
---

# Worker

You do the actual work on one branch/worktree, within your **owned paths** only. Load the
`coordination-protocol` skill first, then follow this loop.

## Bootstrap

- The `sessionStart` hook registers you and writes your first heartbeat. To do it by hand:
  `coord register --session $ID --role <role> --branch <branch> --paths '<globs>'`.
- Run `coord checkpoint --session $ID` once up front to pull the current `desired` state and
  any messages before you start.

## Claim work

- `coord tasks` — see what is open.
- `coord claim --session $ID --task <id>` — claim atomically. If it fails because of unmet
  dependencies or another session, pick a different task; never work an unclaimed task.

## Work in small units

- Make one small, coherent change at a time, only inside your owned paths. Writes outside them
  are denied by the `preToolUse` hook — that is expected, not a bug.
- If a change needs a shared resource, take a lease first:
  `coord lock acquire --session $ID --resource <name> --ttl <sec>`, and release it when done.

## Checkpoint frequently

Run the checkpoint ritual (from `coordination-protocol`) between units and during long work:
`coord checkpoint --session $ID` → halt if `stop` is non-empty → act only on `messages` →
reconcile to `desired`. Checkpointing also heartbeats, which keeps your claims and locks from
being reaped out from under you.

## Finish

- Record the outcome: `coord complete --session $ID --task <id> --status done` (or `failed`).
- Report back to the orchestrator when useful:
  `coord send --from $ID --to orchestrator --body "<result>" --as-of <desired_version>`.
- Do a final `coord checkpoint --session $ID` before you stop.

## Self-continue directive

After every `coord checkpoint`, look at the `continue` field it prints (AUTONOMY_SPEC §6):
if it is `true` — you still hold a `claimed` task that is not `done` — **immediately continue
the next unit of work; do not yield the turn.** Only yield when: your task reaches `done`
(`continue` goes `false`), a `stop` flag is set at checkpoint, or you must `escalate` (raise a
`blocker`/`fork` and stop to await a decision). Do not sit idle mid-task waiting to be re-prompted.

**Never open a human-prompt modal (`ask_user`) to ask a question — it blocks you and the cockpit
cannot clear it.** Route every question to the human through `coord escalate` (see
`coordination-protocol`); their answer comes back to you as a checkpoint message once they
`coord resolve` it.
