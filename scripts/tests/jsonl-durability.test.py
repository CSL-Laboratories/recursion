#!/usr/bin/env python3
"""Focused controls for the shared crash-safe JSONL append boundary."""

from __future__ import annotations

import atexit
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock


_NT_POSIX_SHIM = '''\
"""Windows stand-ins for POSIX facilities the Linux controller assumes unconditionally."""
import os
import tempfile

if os.name == "nt" and not getattr(os, "_recursion_nt_posix_shim", False):
    os._recursion_nt_posix_shim = True
    # The shared `mkstemp -> fchmod -> os.replace` writers raise AttributeError while their
    # descriptor is still open, and the cleanup `unlink` of that open file then masks it as
    # `[WinError 32] ... used by another process`, which callers report as a fabricated
    # "taskctl lock contended". POSIX mode bits carry no meaning on NTFS, so a no-op is exact.
    os.fchmod = lambda _descriptor, _mode: None
    _real_open, _scratch = os.open, tempfile.TemporaryFile()

    def _open(path, flags, mode=0o777, **kwargs):
        # A directory has no openable handle on Windows, so notification-outbox.fsync_dir
        # cannot take one. Return a syncable duplicate: the call sequence stays valid and the
        # sync is inert, which is what runtime-state.py and jsonl_durable.py already do on nt
        # behind their own `os.name` guards.
        try:
            inert = not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT) and os.path.isdir(path)
        except (TypeError, ValueError, OSError):
            inert = False
        return os.dup(_scratch.fileno()) if inert else _real_open(path, flags, mode, **kwargs)

    os.open = _open
'''


def _install_nt_posix_shim() -> None:
    """Neutralize POSIX-only facilities on the Windows authoring host; production is Linux.

    `fcntl`, `os.fchmod`, and a read-only `os.open` of a directory are all absent here, and
    each one fails as if an invariant broke rather than as a missing platform feature. These
    controls are hermetic and single-process, so no-contention stand-ins let the real state
    machines run instead of a platform artifact. The shim directory is exported on PYTHONPATH,
    where `sitecustomize` re-applies it to the child interpreters these controls spawn. On
    Linux this returns immediately and nothing below it is reachable.
    """
    if os.name != "nt":
        return
    directory = Path(tempfile.mkdtemp(prefix="nt-posix-shim-"))
    atexit.register(shutil.rmtree, directory, True)
    (directory / "fcntl.py").write_text(
        "LOCK_SH = 1\nLOCK_EX = 2\nLOCK_NB = 4\nLOCK_UN = 8\n"
        "def flock(_descriptor, _flags):\n    return None\n",
        encoding="utf-8", newline="\n",
    )
    (directory / "_recursion_nt_posix.py").write_text(
        _NT_POSIX_SHIM, encoding="utf-8", newline="\n",
    )
    (directory / "sitecustomize.py").write_text(
        "import _recursion_nt_posix  # noqa: F401\n", encoding="utf-8", newline="\n",
    )
    sys.path.insert(0, str(directory))
    __import__("_recursion_nt_posix")
    inherited = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = (
        str(directory) + os.pathsep + inherited if inherited else str(directory)
    )


_install_nt_posix_shim()

SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE / "scripts"))

import jsonl_durable  # noqa: E402
from jsonl_durable import (  # noqa: E402
    JsonlDurabilityError, acknowledge_jsonl, append_jsonl, find_jsonl_objects,
    read_jsonl_objects, read_jsonl_since,
)
from transition_receipt import commit_event  # noqa: E402


