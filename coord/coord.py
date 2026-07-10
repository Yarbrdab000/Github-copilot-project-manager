#!/usr/bin/env python3
"""
coord — a filesystem control plane for coordinating parallel Copilot agent sessions.

Design principles (see docs/protocol.md):
  * Coordinate through DECLARATIVE shared state reconciled at checkpoints,
    not imperative messages dropped into a queue that goes stale.
  * Ledgers are APPEND-ONLY (jsonl) so concurrent writers don't clobber each other.
  * Mutable state is written atomically (temp file + os.replace).
  * Locks are LEASES with expiry, acquired via atomic os.mkdir, and are
    steal-able only when the holder's heartbeat is provably stale (no deadlock
    from a crashed session).
  * Every session heartbeats; a dead session's work is reclaimable.

Stdlib only. Python 3.8+.  Run `coord --help`.
"""
from __future__ import annotations
import argparse, json, os, sys, time, glob, fnmatch, re, posixpath
from pathlib import Path

# ---- layout ---------------------------------------------------------------
ROOT_ENV = "COORD_ROOT"
DEFAULT_DIR = ".coordination"
HEARTBEAT_STALE_SEC = 300  # a session silent this long is considered dead


def root() -> Path:
    return Path(os.environ.get(ROOT_ENV, DEFAULT_DIR)).resolve()


def _p(*parts) -> Path:
    return root().joinpath(*parts)


def now() -> float:
    return time.time()


def iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else now()))


# ---- atomic helpers -------------------------------------------------------
def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{time.time_ns()}")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)  # atomic on POSIX and Windows


