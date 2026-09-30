#!/usr/bin/env python3
"""Phase-8 remote-proof availability must defer, never terminally block an active goal."""

from __future__ import annotations

import atexit
import json
import contextlib
import io
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

from windows_test_support import bash_command


SOURCE = Path(__file__).resolve().parents[2]

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

    The `fcntl` half of this was already here, because project-reconcile.py and
    project-pilot.py import it unconditionally. `os.fchmod` is the other half and was the
    reason this suite died on a missing state/events.jsonl: every deferral audit append went
    through a `mkstemp -> fchmod -> os.replace` writer that raised AttributeError with its
    descriptor still open, whose cleanup `unlink` then masked it as `[WinError 32]`, so the
    journal was never created. The controls are hermetic and single-process, so no-contention
    stand-ins let the real state machines run. The shim directory is exported on PYTHONPATH,
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
    __import__("fcntl")
    inherited = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = (
        str(directory) + os.pathsep + inherited if inherited else str(directory)
    )


_install_nt_posix_shim()

RECONCILE = runpy.run_path(str(SOURCE / "scripts/project-reconcile.py"))
PILOT = runpy.run_path(str(SOURCE / "scripts/project-pilot.py"))
phase8_fresh_base = RECONCILE["phase8_fresh_base"]
record_authority_deferral = RECONCILE["record_authority_deferral"]
require_window = RECONCILE["require_window"]
open_window = RECONCILE["open_window"]
admit_work = RECONCILE["admit_work"]
check_work_authority = RECONCILE["check_work_authority"]
reconcile_read_json = RECONCILE["read_json"]
reconcile_read_fields = RECONCILE["read_fields"]
schedule = RECONCILE["schedule"]
ReconcileError = RECONCILE["ReconcileError"]
pilot_records = PILOT["records"]
pilot_active_source_pin = PILOT["active_source_pin"]


