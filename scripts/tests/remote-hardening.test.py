#!/usr/bin/env python3
"""Hermetic race, path, PAUSE-admission, and ambiguity regressions for Phases 6/7."""

from __future__ import annotations

import os
import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock

SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE / "scripts"))

import remote_backend as rb  # noqa: E402
from remote_assignment import RemoteError  # noqa: E402

PYTHON = sys.executable
failures: list[str] = []


def check(condition: bool, label: str) -> None:
    print(("ok - " if condition else "FAIL - ") + label)
    if not condition:
        failures.append(label)


def child(base: Path, code: str, *args: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SOURCE / "scripts") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen([PYTHON, "-c", code, str(base), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)


def link_directory(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        junction = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True, check=False,
        )
        if junction.returncode != 0:
            raise AssertionError(f"directory link fixture could not be created: {junction.stderr}")


BEGIN = r'''
import sys
sys.path.insert(0, sys.argv[2] + r"/scripts")
import remote_backend as rb
from pathlib import Path
try:
    h = rb.begin_attempt(Path(sys.argv[1]), "grok-bot", dispatch_id="dispatch-race",
                         operation="dispatch", assignment_digest="d" * 64,
                         request_digest="r" * 64, max_attempts=3)
    print("replay" if h.replayed else "unreconciled" if h.unreconciled else "open")
except Exception as exc:
    print(type(exc).__name__ + ":" + str(exc))
'''

RECONCILE = r'''
import sys
sys.path.insert(0, sys.argv[2] + r"/scripts")
import remote_backend as rb
from pathlib import Path
try:
    rb.reconcile_attempt(Path(sys.argv[1]), "grok-bot", sys.argv[3],
                         observed="unknown", observer="race-test")
    print("closed")
except Exception as exc:
    print(type(exc).__name__ + ":" + str(exc))
'''

RECONCILIATION_INPUT = r'''
import importlib.util
import json
import sys
sys.path.insert(0, sys.argv[2] + r"/scripts")
import remote_backend as rb
from pathlib import Path
spec = importlib.util.spec_from_file_location(
    "cursor_cloud_race", Path(sys.argv[2]) / "scripts" / "cursor-cloud-adapter.py")
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)
manifest = rb.validate_manifest({
    "schema_version": 1, "name": "cursor-cloud", "kind": "cursor-cloud",
    "description": "race fixture", "endpoint": "https://api.cursor.com",
    "endpoint_ref": None, "credential_ref": "secret:CURSOR_CLOUD_API_KEY",
    "signing_key_ref": None, "capacity_pool": "cursor-cloud-pool",
    "host_id": "cursor-cloud", "billing_basis": "included", "on_demand": False,
    "timeout_seconds": 60, "max_attempts": 3, "concurrency_lease": 1,
    "publication": {"default_branch": "main", "integration_branch": "recursion/integration",
                     "task_branch_pattern": "recursion/extern/<task-id>",
                     "allowed_result_branch_patterns": ["cursor/*"]}}, "cursor-cloud")
adapter = cc.CursorCloudAdapter(Path(sys.argv[1]), manifest, rb.RefusingTransport())
try:
    adapter.reconciliation_input("dispatch-input", json.loads(sys.argv[3]))
    print("ok")
except Exception as exc:
    print(type(exc).__name__ + ":" + getattr(exc, "code", ""))
'''

with tempfile.TemporaryDirectory(prefix="recursion-remote-hardening-") as raw:
    base = Path(raw)
    (base / "state").mkdir()

    first = child(base, BEGIN, str(SOURCE))
    second = child(base, BEGIN, str(SOURCE))
    first_out, first_err = first.communicate(timeout=30)
    second_out, second_err = second.communicate(timeout=30)
    rows = rb.read_attempts(base, "grok-bot")
    states = {line.strip() for line in (first_out + second_out).splitlines()}
    check(len(rows) == 1 and "open" in states and
          ("unreconciled" in states or "open" in states),
          "concurrent processes reserve one idempotency key atomically: " +
          repr((first_out, first_err, second_out, second_err)))

    handle = rb.begin_attempt(base, "grok-bot", dispatch_id="dispatch-race",
                              operation="dispatch", assignment_digest="d" * 64,
                              request_digest="r" * 64, max_attempts=3)
    rb.reconcile_attempt(base, "grok-bot", handle.attempt_id,
                         observed="landed", observer="hardening-test")
    third = child(base, BEGIN, str(SOURCE))
    fourth = child(base, BEGIN, str(SOURCE))
    third_out, third_err = third.communicate(timeout=30)
    fourth_out, fourth_err = fourth.communicate(timeout=30)
    rows = rb.read_attempts(base, "grok-bot")
    check(len(rows) == 2 and third_out.strip() == "replay" and fourth_out.strip() == "replay",
          "a closed successful reservation remains idempotent across processes: " +
          repr((third_out, third_err, fourth_out, fourth_err)))

    open_handle = rb.begin_attempt(base, "grok-bot", dispatch_id="dispatch-reconcile",
                                   operation="dispatch", assignment_digest="e" * 64,
                                   request_digest="s" * 64, max_attempts=3)
    left = child(base, RECONCILE, str(SOURCE), open_handle.attempt_id)
    right = child(base, RECONCILE, str(SOURCE), open_handle.attempt_id)
    left_out, left_err = left.communicate(timeout=30)
    right_out, right_err = right.communicate(timeout=30)
    check(sorted((left_out.strip(), right_out.strip())) == [
        "RemoteError:request-invalid: attempt " + open_handle.attempt_id +
        " is 'unreconciled', not open",
        "closed",
    ], "concurrent reconciliation has one transactional winner: " +
          repr((left_out, left_err, right_out, right_err)))

    input_base = base / "reconciliation-input"
    (input_base / "state").mkdir(parents=True)
    rb.write_session(input_base, "cursor-cloud", "dispatch-input", {
        "kind": "cursor-cloud-session", "dispatch_id": "dispatch-input",
        "run_id": "provider-run", "assignment_digest": "a" * 64,
        "source_repository": "https://github.com/NotSkittlz/example-ops",
        "authorized_repositories": [{
            "repository": "https://github.com/NotSkittlz/example-ops",
            "source_branch": "main", "source_sha": "b" * 40,
        }],
    })
    first_input = json.dumps({"status": "FINISHED", "target": {
        "branchName": "cursor/one", "commit": "c" * 40}})
    second_input = json.dumps({"status": "FINISHED", "target": {
        "branchName": "cursor/two", "commit": "d" * 40}})
    first = child(input_base, RECONCILIATION_INPUT, str(SOURCE), first_input)
    second = child(input_base, RECONCILIATION_INPUT, str(SOURCE), second_input)
    first_out, first_err = first.communicate(timeout=30)
    second_out, second_err = second.communicate(timeout=30)
    check(sorted((first_out.strip(), second_out.strip())) == [
        "RemoteError:request-invalid", "ok",
    ], "cross-process reconciliation input has one winner and one conflict: " +
          repr((first_out, first_err, second_out, second_err)))
    winner = rb.read_session(input_base, "cursor-cloud", "dispatch-input")["reconciliation"]
    winner_payload = (first_input if winner["repositories"][0]["result_branch"] == "cursor/one"
                      else second_input)
    replay = child(input_base, RECONCILIATION_INPUT, str(SOURCE), winner_payload)
    replay_out, replay_err = replay.communicate(timeout=30)
    check(replay_out.strip() == "ok",
          "cross-process replay of the winning reconciliation input is idempotent: " +
          repr((replay_out, replay_err)))

    path_base = base / "path"
    (path_base / "state").mkdir(parents=True)
    (path_base / "state" / "remote").write_text("not a directory", encoding="utf-8")
    try:
        rb.write_session(path_base, "grok-bot", "dispatch-path", {"a": 1})
        path_refused = False
    except RemoteError as exc:
        path_refused = exc.code == "request-invalid"
    check(path_refused, "runtime storage refuses a non-directory path component")

    del_path = path_base / "state" / "remote"
    del_path.unlink()
    outside = base / "outside"
    outside.mkdir()
    try:
        del_path.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        if os.name != "nt":
            raise AssertionError(f"symlink hardening test could not create its fixture: {exc}")
        junction = subprocess.run(["cmd", "/c", "mklink", "/J", str(del_path),
                                   str(outside)], capture_output=True, text=True)
        if junction.returncode != 0:
            raise AssertionError(f"link hardening fixture could not be created: {junction.stderr}")
    try:
        rb.write_session(path_base, "grok-bot", "dispatch-path", {"a": 1})
        symlink_refused = False
    except RemoteError as exc:
        symlink_refused = exc.code == "request-invalid"
    check(symlink_refused and not (outside / "grok-bot").exists(),
          "runtime storage refuses symlink traversal")

    journal_base = base / "journal"
    journal_parent = journal_base / "state" / "remote"
    journal_parent.mkdir(parents=True)
    journal_target = outside / "journal-target"
    journal_target.mkdir()
    journal_link = journal_parent / "grok-bot"
    try:
        journal_link.symlink_to(journal_target, target_is_directory=True)
    except OSError as exc:
        if os.name != "nt":
            raise AssertionError(f"journal symlink fixture could not be created: {exc}")
        junction = subprocess.run(["cmd", "/c", "mklink", "/J", str(journal_link),
                                   str(journal_target)], capture_output=True, text=True)
        if junction.returncode != 0:
            raise AssertionError(f"journal link fixture could not be created: {junction.stderr}")
    try:
        rb.read_attempts(journal_base, "grok-bot")
        journal_refused = False
    except RemoteError as exc:
        journal_refused = exc.code == "request-invalid"
    check(journal_refused, "journal reads refuse a symlinked storage directory")

    # Every remote durable site uses the same pinned-parent writer/reader seam.  Keep the
    # fixtures distinct so a later site cannot pass merely because an earlier one rejected the
    # shared backend root.
    sites_base = base / "durable-sites"
    (sites_base / "state/remote/grok-bot/attempts").mkdir(parents=True)
    (sites_base / "state/remote/grok-bot/sessions").mkdir(parents=True)
    (sites_base / "state/remote/grok-bot/usage").mkdir(parents=True)
    sites_outside = base / "durable-sites-outside"
    sites_outside.mkdir()

    atomic_json_path = sites_base / "state/remote/grok-bot/attempts/a.json"
    attempts_dir = atomic_json_path.parent
    attempts_dir.rename(sites_base / "state/remote/grok-bot/attempts-real")
    link_directory(attempts_dir, sites_outside)
    try:
        rb.atomic_json(atomic_json_path, {"site": "attempt-projection"})
        atomic_json_refused = False
    except RemoteError as exc:
        atomic_json_refused = exc.code == "request-invalid"
    check(atomic_json_refused and not list(sites_outside.iterdir()),
          "atomic_json refuses a linked ancestor without an outside write")
    attempts_dir.unlink()
    (sites_base / "state/remote/grok-bot/attempts-real").rename(attempts_dir)

    atomic_bytes_path = sites_base / "state/linked-state/PAUSE"
    linked_state = atomic_bytes_path.parent
    linked_state.mkdir()
    linked_state.rename(sites_base / "state/real-state")
    link_directory(linked_state, sites_outside)
    try:
        rb.atomic_bytes(atomic_bytes_path, b"PAUSED site\n")
        atomic_bytes_refused = False
    except RemoteError as exc:
        atomic_bytes_refused = exc.code == "request-invalid"
    check(atomic_bytes_refused and not list(sites_outside.iterdir()),
          "atomic_bytes (the PAUSE writer) refuses a linked ancestor without an outside write")
    linked_state.unlink()
    (sites_base / "state/real-state").rename(linked_state)

    session_path = sites_base / "state/remote/grok-bot/sessions/dispatch-site.json"
    outside_session = sites_outside / session_path.name
    outside_session.write_text('{"site":"outside"}\n', encoding="utf-8")
    sessions_dir = session_path.parent
    sessions_dir.rename(sites_base / "state/remote/grok-bot/sessions-real")
    link_directory(sessions_dir, sites_outside)
    try:
        rb.read_session(sites_base, "grok-bot", "dispatch-site")
        session_refused = False
    except RemoteError as exc:
        session_refused = exc.code == "request-invalid"
    check(session_refused, "session reader refuses a linked ancestor before opening outside data")
    sessions_dir.unlink()
    (sites_base / "state/remote/grok-bot/sessions-real").rename(sessions_dir)

    usage_path = sites_base / "state/remote/grok-bot/usage/dispatch-site.json"
    outside_usage = sites_outside / usage_path.name
    outside_usage.write_text('{"site":"outside"}\n', encoding="utf-8")
    usage_dir = usage_path.parent
    usage_dir.rename(sites_base / "state/remote/grok-bot/usage-real")
    link_directory(usage_dir, sites_outside)
    try:
        rb.read_usage(sites_base, "grok-bot", "dispatch-site")
        usage_refused = False
    except RemoteError as exc:
        usage_refused = exc.code == "request-invalid"
    check(usage_refused, "usage reader refuses a linked ancestor before opening outside data")
    usage_dir.unlink()
    (sites_base / "state/remote/grok-bot/usage-real").rename(usage_dir)

    list_path = sites_base / "state/remote/grok-bot/sessions/list-site.json"
    list_path.write_text('{"site":"inside"}\n', encoding="utf-8")
    sessions_dir.rename(sites_base / "state/remote/grok-bot/sessions-real")
    link_directory(sessions_dir, sites_outside)
    try:
        rb.list_sessions(sites_base, "grok-bot")
        list_refused = False
    except RemoteError as exc:
        list_refused = exc.code == "request-invalid"
    check(list_refused, "session enumeration refuses a linked ancestor before opening outside data")
    sessions_dir.unlink()
    (sites_base / "state/remote/grok-bot/sessions-real").rename(sessions_dir)

    swap_base = base / "durable-replace-swap"
    swap_parent = swap_base / "state/remote/grok-bot"
    swap_parent.mkdir(parents=True)
    swap_path = swap_parent / "projection.json"
    swap_old = swap_parent.parent / "grok-bot-old"
    swap_outside = base / "durable-replace-outside"
    swap_outside.mkdir()

    def swap_remote_parent(_path: Path) -> None:
        swap_parent.rename(swap_old)
        link_directory(swap_parent, swap_outside)

    try:
        with mock.patch("remote_backend._before_remote_leaf_replace",
                        side_effect=swap_remote_parent):
            rb.atomic_json(swap_path, {"site": "must-not-write"})
        swap_refused = False
    except RemoteError as exc:
        swap_refused = exc.code == "request-invalid"
    check(swap_refused and not list(swap_outside.iterdir()),
          "remote durable replacement refuses a deterministic ancestor swap")

    # The outbound lock gives PAUSE and send a total order: a pause setter waits for an
    # admitted operation, then the next operation sees PAUSE before its transport call.
    safe_base = base / "safe"
    (safe_base / "state").mkdir(parents=True)
    entered = threading.Event()
    release = threading.Event()

    def hold_admission() -> None:
        with rb.outbound_admission(safe_base, "grok-bot", "race probe"):
            entered.set()
            release.wait(5)

    holder = threading.Thread(target=hold_admission)
    holder.start()
    check(entered.wait(5), "outbound admission can be held for the PAUSE race proof")
    setter = child(safe_base, r'''
import sys
sys.path.insert(0, sys.argv[2] + r"/scripts")
import remote_backend as rb
from pathlib import Path
with rb.remote_state_lock(Path(sys.argv[1]), "pause-control"):
    rb.atomic_bytes(rb.pause_path(Path(sys.argv[1])), b"PAUSED race\n")
print("paused")
''', str(SOURCE))
    # The setter must not publish PAUSE while an outbound critical section owns the lock.
    try:
        setter.wait(timeout=0.2)
        blocked = setter.poll() is None
    except subprocess.TimeoutExpired:
        blocked = True
    check(blocked, "PAUSE publication waits behind an admitted outbound critical section")
    release.set()
    holder.join(timeout=10)
    setter_out, _ = setter.communicate(timeout=30)
    check(setter_out.strip() == "paused" and rb.is_paused(safe_base),
          "PAUSE publishes after admission and blocks the next operation")
    pause_path = rb.pause_path(safe_base)
    pause_old = safe_base / "state/PAUSE-old"
    pause_outside = base / "pause-remove-outside"
    pause_outside.write_bytes(b"outside pause target\n")

    def swap_pause_leaf(_path: Path) -> None:
        pause_path.rename(pause_old)
        os.link(pause_outside, pause_path)

    pause_remove_refused = False
    try:
        with mock.patch("remote_backend._before_remote_leaf_remove", side_effect=swap_pause_leaf):
            rb.remove_runtime_file(pause_path)
        pause_remove_refused = False
    except RemoteError as exc:
        pause_remove_refused = exc.code == "request-invalid"
    finally:
        if pause_path.exists() or pause_path.is_symlink():
            pause_path.unlink()
        pause_old.rename(pause_path)
    check(pause_remove_refused and pause_outside.read_bytes() == b"outside pause target\n",
          "PAUSE removal refuses a checked-leaf swap without touching the outside target")
    clearer = subprocess.run(
        [PYTHON, str(SOURCE / "scripts" / "remote-pause.py"), "clear",
         "--base", str(safe_base)], capture_output=True, text=True, check=False,
    )
    check(clearer.returncode == 0 and not rb.is_paused(safe_base),
          "remote-pause.py clears PAUSE through the pinned parent and same-handle removal")

if failures:
    print(f"{len(failures)} check(s) failed")
    raise SystemExit(1)
print("all checks passed")
