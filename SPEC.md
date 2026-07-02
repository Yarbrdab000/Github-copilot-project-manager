# SPEC — `agent-coordination-skills`

**This file is the source of truth for the build.** The building agent must not invent
scope beyond it. When something here is ambiguous, stop and ask rather than guessing.

Rename the repo/package freely; `agent-coordination-skills` is the working name (matches
the `*-skills` convention).

---

## 1. What this repo is

A drop-in coordination layer for running **multiple GitHub Copilot agent sessions in
parallel on one repository** without them stalling, drifting, clobbering each other's
branches, or acting on stale cross-session messages.

It ships two coordination modes and is opinionated about when to use each:

- **Native mode (preferred): hub-and-spoke.** One *orchestrator* agent delegates scoped
  work to *worker* sub-agents that run in isolated contexts and report back through
  Copilot's native sub-agent lifecycle events. Coordination is vertical (parent↔child),
  so there is no lateral peer-messaging problem to solve.
- **Fallback mode: filesystem control plane.** For genuine long-running *peer* sessions
  that are not in a parent/child tree, sessions coordinate through a shared on-disk
  control plane (`.coordination/`) that they read at defined checkpoints. This mode
  exists because peer sessions have no preemption and no delivery guarantees.

## 2. Design principles (do not violate)

1. **Declarative state over imperative messages.** The primary coordination channel is a
   versioned `desired.json` that sessions reconcile toward at checkpoints — not commands
   dropped into a queue. A command goes stale; "current target is X" read fresh does not.
2. **Deterministic core, probabilistic shell.** Coordination invariants live in tooling
   (the `coord` CLI, hooks, git) — never in prose the model can skip under context
   pressure. Instructions *teach* the protocol; tools *enforce* it.
3. **Append-only ledgers.** Task board and inboxes are JSONL appends, never
   read-modify-write on a shared file. Mutable state is written atomically (temp +
   `os.replace`).
4. **Leases, not locks.** Every lock has a TTL and is steal-able **only** when the lease
   has expired **and** the holder's heartbeat is provably stale. No deadlock from a dead
   session.
5. **Small units + frequent checkpoints.** Because sessions can't be preempted, work is
   broken into small units with a coordination beat (`coord checkpoint`) between each.

## 3. The five failure modes → the mechanism that fixes each

| Failure mode | Fix (in this repo) |
|---|---|
| Agents don't stop / wander off task | Scoped tools per agent (read-only workers can't edit) + `pre-tool-use` write-scope hook + orchestrator holds exit criteria |
| Lose context | Orchestrator holds the durable plan; workers start fresh with only their slice; per-agent skills preloaded at startup |
| Overwrite each other's branches | Worktree-per-worker (native) + write-scoped tools + `pre-tool-use` hook rejecting out-of-owned-path writes |
| Don't coordinate | Hub-and-spoke via orchestrator + lifecycle events; no lateral messaging |
| Append-only queue / stale messages | Native `steering` for live redirect; fallback: per-recipient inboxes with `as_of` + TTL staleness filtering, surfaced only at checkpoints |

## 4. Locked reference component — DO NOT reinvent

`reference/coord.reference.py` is a **tested, working** implementation of the filesystem
control plane. Place it at `coord/coord.py` **unchanged** (bugfixes welcome via PR, but do
not rewrite from scratch — it already handles atomic claims, lease-stealing, heartbeat
reaping, and staleness filtering, and a naive reimplementation reintroduces a nested-lock
visibility bug). Your job is to build the test suite that proves it and everything around it.

CLI surface (already implemented — build docs/tests to match, don't change signatures
without updating SPEC): `init, register, heartbeat, checkpoint, state {show,set}, add-task,
tasks, claim, complete, lock {acquire,release}, send, inbox, stop, resume, status, reap`.

Control-plane layout it creates under `.coordination/`: `registry/<session>.json`,
`inbox/<session>.jsonl`, `cursor/<session>.json`, `locks/<name>.lockdir/meta.json`,
`state/desired.json`, `board/{tasks,events}.jsonl`, `control/STOP[-<session>]`.

## 5. File manifest (build all of these)

