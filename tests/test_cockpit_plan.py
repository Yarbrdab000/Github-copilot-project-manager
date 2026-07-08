"""
Cockpit addendum tests — plan propose/validate/approve/reject (COCKPIT_SPEC.md §7.1-§7.5).

Phase 1 (this file, partial): the pure owned-path overlap helper (§3.3) that `plan
propose` will use in a later phase to reject fleets with colliding write-scopes. We
import `coord/coord.py` directly (rather than driving the CLI as a subprocess) since
these are pure functions with no filesystem side effects.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

COORD_PY = Path(__file__).resolve().parent.parent / "coord" / "coord.py"


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