def control_torn_tail() -> tuple[bool, str]:
    root = Path(tempfile.mkdtemp(prefix="jsonl-torn-"))
    try:
        path = root / "state/events.jsonl"
        path.parent.mkdir(parents=True)
        first = b'{"id":"committed"}\n'
        torn = b'{"id":"partial","message":"exact bytes'
        path.write_bytes(first + torn)
        evidence = append_jsonl(path, {"id": "new"}).quarantine
        expected = first + b'{"id":"new"}\n'
        quarantine = root / "state/jsonl-quarantine"
        payloads = list(quarantine.glob("*.bin"))
        metadata = list(quarantine.glob("*.json"))
        if path.read_bytes() != expected or len(payloads) != 1 or len(metadata) != 1:
            return False, f"journal={path.read_bytes()!r} payloads={len(payloads)} metadata={len(metadata)}"
        row = json.loads(metadata[0].read_text(encoding="utf-8"))
        passed = (
            payloads[0].read_bytes() == torn
            and row == evidence
            and row.get("status") == "open"
            and row.get("requires_human_review") is True
            and row.get("journal") == "state/events.jsonl"
            and row.get("journal_offset") == len(first)
            and row.get("tail_sha256") == hashlib.sha256(torn).hexdigest()
        )
        return passed, json.dumps(row, sort_keys=True)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def control_complete_malformed_fails_closed() -> tuple[bool, str]:
    root = Path(tempfile.mkdtemp(prefix="jsonl-malformed-"))
    try:
        path = root / "state/events.jsonl"
        path.parent.mkdir(parents=True)
        original = b'{"id":"valid"}\n{"id":}\n'
        path.write_bytes(original)
        try:
            append_jsonl(path, {"id": "must-not-append"})
        except JsonlDurabilityError as exc:
            return path.read_bytes() == original, str(exc)
        return False, "malformed committed row was accepted"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def control_record_limits_are_checked_at_public_boundaries() -> tuple[bool, str]:
    """A bool record bound must be refused before a public API delegates or mutates state."""
    root = Path(tempfile.mkdtemp(prefix="jsonl-record-limit-"))
    path = root / "events.jsonl"
    batch = jsonl_durable.IndexedBatch(
        (), 0, jsonl_durable.ZERO_CHAIN_SHA256, False
    )
    cases = (
        ("read", lambda: read_jsonl_objects(path, True)),
        ("find", lambda: find_jsonl_objects(path, "id", "value", True)),
        ("since", lambda: read_jsonl_since(path, "fixture-consumer", True)),
        ("acknowledge", lambda: acknowledge_jsonl(path, "fixture-consumer", batch, True)),
        ("append", lambda: append_jsonl(path, {"id": "value"}, True)),
    )
    # No journal is created, and that is the instrument. Without the guard each read entry point
    # reaches _verified_descriptor, takes its FileNotFoundError arm and returns empty; append
    # creates the journal. So an unguarded build is observable from outside the module -- an empty
    # result, or a file on disk -- and nothing here has to patch the module under test.
    try:
        for label, operation in cases:
            try:
                result = operation()
            except JsonlDurabilityError as exc:
                if "invalid JSONL record-size bound" not in str(exc):
                    return False, f"{label}: wrong refusal: {exc}"
            else:
                return False, f"{label}: bool record bound was accepted, returned {result!r}"
            if path.exists():
                return False, f"{label}: refused but the journal was created"
        return True, "all five public boundaries refused bool without creating the journal"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def control_record_limit_positive_paths_still_work() -> tuple[bool, str]:
    """The guard must refuse bad bounds without refusing good ones."""
    root = Path(tempfile.mkdtemp(prefix="jsonl-record-limit-ok-"))
    path = root / "events.jsonl"
    try:
        append_jsonl(path, {"id": "first"})
        append_jsonl(path, {"id": "second"}, 65536)
        read_all = read_jsonl_objects(path, 65536)
        found = find_jsonl_objects(path, "id", "second", 65536)
        since = read_jsonl_since(path, "fixture-consumer", 65536)
        return (
            [entry["id"] for entry in read_all] == ["first", "second"]
            and [entry["id"] for entry in found] == ["second"]
            and since is not None
        ), f"read={len(read_all)} found={len(found)}"
    except JsonlDurabilityError as exc:
        return False, f"a valid bound was refused: {exc}"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def control_hardlinked_journal_is_refused_without_escape() -> tuple[bool, str]:
    """A journal hardlink must not turn an append into a write outside state/."""
    root = Path(tempfile.mkdtemp(prefix="jsonl-hardlink-"))
    try:
        state = root / "state"
        state.mkdir()
        outside = root / "outside-target.jsonl"
        original = b'{"id":"outside-original"}\n'
        outside.write_bytes(original)
        journal = state / "events.jsonl"
        os.link(outside, journal)
        try:
            append_jsonl(journal, {"id": "must-not-escape"})
        except JsonlDurabilityError as exc:
            refused = True
            detail = str(exc)
        except BaseException as exc:  # noqa: BLE001 - raw refusal is a control failure
            refused = False
            detail = f"raw {type(exc).__name__}: {exc}"
        else:
            refused = False
            detail = "hardlinked journal append was accepted"
        unchanged = outside.read_bytes() == original and journal.read_bytes() == original
        return refused and unchanged, detail
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _link_directory(link: Path, target: Path) -> None:
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


def control_linked_ancestor_is_refused_without_escape() -> tuple[bool, str]:
    """The journal leaf check must not be the only protection against a linked ancestor."""
    root = Path(tempfile.mkdtemp(prefix="jsonl-ancestor-link-"))
    try:
        state = root / "state"
        state.mkdir()
        outside = root / "outside"
        outside.mkdir()
        linked = state / "linked"
        _link_directory(linked, outside)
        try:
            append_jsonl(linked / "events.jsonl", {"id": "must-not-escape"})
        except JsonlDurabilityError as exc:
            detail = str(exc)
            refused = True
        except BaseException as exc:  # noqa: BLE001 - raw refusal is a control failure
            detail = f"raw {type(exc).__name__}: {exc}"
            refused = False
        else:
            detail = "linked ancestor append was accepted"
            refused = False
        return refused and not list(outside.iterdir()), detail
    finally:
        shutil.rmtree(root, ignore_errors=True)


def control_ancestor_swap_is_refused_at_leaf_window() -> tuple[bool, str]:
    """A deterministic ancestor swap in the check/open window must be refused."""
    root = Path(tempfile.mkdtemp(prefix="jsonl-ancestor-swap-"))
    try:
        state = root / "state"
        state.mkdir()
        linked = state / "linked"
        linked.mkdir()
        outside = root / "outside"
        outside.mkdir()
        old_linked = state / "linked-old"
        swapped = False

        def swap_ancestor(_path: Path) -> None:
            nonlocal swapped
            linked.rename(old_linked)
            _link_directory(linked, outside)
            swapped = True

        try:
            with mock.patch(
                    "jsonl_durable._before_journal_leaf_open",
                    side_effect=swap_ancestor) as seam, \
                    mock.patch("jsonl_durable._open_durable_leaf",
                               wraps=jsonl_durable._open_durable_leaf) as leaf_open:
                append_jsonl(linked / "events.jsonl", {"id": "must-not-write"})
        except JsonlDurabilityError as exc:
            detail = str(exc)
            refused = True
        except BaseException as exc:  # noqa: BLE001 - raw refusal is a control failure
            detail = f"raw {type(exc).__name__}: {exc}"
            refused = False
        else:
            detail = "ancestor swap was accepted"
            refused = False
        return (
            refused and swapped and seam.call_count == 1 and not leaf_open.called
            and not list(outside.iterdir())
        ), detail
    finally:
        shutil.rmtree(root, ignore_errors=True)