def _append(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:  # append is atomic for small lines
        f.write(json.dumps(obj, separators=(",", ":")) + "\n")


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _read_jsonl(path: Path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _die(msg: str, code: int = 1):
    print(f"coord: {msg}", file=sys.stderr)
    sys.exit(code)


# ---- init -----------------------------------------------------------------
def cmd_init(a):
    for d in ("registry", "inbox", "cursor", "locks", "state", "log", "control", "board", "escalations"):
        _p(d).mkdir(parents=True, exist_ok=True)
    _p("state", "proposals").mkdir(parents=True, exist_ok=True)
    state = _p("state", "desired.json")
    if not state.exists():
        _atomic_write(state, json.dumps({"version": 0, "updated": iso(), "desired": {}}, indent=2))
    _atomic_write(_p("board", "tasks.jsonl"), "") if not _p("board", "tasks.jsonl").exists() else None
    print(f"initialized control plane at {root()}")


# ---- fleet spec (COCKPIT SPEC §3.1) ---------------------------------------
def _get_fleet(desired: dict) -> dict:
    """Read the optional `fleet` object out of a `desired` dict (i.e. desired.json's
    "desired" key). Absent on legacy planes -- missing must never crash a read, so this
    normalizes to `{"max_concurrent": 0, "workers": []}` when there is no fleet declared
    yet. Pure/read-only; does not write anything (fleet is first written by `plan
    approve`, landing in a later phase)."""
    fleet = desired.get("fleet") or {}
    return {
        "max_concurrent": fleet.get("max_concurrent", 0),
        "workers": fleet.get("workers", []),
    }


def _normalize_owned_glob(glob_pat: str) -> str:
    """Strip a trailing `/**` or `/*` so two owned-path globs can be compared as plain
    path prefixes (COCKPIT SPEC §3.3)."""
    for suffix in ("/**", "/*"):
        if glob_pat.endswith(suffix):
            return glob_pat[: -len(suffix)]
    return glob_pat


def _path_prefix_overlaps(a: str, b: str) -> bool:
    """Path-segment-aware prefix check: true if one normalized path is a segment-wise
    prefix of the other (or identical). Segment-aware so `src` does NOT match `src2` --
    plain string prefixing would falsely overlap them."""
    a_segs = [s for s in a.split("/") if s]
    b_segs = [s for s in b.split("/") if s]
    shorter, longer = (a_segs, b_segs) if len(a_segs) <= len(b_segs) else (b_segs, a_segs)
    return shorter == longer[: len(shorter)]


def _owned_paths_overlap(a_globs, b_globs) -> bool:
    """True if any glob in `a_globs` overlaps any glob in `b_globs` (COCKPIT SPEC §3.3).
    Pure/deterministic. Used to reject a fleet plan whose declared workers' owned paths
    could collide under the write-scope hook."""
    a_norm = [_normalize_owned_glob(g) for g in a_globs]
    b_norm = [_normalize_owned_glob(g) for g in b_globs]
    return any(_path_prefix_overlaps(x, y) for x in a_norm for y in b_norm)


# ---- session lifecycle ----------------------------------------------------
def cmd_register(a):
    reg = {
        "session": a.session,
        "role": a.role,
        "branch": a.branch,
        "worktree": a.worktree or os.getcwd(),
        "owned_paths": [p for p in (a.paths or "").split(",") if p],
        "registered": iso(),
        "heartbeat": now(),
        "heartbeat_iso": iso(),
    }
    _atomic_write(_p("registry", f"{a.session}.json"), json.dumps(reg, indent=2))
    _append(_p("board", "events.jsonl"), {"ts": now(), "event": "register", "session": a.session, "role": a.role})
    print(f"registered {a.session} as {a.role} on branch '{a.branch}' owning {reg['owned_paths']}")


def _touch_heartbeat(session: str):
    path = _p("registry", f"{session}.json")
    reg = _read_json(path)
    if reg is None:
        _die(f"session '{session}' is not registered (run `coord register` first)")
    reg["heartbeat"] = now()
    reg["heartbeat_iso"] = iso()
    _atomic_write(path, json.dumps(reg, indent=2))
    return reg


def cmd_heartbeat(a):
    _touch_heartbeat(a.session)
    print(f"heartbeat {a.session} @ {iso()}")


def _stop_flags(session: str):
    flags = []
    if _p("control", "STOP").exists():
        flags.append("GLOBAL")
    if _p("control", f"STOP-{session}").exists():
        flags.append(session)
    return flags


# ---- the checkpoint ritual -------------------------------------------------
def cmd_checkpoint(a):
    """The one command every worker runs at every checkpoint boundary.
    Heartbeats, checks stop-flags, surfaces fresh (non-stale) inbox messages,
    and prints the current desired-state version to reconcile against."""
    _touch_heartbeat(a.session)
    stop = _stop_flags(a.session)
    state = _read_json(_p("state", "desired.json"), {"version": 0, "desired": {}})
    fresh, stale = _inbox_partition(a.session, state.get("version", 0))
    # continue: true when this session holds unfinished (claimed, not-done) work --
    # a machine-readable "keep going, don't yield the turn" signal (AUTONOMY_SPEC §6).
    tasks = _fold_tasks()
    keep_going = any(t["status"] == "claimed" and t.get("claimed_by") == a.session for t in tasks.values())
    out = {
        "session": a.session,
        "time": iso(),
        "stop": stop,                       # non-empty => HALT at this checkpoint
        "desired_version": state.get("version", 0),
        "desired": state.get("desired", {}),
        "messages": fresh,                  # act on these
        "stale_messages_skipped": len(stale),
        "continue": keep_going,
    }
    print(json.dumps(out, indent=2))
    if stop:
        # exit 3 = a wrapper / the agent should stop cleanly
        sys.exit(3)


# ---- declarative desired state --------------------------------------------
def _lock_key(name: str) -> str:
    # flatten path separators so every lockdir lives directly under locks/
    # (keeps status/reap's glob able to see it, and blocks path traversal)
    return name.replace(os.sep, "__").replace("/", "__").replace("..", "__")


def _lockdir(name: str) -> Path:
    return _p("locks", _lock_key(name) + ".lockdir")


def _acquire_raw(name: str, holder: str, ttl: int) -> bool:
    ld = _lockdir(name)
    try:
        ld.mkdir(parents=True, exist_ok=False)  # atomic
    except FileExistsError:
        meta = _read_json(ld / "meta.json", {})
        expired = (now() - meta.get("acquired", 0)) > meta.get("ttl", 0)
        holder_stale = _heartbeat_stale(meta.get("holder"))
        # An empty meta means the dir exists but meta.json isn't written yet (the
        # mkdir->meta-write window) or is mid-release: that lock is freshly held,
        # not stealable. Require a real holder before stealing, else two racers
        # could both "steal" a just-created lock and both win a task claim.
        if meta.get("holder") and expired and holder_stale:
            # steal: only when lease expired AND holder is provably dead
            _atomic_write(ld / "meta.json",
                          json.dumps({"holder": holder, "acquired": now(), "ttl": ttl, "stolen_from": meta.get("holder")}))
            return True
        return False
    _atomic_write(ld / "meta.json", json.dumps({"holder": holder, "acquired": now(), "ttl": ttl}))
    return True


def _release_raw(name: str, holder: str) -> bool:
    ld = _lockdir(name)
    meta = _read_json(ld / "meta.json", {})
    if meta and meta.get("holder") not in (holder, None):
        return False
    try:
        (ld / "meta.json").unlink(missing_ok=True)
        ld.rmdir()
    except OSError:
        pass
    return True


def cmd_state(a):
    path = _p("state", "desired.json")
    if a.action == "show":
        print(json.dumps(_read_json(path, {"version": 0, "desired": {}}), indent=2))
        return
    if a.action == "set":
        if not a.key:
            _die("`state set` requires --key and --value")
        # serialize desired-state writes behind an internal lock to keep version monotonic
        for _ in range(50):
            if _acquire_raw("__state__", a.session or "state", 30):
                break
            time.sleep(0.1)
        else:
            _die("could not acquire state lock")
        try:
            st = _read_json(path, {"version": 0, "desired": {}})
            try:
                val = json.loads(a.value)
            except (json.JSONDecodeError, TypeError):
                val = a.value
            st.setdefault("desired", {})[a.key] = val
            st["version"] = st.get("version", 0) + 1
            st["updated"] = iso()
            _atomic_write(path, json.dumps(st, indent=2))
            _append(_p("board", "events.jsonl"),
                    {"ts": now(), "event": "state_set", "key": a.key, "version": st["version"]})
            print(f"desired.{a.key} set; state version -> {st['version']}")
        finally:
            _release_raw("__state__", a.session or "state")
        return
    if a.action == "propose":
        _state_propose(a)
        return
    if a.action == "proposals":
        _state_proposals(a)
        return
    if a.action == "approve":
        _state_approve(a)
        return
    if a.action == "reject":
        _state_reject(a)
        return


# ---- navigator proposals (human-gated desired-state amendments) -----------
def _proposal_path(pid: str) -> Path:
    return _p("state", "proposals", f"{pid}.json")


def _state_propose(a):
    """Write a PENDING proposal to amend desired[key]. Does NOT bump the live version."""
    if not a.key or a.value is None:
        _die("`state propose` requires --key and --value")
    st = _read_json(_p("state", "desired.json"), {"version": 0, "desired": {}})
    try:
        val = json.loads(a.value)
    except (json.JSONDecodeError, TypeError):
        val = a.value
    pid = str(time.time_ns())
    current = st.get("desired", {}).get(a.key)
    proposal = {
        "pid": pid,
        "from": a.session or "unknown",
        "key": a.key,
        "value": val,
        "invalidates": [t for t in (a.invalidates or "").split(",") if t],
        "note": a.note or "",
        "status": "pending",
        "created": iso(),
        "base_version": st.get("version", 0),
    }
    _atomic_write(_proposal_path(pid), json.dumps(proposal, indent=2))
    print(f"proposed {pid}: desired.{a.key}: {json.dumps(current)} -> {json.dumps(val)} "
          f"(pending; version unchanged at {st.get('version', 0)})")
    if proposal["invalidates"]:
        print(f"  invalidates: {proposal['invalidates']}")


def _state_proposals(a):
    """List pending proposals with their current->proposed diff."""
    st = _read_json(_p("state", "desired.json"), {"version": 0, "desired": {}})
    desired = st.get("desired", {})
    pending = []
    for pf in sorted(_p("state", "proposals").glob("*.json")):
        prop = _read_json(pf, {})
        if prop.get("status") == "pending":
            pending.append(prop)
    if not pending:
        print("(no pending proposals)")
        return
    for prop in pending:
        cur = desired.get(prop["key"])
        inv = ("  invalidates=" + ",".join(prop.get("invalidates", []))) if prop.get("invalidates") else ""
        note = ("  note=" + prop["note"]) if prop.get("note") else ""
        print(f"  {prop['pid']}  from={prop.get('from')}  {prop['key']}: "
              f"{json.dumps(cur)} -> {json.dumps(prop['value'])}{inv}{note}")


def _state_approve(a):
    """Apply a pending proposal: bump the version, requeue invalidated tasks, and
    notify their claimants as of the NEW version. Human-gated (the navigator cannot
    run this — see the write-scope guard)."""
    if not a.id:
        _die("`state approve` requires --id")
    ppath = _proposal_path(a.id)
    prop = _read_json(ppath)
    if prop is None:
        _die(f"no such proposal '{a.id}'")
    if prop.get("status") != "pending":
        _die(f"proposal '{a.id}' is '{prop.get('status')}' — not pending")
    path = _p("state", "desired.json")
    # serialize desired-state writes behind the internal lock (same pattern as `state set`)
    for _ in range(50):
        if _acquire_raw("__state__", a.session or "state", 30):
            break
        time.sleep(0.1)
    else:
        _die("could not acquire state lock")
    try:
        st = _read_json(path, {"version": 0, "desired": {}})
        st.setdefault("desired", {})[prop["key"]] = prop["value"]
        st["version"] = st.get("version", 0) + 1
        st["updated"] = iso()
        new_version = st["version"]
        _atomic_write(path, json.dumps(st, indent=2))
        _append(_p("board", "events.jsonl"),
                {"ts": now(), "event": "state_approved", "pid": a.id, "key": prop["key"], "version": new_version})
        # requeue each invalidated task and message its current claimant as-of the NEW
        # version, so the note lands FRESH (not stale) at the claimant's next checkpoint.
        requeued = []
        tasks = _fold_tasks()
        for tid in prop.get("invalidates", []):
            t = tasks.get(tid)
            if not t:
                continue
            claimant = t.get("claimed_by")
            _append(_p("board", "tasks.jsonl"),
                    {"ts": now(), "id": tid, "status": "open", "claimed_by": None})
            if claimant:
                msg = {
                    "seq": time.time_ns(),
                    "ts": iso(),
                    "from": a.session or "orchestrator",
                    "to": claimant,
                    "body": f"task '{tid}' invalidated by approved proposal {a.id} "
                            f"(desired.{prop['key']} -> v{new_version}); stop and re-claim",
                    "as_of": new_version,
                    "expires": None,
                }
                _append(_p("inbox", f"{claimant}.jsonl"), msg)
            requeued.append({"task": tid, "notified": claimant})
        prop["status"] = "applied"
        _atomic_write(ppath, json.dumps(prop, indent=2))
        print(f"approved {a.id}: desired.{prop['key']} applied; state version -> {new_version}")
        if requeued:
            print(f"  requeued: {requeued}")
    finally:
        _release_raw("__state__", a.session or "state")


def _state_reject(a):
    """Mark a pending proposal rejected. No version change."""
    if not a.id:
        _die("`state reject` requires --id")
    ppath = _proposal_path(a.id)
    prop = _read_json(ppath)
    if prop is None:
        _die(f"no such proposal '{a.id}'")
    if prop.get("status") != "pending":
        _die(f"proposal '{a.id}' is '{prop.get('status')}' — not pending")
    prop["status"] = "rejected"
    if a.reason:
        prop["reason"] = a.reason
    _atomic_write(ppath, json.dumps(prop, indent=2))
    _append(_p("board", "events.jsonl"),
            {"ts": now(), "event": "state_rejected", "pid": a.id, "key": prop.get("key")})
    print(f"rejected {a.id} (version unchanged)")


# ---- tasks (append-only board + atomic claim) -----------------------------
def _fold_tasks():
    """Fold the append-only task ledger into current task state (event sourcing)."""
    tasks = {}
    for ev in _read_jsonl(_p("board", "tasks.jsonl")):
        tid = ev.get("id")
        if not tid:
            continue
        t = tasks.setdefault(tid, {"id": tid, "status": "open", "deps": [], "claimed_by": None,
                                    "attempts": 0, "verified": False})
        for k in ("desc", "deps", "status", "claimed_by", "claimed_at_version",
                  "verify", "max_attempts", "attempts", "verified", "owned_by"):
            if k in ev and ev[k] is not None:
                t[k] = ev[k]
    return tasks


def cmd_add_task(a):
    ev = {"ts": now(), "id": a.id, "desc": a.desc, "deps": [d for d in (a.deps or "").split(",") if d],
          "status": "open", "claimed_by": None}
    if a.verify is not None:
        ev["verify"] = a.verify
    if a.max_attempts is not None:
        ev["max_attempts"] = a.max_attempts
    _append(_p("board", "tasks.jsonl"), ev)
    print(f"added task {a.id}")


def cmd_tasks(a):
    tasks = _fold_tasks()
    if not tasks:
        print("(no tasks)")
        return
    for t in tasks.values():
        deps = ("deps=" + ",".join(t["deps"])) if t["deps"] else ""
        by = ("<- " + t["claimed_by"]) if t["claimed_by"] else ""
        print(f"  [{t['status']:>10}] {t['id']:<20} {by} {deps}  {t.get('desc','')}")


# ---- plans (COCKPIT SPEC §3.2: append-only ledger + fold, like tasks) ------
def _fold_plans():
    """Fold the append-only plans ledger into current plan state (event sourcing,
    same shape as `_fold_tasks`). The propose event carries `as_of`/`note`/`fleet`/
    `tasks`; later approve/reject events only flip `status`."""
    plans = {}
    for ev in _read_jsonl(_p("state", "plans.jsonl")):
        pid = ev.get("id")
        if not pid:
            continue
        p = plans.setdefault(pid, {"id": pid, "status": "pending", "as_of": 0,
                                    "note": "", "fleet": {}, "tasks": []})
        for k in ("as_of", "note", "fleet", "tasks", "status"):
            if k in ev and ev[k] is not None:
                p[k] = ev[k]
    return plans


def _toposort_waves(ids, deps_map):
    """Kahn layered topological sort over a task DAG. `ids` is the node collection and
    `deps_map[id]` lists the ids that `id` depends on (deps outside `ids` are ignored).
    Returns `(waves, cyclic)`: `waves` is a list of dependency-ordered levels, each a
    sorted list of ids whose deps are all satisfied by earlier waves (so a level's tasks
    can run in parallel); `cyclic` is the sorted list of ids that could never be
    scheduled because they sit in or downstream of a dependency cycle (empty => a DAG).
    Pure and deterministic — no I/O."""
    remaining = set(ids)
    deps_in = {i: {d for d in deps_map.get(i, []) if d in remaining} for i in remaining}
    waves = []
    while remaining:
        ready = sorted(i for i in remaining if not (deps_in[i] & remaining))
        if not ready:
            break  # nothing schedulable => everything left is in/after a cycle
        waves.append(ready)
        remaining.difference_update(ready)
    return waves, sorted(remaining)


def _longest_dep_path(ids, deps_map, waves):
    """Return one longest dependency chain (a list of ids, dependency-first) through the
    DAG described by `waves` (from `_toposort_waves`); its length is the critical path.
    A node in wave K always has a dep in wave K-1, so walking from a deepest node down the
    deepest dep at each step yields a chain of length == len(waves). Deterministic."""
    wave_of = {i: wi for wi, w in enumerate(waves) for i in w}
    if not wave_of:
        return []
    node = max(wave_of, key=lambda i: (wave_of[i], str(i)))
    path = [node]
    while True:
        deeper = [d for d in deps_map.get(node, []) if d in wave_of]
        if not deeper:
            break
        node = max(deeper, key=lambda d: (wave_of[d], str(d)))
        path.append(node)
    path.reverse()
    return path


def _plan_validate(doc: dict, tasks_on_board: dict):
    """Validate a proposed plan document (COCKPIT SPEC §3.2). Returns a list of error
    strings; empty means valid. Pure — takes the current task board as a parameter so
    it never reads disk itself (callers already have it)."""
    errors = []
    fleet = doc.get("fleet") or {}
    workers = fleet.get("workers") or []
    max_concurrent = fleet.get("max_concurrent")

    if not isinstance(max_concurrent, int) or isinstance(max_concurrent, bool) or max_concurrent < 1:
        errors.append(f"fleet.max_concurrent must be an int >= 1, got {max_concurrent!r}")

    worker_ids = [w.get("id") for w in workers]
    if len(worker_ids) != len(set(worker_ids)):
        errors.append(f"fleet.workers ids must be unique, got {worker_ids}")

    for w in workers:
        if not w.get("owned_paths"):
            errors.append(f"worker '{w.get('id')}' has no owned_paths")

    for i in range(len(workers)):
        for j in range(i + 1, len(workers)):
            wa, wb = workers[i], workers[j]
            if _owned_paths_overlap(wa.get("owned_paths") or [], wb.get("owned_paths") or []):
                errors.append(
                    f"workers '{wa.get('id')}' and '{wb.get('id')}' have overlapping owned_paths"
                )

    worker_id_set = set(worker_ids)
    tasks = doc.get("tasks") or []
    plan_task_ids = [t.get("id") for t in tasks]
    if len(plan_task_ids) != len(set(plan_task_ids)):
        errors.append(f"plan task ids must be unique, got {plan_task_ids}")
    plan_task_id_set = set(plan_task_ids)

    for t in tasks:
        tid = t.get("id")
        if tid in tasks_on_board:
            errors.append(f"task '{tid}' already exists on the live board")
        owned_by = t.get("owned_by")
        if owned_by is not None and owned_by not in worker_id_set:
            errors.append(f"task '{tid}' owned_by '{owned_by}' does not reference a declared worker")
        for dep in t.get("deps") or []:
            if dep not in plan_task_id_set:
                errors.append(f"task '{tid}' dep '{dep}' does not reference a task in this plan")
        if "verify" not in t:
            errors.append(f"task '{tid}' is missing the 'verify' key (use null to opt out)")

    # Acyclicity: the task deps must form a DAG. A cycle passes every check above (each
    # dep references a real plan task) but then deadlocks forever at claim time, since a
    # task can never be claimed until its deps are done. Reject it up front rather than
    # letting the fleet wedge silently.
    deps_map = {t.get("id"): list(t.get("deps") or []) for t in tasks}
    _, cyclic = _toposort_waves(plan_task_id_set, deps_map)
    if cyclic:
        errors.append(
            f"task deps contain a cycle among {cyclic} — a dependency cycle would "
            "deadlock at claim time (no task in the cycle can ever be claimed)"
        )

    return errors


def _plan_read_doc(a) -> dict:
    """Read the plan document from --file, or stdin when --file is absent."""
    if getattr(a, "file", None):
        return json.loads(Path(a.file).read_text(encoding="utf-8"))
    return json.loads(sys.stdin.read())


def cmd_plan(a):
    if a.action == "propose":
        _plan_propose(a)
    elif a.action == "show":
        _plan_show(a)
    elif a.action == "approve":
        _plan_approve(a)
    elif a.action == "reject":
        _plan_reject(a)
    elif a.action == "analyze":
        _plan_analyze_cmd(a)
    elif a.action == "seams":
        _plan_seams_cmd(a)
    elif a.action == "scaffold":
        _plan_scaffold_cmd(a)


def _plan_propose(a):
    """Validate and write a PENDING plan to .coordination/state/plans.jsonl. Does NOT
    bump desired.version (that happens at `plan approve`, Phase 3). Navigator-allowed."""
    try:
        doc = _plan_read_doc(a)
    except (OSError, json.JSONDecodeError) as e:
        _die(f"could not read plan document: {e}")
        return

    errors = _plan_validate(doc, _fold_tasks())
    if errors:
        _die("plan rejected:\n  " + "\n  ".join(errors))
        return

    st = _read_json(_p("state", "desired.json"), {"version": 0, "desired": {}})
    current_fleet = _get_fleet(st.get("desired", {}))
    proposed_fleet = doc.get("fleet") or {}
    tasks = doc.get("tasks") or []

    pid = str(time.time_ns())
    ev = {
        "ts": now(),
        "id": pid,
        "as_of": st.get("version", 0),
        "note": doc.get("note", ""),
        "fleet": proposed_fleet,
        "tasks": tasks,
        "status": "pending",
    }
    _append(_p("state", "plans.jsonl"), ev)
    print(f"proposed plan {pid} (pending; desired.version unchanged at {st.get('version', 0)})")
    print(f"  fleet: {json.dumps(current_fleet)} -> {json.dumps(proposed_fleet)}")
    print(f"  tasks: 0 -> {len(tasks)}")
    for t in tasks:
        deps = (" deps=" + ",".join(t.get("deps") or [])) if t.get("deps") else ""
        print(f"    + {t.get('id')} owned_by={t.get('owned_by')}{deps}")


def cmd_plans(a):
    """List pending plans: id, as_of, note, worker count, task count."""
    plans = [p for p in _fold_plans().values() if p["status"] == "pending"]
    if not plans:
        print("(no pending plans)")
        return
    for p in sorted(plans, key=lambda x: x["id"]):
        n_workers = len((p.get("fleet") or {}).get("workers") or [])
        n_tasks = len(p.get("tasks") or [])
        note = ("  note=" + p["note"]) if p.get("note") else ""
        print(f"  {p['id']}  as_of={p['as_of']}  workers={n_workers}  tasks={n_tasks}{note}")


def _plan_show(a):
    """Print the full current -> proposed diff (fleet + task DAG) for a pending plan."""
    if not a.id:
        _die("`plan show` requires --id")
    plans = _fold_plans()
    p = plans.get(a.id)
    if not p:
        _die(f"no such plan '{a.id}'")
        return
    st = _read_json(_p("state", "desired.json"), {"version": 0, "desired": {}})
    current_fleet = _get_fleet(st.get("desired", {}))
    proposed_fleet = p.get("fleet") or {}
    print(f"plan {p['id']}  status={p['status']}  as_of={p['as_of']}")
    if p.get("note"):
        print(f"  note: {p['note']}")
    print(f"  fleet current:  {json.dumps(current_fleet)}")
    print(f"  fleet proposed: {json.dumps(proposed_fleet)}")
    print("  tasks (proposed):")
    for t in p.get("tasks") or []:
        deps = (" deps=" + ",".join(t.get("deps") or [])) if t.get("deps") else ""
        print(f"    {t.get('id'):<20} owned_by={t.get('owned_by')}{deps}  {t.get('desc', '')}")


def _plan_analyze(doc: dict, tasks_on_board: dict) -> dict:
    """Read-only *shape* analysis of a proposed plan — the work-routing signals a
    navigator needs to judge whether the plan parallelizes well and whether the workers
    are actually isolated from one another. Pure: computes only from `doc` (plus the live
    board, so it can reuse `_plan_validate` as a dry-run preview) and writes nothing.

    Returns: the topological `waves` and `peak_parallel_width`; the `critical_path`
    (longest dependency chain) and its length; `cross_worker_deps` — every edge where a
    task depends on work owned by a *different* worker, the coupling that erodes worktree
    isolation; `prelude_candidates` — tasks two or more others depend on (high fan-in),
    the contracts worth pinning down first; `worker_load`; any `cyclic_tasks`; and the
    `errors` `plan propose` would reject the plan for."""
    tasks = doc.get("tasks") or []
    ids = []
    for t in tasks:
        tid = t.get("id")
        if tid is not None and tid not in ids:
            ids.append(tid)
    owner = {t.get("id"): t.get("owned_by") for t in tasks}
    deps_map = {t.get("id"): list(t.get("deps") or []) for t in tasks}

    waves, cyclic = _toposort_waves(ids, deps_map)
    critical_path = _longest_dep_path(ids, deps_map, waves)

    cross = []
    for t in tasks:
        tid = t.get("id")
        to = owner.get(tid)
        for d in deps_map.get(tid, []):
            do = owner.get(d)
            if to is not None and do is not None and to != do:
                cross.append({"task": tid, "owner": to, "dep": d, "dep_owner": do})

    dependents = {i: 0 for i in ids}
    for t in tasks:
        for d in deps_map.get(t.get("id"), []):
            if d in dependents:
                dependents[d] += 1
    prelude = [
        {"task": i, "dependents": dependents[i], "owner": owner.get(i)}
        for i in ids if dependents[i] >= 2
    ]
    prelude.sort(key=lambda x: (-x["dependents"], str(x["task"])))

    load = {}
    for t in tasks:
        o = owner.get(t.get("id"))
        load[o] = load.get(o, 0) + 1

    return {
        "task_count": len(ids),
        "worker_count": len((doc.get("fleet") or {}).get("workers") or []),
        "max_concurrent": (doc.get("fleet") or {}).get("max_concurrent"),
        "waves": waves,
        "peak_parallel_width": max((len(w) for w in waves), default=0),
        "critical_path": critical_path,
        "critical_path_length": len(critical_path),
        "cross_worker_deps": cross,
        "cross_worker_dep_count": len(cross),
        "prelude_candidates": prelude,
        "worker_load": load,
        "cyclic_tasks": cyclic,
        "errors": _plan_validate(doc, tasks_on_board),
    }


def _plan_analyze_cmd(a):
    """`coord plan analyze` — read-only work-routing analysis of a proposed plan document
    (from --file or stdin). Writes nothing; a navigator runs it BEFORE `plan propose` to
    see the plan's shape and re-slice for better worker isolation before asking for a
    human's approval."""
    try:
        doc = _plan_read_doc(a)
    except (OSError, json.JSONDecodeError) as e:
        _die(f"could not read plan document: {e}")
        return
    r = _plan_analyze(doc, _fold_tasks())
    if getattr(a, "json", False):
        print(json.dumps(r, indent=2))
        return
    print(f"tasks={r['task_count']}  workers={r['worker_count']}  max_concurrent={r['max_concurrent']}")
    print(f"waves={len(r['waves'])}  peak_parallel_width={r['peak_parallel_width']}  "
          f"critical_path_length={r['critical_path_length']}")
    for i, w in enumerate(r["waves"]):
        print(f"  wave {i + 1}: {', '.join(str(x) for x in w)}")
    if r["critical_path"]:
        print(f"critical path: {' -> '.join(str(x) for x in r['critical_path'])}")
    print(f"cross-worker deps: {r['cross_worker_dep_count']}")
    for c in r["cross_worker_deps"]:
        print(f"  {c['task']}({c['owner']}) depends on {c['dep']}({c['dep_owner']})")
    if r["prelude_candidates"]:
        print("prelude candidates (high fan-in -- pin these down as contracts first):")
        for p in r["prelude_candidates"]:
            print(f"  {p['task']}  <- {p['dependents']} dependents  owner={p['owner']}")
    if r["worker_load"]:
        print(f"worker load: {json.dumps(r['worker_load'])}")
    if r["cyclic_tasks"]:
        print(f"WARNING: cyclic tasks would deadlock: {', '.join(str(x) for x in r['cyclic_tasks'])}")
    if r["errors"]:
        print("validation errors (plan propose would reject this plan):")
        for e in r["errors"]:
            print(f"  - {e}")


# --- work-routing: repo seam detection (`coord plan seams`) ------------------
# The GENERATIVE complement to `plan analyze`. Instead of critiquing a plan a human
# already wrote, read the repository's own import graph and SUGGEST a partition of the
# tree into worker-owned path clusters ("seams") that minimizes cross-worker coupling,
# so each worker gets a vertical slice it can build in its own worktree without waiting
# on another worker's output. Read-only, deterministic, and heuristic -- a starting
# point the navigator refines, then feeds into `plan propose` + `plan analyze`.
_SEAM_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", "out", "target", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".tox", ".idea", ".vscode", ".coordination", ".next", "coverage", ".gradle", "vendor",
}
_SEAM_TEXT_EXT = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".cxx",
}
_PY_IMPORT_RE = re.compile(r"^[ \t]*(?:from[ \t]+([.\w]+)[ \t]+import\b|import[ \t]+([\w][\w.]*))", re.M)
_JS_IMPORT_RE = re.compile(r"""(?:\bfrom[ \t]*|\brequire[ \t]*\([ \t]*|\bimport[ \t]*\([ \t]*|\bimport[ \t]+)['"]([^'"]+)['"]""")
_C_INCLUDE_RE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*"([^"]+)"', re.M)


