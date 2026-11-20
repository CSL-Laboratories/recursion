"""``recursiond provision`` and ``recursiond up``.

``provision`` is install plus the stores plus the route homes' custody root, in one JSON
object. A second call changes nothing. ``up`` starts the user unit through an injected
systemctl runner: exactly ``--user daemon-reload``, then ``--user enable --now
recursion-daemon.service``. A doctor check in state fail (other than ``daemon.socket``,
which is absent until the unit listens) refuses before any call. If the control socket
does not answer within the bound, the code is ``unconfirmed`` and never ``ok``.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import recursion_core
from recursion_core import _entrypoints as entry
from recursion_core.control.commands import setup
from recursion_core.daemon_install import UNIT_NAME, Roots

PY = "/opt/recursion/venv/bin/python3.14t"
PACKAGE = recursion_core.__file__
RELOAD = ["--user", "daemon-reload"]
ENABLE = ["--user", "enable", "--now", "recursion-daemon.service"]
GUI = "http://127.0.0.1:8765"


def _probe(**changes):
    info = {"implementation": "cpython", "version": [3, 14], "free_threaded_build": True, "package": PACKAGE}
    info.update(changes)
    return lambda interpreter: dict(info)


def _report(*fails, unknown=()):
    checks = []
    for check_id in (
        "platform.linux", "interpreter.free_threaded", "systemd.user_manager", "systemd.linger",
        "daemon.unit", "daemon.socket", "store.ledger", "store.breaker", "engine.binary",
    ):
        if check_id in fails:
            state = "fail"
        elif check_id in unknown:
            state = "unknown"
        else:
            state = "ok"
        checks.append({"id": check_id, "subject": None, "state": state, "detail": state, "fix": None})
    return {"generated_at": "2026-11-17T00:00:00.000Z", "checks": checks}


def _main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = entry.main(argv)
    return code, out.getvalue(), err.getvalue()


def _snapshot(root):
    found = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            info = os.lstat(path)
            digest = None
            if stat.S_ISREG(info.st_mode):
                with open(path, "rb") as handle:
                    digest = hashlib.sha256(handle.read()).hexdigest()
            found[os.path.relpath(path, root)] = (stat.S_IFMT(info.st_mode), info.st_ino, digest)
    return found


def _guard_systemctl():
    real = subprocess.run

    def run(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args")
        if isinstance(cmd, (list, tuple)) and cmd and os.path.basename(str(cmd[0])) == "systemctl":
            raise AssertionError(f"systemctl ran for real: {cmd}")
        return real(*args, **kwargs)

    return mock.patch("subprocess.run", run)


@unittest.skipUnless(os.name == "posix", "the installer is Linux-only")
class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="provision-")
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.join(self._tmp.name, "home")
        os.mkdir(self.home, 0o700)
        self.roots = Roots(home=self.home)
        self.unit = os.path.join(self.home, ".config", "systemd", "user", UNIT_NAME)
        self.homes = os.path.join(self.home, ".local", "state", "recursion", "homes")
        self.store_dir = os.path.join(self.home, ".local", "state", "recursion", "plugin-security")

    def run_provision(self, *, probe=None, environ=None, roots=None):
        self.assertTrue(hasattr(setup, "provision"), "provision is not implemented")
        out = io.StringIO()
        with _guard_systemctl():
            code = setup.provision(
                out, roots=self.roots if roots is None else roots, interpreter=PY, package=PACKAGE,
                probe=probe or _probe(), environ={} if environ is None else environ)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        return code, json.loads(lines[0])

    def test_recursiond_provision_is_a_command_and_prints_one_object(self):
        with mock.patch.object(entry, "_linux", return_value=True), \
                mock.patch.object(setup, "default_roots", return_value=self.roots), \
                mock.patch.object(setup, "probe_interpreter", _probe()), \
                _guard_systemctl():
            code, out, err = _main(["provision"])
        self.assertNotEqual(code, 2, f"provision is still an unknown command\n{err}\n{out}")
        self.assertEqual(err, "")
        self.assertTrue(out.endswith("\n") and not out.endswith("\n\n"), out)
        answer = json.loads(out)
        self.assertEqual(out, json.dumps(answer, sort_keys=True) + "\n")
        self.assertEqual(answer["code"], "ok")
        self.assertEqual(answer["action"], "provision")
        self.assertEqual(answer["unit"], self.unit)
        self.assertEqual(answer["homes"]["root"], self.homes)
        self.assertTrue(answer["homes"]["created"])
        self.assertEqual(
            [(row["kind"], row["created"]) for row in answer["stores"]],
            [("breaker_store", True), ("revocation_store", True)])
        self.assertEqual(answer["next"], [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", UNIT_NAME]])
        self.assertNotIn("--now", json.dumps(answer["next"]))

    def test_provision_twice_changes_nothing(self):
        code, first = self.run_provision()
        self.assertEqual(code, 0)
        self.assertTrue(first["changed"])
        self.assertTrue(first["homes"]["created"])
        self.assertTrue(all(row["created"] for row in first["stores"]))
        shot = _snapshot(self.home)
        code, second = self.run_provision()
        self.assertEqual(code, 0)
        self.assertFalse(second["changed"])
        self.assertFalse(second["homes"]["created"])
        self.assertTrue(all(row["created"] is False for row in second["stores"]))
        self.assertEqual(second["homes"]["root"], first["homes"]["root"])
        self.assertEqual(second["unit"], first["unit"])
        self.assertEqual(_snapshot(self.home), shot)
        with open(self.unit, encoding="utf-8") as handle:
            self.assertIn("recursion_core.daemon", handle.read())

    def test_provision_after_install_changes_nothing(self):
        out = io.StringIO()
        code = setup.run("install", out=out, roots=self.roots, interpreter=PY, package=PACKAGE,
                         probe=_probe(), environ={})
        self.assertEqual(code, 0)
        shot = _snapshot(self.home)
        code, answer = self.run_provision()
        self.assertEqual((code, answer["changed"], answer["homes"]["created"]), (0, False, False))
        self.assertTrue(all(row["created"] is False for row in answer["stores"]))
        self.assertEqual(_snapshot(self.home), shot)

    def test_a_second_home_is_provisioned_on_its_own(self):
        code, first = self.run_provision()
        self.assertEqual(code, 0)
        other_root = os.path.join(self._tmp.name, "other")
        os.mkdir(other_root, 0o700)
        other = Roots(home=other_root)
        code, second = self.run_provision(roots=other)
        self.assertEqual(code, 0)
        self.assertTrue(second["changed"])
        self.assertNotEqual(second["unit"], first["unit"])
        self.assertTrue(os.path.isfile(first["unit"]))
        self.assertTrue(os.path.isfile(second["unit"]))
        self.assertNotEqual(os.path.realpath(first["homes"]["root"]), os.path.realpath(second["homes"]["root"]))

    def test_a_bad_interpreter_refuses_and_writes_nothing(self):
        code, answer = self.run_provision(probe=_probe(free_threaded_build=False))
        self.assertEqual((code, answer["code"]), (1, "interpreter_not_free_threaded"))
        self.assertNotEqual(answer["code"], "ok")
        self.assertEqual(os.listdir(self.home), [])

    def test_a_state_home_elsewhere_refuses_before_anything_is_written(self):
        code, answer = self.run_provision(environ={"XDG_STATE_HOME": "/var/tmp/not-the-default-state"})
        self.assertEqual((code, answer["code"]), (1, "state_home_elsewhere"))
        self.assertIn("recursiond provision", answer["detail"])
        self.assertEqual(os.listdir(self.home), [])

    def test_a_filesystem_error_is_a_refusal(self):
        with mock.patch.object(setup, "install", side_effect=OSError(28, "No space left on device")):
            code, answer = self.run_provision()
        self.assertEqual(code, 1)
        self.assertEqual(answer["code"], "filesystem_error")
        self.assertNotEqual(answer["code"], "ok")
        self.assertIn("OSError", answer["detail"])

    def test_an_interruption_propagates(self):
        def boom(_interpreter):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.run_provision(probe=boom)

        def leave(_interpreter):
            raise SystemExit(3)

        with self.assertRaises(SystemExit):
            self.run_provision(probe=leave)

    def test_off_linux_provision_refuses_before_it_writes(self):
        with mock.patch.object(entry, "_linux", return_value=False), \
                mock.patch.object(setup, "provision") as provision:
            code, out, _ = _main(["provision"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["code"], "platform_unsupported")
        provision.assert_not_called()

    def test_help_lists_provision(self):
        code, out, _ = _main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("provision", out)


class UpTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="up-sock-")
        self.addCleanup(self._tmp.cleanup)
        self.runtime = self._tmp.name
        self.calls = []

    def _runner(self, args):
        self.calls.append(list(args))
        return 0

    def _up(self, *, report, runner=None, bound_s=5.0, socket_wait=None, environ=None):
        import recursion_core.setup.up as up

        out = io.StringIO()
        kwargs = {
            "out": out,
            "runner": self._runner if runner is None else runner,
            "report": report,
            "bound_s": bound_s,
            "environ": {"XDG_RUNTIME_DIR": self.runtime} if environ is None else environ,
        }
        if socket_wait is not None:
            kwargs["socket_wait"] = socket_wait
        with _guard_systemctl():
            code = up.command(**kwargs)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        return code, json.loads(lines[0])

    def test_an_injected_runner_reloads_then_enables_now(self):
        seen = {}

        def socket_wait(path, bound):
            seen["path"] = path
            seen["bound"] = bound
            seen["calls"] = list(self.calls)
            return True

        code, answer = self._up(report=_report(), bound_s=5, socket_wait=socket_wait)
        self.assertEqual(code, 0)
        self.assertEqual(answer, {"code": "ok", "gui": GUI})
        self.assertEqual(self.calls, [RELOAD, ENABLE])
        self.assertEqual(seen["calls"], [RELOAD, ENABLE])
        self.assertEqual(seen["bound"], 5)
        self.assertTrue(seen["path"].endswith("/recursion/control.sock"), seen["path"])

    def test_a_doctor_fail_refuses_and_calls_nothing(self):
        code, answer = self._up(report=_report("daemon.unit", "store.breaker"), socket_wait=lambda *_: True)
        self.assertEqual(code, 1)
        self.assertEqual(answer["code"], "doctor_failed")
        self.assertNotEqual(answer["code"], "ok")
        self.assertEqual(answer["failed"], ["daemon.unit", "store.breaker"])
        self.assertNotIn("gui", answer)
        self.assertEqual(self.calls, [])

    def test_daemon_socket_fail_does_not_refuse_the_start(self):
        # The socket check is fail until the unit is listening. That is what the wait is for.
        code, answer = self._up(report=_report("daemon.socket"), socket_wait=lambda *_: True)
        self.assertEqual((code, answer["code"]), (0, "ok"))
        self.assertEqual(self.calls, [RELOAD, ENABLE])
        self.assertEqual(answer["gui"], GUI)

    def test_unknown_does_not_refuse(self):
        code, answer = self._up(report=_report(unknown=("systemd.linger", "engine.binary")),
                                socket_wait=lambda *_: True)
        self.assertEqual((code, answer["code"]), (0, "ok"))
        self.assertEqual(self.calls, [RELOAD, ENABLE])

    def test_a_silent_socket_is_unconfirmed_and_never_ok(self):
        started = time.monotonic()
        code, answer = self._up(report=_report(), bound_s=0.2)
        elapsed = time.monotonic() - started
        self.assertEqual(code, 1)
        self.assertEqual(answer["code"], "unconfirmed")
        self.assertNotEqual(answer["code"], "ok")
        self.assertNotIn("gui", answer)
        self.assertIn("control.sock", answer["socket"])
        self.assertIn("0.2", answer["detail"])
        self.assertEqual(self.calls, [RELOAD, ENABLE])
        self.assertLess(elapsed, 2.0)
        self.assertGreaterEqual(elapsed, 0.15)

    def test_a_socket_that_answers_within_the_bound_is_ok(self):
        path_holder = {}

        def runner(args):
            self.calls.append(list(args))
            if list(args) == ENABLE:
                import recursion_core.setup.up as up
                path = up.socket_path({"XDG_RUNTIME_DIR": self.runtime})
                os.makedirs(os.path.dirname(path), 0o700)
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                listener.bind(path)
                listener.listen(1)
                self.addCleanup(listener.close)
                path_holder["path"] = path
            return 0

        code, answer = self._up(report=_report(), bound_s=2, runner=runner)
        self.assertEqual((code, answer["code"], answer["gui"]), (0, "ok", GUI))
        self.assertEqual(self.calls, [RELOAD, ENABLE])
        self.assertTrue(os.path.exists(path_holder["path"]))

    def test_a_failing_runner_stops_and_is_not_ok(self):
        def runner(args):
            self.calls.append(list(args))
            return 1

        code, answer = self._up(report=_report(), runner=runner, socket_wait=lambda *_: True)
        self.assertEqual(code, 1)
        self.assertEqual(answer["code"], "systemctl_failed")
        self.assertNotEqual(answer["code"], "ok")
        self.assertEqual(self.calls, [RELOAD])
        self.assertEqual(answer["command"], RELOAD)

    def test_a_runner_oserror_is_a_refusal_and_an_interruption_propagates(self):
        def broken(_args):
            raise OSError("systemctl is not on PATH")

        code, answer = self._up(report=_report(), runner=broken, socket_wait=lambda *_: True)
        self.assertEqual(answer["code"], "systemctl_failed")
        self.assertIn("OSError", answer["detail"])
        self.assertNotEqual(answer["code"], "ok")

        def interrupt(_args):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self._up(report=_report(), runner=interrupt, socket_wait=lambda *_: True)

        def leave(_args):
            raise SystemExit(4)

        with self.assertRaises(SystemExit):
            self._up(report=_report(), runner=leave)

    def test_a_report_that_is_not_a_report_refuses(self):
        code, answer = self._up(report={"checks": "no"}, socket_wait=lambda *_: True)
        self.assertEqual((code, answer["code"]), (1, "doctor_failed"))
        self.assertEqual(self.calls, [])

    def test_a_second_caller_gets_its_own_answer(self):
        first_code, first = self._up(report=_report(), socket_wait=lambda *_: True)
        second_code, second = self._up(report=_report("platform.linux"), socket_wait=lambda *_: True)
        self.assertIsNot(first, second)
        self.assertEqual((first_code, first["code"]), (0, "ok"))
        self.assertEqual((second_code, second["code"]), (1, "doctor_failed"))
        self.assertEqual(self.calls, [RELOAD, ENABLE])

    def test_no_injected_report_reads_doctor_and_refuses_its_fail(self):
        import recursion_core.setup.doctor as doctor
        import recursion_core.setup.up as up

        with mock.patch.object(doctor, "run", return_value=_report("interpreter.free_threaded")) as run:
            out = io.StringIO()
            with _guard_systemctl():
                code = up.command(out=out, runner=self._runner, report=None, socket_wait=lambda *_: True,
                                  environ={"XDG_RUNTIME_DIR": self.runtime})
        run.assert_called_once_with(None)
        answer = json.loads(out.getvalue())
        self.assertEqual((code, answer["code"]), (1, "doctor_failed"))
        self.assertEqual(self.calls, [])

    def test_the_default_runner_is_systemctl_with_those_arguments_only(self):
        import recursion_core.setup.up as up

        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0)
            self.assertEqual(up.run_systemctl(RELOAD), 0)
            self.assertEqual(up.run_systemctl(ENABLE), 0)
            self.assertEqual(
                [call.args[0] for call in run.call_args_list],
                [["systemctl", *RELOAD], ["systemctl", *ENABLE]])
        with mock.patch("subprocess.run") as run:
            with self.assertRaises(ValueError):
                up.run_systemctl(["--user", "reboot"])
            run.assert_not_called()

    def test_recursiond_up_is_a_command_and_refuses_off_linux(self):
        import recursion_core.setup.up as up

        with mock.patch.object(entry, "_linux", return_value=True), \
                mock.patch.object(up, "command", return_value=0) as command:
            code, out, err = _main(["up"])
        self.assertNotEqual(code, 2, err or out)
        self.assertEqual((code, err), (0, ""))
        command.assert_called_once()
        with mock.patch.object(entry, "_linux", return_value=False), \
                mock.patch.object(up, "command") as command:
            code, out, _ = _main(["up"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["code"], "platform_unsupported")
        command.assert_not_called()

    def test_help_lists_up(self):
        code, out, _ = _main(["--help"])
        self.assertEqual(code, 0)
        # A command token, not the letters inside "upgrade".
        self.assertIn("up", out.replace("upgrade", ""))

    def test_up_itself_refuses_off_linux_before_the_runner(self):
        import recursion_core.setup.up as up

        with mock.patch.object(up.sys, "platform", "darwin"):
            out = io.StringIO()
            code = up.command(out=out, runner=self._runner, report=_report(), socket_wait=lambda *_: True)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["code"], "platform_unsupported")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
