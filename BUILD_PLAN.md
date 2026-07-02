# BUILD_PLAN — phased, checkpointed

Execute one phase at a time. After each phase, run its checks, commit, and **stop to report
status** before starting the next. This mirrors the coordination discipline the repo teaches:
small units, a checkpoint between each. Do not run the whole plan in one pass.

Reference the locked control-plane implementation in `reference/coord.reference.py` and the
authoritative `SPEC.md` throughout.

---

## Phase 0 — Scaffold  (no deps)
- Create the full directory tree from SPEC §5.
- Add `LICENSE` (MIT), `.gitignore` (ignore `.coordination/` runtime contents but keep a
  `.coordination/.gitkeep`; ignore `*.tmp.*`, `__pycache__/`, `.pytest_cache/`).
- Copy `reference/coord.reference.py` → `coord/coord.py` **verbatim**.
- **DoD:** tree matches manifest; `python3 coord/coord.py --help` prints usage.
- **Checkpoint & report.**

## Phase 1 — Control-plane tests  (deps: 0)
- Write `tests/test_coord.py` (pytest) covering SPEC §7.1–§7.5 by driving the CLI in a
  temp `COORD_ROOT`. Assert the exact behaviors, including exit code 3 on global STOP and
  the staleness skip.
- **DoD:** `pytest -q tests/test_coord.py` green; paste output.
- **Checkpoint & report.**

## Phase 2 — Schemas  (deps: 0)
- Write the four `coord/schema/*.json` as valid JSON Schema (draft 2020-12) matching what
  the CLI actually writes (inspect the reference file to get field names right).
- Add a tiny test that loads each schema and validates a sample the CLI produces.
- **DoD:** schemas are valid JSON Schema; sample-validation test green.
- **Checkpoint & report.**

## Phase 3 — Write-scope hook + tests  (deps: 0)
- Implement `hooks/scripts/write_scope_guard.py` per SPEC §6 (stdin `toolName`/`toolArgs`,
  cwd→session resolution, owned-path globbing, deny out-of-scope + `../` traversal, allow
  reads, fail-open on parse error).
- Implement `hooks/scripts/session_register.sh` (sessionStart → `coord register`/heartbeat).
- Write `.github/hooks/coordination.json` wiring both, cross-platform (`bash`+`powershell`).
- Write `tests/test_write_scope_guard.py` covering SPEC §7.6 by piping crafted payloads.
- **DoD:** hook tests green (paste output); `coordination.json` validates against Copilot's
  hook schema.
- **Checkpoint & report.**

## Phase 4 — Skills  (deps: understanding of Phases 0–3)
- Write the three `skills/*/SKILL.md` with YAML frontmatter. The protocol skill must state
  the **checkpoint ritual** verbatim (run `coord checkpoint`; halt on `stop`; act only on
  returned `messages`; reconcile to `desired`). Keep each skill tight and operational.
- **DoD:** frontmatter parses; ritual matches the CLI's actual `checkpoint` output shape.
- **Checkpoint & report.**

## Phase 5 — Agent definitions  (deps: 4)
- Write `agents/*.agent.md`: scoped tool lists (researcher read-only; editor write-scoped;
  orchestrator excludes heavy edits to force delegation), each loading the protocol skill +
  its role skill.
- **DoD:** tool scoping matches SPEC §6; researcher has no write/bash tools.
- **Checkpoint & report.**

## Phase 6 — Docs + example  (deps: all above)
- Write `docs/architecture.md`, `docs/protocol.md`, `docs/quickstart.md`,
  `examples/research-and-migrate/README.md`, and the top-level `README.md`.
- Expand SPEC §2–3 into architecture.md; document every `coord` command in protocol.md.
- **DoD:** README quickstart commands run copy-paste clean in a temp dir (paste the run).
- **Checkpoint & report.**

## Phase 7 — Final gate  (deps: all)
- Run the full test suite. Confirm every SPEC §7 acceptance scenario has a passing test.
- Confirm the manifest is complete and nothing extra was added.
- Open a PR with a summary + pasted acceptance results. **Do not self-merge.**
- **Checkpoint & report — done.**
