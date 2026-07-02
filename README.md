# agent-coordination-skills

A drop-in coordination layer for running **multiple GitHub Copilot agent sessions in parallel
on one repository** — without them stalling, drifting, clobbering each other's branches, or
acting on stale cross-session messages.

It ships two coordination modes and is opinionated about when to use each:

- **Native mode (preferred): hub-and-spoke.** One *orchestrator* agent delegates scoped work
  to *worker* sub-agents that run in isolated contexts and report back through Copilot's native
  sub-agent lifecycle. Coordination is vertical (parent ↔ child), so there is no lateral
  peer-messaging problem to solve.
- **Fallback mode: filesystem control plane.** For genuine long-running *peer* sessions that
  are **not** in a parent/child tree, sessions coordinate through a shared on-disk control
  plane (`.coordination/`) they read at defined checkpoints. This mode exists because peer
  sessions have no preemption and no delivery guarantees.

The guiding idea: **invariants live in tooling, not prose.** Instructions *teach* the protocol;
the `coord` CLI, the hooks, and git *enforce* it — so a model under context pressure can't skip
them. See [`docs/architecture.md`](docs/architecture.md) for the full design.

## Which mode should I use?

| Situation | Mode |
|---|---|
| One driver decomposing work into scoped slices | **Native** hub-and-spoke |
| Sub-agents that live and die inside one parent run | **Native** |
| Two+ independent, long-lived sessions started separately | **Fallback** control plane |
| Peers that must survive many turns and periodically re-sync | **Fallback** |

When in doubt, start native. Reach for the control plane only when you have true peers.

## Requirements

- **Python 3.8+** (standard library only — no third-party runtime dependencies).
- **pytest** only if you want to run the test suite (a dev dependency).
- The `sessionStart` hook auto-registers a session via `bash`; on Windows that means Git Bash
  (see [`hooks/README.md`](hooks/README.md)). Everything else runs on plain `python`.

## Quickstart

Run from the repo root. `coord` is just `python coord/coord.py`; define an alias once (this
form uses `python3` when present, else `python`, so it works on Linux/macOS/Windows):

```sh
coord() { command -v python3 >/dev/null 2>&1 && python3 coord/coord.py "$@" || python coord/coord.py "$@"; }

# 1. Create the control plane (safe to re-run)
coord init

# 2. Register two sessions with the paths each is allowed to write
coord register --session researcher --role research --branch research/fmt --paths "docs/findings/**"
coord register --session editor     --role migrate  --branch feat/mapper --paths "src/mapper/**"

# 3. Put work on the board, with a dependency
coord add-task --id research-formatting --desc "research formatting"
coord add-task --id write-mapper        --desc "build mapper" --deps research-formatting

# 4. Research first; the dependent task unblocks once it's done
coord claim    --session researcher --task research-formatting
coord complete --session researcher --task research-formatting --status done
coord claim    --session editor     --task write-mapper

# 5. See the whole fleet
coord status
```

> On PowerShell, define the alias as `function coord { python coord/coord.py @args }` instead.

`.coordination/` is created at the repo root and is already git-ignored. For the full 2-session
and orchestrator walkthroughs see [`docs/quickstart.md`](docs/quickstart.md); for a worked
end-to-end scenario see [`examples/research-and-migrate/`](examples/research-and-migrate/README.md).

## How agents use it

- **Skills** ([`skills/`](skills/)) teach the protocol. Every agent loads
  [`coordination-protocol`](skills/coordination-protocol/SKILL.md) (identity + the checkpoint
  ritual) plus its role skill — [`orchestrator`](skills/orchestrator/SKILL.md) or
  [`worker`](skills/worker/SKILL.md).
- **Custom agents** ([`agents/`](agents/)) are scoped by tools: the
  [`researcher`](agents/researcher.agent.md) is read-only, the
  [`editor`](agents/editor.agent.md) is write-scoped, and the
  [`orchestrator`](agents/orchestrator.agent.md) excludes heavy edits so it delegates.
- **Hooks** ([`.github/hooks/coordination.json`](.github/hooks/coordination.json)) enforce it
  at runtime: `sessionStart` auto-registers a session and `preToolUse` denies any write outside
  the session's owned paths. See [`hooks/README.md`](hooks/README.md).

## Repository layout

```
coord/          # the coord CLI + JSON Schemas for its records
skills/         # protocol + role skills every agent loads
agents/         # scoped custom-agent definitions (orchestrator, researcher, editor)
hooks/          # write-scope guard + session-register scripts
.github/hooks/  # hook wiring (coordination.json)
docs/           # architecture, full protocol reference, quickstart
examples/       # a worked research-and-migrate scenario
tests/          # pytest suite (control plane + hook)
```

## Testing

```sh
python -m pytest -q
```

The suite exercises the control plane (dependency blocking, atomic claim, lease deny→steal→reap,
staleness filtering, stop-flag halt) and the write-scope hook (in-scope allow, out-of-scope and
traversal deny), plus JSON-Schema validation of the record formats.

## License

[MIT](LICENSE).