def work_checkpoint_routing() -> bool:
    """Execute the exact work.sh checkpoint function and observe its controller seam."""
    work = (SOURCE / "scripts/work.sh").read_text(encoding="utf-8")
    start = work.find("window_checkpoint(){")
    end_marker = "\n}\nwindow_checkpoint startup || exit 75"
    end = work.find(end_marker, start)
    admission = work.find("\nfresh_window_admission || exit $?\n", end)
    launch = work.find('run_arm_supervised "$WT" 1')
    if start < 0 or end < 0 \
            or admission < 0 or launch < 0 or admission > launch \
            or 'window_checkpoint "$role execution"' not in work \
            or 'cancelled "$WINDOW_AUTHORITY_FAILURE" 75' not in work:
        return False
    function = work[start:end + 2]
    root = Path(tempfile.mkdtemp(prefix="phase8-work-checkpoint-"))
    try:
        scripts = root / "scripts"
        scripts.mkdir()
        calls = root / "calls"
        helper = scripts / "project-reconcile.py"
        helper.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, sys\n"
            "calls = pathlib.Path(os.environ['CALLS'])\n"
            "prior = calls.read_text(encoding='utf-8').splitlines() if calls.is_file() else []\n"
            "with calls.open('a', encoding='utf-8') as out:\n"
            "    out.write(sys.argv[1] + '\\n')\n"
            "if sys.argv[1] == 'admit-work':\n"
            "    raise SystemExit(int(os.environ.get('ADMIT_RC', '0')))\n"
            "if sys.argv[1] == 'check-work-authority':\n"
            "    count = sum(value == 'check-work-authority' for value in prior) + 1\n"
            "    after = int(os.environ.get('CHECK_FAIL_AFTER', '0'))\n"
            "    if after and count >= after:\n"
            "        raise SystemExit(int(os.environ.get('CHECK_RC', '74')))\n"
            "    if after:\n"
            "        raise SystemExit(0)\n"
            "    raise SystemExit(int(os.environ.get('CHECK_RC', '0')))\n",
            encoding="utf-8",
        )
        helper.chmod(0o755)
        runner = root / "run.sh"
        runner.write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            f"ROOT={json.dumps(str(root))}\n"
            "PILOT_ID=pilot-fixture\nID=task-fixture\nLOG=/dev/null\n"
            "log(){ :; }\n"
            f"{function}\n"
            "window_checkpoint startup\n"
            "fresh_window_admission\n"
            "window_checkpoint 'implementer execution'\n"
            "window_checkpoint 'verifier execution'\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            bash_command(str(runner)), env={
                **os.environ, "CALLS": str(calls), "ADMIT_RC": "0", "CHECK_RC": "0",
            },
            text=True, capture_output=True, timeout=30, check=False,
        )
        normal_calls = calls.read_text(encoding="utf-8").splitlines() if calls.is_file() else []
        deferred = root / "deferred.sh"
        deferred.write_text(
            "#!/usr/bin/env bash\nset -uo pipefail\n"
            f"ROOT={json.dumps(str(root))}\n"
            "PILOT_ID=pilot-fixture\nID=task-fixture\nLOG=/dev/null\n"
            "log(){ :; }\n"
            f"{function}\n"
            "fresh_window_admission\n"
            "printf '%s\\n' \"$?\"\n",
            encoding="utf-8",
        )
        unavailable = subprocess.run(
            bash_command(str(deferred)),
            env={**os.environ, "CALLS": str(calls), "ADMIT_RC": "74"},
            text=True, capture_output=True, timeout=30, check=False,
        )
        revoked = subprocess.run(
            bash_command(str(deferred)),
            env={**os.environ, "CALLS": str(calls), "ADMIT_RC": "75"},
            text=True, capture_output=True, timeout=30, check=False,
        )
        invalid = subprocess.run(
            bash_command(str(deferred)),
            env={**os.environ, "CALLS": str(calls), "ADMIT_RC": "4"},
            text=True, capture_output=True, timeout=30, check=False,
        )
        crashed = subprocess.run(
            bash_command(str(deferred)),
            env={**os.environ, "CALLS": str(calls), "ADMIT_RC": "2"},
            text=True, capture_output=True, timeout=30, check=False,
        )
        local = root / "local-deferred.sh"
        local.write_text(
            "#!/usr/bin/env bash\nset -uo pipefail\n"
            f"ROOT={json.dumps(str(root))}\n"
            "PILOT_ID=pilot-fixture\nID=task-fixture\nLOG=/dev/null\n"
            "log(){ :; }\n"
            f"{function}\n"
            "window_checkpoint startup\n"
            "printf '%s %s\\n' \"$?\" \"$WINDOW_AUTHORITY_FAILURE\"\n",
            encoding="utf-8",
        )
        local_unavailable = subprocess.run(
            bash_command(str(local)),
            env={**os.environ, "CALLS": str(calls), "CHECK_RC": "74"},
            text=True, capture_output=True, timeout=30, check=False,
        )
        local_invalid = subprocess.run(
            bash_command(str(local)),
            env={**os.environ, "CALLS": str(calls), "CHECK_RC": "4"},
            text=True, capture_output=True, timeout=30, check=False,
        )
        local_deferred = subprocess.run(
            bash_command(str(local)),
            env={**os.environ, "CALLS": str(calls), "CHECK_RC": "75"},
            text=True, capture_output=True, timeout=30, check=False,
        )
        role_calls = root / "role-calls"
        role_sentinel = root / "role-sentinel"
        role = root / "role-deferred.sh"
        role.write_text(
            "#!/usr/bin/env bash\nset -uo pipefail\n"
            f"ROOT={json.dumps(str(root))}\n"
            f"SENTINEL={json.dumps(str(role_sentinel))}\n"
            "PILOT_ID=pilot-fixture\nID=task-fixture\nLOG=/dev/null\n"
            "log(){ :; }\n"
            f"{function}\n"
            "window_checkpoint startup || exit $?\n"
            "printf '%s\\n' startup-passed > \"$SENTINEL\"\n"
            "window_checkpoint 'implementer execution'\n"
            "rc=$?\n"
            "[ \"$rc\" -eq 0 ] && printf '%s\\n' later-execution >> \"$SENTINEL\"\n"
            "printf '%s %s\\n' \"$rc\" \"$WINDOW_AUTHORITY_FAILURE\"\n",
            encoding="utf-8",
        )
        role_unavailable = subprocess.run(
            bash_command(str(role)), env={
                **os.environ, "CALLS": str(role_calls), "CHECK_RC": "74",
                "CHECK_FAIL_AFTER": "2",
            },
            text=True, capture_output=True, timeout=30, check=False,
        )
        return result.returncode == 0 and normal_calls == [
            "check-work-authority", "admit-work",
            "check-work-authority", "check-work-authority",
        ] and unavailable.returncode == 0 and unavailable.stdout.strip() == "75" \
            and revoked.returncode == 0 and revoked.stdout.strip() == "75" \
            and invalid.returncode == 0 and invalid.stdout.strip() == "4" \
            and crashed.returncode == 0 and crashed.stdout.strip() == "75" \
            and local_unavailable.returncode == 0 \
            and local_unavailable.stdout.strip() == "75 window-authority-unavailable" \
            and local_invalid.returncode == 0 \
            and local_invalid.stdout.strip() == "75 window-authority-revoked" \
            and local_deferred.returncode == 0 \
            and local_deferred.stdout.strip() == "75 window-authority-deferred" \
            and role_unavailable.returncode == 0 \
            and role_unavailable.stdout.strip() == "75 window-authority-unavailable" \
            and role_calls.read_text(encoding="utf-8").splitlines() == [
                "check-work-authority", "check-work-authority",
            ] \
            and role_sentinel.read_text(encoding="utf-8").splitlines() == ["startup-passed"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def repeated_deferrals_bump_one_escalation_identity() -> bool:
    root = Path(tempfile.mkdtemp(prefix="phase8-authority-deferral-"))
    globals_ = record_authority_deferral.__globals__
    original_run = globals_["subprocess"].run
    commands: list[list[str]] = []
    try:
        (root / "scripts").mkdir()
        (root / "scripts/taskctl.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        (root / "state/.locks").mkdir(parents=True)

        def fake_run(argv, **_kwargs):
            commands.append(list(argv))
            return types.SimpleNamespace(returncode=0, stdout="DUP esc-0001 2\n", stderr="")

        globals_["subprocess"].run = fake_run
        remote_error = RuntimeError("simulated remote timeout")
        provider_error = RuntimeError("simulated provider outage")
        record_authority_deferral(
            root, "example-ops", "goal-example-ops", "remote-unavailable", remote_error,
        )
        record_authority_deferral(
            root, "example-ops", "goal-example-ops", "provider-unavailable", provider_error,
        )
        record_authority_deferral(
            root, "example-ops", "goal-example-ops", "remote-unavailable", remote_error,
        )
        keys = [argv[3] for argv in commands if len(argv) > 3 and argv[2] == "esc-record"]
        events = [
            json.loads(line) for line in (root / "state/events.jsonl").read_text(
                encoding="utf-8",
            ).splitlines()
        ]
        return (
            len(events) == 2 and len({row["id"] for row in events}) == 2
            and {row["detail"]["reason"] for row in events}
            == {"remote-unavailable", "provider-unavailable"}
            and all(set(row["detail"]) == {"reason"} for row in events)
            and keys == ["phase8-authority-proof:example-ops:goal-example-ops"] * 3
        )
    finally:
        globals_["subprocess"].run = original_run
        shutil.rmtree(root, ignore_errors=True)


def final_window_revocation_is_typed_as_deferral() -> bool:
    globals_ = admit_work.__globals__
    originals = {
        name: globals_[name] for name in (
            "read_fields", "verify_phase8_goal_contract",
            "require_provider_capability", "require_window",
        )
    }
    observed_codes: list[int] = []
    try:
        globals_["read_fields"] = lambda *_args, **_kwargs: {
            "project_id": "example-ops", "goal_id": "goal-example-ops",
        }
        globals_["verify_phase8_goal_contract"] = lambda *_args, **_kwargs: {
            "project_id": "example-ops", "goal_id": "goal-example-ops",
        }
        globals_["require_provider_capability"] = lambda *_args, **_kwargs: None

        def revoked(*_args, absent_code=4, **_kwargs):
            observed_codes.append(absent_code)
            raise ReconcileError("exact open pilot window authority is absent", absent_code)

        globals_["require_window"] = revoked
        try:
            admit_work(Path("/fixture"), "task-example-ops")
        except ReconcileError as exc:
            return observed_codes == [75] and exc.code == 75 \
                and "window authority is absent" in str(exc)
        return False
    finally:
        globals_.update(originals)


def hard_refusal_survives_audit_failure() -> bool:
    globals_ = admit_work.__globals__
    originals = {
        name: globals_[name] for name in (
            "read_fields", "verify_phase8_goal_contract", "append_event",
        )
    }
    try:
        globals_["read_fields"] = lambda *_args, **_kwargs: {
            "project_id": "example-ops", "goal_id": "goal-example-ops",
        }

        def invalid(*_args, **_kwargs):
            raise ReconcileError("proved authority contradiction", 4)

        def audit_failed(*_args, **_kwargs):
            raise ReconcileError("simulated journal outage", 4)

        globals_["verify_phase8_goal_contract"] = invalid
        globals_["append_event"] = audit_failed
        try:
            admit_work(Path("/fixture"), "task-example-ops")
        except ReconcileError as exc:
            return exc.code == 4 and "proved authority contradiction" in str(exc)
        return False
    finally:
        globals_.update(originals)


def unreadable_authority_records_are_unavailable_not_invalid() -> bool:
    root = Path(tempfile.mkdtemp(prefix="phase8-authority-typed-read-"))
    original_open = Path.open
    try:
        (root / "state/project-config").mkdir(parents=True)
        (root / "state/pilots").mkdir(parents=True)
        (root / "state/portfolio.tsv").write_text(
            "#project\tpath\tremote\tbranch\tkey\troadmap\ttopic\tstatus\n"
            "example-ops\t/repo\torigin\tmain\t-\t-\t6\tactive\n",
            encoding="utf-8",
        )
        config_path = root / "state/project-config/example-ops.json"
        pilot_path = root / "state/pilots/pilot-example-ops.json"
        config_path.write_text(json.dumps({
            "id": "example-ops", "pilot_id": "pilot-example-ops", "topic": "6",
            "charter": "charter", "status": "active",
        }), encoding="utf-8")
        pilot_path.write_text(json.dumps({
            "id": "pilot-example-ops", "project_id": "example-ops", "topic": "6",
            "charter": "charter", "status": "active",
        }), encoding="utf-8")

        ambient = root / "ambient-portfolio.tsv"
        ambient.write_text(
            "#project\tpath\tremote\tbranch\tkey\troadmap\ttopic\tstatus\n"
            "wrong\t/other\torigin\tmain\t-\t-\t9\tactive\n",
            encoding="utf-8",
        )
        previous_registry = os.environ.get("RECURSION_PORTFOLIO")
        os.environ["RECURSION_PORTFOLIO"] = str(ambient)
        try:
            ambient_ignored = pilot_records(root, "example-ops", active=True)[0].get("id") == "example-ops"
        except SystemExit:
            ambient_ignored = False
        finally:
            if previous_registry is None:
                os.environ.pop("RECURSION_PORTFOLIO", None)
            else:
                os.environ["RECURSION_PORTFOLIO"] = previous_registry

        healthy = False
        healthy_code = 0
        try:
            healthy = pilot_records(root, "example-ops", active=True)[0].get("id") == "example-ops"
        except SystemExit as exc:
            healthy_code = int(exc.code)

        open_calls: list[Path] = []

        def denied(self, *args, **kwargs):
            if self == config_path:
                open_calls.append(self)
                raise PermissionError("simulated transient authority read failure")
            return original_open(self, *args, **kwargs)

        Path.open = denied
        unavailable = 0
        try:
            pilot_records(root, "example-ops", active=True, unavailable_code=74)
        except SystemExit as exc:
            unavailable = int(exc.code)
        finally:
            Path.open = original_open
        config_path.write_text("{malformed\n", encoding="utf-8")
        malformed = 0
        try:
            pilot_records(root, "example-ops", active=True, unavailable_code=74)
        except SystemExit as exc:
            malformed = int(exc.code)
        config_path.unlink()
        absent = 0
        try:
            pilot_records(root, "example-ops", active=True, unavailable_code=74)
        except SystemExit as exc:
            absent = int(exc.code)
        return (
            ambient_ignored and healthy and healthy_code != 74
            and unavailable == 74 and open_calls == [config_path]
            and malformed == 4 and absent == 4
        )
    finally:
        Path.open = original_open
        shutil.rmtree(root, ignore_errors=True)


def reconcile_record_io_preserves_availability_typing() -> bool:
    root = Path(tempfile.mkdtemp(prefix="phase8-reconcile-typed-read-"))
    path = root / "record.json"
    fields_path = root / "record.md"
    original_read_text = Path.read_text
    try:
        path.write_text('{"id":"record"}\n', encoding="utf-8")
        fields_path.write_text("id: record\n", encoding="utf-8")

        def denied(self, *args, **kwargs):
            if self in {path, fields_path}:
                raise PermissionError("simulated transient reconcile read failure")
            return original_read_text(self, *args, **kwargs)

        Path.read_text = denied
        unavailable = 0
        try:
            reconcile_read_json(path)
        except ReconcileError as exc:
            unavailable = exc.code
        fields_unavailable = 0
        try:
            reconcile_read_fields(fields_path)
        except ReconcileError as exc:
            fields_unavailable = exc.code
        finally:
            Path.read_text = original_read_text

        path.write_text("{malformed\n", encoding="utf-8")
        malformed = 0
        try:
            reconcile_read_json(path)
        except ReconcileError as exc:
            malformed = exc.code
        path.unlink()
        absent = 0
        try:
            reconcile_read_json(path)
        except ReconcileError as exc:
            absent = exc.code
        fields_path.unlink()
        fields_absent = 0
        try:
            reconcile_read_fields(fields_path)
        except ReconcileError as exc:
            fields_absent = exc.code
        return (
            unavailable == 74 and fields_unavailable == 74
            and malformed == 4 and absent == 4 and fields_absent == 4
        )
    finally:
        Path.read_text = original_read_text
        shutil.rmtree(root, ignore_errors=True)


def invalid_worker_authority_identity_is_rejected_before_read() -> bool:
    globals_ = admit_work.__globals__
    original = globals_["read_fields"]
    reads: list[Path] = []
    observed: list[tuple[int, str]] = []
    try:
        def forbidden(path, *_args, **_kwargs):
            reads.append(path)
            raise RuntimeError("invalid task identity reached state I/O")

        globals_["read_fields"] = forbidden
        for check in (admit_work, check_work_authority):
            try:
                check(Path("/fixture-that-must-not-be-read"), "../outside")
            except ReconcileError as exc:
                observed.append((exc.code, str(exc)))
            except RuntimeError:
                return False
        return reads == [] and observed == [
            (4, "work task identity is invalid"),
            (4, "work task identity is invalid"),
        ]
    finally:
        globals_["read_fields"] = original


def worker_authority_task_reads_preserve_availability_typing() -> bool:
    root = Path(tempfile.mkdtemp(prefix="phase8-worker-task-read-"))
    task_path = root / "state/tasks/task-example-ops.md"
    original_read_text = Path.read_text
    try:
        task_path.parent.mkdir(parents=True)
        task_path.write_text(
            "id: task-example-ops\nproject_id: example-ops\ngoal_id: goal-example-ops\n",
            encoding="utf-8",
        )

        def denied(self, *args, **kwargs):
            if self == task_path:
                raise PermissionError("simulated transient worker task read failure")
            return original_read_text(self, *args, **kwargs)

        unavailable: list[int] = []
        Path.read_text = denied
        for check in (admit_work, check_work_authority):
            try:
                check(root, "task-example-ops")
            except ReconcileError as exc:
                unavailable.append(exc.code)
        Path.read_text = original_read_text
        task_path.unlink()
        absent: list[int] = []
        for check in (admit_work, check_work_authority):
            try:
                check(root, "task-example-ops")
            except ReconcileError as exc:
                absent.append(exc.code)
        return unavailable == [74, 74] and absent == [4, 4] \
            and not (root / "state/events.jsonl").exists()
    finally:
        Path.read_text = original_read_text
        shutil.rmtree(root, ignore_errors=True)


def window_record_reads_preserve_availability_typing() -> bool:
    root = Path(tempfile.mkdtemp(prefix="phase8-window-record-read-"))
    window_path = root / "state/pilot-windows/window-example-ops.json"
    original_read_text = Path.read_text
    try:
        window_path.parent.mkdir(parents=True)
        window_path.write_text(
            json.dumps({
                "id": "window-example-ops", "project_id": "example-ops", "goal_id": "goal-example-ops",
                "status": "open", "expires_at_epoch": int(time.time()) + 300,
            }) + "\n",
            encoding="utf-8",
        )

        def denied(self, *args, **kwargs):
            if self == window_path:
                raise PermissionError("simulated transient open-window record read failure")
            return original_read_text(self, *args, **kwargs)

        Path.read_text = denied
        unavailable = 0
        try:
            open_window(root, "example-ops", "goal-example-ops", {"window_required": True})
        except ReconcileError as exc:
            unavailable = exc.code
        finally:
            Path.read_text = original_read_text
        window_path.write_text("{malformed\n", encoding="utf-8")
        invalid = 0
        try:
            open_window(root, "example-ops", "goal-example-ops", {"window_required": True})
        except ReconcileError as exc:
            invalid = exc.code
        return unavailable == 74 and invalid == 4 \
            and not (root / "state/events.jsonl").exists()
    finally:
        Path.read_text = original_read_text
        shutil.rmtree(root, ignore_errors=True)


def terminal_source_pin_receipt_io_is_unavailable() -> bool:
    globals_ = pilot_active_source_pin.__globals__
    names = ("status_files", "read_json", "goal_for", "authenticated_transition")
    originals = {name: globals_[name] for name in names}
    try:
        window = {
            "schema_version": 1, "id": "window-example-ops", "project_id": "example-ops",
            "pilot_id": "pilot-example-ops", "goal_id": "goal-example-ops", "plan_id": "plan-example-ops",
            "status": "open", "expires_at_epoch": int(time.time()) + 300,
        }
        globals_["status_files"] = lambda *_args, **_kwargs: [Path("/window-example-ops.json")]
        globals_["read_json"] = lambda *_args, **_kwargs: dict(window)
        globals_["goal_for"] = lambda *_args, **_kwargs: (
            Path("/goal-example-ops.md"),
            {
                "id": "goal-example-ops", "project_id": "example-ops", "status": "achieved",
                "last_transition_from": "verifying", "last_transition_reason": "verified",
                "version": "3",
            },
        )

        def unavailable(*_args, **_kwargs):
            raise PermissionError("simulated receipt store outage")

        globals_["authenticated_transition"] = unavailable
        try:
            pilot_active_source_pin(Path("/fixture"), "example-ops")
        except SystemExit as exc:
            return int(exc.code) == 74
        return False
    finally:
        globals_.update(originals)


def worker_admission_keeps_original_unavailability_when_audit_fails() -> bool:
    globals_ = admit_work.__globals__
    names = ("read_fields", "verify_phase8_goal_contract", "record_authority_deferral")
    originals = {name: globals_[name] for name in names}
    try:
        globals_["read_fields"] = lambda *_args, **_kwargs: {
            "project_id": "example-ops", "goal_id": "goal-example-ops",
        }

        def unavailable(*_args, **_kwargs):
            raise ReconcileError("original remote proof outage", 74)

        def audit_failed(*_args, **_kwargs):
            raise ReconcileError("secondary deferral audit outage", 74)

        globals_["verify_phase8_goal_contract"] = unavailable
        globals_["record_authority_deferral"] = audit_failed
        try:
            admit_work(Path("/fixture"), "task-example-ops")
        except ReconcileError as exc:
            return exc.code == 74 and str(exc) == "original remote proof outage"
        return False
    finally:
        globals_.update(originals)


def one_deferral_audit_outage_does_not_abort_schedule() -> bool:
    root = Path(tempfile.mkdtemp(prefix="phase8-schedule-isolation-"))
    globals_ = schedule.__globals__
    names = (
        "project_records", "parse_budget", "open_window", "record_authority_deferral",
        "snapshot_digest", "enqueue_decision",
    )
    originals = {name: globals_[name] for name in names}
    queued: list[str] = []
    deferrals: list[str] = []
    try:
        goals = root / "state/goals"
        goals.mkdir(parents=True)
        for project in ("alpha", "beta"):
            (goals / f"goal-{project}.md").write_text(
                f"id: goal-{project}\nproject_id: {project}\nstatus: active\n",
                encoding="utf-8",
            )
        globals_["project_records"] = lambda _base, project: (
            {"id": project},
            {"id": f"pilot-{project}", "concurrency_limit": 1, "window_required": True},
        )
        globals_["parse_budget"] = lambda _goal: {}

        def window(_base, project, _goal, _pilot):
            if project == "alpha":
                raise ReconcileError("simulated local window proof outage", 74)
            return {"id": "window-beta", "status": "open"}

        def audit_outage(_base, project, *_args, **_kwargs):
            deferrals.append(str(project))
            raise ReconcileError("simulated event and escalation outage", 74)

        globals_["open_window"] = window
        globals_["record_authority_deferral"] = audit_outage
        globals_["snapshot_digest"] = lambda *_args: "f" * 64
        globals_["enqueue_decision"] = lambda _base, _pilot, _verb, args, *_rest: queued.append(
            str(args[0])
        )
        diagnostic = io.StringIO()
        with contextlib.redirect_stderr(diagnostic), contextlib.redirect_stdout(io.StringIO()):
            result = schedule(root, None, None)
        return result == 0 and deferrals == ["alpha"] and queued == ["beta"] \
            and diagnostic.getvalue().count("window deferral audit unavailable") == 1
    finally:
        globals_.update(originals)
        shutil.rmtree(root, ignore_errors=True)


def one_goal_read_outage_does_not_abort_schedule() -> bool:
    root = Path(tempfile.mkdtemp(prefix="phase8-goal-read-isolation-"))
    globals_ = schedule.__globals__
    names = (
        "read_fields", "project_records", "parse_budget", "open_window", "snapshot_digest",
        "enqueue_decision",
    )
    originals = {name: globals_[name] for name in names}
    original_read_text = Path.read_text
    queued: list[str] = []
    try:
        goals = root / "state/goals"
        goals.mkdir(parents=True)
        alpha = goals / "goal-alpha.md"
        beta = goals / "goal-beta.md"
        alpha.write_text("id: goal-alpha\nproject_id: alpha\nstatus: active\n", encoding="utf-8")
        beta.write_text("id: goal-beta\nproject_id: beta\nstatus: active\n", encoding="utf-8")
        before = {alpha: alpha.read_bytes(), beta: beta.read_bytes()}
        globals_["project_records"] = lambda _base, project: (
            {"id": project},
            {"id": f"pilot-{project}", "concurrency_limit": 1, "window_required": True},
        )
        globals_["parse_budget"] = lambda _goal: {}
        globals_["open_window"] = lambda *_args: {"id": "window-beta", "status": "open"}
        globals_["snapshot_digest"] = lambda *_args: "f" * 64
        globals_["enqueue_decision"] = lambda _base, _pilot, _verb, args, *_rest: queued.append(
            str(args[0])
        )

        def deny_alpha(self, *args, **kwargs):
            if self == alpha:
                raise PermissionError("simulated transient alpha goal read outage")
            return original_read_text(self, *args, **kwargs)

        Path.read_text = deny_alpha
        diagnostic = io.StringIO()
        with contextlib.redirect_stderr(diagnostic), contextlib.redirect_stdout(io.StringIO()):
            result = schedule(root, None, None)
        Path.read_text = original_read_text
        transient_isolated = result == 0 and queued == ["beta"] \
            and all(path.read_bytes() == exact for path, exact in before.items()) \
            and not (root / "state/events.jsonl").exists() \
            and diagnostic.getvalue().count("goal record unavailable for this scheduler tick") == 1

        original_read_fields = originals["read_fields"]

        def invalid_alpha(path):
            if path == alpha:
                raise ReconcileError("proved invalid alpha goal record", 4)
            return original_read_fields(path)

        queued.clear()
        globals_["read_fields"] = invalid_alpha
        invalid_code = 0
        try:
            schedule(root, None, None)
        except ReconcileError as exc:
            invalid_code = exc.code
        return transient_isolated and invalid_code == 4 and queued == []
    finally:
        Path.read_text = original_read_text
        globals_.update(originals)
        shutil.rmtree(root, ignore_errors=True)


def fixture(exit_code: int, payload: object | None = None, *, raw: str | None = None) -> Path:
    root = Path(tempfile.mkdtemp(prefix="phase8-authority-availability-"))
    scripts = root / "scripts"
    scripts.mkdir()
    output = raw if raw is not None else json.dumps(payload) if payload is not None else "{}"
    (scripts / "project-pilot.py").write_text(
        "#!/usr/bin/env python3\n"
        f"print({output!r})\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    return root


def main() -> int:
    unavailable = fixture(74)
    invalid = fixture(4)
    crashed = fixture(1)
    argparse_mismatch = fixture(2)
    non_object = fixture(0, [])
    malformed = fixture(0, raw="{malformed")
    bad_oid = fixture(0, {
        "project_id": "example-ops", "goal_id": "goal-example-ops", "source_head": "bad",
    })
    missing_helper = Path(tempfile.mkdtemp(prefix="phase8-authority-missing-helper-"))
    (missing_helper / "scripts").mkdir()
    source = "1" * 40
    valid = fixture(0, {"project_id": "example-ops", "goal_id": "goal-example-ops", "source_head": source})
    try:
        controls = [
            (
                phase8_fresh_base(unavailable, "example-ops", "goal-example-ops")
                == (None, "", "authority-proof-unavailable"),
                "an unavailable remote proof defers without claiming immutable staleness",
            ),
            (
                phase8_fresh_base(invalid, "example-ops", "goal-example-ops")
                == (False, "", "authority-contract-stale"),
                "a proved contract failure remains terminally blockable",
            ),
            (
                all(
                    phase8_fresh_base(root, "example-ops", "goal-example-ops")
                    == (None, "", "authority-proof-unavailable")
                    for root in (
                        crashed, argparse_mismatch, non_object, malformed, bad_oid, missing_helper,
                    )
                ),
                "missing, crashed, incompatible, and malformed verifiers defer as unavailable",
            ),
            (
                phase8_fresh_base(valid, "example-ops", "goal-example-ops") == (True, source, "fresh"),
                "valid exact authority returns the measured source and never the remote tip",
            ),
            (
                work_checkpoint_routing(),
                "work.sh validates locally, freshly admits once, then uses local-only heartbeats",
            ),
            (
                repeated_deferrals_bump_one_escalation_identity(),
                "distinct proof outages journal separately while bumping one escalation identity",
            ),
            (
                final_window_revocation_is_typed_as_deferral(),
                "final window revocation remains a deferred action instead of a hard failure",
            ),
            (
                hard_refusal_survives_audit_failure(),
                "a refusal-audit outage cannot replace the proved authority diagnosis",
            ),
            (
                unreadable_authority_records_are_unavailable_not_invalid(),
                "transient authority reads defer while absence and malformed bytes stay invalid",
            ),
            (
                reconcile_record_io_preserves_availability_typing(),
                "reconcile record I/O distinguishes transient reads from invalid records",
            ),
            (
                invalid_worker_authority_identity_is_rejected_before_read(),
                "worker authority entry points reject path-shaped task identities before I/O",
            ),
            (
                worker_authority_task_reads_preserve_availability_typing(),
                "worker authority task reads preserve transient-versus-invalid typing",
            ),
            (
                window_record_reads_preserve_availability_typing(),
                "window record reads preserve transient-versus-invalid typing",
            ),
            (
                terminal_source_pin_receipt_io_is_unavailable(),
                "terminal source-pin receipt I/O remains unavailable rather than invalid",
            ),
            (
                worker_admission_keeps_original_unavailability_when_audit_fails(),
                "a secondary deferral-audit outage cannot replace worker proof unavailability",
            ),
            (
                one_deferral_audit_outage_does_not_abort_schedule(),
                "one project's deferral-audit outage cannot abort later scheduler projects",
            ),
            (
                one_goal_read_outage_does_not_abort_schedule(),
                "one transient goal read cannot abort later scheduler projects",
            ),
        ]
        failures = 0
        for passed, label in controls:
            if passed:
                print(f"ok    {label}")
            else:
                print(f"FAIL  {label}")
                failures += 1
        if failures:
            print(f"phase8-authority-availability: {failures} FAILED")
            return 1
        print("phase8-authority-availability: ALL PASS")
        return 0
    finally:
        for root in (
            unavailable, invalid, crashed, argparse_mismatch, non_object, malformed, bad_oid,
            missing_helper, valid,
        ):
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
