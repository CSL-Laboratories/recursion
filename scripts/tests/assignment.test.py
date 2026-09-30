#!/usr/bin/env python3
"""Hermetic controls for scripts/assignment.py (Phase 3, D-016).

Covers: digest stability/sensitivity, content-addressed idempotent persistence, fail-closed
capacity-pool manifest validation, and the resolve/show CLI end to end. Everything runs under
a disposable tempdir; nothing touches the live repo's state/.
"""

from __future__ import annotations

import json
import hashlib
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import tempfile
from unittest import mock
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2]
SCRIPTS = SOURCE / "scripts"
ASSIGNMENT = SCRIPTS / "assignment.py"

sys.path.insert(0, str(SCRIPTS))
import assignment as amod  # noqa: E402

failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"ok - {label}")
    else:
        print(f"FAIL - {label}")
        failures.append(label)


BASE_KWARGS = dict(
    task_id="task-001", project_id="proj-001", role="implementer",
    source_repository="/opt/example-project", source_branch="loop/work/task-001",
    source_sha="a" * 40, engine="claude", provider="anthropic-claude-code",
    host="gs-recursion", sandbox_profile="strict-linux", network_policy="provider-only",
    network_profile="anthropic",
    allowed_paths=["apps/api"], publication_policy="verified-integration-only",
)


def test_network_policy_and_profile_are_separate() -> None:
    value = amod.resolve_assignment(**BASE_KWARGS, created_at="2026-08-28T00:00:00Z")
    check(value["network_policy"] == "provider-only",
          "assignment stores the network policy in network_policy")
    check(value["network_profile"] == "anthropic",
          "assignment stores the declared provider profile in network_profile")

    legacy = dict(BASE_KWARGS)
    legacy.pop("network_policy")
    legacy["network_profile"] = "provider-only"
    migrated = amod.resolve_assignment(**legacy, created_at="2026-08-28T00:00:00Z")
    check(migrated["network_policy"] == "provider-only" and migrated["network_profile"] == "anthropic",
          "legacy policy-shaped network_profile input is normalized to both fields")

    for field, bad in (("network_policy", "anthropic"), ("network_profile", "provider-only")):
        candidate = dict(BASE_KWARGS)
        candidate[field] = bad
        try:
            amod.resolve_assignment(**candidate)
            check(False, f"network {field} vocabulary collision {bad!r} is refused")
        except amod.AssignmentError:
            check(True, f"network {field} vocabulary collision {bad!r} is refused")

    missing_policy = dict(BASE_KWARGS)
    missing_policy.pop("network_policy")
    try:
        amod.resolve_assignment(**missing_policy)
        check(False, "a provider profile without its separate network policy is refused")
    except amod.AssignmentError:
        check(True, "a provider profile without its separate network policy is refused")

    unknown_legacy = dict(legacy, engine="hermes", provider="unregistered-provider")
    try:
        amod.resolve_assignment(**unknown_legacy)
        check(False, "a legacy policy without a declared provider profile is refused")
    except amod.AssignmentError:
        check(True, "a legacy policy without a declared provider profile is refused")


def test_digest_stable_and_sensitive() -> None:
    first = amod.resolve_assignment(**BASE_KWARGS, created_at="2026-08-28T00:00:00Z")
    second = amod.resolve_assignment(**BASE_KWARGS, created_at="2026-08-28T00:00:00Z")
    check(first["assignment_digest"] == second["assignment_digest"],
          "identical inputs (incl. created_at) produce the identical digest")
    check(amod.canonical_digest(first) == first["assignment_digest"],
          "canonical_digest recomputation matches the stored digest")

    changed_kwargs = dict(BASE_KWARGS)
    changed_kwargs["engine"] = "hermes"
    third = amod.resolve_assignment(**changed_kwargs, created_at="2026-08-28T00:00:00Z")
    check(third["assignment_digest"] != first["assignment_digest"],
          "changing engine changes the digest")

    changed_time = amod.resolve_assignment(**BASE_KWARGS, created_at="2026-08-28T00:00:01Z")
    check(changed_time["assignment_digest"] != first["assignment_digest"],
          "changing created_at changes the digest (each resolution is its own record)")