def control_quarantine_persists_before_truncate() -> tuple[bool, str]:
    root = Path(tempfile.mkdtemp(prefix="jsonl-order-"))
    try:
        path = root / "state/events.jsonl"
        path.parent.mkdir(parents=True)
        path.write_bytes(b'{"id":"committed"}\n{"id":"torn"')
        calls: list[str] = []
        real_truncate = os.ftruncate

        if os.name == "nt":
            real_fsync_dir = jsonl_durable._fsync_dir

            def record_fsync(directory: Path, _descriptor: int | None = None) -> None:
                calls.append("fsync:" + directory.relative_to(root).as_posix())
                real_fsync_dir(directory)
            fsync_patch = mock.patch("jsonl_durable._fsync_dir", side_effect=record_fsync)
        else:
            real_fsync = os.fsync

            def record_fsync(descriptor: int) -> None:
                target = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
                try:
                    label = target.relative_to(root).as_posix()
                except ValueError:
                    label = "outside:" + target.as_posix()
                calls.append("fsync:" + label)
                real_fsync(descriptor)
            fsync_patch = mock.patch("jsonl_durable.os.fsync", side_effect=record_fsync)

        def checked_truncate(descriptor: int, size: int) -> None:
            quarantine = root / "state/jsonl-quarantine"
            if len(list(quarantine.glob("*.bin"))) != 1 or len(list(quarantine.glob("*.json"))) != 1:
                raise AssertionError("journal truncation preceded durable quarantine artifacts")
            calls.append("truncate")
            real_truncate(descriptor, size)

        with fsync_patch, \
             mock.patch("jsonl_durable.os.ftruncate", side_effect=checked_truncate):
            append_jsonl(path, {"id": "new"})
        parent_sync = calls.index("fsync:state")
        metadata_sync = max(index for index, value in enumerate(calls)
                            if value == "fsync:state/jsonl-quarantine")
        truncate = calls.index("truncate")
        return parent_sync < metadata_sync < truncate, repr(calls)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def control_complete_write_loop() -> tuple[bool, str]:
    root = Path(tempfile.mkdtemp(prefix="jsonl-short-write-"))
    try:
        path = root / "state/events.jsonl"
        real_write = os.write

        def short_write(descriptor: int, value: bytes | memoryview) -> int:
            return real_write(descriptor, value[:3])

        with mock.patch("jsonl_durable.os.write", side_effect=short_write):
            append_jsonl(path, {"id": "short-write", "value": "preserved"})
        rows = read_jsonl_objects(path)
        return rows == [{"id": "short-write", "value": "preserved"}], path.read_text(encoding="utf-8")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def control_dedupe_still_repairs_tail() -> tuple[bool, str]:
    root = Path(tempfile.mkdtemp(prefix="jsonl-dedupe-tail-"))
    try:
        path = root / "state/events.jsonl"
        path.parent.mkdir(parents=True)
        committed = b'{"id":"same"}\n'
        torn = b'{"id":"interrupted"'
        path.write_bytes(committed + torn)
        outcome = append_jsonl(path, {"id": "same"}, dedupe_field="id", dedupe_value="same")
        payloads = list((root / "state/jsonl-quarantine").glob("*.bin"))
        return (
            not outcome.appended and path.read_bytes() == committed
            and len(payloads) == 1 and payloads[0].read_bytes() == torn
        ), repr(outcome)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def control_hardlinked_leaves_are_rejected() -> tuple[bool, str]:
    root = Path(tempfile.mkdtemp(prefix="jsonl-hardlink-"))
    try:
        path = root / "inside" / "events.jsonl"
        outside = root / "outside.jsonl"
        path.parent.mkdir(parents=True)
        original = b'{"id":"outside-sentinel"}\n'
        outside.write_bytes(original)
        try:
            path.hardlink_to(outside)
        except OSError as exc:
            return False, f"hardlink fixture could not be created: {exc}"
        try:
            append_jsonl(path, {"id": "must-not-escape"})
        except JsonlDurabilityError as exc:
            return outside.read_bytes() == original and path.read_bytes() == original, str(exc)
        return False, "a hardlinked journal leaf was accepted"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def control_refusal_text_has_no_path_or_os_details() -> tuple[bool, str]:
    """Public append refusals expose a typed reason, never caller paths or host errno text."""
    root = Path(tempfile.mkdtemp(prefix="jsonl-refusal-hygiene-"))
    try:
        blocker = root / "state"
        blocker.write_text("not a directory", encoding="utf-8")
        try:
            append_jsonl(blocker / "journal.jsonl", {"id": "must-not-append"})
        except JsonlDurabilityError as exc:
            message = str(exc)
            forbidden = (str(root), "[Errno", "[WinError", "Not a directory")
            return not any(item in message for item in forbidden), message
        return False, "a journal below a non-directory parent was accepted"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def control_hardlink_created_after_open_is_rejected() -> tuple[bool, str]:
    """A hardlink added after the open-time check must not receive the new record."""
    root = Path(tempfile.mkdtemp(prefix="jsonl-hardlink-race-"))
    try:
        path = root / "inside" / "events.jsonl"
        outside = root / "outside-alias.jsonl"
        path.parent.mkdir(parents=True)
        original = b'{"id":"prior"}\n'
        path.write_bytes(original)
        armed = True

        def hook(stage: str) -> None:
            nonlocal armed
            if armed and stage == "pre-journal-write":
                armed = False
                outside.hardlink_to(path)

        try:
            append_jsonl(path, {"id": "must-not-escape"}, hook=hook)
        except JsonlDurabilityError as exc:
            return (
                not armed and path.read_bytes() == original and outside.read_bytes() == original
                and b"must-not-escape" not in outside.read_bytes()
            ), str(exc)
        finally:
            outside.unlink(missing_ok=True)
        return False, "a hardlink created after validation was accepted"
    except OSError as exc:
        return False, f"hardlink race fixture could not be created: {exc}"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def control_hardlinked_index_is_rejected() -> tuple[bool, str]:
    root = Path(tempfile.mkdtemp(prefix="jsonl-index-hardlink-"))
    try:
        path = root / "state/events.jsonl"
        path.parent.mkdir(parents=True)
        path.write_bytes(b'{"id":"one"}\n')
        read_jsonl_objects(path)
        index = next((root / "state/jsonl-index").glob("*.sqlite3"))
        outside = root / "outside-index.sqlite3"
        outside.write_bytes(index.read_bytes())
        index.unlink()
        index.hardlink_to(outside)
        try:
            read_jsonl_objects(path)
        except JsonlDurabilityError as exc:
            return outside.read_bytes() == index.read_bytes(), str(exc)
        return False, "a hardlinked verification index was accepted"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def control_verified_index_avoids_prefix_rescan() -> tuple[bool, str]:
    root = Path(tempfile.mkdtemp(prefix="jsonl-index-tail-"))
    try:
        path = root / "state/events.jsonl"
        path.parent.mkdir(parents=True)
        rows = [{"id": f"event-{number:04d}"} for number in range(128)]
        path.write_text(
            "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                    for row in rows),
            encoding="utf-8",
        )
        first = read_jsonl_since(path, "fixture-consumer")
        acknowledge_jsonl(path, "fixture-consumer", first)
        if len(first.lines) != len(rows) or not first.rebuilt:
            return False, f"initial_lines={len(first.lines)} rebuilt={first.rebuilt}"
        with mock.patch("jsonl_durable._iter_complete_lines",
                        side_effect=AssertionError("verified prefix was rescanned")):
            steady = read_jsonl_since(path, "fixture-consumer")
            appended = append_jsonl(path, {"id": "event-tail"})
            tail = read_jsonl_since(path, "fixture-consumer")
        return (
            not steady.lines and appended.appended and len(tail.lines) == 1
            and tail.lines[0].value == {"id": "event-tail"}
        ), f"steady={len(steady.lines)} tail={len(tail.lines)}"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def control_index_rebuilds_after_change_or_cache_loss() -> tuple[bool, str]:
    root = Path(tempfile.mkdtemp(prefix="jsonl-index-rebuild-"))
    try:
        path = root / "state/events.jsonl"
        path.parent.mkdir(parents=True)
        original = b'{"id":"one"}\n{"id":"two"}\n'
        path.write_bytes(original)
        if [row["id"] for row in read_jsonl_objects(path)] != ["one", "two"]:
            return False, "initial index build returned the wrong rows"
        indexes = list((root / "state/jsonl-index").glob("*.sqlite3"))
        if len(indexes) != 1:
            return False, f"index_count={len(indexes)}"
        indexes[0].write_bytes(b"not-a-sqlite-database")
        if [row["id"] for row in read_jsonl_objects(path)] != ["one", "two"]:
            return False, "corrupt derived cache did not rebuild from the journal"
        indexes[0].unlink()
        if [row["id"] for row in read_jsonl_objects(path)] != ["one", "two"]:
            return False, "missing derived cache did not rebuild from the journal"
        changed = b'{"id":"one"}\n{"id":xxxxx}\n'
        if len(changed) != len(original):
            return False, "fixture mutation is not size preserving"
        path.write_bytes(changed)
        try:
            read_jsonl_objects(path)
        except JsonlDurabilityError as exc:
            return "malformed JSONL value" in str(exc), str(exc)
        return False, "same-size journal mutation reused stale verification state"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def control_interrupted_index_advance_replays_exactly() -> tuple[bool, str]:
    root = Path(tempfile.mkdtemp(prefix="jsonl-index-interrupt-"))
    try:
        path = root / "state/events.jsonl"
        with mock.patch(
            "jsonl_durable._update_index_after_append",
            side_effect=JsonlDurabilityError("simulated index commit interruption"),
        ):
            try:
                append_jsonl(path, {"id": "event-once"}, dedupe_field="id",
                             dedupe_value="event-once")
            except JsonlDurabilityError:
                pass
            else:
                return False, "interrupted derived-index update was reported as committed"
        outcome = append_jsonl(path, {"id": "event-once"}, dedupe_field="id",
                               dedupe_value="event-once")
        rows = read_jsonl_objects(path)
        return not outcome.appended and rows == [{"id": "event-once"}], repr(rows)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def control_pinned_posix_append_and_finally_preserve_original() -> tuple[bool, str]:
    """Exercise the descriptor-pinned append path, including its error cleanup."""
    if os.name == "nt":
        return True, "POSIX descriptor-pinned append is platform-scoped"
    if not getattr(os, "supports_dir_fd", None) or not Path("/proc/self/fd").is_dir():
        return False, "POSIX descriptor-pinned append prerequisites are unavailable"

    root = Path(tempfile.mkdtemp(prefix="jsonl-pinned-posix-"))
    descriptors: list[int] = []
    try:
        cache = root / "cache"
        cache.mkdir()
        root_descriptor = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        cache_descriptor = os.open(cache, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        descriptors.extend((root_descriptor, cache_descriptor))
        identity = "phase3/pinned/events.jsonl"
        first = jsonl_durable.append_jsonl_at(
            root_descriptor, "events.jsonl", {"id": "pinned"},
            cache_descriptor, identity, dedupe_field="id", dedupe_value="pinned",
        )
        cache_name = jsonl_durable.index_cache_name(identity)
        jsonl_durable.validate_durable_leaf_at(cache_descriptor, cache_name)
        duplicate = jsonl_durable.append_jsonl_at(
            root_descriptor, "events.jsonl", {"id": "pinned"},
            cache_descriptor, identity, dedupe_field="id", dedupe_value="pinned",
        )
        if not first.appended or duplicate.appended \
                or (root / "events.jsonl").read_bytes() != b'{"id":"pinned"}\n':
            return False, f"first={first!r} duplicate={duplicate!r}"

        failure_root = root / "failure"
        failure_cache = failure_root / "cache"
        failure_cache.mkdir(parents=True)
        failure_root_descriptor = os.open(
            failure_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        failure_cache_descriptor = os.open(
            failure_cache, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        descriptors.extend((failure_root_descriptor, failure_cache_descriptor))
        original = RuntimeError("original pinned append body failure")
        with mock.patch.object(jsonl_durable, "_update_index_after_append",
                               side_effect=original):
            try:
                jsonl_durable.append_jsonl_at(
                    failure_root_descriptor, "events.jsonl", {"id": "failure"},
                    failure_cache_descriptor, "phase3/pinned/failure.jsonl",
                )
            except RuntimeError as exc:
                if exc is not original:
                    return False, f"finally replaced original exception with {exc!r}"
            except BaseException as exc:  # noqa: BLE001 - cleanup masking is the control.
                return False, f"finally replaced original exception with {type(exc).__name__}: {exc}"
            else:
                return False, "the injected append-body failure was swallowed"
        return True, "pinned append success/dedupe and original body exception survived finally"
    except (OSError, TypeError, ValueError, OverflowError, RecursionError,
            UnicodeDecodeError, UnicodeEncodeError, sqlite3.DatabaseError) as exc:
        return False, f"pinned append leaked {type(exc).__name__}: {exc}"
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        shutil.rmtree(root, ignore_errors=True)


def control_rebuild_in_place_first_build_and_retention() -> tuple[bool, str]:
    """Prove first-build, valid-retention, and corrupt-cache rebuild are distinct."""
    root = Path(tempfile.mkdtemp(prefix="jsonl-rebuild-in-place-"))
    try:
        path = root / "events.jsonl"
        first = append_jsonl(path, {"id": "first"}, rebuild_in_place=True)
        indexes = list((root / "jsonl-index").glob("*.sqlite3"))
        if not first.appended or len(indexes) != 1:
            return False, f"first={first!r} index_count={len(indexes)}"
        index = indexes[0]
        retained_bytes = index.read_bytes()
        with mock.patch.object(
            jsonl_durable, "_rebuild_index_in_place",
            side_effect=AssertionError("a valid retained cache was rebuilt"),
        ):
            duplicate = append_jsonl(
                path, {"id": "first"}, dedupe_field="id", dedupe_value="first",
                rebuild_in_place=True,
            )
        if duplicate.appended or index.read_bytes() != retained_bytes:
            return False, f"valid cache was not retained: {duplicate!r}"
        index.write_bytes(b"not-a-sqlite-cache")
        rebuilt = append_jsonl(path, {"id": "second"}, rebuild_in_place=True)
        return (
            rebuilt.appended and read_jsonl_objects(path) == [{"id": "first"}, {"id": "second"}]
        ), repr(rebuilt)
    except (OSError, TypeError, ValueError, OverflowError, RecursionError,
            UnicodeDecodeError, UnicodeEncodeError, sqlite3.DatabaseError,
            AssertionError) as exc:
        return False, f"rebuild-in-place control leaked {type(exc).__name__}: {exc}"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def control_project_appenders_fail_closed_on_quarantine() -> tuple[bool, str]:
    if os.name == "nt":
        sys.modules.setdefault("fcntl", type("FcntlStub", (), {"LOCK_EX": 2, "LOCK_NB": 4,
                                                                 "flock": staticmethod(lambda *_: None)}))
    for script_name in ("project-pilot.py", "project-reconcile.py"):
        root = Path(tempfile.mkdtemp(prefix="jsonl-project-appender-"))
        try:
            (root / "state/.locks").mkdir(parents=True)
            journal = root / "state/events.jsonl"
            committed = b'{"id":"prior"}\n'
            torn = b'{"id":"crashed"'
            journal.write_bytes(committed + torn)
            spec = importlib.util.spec_from_file_location(
                "jsonl_appender_" + script_name.replace("-", "_"), SOURCE / "scripts" / script_name
            )
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            refused = False
            try:
                module.append_event(root, "test-event", "test-project", "test-goal", event_id="new-id")
            except BaseException:  # SystemExit in project-pilot; ReconcileError in Phase 6.
                refused = True
            rows = read_jsonl_objects(journal)
            payloads = list((root / "state/jsonl-quarantine").glob("*.bin"))
            projected = list((root / "state/recovery/transitions").glob("*/projected.json"))
            if not refused or [row.get("id") for row in rows] != ["prior", "new-id"]:
                return False, f"{script_name} refused={refused} rows={rows!r}"
            if len(payloads) != 1 or payloads[0].read_bytes() != torn:
                return False, f"{script_name} did not preserve its torn tail"
            if projected:
                return False, f"{script_name} silently marked a quarantined event projected"
        finally:
            shutil.rmtree(root, ignore_errors=True)
    return True, "both project appenders retained bytes and stopped on open quarantine"


def control_runtime_rotation_rejects_malformed() -> tuple[bool, str]:
    # This control used to self-skip on nt as "runtime-state process-start witness is
    # Linux-only", and the harness reported that skip as a plain `ok`. The real blocker was
    # the missing `os.fchmod` (see `_install_nt_posix_shim`), which surfaced as an unrelated
    # lock error; the rotation refusal itself needs no Linux facility, so with the shim in
    # place the control genuinely runs here instead of silently claiming a pass.
    root = Path(tempfile.mkdtemp(prefix="jsonl-rotation-"))
    try:
        for relative in ("scripts", "state/.locks"):
            (root / relative).mkdir(parents=True, exist_ok=True)
        for name in (
            "runtime-state.py", "notification-outbox.py", "jsonl_durable.py",
            "transition_receipt.py", "pilot_instruction.py", "instruction_routing.py",
        ):
            shutil.copy2(SOURCE / "scripts" / name, root / "scripts" / name)
        (root / "state/PAUSE").write_text("test pause\n", encoding="utf-8")
        source = root / "state/events.jsonl"
        original = b'{"schema_version":1,"event":"noop"}\nnot-json\n'
        source.write_bytes(original)
        result = subprocess.run(
            [sys.executable, "scripts/runtime-state.py", "rotate-events", "--max-bytes", "1"],
            cwd=root, env={**os.environ, "RECURSION_ROOT": str(root)}, text=True,
            capture_output=True, check=False,
        )
        archives = list((root / "state/event-archive").glob("*")) if (root / "state/event-archive").exists() else []
        detail = (result.stdout + result.stderr)[-300:]
        return (
            result.returncode != 0 and source.read_bytes() == original and not archives
            and "source event journal is corrupt" in detail
            and "malformed JSONL value" in detail
        ), detail
    finally:
        shutil.rmtree(root, ignore_errors=True)


def load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def notification_root(parent: Path, *, configured: bool) -> Path:
    root = parent / ("configured" if configured else "routing-error")
    for relative in (
        "scripts", "state/.locks", "state/project-config", "state/pilots",
        "state/tasks", "state/assists",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    taskctl = root / "scripts/taskctl.sh"
    taskctl.write_text("""#!/usr/bin/env bash
set -eu
[ \"${1:-}\" = esc-record ] || exit 2
shift
root=\"${RECURSION_ROOT:?}\"
if [ -f \"$root/state/escalation-fail-once\" ]; then
  rm -f \"$root/state/escalation-fail-once\"
  exit 41
fi
key=\"${1:?}\"; title=\"${2:-}\"; workstream=\"${3:-}\"; pointer=\"${4:-}\"
index=\"$root/state/escalations.tsv\"
if [ ! -f \"$index\" ]; then
  printf '#id\\tkey\\tstatus\\tfirst_seen\\tlast_seen\\tcount\\tworkstream\\tpointer\\ttitle\\n' > \"$index\"
fi
awk -F '\\t' -v key=\"$key\" '$2==key && $3==\"open\" {found=1} END{exit !found}' \"$index\" && exit 0
printf 'esc-0001\\t%s\\topen\\t2026-08-10T00:00:00Z\\t2026-08-10T00:00:00Z\\t1\\t%s\\t%s\\t%s\\n' \\
  \"$key\" \"$workstream\" \"$pointer\" \"$title\" >> \"$index\"
""", encoding="utf-8")
    taskctl.chmod(0o700)
    source = {
        "schema_version": 1, "id": "source-assist", "ts": "2026-08-10T00:00:00Z",
        "event": "assist-open", "task": "task-one", "project": "example-ops",
        "goal": "goal-one", "pid": 1, "detail": "decision: needs Lucas",
    }
    (root / "state/tasks/task-one.md").write_text(
        "schema_version: 1\nid: task-one\nproject_id: example-ops\n"
        "pilot_id: pilot-example-ops\ngoal_id: goal-one\nstatus: blocked\n",
        encoding="utf-8",
    )
    (root / "state/assists/task-one.md").write_text(
        "schema_version: 1\ntask: task-one\nkind: decision\nstatus: open\n\n"
        "## QUESTION\nneeds Lucas\n## WHAT WOULD SATISFY THIS\nA bounded fixture answer.\n",
        encoding="utf-8",
    )
    commit_event(root, source)
    if configured:
        targets = [
            "project-topic:telegram:-100:11",
            "head-hermes:telegram:-100:12",
            "lucas-direct:telegram:123",
        ]
        (root / "state/project-config/example-ops.json").write_text(
            json.dumps({"id": "example-ops", "pilot_id": "pilot-example-ops", "status": "active",
                        "notification_targets": targets}) + "\n",
            encoding="utf-8",
        )
        (root / "state/pilots/pilot-example-ops.json").write_text(
            json.dumps({"id": "pilot-example-ops", "project_id": "example-ops", "status": "active",
                        "notification_targets": targets}) + "\n",
            encoding="utf-8",
        )
    return root


def control_runtime_rotation_requires_canonical_outbox() -> tuple[bool, str]:
    if os.name == "nt":
        sys.modules.setdefault(
            "fcntl",
            types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=lambda *_args: None),
        )
    parent = Path(tempfile.mkdtemp(prefix="rotation-outbox-boundary-"))
    try:
        notification = load_script(
            "rotation_notification_fixture", SOURCE / "scripts/notification-outbox.py"
        )
        runtime = load_script("rotation_runtime_fixture", SOURCE / "scripts/runtime-state.py")
        if os.name == "nt":
            original_run = notification.subprocess.run

            def windows_taskctl_stub(command, *args, **kwargs):
                if len(command) >= 4 and str(command[1]).endswith("taskctl.sh") \
                        and command[2] == "esc-record":
                    fixture_root = Path(kwargs["env"]["RECURSION_ROOT"])
                    marker = fixture_root / "state/escalation-fail-once"
                    if marker.is_file():
                        marker.unlink()
                        return subprocess.CompletedProcess(command, 41, "", "fixture esc-record failure")
                    key, title = command[3], command[4]
                    workstream = command[5] if len(command) > 5 else ""
                    pointer = command[6] if len(command) > 6 else ""
                    index = fixture_root / "state/escalations.tsv"
                    header = "#id\tkey\tstatus\tfirst_seen\tlast_seen\tcount\tworkstream\tpointer\ttitle\n"
                    if not index.is_file():
                        index.write_text(header, encoding="utf-8")
                    rows = index.read_text(encoding="utf-8").splitlines()[1:]
                    if not any(row.split("\t")[1:3] == [key, "open"] for row in rows):
                        with index.open("a", encoding="utf-8", newline="\n") as output:
                            output.write(
                                f"esc-0001\t{key}\topen\t2026-08-10T00:00:00Z\t"
                                f"2026-08-10T00:00:00Z\t1\t{workstream}\t{pointer}\t{title}\n"
                            )
                    return subprocess.CompletedProcess(command, 0, "NEW esc-0001", "")
                return original_run(command, *args, **kwargs)

            notification.subprocess.run = windows_taskctl_stub
        root = notification_root(parent, configured=True)
        event = notification.all_mapped(root)[0]
        notification.create_event(root, event)
        runtime.assert_notification_projection_complete(root)

        projection_path = root / "state/notification-events" / f"{event['id']}.json"
        projection_bytes = projection_path.read_bytes()
        projection = json.loads(projection_bytes)
        projection["source_event_id"] = "source-spoofed"
        projection_path.write_text(json.dumps(projection) + "\n", encoding="utf-8")
        try:
            runtime.assert_notification_projection_complete(root)
        except RuntimeError as exc:
            if "content mismatch" not in str(exc):
                return False, f"spoof refusal={exc}"
        else:
            return False, "wrong-source projection authorized rotation"

        projection_path.write_bytes(b'{"id":')
        try:
            runtime.assert_notification_projection_complete(root)
        except RuntimeError as exc:
            if "cannot be validated" not in str(exc):
                return False, f"malformed refusal={exc}"
        else:
            return False, "malformed projection authorized rotation"
        projection_path.write_bytes(projection_bytes)

        delivery_id = notification.stable_id(
            "delivery", event["id"], "lucas-direct:telegram:123"
        )
        delivery_path = root / "state/deliveries" / f"{delivery_id}.json"
        delivery_bytes = delivery_path.read_bytes()
        delivery_path.unlink()
        try:
            runtime.assert_notification_projection_complete(root)
        except RuntimeError as exc:
            if "delivery set mismatch" not in str(exc):
                return False, f"missing-delivery refusal={exc}"
        else:
            return False, "projection without its durable delivery authorized rotation"
        delivery_path.write_bytes(delivery_bytes)

        delivery = json.loads(delivery_bytes)
        delivery["message_digest"] = "0" * 64
        delivery_path.write_text(json.dumps(delivery) + "\n", encoding="utf-8")
        try:
            runtime.assert_notification_projection_complete(root)
        except RuntimeError as exc:
            if "delivery identity/content mismatch" not in str(exc):
                return False, f"delivery-content refusal={exc}"
        else:
            return False, "delivery with the wrong message identity authorized rotation"
        delivery_path.write_bytes(delivery_bytes)
        runtime.assert_notification_projection_complete(root)

        routing_root = notification_root(parent, configured=False)
        routing_event = notification.all_mapped(routing_root)[0]
        (routing_root / "state/escalation-fail-once").write_text("fail once\n", encoding="utf-8")
        try:
            notification.create_event(routing_root, routing_event)
        except notification.OutboxError as exc:
            if "durable escalation failed" not in str(exc):
                return False, f"routing escalation refusal={exc}"
        else:
            return False, "routing-error projection ignored failed durable escalation"
        try:
            runtime.assert_notification_projection_complete(routing_root)
        except RuntimeError as exc:
            if "escalation" not in str(exc):
                return False, f"pre-escalation rotation refusal={exc}"
        else:
            return False, "routing-error projection without escalation authorized rotation"
        routed = notification.create_event(routing_root, routing_event)
        if not routed.get("routing_error") or list((routing_root / "state/deliveries").glob("*.json")):
            return False, "fixture did not create the canonical routing-error/no-delivery class"
        escalation_rows = [line for line in (routing_root / "state/escalations.tsv").read_text(
            encoding="utf-8").splitlines() if not line.startswith("#")]
        if len(escalation_rows) != 1 or f"notification-routing:{routing_event['id']}\topen\t" not in escalation_rows[0]:
            return False, "routing-error retry did not create exactly one OPEN escalation"
        runtime.assert_notification_projection_complete(routing_root)
        escalation_path = routing_root / "state/escalations.tsv"
        escalation_bytes = escalation_path.read_bytes()

        refused_classes = {
            "closed": escalation_bytes.replace(b"\topen\t", b"\tresolved\t", 1),
            "duplicate": escalation_bytes + escalation_rows[0].replace(
                "esc-0001\t", "esc-0002\t", 1
            ).encode() + b"\n",
            "malformed": escalation_bytes + b"esc-bad\ttoo-few-fields\n",
        }
        for label, value in refused_classes.items():
            escalation_path.write_bytes(value)
            try:
                runtime.assert_notification_projection_complete(routing_root)
            except RuntimeError as exc:
                if "escalation" not in str(exc):
                    return False, f"{label} escalation refusal={exc}"
            else:
                return False, f"{label} routing escalation authorized rotation"
        escalation_path.write_bytes(escalation_bytes)
        runtime.assert_notification_projection_complete(routing_root)
        return True, (
            "canonical delivery and exact OPEN routing-escalation classes accepted; "
            "missing/closed/duplicate/malformed classes refused"
        )
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def main() -> int:
    controls = (
        ("a torn prior tail is quarantined byte-exactly before a complete append", control_torn_tail),
        ("a complete malformed prior record fails closed without mutation", control_complete_malformed_fails_closed),
        ("all public JSONL entry points reject bool record-size bounds before delegation",
         control_record_limits_are_checked_at_public_boundaries),
        ("valid record-size bounds still pass through every guarded entry point",
         control_record_limit_positive_paths_still_work),
        ("a hardlinked journal is refused without writing outside the state tree",
         control_hardlinked_journal_is_refused_without_escape),
        ("a linked journal ancestor is refused without writing outside the state tree",
         control_linked_ancestor_is_refused_without_escape),
        ("a deterministic ancestor swap at leaf open is refused",
         control_ancestor_swap_is_refused_at_leaf_window),
        ("quarantine directory and evidence persist before journal truncation", control_quarantine_persists_before_truncate),
        ("short writes are retried until the complete record is durable", control_complete_write_loop),
        ("idempotent appends still repair and preserve a torn tail", control_dedupe_still_repairs_tail),
        ("hardlinked journal leaves are rejected without an outside-root write", control_hardlinked_leaves_are_rejected),
        ("append refusals do not disclose paths or raw OS errors",
         control_refusal_text_has_no_path_or_os_details),
        ("a post-validation hardlink race is refused before journal bytes escape",
         control_hardlink_created_after_open_is_rejected),
        ("hardlinked verification-index leaves are rejected", control_hardlinked_index_is_rejected),
        ("an exact verification index makes steady-state append and reads tail-only",
         control_verified_index_avoids_prefix_rescan),
        ("cache loss rebuilds and journal mutation cannot reuse stale verification",
         control_index_rebuilds_after_change_or_cache_loss),
        ("a crash between journal fsync and index advance replays exactly once",
         control_interrupted_index_advance_replays_exactly),
        ("the POSIX descriptor-pinned append succeeds and preserves its original body exception",
         control_pinned_posix_append_and_finally_preserve_original),
        ("rebuild_in_place distinguishes first build, retained cache, and corrupt-cache rebuild",
         control_rebuild_in_place_first_build_and_retention),
        ("both project event appenders stop on an open repaired-tail mismatch",
         control_project_appenders_fail_closed_on_quarantine),
        ("runtime rotation refuses malformed source events instead of archiving them", control_runtime_rotation_rejects_malformed),
        ("runtime rotation requires canonical notification content and durable delivery identity",
         control_runtime_rotation_requires_canonical_outbox),
    )
    failures = 0
    for description, control in controls:
        try:
            passed, detail = control()
        except Exception as exc:  # noqa: BLE001 - every control must report a mismatch.
            passed, detail = False, repr(exc)
        if passed:
            print(f"ok    {description}")
        else:
            failures += 1
            print(f"FAIL  {description}: {detail}")
    print()
    if failures:
        print(f"jsonl-durability: {failures} FAILED")
        return 1
    print("jsonl-durability: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