```
README.md                              # what it is, quickstart, mode-selection guidance
LICENSE                                # MIT
.gitignore                             # ignore .coordination/ runtime contents (keep .gitkeep), tmp files
coord/
  coord.py                             # = reference/coord.reference.py, verbatim
  schema/
    registry.schema.json               # JSON Schema for a registry entry
    task.schema.json                   # JSON Schema for a task-ledger event
    message.schema.json                # JSON Schema for an inbox message
    desired-state.schema.json          # JSON Schema for desired.json
skills/
  coordination-protocol/SKILL.md       # the shared protocol + checkpoint ritual EVERY agent loads
  orchestrator/SKILL.md                # how to run the fleet: plan, delegate, reap, integrate
  worker/SKILL.md                      # bootstrap → claim → work-in-small-units → checkpoint → finish
agents/
  orchestrator.agent.md                # custom-agent def: planning tools, delegation, NO heavy edits
  researcher.agent.md                  # read-only worker (grep/glob/view only)
  editor.agent.md                      # write-scoped worker (edit/bash within owned paths)
hooks/
  README.md                            # how the hooks enforce scope; install notes
  scripts/write_scope_guard.py         # pre-tool-use: deny writes outside the session's owned paths
  scripts/session_register.sh          # sessionStart: coord register + heartbeat
.github/
  hooks/coordination.json              # wires sessionStart + preToolUse to the scripts above
  copilot-instructions.md              # standing guardrails so agents follow the protocol
docs/
  architecture.md                      # topology, native-vs-fallback, failure-mode mapping (expand §2–3)
  protocol.md                          # full control-plane protocol spec + every coord command
  quickstart.md                        # 2-session and orchestrator walkthroughs
examples/
  research-and-migrate/README.md       # worked example: a read-only research session + a write session
tests/
  test_coord.py                        # pytest: exercise the CLI (see §7 acceptance)
  test_write_scope_guard.py            # pytest: hook allows in-scope, denies out-of-scope + traversal
```

## 6. Component contracts

**`hooks/scripts/write_scope_guard.py`** (pre-tool-use hook). Reads the hook payload on
stdin. Per the verified Copilot contract, command-hook stdin is JSON with `toolName` and
`toolArgs` (where **`toolArgs` is itself a JSON string** and must be parsed). Behavior:
- Resolve the acting session by matching the payload `cwd` against registry entries'
  `worktree` (allow `COORD_SESSION` env override). Find its `owned_paths`.
- For file-writing tools (`edit`, `create`, `str_replace`, `write`, `create_file`,
  `apply_patch`): extract the target path (`path`/`file_path`/`filePath`/`filename`),
  resolve against `cwd`, and **deny** if it escapes the worktree or matches no
  `owned_paths` glob.
- For `bash`: best-effort deny of `git push`, `git checkout`/`switch` to a branch other
  than the session's, and redirects to absolute paths outside the worktree. Otherwise allow.
- Allow all read tools.
- Emit `{"permissionDecision":"allow"}` or
  `{"permissionDecision":"deny","permissionDecisionReason":"..."}` to stdout, exit 0.
  (Denials must be fail-closed; if the payload can't be parsed, default to allow so a
  broken hook never wedges every tool call — matches Copilot's timeout=fail-open stance,
  but log to stderr.)

**`.github/hooks/coordination.json`** — `version: 1`, wiring `sessionStart` →
`session_register.sh` and `preToolUse` → `write_scope_guard.py`, with both `bash` and
`powershell` keys so it runs cross-platform. Must be valid against Copilot's hook schema.

**`agents/*.agent.md`** — each defines name, description, a tight system prompt that loads
`skills/coordination-protocol` + its role skill, and a **scoped tool list**. `researcher`
gets read-only tools only. `orchestrator` excludes heavy edit tools so it delegates.

**`skills/*/SKILL.md`** — YAML frontmatter (`name`, `description`) + body. The protocol
skill defines the **checkpoint ritual** verbatim: at every checkpoint boundary run
`coord checkpoint --session $ID`; if `stop` is non-empty, halt; act only on returned
`messages`; reconcile behavior to returned `desired`.

## 7. Acceptance criteria (the build is done when these pass)

See `reference/ACCEPTANCE.md` for the exact scenarios (they already pass against the
reference implementation). At minimum, `tests/` must assert:
1. Dependency blocking: claiming a task with an unmet dep fails.
2. Atomic claim: two claims of one task → exactly one winner.
3. Lease deny then steal: a held lease denies; after TTL expiry **and** holder-heartbeat
   staleness, `reap` reclaims it and a live session can re-acquire.
4. Staleness filter: a message with `as_of` < current `desired.version` is skipped by
   `checkpoint`; the current one is surfaced.
5. Stop flag: global `STOP` makes `checkpoint` exit 3.
6. Hook scope: an in-owned-path write is allowed; an out-of-path write and a `../`
   traversal are denied.

`README.md` quickstart must run copy-paste clean. All hook JSON must validate. All
`coord/schema/*.json` must be valid JSON Schema and match what the CLI actually writes.