def test_allowed_paths_normalized() -> None:
    value = amod.resolve_assignment(
        **{**BASE_KWARGS, "allowed_paths": ["b", "a", "a", "b"]},
        created_at="2026-08-28T00:00:00Z",
    )
    check(value["allowed_paths"] == ["a", "b"], "allowed_paths is sorted and deduped")


def test_allowed_paths_fail_closed() -> None:
    for path in ("../escape", "a/../../escape", "/absolute", r"C:\absolute"):
        try:
            amod.resolve_assignment(
                **{**BASE_KWARGS, "allowed_paths": [path]},
                created_at="2026-08-28T00:00:00Z",
            )
            check(False, f"authority-bearing path {path!r} is refused")
        except amod.AssignmentError:
            check(True, f"authority-bearing path {path!r} is refused")

    for path in (r"apps\api", "apps/./api", "apps//api", "./apps/api"):
        try:
            amod.resolve_assignment(**{**BASE_KWARGS, "allowed_paths": [path]}, created_at="2026-08-28T00:00:00Z")
            check(False, f"noncanonical path alias {path!r} is refused")
        except amod.AssignmentError:
            check(True, f"noncanonical path alias {path!r} is refused")


def test_fail_closed_invalid_inputs() -> None:
    for field, bad in (
        ("task_id", "bad id with spaces"),
        ("role", "implementor"),
        ("source_sha", "not-a-sha"),
        ("engine", ""),
        ("provider", "Anthropic Claude Code"),  # spaces/caps not allowed
        ("host", ""),
        ("sandbox_profile", "made-up-profile"),
        ("network_policy", "anthropic"),
        ("network_profile", "open"),
        ("publication_policy", "!!!"),
    ):
        kwargs = dict(BASE_KWARGS)
        kwargs[field] = bad
        try:
            amod.resolve_assignment(**kwargs)
            check(False, f"invalid {field}={bad!r} is refused")
        except amod.AssignmentError:
            check(True, f"invalid {field}={bad!r} is refused")


def test_empty_allowed_paths_refused() -> None:
    kwargs = dict(BASE_KWARGS)
    kwargs["allowed_paths"] = []
    try:
        amod.resolve_assignment(**kwargs)
        check(False, "empty allowed_paths is refused")
    except amod.AssignmentError:
        check(True, "empty allowed_paths is refused")


def test_validate_rejects_malformed_field_types() -> None:
    value = amod.resolve_assignment(**BASE_KWARGS, created_at="2026-08-28T00:00:00Z")
    malformed = (
        ("schema_version", True), ("task_id", 123), ("project_id", []),
        ("role", {}), ("source_repository", []), ("source_branch", []),
        ("source_sha", 123), ("source_evidence", []), ("seed_manifest_digest", []),
        ("engine", []), ("provider", []), ("capacity_pool", []),
        ("billing_basis", []), ("metered_approved", []), ("model", 123),
        ("model_parameters", []), ("host", []), ("sandbox_profile", []),
        ("network_policy", []), ("network_profile", []), ("allowed_paths", {}), ("gate_digest", []),
        ("publication_policy", []), ("default_branch", []),
        ("integration_branch", []), ("created_at", []),
    )
    for field, bad in malformed:
        candidate = dict(value)
        candidate[field] = bad
        candidate["assignment_digest"] = amod.canonical_digest(candidate)
        try:
            amod.validate_assignment(candidate, require_source_evidence=True)
        except amod.AssignmentError:
            check(True, f"malformed assignment field type {field} is a typed refusal")
        except Exception as exc:  # noqa: BLE001 - raw exceptions are the regression target.
            check(False, f"malformed assignment field type {field} raised raw {type(exc).__name__}")
        else:
            check(False, f"malformed assignment field type {field} was accepted")

    candidate = dict(value)
    candidate["assignment_digest"] = 123
    try:
        amod.validate_assignment(candidate, require_source_evidence=True)
    except amod.AssignmentError:
        check(True, "non-string assignment_digest is a typed refusal")
    except Exception as exc:  # noqa: BLE001
        check(False, f"non-string assignment_digest raised raw {type(exc).__name__}")
    else:
        check(False, "non-string assignment_digest was accepted")