def _seam_module_of(relpath: str) -> str:
    """The 'module' a file belongs to for clustering: its containing directory
    (posix), or '.' for a file at the repo root."""
    d = posixpath.dirname(relpath)
    return d if d else "."


def _seam_owned_path(module: str) -> str:
    """Turn a module directory into an owned-path glob suitable to drop into a plan
    fleet ('src/api' -> 'src/api/**'; the root module -> '*')."""
    return "*" if module == "." else module + "/**"


def _seam_iter_files(root):
    """Yield repo-relative posix paths of candidate source files under `root`, pruning
    VCS/build/dependency directories. Deterministic (sorted)."""
    root = str(root)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SEAM_SKIP_DIRS)
        for fn in filenames:
            if posixpath.splitext(fn)[1].lower() in _SEAM_TEXT_EXT:
                rel = os.path.relpath(os.path.join(dirpath, fn), root)
                out.append(rel.replace(os.sep, "/"))
    return sorted(out)


def _seam_resolve_python(src, module, fileset):
    """Resolve a Python import (absolute dotted or leading-dot relative) to a repo
    file in `fileset`, or None if it is external/unresolvable."""
    src_dir = posixpath.dirname(src)
    if module.startswith("."):
        n_up = len(module) - len(module.lstrip("."))
        rest = module[n_up:]
        base = src_dir
        for _ in range(n_up - 1):
            base = posixpath.dirname(base)
        parts = [p for p in rest.split(".") if p]
        if parts:
            stem = ("/".join([base] + parts)) if base else "/".join(parts)
            cands = [stem + ".py", posixpath.join(stem, "__init__.py")]
        else:
            cands = [posixpath.join(base, "__init__.py") if base else "__init__.py"]
        for c in cands:
            c = c.lstrip("/")
            if c in fileset:
                return c
        return None
    parts = module.split(".")
    for depth in range(len(parts), 0, -1):
        stem = "/".join(parts[:depth])
        for c in (stem + ".py", posixpath.join(stem, "__init__.py")):
            if c in fileset:
                return c
    return None


