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

## Where to go next

- A concrete, worked end-to-end scenario:
  [`examples/research-and-migrate/`](../examples/research-and-migrate/README.md).
- The exact behavior of every command: [`protocol.md`](./protocol.md).
- How the hook enforces write scope: [`hooks/README.md`](../hooks/README.md).
