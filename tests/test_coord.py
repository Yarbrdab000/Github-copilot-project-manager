"""
Control-plane acceptance tests for `coord/coord.py` (SPEC.md §7.1–§7.5).

Each test drives the real CLI as a subprocess against a throwaway COORD_ROOT
(pytest `tmp_path`), never the repo's own `.coordination/`. Behaviors asserted
here are the ones transcribed in `reference/ACCEPTANCE.md`.

We invoke the interpreter via `sys.executable` for portability (this repo's dev
box has no `python3` alias, only `python`).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

COORD_PY = Path(__file__).resolve().parent.parent / "coord" / "coord.py"


@pytest.fixture
def coord(tmp_path):
    """Return a `run(*args)` callable bound to an initialized, isolated plane."""
    root = tmp_path / "coordination"

    def run(*args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["COORD_ROOT"] = str(root)
        return subprocess.run(
            [sys.executable, str(COORD_PY), *args],
            capture_output=True,
            text=True,
            env=env,
        )

    run.root = root.resolve()  # coord.py resolves COORD_ROOT; read artifacts from the same place
    init = run("init")
    assert init.returncode == 0, init.stderr
    return run


def _register(coord, session, role="worker", branch=None):
    branch = branch or f"feat/{session}"
    r = coord("register", "--session", session, "--role", role, "--branch", branch)
    assert r.returncode == 0, r.stderr
    return r


# --- §7.1 Dependency blocking ---------------------------------------------
def test_claim_with_unmet_dependency_fails(coord):
    coord("add-task", "--id", "write-mapper", "--deps", "research-formatting")
    r = coord("claim", "--session", "editor", "--task", "write-mapper")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "blocked on unmet deps" in r.stderr
    assert "research-formatting" in r.stderr


def test_claim_succeeds_once_dependency_is_done(coord):
    coord("add-task", "--id", "research-formatting")
    coord("add-task", "--id", "write-mapper", "--deps", "research-formatting")
    # dep still open -> blocked
    assert coord("claim", "--session", "editor", "--task", "write-mapper").returncode == 1
    # mark dep done -> now claimable
    assert coord("claim", "--session", "researcher", "--task", "research-formatting").returncode == 0
    assert coord("complete", "--session", "researcher", "--task", "research-formatting").returncode == 0
    ok = coord("claim", "--session", "editor", "--task", "write-mapper")
    assert ok.returncode == 0, ok.stderr
    assert "editor claimed write-mapper" in ok.stdout


# --- §7.2 Atomic claim (exactly one winner) -------------------------------
def test_two_concurrent_claims_yield_exactly_one_winner(coord):
    coord("add-task", "--id", "research-formatting")
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [
            ex.submit(coord, "claim", "--session", s, "--task", "research-formatting")
            for s in ("researcher", "editor")
        ]
        results = [f.result() for f in futs]

    winners = [r for r in results if r.returncode == 0]
    losers = [r for r in results if r.returncode != 0]
    assert len(winners) == 1, [(r.returncode, r.stdout, r.stderr) for r in results]
    assert len(losers) == 1
    assert "claimed research-formatting" in winners[0].stdout

    # A subsequent claim of the now-claimed task is also rejected.
    again = coord("claim", "--session", "third", "--task", "research-formatting")
    assert again.returncode == 1
    assert "not claimable" in again.stderr


# --- §7.3 Lease deny, then heartbeat-gated steal + reap -------------------
def test_lease_denies_then_reap_steals_and_reacquires(coord):
    res = "shared/theme.json"  # slashed name -> must flatten to shared__theme.json

    # (a) a valid lease denies another session
    assert coord("lock", "acquire", "--session", "worker1", "--resource", res, "--ttl", "60").returncode == 0
    denied = coord("lock", "acquire", "--session", "worker2", "--resource", res, "--ttl", "60")
    assert denied.returncode == 1
    assert "held" in denied.stderr.lower()

    # re-take with a 1s lease by an *unregistered* holder (heartbeat provably stale)
    assert coord("lock", "release", "--session", "worker1", "--resource", res).returncode == 0
    assert coord("lock", "acquire", "--session", "worker1", "--resource", res, "--ttl", "1").returncode == 0
    time.sleep(1.2)  # let the TTL elapse

    # (b) reap reclaims the expired + stale lease; slash was flattened to __
    reap = coord("reap")
    assert reap.returncode == 0, reap.stderr
    data = json.loads(reap.stdout)
    reaped_names = [name for name, _holder in data["reaped_locks"]]
    assert "shared__theme.json" in reaped_names
    assert ["shared__theme.json", "worker1"] in data["reaped_locks"]

    # (c) a live session can now acquire it
    got = coord("lock", "acquire", "--session", "worker2", "--resource", res, "--ttl", "60")
    assert got.returncode == 0, got.stderr


def test_expired_lease_with_live_holder_is_not_stolen(coord):
    """Steal requires TTL expiry AND a stale heartbeat — not just expiry."""
    res = "shared/theme.json"
    _register(coord, "worker1")  # fresh heartbeat => NOT stale
    assert coord("lock", "acquire", "--session", "worker1", "--resource", res, "--ttl", "1").returncode == 0
    time.sleep(1.2)  # TTL elapsed, but worker1's heartbeat is seconds old

    # holder still alive -> another session is denied even though the lease expired
    denied = coord("lock", "acquire", "--session", "worker2", "--resource", res, "--ttl", "60")
    assert denied.returncode == 1
    assert "held" in denied.stderr.lower()

    # ...and reap must NOT reclaim it
    reap = coord("reap")
    assert reap.returncode == 0, reap.stderr
    reaped_names = [name for name, _holder in json.loads(reap.stdout)["reaped_locks"]]
    assert "shared__theme.json" not in reaped_names


# --- §7.4 Staleness filter -------------------------------------------------
def test_checkpoint_skips_stale_message_and_surfaces_current(coord):
    _register(coord, "editor")
    # version -> 1, then a message pinned to as_of=1
    coord("state", "set", "--session", "orch", "--key", "target_palette", "--value", '"v2"')
    coord("send", "--from", "orch", "--to", "editor", "--body", "use palette v1", "--as-of", "1")
    # version -> 2 (the as_of=1 message is now stale), then a current message
    coord("state", "set", "--session", "orch", "--key", "target_palette", "--value", '"v3"')
    coord("send", "--from", "orch", "--to", "editor", "--body", "now use v3", "--as-of", "2")

    cp = coord("checkpoint", "--session", "editor")
    assert cp.returncode == 0, cp.stderr
    out = json.loads(cp.stdout)
    assert out["desired_version"] == 2
    assert out["stale_messages_skipped"] == 1
    assert [m["body"] for m in out["messages"]] == ["now use v3"]
    assert out["messages"][0]["as_of"] == 2


# --- §7.5 Stop-flag halt ---------------------------------------------------
def test_global_stop_makes_checkpoint_exit_3(coord):
    _register(coord, "editor")
    assert coord("stop").returncode == 0
    cp = coord("checkpoint", "--session", "editor")
    assert cp.returncode == 3, (cp.returncode, cp.stdout, cp.stderr)
    out = json.loads(cp.stdout)  # state is still printed before the halt
    assert out["stop"] == ["GLOBAL"]


def test_session_stop_flag_halts_only_that_session(coord):
    _register(coord, "editor")
    _register(coord, "researcher")
    assert coord("stop", "--session", "editor").returncode == 0

    halted = coord("checkpoint", "--session", "editor")
    assert halted.returncode == 3
    assert json.loads(halted.stdout)["stop"] == ["editor"]

    running = coord("checkpoint", "--session", "researcher")
    assert running.returncode == 0
    assert json.loads(running.stdout)["stop"] == []


# --- Schema validation (SPEC §5 coord/schema/*.json) -----------------------
# The repo is stdlib-only (pytest is the sole dev dep), so instead of the
# third-party `jsonschema` package we use a tiny validator covering exactly the
# keywords our schemas use. Its job is to catch drift between a schema and what
# the CLI actually writes; the negative test below proves it discriminates.
SCHEMA_DIR = COORD_PY.parent / "schema"


def _type_ok(value, t):
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "null":
        return value is None
    raise AssertionError(f"schema uses unsupported type {t!r}")


def _schema_errors(instance, schema, path="$"):
    errors = []
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in enum {schema['enum']}")
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_ok(instance, t) for t in types):
            return errors + [f"{path}: {type(instance).__name__} is not one of {types}"]
    if "minimum" in schema and _type_ok(instance, "number"):
        if instance < schema["minimum"]:
            errors.append(f"{path}: {instance} < minimum {schema['minimum']}")
    if isinstance(instance, dict):
        for req in schema.get("required", []):
            if req not in instance:
                errors.append(f"{path}: missing required '{req}'")
        props = schema.get("properties", {})
        if schema.get("additionalProperties", True) is False:
            for key in instance:
                if key not in props:
                    errors.append(f"{path}: unexpected property '{key}'")
        for key, subschema in props.items():
            if key in instance:
                errors.extend(_schema_errors(instance[key], subschema, f"{path}.{key}"))
    if isinstance(instance, list) and "items" in schema:
        for i, element in enumerate(instance):
            errors.extend(_schema_errors(element, schema["items"], f"{path}[{i}]"))
    return errors


def _load_schema(name):
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def _assert_valid(instance, schema_name):
    errs = _schema_errors(instance, _load_schema(schema_name))
    assert not errs, f"{schema_name} validation failed:\n  " + "\n  ".join(errs)


ALL_SCHEMAS = [
    "registry.schema.json",
    "task.schema.json",
    "message.schema.json",
    "desired-state.schema.json",
]


def test_schema_files_are_valid_json_schema_documents():
    for name in ALL_SCHEMAS:
        schema = _load_schema(name)  # parses => valid JSON
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert "$id" in schema
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict) and schema["properties"]
        for req in schema.get("required", []):
            assert req in schema["properties"], f"{name}: required '{req}' not in properties"


def test_registry_sample_matches_schema(coord):
    _register(coord, "editor", role="editor", branch="feat/editor")
    coord("heartbeat", "--session", "editor")  # refreshes heartbeat fields
    reg = json.loads((coord.root / "registry" / "editor.json").read_text(encoding="utf-8"))
    _assert_valid(reg, "registry.schema.json")


def test_task_event_samples_match_schema(coord):
    coord("add-task", "--id", "research-formatting", "--desc", "figure out palette", "--deps", "")
    coord("claim", "--session", "researcher", "--task", "research-formatting")
    coord("complete", "--session", "researcher", "--task", "research-formatting")
    lines = (coord.root / "board" / "tasks.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(l) for l in lines if l.strip()]
    assert len(events) >= 3  # add-task, claim, complete
    for ev in events:
        _assert_valid(ev, "task.schema.json")


def test_message_sample_matches_schema(coord):
    coord("send", "--from", "orch", "--to", "editor", "--body", "use palette v3", "--as-of", "2", "--ttl", "600")
    coord("send", "--from", "orch", "--to", "editor", "--body", "no ttl, no version")  # as_of/expires null
    lines = (coord.root / "inbox" / "editor.jsonl").read_text(encoding="utf-8").splitlines()
    for l in lines:
        if l.strip():
            _assert_valid(json.loads(l), "message.schema.json")


def test_desired_state_sample_matches_schema(coord):
    # after init
    path = coord.root / "state" / "desired.json"
    _assert_valid(json.loads(path.read_text(encoding="utf-8")), "desired-state.schema.json")
    # after a state set (version bump + populated desired map)
    coord("state", "set", "--session", "orch", "--key", "target_palette", "--value", '"v3"')
    _assert_valid(json.loads(path.read_text(encoding="utf-8")), "desired-state.schema.json")


def test_schema_validator_discriminates():
    """Guard against a no-op validator: bad instances must produce errors."""
    good_reg = {
        "session": "s", "role": "r", "branch": "b", "worktree": "/w", "owned_paths": [],
        "registered": "2020-01-01T00:00:00Z", "heartbeat": 1.0, "heartbeat_iso": "2020-01-01T00:00:00Z",
    }
    reg = _load_schema("registry.schema.json")
    assert _schema_errors(good_reg, reg) == []
    assert _schema_errors({k: v for k, v in good_reg.items() if k != "session"}, reg)  # missing required
    assert _schema_errors({**good_reg, "owned_paths": "not-a-list"}, reg)              # wrong type
    assert _schema_errors({**good_reg, "surprise": 1}, reg)                            # extra property

    msg = _load_schema("message.schema.json")
    good_msg = {"seq": 1, "ts": "t", "from": "a", "to": "b", "body": "x", "as_of": None, "expires": None}
    assert _schema_errors(good_msg, msg) == []
    assert _schema_errors({**good_msg, "as_of": "1"}, msg)  # string, not integer/null

    task = _load_schema("task.schema.json")
    assert _schema_errors({"ts": 1.0, "id": "t", "status": "bogus", "claimed_by": None}, task)  # bad enum