def test_persist_idempotent_and_collision_safe(tmp_path: Path) -> None:
    root = tmp_path / "assignments"
    value = amod.resolve_assignment(**BASE_KWARGS, created_at="2026-08-28T00:00:00Z")
    first_path = amod.persist_assignment(root, value)
    check(first_path.is_file(), "persist_assignment writes the assignment file")
    second_path = amod.persist_assignment(root, dict(value))
    check(second_path == first_path, "persisting identical content twice is a no-op (same path)")
    original = first_path.read_text(encoding="utf-8")

    # Simulate a content-address conflict directly (a real sha256 collision is not producible
    # in a test): overwrite the same digest-addressed path with different bytes, then confirm
    # persist_assignment refuses to reconcile/overwrite it.
    collide_path = amod.assignment_path(root, value["task_id"], value["assignment_digest"])
    collide_path.write_text(json.dumps({**value, "engine": "hermes"}, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")
    try:
        amod.persist_assignment(root, value)
        check(False, "a content-address collision (same digest path, different bytes) is refused")
    except amod.AssignmentError:
        check(True, "a content-address collision (same digest path, different bytes) is refused")
    finally:
        collide_path.write_text(original, encoding="utf-8")


def _make_directory_link(link: Path, target: Path) -> None:
    """Create a directory link on both supported test platforms; never silently skip it."""
    if os.name == "nt":
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise AssertionError(f"could not create Windows junction: {result.stderr or result.stdout}")
    else:
        link.symlink_to(target, target_is_directory=True)


def _make_leaf_link(link: Path, target: Path) -> None:
    """Create a file symlink on POSIX or a directory junction at the leaf on Windows."""
    if os.name == "nt":
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise AssertionError(f"could not create Windows leaf junction: {result.stderr or result.stdout}")
        return
    try:
        link.symlink_to(target)
    except OSError as exc:
        raise AssertionError(f"could not create file symlink for confinement test: {exc}") from exc


def test_persist_rejects_linked_paths_before_writes(tmp_path: Path) -> None:
    value = amod.resolve_assignment(**BASE_KWARGS, created_at="2026-08-28T00:00:00Z")

    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    _make_directory_link(linked_parent, outside)
    try:
        amod.persist_assignment(linked_parent / "assignments", value)
        check(False, "an assignment root below a linked ancestor is refused")
    except amod.AssignmentError:
        check(True, "an assignment root below a linked ancestor is refused")
    check(not (outside / "assignments").exists(),
          "a linked ancestor cannot redirect assignment writes outside the intended root")

    task_root = tmp_path / "task-link-root"
    task_root.mkdir()
    outside_task = outside / "task-link-target"
    outside_task.mkdir()
    _make_directory_link(task_root / value["task_id"], outside_task)
    try:
        amod.persist_assignment(task_root, value)
        check(False, "a linked assignment task directory is refused")
    except amod.AssignmentError:
        check(True, "a linked assignment task directory is refused")
    check(not (outside_task / f"{value['assignment_digest']}.json").exists(),
          "a linked task directory receives no assignment bytes")

    record_root = tmp_path / "record-link-root"
    record_task = record_root / value["task_id"]
    record_task.mkdir(parents=True)
    outside_record = outside / "record-target"
    outside_record.mkdir()
    record_sentinel = outside_record / "sentinel"
    record_sentinel.write_text("sentinel\n", encoding="utf-8")
    _make_leaf_link(record_task / f"{value['assignment_digest']}.json", outside_record)
    try:
        amod.persist_assignment(record_root, value)
        check(False, "a linked assignment record leaf is refused")
    except amod.AssignmentError:
        check(True, "a linked assignment record leaf is refused")
    check(record_sentinel.read_text(encoding="utf-8") == "sentinel\n",
          "a linked assignment record cannot overwrite its target")

    index_root = tmp_path / "index-link-root"
    (index_root / value["task_id"]).mkdir(parents=True)
    outside_index = outside / "index-target"
    outside_index.mkdir()
    index_sentinel = outside_index / "sentinel"
    index_sentinel.write_text("sentinel\n", encoding="utf-8")
    _make_leaf_link(index_root / "index.jsonl", outside_index)
    try:
        amod.persist_assignment(index_root, value)
        check(False, "a linked assignment index leaf is refused")
    except amod.AssignmentError:
        check(True, "a linked assignment index leaf is refused")
    check(not (index_root / value["task_id"] / f"{value['assignment_digest']}.json").exists(),
          "an unsafe linked index is rejected before the assignment file is written")
    check(index_sentinel.read_text(encoding="utf-8") == "sentinel\n",
          "a linked assignment index cannot be overwritten")

    cache_root = tmp_path / "cache-link-root"
    (cache_root / value["task_id"]).mkdir(parents=True)
    cache_dir = cache_root / ".jsonl-index"
    cache_dir.mkdir()
    outside_cache = outside / "cache-target"
    outside_cache.mkdir()
    cache_leaf = hashlib.sha256(
        f"assignment-root/{value['task_id']}/index.jsonl".encode("utf-8")
    ).hexdigest() + ".sqlite3"
    _make_leaf_link(cache_dir / cache_leaf, outside_cache)
    try:
        amod.persist_assignment(cache_root, value)
        check(False, "a linked verification-cache leaf is refused")
    except amod.AssignmentError:
        check(True, "a linked verification-cache leaf is refused")
    check(not (cache_root / value["task_id"] / f"{value['assignment_digest']}.json").exists(),
          "an unsafe linked cache is rejected before the assignment file is written")


def _swap_root_to_outside(root: Path, outside: Path) -> None:
    saved = root.with_name(root.name + ".pinned")
    root.rename(saved)
    root.symlink_to(outside, target_is_directory=True)


def test_persist_rejects_deterministic_swaps(tmp_path: Path) -> None:
    """Arm each decisive TOCTOU point and prove no byte reaches the outside tree."""
    stages = (
        ("pre-mkstemp", "task", "task-directory swap before assignment temp create"),
        ("pre-index-open", "root", "root swap before index open"),
        ("pre-sqlite-open", "root", "root swap before SQLite open"),
        ("pre-replace", "root", "root swap before verification-cache replace"),
    )
    for stage, target, label in stages:
        root = tmp_path / f"root-{stage}"
        outside = tmp_path / f"outside-{stage}"
        outside.mkdir()
        value = amod.resolve_assignment(
            **{**BASE_KWARGS, "task_id": f"task-{stage}"},
            created_at="2026-08-28T00:00:00Z",
        )
        task = root / value["task_id"]
        outside_task = outside / "task-target"
        outside_task.mkdir()
        outside_before = {item.relative_to(outside) for item in outside.rglob("*")}
        armed = True
        swap_refused = False

        def hook(current: str, *, root=root, outside=outside, task=task,
                 outside_task=outside_task, target=target) -> None:
            nonlocal armed, swap_refused
            if not armed or current != stage:
                return
            armed = False
            try:
                if target == "root":
                    _swap_root_to_outside(root, outside)
                else:
                    task.rename(task.with_name(task.name + ".pinned"))
                    if os.name == "nt":
                        _make_directory_link(task, outside_task)
                    else:
                        task.symlink_to(outside_task, target_is_directory=True)
            except OSError:
                # On Windows the native parent handle is expected to deny the rename. The
                # attempt still exercises the protected point; a vulnerable path allows the
                # swap and the outside-tree assertion below fails.
                swap_refused = True

        previous = amod._assignment_persistence_hook
        amod._assignment_persistence_hook = hook
        try:
            try:
                amod.persist_assignment(root, value)
            except amod.AssignmentError:
                if os.name != "nt":
                    check(True, f"{label} is refused rather than accepted")
        finally:
            amod._assignment_persistence_hook = previous
            if target == "root":
                if root.is_symlink():
                    root.unlink()
                saved = root.with_name(root.name + ".pinned")
                if saved.exists():
                    saved.rename(root)
            else:
                if task.is_symlink():
                    task.unlink()
                saved = task.with_name(task.name + ".pinned")
                if saved.exists():
                    saved.rename(task)
        outside_after = {item.relative_to(outside) for item in outside.rglob("*")}
        check(outside_after == outside_before, f"{label} creates no outside file")
        if os.name == "nt":
            check(swap_refused, f"{label} is denied by the pinned Windows directory handle")
            check(not armed, f"{label} reaches the native protected swap point")


def test_windows_temp_collision_retries_typed(tmp_path: Path) -> None:
    if os.name != "nt":
        check(True, "Windows temp-name collision control is platform-scoped")
        return

    root = tmp_path / "windows-temp-collision"
    value = amod.resolve_assignment(**BASE_KWARGS, created_at="2026-08-28T00:00:00Z")
    original_open = amod._win_open_file
    collisions = 0

    def collide_once(path: Path, disposition: int) -> int:
        nonlocal collisions
        if disposition == amod._CREATE_NEW and collisions == 0:
            collisions += 1
            raise OSError(80, "simulated ERROR_FILE_EXISTS")
        return original_open(path, disposition)

    with mock.patch.object(amod, "_win_open_file", side_effect=collide_once):
        try:
            persisted = amod.persist_assignment(root, value)
            check(persisted.is_file(), "a Windows temp-name collision retries to typed success")
        except Exception as exc:  # noqa: BLE001 - raw OSError is the regression target.
            check(False, f"a Windows temp-name collision is not a raw {type(exc).__name__}")
    check(collisions == 1, "the deterministic Windows temp-name collision was exercised")


def test_windows_publication_failure_cleans_private_temp(tmp_path: Path) -> None:
    if os.name != "nt":
        check(True, "Windows publication cleanup control is platform-scoped")
        return

    root = tmp_path / "windows-publication-cleanup"
    outside = tmp_path / "outside-publication-target"
    outside.mkdir()
    value = amod.resolve_assignment(
        **BASE_KWARGS, created_at="2026-08-28T00:00:00Z",
    )
    final = root / value["task_id"] / f"{value['assignment_digest']}.json"
    armed = True

    def hook(stage: str) -> None:
        nonlocal armed
        if armed and stage == "pre-mkstemp":
            armed = False
            _make_leaf_link(final, outside)

    previous = amod._assignment_persistence_hook
    amod._assignment_persistence_hook = hook
    try:
        try:
            amod.persist_assignment(root, value)
            check(False, "a raced Windows reparse leaf refuses assignment publication")
        except amod.AssignmentError:
            check(True, "a raced Windows reparse leaf refuses assignment publication")
    finally:
        amod._assignment_persistence_hook = previous

    leftovers = list(final.parent.glob(f".{value['assignment_digest']}.json.*.tmp"))
    check(not leftovers, "failed Windows hard-link publication removes its private temporary")
    check(not any(outside.iterdir()), "failed Windows publication creates no outside file")
    if final.exists():
        os.rmdir(final)


def test_windows_durable_lock_atomically_rejects_reparse_leaf(tmp_path: Path) -> None:
    if os.name != "nt":
        check(True, "Windows atomic durable-lock control is platform-scoped")
        return

    journal = tmp_path / "lock-race" / "index.jsonl"
    journal.parent.mkdir(parents=True)
    lock_path = journal.parent / f".{journal.name}.lock"
    outside = tmp_path / "outside-lock-target"
    outside.mkdir()
    durable = amod.jsonl_durable
    original = durable.ensure_no_symlink
    armed = True

    def race(path: Path) -> None:
        nonlocal armed
        original(path)
        if armed and Path(path) == lock_path:
            armed = False
            _make_leaf_link(lock_path, outside)

    durable.ensure_no_symlink = race
    try:
        try:
            with durable.durable_lock(journal):
                pass
            check(False, "a raced Windows durable-lock reparse leaf is refused atomically")
        except durable.JsonlDurabilityError:
            check(True, "a raced Windows durable-lock reparse leaf is refused atomically")
    finally:
        durable.ensure_no_symlink = original

    check(not any(outside.iterdir()), "a raced Windows durable-lock leaf creates no outside file")
    if lock_path.exists():
        os.rmdir(lock_path)


def test_windows_durable_lock_pins_parent_through_leaf_open(tmp_path: Path) -> None:
    if os.name != "nt":
        check(True, "Windows durable-lock parent pinning control is platform-scoped")
        return

    journal_parent = tmp_path / "lock-parent-race"
    journal_parent.mkdir()
    journal = journal_parent / "index.jsonl"
    lock_path = journal_parent / f".{journal.name}.lock"
    saved_parent = journal_parent.with_name(journal_parent.name + ".renamed")
    outside = tmp_path / "outside-parent-lock-target"
    outside.mkdir()
    outside_lock = outside / lock_path.name
    durable = amod.jsonl_durable
    original = durable.ensure_no_symlink
    armed = True

    def race(path: Path) -> None:
        nonlocal armed
        original(path)
        if armed and Path(path) == lock_path:
            armed = False
            # The final precheck has completed.  A vulnerable implementation permits
            # this rename+junction replacement before CreateFileW opens the lock leaf.
            journal_parent.rename(saved_parent)
            _make_directory_link(journal_parent, outside)

    durable.ensure_no_symlink = race
    refused_typed = False
    try:
        try:
            with durable.durable_lock(journal):
                pass
        except durable.JsonlDurabilityError:
            refused_typed = True
        except Exception as exc:
            check(False, f"a raced Windows lock-parent swap is typed ({type(exc).__name__})")
    finally:
        durable.ensure_no_symlink = original
        # Restore the local fixture if this test is run against a vulnerable build.
        if saved_parent.exists():
            if journal_parent.exists():
                os.rmdir(journal_parent)
            saved_parent.rename(journal_parent)

    check(refused_typed, "a rename+junction swap after the final lock precheck is refused typed")
    check(not armed, "the Windows lock-parent race runs after the final pathname precheck")
    check(not outside_lock.exists(), "a raced Windows lock parent creates no outside lock")
    check(not lock_path.exists(), "a refused Windows lock-parent race leaks no local lock file")
    check(not saved_parent.exists(), "a refused Windows lock-parent race leaks no renamed tree")


def _persist_worker(args: tuple[str, dict]) -> str:
    root_name, value = args
    sys.path.insert(0, str(SCRIPTS))
    import assignment as worker_assignment
    try:
        worker_assignment.persist_assignment(Path(root_name), value)
        return "ok"
    except Exception as exc:  # the parent reports any unexpected worker refusal
        return f"error:{type(exc).__name__}:{exc}"


def test_persist_40_process_idempotency(tmp_path: Path) -> None:
    root = tmp_path / "stress-root"
    value = amod.resolve_assignment(**BASE_KWARGS, created_at="2026-08-28T00:00:00Z")
    context = multiprocessing.get_context("spawn")
    old_attempts = os.environ.get("RECURSION_DURABLE_LOCK_ATTEMPTS")
    os.environ["RECURSION_DURABLE_LOCK_ATTEMPTS"] = "1000"
    pool = context.Pool(40)
    try:
        results = pool.map(_persist_worker, [(str(root), value)] * 40)
    finally:
        pool.close()
        pool.join()
        if old_attempts is None:
            os.environ.pop("RECURSION_DURABLE_LOCK_ATTEMPTS", None)
        else:
            os.environ["RECURSION_DURABLE_LOCK_ATTEMPTS"] = old_attempts
    check(all(result == "ok" for result in results), "40 concurrent persistence workers succeed idempotently")
    record = root / value["task_id"] / f"{value['assignment_digest']}.json"
    check(record.is_file() and record.stat().st_nlink == 1,
          "40-process persistence leaves one immutable single-link assignment record")
    index = root / "index.jsonl"
    rows = [json.loads(line) for line in index.read_text(encoding="utf-8").splitlines() if line]
    check(len(rows) == 1 and rows[0]["assignment_digest"] == value["assignment_digest"],
          "40-process persistence appends exactly one index row")
    cache_files = list((root / ".jsonl-index").glob("*.sqlite3"))
    cache_ok = False
    if len(cache_files) == 1:
        connection = sqlite3.connect(cache_files[0])
        try:
            cache_ok = connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            connection.close()
    check(cache_ok, "40-process persistence leaves one intact verification SQLite cache inside the root")
    check(not list(root.parent.glob("jsonl-index")),
          "40-process persistence creates no verification cache outside the pinned root")

    divergent_root = tmp_path / "divergent-stress-root"
    divergent_values = [
        amod.resolve_assignment(
            **{**BASE_KWARGS, "task_id": f"task-{number:03d}"},
            created_at=f"2026-08-28T00:00:{number:02d}Z",
        )
        for number in range(40)
    ]
    divergent_pool = context.Pool(40)
    try:
        divergent_results = divergent_pool.map(
            _persist_worker, [(str(divergent_root), value) for value in divergent_values]
        )
    finally:
        divergent_pool.close()
        divergent_pool.join()
    check(all(result == "ok" for result in divergent_results)
          and not any("OSError" in result for result in divergent_results),
          "40 concurrent divergent writers return typed success without raw OSError")
    divergent_index = divergent_root / "index.jsonl"
    divergent_rows = [line for line in divergent_index.read_text(encoding="utf-8").splitlines() if line]
    check(len(divergent_rows) == 40,
          "40 concurrent divergent writers append all distinct index records")


def test_capacity_pool_validation(tmp_path: Path) -> None:
    pool_dir = tmp_path / "pools"
    pool_dir.mkdir()
    good = {
        "schema_version": 1, "id": "cursor-ultra", "kind": "subscription",
        "billing_basis": "metered", "provider": "cursor",
        "quota": {"period": "weekly", "source": "declared", "limit": 500, "unit": "requests"},
        "on_demand": False,
    }
    (pool_dir / "cursor-ultra.json").write_text(json.dumps(good), encoding="utf-8")
    loaded = amod.load_capacity_pool(pool_dir, "cursor-ultra")
    check(loaded["id"] == "cursor-ultra", "well-formed capacity-pool manifest loads")

    kwargs = dict(BASE_KWARGS)
    kwargs["capacity_pool"] = "cursor-ultra"
    kwargs["engine"] = "cursor"
    kwargs["provider"] = "cursor"
    kwargs["billing_basis"] = "metered"
    kwargs["metered_approved"] = True
    value = amod.resolve_assignment(**kwargs, capacity_pool_dir=pool_dir, created_at="2026-08-28T00:00:00Z")
    check(value["capacity_pool"] == "cursor-ultra", "assignment records a valid capacity_pool")

    kwargs["capacity_pool"] = "does-not-exist"
    try:
        amod.resolve_assignment(**kwargs, capacity_pool_dir=pool_dir)
        check(False, "referencing an unknown capacity_pool is refused")
    except amod.AssignmentError:
        check(True, "referencing an unknown capacity_pool is refused")

    bad = dict(good)
    bad["kind"] = "made-up"
    (pool_dir / "broken.json").write_text(json.dumps(bad), encoding="utf-8")
    try:
        amod.load_capacity_pool(pool_dir, "broken")
        check(False, "capacity-pool manifest with an invalid kind is refused")
    except amod.AssignmentError:
        check(True, "capacity-pool manifest with an invalid kind is refused")

    mismatched_id = dict(good); mismatched_id["id"] = "something-else"
    (pool_dir / "cursor-ultra2.json").write_text(json.dumps(mismatched_id), encoding="utf-8")
    try:
        amod.load_capacity_pool(pool_dir, "cursor-ultra2")
        check(False, "capacity-pool manifest id/filename mismatch is refused")
    except amod.AssignmentError:
        check(True, "capacity-pool manifest id/filename mismatch is refused")


def test_load_rejects_reparse_task_directory(tmp_path: Path) -> None:
    root = tmp_path / "assignments"
    outside = tmp_path / "outside-task"
    root.mkdir()
    outside.mkdir()
    value = amod.resolve_assignment(**BASE_KWARGS, created_at="2026-08-28T00:00:00Z")
    (outside / f"{value['assignment_digest']}.json").write_text(
        json.dumps(value, sort_keys=True), encoding="utf-8",
    )
    _make_directory_link(root / value["task_id"], outside)
    loaded = amod.load_assignment(root, value["task_id"], value["assignment_digest"])
    check(loaded is None,
          "load_assignment refuses an assignments/<task> junction/reparse point")
    check((outside / f"{value['assignment_digest']}.json").is_file(),
          "a refused junction load does not mutate the outside target")


def run_cli(args: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ASSIGNMENT), *args],
                          capture_output=True, text=True, env=env)


