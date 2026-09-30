#!/usr/bin/env python3
"""Focused controls for immutable decision authority/effect identity."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock


SOURCE = Path(__file__).resolve().parents[2]
if os.name == "nt":
    sys.modules.setdefault(
        "fcntl",
        types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=lambda *_args: None),
    )
spec = importlib.util.spec_from_file_location(
    "decision_queue_identity", SOURCE / "scripts/decision-queue.py"
)
assert spec is not None and spec.loader is not None
queue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queue)


def raw(**changes: object) -> dict:
    value: dict = {
        "id": "decision-fixed",
        "actor": "pilot-example-ops",
        "verb": "work",
        "args": ["task-one"],
        "text": "first effect",
        "plan": {"step": "one"},
        "instruction_id": "instruction-one",
        "project": "example-ops",
        "goal": "goal-one",
        "why": "deterministic reconciliation",
        "wrong_if": "state changed",
        "decision_source": "project-reconciler",
        "routed_decider": "pilot-example-ops",
        "idempotency_key": "fixed-key",
    }
    value.update(changes)
    return value


def collision_control() -> tuple[bool, str]:
    root = Path(tempfile.mkdtemp(prefix="decision-identity-"))
    try:
        queue.validate_record = lambda *_args: None
        first = queue.normalize(raw())
        path = queue.enqueue(root, first, journal=False)
        if queue.enqueue(root, queue.normalize(raw()), journal=False) != path:
            return False, "byte-equivalent replay did not reuse its projection"
        mutations = (
            {"text": "different effect"},
            {"plan": {"step": "two"}},
            {"actor": "head-hermes"},
            {"decision_source": "hermes-model"},
            {"routed_decider": "head-hermes"},
        )
        refused: list[str] = []
        for mutation in mutations:
            try:
                queue.enqueue(root, queue.normalize(raw(**mutation)), journal=False)
            except ValueError as exc:
                refused.append(str(exc))
        return len(refused) == len(mutations), f"refused={len(refused)}/{len(mutations)}"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def journal_collision_control() -> tuple[bool, str]:
    root = Path(tempfile.mkdtemp(prefix="decision-journal-identity-"))
    try:
        queue.validate_record = lambda *_args: None
        journal = root / "state/decisions.jsonl"
        journal.parent.mkdir(parents=True)
        records = [queue.normalize(raw()), queue.normalize(raw(text="other effect"))]
        journal.write_text(
            "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in records),
            encoding="utf-8",
        )
        try:
            queue.import_journal(root)
        except ValueError as exc:
            projections = list((root / "state/decision-queue").glob("*/*.json"))
            return not projections and "collision" in str(exc), str(exc)
        return False, "journal collision was imported"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def stored_projection_tamper_control() -> tuple[bool, str]:
    root = Path(tempfile.mkdtemp(prefix="decision-projection-tamper-"))
    try:
        queue.validate_record = lambda *_args: None
        original = queue.normalize(raw())
        path = queue.enqueue(root, original, journal=False)
        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["text"] = "changed after durable enqueue"
        path.write_text(json.dumps(tampered, sort_keys=True) + "\n", encoding="utf-8")
        try:
            queue.enqueue(root, original, journal=False)
        except ValueError as exc:
            return "digest is inconsistent" in str(exc), str(exc)
        return False, "tampered stored projection reused its stale digest"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def claimed_projection_tamper_control() -> tuple[bool, str]:
    root = Path(tempfile.mkdtemp(prefix="decision-claim-tamper-"))
    try:
        queue.validate_record = lambda *_args: None
        original = queue.normalize(raw(
            id="decision-claim", idempotency_key="decision-claim-key"
        ))
        path = queue.enqueue(root, original, journal=True)
        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["args"] = ["different-task"]
        path.write_text(json.dumps(tampered, sort_keys=True) + "\n", encoding="utf-8")
        try:
            queue.claim(root)
        except ValueError as exc:
            running = list((root / "state/decision-queue/running").glob("*.json"))
            return not running and "digest is inconsistent" in str(exc), str(exc)
        return False, "tampered pending authority was claimed"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def torn_tail_enqueue_control() -> tuple[bool, str]:
    root = Path(tempfile.mkdtemp(prefix="decision-enqueue-torn-tail-"))
    try:
        queue.validate_record = lambda *_args: None
        first = queue.normalize(raw(id="decision-prior", idempotency_key="prior-key"))
        incoming = queue.normalize(raw(id="decision-next", idempotency_key="next-key"))
        first_bytes = (json.dumps(first, sort_keys=True, separators=(",", ":")) + "\n").encode()
        torn = b'{"schema_version":1,"id":"decision-interrupted"'
        journal = root / "state/decisions.jsonl"
        journal.parent.mkdir(parents=True)
        before = first_bytes + torn
        journal.write_bytes(before)

        projection = queue.enqueue(root, incoming, journal=True)
        rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        payloads = list((root / "state/decision-queue/quarantine").glob("*.bin"))
        metadata = list((root / "state/decision-queue/quarantine").glob("*.json"))
        if len(payloads) != 1 or len(metadata) != 1:
            return False, f"payloads={len(payloads)} metadata={len(metadata)}"
        evidence = json.loads(metadata[0].read_text(encoding="utf-8"))
        passed = (
            rows == [first, incoming]
            and journal.read_bytes().endswith(b"\n")
            and payloads[0].read_bytes() == torn
            and evidence.get("journal_sha256_before") == hashlib.sha256(before).hexdigest()
            and evidence.get("journal_offset") == len(first_bytes)
            and evidence.get("tail_bytes") == len(torn)
            and evidence.get("tail_sha256") == hashlib.sha256(torn).hexdigest()
            and json.loads(projection.read_text(encoding="utf-8")) == incoming
        )
        return passed, json.dumps(evidence, sort_keys=True)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def complete_bad_row_enqueue_control() -> tuple[bool, str]:
    details: list[str] = []
    for suffix, bad in (("malformed", b'{"id":}\n'), ("non-object", b'[]\n')):
        root = Path(tempfile.mkdtemp(prefix=f"decision-enqueue-{suffix}-"))
        try:
            queue.validate_record = lambda *_args: None
            journal = root / "state/decisions.jsonl"
            journal.parent.mkdir(parents=True)
            original = b'{"id":"legacy-object-preserved-exactly"}\n' + bad
            journal.write_bytes(original)
            incoming = queue.normalize(raw(id=f"decision-{suffix}", idempotency_key=suffix))
            try:
                queue.enqueue(root, incoming, journal=True)
            except ValueError as exc:
                details.append(str(exc))
            else:
                return False, f"{suffix} row was accepted"
            if journal.read_bytes() != original or list((root / "state/decision-queue").glob("*/*.json")):
                return False, f"{suffix} refusal mutated journal or created a projection"
        finally:
            shutil.rmtree(root, ignore_errors=True)
    return len(details) == 2, " | ".join(details)


def crash_after_journal_retry_control() -> tuple[bool, str]:
    root = Path(tempfile.mkdtemp(prefix="decision-journal-retry-"))
    try:
        queue.validate_record = lambda *_args: None
        record = queue.normalize(raw(id="decision-retry", idempotency_key="retry-key"))
        target = root / "state/decision-queue/pending/decision-retry.json"

        real_atomic_json = queue.atomic_json

        def crash_before_projection(path: Path, value: dict) -> None:
            if path == target:
                raise OSError("simulated crash after journal fsync")
            real_atomic_json(path, value)

        with mock.patch.object(queue, "atomic_json", side_effect=crash_before_projection):
            try:
                queue.enqueue(root, record, journal=True)
            except OSError:
                pass
            else:
                return False, "crash failpoint did not interrupt projection"
        journal = root / "state/decisions.jsonl"
        if target.exists() or len(journal.read_text(encoding="utf-8").splitlines()) != 1:
            return False, "crash did not leave exactly one journal row and no projection"

        collision = queue.normalize(raw(
            id="decision-retry", idempotency_key="retry-key", text="different effect"
        ))
        try:
            queue.enqueue(root, collision, journal=True)
        except ValueError as exc:
            if "collision" not in str(exc):
                return False, str(exc)
        else:
            return False, "same-id journal collision was accepted"

        projection = queue.enqueue(root, record, journal=True)
        rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        return (
            projection == target and rows == [record]
            and json.loads(projection.read_text(encoding="utf-8")) == record
        ), f"rows={len(rows)} projection={projection.exists()}"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def journal_directory_fsync_control() -> tuple[bool, str]:
    invocation_root = Path(tempfile.mkdtemp(prefix="decision-journal-dir-fsync-"))
    failure_root = Path(tempfile.mkdtemp(prefix="decision-journal-dir-fsync-failure-"))
    try:
        queue.validate_record = lambda *_args: None
        for root in (invocation_root, failure_root):
            (root / "state/.locks").mkdir(parents=True)
            (root / "state/decision-queue/pending").mkdir(parents=True)

        calls: list[Path] = []
        real_fsync_dir = queue.fsync_dir

        def record_fsync(path: Path) -> None:
            calls.append(path)
            real_fsync_dir(path)

        with mock.patch.object(queue, "fsync_dir", side_effect=record_fsync):
            queue.enqueue(
                invocation_root,
                queue.normalize(raw(id="decision-dir-sync", idempotency_key="dir-sync")),
                journal=True,
            )
        if invocation_root / "state" not in calls:
            return False, f"journal parent fsync absent: {[str(path) for path in calls]}"

        failed = False
        failure_record = queue.normalize(raw(id="decision-dir-failure", idempotency_key="dir-failure"))

        def fail_journal_parent(path: Path) -> None:
            nonlocal failed
            if path == failure_root / "state" and not failed:
                failed = True
                raise OSError("simulated journal directory fsync failure")
            real_fsync_dir(path)

        with mock.patch.object(queue, "fsync_dir", side_effect=fail_journal_parent):
            try:
                queue.enqueue(failure_root, failure_record, journal=True)
            except OSError as exc:
                if "directory fsync failure" not in str(exc):
                    return False, str(exc)
            else:
                return False, "directory fsync failure did not stop projection"
        failure_journal = failure_root / "state/decisions.jsonl"
        failure_target = failure_root / "state/decision-queue/pending/decision-dir-failure.json"
        if failure_target.exists() or len(failure_journal.read_text(encoding="utf-8").splitlines()) != 1:
            return False, "directory fsync failure did not preserve one recoverable journal row"

        queue.enqueue(failure_root, failure_record, journal=True)
        rows = [json.loads(line) for line in failure_journal.read_text(encoding="utf-8").splitlines()]
        return rows == [failure_record] and failure_target.is_file(), f"calls={len(calls)} rows={len(rows)}"
    finally:
        shutil.rmtree(invocation_root, ignore_errors=True)
        shutil.rmtree(failure_root, ignore_errors=True)


def incremental_import_checkpoint_control() -> tuple[bool, str]:
    root = Path(tempfile.mkdtemp(prefix="decision-import-checkpoint-"))
    try:
        journal = root / "state/decisions.jsonl"
        journal.parent.mkdir(parents=True)
        records = [
            queue.normalize(raw(
                id=f"decision-history-{number:04d}",
                idempotency_key=f"history-{number:04d}",
                text=f"effect-{number:04d}",
            ))
            for number in range(96)
        ]
        journal.write_text(
            "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                    for row in records),
            encoding="utf-8",
        )
        validations = 0

        def count_validation(_base: Path, _record: dict) -> None:
            nonlocal validations
            validations += 1

        queue.validate_record = count_validation
        if queue.import_journal(root) != len(records):
            return False, "initial import did not project the full authoritative journal"
        initial = validations
        if queue.import_journal(root) != 0:
            return False, "steady-state import recreated a projection"
        steady_delta = validations - initial
        tail_record = queue.normalize(raw(
            id="decision-history-tail", idempotency_key="history-tail", text="tail-effect"
        ))
        queue.append_journal(root, tail_record)
        before_tail = validations
        if queue.import_journal(root) != 1:
            return False, "tail import did not create exactly one projection"
        tail_delta = validations - before_tail
        return (
            initial == len(records) + 1 and steady_delta == 1 and tail_delta == 2
        ), f"initial={initial} steady_delta={steady_delta} tail_delta={tail_delta}"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def succeeded_transition_control() -> tuple[bool, str]:
    root = Path(tempfile.mkdtemp(prefix="decision-succeeded-transition-"))
    try:
        queue.validate_record = lambda *_args: None
        record = queue.normalize(raw(
            id="decision-succeeded", idempotency_key="succeeded-key"
        ))
        running = root / "state/decision-queue/running"
        actions = root / "state/actions"
        running.mkdir(parents=True)
        actions.mkdir(parents=True)
        projection = running / f"{record['id']}.json"
        encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        projection.write_bytes(encoded)
        action_id = queue.action_id_for_decision(record)
        action = {
            "schema_version": 1, "id": action_id, "decision_id": record["id"],
            "instruction_id": record.get("instruction_id"),
            "idempotency_key": record["idempotency_key"], "verb": record["verb"],
            "arguments": record["args"], "goal_id": record["goal_id"],
            "actor": record["actor"], "project": record.get("project"),
            "decision_source": record["decision_source"],
            "routed_decider": record["routed_decider"],
            "action_payload_sha256": record["decision_payload_sha256"],
            "status": "succeeded", "attempts": 1,
        }
        (actions / f"{action_id}.json").write_text(json.dumps(action) + "\n", encoding="utf-8")
        target = queue.transition(
            root, projection, "succeeded", "action succeeded", hashlib.sha256(encoded).hexdigest()
        )
        if not target.is_file() or projection.exists():
            return False, "matching succeeded action did not publish terminal projection"

        denied = queue.normalize(raw(
            id="decision-actionless", idempotency_key="actionless-key"
        ))
        denied_path = running / f"{denied['id']}.json"
        denied_bytes = (json.dumps(denied, sort_keys=True, separators=(",", ":")) + "\n").encode()
        denied_path.write_bytes(denied_bytes)
        try:
            queue.transition(
                root, denied_path, "succeeded", "should refuse", hashlib.sha256(denied_bytes).hexdigest()
            )
        except ValueError as exc:
            return ("without its succeeded action" in str(exc) and denied_path.is_file(),
                    str(exc))
        return False, "actionless decision reached succeeded"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    failures = 0
    for description, control in (
        ("same-id decision projections bind every authority/effect field", collision_control),
        ("same-id journal collisions fail before any projection", journal_collision_control),
        ("stored decision tampering cannot reuse a stale payload digest", stored_projection_tamper_control),
        ("a pending decision is rebound to its journal authority immediately before claim",
         claimed_projection_tamper_control),
        ("enqueue quarantines a SIGKILL-torn tail before a separately parseable append",
         torn_tail_enqueue_control),
        ("enqueue refuses complete malformed and non-object rows without mutation",
         complete_bad_row_enqueue_control),
        ("crash-after-journal retry reuses one row and rejects a same-id collision",
         crash_after_journal_retry_control),
        ("journal append fsyncs its directory and a failure leaves one retryable row",
         journal_directory_fsync_control),
        ("decision import validates only its bounded legacy prefix and uncheckpointed tail",
         incremental_import_checkpoint_control),
        ("queue success requires the matching durable succeeded action",
         succeeded_transition_control),
    ):
        try:
            passed, detail = control()
        except Exception as exc:  # noqa: BLE001
            passed, detail = False, repr(exc)
        print(("ok    " if passed else "FAIL  ") + description + ("" if passed else f": {detail}"))
        failures += 0 if passed else 1
    print(f"decision-identity: {'ALL PASS' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
