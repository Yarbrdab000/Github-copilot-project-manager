# Quickstart

Two walkthroughs: a **two-session peer** flow on the filesystem control plane (fallback mode),
and an **orchestrator + workers** flow (native hub-and-spoke). For the design behind them see
[`architecture.md`](./architecture.md); for the full command surface see
[`protocol.md`](./protocol.md).

## Setup: the `coord` alias

`coord` is just `python coord/coord.py`. Define an alias once per shell so the commands below
are copy-paste clean. The function form below picks `python3` when present and falls back to
`python`, so it works on Linux, macOS, and Windows Python installs:

**bash / zsh**
```sh
coord() { command -v python3 >/dev/null 2>&1 && python3 coord/coord.py "$@" || python coord/coord.py "$@"; }
```

**PowerShell**
```powershell
function coord { python coord/coord.py @args }
```

Run all commands from the repo root (the control plane is created at `.coordination/`, which
`.gitignore` already ignores). To keep an experiment out of your real control plane, set
`COORD_ROOT` to a scratch directory first.

---

## Walkthrough A — two peer sessions

Two independent, long-lived sessions — a **researcher** (read-only) and an **editor**
(write-scoped) — share one repo. The researcher investigates; the editor builds once the
research is done. They never talk directly; they coordinate through the control plane.

### 1. Initialize (once, by whoever starts first)

```sh
coord init
```

### 2. Each session registers its identity

```sh
coord register --session researcher --role researcher --branch research/formatting --paths "docs/findings/**"
coord register --session editor    --role editor     --branch feat/mapper        --paths "src/mapper/**,tests/mapper/**"
```

Each session's `--paths` are the only paths it may write; the `preToolUse` hook enforces this.

### 3. Put the work on the board, with a dependency

```sh
coord add-task --id research-formatting --desc "document the source formatting rules"
coord add-task --id write-mapper        --desc "build the field mapper" --deps research-formatting
```

`write-mapper` can't be claimed until `research-formatting` is `done`.

### 4. Researcher claims, works in small units, checkpoints, completes

```sh
coord claim --session researcher --task research-formatting
# ... investigate; write notes into docs/findings/ ...
coord checkpoint --session researcher     # heartbeat, check stop-flags, read desired state
coord complete --session researcher --task research-formatting --status done
```

### 5. Editor's dependency is now satisfied

```sh
coord claim --session editor --task write-mapper   # succeeds now that the dep is done
```

Try it before step 4 and it is denied:
```
coord: task 'write-mapper' blocked on unmet deps: ['research-formatting']   # exit 1
```

### 6. Coordinate a shared resource with a lease

If both sessions must touch one shared file, guard it with a lease so only one holds it:

```sh
coord lock acquire --session editor --resource shared/config.json --ttl 120
# ... edit shared/config.json ...
coord lock release --session editor --resource shared/config.json
```

### 7. Check the whole fleet at any time

```sh
coord status
```

---

## Walkthrough B — orchestrator + workers

One **orchestrator** holds the plan and the desired state; **workers** reconcile toward it. In
native hub-and-spoke the orchestrator is a parent agent delegating to sub-agents (see the
[`orchestrator`](../skills/orchestrator/SKILL.md) and [`worker`](../skills/worker/SKILL.md)
skills); the same control-plane commands work for peer orchestration too.

### 1. Orchestrator sets up the plane and the desired state

```sh
coord init
coord register --session orch --role orchestrator --branch main --paths ""
coord state set --session orch --key target_palette --value '"v2"'    # version -> 1
```

### 2. Orchestrator lays out tasks

```sh
coord add-task --id research-formatting --desc "research the formatting rules"
coord add-task --id write-mapper --desc "apply them in the mapper" --deps research-formatting
```

### 3. Workers register and pull work

Each worker loads the coordination-protocol + worker skills, registers, then claims:

```sh
coord register --session w1 --role researcher --branch research/fmt --paths "docs/findings/**"
coord claim --session w1 --task research-formatting
```

### 4. The orchestrator steers by changing desired state

Rather than sending a command that could go stale, the orchestrator updates `desired`. Workers
pick it up at their next checkpoint:

