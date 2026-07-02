# Example — research-and-migrate

A worked end-to-end scenario for the **filesystem control plane** (fallback mode): a
**read-only research session** investigates something, and a **write session** uses the result
to make scoped edits. The two never talk directly — they coordinate through `.coordination/`.

This mirrors a real use case: one session researches how a source system formats its output,
and a second session applies those rules in a migration/mapper — safely, in parallel, without
clobbering each other.

## Cast

| Session | Role | Branch | Owned paths (may write) |
|---|---|---|---|
| `researcher` | research (read-only) | `research/formatting` | `docs/findings/**` |
| `editor` | migrate (write-scoped) | `feat/mapper` | `src/mapper/**`, `tests/mapper/**` |

The `researcher` agent has **no edit/execute tools** at all (see
[`agents/researcher.agent.md`](../../agents/researcher.agent.md)); the `editor` is
write-scoped and the `preToolUse` hook denies any write outside its owned paths.

## Prerequisites

Set up the `coord` alias from the [quickstart](../../docs/quickstart.md#setup-the-coord-alias)
and run from the repo root. To keep this experiment isolated from your real control plane,
point `COORD_ROOT` at a scratch directory first:

```sh
export COORD_ROOT="$(mktemp -d)/.coordination"     # bash/zsh
# PowerShell: $env:COORD_ROOT = "$env:TEMP\coord-example\.coordination"
```

## The run

### 1. Initialize and register both sessions

```sh
coord init
coord register --session researcher --role research --branch research/formatting --paths "docs/findings/**"
coord register --session editor     --role migrate  --branch feat/mapper --paths "src/mapper/**,tests/mapper/**"
```

Expected:
```
initialized control plane at /.../.coordination
registered researcher as research on branch 'research/formatting' owning ['docs/findings/**']
registered editor as migrate on branch 'feat/mapper' owning ['src/mapper/**', 'tests/mapper/**']
```

### 2. Lay out the work with a dependency

The mapper work depends on the research being done first:

```sh
coord add-task --id research-formatting --desc "document source formatting rules"
coord add-task --id write-mapper --desc "apply formatting rules in the mapper" --deps research-formatting
coord tasks
```

Expected:
```
added task research-formatting
added task write-mapper
  [      open] research-formatting                     document source formatting rules
  [      open] write-mapper        deps=research-formatting  apply formatting rules in the mapper
```

### 3. The dependency is enforced

The editor tries to jump ahead and is blocked:

```sh
coord claim --session editor --task write-mapper
```

Expected (exit 1):
```
coord: task 'write-mapper' blocked on unmet deps: ['research-formatting']
```

### 4. Researcher does its slice

The researcher claims, checkpoints as it works, records findings **only under its owned path**
(`docs/findings/`), and marks the task done:

```sh
coord claim --session researcher --task research-formatting
coord checkpoint --session researcher      # heartbeat + read desired state + stop-flags
# ... writes docs/findings/formatting.md (allowed: inside owned_paths) ...
coord complete --session researcher --task research-formatting --status done
```

Expected:
```
researcher claimed research-formatting
{ "session": "researcher", ... "stop": [], "desired_version": 0, "messages": [], "stale_messages_skipped": 0 }
task research-formatting -> done
```

### 5. Editor is unblocked

Now the dependency is satisfied, so the editor can claim its task:

```sh
coord claim --session editor --task write-mapper
```

Expected:
```
editor claimed write-mapper
```

### 6. Steering via declarative state (not stale messages)

Midway, the target changes. The orchestrator/human updates **desired state** rather than
firing a command that could go stale, and an older in-flight message is filtered out
automatically:

```sh
coord state set --session researcher --key target_format --value '"v2"'    # version -> 1
coord send --from researcher --to editor --body "use format v1" --as-of 1  # tied to v1-era
coord state set --session researcher --key target_format --value '"v3"'    # version -> 2
coord send --from researcher --to editor --body "now use v3" --as-of 2
coord checkpoint --session editor
```

Expected — only the current message survives; the `v1` one is skipped as stale:
```
desired.target_format set; state version -> 1
queued message ... -> editor
desired.target_format set; state version -> 2
queued message ... -> editor
{
  "session": "editor",
  "stop": [],
  "desired_version": 2,
  "desired": { "target_format": "v3" },
  "messages": [ { "body": "now use v3", "as_of": 2, ... } ],
  "stale_messages_skipped": 1
}
```

The editor reconciles to `target_format = v3` and ignores the outdated `v1` instruction — the
core anti-stale-message guarantee.

### 7. Finish and inspect

```sh
coord complete --session editor --task write-mapper --status done
coord status
```

Expected — both tasks done, sessions alive, no stop-flags:
```
task write-mapper -> done
control plane: /.../.coordination
stop flags: none
sessions:
  researcher       research       ALIVE  hb ... ago  branch=research/formatting
  editor           migrate        ALIVE  hb ... ago  branch=feat/mapper
locks:
tasks:
  [      done] research-formatting <- researcher  document source formatting rules
  [      done] write-mapper        <- editor deps=research-formatting  apply formatting rules in the mapper
```

## What this demonstrates

- **Scoped identity + write enforcement** — each session declares `owned_paths`; the hook
  denies out-of-scope writes, so the read-only researcher and the write-scoped editor can't
  step on each other.
- **Dependency ordering** — `write-mapper` was un-claimable until `research-formatting` was
  `done`.
- **Declarative steering with staleness filtering** — changing `desired` state re-targets the
  editor, and a message written against an older desired-state version is dropped, not acted
  on.