def test_cli_resolve_and_persist(tmp_path: Path) -> None:
    import os
    env = dict(os.environ)
    root = tmp_path / "assignments"
    out = tmp_path / "out.json"
    argv = [
        "resolve", "--task-id", "task-cli", "--project-id", "proj-cli", "--role", "implementer",
        "--source-repository", "/opt/x", "--source-branch", "main", "--source-sha", "b" * 40,
        "--engine", "claude", "--provider", "anthropic-claude-code", "--host", "gs-recursion",
        "--sandbox-profile", "strict-linux", "--network-profile", "provider-only",
        "--allowed-paths", "apps/api,apps/bot", "--publication-policy", "verified-integration-only",
        "--root", str(root), "--out", str(out),
    ]
    result = run_cli(argv, env)
    check(result.returncode == 0, f"CLI resolve exits 0 (stderr={result.stderr!r})")
    check(out.is_file(), "CLI resolve writes the --out copy")
    if out.is_file():
        emitted = json.loads(out.read_text(encoding="utf-8"))
        digest = emitted.get("assignment_digest", "")
        persisted = root / "task-cli" / f"{digest}.json"
        check(persisted.is_file(), "CLI resolve persists the content-addressed assignment file")
        check(emitted.get("allowed_paths") == ["apps/api", "apps/bot"],
              "CLI resolve normalizes a comma-separated allowed-paths string")
        index = root / "index.jsonl"
        check(index.is_file(), "CLI resolve appends to the durable assignment index")

        # Idempotent re-resolution with the exact same content-addressed file (created_at
        # differs, so this is really a *new* record; assert it does not clobber the first).
        result2 = run_cli(argv, env)
        check(result2.returncode == 0, "a second CLI resolve with identical arguments also exits 0")
        check(persisted.is_file(), "the first persisted assignment file is untouched by the second call")

        show = subprocess.run(
            [sys.executable, str(ASSIGNMENT), "show", "--task-id", "task-cli",
             "--assignment-digest", digest, "--root", str(root)],
            capture_output=True, text=True, env=env,
        )
        check(show.returncode == 0, "CLI show retrieves a persisted assignment")
        check(json.loads(show.stdout).get("assignment_digest") == digest, "CLI show returns matching content")