```sh
coord state set --session orch --key target_palette --value '"v3"'    # version -> 2
```

If the orchestrator also sent an older message, it auto-goes-stale:

```sh
coord send --from orch --to w1 --body "use palette v1" --as-of 1     # now older than v2
coord checkpoint --session w1
# -> messages: [] ... "stale_messages_skipped": 1   (the v1 message was filtered out)
```

### 5. Orchestrator reaps dead workers

If a worker crashes, its lease and its claimed task shouldn't wedge the fleet. Periodically:

```sh
coord reap
# { "reaped_locks": [...], "requeued_tasks": [...] }   # requeued tasks are open again
```

### 6. Halt cleanly

To stop the whole fleet (e.g. exit criteria met, or something went wrong):

```sh
coord stop                       # global halt
coord checkpoint --session w1    # prints state with "stop": ["GLOBAL"], exits 3
coord resume                     # clear it to continue
```

---

## Walkthrough C — navigator proposes, human approves

A **navigator** is the fleet's design partner: it deliberates with the human but has no
authority to act. Its only lever is a *proposal* to change `desired.json`, which a human
approves. This runs the full **propose → approve → propagate** loop, including how an approved
change with `--invalidates` requeues in-flight work instead of stomping it.

Use a scratch `COORD_ROOT` so this stays out of any real plane:

```sh
export COORD_ROOT="$(mktemp -d)"    # PowerShell: $env:COORD_ROOT = Join-Path $env:TEMP ([guid]::NewGuid())
```

### 1. Init the plane; register a worker and a navigator

```sh
coord init
coord register --session w1  --role editor    --branch feat/mapper --paths "src/**"
coord register --session nav --role navigator --branch main         --paths ""
coord state set --session orch --key target_palette --value '"v2"'   # -> state version -> 1
```

### 2. Put work on the board and have the worker claim it

```sh
coord add-task --id write-mapper --desc "build the field mapper"
coord claim --session w1 --task write-mapper    # -> w1 claimed write-mapper
```

### 3. The navigator proposes a change — it does NOT take effect yet

```sh
coord state propose --session nav --key target_palette --value '"v3"' \
    --invalidates write-mapper --note "v3 changes the mapper contract"
# -> proposed 1783358576540003700: desired.target_palette: "v2" -> "v3" (pending; version unchanged at 1)
# ->   invalidates: ['write-mapper']

coord state show
# -> "version": 1 — propose writes a pending record; it does NOT move the fleet

coord state proposals
# -> 1783358576540003700  from=nav  target_palette: "v2" -> "v3"  invalidates=write-mapper  note=v3 changes the mapper contract
```

### 4. A human approves — now it propagates

The navigator **cannot** approve its own proposal (the write-scope hook denies it); a human or
the orchestrator runs `approve`:

```sh
coord state approve --session orch --id 1783358576540003700
# -> approved 1783358576540003700: desired.target_palette applied; state version -> 2
# ->   requeued: [{'task': 'write-mapper', 'notified': 'w1'}]
```

Three things happened together — the version bumped, the task requeued, and the claimant was
notified:

```sh
coord state show
# -> "version": 2, desired.target_palette now "v3"

coord tasks
# -> [      open] write-mapper   <- folded back to OPEN, re-claimable

coord checkpoint --session w1
# -> "desired_version": 2, plus a FRESH message (as_of=2) in w1's inbox:
#    "task 'write-mapper' invalidated by approved proposal 1783358576540003700 ...; stop and re-claim"
```

### 5. Stale work can't be completed

If `w1` ignored the notice and tried to finish the old task, the **stale-completion guard**
refuses it — the task is no longer claimed by `w1`:

```sh
coord complete --session w1 --task write-mapper --status done
# -> coord: cannot complete 'write-mapper': it is 'open' (claimed_by=w1), not claimed by 'w1'
#    — it may have been requeued/invalidated; re-claim before completing        # exit 1
```

`w1` must re-`claim write-mapper` before it can work or complete it — the approved re-plan, not
stale momentum, wins.

### 6. Rejection changes nothing

A proposal the human doesn't want is rejected, and the desired state is untouched:

