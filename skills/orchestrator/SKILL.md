---
name: orchestrator
description: "Run a fleet of parallel Copilot worker sessions: plan the work, publish declarative desired-state and tasks, reap dead sessions, and integrate finished branches. Delegates all heavy editing to workers."
---

# Orchestrator

You own the plan and the shared state; you do **not** do heavy editing yourself — you delegate
that to `worker` sessions (`researcher`, `editor`). Load the `coordination-protocol` skill
first, then follow this.

## Bootstrap the control plane

1. `coord init` — create the control plane under `.coordination/`.
2. Register yourself:
   `coord register --session orchestrator --role orchestrator --branch <branch>`.
3. Declare the goal as **desired state** (not prose):
   `coord state set --session orchestrator --key <k> --value <json>`. Each `state set` bumps
   `desired_version`.
4. Put work on the board with dependencies:
   `coord add-task --id <id> --desc "<what>" --deps <id1,id2>`.

## Launch workers

- Give each worker its **own worktree** and a disjoint set of **owned paths**, so two workers
  can never write the same files. Worktree-per-worker is the isolation boundary.
- Assign roles: `researcher` for read-only investigation, `editor` for scoped edits.
- Each worker registers with its owned paths, e.g.
  `coord register --session editor --role editor --branch <b> --paths 'src/**,tests/**'`.

## Run the loop

At each of your checkpoints (follow the ritual in `coordination-protocol`):

1. `coord status` — who is alive, which locks are held, and the task board.
2. `coord reap` — release locks held by dead sessions and requeue tasks a crashed worker
   abandoned, so the fleet never wedges on a dead session.
3. Steer with **state, not chatter**: when the plan changes, `coord state set ...` (bumping
   the version) so every worker reconciles at its next checkpoint. Use
   `coord send --as-of <version>` only for targeted nudges.

## Integrate and finish

- When tasks are `done`, integrate the workers' branches (merge or PR each worktree branch).
- Hold the exit criteria: only wind down once the desired state is satisfied.
- Stop the fleet cleanly: `coord stop` (global) or `coord stop --session <id>` (one worker);
  undo with `coord resume`.
