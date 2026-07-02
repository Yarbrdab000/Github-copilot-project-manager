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
    for d in ("registry", "inbox", "cursor", "locks", "state", "log", "control", "board"):
        _p(d).mkdir(parents=True, exist_ok=True)
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
        if expired and holder_stale:
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


# ---- tasks (append-only board + atomic claim) -----------------------------
def _fold_tasks():
    """Fold the append-only task ledger into current task state (event sourcing)."""
    tasks = {}
    for ev in _read_jsonl(_p("board", "tasks.jsonl")):
        tid = ev.get("id")
        if not tid:
            continue
        t = tasks.setdefault(tid, {"id": tid, "status": "open", "deps": [], "claimed_by": None})
        for k in ("desc", "deps", "status", "claimed_by"):
            if k in ev and ev[k] is not None:
                t[k] = ev[k]
    return tasks


def cmd_add_task(a):
    _append(_p("board", "tasks.jsonl"),
            {"ts": now(), "id": a.id, "desc": a.desc, "deps": [d for d in (a.deps or "").split(",") if d],
             "status": "open", "claimed_by": None})
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
        _append(_p("board", "tasks.jsonl"),
                {"ts": now(), "id": a.task, "status": "claimed", "claimed_by": a.session})
    finally:
        _release_raw(f"task-{a.task}", a.session)
    print(f"{a.session} claimed {a.task}")


def cmd_complete(a):
    tasks = _fold_tasks()
    if a.task not in tasks:
        _die(f"no such task '{a.task}'")
    _append(_p("board", "tasks.jsonl"), {"ts": now(), "id": a.task, "status": a.status, "claimed_by": a.session})
    print(f"task {a.task} -> {a.status}")


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


def cmd_reap(a):
    """Orchestrator hygiene: release locks held by dead sessions and requeue
    their in-progress tasks so a crashed worker doesn't wedge the fleet."""
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
    print(json.dumps({"reaped_locks": reaped_locks, "requeued_tasks": requeued}, indent=2))


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
    st.add_argument("action", choices=["show", "set"]); st.add_argument("--key"); st.add_argument("--value")
    st.add_argument("--session")

    at = sub.add_parser("add-task"); at.set_defaults(func=cmd_add_task)
    at.add_argument("--id", required=True); at.add_argument("--desc", default=""); at.add_argument("--deps", default="")

    sub.add_parser("tasks").set_defaults(func=cmd_tasks)

    cl = sub.add_parser("claim"); cl.set_defaults(func=cmd_claim)
    cl.add_argument("--session", required=True); cl.add_argument("--task", required=True)

    cp = sub.add_parser("complete"); cp.set_defaults(func=cmd_complete)
    cp.add_argument("--session", required=True); cp.add_argument("--task", required=True)
    cp.add_argument("--status", default="done", choices=["done", "failed", "open"])

    lk = sub.add_parser("lock"); lk.set_defaults(func=cmd_lock)
    lk.add_argument("action", choices=["acquire", "release"]); lk.add_argument("--session", required=True)
    lk.add_argument("--resource", required=True); lk.add_argument("--ttl", type=int, default=120)

    sd = sub.add_parser("send"); sd.set_defaults(func=cmd_send)
    sd.add_argument("--from", dest="sender", required=True); sd.add_argument("--to", required=True)
    sd.add_argument("--body", required=True); sd.add_argument("--as-of", dest="as_of", type=int)
    sd.add_argument("--ttl", type=int, default=0, help="seconds until the message is considered stale")

    ib = sub.add_parser("inbox"); ib.set_defaults(func=cmd_inbox); ib.add_argument("--session", required=True)

    stp = sub.add_parser("stop"); stp.set_defaults(func=cmd_stop); stp.add_argument("--session")
    rsm = sub.add_parser("resume"); rsm.set_defaults(func=cmd_resume); rsm.add_argument("--session")

    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("reap").set_defaults(func=cmd_reap)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