def _seam_resolve_relative(src, spec, fileset, exts):
    """Resolve a relative JS/TS (or C include) specifier against the source dir,
    trying `exts` and an index/ barrel file. Bare specifiers return None."""
    if not spec.startswith("."):
        return None
    base = posixpath.normpath(posixpath.join(posixpath.dirname(src), spec))
    cands = [base] + [base + e for e in exts] + [posixpath.join(base, "index" + e) for e in exts]
    for c in cands:
        if c in fileset:
            return c
    return None


def _seam_resolve_c(src, inc, fileset):
    """Resolve a quoted C/C++ #include to a repo file: relative to the source dir
    first, then a unique repo-wide basename match."""
    cand = posixpath.normpath(posixpath.join(posixpath.dirname(src), inc))
    if cand in fileset:
        return cand
    base = posixpath.basename(inc)
    matches = [f for f in fileset if posixpath.basename(f) == base]
    return matches[0] if len(matches) == 1 else None


def _scan_repo_graph(root):
    """Walk `root` and parse each source file's INTRA-repo imports into an undirected
    coupling graph. Returns (files, edges): `files` = sorted relpaths; `edges` = sorted
    list of (a, b) file pairs (a < b) that reference each other. External/package
    imports are ignored -- they don't couple two worktrees. I/O; deterministic."""
    files = _seam_iter_files(root)
    fileset = set(files)
    _JS_EXT = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".d.ts"]
    edge_set = set()
    for f in files:
        ext = posixpath.splitext(f)[1].lower()
        try:
            text = (Path(root) / f).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        targets = set()
        if ext in (".py", ".pyi"):
            for m in _PY_IMPORT_RE.finditer(text):
                mod = m.group(1) or m.group(2)
                t = _seam_resolve_python(f, mod, fileset) if mod else None
                if t:
                    targets.add(t)
        elif ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
            for m in _JS_IMPORT_RE.finditer(text):
                t = _seam_resolve_relative(f, m.group(1), fileset, _JS_EXT)
                if t:
                    targets.add(t)
        elif ext in (".c", ".h", ".cc", ".cpp", ".hpp", ".cxx"):
            for m in _C_INCLUDE_RE.finditer(text):
                t = _seam_resolve_c(f, m.group(1), fileset)
                if t:
                    targets.add(t)
        for t in targets:
            if t != f:
                edge_set.add(tuple(sorted((f, t))))
    return files, sorted(edge_set)


# --- work-routing: greenfield declared-graph input --------------------------
# `_scan_repo_graph` reads coupling from code that already exists. For a NEW project
# there is nothing to scan yet -- so the navigator reasons the prose goal into an
# intended module graph (components + the dependencies between them) and declares it.
# `_load_declared_graph` turns that declaration into the SAME (files, edges) shape the
# scanner produces, so `plan seams`/`plan scaffold` partition a greenfield project with
# the identical isolation engine. The language understanding lives in the navigator;
# the deterministic partition + valid-plan emit lives here.
def _normalize_decl_module(raw):
    """Normalize a navigator-declared module path to the shape `_seam_module_of` yields
    ('./src/api/' -> 'src/api'; ''/root -> '.'), rejecting absolute or parent-escaping
    paths so a declared module always maps to an in-repo owned-path glob."""
    p = posixpath.normpath(str(raw).strip().replace("\\", "/"))
    if p in ("", "."):
        return "."
    if p.startswith("/") or p == ".." or p.startswith("../"):
        _die(f"declared module must be a repo-relative path (no absolute or '..' paths): {raw!r}")
    return p


def _load_declared_graph(spec):
    """Turn a navigator-declared module graph into synthetic (files, edges) that
    `_module_graph` rolls up to exactly the declared modules and edge weights -- the
    GREENFIELD counterpart to `_scan_repo_graph`. `spec` is
    {"modules": ["src/api", ...], "edges": [["src/api", "src/auth"(, weight)], ...]}:
    `modules` are the intended component directories (each becomes an owned-path glob),
    `edges` the intended dependencies (undirected coupling; optional integer weight,
    default 1 -- a heavier weight keeps two components together under a forced cut).
    Deterministic; validates strictly and `_die`s on malformed input."""
    if not isinstance(spec, dict):
        _die("declared graph must be a JSON object with 'modules' (and optional 'edges')")
    raw_modules = spec.get("modules")
    if not isinstance(raw_modules, list) or not raw_modules:
        _die("declared graph 'modules' must be a non-empty list of repo-relative path strings")
    modules, seen = [], set()
    for m in raw_modules:
        if not isinstance(m, str) or not m.strip():
            _die(f"declared module must be a non-empty string: {m!r}")
        norm = _normalize_decl_module(m)
        if norm not in seen:
            seen.add(norm)
            modules.append(norm)
    fileof = {m: ("__decl__" if m == "." else m + "/__decl__") for m in modules}
    files = [fileof[m] for m in modules]

    raw_edges = spec.get("edges")
    if raw_edges is not None and not isinstance(raw_edges, list):
        _die("declared graph 'edges' must be a list of [from, to] pairs (optional weight)")
    edges = []
    for e in raw_edges or []:
        if not isinstance(e, (list, tuple)) or len(e) < 2:
            _die(f"declared edge must be [from, to] with an optional weight: {e!r}")
        a, b = _normalize_decl_module(e[0]), _normalize_decl_module(e[1])
        for endpoint in (a, b):
            if endpoint not in seen:
                _die(f"declared edge references undeclared module {endpoint!r}; add it to 'modules'")
        w = 1
        if len(e) >= 3:
            if isinstance(e[2], bool) or not isinstance(e[2], int):
                _die(f"declared edge weight must be an integer >= 1: {e[2]!r}")
            w = e[2]
            if w < 1:
                _die(f"declared edge weight must be >= 1: {w}")
        if a != b:
            edges.extend([tuple(sorted((fileof[a], fileof[b])))] * w)
    return files, sorted(edges)


def _read_graph_spec(path):
    """Read a declared-graph JSON document from `path`, or from stdin when path is '-'."""
    if path == "-":
        raw = sys.stdin.read()
    else:
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError as e:
            _die(f"cannot read declared graph {path!r}: {e}")
            return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        _die(f"declared graph is not valid JSON: {e}")


def _module_graph(files, edges):
    """Pure: roll file-level coupling up to directory 'modules'. Returns
    (modules, weights): `modules` = sorted unique module dirs; `weights` = dict keyed
    by a sorted (a, b) module pair -> count of file edges crossing them. Intra-module
    edges are dropped (always internal / free)."""
    modules = sorted({_seam_module_of(f) for f in files})
    weights = {}
    for a, b in edges:
        ma, mb = _seam_module_of(a), _seam_module_of(b)
        if ma == mb:
            continue
        key = tuple(sorted((ma, mb)))
        weights[key] = weights.get(key, 0) + 1
    return modules, weights


def _agglomerate(modules, weights, target_k):
    """Pure, deterministic agglomerative clustering. Start with each module in its own
    cluster and repeatedly merge the most tightly-coupled pair -- greedy, so heavy edges
    end up INSIDE a cluster and the cut BETWEEN clusters stays small. With
    `target_k=None`, merge every positively-weighted pair: the result is the connected
    components (the zero-coupling partition). With `target_k` set, stop at K clusters;
    if the repo has more independent components than K, the extra merges join the
    SMALLEST clusters (independent groups are forced together only because fewer workers
    were asked for). A module is atomic, so at most `len(modules)` clusters are possible.
    Returns a list of clusters, each a sorted list of module names."""
    clusters = {m: {m} for m in modules}          # cid (a member module) -> set of modules
    adj = {m: {} for m in modules}                # cid -> {cid: weight}
    for (a, b), w in weights.items():
        adj[a][b] = adj[a].get(b, 0) + w
        adj[b][a] = adj[b].get(a, 0) + w

    def _merge(x, y):
        clusters[x] |= clusters.pop(y)
        for nbr, w in adj.pop(y).items():
            if nbr not in clusters or nbr == x:
                continue
            adj[nbr].pop(y, None)
            adj[x][nbr] = adj[x].get(nbr, 0) + w
            adj[nbr][x] = adj[nbr].get(x, 0) + w
        adj[x].pop(y, None)

    def _best_positive():
        best = None
        for x in clusters:
            for y, w in adj[x].items():
                if w <= 0 or y not in clusters or x >= y:
                    continue
                # max weight, then smaller combined size, then lexicographic pair
                cand = (w, -(len(clusters[x]) + len(clusters[y])), x, y)
                if best is None or cand > best:
                    best = cand
        return (best[2], best[3]) if best else None

    def _smallest_pair():
        keys = sorted(clusters, key=lambda c: (len(clusters[c]), c))
        return (keys[0], keys[1]) if len(keys) >= 2 else None

    while True:
        if target_k is not None and len(clusters) <= target_k:
            break
        pair = _best_positive()
        if pair is None:
            break
        _merge(*(pair if pair[0] < pair[1] else (pair[1], pair[0])))
    if target_k is not None:
        while len(clusters) > target_k:
            pair = _smallest_pair()
            if pair is None:
                break
            _merge(*pair)

    return [sorted(members) for members in clusters.values()]


def _plan_seams(files, edges, target_k=None, root_label="."):
    """Pure: from a scanned repo graph, suggest a `target_k`-way (or natural-component)
    partition into worker seams. Mirrors `_plan_analyze` -- takes already-gathered inputs
    so it is unit-testable without touching the filesystem, and writes nothing. Returns
    the seam clusters (with owned-path globs ready for a plan fleet), the natural
    zero-coupling component count, and the cross-seam coupling edges to minimize (each a
    shared contract worth pinning down before the workers fork)."""
    modules, weights = _module_graph(files, edges)
    components = _agglomerate(modules, weights, None)
    clusters = _agglomerate(modules, weights, target_k)

    files_by_module = {}
    for f in files:
        files_by_module.setdefault(_seam_module_of(f), []).append(f)

    def _cfiles(ms):
        return sum(len(files_by_module.get(m, [])) for m in ms)

    clusters.sort(key=lambda ms: (-_cfiles(ms), ms))
    module_to_seam = {m: i for i, ms in enumerate(clusters) for m in ms}

    internal = [0] * len(clusters)
    cross = []
    for (a, b), w in sorted(weights.items()):
        sa, sb = module_to_seam[a], module_to_seam[b]
        if sa == sb:
            internal[sa] += w
        else:
            cross.append({"a": a, "b": b, "weight": w,
                          "a_seam": f"seam-{sa + 1}", "b_seam": f"seam-{sb + 1}"})
    cross.sort(key=lambda e: (-e["weight"], e["a"], e["b"]))

    cluster_objs = [{
        "id": f"seam-{i + 1}",
        "modules": ms,
        "owned_paths": [_seam_owned_path(m) for m in ms],
        "file_count": _cfiles(ms),
        "internal_edge_weight": internal[i],
    } for i, ms in enumerate(clusters)]

    notes = []
    if target_k is not None and target_k > len(modules) and modules:
        notes.append(f"requested {target_k} seams but only {len(modules)} modules exist; returning {len(clusters)}")

    return {
        "root": root_label,
        "file_count": len(files),
        "module_count": len(modules),
        "file_edge_count": len(edges),
        "module_edge_count": len(weights),
        "natural_seams": len(components),
        "requested_workers": target_k,
        "workers": len(clusters),
        "clusters": cluster_objs,
        "cross_cluster_edges": cross,
        "cross_cluster_edge_count": len(cross),
        "cross_cluster_edge_weight": sum(e["weight"] for e in cross),
        "notes": notes,
    }


