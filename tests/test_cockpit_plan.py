"""
Cockpit addendum tests — plan propose/validate/approve/reject (COCKPIT_SPEC.md §7.1-§7.5).

Phase 1 (pure-helper section below): the owned-path overlap helper (§3.3) and fleet
tolerance, imported directly from `coord/coord.py` since they are pure functions with
no filesystem side effects.

Phase 2 (CLI section below): `plan propose` / `plans` / `plan show` (§3.2, acceptance
§7.1-§7.3) driven as a real subprocess against a throwaway COORD_ROOT, mirroring the
`coord` fixture pattern in `tests/test_coord.py`. `plan approve`/`plan reject` are
Phase 3 (keystone) and are intentionally NOT exercised here.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

COORD_PY = Path(__file__).resolve().parent.parent / "coord" / "coord.py"
SCHEMA_DIR = COORD_PY.parent / "schema"


def _load_coord_module():
    spec = importlib.util.spec_from_file_location("coord_module_under_test", COORD_PY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


coord = _load_coord_module()


# --- owned-path overlap helper (§3.3) ---------------------------------------

def test_overlap_parent_and_child_globs_overlap():
    assert coord._owned_paths_overlap(["src/**"], ["src/api/**"]) is True


def test_overlap_sibling_globs_do_not_overlap():
    assert coord._owned_paths_overlap(["src/api/**"], ["src/ui/**"]) is False


def test_overlap_identical_globs_overlap():
    assert coord._owned_paths_overlap(["src/**"], ["src/**"]) is True


def test_overlap_is_path_segment_aware_not_string_prefix():
    # "src" is a string-prefix of "src2" but must NOT be treated as a path overlap.
    assert coord._owned_paths_overlap(["src/**"], ["src2/**"]) is False


def test_overlap_child_and_parent_globs_overlap_reverse_order():
    assert coord._owned_paths_overlap(["tests/api/**"], ["tests/**"]) is True


def test_overlap_checks_all_pairs_across_sets():
    # No single-glob overlap, but a cross-pair does.
    assert coord._owned_paths_overlap(
        ["src/ui/**", "docs/**"], ["src/ui/**", "tests/**"]
    ) is True


def test_overlap_disjoint_sets_do_not_overlap():
    assert coord._owned_paths_overlap(
        ["src/api/**", "tests/api/**"], ["src/ui/**", "tests/ui/**"]
    ) is False


# --- fleet spec tolerance (§3.1) --------------------------------------------

def test_get_fleet_missing_key_returns_empty_defaults():
    # Legacy plane: no "fleet" key in desired.json's "desired" object at all.
    fleet = coord._get_fleet({})
    assert fleet == {"max_concurrent": 0, "workers": []}


def test_get_fleet_present_key_is_read_through():
    desired = {"fleet": {"max_concurrent": 3, "workers": [{"id": "w-api", "owned_paths": ["src/api/**"]}]}}
    fleet = coord._get_fleet(desired)
    assert fleet["max_concurrent"] == 3
    assert fleet["workers"] == [{"id": "w-api", "owned_paths": ["src/api/**"]}]


# --- `coord plan propose` / `coord plans` / `coord plan show` CLI (§3.2, §7.1-§7.3) ---

@pytest.fixture
def cli(tmp_path):
    """Return a `run(*args)` callable bound to an initialized, isolated plane (same
    pattern as the `coord` fixture in tests/test_coord.py)."""
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

    run.root = root.resolve()
    init = run("init")
    assert init.returncode == 0, init.stderr
    return run


def _valid_plan_doc(note="build feature X"):
    return {
        "note": note,
        "fleet": {
            "max_concurrent": 3,
            "workers": [
                {"id": "w-api", "owned_paths": ["src/api/**", "tests/api/**"]},
                {"id": "w-ui", "owned_paths": ["src/ui/**", "tests/ui/**"]},
            ],
        },
        "tasks": [
            {"id": "api-model", "desc": "model", "owned_by": "w-api", "deps": [],
             "verify": "pytest tests/api -q", "max_attempts": 3},
            {"id": "api-routes", "desc": "routes", "owned_by": "w-api", "deps": ["api-model"],
             "verify": "pytest tests/api -q", "max_attempts": 3},
            {"id": "ui-page", "desc": "page", "owned_by": "w-ui", "deps": [],
             "verify": None, "max_attempts": 1},
        ],
    }


def _propose(cli, doc, tmp_path, name="plan.json"):
    plan_file = tmp_path / name
    plan_file.write_text(json.dumps(doc), encoding="utf-8")
    return cli("plan", "propose", "--file", str(plan_file))


def _desired_version(cli):
    r = cli("state", "show")
    return json.loads(r.stdout)["version"]


# §7.1 — valid plan writes a pending plan and does NOT bump desired.version
def test_plan_propose_valid_writes_pending_and_does_not_bump_version(cli, tmp_path):
    version_before = _desired_version(cli)
    r = _propose(cli, _valid_plan_doc(), tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    version_after = _desired_version(cli)
    assert version_after == version_before

    listing = cli("plans")
    assert listing.returncode == 0
    assert "build feature X" in listing.stdout
    assert "workers=2" in listing.stdout
    assert "tasks=3" in listing.stdout


# §7.2 — overlapping owned_paths is rejected: non-zero exit, nothing written
def test_plan_propose_overlapping_owned_paths_rejected(cli, tmp_path):
    doc = _valid_plan_doc()
    doc["fleet"]["workers"] = [
        {"id": "w-a", "owned_paths": ["src/**"]},
        {"id": "w-b", "owned_paths": ["src/api/**"]},
    ]
    doc["tasks"] = [
        {"id": "t1", "desc": "x", "owned_by": "w-a", "deps": [], "verify": None},
    ]
    r = _propose(cli, doc, tmp_path)
    assert r.returncode != 0
    assert "overlapping" in r.stderr

    listing = cli("plans")
    assert listing.stdout.strip() == "(no pending plans)"


# §7.3 — `plans` / `plan show` list the pending plan with a current -> proposed view
def test_plans_and_plan_show_list_current_to_proposed(cli, tmp_path):
    r = _propose(cli, _valid_plan_doc(), tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    pid = r.stdout.split()[2]

    listing = cli("plans")
    assert pid in listing.stdout

    shown = cli("plan", "show", "--id", pid)
    assert shown.returncode == 0, shown.stderr
    assert "fleet current" in shown.stdout
    assert "fleet proposed" in shown.stdout
    assert "api-model" in shown.stdout
    assert "api-routes" in shown.stdout
    assert "deps=api-model" in shown.stdout
    assert "ui-page" in shown.stdout


# --- validator lock-down: one failure test per validation rule ---------------

def test_plan_propose_rejects_duplicate_task_id(cli, tmp_path):
    doc = _valid_plan_doc()
    doc["tasks"] = [
        {"id": "dup", "desc": "a", "owned_by": "w-api", "deps": [], "verify": None},
        {"id": "dup", "desc": "b", "owned_by": "w-api", "deps": [], "verify": None},
    ]
    r = _propose(cli, doc, tmp_path)
    assert r.returncode != 0
    assert "unique" in r.stderr
    assert cli("plans").stdout.strip() == "(no pending plans)"


def test_plan_propose_rejects_dep_referencing_unknown_task(cli, tmp_path):
    doc = _valid_plan_doc()
    doc["tasks"] = [
        {"id": "t1", "desc": "a", "owned_by": "w-api", "deps": ["nonexistent"], "verify": None},
    ]
    r = _propose(cli, doc, tmp_path)
    assert r.returncode != 0
    assert "nonexistent" in r.stderr
    assert cli("plans").stdout.strip() == "(no pending plans)"


def test_plan_propose_rejects_owned_by_referencing_undeclared_worker(cli, tmp_path):
    doc = _valid_plan_doc()
    doc["tasks"] = [
        {"id": "t1", "desc": "a", "owned_by": "w-ghost", "deps": [], "verify": None},
    ]
    r = _propose(cli, doc, tmp_path)
    assert r.returncode != 0
    assert "w-ghost" in r.stderr
    assert cli("plans").stdout.strip() == "(no pending plans)"


def test_plan_propose_rejects_task_missing_verify_key(cli, tmp_path):
    doc = _valid_plan_doc()
    doc["tasks"] = [
        {"id": "t1", "desc": "a", "owned_by": "w-api", "deps": []},  # no "verify" key at all
    ]
    r = _propose(cli, doc, tmp_path)
    assert r.returncode != 0
    assert "verify" in r.stderr
    assert cli("plans").stdout.strip() == "(no pending plans)"


def test_plan_propose_rejects_task_id_already_on_live_board(cli, tmp_path):
    cli("add-task", "--id", "api-model")
    doc = _valid_plan_doc()
    r = _propose(cli, doc, tmp_path)
    assert r.returncode != 0
    assert "api-model" in r.stderr
    assert cli("plans").stdout.strip() == "(no pending plans)"


# --- plan-instance schema validation (mirrors tests/test_coord.py's schema checks) ---

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


def test_plan_propose_produces_instance_valid_against_plan_schema(cli, tmp_path):
    r = _propose(cli, _valid_plan_doc(), tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr

    ledger_path = cli.root / "state" / "plans.jsonl"
    lines = [json.loads(l) for l in ledger_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    schema = json.loads((SCHEMA_DIR / "plan.schema.json").read_text(encoding="utf-8"))
    errs = _schema_errors(lines[0], schema)
    assert not errs, f"plan.schema.json validation failed:\n  " + "\n  ".join(errs)