def test_cli_refuses_bad_role(tmp_path: Path) -> None:
    import os
    env = dict(os.environ)
    argv = [
        "resolve", "--task-id", "task-bad", "--project-id", "proj-bad", "--role", "not-a-role",
        "--source-repository", "/opt/x", "--source-branch", "main", "--source-sha", "c" * 40,
        "--engine", "claude", "--provider", "anthropic-claude-code", "--host", "gs-recursion",
        "--sandbox-profile", "strict-linux", "--network-profile", "provider-only",
        "--allowed-paths", ".", "--publication-policy", "verified-integration-only",
        "--root", str(tmp_path / "assignments"),
    ]
    result = run_cli(argv, env)
    check(result.returncode == 2, f"CLI resolve refuses an invalid role with exit 2 (got {result.returncode})")


def main() -> int:
    test_network_policy_and_profile_are_separate()
    test_digest_stable_and_sensitive()
    test_allowed_paths_normalized()
    test_allowed_paths_fail_closed()
    test_fail_closed_invalid_inputs()
    test_empty_allowed_paths_refused()
    test_validate_rejects_malformed_field_types()
    with tempfile.TemporaryDirectory() as tmp:
        test_persist_idempotent_and_collision_safe(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_persist_rejects_linked_paths_before_writes(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_persist_rejects_deterministic_swaps(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_windows_temp_collision_retries_typed(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_windows_publication_failure_cleans_private_temp(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_windows_durable_lock_atomically_rejects_reparse_leaf(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_windows_durable_lock_pins_parent_through_leaf_open(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_persist_40_process_idempotency(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_capacity_pool_validation(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_load_rejects_reparse_task_directory(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_cli_resolve_and_persist(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_cli_refuses_bad_role(Path(tmp))

    if failures:
        print(f"assignment.test: {len(failures)} FAILED")
        return 1
    print("assignment.test: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