def _plan_seams_cmd(a):
    """`coord plan seams [--root DIR | --graph FILE] [--workers N] [--json]` -- read-only
    repo-partition suggester. Scans `--root` (default '.') for intra-repo import coupling,
    OR reads a navigator-declared module graph via `--graph FILE` (or `--graph -` for
    stdin) for a greenfield project with no code to scan yet. Prints a seam partition a
    navigator can lift into a `plan propose` fleet. Writes nothing."""
    graph = getattr(a, "graph", None)
    if graph:
        files, edges = _load_declared_graph(_read_graph_spec(graph))
        root_label = "<declared graph>"
    else:
        root_label = getattr(a, "root", None) or "."
        files, edges = _scan_repo_graph(root_label)
    r = _plan_seams(files, edges, target_k=getattr(a, "workers", None), root_label=root_label)
    if getattr(a, "json", False):
        print(json.dumps(r, indent=2))
        return
    if graph:
        print(f"source=declared graph  modules={r['module_count']}  "
              f"intended_edges={r['module_edge_count']}")
    else:
        print(f"root={r['root']}  files={r['file_count']}  modules={r['module_count']}  "
              f"import_edges={r['file_edge_count']}")
    req = f" (requested {r['requested_workers']})" if r["requested_workers"] else ""
    print(f"natural_seams(zero-coupling components)={r['natural_seams']}  seams={r['workers']}{req}")
    for c in r["clusters"]:
        print(f"  {c['id']}: files={c['file_count']}  internal_edges={c['internal_edge_weight']}")
        for op in c["owned_paths"]:
            print(f"      {op}")
    print(f"cross-seam coupling: {r['cross_cluster_edge_count']} edges, "
          f"weight {r['cross_cluster_edge_weight']} (minimize this)")
    for e in r["cross_cluster_edges"]:
        print(f"  {e['a']}({e['a_seam']}) <-> {e['b']}({e['b_seam']})  x{e['weight']}  "
              f"-- shared seam: pin this contract down first")
    for n in r["notes"]:
        print(f"note: {n}")


# --- work-routing: plan scaffold (`coord plan scaffold`) ---------------------
# The bridge from `plan seams` to `plan propose`. `seams` tells you WHERE the isolated
# boundaries are; `scaffold` turns that partition into a VALID, ready-to-edit plan
# document -- fleet wired from the seams, one placeholder task per seam. When the
# partition is naturally decoupled (or under-split), task deps are empty (maximally
# parallel, zero coupling to start). When a forced `--workers N` cut splits a coupled
# component, `scaffold` is CONTRACT-AWARE: for every pair of seams the cut left coupled
# it emits one unowned "contract" prelude task and makes both seams' impl tasks depend
# on it -- turning coupling that would otherwise be dropped on the floor into an explicit
# contracts-first wave-0. The navigator fills in real task descriptions, assigns each
# contract an owner, runs `plan analyze`, and proposes. It is guaranteed to pass
# `_plan_validate` (no cross-worker owned-path overlap; acyclic deps; every task carries a
# `verify` key), so `coord plan scaffold --root . | coord plan analyze` round-trips
# cleanly. Read-only.
def _collapse_owned_paths(globs):
    """Within a SINGLE worker, drop an owned-path glob a shallower one already covers
    ('src/api/**' makes 'src/api/v2/**' redundant). Shallowest-first, deterministic."""
    items = sorted({(_normalize_owned_glob(g), g) for g in globs})
    items.sort(key=lambda it: (len([s for s in it[0].split("/") if s]), it[0]))
    kept = []
    for norm, g in items:
        if any(_path_prefix_overlaps(k, norm) for k, _ in kept):
            continue
        kept.append((norm, g))
    return sorted(g for _, g in kept)


def _merge_nested_clusters(clusters):
    """Union any clusters whose owned_paths would overlap across DISTINCT workers -- a
    worker owning 'src' and another owning 'src/api' is both an illegal plan overlap and
    impossible to isolate. Reuses the plan validator's own overlap test, so the scaffold
    this feeds always passes `_plan_validate`. Deterministic."""
    owned = [[_seam_owned_path(m) for m in ms] for ms in clusters]
    parent = list(range(len(clusters)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(clusters)):
        for j in range(i + 1, len(clusters)):
            if _owned_paths_overlap(owned[i], owned[j]):
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[max(ri, rj)] = min(ri, rj)

    groups = {}
    for i, ms in enumerate(clusters):
        groups.setdefault(find(i), []).extend(ms)
    return [sorted(set(g)) for g in groups.values()]


def _plan_scaffold(files, edges, target_k=None, max_concurrent=None, root_label="."):
    """Pure: turn a repo's seam partition into a VALID plan document (the shape
    `plan propose`/`plan analyze` consume). Mirrors the `_plan_seams` split so the core
    is unit-testable without the filesystem. One placeholder task per seam, owner wired.

    CONTRACT-AWARE: cross-seam coupling only survives a forced `--workers N` cut (a
    natural or under-split partition leaves zero edges between seams). For each pair of
    seams the cut left coupled, emit ONE shared "contract" prelude task -- unowned
    (`owned_by: null`), because a boundary interface belongs to neither worker's private
    worktree until a human decides who publishes it -- and make BOTH coupled seams' impl
    tasks depend on it. Multiple module edges crossing the same seam pair collapse to a
    single contract. Writes nothing."""
    modules, weights = _module_graph(files, edges)
    clusters = _merge_nested_clusters(_agglomerate(modules, weights, target_k))

    files_by_module = {}
    for f in files:
        files_by_module.setdefault(_seam_module_of(f), []).append(f)
    clusters.sort(key=lambda ms: (-sum(len(files_by_module.get(m, [])) for m in ms), ms))

    module_to_seam = {m: i for i, ms in enumerate(clusters) for m in ms}

    # Roll every module edge that crosses a seam boundary up to the (seam_i, seam_j) pair
    # it connects, summing weights, so N crossing module edges become ONE shared contract
    # between the two workers.
    pair_weight = {}
    for (a, b), w in weights.items():
        sa, sb = module_to_seam.get(a), module_to_seam.get(b)
        if sa is None or sb is None or sa == sb:
            continue
        key = (min(sa, sb), max(sa, sb))
        pair_weight[key] = pair_weight.get(key, 0) + w

    workers = []
    contract_deps = {i: [] for i in range(len(clusters))}
    for i, ms in enumerate(clusters):
        owned = _collapse_owned_paths([_seam_owned_path(m) for m in ms])
        workers.append({"id": f"seam-{i + 1}", "owned_paths": owned})

    contracts = []
    for (i, j) in sorted(pair_weight):
        cid = f"contract-seam-{i + 1}-seam-{j + 1}"
        contracts.append({
            "id": cid,
            "desc": (f"CONTRACT shared by seam-{i + 1} and seam-{j + 1} (coupling weight "
                     f"{pair_weight[(i, j)]}): define the interface both seams depend on FIRST, "
                     "then set owned_by to whichever seam will publish it."),
            "owned_by": None,
            "deps": [],
            "verify": None,
        })
        contract_deps[i].append(cid)
        contract_deps[j].append(cid)

    impls = []
    for i, w in enumerate(workers):
        sid = w["id"]
        impls.append({
            "id": f"{sid}-impl",
            "desc": "TODO: implement " + ", ".join(w["owned_paths"]),
            "owned_by": sid,
            "deps": sorted(contract_deps[i]),
            "verify": None,
        })

    tasks = contracts + impls
    mc = max_concurrent if (isinstance(max_concurrent, int) and not isinstance(max_concurrent, bool)
                            and max_concurrent >= 1) else max(len(workers), 1)
    if contracts:
        note = (f"scaffold from {len(workers)} seam(s) over {root_label}: a forced cut left "
                f"{len(contracts)} cross-seam contract(s). Each is an UNOWNED prelude (assign "
                "owned_by to whichever seam publishes it) that both coupled seams wait on. Fill "
                "in task desc, assign every contract an owner, then `coord plan analyze` before "
                "proposing.")
    else:
        note = (f"scaffold from {len(workers)} seam(s) over {root_label}: one placeholder task per "
                "seam, no deps (max parallelism). Edit task desc/deps -- add contracts-first prelude "
                "deps for any shared interface -- then `coord plan analyze` before proposing.")
    return {"note": note, "fleet": {"max_concurrent": mc, "workers": workers}, "tasks": tasks}


def _plan_scaffold_cmd(a):
    """`coord plan scaffold [--root DIR | --graph FILE] [--workers N] [--max-concurrent M]`
    -- emit a valid plan document to stdout, ready to pipe onward:
    `coord plan scaffold --root . | coord plan analyze`, then edit and `plan propose`.
    Derives the seams by scanning `--root` (default '.'), OR from a navigator-declared
    module graph via `--graph FILE` (or `--graph -` for stdin) for a greenfield project
    with no code to scan yet. Read-only; writes nothing to the coordination plane."""
    graph = getattr(a, "graph", None)
    if graph:
        files, edges = _load_declared_graph(_read_graph_spec(graph))
        root_label = "<declared graph>"
    else:
        root_label = getattr(a, "root", None) or "."
        files, edges = _scan_repo_graph(root_label)
    doc = _plan_scaffold(
        files, edges,
        target_k=getattr(a, "workers", None),
        max_concurrent=getattr(a, "max_concurrent", None),
        root_label=root_label,
    )
    print(json.dumps(doc, indent=2))


def _plan_approve(a):
    """Apply a pending plan ATOMICALLY (COCKPIT_SPEC.md §3.2/§3.4, keystone). Models
    `_state_approve`'s lock/transaction shape: serialize under the `__state__` lease,
    re-validate against the CURRENT board (a task id may have landed since propose),
    and only then perform every effect -- create tasks, set desired.fleet, bump
    desired.version by exactly 1, mark the plan approved, and emit the initial capped
    `spawn` directive batch. Never touches `authorized_phase` or any other desired
    key. Human/orchestrator-gated (the navigator is DENIED this by the hook, Phase 6)."""
    if not a.id:
        _die("`plan approve` requires --id")
        return
    for _ in range(50):
        if _acquire_raw("__state__", a.session or "state", 30):
            break
        time.sleep(0.1)
    else:
        _die("could not acquire state lock")
        return
    try:
        plan = _fold_plans().get(a.id)
        if not plan:
            _die(f"no such plan '{a.id}'")
            return
        if plan["status"] != "pending":
            _die(f"plan '{a.id}' is '{plan['status']}' — not pending")
            return
        # re-validate at approve time: the board may have moved since propose.
        errors = _plan_validate(
            {"fleet": plan.get("fleet") or {}, "tasks": plan.get("tasks") or []}, _fold_tasks()
        )
        if errors:
            _die("plan approve rejected (re-validation failed):\n  " + "\n  ".join(errors))
            return

        # from here on, every effect must land -- no more validation failures possible.
        for t in plan.get("tasks") or []:
            ev = {
                "ts": now(),
                "id": t.get("id"),
                "desc": t.get("desc", ""),
                "deps": t.get("deps") or [],
                "status": "open",
                "claimed_by": None,
            }
            if t.get("owned_by") is not None:
                ev["owned_by"] = t["owned_by"]
            if t.get("verify") is not None:
                ev["verify"] = t["verify"]
            if t.get("max_attempts") is not None:
                ev["max_attempts"] = t["max_attempts"]
            _append(_p("board", "tasks.jsonl"), ev)

        path = _p("state", "desired.json")
        st = _read_json(path, {"version": 0, "desired": {}})
        st.setdefault("desired", {})["fleet"] = plan.get("fleet") or {}
        st["version"] = st.get("version", 0) + 1
        st["updated"] = iso()
        new_version = st["version"]
        _atomic_write(path, json.dumps(st, indent=2))
        _append(_p("board", "events.jsonl"),
                {"ts": now(), "event": "plan_approved", "pid": a.id, "version": new_version})

        _append(_p("state", "plans.jsonl"), {"ts": now(), "id": a.id, "status": "approved"})

        fleet = plan.get("fleet") or {}
        workers = fleet.get("workers") or []
        max_concurrent = fleet.get("max_concurrent", 0)
        spawned = []
        for w in workers[:max_concurrent]:
            _append(_p("state", "directives.jsonl"), {
                "kind": "spawn",
                "worker": w.get("id"),
                "owned_paths": w.get("owned_paths") or [],
                "as_of": new_version,
                "ts": iso(),
            })
            spawned.append(w.get("id"))

        print(f"approved {a.id}: {len(plan.get('tasks') or [])} tasks created; "
              f"desired.fleet set; state version -> {new_version}")
        if spawned:
            print(f"  spawn directives: {spawned}")
    finally:
        _release_raw("__state__", a.session or "state")


def _plan_reject(a):
    """Mark a pending plan rejected. No version bump, no fleet change, no tasks, no
    directives -- rejection is a pure no-op besides the ledger entry."""
    if not a.id:
        _die("`plan reject` requires --id")
        return
    plan = _fold_plans().get(a.id)
    if not plan:
        _die(f"no such plan '{a.id}'")
        return
    if plan["status"] != "pending":
        _die(f"plan '{a.id}' is '{plan['status']}' — not pending")
        return
    _append(_p("state", "plans.jsonl"), {"ts": now(), "id": a.id, "status": "rejected"})
    print(f"rejected plan {a.id} (version unchanged)")


def cmd_claim(a):
    tasks = _fold_tasks()
    t = tasks.get(a.task)
    if not t:
        _die(f"no such task '{a.task}'")
    if t["status"] not in ("open",):
        _die(f"task '{a.task}' is '{t['status']}' (claimed_by={t['claimed_by']}) — not claimable")
    unmet = [d for d in t["deps"] if tasks.get(d, {}).get("status") != "done"]
    if unmet:
        _die(f"task '{a.task}' blocked on unmet deps: {unmet}")
    # atomic claim: guard with a per-task lockdir so two sessions can't both win
    if not _acquire_raw(f"task-{a.task}", a.session, 10):
        _die(f"task '{a.task}' is being claimed by another session right now")
    try:
        tasks = _fold_tasks()  # re-read under lock
        if tasks[a.task]["status"] != "open":
            _die(f"task '{a.task}' was just claimed by {tasks[a.task]['claimed_by']}")
        cur_version = _read_json(_p("state", "desired.json"), {"version": 0}).get("version", 0)
        _append(_p("board", "tasks.jsonl"),
                {"ts": now(), "id": a.task, "status": "claimed", "claimed_by": a.session,
                 "claimed_at_version": cur_version})
    finally:
        _release_raw(f"task-{a.task}", a.session)
    print(f"{a.session} claimed {a.task}")


def cmd_complete(a):
    tasks = _fold_tasks()
    if a.task not in tasks:
        _die(f"no such task '{a.task}'")
    t = tasks[a.task]
    # Stale-completion guard: refuse to complete a task this session doesn't
    # currently hold. If it was requeued/invalidated out from under the worker,
    # its folded status is no longer 'claimed' by this session — so a worker that
    # kept going on an invalidated task cannot mark it done stale.
    if t.get("status") != "claimed" or t.get("claimed_by") != a.session:
        _die(f"cannot complete '{a.task}': it is '{t.get('status')}' "
             f"(claimed_by={t.get('claimed_by')}), not claimed by '{a.session}' — "
             f"it may have been requeued/invalidated; re-claim before completing")
    _append(_p("board", "tasks.jsonl"), {"ts": now(), "id": a.task, "status": a.status, "claimed_by": a.session})
    print(f"task {a.task} -> {a.status}")


# ---- coded acceptance gates -------------------------------------------------
def _resolve_worktree(session: str | None) -> str:
    """Resolve a session's registered worktree, falling back to the current cwd."""
    if session:
        reg = _read_json(_p("registry", f"{session}.json"))
        if reg and reg.get("worktree"):
            return reg["worktree"]
    return os.getcwd()


def _run_verify_cmd(verify_cmd: str, worktree: str) -> int:
    """Run a task's verify command in `worktree`, inheriting env. Returns the rc."""
    import subprocess
    proc = subprocess.run(verify_cmd, shell=True, cwd=worktree, env=os.environ.copy())
    return proc.returncode


def cmd_verify(a):
    tasks = _fold_tasks()
    t = tasks.get(a.task)
    if not t:
        _die(f"no such task '{a.task}'")
    verify_cmd = t.get("verify")
    if not verify_cmd:
        # No verify command set on this task -> trivially passing.
        _append(_p("board", "tasks.jsonl"), {"ts": now(), "id": a.task, "verified": True})
        result = {"task": a.task, "verified": True, "trivial": True}
        print(json.dumps(result, indent=2) if a.json else f"task '{a.task}' has no verify command — trivially verified")
        return
    worktree = _resolve_worktree(t.get("claimed_by"))
    rc = _run_verify_cmd(verify_cmd, worktree)
    if rc == 0:
        _append(_p("board", "tasks.jsonl"), {"ts": now(), "id": a.task, "verified": True})
        result = {"task": a.task, "verified": True, "returncode": rc}
        print(json.dumps(result, indent=2) if a.json else f"task '{a.task}' verified (rc=0)")
    else:
        attempts = t.get("attempts", 0) + 1
        _append(_p("board", "tasks.jsonl"), {"ts": now(), "id": a.task, "verified": False, "attempts": attempts})
        result = {"task": a.task, "verified": False, "returncode": rc, "attempts": attempts}
        if a.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"coord: task '{a.task}' verify failed (rc={rc}), attempts={attempts}",
                  file=sys.stderr)
        sys.exit(rc if rc != 0 else 1)


