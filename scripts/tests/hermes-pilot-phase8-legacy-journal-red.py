#!/usr/bin/env python3
"""Phase 8 regression for the reviewed legacy decision-journal compatibility boundary."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from windows_test_support import bash_command, install_single_process_flock_stub, prepare_shell_env


SOURCE = Path(__file__).resolve().parents[2]
LEGACY_HASHES = (
    "d589999830dbae9f212aaefaa95dab3562e89654c799426799f6ce71e3afe081",
    "726c5d82795ee7b42def62b2601f36d0662cb0f5de6f5e4886b11b77f597cc8a",
    "42d4663038df766c987f7555cdf3c3d6a858ff156f5416ce040a0fa62f8b941e",
    "160cee2fed8ead1e0154bb3635cb19588a3ad312fb4ae4c5501a3b9da4e7784b",
    "6c14cb6556f79e81bc3b1b4a0a4cb3c14593cb22902e7170410c990372678dda",
)
CONTROLS = (
    "five reviewed legacy noops are marked without execution and the current noop succeeds once",
    "a new invalid schema-v1 record fails before any marker or projection is written",
    "an unreviewed schema-less noop is not accepted as legacy",
    "an incomplete reviewed legacy prefix is not accepted",
    "a changed legacy marker fails closed instead of rewriting migration evidence",
    "an unterminated torn tail is quarantined exactly while committed decisions still import",
    "the supervised actuator reports a journal-import failure while preserving pending state",
)


class PlatformSkip(Exception):
    """A Linux process/signal proof that cannot be exercised faithfully on Windows."""


def legacy_rows() -> list[dict]:
    return [
        {
            "args": [], "confidence": "high", "cost_usd": "0.0",
            "goal": "D-010 — rebase Recursion onto a Hermes-led control plane with bounded executors",
            "project": "recursion", "provider": "openai-codex", "text": "",
            "ts": "2026-08-07T03:15:01Z", "verb": "noop",
            "why": "The runtime is PAUSED and permits only noop. No open Lucas instruction requires authoring or escalation, and execution must remain halted during the architecture rebase.",
            "wrong_if": "The runtime pause is lifted or Lucas issues an instruction requiring an allowed authoring or escalation verb.",
        },
        {
            "args": [], "confidence": "high", "cost_usd": "0.0",
            "goal": "D-010 — Rebase Recursion onto a Hermes-led control plane; Claude becomes a bounded executor",
            "project": "recursion", "provider": "openai-codex", "text": "",
            "ts": "2026-08-07T06:15:01Z", "verb": "noop",
            "why": "RUNTIME reports PAUSED, and the stated halt forbids planning, conducting, or running arms until a human resumes execution.",
            "wrong_if": "The runtime pause is demonstrably lifted by scripts/resume.sh and reports execution admitted.",
        },
        {
            "args": [], "confidence": "high", "cost_usd": "0.0",
            "goal": "SAMPLE-first sequencing: reach a usable example-game reconstruction checkpoint against the August game-update clock",
            "project": "sample", "provider": "openai-codex", "text": "",
            "ts": "2026-08-07T09:15:01Z", "verb": "noop",
            "why": "PAUSED explicitly restricts executable decisions to noop. No open Lucas instruction overrides the halt.",
            "wrong_if": "The runtime PAUSE state is lifted or shown inaccurate before this decision is executed.",
        },
        {
            "args": [], "confidence": "high", "cost_usd": "0.0",
            "goal": "D-012 — PAUSE halts execution and preserves awareness",
            "project": "recursion", "provider": "openai-codex", "text": "",
            "ts": "2026-08-07T12:15:01Z", "verb": "noop",
            "why": "Execution is explicitly halted pending the human-led architecture rebase; no executable verb other than noop is permitted.",
            "wrong_if": "The runtime reports PAUSED is lifted and execution is authorized without a superseding blocker.",
        },
        {
            "args": [], "confidence": "high", "cost_usd": "0.0",
            "goal": "D-012 — PAUSE halts execution and preserves awareness",
            "project": "recursion", "provider": "openai-codex", "text": "",
            "ts": "2026-08-07T15:15:01Z", "verb": "noop",
            "why": "The runtime explicitly reports PAUSED; under the governing rules, noop is the only executable decision until Lucas resumes the box.",
            "wrong_if": "The runtime state changes from PAUSED to resumed, or a human instruction explicitly authorizes execution.",
        },
    ]


def legacy_lines() -> list[str]:
    lines = [json.dumps(row, ensure_ascii=True) for row in legacy_rows()]
    assert tuple(hashlib.sha256(line.encode()).hexdigest() for line in lines) == LEGACY_HASHES
    return lines


def current_noop() -> dict:
    reason = "PAUSE remains active and no permitted instruction requires execution."
    return {
        "actor": "head-hermes", "args": [], "arguments": [], "confidence": "high",
        "cost_usd": 0.0, "goal": "architecture-rebase", "goal_id": "architecture-rebase",
        "decision_source": "hermes-model", "routed_decider": "head-hermes",
        "id": "decision-6f037cbf6fb51a7f641ea9d6",
        "idempotency_key": "decision:44ce5a43f9a48395df055f2614ff65a639bb24761e46a0a4b83dbd77626b8b9e",
        "project": "recursion", "provider": "openai-codex", "reason": reason,
        "schema_version": 1, "status": "queued", "ts": "2026-08-09T15:15:01Z",
        "usage": {"estimated_cost_usd": 0.0, "provider": "openai-codex", "total_tokens": 1},
        "verb": "noop", "why": reason,
        "wrong_if": "state/PAUSE is removed or Lucas supplies an authenticated instruction.",
    }


class Fixture:
    def __init__(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="phase8-legacy-journal-"))
        self.root = self.temp / "root"
        self.fixture_bin = self.temp / "fixture-bin"
        for relative in (
            "scripts", "state/decision-queue/pending", "state/decision-queue/running",
            "state/decision-queue/succeeded", "state/decision-queue/failed", "state/actions",
            "state/.locks", "loop/logs",
        ):
            (self.root / relative).mkdir(parents=True, exist_ok=True)
        for name in (
            "assignment.py", "assignment_contract.py", "phase5_safety.py",
            "assist.sh", "author.sh", "board.sh", "branch-review.sh",
            "capacity.sh", "conduct-hermes.sh", "conduct.sh", "decision-queue.py",
            "engine_contract.py", "git-scope.py", "hermes-actuator.sh", "hermes-decide.sh",
            "instruction_routing.py", "intake.sh", "jsonl_durable.py", "notification-outbox.py",
            "notify.sh", "pilot_instruction.py", "plan.sh", "project-pilot.py",
            "project-reconcile.py", "project_authority.py", "provider-connect-proxy.py", "provider_contract.py",
            "provider-network.sh", "provider-egress.py", "publish.sh", "remote_evidence.py", "resolve-engine.py",
            "resolve-model-alias.py", "resync.sh", "run_contract.py", "runtime-state.py",
            "seed_contract.py", "taskctl.sh", "transition_receipt.py",
            "validate-assignment-result.py", "validate-control-state.py", "validate-decision.py",
            "validate-gate.py", "validate-verifier-result.py", "validate-worker-result.py",
            "work.sh", "assignment-token.py", "worker-evidence.py", "worker-sandbox.sh",
        ):
            shutil.copy2(SOURCE / "scripts" / name, self.root / "scripts" / name)
        if os.name == "nt":
            (self.root / "scripts/fcntl.py").write_text(
                "LOCK_EX=2\nLOCK_NB=4\ndef flock(_file, _flags):\n    return None\n", encoding="utf-8"
            )
            install_single_process_flock_stub(self.fixture_bin)
        for path in (self.root / "scripts").iterdir():
            path.chmod(path.stat().st_mode | 0o111)
        self.env = os.environ.copy()
        prepare_shell_env(self.env)
        if os.name == "nt":
            self.env["PATH"] = str(self.fixture_bin) + os.pathsep + self.env.get("PATH", "")
        self.env.update({"RECURSION_ROOT": str(self.root), "RECURSION_NO_NOTIFY": "1"})

    def close(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def write_journal(self, lines: list[str]) -> bytes:
        value = ("\n".join(lines) + "\n").encode()
        (self.root / "state/decisions.jsonl").write_bytes(value)
        return value

    def write_current_projection(self) -> Path:
        path = self.root / "state/decision-queue/pending/decision-6f037cbf6fb51a7f641ea9d6.json"
        normalized = subprocess.run(
            [sys.executable, "scripts/decision-queue.py", "normalize"], cwd=self.root,
            env=self.env, input=json.dumps(current_noop()), text=True,
            capture_output=True, check=False,
        )
        if normalized.returncode:
            raise RuntimeError(f"current noop normalization failed: {normalized.stderr.strip()}")
        path.write_text(normalized.stdout, encoding="utf-8")
        return path

    def run_queue(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "scripts/decision-queue.py", "import-journal"], cwd=self.root,
            env=self.env, text=True, capture_output=True, check=False,
        )

    def run_conduct(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            bash_command("scripts/conduct-hermes.sh", "--paused-safe"), cwd=self.root,
            env=self.env, text=True, capture_output=True, check=False,
        )


def state_digest(root: Path) -> str:
    rows: list[tuple[str, str]] = []
    for relative in ("state/decision-queue", "state/actions", "state/action-events.jsonl"):
        path = root / relative
        if path.is_file():
            rows.append((relative, hashlib.sha256(path.read_bytes()).hexdigest()))
        elif path.is_dir():
            for item in sorted(path.rglob("*")):
                if item.is_file():
                    rows.append((item.relative_to(root).as_posix(), hashlib.sha256(item.read_bytes()).hexdigest()))
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def test_live_shape() -> tuple[bool, str]:
    fixture = Fixture()
    try:
        original = fixture.write_journal(legacy_lines() + [json.dumps(current_noop(), sort_keys=True, separators=(",", ":"))])
        fixture.write_current_projection()
        (fixture.root / "state/PAUSE").write_text("fixture PAUSE\n", encoding="utf-8")
        first = fixture.run_conduct()
        markers = sorted((fixture.root / "state/decision-queue/legacy").glob("*.json"))
        succeeded = list((fixture.root / "state/decision-queue/succeeded").glob("*.json"))
        actions = list((fixture.root / "state/actions").glob("*.json"))
        events_path = fixture.root / "state/action-events.jsonl"
        if first.returncode or len(markers) != 5 or len(succeeded) != 1 or len(actions) != 1 or not events_path.is_file():
            return False, f"rc={first.returncode} markers={len(markers)} succeeded={len(succeeded)} actions={len(actions)} stderr={first.stderr[-160:]}"
        if (fixture.root / "state/decisions.jsonl").read_bytes() != original:
            return False, "append-only journal changed"
        marker_rows = [json.loads(path.read_text(encoding="utf-8")) for path in markers]
        if {row.get("source_sha256") for row in marker_rows} != set(LEGACY_HASHES):
            return False, "legacy marker hashes differ"
        if {row.get("source_line") for row in marker_rows} != set(range(1, 6)):
            return False, "legacy marker source lines differ"
        if any(row.get("disposition") != "not-enqueued" or row.get("classification") != "reviewed-pre-schema-noop" for row in marker_rows):
            return False, "legacy marker disposition differs"
        marker_text = "\n".join(path.read_text(encoding="utf-8") for path in markers)
        if any(row["goal"] in marker_text for row in legacy_rows()):
            return False, "marker copied legacy prose"
        action = json.loads(actions[0].read_text(encoding="utf-8"))
        events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line]
        decision = json.loads(succeeded[0].read_text(encoding="utf-8"))
        if action.get("status") != "succeeded" or action.get("attempts") != 1 or action.get("verb") != "noop":
            return False, "current noop action was not exactly-once succeeded"
        expected_action = "action-" + hashlib.sha256(current_noop()["idempotency_key"].encode()).hexdigest()[:24]
        if action.get("id") != expected_action or action.get("decision_id") != current_noop()["id"]:
            return False, "current noop action identity differs"
        if [row.get("status") for row in events] != ["pending", "running", "succeeded"]:
            return False, "current noop action events differ"
        if decision.get("id") != current_noop()["id"] or decision.get("status") != "executed":
            return False, "current noop decision did not reach succeeded"
        before = state_digest(fixture.root)
        second = fixture.run_conduct()
        if second.returncode or state_digest(fixture.root) != before:
            return False, "restart replay changed queue/action state"
        return True, ""
    finally:
        fixture.close()


def test_new_invalid() -> tuple[bool, str]:
    fixture = Fixture()
    try:
        invalid = current_noop()
        invalid["id"] = "decision-new-invalid"
        invalid["goal"] = invalid["goal_id"] = "new invalid prose goal"
        fixture.write_journal(legacy_lines() + [json.dumps(current_noop()), json.dumps(invalid)])
        result = fixture.run_queue()
        files = list((fixture.root / "state/decision-queue").rglob("*.json"))
        return result.returncode != 0 and not files, result.stderr[-160:]
    finally:
        fixture.close()


def test_unreviewed_legacy() -> tuple[bool, str]:
    fixture = Fixture()
    try:
        row = legacy_rows()[0]
        row["ts"] = "2026-08-07T03:15:02Z"
        fixture.write_journal([json.dumps(row, ensure_ascii=True)])
        result = fixture.run_queue()
        files = list((fixture.root / "state/decision-queue").rglob("*.json"))
        return result.returncode != 0 and not files, result.stderr[-160:]
    finally:
        fixture.close()


def test_incomplete_prefix() -> tuple[bool, str]:
    fixture = Fixture()
    try:
        fixture.write_journal(legacy_lines()[:4] + [json.dumps(current_noop())])
        result = fixture.run_queue()
        files = list((fixture.root / "state/decision-queue").rglob("*.json"))
        return result.returncode != 0 and not files, result.stderr[-160:]
    finally:
        fixture.close()


def test_marker_tamper() -> tuple[bool, str]:
    fixture = Fixture()
    try:
        fixture.write_journal(legacy_lines())
        first = fixture.run_queue()
        markers = sorted((fixture.root / "state/decision-queue/legacy").glob("*.json"))
        if first.returncode or len(markers) != 5:
            return False, f"initial migration rc={first.returncode} markers={len(markers)}"
        value = json.loads(markers[0].read_text(encoding="utf-8"))
        value["disposition"] = "tampered"
        tampered_bytes = json.dumps(value) + "\n"
        markers[0].write_text(tampered_bytes, encoding="utf-8")
        second = fixture.run_queue()
        # Assert the property, not the prose. The CLI now emits one controlled refusal
        # ("decision-queue: operation refused") and deliberately keeps the specific
        # marker-mismatch text off a public boundary, so grepping stderr for "marker" tested the
        # wording rather than the behaviour. What this control actually claims is in its name:
        # the run fails closed AND the tampered marker is not rewritten to re-bless itself as
        # migration evidence. Both are checked directly, and the second was never checked before.
        failed_closed = second.returncode != 0
        still_tampered = markers[0].read_text(encoding="utf-8") == tampered_bytes
        marker_count_unchanged = len(sorted(
            (fixture.root / "state/decision-queue/legacy").glob("*.json"))) == 5
        detail = (f"rc={second.returncode} still_tampered={still_tampered} "
                  f"markers={marker_count_unchanged} stderr={second.stderr[-100:]}")
        return failed_closed and still_tampered and marker_count_unchanged, detail
    finally:
        fixture.close()


def test_torn_tail_quarantine() -> tuple[bool, str]:
    fixture = Fixture()
    try:
        committed = (json.dumps(current_noop(), sort_keys=True, separators=(",", ":")) + "\n").encode()
        torn = b'{"schema_version":1,"id":"decision-torn"'
        journal = fixture.root / "state/decisions.jsonl"
        journal.write_bytes(committed + torn)
        result = fixture.run_queue()
        pending = fixture.root / "state/decision-queue/pending" / f"{current_noop()['id']}.json"
        quarantine = fixture.root / "state/decision-queue/quarantine"
        payloads = sorted(quarantine.glob("*.bin")) if quarantine.exists() else []
        metadata = sorted(quarantine.glob("*.json")) if quarantine.exists() else []
        if result.returncode or not pending.is_file() or journal.read_bytes() != committed:
            return False, f"rc={result.returncode} pending={pending.exists()} stderr={result.stderr[-160:]}"
        if len(payloads) != 1 or len(metadata) != 1 or payloads[0].read_bytes() != torn:
            return False, f"payloads={len(payloads)} metadata={len(metadata)}"
        evidence = json.loads(metadata[0].read_text(encoding="utf-8"))
        return (
            evidence.get("tail_sha256") == hashlib.sha256(torn).hexdigest()
            and evidence.get("tail_bytes") == len(torn)
            and evidence.get("journal_offset") == len(committed)
        ), json.dumps(evidence, sort_keys=True)[-200:]
    finally:
        fixture.close()


def test_actuator_diagnostic() -> tuple[bool, str]:
    if os.name == "nt":
        raise PlatformSkip(
            "supervised actuator process-tree and signal handling require Linux; proof remains pending"
        )
    fixture = Fixture()
    try:
        invalid = current_noop()
        invalid["goal"] = invalid["goal_id"] = "invalid current goal"
        fixture.write_journal([json.dumps(invalid)])
        (fixture.root / "state/PAUSE").write_text("fixture PAUSE\n", encoding="utf-8")
        env = dict(fixture.env, RECURSION_ACTUATOR_INTERVAL_S="0.05")
        process = subprocess.Popen(
            bash_command("scripts/hermes-actuator.sh"), cwd=fixture.root, env=env,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        time.sleep(0.25)
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
        diagnostic = stdout + stderr
        actions = list((fixture.root / "state/actions").glob("*.json"))
        return (
            "PAUSE-safe queue pass failed rc=74; pending state was preserved" in diagnostic
            and not actions
        ), diagnostic[-240:]
    finally:
        fixture.close()


def main() -> int:
    tests = (
        test_live_shape, test_new_invalid, test_unreviewed_legacy, test_incomplete_prefix,
        test_marker_tamper, test_torn_tail_quarantine, test_actuator_diagnostic,
    )
    failed = 0
    skipped = 0
    for description, test in zip(CONTROLS, tests):
        try:
            passed, detail = test()
        except PlatformSkip as reason:
            skipped += 1
            print(f"skip  {description}: {reason}")
            continue
        except Exception as exc:  # noqa: BLE001 - the control must report every mismatch.
            passed, detail = False, repr(exc)
        if passed:
            print(f"ok    {description}")
        else:
            failed += 1
            print(f"FAIL  {description}: {detail}")
    print()
    if failed:
        print(f"hermes-pilot-phase8-legacy-journal-red: {failed} FAILED")
        return 1
    if skipped:
        print(f"hermes-pilot-phase8-legacy-journal-red: SKIPPED ({skipped} of {len(CONTROLS)} did not run)")
        return 77
    print("hermes-pilot-phase8-legacy-journal-red: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
