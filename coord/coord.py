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
import argparse, json, os, sys, time, glob, fnmatch
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
    out = {
        "session": a.session,
        "time": iso(),
        "stop": stop,                       # non-empty => HALT at this checkpoint
        "desired_version": state.get("version", 0),
        "desired": state.get("desired", {}),
        "messages": fresh,                  # act on these
        "stale_messages_skipped": len(stale),
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
                  "verify", "max_attempts", "attempts", "verified"):
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


def cmd_tick(a):
    """Perform ONE deterministic reconciliation pass and report the effects as JSON.

    HARD INVARIANT (AUTONOMY_SPEC §3.3): this command never changes `authorized_phase`,
    never approves/rejects a proposal, and never performs a git write. It only reads
    desired-state and reconciles WITHIN the current human authorization — dispatch and
    stall-nudge steps are advisory (they queue messages; delivery/waking a session is a
    runtime adapter's job, not this command's).
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

    def _is_idle(session: str) -> bool:
        if _heartbeat_stale(session):
            return False
        return not any(tt["status"] == "claimed" and tt["claimed_by"] == session for tt in tasks.values())

    # 3. dispatch (advisory): ready, unclaimed tasks -> message an idle worker to claim.
    # Tasks don't currently carry their own path set, so "owned_paths match the task" is
    # applied at the granularity we have: any idle, live, registered worker is a candidate.
    max_parallel = desired.get("max_parallel")
    claimed_count = sum(1 for t in tasks.values() if t["status"] == "claimed")
    idle_workers = [s for s in registries if _is_idle(s)]
    for t in tasks.values():
        if t["status"] != "open" or t.get("claimed_by"):
            continue
        if any(tasks.get(d, {}).get("status") != "done" for d in t.get("deps", [])):
            continue
        if max_parallel is not None and claimed_count >= max_parallel:
            break
        if not idle_workers:
            break
        worker = idle_workers.pop(0)
        _send_message("tick", worker, f"task '{t['id']}' is ready and unclaimed; claim it", as_of=version)
        dispatched.append({"task": t["id"], "to": worker})
        claimed_count += 1  # advisory: reserve capacity against max_parallel for this pass

    # 4. stall nudge (advisory): claimed task whose heartbeat is aging but not yet reap-stale.
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

    # 5. budgets: a global time-budget breach stops the fleet (never touches authorized_phase).
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

    # 6. surface open escalations for the human/navigator to act on.
    awaiting_decision = [e for e in _read_escalations() if e.get("status") == "open"]

    report = {
        "reaped": reaped,
        "verified": verified,
        "requeued": requeued,
        "dispatched": dispatched,
        "nudged": nudged,
        "failed": failed,
        "awaiting_decision": awaiting_decision,
    }
    print(json.dumps(report, indent=2))


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
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