# ---- escalations (the human/navigator interface) ---------------------------
ESCALATION_KINDS = ("decision", "blocker", "fork")


def _escalation_path(eid: str) -> Path:
    return _p("escalations", f"{eid}.json")


def _open_escalation(session: str, kind: str, body: str, task: str | None = None) -> str:
    """Write an open escalation and return its eid. Shared by `cmd_escalate` and `tick`."""
    if kind not in ESCALATION_KINDS:
        raise ValueError(f"--kind must be one of {ESCALATION_KINDS}, got '{kind}'")
    st = _read_json(_p("state", "desired.json"), {"version": 0})
    eid = str(time.time_ns())
    esc = {
        "eid": eid,
        "from": session,
        "kind": kind,
        "task": task,
        "body": body,
        "status": "open",
        "created": iso(),
        "as_of": st.get("version", 0),
        "resolved_note": None,
    }
    _atomic_write(_escalation_path(eid), json.dumps(esc, indent=2))
    return eid


def cmd_escalate(a):
    if a.kind not in ESCALATION_KINDS:
        _die(f"--kind must be one of {ESCALATION_KINDS}, got '{a.kind}'")
    eid = _open_escalation(a.session, a.kind, a.body, a.task)
    print(f"escalated {eid}: [{a.kind}] from={a.session} task={a.task} — {a.body}")


def _read_escalations():
    escs = []
    for ef in sorted(_p("escalations").glob("*.json")):
        esc = _read_json(ef, {})
        if esc:
            escs.append(esc)
    return escs


def cmd_escalations(a):
    open_escs = [e for e in _read_escalations() if e.get("status") == "open"]
    open_escs.sort(key=lambda e: e.get("eid", ""), reverse=True)
    if a.json:
        print(json.dumps(open_escs, indent=2))
        return
    if not open_escs:
        print("(no open escalations)")
        return
    for e in open_escs:
        print(f"  [{e['kind']:>8}] {e['eid']} from={e['from']} task={e.get('task')}  {e['body']}")


def cmd_resolve(a):
    path = _escalation_path(a.id)
    esc = _read_json(path)
    if esc is None:
        _die(f"no such escalation '{a.id}'")
    esc["status"] = "resolved"
    esc["resolved_note"] = a.note
    _atomic_write(path, json.dumps(esc, indent=2))
    # Close the decision loop: deliver the human's answer back to the session that
    # raised the escalation, as a checkpoint message tied to the current desired
    # version so it surfaces as fresh. This is what lets `coord escalate --kind
    # decision` stand in for a direct human prompt -- the asking session receives the
    # answer at its next `coord checkpoint` instead of blocking on a modal the cockpit
    # cannot clear. `tick` is a pseudo-session (its blocker escalations are FYI to the
    # human), so there is nobody there to answer.
    asker = esc.get("from")
    if asker and asker != "tick":
        version = _read_json(_p("state", "desired.json"), {"version": 0}).get("version", 0)
        answer = a.note if a.note else "(resolved with no note)"
        _send_message("human", asker, f"escalation {a.id} resolved: {answer}", as_of=version)
    print(f"resolved {a.id}")


# ---- leases (public lock cmds) --------------------------------------------
def _heartbeat_stale(session: str | None) -> bool:
    if not session:
        return True
    reg = _read_json(_p("registry", f"{session}.json"))
    if not reg:
        return True
    return (now() - reg.get("heartbeat", 0)) > HEARTBEAT_STALE_SEC


def cmd_lock(a):
    if a.action == "acquire":
        ok = _acquire_raw(a.resource, a.session, a.ttl)
        if ok:
            print(f"lock '{a.resource}' acquired by {a.session} (ttl {a.ttl}s)")
        else:
            _die(f"lock '{a.resource}' is held (holder alive or lease valid)")
    elif a.action == "release":
        ok = _release_raw(a.resource, a.session)
        print(f"lock '{a.resource}' released" if ok else f"cannot release '{a.resource}' (held by another)")


# ---- messaging (per-recipient inbox + staleness metadata) -----------------
def cmd_send(a):
    msg = {
        "seq": time.time_ns(),
        "ts": iso(),
        "from": a.sender,
        "to": a.to,
        "body": a.body,
        "as_of": a.as_of,                      # desired-state version this msg assumed
        "expires": (now() + a.ttl) if a.ttl else None,
    }
    _append(_p("inbox", f"{a.to}.jsonl"), msg)
    print(f"queued message {msg['seq']} -> {a.to}")


def _inbox_partition(session: str, current_version: int):
    """Return (fresh, stale) messages after this session's cursor.
    Stale = TTL-expired OR references an older desired-state version than current."""
    cursor_path = _p("cursor", f"{session}.json")
    cursor = _read_json(cursor_path, {"seq": 0}).get("seq", 0)
    msgs = [m for m in _read_jsonl(_p("inbox", f"{session}.jsonl")) if m.get("seq", 0) > cursor]
    fresh, stale = [], []
    for m in msgs:
        expired = m.get("expires") is not None and now() > m["expires"]
        outdated = m.get("as_of") is not None and m["as_of"] < current_version
        (stale if (expired or outdated) else fresh).append(m)
    return fresh, stale


def cmd_inbox(a):
    state = _read_json(_p("state", "desired.json"), {"version": 0})
    fresh, stale = _inbox_partition(a.session, state.get("version", 0))
    # advance cursor past everything we just surfaced (fresh + stale)
    all_seen = fresh + stale
    if all_seen:
        newest = max(m["seq"] for m in all_seen)
        _atomic_write(_p("cursor", f"{a.session}.json"), json.dumps({"seq": newest, "updated": iso()}))
    print(json.dumps({"fresh": fresh, "stale_skipped": len(stale)}, indent=2))


# ---- control + status + reap ----------------------------------------------
def cmd_stop(a):
    name = f"STOP-{a.session}" if a.session else "STOP"
    _atomic_write(_p("control", name), iso())
    print(f"wrote {name}")


def cmd_resume(a):
    name = f"STOP-{a.session}" if a.session else "STOP"
    (_p("control", name)).unlink(missing_ok=True)
    print(f"cleared {name}")