```sh
coord state propose --session nav --key target_palette --value '"v4"' --note "alternative direction"
# -> proposed 1783358611618012800: desired.target_palette: "v3" -> "v4" (pending; version unchanged at 2)

coord state reject --session orch --id 1783358611618012800 --reason "staying on v3"
# -> rejected 1783358611618012800 (version unchanged)

coord state show
# -> still "version": 2, desired.target_palette "v3"
```

The navigator influences the fleet **only** by proposing a change a human approves — it never
dispatches, merges, edits, or self-approves. See
[`agents/navigator.agent.md`](../agents/navigator.agent.md) and
[`skills/navigator/SKILL.md`](../skills/navigator/SKILL.md) for the role, and
[`architecture.md`](./architecture.md) §7 for the design.

---

## Walkthrough D — autonomous run to an escalation

This traces a coded acceptance gate that keeps failing all the way through `coord run` to an
open, human-facing escalation — no human babysits every step; the automation stops and asks
exactly when it should. Use a scratch `COORD_ROOT`:

```sh
export COORD_ROOT="$(mktemp -d)"    # PowerShell: $env:COORD_ROOT = Join-Path $env:TEMP ([guid]::NewGuid())
coord init
coord register --session orch --role orchestrator --branch main      --paths ""
coord register --session w1   --role editor       --branch feat/thing --paths "src/**"
```

### 1. The orchestrator adds a task with a failing acceptance gate

`--max-attempts 1` means the very first failing verify already exhausts the budget:

```sh
coord add-task --id ship-thing --desc "ship the thing" \
    --verify "python -c \"import sys; sys.exit(1)\"" --max-attempts 1
# -> added task ship-thing
```

### 2. The worker claims it and (wrongly) marks it done

```sh
coord claim --session w1 --task ship-thing
coord complete --session w1 --task ship-thing
# -> w1 claimed ship-thing
# -> task ship-thing -> done
```

`complete` only records the worker's *claim* that it's done — the coded gate hasn't run yet.

### 3. `coord run --once` drives the tick that catches it

```sh
coord run --once --interval 0
```
```json
{
  "reaped": [],
  "verified": [],
  "requeued": [],
  "dispatched": [],
  "nudged": [],
  "failed": [
    { "task": "ship-thing", "attempts": 1, "escalation": "1783452744369564200" }
  ],
  "awaiting_decision": [
    {
      "eid": "1783452744369564200",
      "from": "tick",
      "kind": "blocker",
      "task": "ship-thing",
      "body": "task 'ship-thing' failed verify 1 time(s) (max_attempts=1); marked failed",
      "status": "open",
      "created": "2026-01-01T00:00:00Z",
      "as_of": 0,
      "resolved_note": null
    }
  ]
}
```

The verify failed, `attempts` (1) already met `max_attempts` (1), so `tick` marked the task
`failed` and opened a `blocker` escalation in the same pass — it did not requeue-and-retry
forever, and it did not silently leave `ship-thing` looking `done`.

### 4. The board and the escalation queue both reflect it

```sh
coord tasks
# -> [    failed] ship-thing           <- w1   ship the thing

coord escalations
# -> [ blocker] 1783452744369564200 from=tick task=ship-thing  task 'ship-thing' failed verify 1 time(s) (max_attempts=1); marked failed
```

### 5. A human (or the orchestrator on their behalf) resolves it

```sh
coord resolve --id 1783452744369564200 --note "known flaky check, disabling gate and re-scoping ship-thing"
# -> resolved 1783452744369564200

coord escalations
# -> (no open escalations)
```

`tick` never retried past `max_attempts` on its own, and it never silently dropped the failure —
it escalated once and stopped, exactly the honest runtime seam [`architecture.md`](./architecture.md)
§8 describes: the control plane records the directive and the failure; a human makes the call.

---

## Where to go next

- A concrete, worked end-to-end scenario:
  [`examples/research-and-migrate/`](../examples/research-and-migrate/README.md).
- The exact behavior of every command: [`protocol.md`](./protocol.md).
- How the hook enforces write scope: [`hooks/README.md`](../hooks/README.md).
