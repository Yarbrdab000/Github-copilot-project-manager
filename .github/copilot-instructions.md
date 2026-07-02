# Copilot instructions — building `agent-coordination-skills`

You are building this repository from `SPEC.md` and `BUILD_PLAN.md`. These instructions are
guardrails; they outrank your own preferences when they conflict.

## Ground rules

- **`SPEC.md` is the source of truth.** Build exactly its file manifest (§5) and satisfy its
  acceptance criteria (§7). Do not add scope, extra abstractions, frameworks, or "nice to
  haves" that aren't in the spec. If the spec is ambiguous or seems wrong, **stop and ask** —
  do not guess and build.
- **`reference/coord.reference.py` is locked.** Copy it to `coord/coord.py` **verbatim**. Do
  not rewrite it. If you believe there's a bug, write a failing test first and raise it — do
  not silently "improve" it. A naive reimplementation reintroduces known concurrency bugs.
- **Work in small, checkpointed commits**, one `BUILD_PLAN.md` phase at a time. After each
  phase: run the tests for that phase, then stop and report status before starting the next.
  Do not run the whole plan in one uninterrupted pass.
- **Verify, don't assert.** When you write the tests, actually run them (`pytest -q`) and
  paste real output. When you write hook JSON or JSON Schema, validate it. Never claim
  something passes without having run it.
- **No secrets, no network calls** in scripts or tests. Everything runs offline against the
  filesystem. Hooks and the CLI are stdlib-only Python 3.8+ (no third-party deps except
  `pytest` as a dev dependency).

## Platform facts (verified — build against these, don't re-derive)

- Copilot hook config lives at `.github/hooks/*.json`, `version: 1`, with a `hooks` object
  keyed `sessionStart`/`preToolUse`/`postToolUse`/etc. Each entry has `type: "command"` plus
  `bash` and/or `powershell` keys.
- A command hook receives its payload as **JSON on stdin**. For tool hooks it includes
  `toolName` and `toolArgs`, and **`toolArgs` is a JSON string** — parse it before reading
  fields like `path` or `command`.
- A `preToolUse` hook denies a call by writing
  `{"permissionDecision":"deny","permissionDecisionReason":"..."}` to stdout; allow with
  `{"permissionDecision":"allow"}` or empty output. Keep hooks fast and fail-open on parse
  errors (log to stderr) so a broken hook never blocks every tool call.

## Definition of done

Every acceptance scenario in `SPEC.md` §7 has a passing test with pasted output; the README
quickstart runs clean; the file manifest is complete; nothing outside the manifest was added.
When all of that is true, open a PR summarizing what was built and which acceptance checks
passed. Do not merge to the default branch yourself.