def cmd_status(a):
    print(f"control plane: {root()}")
    print(f"stop flags: {[p.name for p in _p('control').glob('STOP*')] or 'none'}")
    print("sessions:")
    for rp in sorted(_p("registry").glob("*.json")):
        reg = _read_json(rp, {})
        age = now() - reg.get("heartbeat", 0)
        live = "ALIVE" if age <= HEARTBEAT_STALE_SEC else "STALE"
        print(f"  {reg.get('session'):<16} {reg.get('role'):<14} {live:<6} "
              f"hb {int(age)}s ago  branch={reg.get('branch')}")
    print("locks:")
    for ld in sorted(_p("locks").glob("*.lockdir")):
        meta = _read_json(ld / "meta.json", {})
        print(f"  {ld.name[:-8]:<24} holder={meta.get('holder')} age={int(now()-meta.get('acquired',0))}s")
    print("tasks:")
    cmd_tasks(a)


# ---- cockpit (COCKPIT SPEC §3.6: single read-only aggregate view) ----------
def _build_cockpit_view() -> dict:
    """Read-only aggregate of the whole cockpit plane. Pure read: no writes, no
    heartbeat/lock side effects -- safe to call anytime. Reuses the existing fold/
    read helpers (`_get_fleet`, `_fold_tasks`, `_heartbeat_stale`, `_read_escalations`,
    `_fold_plans`, the proposals reader, the directives ledger) rather than
    reimplementing any state derivation."""
    st = _read_json(_p("state", "desired.json"), {"version": 0, "desired": {}})
    desired = st.get("desired", {})
    fleet = _get_fleet(desired)
    declared_workers = fleet.get("workers") or []
    max_concurrent = fleet.get("max_concurrent", 0)

    tasks = _fold_tasks()
    by_status = {"open": [], "claimed": [], "done": [], "failed": []}
    counts = {}
    for t in tasks.values():
        status = t.get("status", "open")
        counts[status] = counts.get(status, 0) + 1
        by_status.setdefault(status, []).append(t["id"])

    live_ids = set()
    workers_view = []
    for w in declared_workers:
        wid = w.get("id")
        reg = _read_json(_p("registry", f"{wid}.json"), {})
        stale = _heartbeat_stale(wid)
        age = (now() - reg.get("heartbeat", 0)) if reg else None
        if not stale:
            live_ids.add(wid)
        holds = next((t["id"] for t in tasks.values()
                      if t.get("status") == "claimed" and t.get("claimed_by") == wid), None)
        workers_view.append({
            "id": wid,
            "liveness": "stale" if stale else "fresh",
            "heartbeat_age_sec": (int(age) if age is not None else None),
            "task": holds,
        })

    escalations = _read_escalations()
    decisions = [e for e in escalations if e.get("kind") == "decision" and e.get("status") == "open"]
    blockers = [e for e in escalations if e.get("kind") == "blocker" and e.get("status") == "open"]

    pending_plans = sorted(p["id"] for p in _fold_plans().values() if p.get("status") == "pending")
    pending_proposals = sorted(
        prop.get("pid") for pf in _p("state", "proposals").glob("*.json")
        for prop in [_read_json(pf, {})] if prop.get("status") == "pending"
    )

    # no consumer exists yet (Phase 7), so every spawn directive ever emitted for a
    # worker is "unconsumed" -- same idempotency notion used by tick's spawn step.
    spawn_workers = sorted({d.get("worker") for d in _read_jsonl(_p("state", "directives.jsonl"))
                            if d.get("kind") == "spawn"})

    return {
        "desired": {
            "version": st.get("version", 0),
            "authorized_phase": desired.get("authorized_phase"),
            "fleet": {
                "max_concurrent": max_concurrent,
                "workers": [w.get("id") for w in declared_workers],
            },
        },
        "tasks": {
            "counts": counts,
            "by_status": by_status,
        },
        "workers": workers_view,
        "decisions": decisions,
        "blockers": blockers,
        "pending": {
            "plans": pending_plans,
            "proposals": pending_proposals,
        },
        "capacity": {
            "live": len(live_ids),
            "max_concurrent": max_concurrent,
            "unconsumed_spawn_directives": len(spawn_workers),
            "unconsumed_spawn_workers": spawn_workers,
        },
    }


def cmd_cockpit(a):
    view = _build_cockpit_view()
    if getattr(a, "json", False):
        print(json.dumps(view, indent=2))
        return
    d = view["desired"]
    print(f"desired: version={d['version']} authorized_phase={d['authorized_phase']} "
          f"fleet(max_concurrent={d['fleet']['max_concurrent']}, workers={d['fleet']['workers']})")
    print("tasks:")
    for status, ids in view["tasks"]["by_status"].items():
        print(f"  {status:<8} {len(ids):>3}  {ids}")
    print("workers:")
    for w in view["workers"]:
        print(f"  {w['id']:<12} {w['liveness']:<6} age={w['heartbeat_age_sec']}s task={w['task']}")
    print(f"decisions: {len(view['decisions'])}  blockers: {len(view['blockers'])}")
    print(f"pending: plans={view['pending']['plans']} proposals={view['pending']['proposals']}")
    c = view["capacity"]
    print(f"capacity: live={c['live']}/{c['max_concurrent']}  "
          f"unconsumed_spawn={c['unconsumed_spawn_directives']} {c['unconsumed_spawn_workers']}")


def _reap_once():
    """Release locks held by dead sessions and requeue their in-progress tasks.
    Returns (reaped_locks, requeued) — lists of (name, holder) tuples — without printing,
    so both `cmd_reap` and `cmd_tick` can share this logic."""
    reaped_locks, requeued = [], []
    for ld in _p("locks").glob("*.lockdir"):
        meta = _read_json(ld / "meta.json", {})
        h = meta.get("holder")
        if _heartbeat_stale(h) and (now() - meta.get("acquired", 0)) > meta.get("ttl", 0):
            try:
                (ld / "meta.json").unlink(missing_ok=True); ld.rmdir(); reaped_locks.append((ld.name[:-8], h))
            except OSError:
                pass
    for t in _fold_tasks().values():
        if t["status"] == "claimed" and _heartbeat_stale(t["claimed_by"]):
            _append(_p("board", "tasks.jsonl"),
                    {"ts": now(), "id": t["id"], "status": "open", "claimed_by": None})
            requeued.append((t["id"], t["claimed_by"]))
    return reaped_locks, requeued


def cmd_reap(a):
    """Orchestrator hygiene: release locks held by dead sessions and requeue
    their in-progress tasks so a crashed worker doesn't wedge the fleet."""
    reaped_locks, requeued = _reap_once()
    print(json.dumps({"reaped_locks": reaped_locks, "requeued_tasks": requeued}, indent=2))


# ---- the keystone: tick -----------------------------------------------------
def _send_message(sender: str, to: str, body: str, as_of: int | None, ttl: int | None = None):
    """Queue a message the same shape `cmd_send` writes. Used by `tick`'s directives."""
    msg = {
        "seq": time.time_ns(),
        "ts": iso(),
        "from": sender,
        "to": to,
        "body": body,
        "as_of": as_of,
        "expires": (now() + ttl) if ttl else None,
    }
    _append(_p("inbox", f"{to}.jsonl"), msg)


def _tick_once() -> dict:
    """Perform ONE deterministic reconciliation pass and return the effects report.

    HARD INVARIANT (AUTONOMY_SPEC §3.3, extended by COCKPIT_SPEC §3.5 to the spawn
    step): this never changes `authorized_phase` or `desired.version`, never
    approves/rejects a plan or proposal, and never performs a git write. It only reads
    desired-state and reconciles WITHIN the current human authorization — dispatch,
    stall-nudge, and spawn are all advisory (they queue messages/directives; actually
    spawning or waking a session is a runtime adapter's job, not this command's).

    Shared by `cmd_tick` (single pass, prints once) and `cmd_run` (the thin loop
    wrapper, which calls this repeatedly) so both go through the identical code path.
    """
    st = _read_json(_p("state", "desired.json"), {"version": 0, "desired": {}})
    version = st.get("version", 0)
    desired = st.get("desired", {})
    default_max_attempts = desired.get("max_attempts_default", 1)

    # 1. reap dead sessions: release their expired-stale locks, requeue their claimed tasks.
    reaped_locks, reaped_tasks = _reap_once()
    reaped = ([{"type": "lock", "resource": name, "holder": holder} for name, holder in reaped_locks]
              + [{"type": "task", "id": tid, "holder": holder} for tid, holder in reaped_tasks])

    verified, requeued, failed, dispatched, nudged = [], [], [], [], []

    # 2. verify acceptance for every done-but-unverified task with a `verify` command.
    tasks = _fold_tasks()
    for t in list(tasks.values()):
        if t["status"] != "done" or not t.get("verify") or t.get("verified"):
            continue
        worktree = _resolve_worktree(t.get("claimed_by"))
        rc = _run_verify_cmd(t["verify"], worktree)
        if rc == 0:
            _append(_p("board", "tasks.jsonl"), {"ts": now(), "id": t["id"], "verified": True})
            verified.append(t["id"])
            continue
        attempts = t.get("attempts", 0) + 1
        max_attempts = t.get("max_attempts", default_max_attempts)
        if attempts < max_attempts:
            _append(_p("board", "tasks.jsonl"),
                    {"ts": now(), "id": t["id"], "status": "open", "claimed_by": None,
                     "verified": False, "attempts": attempts})
            claimant = t.get("claimed_by")
            if claimant:
                _send_message("tick", claimant,
                               f"task '{t['id']}' failed verify (attempt {attempts}/{max_attempts}); "
                               f"requeued to open — re-claim and fix", as_of=version)
            requeued.append({"task": t["id"], "attempts": attempts, "notified": claimant})
        else:
            _append(_p("board", "tasks.jsonl"),
                    {"ts": now(), "id": t["id"], "status": "failed", "verified": False, "attempts": attempts})
            eid = _open_escalation("tick", "blocker",
                                    f"task '{t['id']}' failed verify {attempts} time(s) "
                                    f"(max_attempts={max_attempts}); marked failed", task=t["id"])
            failed.append({"task": t["id"], "attempts": attempts, "escalation": eid})

    # re-fold after step 2's writes so dispatch/nudge see the current state.
    tasks = _fold_tasks()
    registries = {rp.stem: _read_json(rp, {}) for rp in _p("registry").glob("*.json")}

    # 3. spawn (COCKPIT_SPEC §3.5): ensure declared-but-not-live fleet workers that own
    # needed work get a `spawn` directive, capped at `max_concurrent`. Purely advisory --
    # `coord` never spawns/wakes a session itself; a runtime adapter (Phase 7) consumes
    # the directives ledger and calls the host's real spawn capability. Runs BEFORE
    # dispatch so a freshly-declared worker can be seen as a dispatch candidate once the
    # adapter has actually spawned it (a later tick, once it registers and heartbeats).
    spawned = []
    fleet = _get_fleet(desired)
    declared_workers = fleet.get("workers") or []
    max_concurrent = fleet.get("max_concurrent", 0)
    if declared_workers:
        live_ids = {w["id"] for w in declared_workers if not _heartbeat_stale(w.get("id"))}
        # a task with owned_by: null owns no worker and never makes anyone "missing".
        needed_ids = {t.get("owned_by") for t in tasks.values()
                      if t.get("owned_by") and t["status"] in ("open", "claimed")}
        missing_workers = [w for w in declared_workers
                            if w.get("id") in needed_ids and w.get("id") not in live_ids]
        # idempotency: a missing worker with an already-queued (unconsumed -- there is no
        # consumer yet) spawn directive counts toward the cap but is NOT re-emitted.
        existing_spawn_ids = {d.get("worker") for d in _read_jsonl(_p("state", "directives.jsonl"))
                               if d.get("kind") == "spawn"}
        new_candidates = [w for w in missing_workers if w.get("id") not in existing_spawn_ids]

        # Every worker already live OR already holding an unconsumed spawn directive occupies
        # a real concurrency slot -- including a declared worker that owns no task, whose
        # directive `plan approve` emits positionally (workers[:max_concurrent]) and which
        # therefore never appears in `missing_workers`. Count the union of both so that slot is
        # never invisible to the cap; otherwise tick can spawn one worker too many and the live
        # fleet breaches the declared hard cap (COCKPIT_SPEC.md §3.1).
        capacity_used = len(live_ids | existing_spawn_ids)
        capacity_remaining = max(max_concurrent - capacity_used, 0)
        to_spawn, over_cap = new_candidates[:capacity_remaining], new_candidates[capacity_remaining:]

        for w in to_spawn:
            _append(_p("state", "directives.jsonl"), {
                "kind": "spawn",
                "worker": w.get("id"),
                "owned_paths": w.get("owned_paths") or [],
                "as_of": version,
                "ts": iso(),
            })
            spawned.append(w.get("id"))

        if over_cap:
            _CAP_DECISION = "fleet at cap; raise max_concurrent or wait"
            already_open = any(
                e.get("kind") == "decision" and e.get("status") == "open" and e.get("body") == _CAP_DECISION
                for e in _read_escalations()
            )
            if not already_open:
                _open_escalation("tick", "decision", _CAP_DECISION)

    def _is_idle(session: str) -> bool:
        if _heartbeat_stale(session):
            return False
        return not any(tt["status"] == "claimed" and tt["claimed_by"] == session for tt in tasks.values())

    # 4. dispatch (advisory): ready, unclaimed tasks -> message an idle worker to claim.
    # Tasks don't currently carry their own path set, so "owned_paths match the task" is
    # applied at the granularity we have: any idle, live, registered worker is a candidate.
    max_parallel = desired.get("max_parallel")
    claimed_count = sum(1 for t in tasks.values() if t["status"] == "claimed")
    idle_workers = [s for s in registries if _is_idle(s)]
    for t in tasks.values():
        if t["status"] != "open":
            continue
        claimed_by = t.get("claimed_by")
        # A task is dispatchable when unclaimed, OR when its lingering claimant is stale.
        # (The locked reference fold's `is not None` guard means a stale claimant's id
        # lingers after reap's `claimed_by: None` event -- see AUTONOMY_SPEC gap. Do not
        # "fix" the fold; treat a stale claimant as no claimant for dispatch purposes.)
        if claimed_by and not _heartbeat_stale(claimed_by):
            continue  # a LIVE claimant still holds this task; not ours to dispatch
        if any(tasks.get(d, {}).get("status") != "done" for d in t.get("deps", [])):
            continue
        if max_parallel is not None and claimed_count >= max_parallel:
            break
        candidates = [w for w in idle_workers if w != claimed_by]
        if not candidates:
            break
        worker = candidates[0]
        idle_workers.remove(worker)
        _send_message("tick", worker, f"task '{t['id']}' is ready and unclaimed; claim it", as_of=version)
        dispatched.append({"task": t["id"], "to": worker})
        claimed_count += 1  # advisory: reserve capacity against max_parallel for this pass

    # 5. stall nudge (advisory): claimed task whose heartbeat is aging but not yet reap-stale.
    for t in tasks.values():
        if t["status"] != "claimed":
            continue
        claimant = t.get("claimed_by")
        reg = registries.get(claimant)
        if not reg:
            continue
        age = now() - reg.get("heartbeat", 0)
        if HEARTBEAT_STALE_SEC * 0.5 <= age < HEARTBEAT_STALE_SEC:
            _send_message("tick", claimant, f"task '{t['id']}' claimed but heartbeat is aging; continue", as_of=version)
            nudged.append(t["id"])

    # 6. budgets: a global time-budget breach stops the fleet (never touches authorized_phase).
    time_budget = desired.get("time_budget_sec")
    if time_budget is not None:
        oldest_registered = None
        for reg in registries.values():
            ts = reg.get("registered")
            if ts:
                oldest_registered = ts if oldest_registered is None or ts < oldest_registered else oldest_registered
        if oldest_registered:
            started = time.mktime(time.strptime(oldest_registered, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
            if (now() - started) > time_budget:
                cmd_stop(argparse.Namespace(session=None))

    # 7. surface open escalations for the human/navigator to act on.
    awaiting_decision = [e for e in _read_escalations() if e.get("status") == "open"]

    return {
        "reaped": reaped,
        "verified": verified,
        "requeued": requeued,
        "spawned": spawned,
        "dispatched": dispatched,
        "nudged": nudged,
        "failed": failed,
        "awaiting_decision": awaiting_decision,
    }


def cmd_tick(a):
    print(json.dumps(_tick_once(), indent=2))


def cmd_run(a):
    """Thin loop wrapper (AUTONOMY_SPEC §3.4): repeatedly call the same tick pass
    `coord tick` uses, sleeping `--interval` seconds between passes. ALL reconciliation
    logic lives in `_tick_once()` -- this command only sleeps, loops, and counts.

    Stops after `--max-ticks` passes (`--once` == `--max-ticks 1`), or immediately
    (before running another pass) once a fleet-wide STOP flag is set. Never touches
    `authorized_phase` or proposals itself -- it inherits tick's invariant by construction."""
    max_ticks = 1 if a.once else a.max_ticks
    count = 0
    while max_ticks is None or count < max_ticks:
        if _p("control", "STOP").exists():
            break
        print(json.dumps(_tick_once(), indent=2))
        count += 1
        if max_ticks is not None and count >= max_ticks:
            break
        if a.interval:
            time.sleep(a.interval)


# ---- arg parsing ----------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(prog="coord", description="Filesystem control plane for parallel Copilot agents.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)

    r = sub.add_parser("register"); r.set_defaults(func=cmd_register)
    r.add_argument("--session", required=True); r.add_argument("--role", required=True)
    r.add_argument("--branch", required=True); r.add_argument("--worktree")
    r.add_argument("--paths", help="comma-separated globs this session is allowed to write")

    h = sub.add_parser("heartbeat"); h.set_defaults(func=cmd_heartbeat); h.add_argument("--session", required=True)

    c = sub.add_parser("checkpoint"); c.set_defaults(func=cmd_checkpoint); c.add_argument("--session", required=True)

    st = sub.add_parser("state"); st.set_defaults(func=cmd_state)
    st.add_argument("action", choices=["show", "set", "propose", "proposals", "approve", "reject"])
    st.add_argument("--key"); st.add_argument("--value")
    st.add_argument("--session")
    st.add_argument("--invalidates", help="comma-separated task ids to requeue when a proposal is approved")
    st.add_argument("--note", help="free-text note attached to a proposal")
    st.add_argument("--id", help="proposal id (pid) for approve/reject")
    st.add_argument("--reason", help="reason recorded when rejecting a proposal")

    at = sub.add_parser("add-task"); at.set_defaults(func=cmd_add_task)
    at.add_argument("--id", required=True); at.add_argument("--desc", default=""); at.add_argument("--deps", default="")
    at.add_argument("--verify", help="shell command that must exit 0 for this task to be accepted")
    at.add_argument("--max-attempts", dest="max_attempts", type=int, help="max failed verify attempts before escalating")

    sub.add_parser("tasks").set_defaults(func=cmd_tasks)

    pl = sub.add_parser("plan"); pl.set_defaults(func=cmd_plan)
    pl.add_argument("action", choices=["propose", "show", "approve", "reject", "analyze", "seams", "scaffold"])
    pl.add_argument("--file", help="path to a plan JSON document (default: read from stdin)")
    pl.add_argument("--id", help="plan id (pid) for `plan show`/`plan approve`/`plan reject`")
    pl.add_argument("--session", help="session id used as the state-lock holder for `plan approve`")
    pl.add_argument("--root", help="repo root to analyze for `plan seams`/`plan scaffold` (default: .)")
    pl.add_argument("--graph", help="declared module-graph JSON for greenfield `plan seams`/`plan scaffold` (a file path, or - for stdin); bypasses --root scanning")
    pl.add_argument("--workers", type=int, help="target seam count for `plan seams`/`plan scaffold` (default: natural components)")
    pl.add_argument("--max-concurrent", dest="max_concurrent", type=int, help="fleet.max_concurrent for `plan scaffold` (default: seam count)")
    pl.add_argument("--json", action="store_true", help="machine-readable output for `plan analyze`/`plan seams`")

    sub.add_parser("plans").set_defaults(func=cmd_plans)

    vf = sub.add_parser("verify"); vf.set_defaults(func=cmd_verify)
    vf.add_argument("--task", required=True); vf.add_argument("--json", action="store_true")

    cl = sub.add_parser("claim"); cl.set_defaults(func=cmd_claim)
    cl.add_argument("--session", required=True); cl.add_argument("--task", required=True)

    cp = sub.add_parser("complete"); cp.set_defaults(func=cmd_complete)
    cp.add_argument("--session", required=True); cp.add_argument("--task", required=True)
    cp.add_argument("--status", default="done", choices=["done", "failed", "open"])

    lk = sub.add_parser("lock"); lk.set_defaults(func=cmd_lock)
    lk.add_argument("action", choices=["acquire", "release"]); lk.add_argument("--session", required=True)
    lk.add_argument("--resource", required=True); lk.add_argument("--ttl", type=int, default=120)

    es = sub.add_parser("escalate"); es.set_defaults(func=cmd_escalate)
    es.add_argument("--session", required=True); es.add_argument("--kind", required=True, choices=list(ESCALATION_KINDS))
    es.add_argument("--body", required=True); es.add_argument("--task", default=None)

    esl = sub.add_parser("escalations"); esl.set_defaults(func=cmd_escalations)
    esl.add_argument("--json", action="store_true")

    rs = sub.add_parser("resolve"); rs.set_defaults(func=cmd_resolve)
    rs.add_argument("--id", required=True); rs.add_argument("--note", default=None)

    sd = sub.add_parser("send"); sd.set_defaults(func=cmd_send)
    sd.add_argument("--from", dest="sender", required=True); sd.add_argument("--to", required=True)
    sd.add_argument("--body", required=True); sd.add_argument("--as-of", dest="as_of", type=int)
    sd.add_argument("--ttl", type=int, default=0, help="seconds until the message is considered stale")

    ib = sub.add_parser("inbox"); ib.set_defaults(func=cmd_inbox); ib.add_argument("--session", required=True)

    stp = sub.add_parser("stop"); stp.set_defaults(func=cmd_stop); stp.add_argument("--session")
    rsm = sub.add_parser("resume"); rsm.set_defaults(func=cmd_resume); rsm.add_argument("--session")

    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("reap").set_defaults(func=cmd_reap)

    tk = sub.add_parser("tick"); tk.set_defaults(func=cmd_tick)
    tk.add_argument("--json", action="store_true", help="accepted for symmetry; tick always prints JSON")

    rn = sub.add_parser("run"); rn.set_defaults(func=cmd_run)
    rn.add_argument("--interval", type=float, default=5.0, help="seconds to sleep between tick passes")
    rn.add_argument("--max-ticks", dest="max_ticks", type=int, default=None, help="stop after this many passes (default: unbounded)")
    rn.add_argument("--once", action="store_true", help="equivalent to --max-ticks 1")

    ck = sub.add_parser("cockpit"); ck.set_defaults(func=cmd_cockpit)
    ck.add_argument("--json", action="store_true", help="print the aggregate as JSON")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
