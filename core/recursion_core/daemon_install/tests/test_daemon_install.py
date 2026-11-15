"""The per-user unit: install, upgrade, uninstall. No systemctl, no network; files under a temporary home."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from recursion_core.daemon_install import layout
from recursion_core.daemon_install import (
    MARKER,
    STOP_TIMEOUT_S,
    UNIT_NAME,
    InstallRefused,
    Roots,
    daemon_exec_args,
    install,
    leftovers,
    plan_commands,
    render_unit,
    service_fields,
    uninstall,
    upgrade,
)

PY = "/opt/recursion/venv/bin/python3.14t"
PY_NEXT = "/opt/recursion/venv-2/bin/python3.14t"
EXEC = daemon_exec_args(PY)
EXEC_NEXT = daemon_exec_args(PY_NEXT)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _tree(root: str) -> dict[str, tuple]:
    """Every path under ``root`` with its type, mode and (for files) bytes; links by target."""
    found = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                found[path] = ("link", os.readlink(path))
            elif stat.S_ISDIR(info.st_mode):
                found[path] = ("dir", stat.S_IMODE(info.st_mode))
            else:
                with open(path, "rb") as handle:
                    found[path] = ("file", stat.S_IMODE(info.st_mode), handle.read())
    return found


class RenderTests(unittest.TestCase):
    def test_the_unit_restarts_on_failure_starts_with_the_user_manager_and_has_a_finite_stop(self) -> None:
        text = render_unit(EXEC)
        self.assertEqual(text.splitlines()[0], MARKER)
        fields = service_fields(text)
        self.assertEqual(fields["Service.Type"], "simple")
        self.assertEqual(fields["Service.ExecStart"], f"{PY} -I -X gil=0 -m recursion_core.daemon")
        self.assertEqual(fields["Service.Restart"], "on-failure")
        self.assertEqual(fields["Service.RestartSec"], "2")
        self.assertEqual(fields["Service.UMask"], "0077")
        self.assertEqual(fields["Service.KillMode"], "control-group")
        self.assertEqual(fields["Service.TimeoutStopSec"], str(STOP_TIMEOUT_S))
        self.assertEqual(fields["Service.SendSIGKILL"], "yes")
        self.assertEqual(fields["Service.UnsetEnvironment"].split(), ["PYTHONPATH", "PYTHONHOME"])
        self.assertEqual(fields["Install.WantedBy"], "default.target")
        self.assertNotIn("Service.Environment", fields)
        self.assertNotIn("Service.ExecStartPre", fields)
        self.assertTrue(0 < STOP_TIMEOUT_S < 3600)

    def test_the_daemon_runs_isolated_with_the_gil_off_under_the_interpreter_as_given(self) -> None:
        self.assertEqual(daemon_exec_args(PY), (PY, "-I", "-X", "gil=0", "-m", "recursion_core.daemon"))
        for bad in ("python3.14t", "bin/python", b"/usr/bin/python3", None):
            with self.assertRaises(InstallRefused) as caught:
                daemon_exec_args(bad)  # type: ignore[arg-type]
            self.assertEqual(caught.exception.kind, "exec_arg")

    def test_exec_args_are_closed(self) -> None:
        for bad in (
            (),
            ("python3", "-m", "recursion_core.daemon"),
            ("%h/bin/python3", "-m", "recursion_core.daemon;id"),
            ("%h/../../bin/python3",),
            ("/opt/py thon/bin/python3",),
            ("%h/bin/python3", "has space"),
            ("/bin/python3", "a" * 300),
            tuple(["/bin/python3"] * 17),
            ["%h/bin/python3"],
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(InstallRefused) as caught:
                    render_unit(bad)  # type: ignore[arg-type]
                self.assertIn(caught.exception.kind, {"exec_args", "exec_arg"})

    def test_a_literal_percent_in_the_interpreter_is_written_as_systemds_literal_percent(self) -> None:
        # systemd reads % in ExecStart as a specifier; %% is its literal % (review 1: Astra R3, Sol R2).
        for path, written in (("/opt/release%Q/bin/python3.14t", "/opt/release%%Q/bin/python3.14t"),
                              ("/opt/a%h/bin/python3.14t", "/opt/a%%h/bin/python3.14t"),
                              ("/opt/a%%b/python3.14t", "/opt/a%%%%b/python3.14t")):
            with self.subTest(path=path):
                self.assertEqual(daemon_exec_args(path)[0], written)
                self.assertEqual(service_fields(render_unit(daemon_exec_args(path)))["Service.ExecStart"].split()[0],
                                 written)

    @unittest.skipUnless(shutil.which("systemd-analyze"), "needs systemd-analyze")
    def test_systemd_reads_the_interpreter_path_back_exactly(self) -> None:
        # systemd's own unit reader, offline: no manager is contacted and nothing is started.
        with tempfile.TemporaryDirectory(prefix="unit-verify-") as root:
            for name in ("basic.target", "shutdown.target", "exit.target"):
                with open(os.path.join(root, name), "w", encoding="utf-8") as handle:
                    handle.write("[Unit]\nDescription=offline verify\nDefaultDependencies=no\n")
            results = {}
            for name in ("python-plain", "python%t"):
                interpreter = os.path.join(root, name)
                with open(interpreter, "w", encoding="utf-8") as handle:
                    handle.write("#!/bin/sh\nexit 0\n")
                os.chmod(interpreter, 0o700)
                unit = os.path.join(root, UNIT_NAME)
                with open(unit, "w", encoding="utf-8") as handle:
                    handle.write(render_unit(daemon_exec_args(interpreter)))
                env = dict(os.environ, SYSTEMD_UNIT_PATH=root, SYSTEMD_LOG_COLOR="0",
                           XDG_RUNTIME_DIR=os.environ.get("XDG_RUNTIME_DIR") or root)
                done = subprocess.run(["systemd-analyze", "--user", "--man=no", "verify", unit], env=env,
                                      capture_output=True, text=True, timeout=60, check=False)
                results[name] = (done.returncode, done.stderr.strip())
            if results["python-plain"][0] != 0:
                self.skipTest(f"systemd-analyze cannot verify a unit here: {results['python-plain'][1]}")
            self.assertEqual(results["python%t"], (0, ""))

    def test_the_reader_keeps_the_last_value_and_refuses_a_line_outside_a_section(self) -> None:
        self.assertEqual(service_fields("[S]\nA=1\nA=2\n; c\n# c\n"), {"S.A": "2"})
        for bad in ("A=1\n", "[S]\nno equals sign\n", None):
            with self.assertRaises(InstallRefused):
                service_fields(bad)  # type: ignore[arg-type]


class CommandTests(unittest.TestCase):
    def test_plans_are_user_manager_only(self) -> None:
        for action in ("install", "upgrade", "uninstall"):
            for command in plan_commands(action):
                self.assertEqual(command[:2], ("systemctl", "--user"))
                self.assertNotIn("sudo", command)
                self.assertNotIn("--system", command)
        self.assertEqual(plan_commands("install"), (("systemctl", "--user", "daemon-reload"),
                                                    ("systemctl", "--user", "enable", UNIT_NAME)))
        self.assertEqual(plan_commands("upgrade")[1], ("systemctl", "--user", "try-restart", UNIT_NAME))
        self.assertEqual(plan_commands("uninstall"), (("systemctl", "--user", "stop", UNIT_NAME),
                                                      ("systemctl", "--user", "daemon-reload")))
        with self.assertRaises(InstallRefused):
            plan_commands("purge")

    def test_package_does_not_import_a_process_or_network_module(self) -> None:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        banned = ("subprocess", "urllib", "socket", "ctypes", "http.client")
        for dirpath, _, filenames in os.walk(root):
            if dirpath.endswith(os.sep + "tests"):
                continue
            for name in filenames:
                if name.endswith(".py"):
                    text = _read(os.path.join(dirpath, name))
                    for needle in banned:
                        self.assertNotIn(f"import {needle}", text)
                        self.assertNotIn(f"from {needle}", text)


@unittest.skipUnless(os.name == "posix", "the installer is Linux-only")
class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="install-")
        self.addCleanup(self._tmp.cleanup)
        self.case = self._tmp.name
        self.home = os.path.join(self.case, "home")
        os.mkdir(self.home, 0o700)
        self.roots = Roots(home=self.home)
        self.unit_dir = os.path.join(self.home, ".config", "systemd", "user")
        self.unit = os.path.join(self.unit_dir, UNIT_NAME)
        # The daemon's state folder: the installer never writes, reads or removes anything in it.
        self.state = os.path.join(self.home, ".local", "state", "recursion")
        os.makedirs(self.state, 0o700)
        with open(os.path.join(self.state, "runs.sqlite3"), "wb") as handle:
            handle.write(b"rows-v1")

    def test_install_writes_the_unit_and_install_twice_equals_install_once(self) -> None:
        state_before = _tree(self.state)
        plan = install(self.roots, EXEC)
        self.assertEqual((plan.action, plan.changed, plan.unit), ("install", True, self.unit))
        self.assertEqual(plan.commands, plan_commands("install"))
        self.assertEqual(_read(self.unit), render_unit(EXEC))
        self.assertEqual(stat.S_IMODE(os.stat(self.unit).st_mode), 0o644)
        once = _tree(self.home)
        again = install(self.roots, EXEC)
        self.assertEqual((again.action, again.changed, again.unit), ("install", False, self.unit))
        self.assertEqual(_tree(self.home), once)
        self.assertEqual(_tree(self.state), state_before)
        self.assertEqual(sorted(os.listdir(self.unit_dir)), [UNIT_NAME])  # no temporary file left

    def test_a_new_unit_folder_is_owner_only_and_an_existing_one_is_left_as_it_is(self) -> None:
        install(self.roots, EXEC)
        for path in (os.path.join(self.home, ".config"), os.path.dirname(self.unit_dir), self.unit_dir):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700, path)
        other = os.path.join(self.case, "other-home")
        os.makedirs(os.path.join(other, ".config"), 0o755)
        os.chmod(os.path.join(other, ".config"), 0o755)
        install(Roots(other), EXEC)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(other, ".config")).st_mode), 0o755)

    def test_install_refuses_a_different_existing_unit_and_leaves_it(self) -> None:
        install(self.roots, EXEC)
        before = _tree(self.home)
        with self.assertRaises(InstallRefused) as caught:
            install(self.roots, EXEC_NEXT)
        self.assertEqual(caught.exception.kind, "unit_differs")
        self.assertIn("recursiond upgrade", caught.exception.detail)
        self.assertEqual(_tree(self.home), before)

    def test_upgrade_rewrites_the_unit_and_changes_nothing_when_it_is_current(self) -> None:
        install(self.roots, EXEC)
        state_before = _tree(self.state)
        plan = upgrade(self.roots, EXEC_NEXT)
        self.assertEqual((plan.action, plan.changed), ("upgrade", True))
        self.assertEqual(plan.commands, plan_commands("upgrade"))
        self.assertIn(PY_NEXT, _read(self.unit))
        self.assertEqual(stat.S_IMODE(os.stat(self.unit).st_mode), 0o644)
        current = _tree(self.home)
        self.assertFalse(upgrade(self.roots, EXEC_NEXT).changed)
        self.assertEqual(_tree(self.home), current)
        self.assertEqual(_tree(self.state), state_before)
        self.assertEqual(sorted(os.listdir(self.unit_dir)), [UNIT_NAME])

    def test_upgrade_replaces_a_hand_placed_unit(self) -> None:
        os.makedirs(self.unit_dir, 0o700)
        with open(self.unit, "w", encoding="utf-8") as handle:
            handle.write("[Service]\nEnvironment=PYTHONPATH=%h/.local/lib/recursion/core\nExecStart=/old\n")
        self.assertTrue(upgrade(self.roots, EXEC).changed)
        self.assertEqual(_read(self.unit), render_unit(EXEC))

    def test_upgrade_without_a_unit_is_refused(self) -> None:
        with self.assertRaises(InstallRefused) as caught:
            upgrade(self.roots, EXEC)
        self.assertEqual(caught.exception.kind, "unit_missing")
        self.assertFalse(os.path.lexists(self.unit_dir))

    def test_an_existing_unit_another_user_could_change_is_refused_by_install_and_rewritten_by_upgrade(self) -> None:
        # Review 3 of #382, DeepSeek L1: the same bytes are not enough. A unit group or world can
        # write, or another user owns, is refused by install (unit_untrusted, left as it is); upgrade
        # writes it again, ours and 0644. Owner-only modes are fine: only another user's write counts.
        install(self.roots, EXEC)
        for mode in (0o664, 0o646, 0o666, 0o620, 0o602):
            with self.subTest(mode=oct(mode)):
                os.chmod(self.unit, mode)
                before = _tree(self.home)
                with self.assertRaises(InstallRefused) as caught:
                    install(self.roots, EXEC)
                self.assertEqual(caught.exception.kind, "unit_untrusted")
                self.assertEqual(caught.exception.detail, f"{self.unit} can be written by group or world; "
                                 "`recursiond upgrade` writes it again, ours and 0644")
                self.assertEqual(_tree(self.home), before)
                plan = upgrade(self.roots, EXEC)
                self.assertEqual((plan.action, plan.changed), ("upgrade", True))
                self.assertEqual(stat.S_IMODE(os.stat(self.unit).st_mode), 0o644)
                self.assertEqual(_read(self.unit), render_unit(EXEC))
                self.assertFalse(install(self.roots, EXEC).changed)
                self.assertEqual(sorted(os.listdir(self.unit_dir)), [UNIT_NAME])
        real_fstat, inode = os.fstat, os.stat(self.unit).st_ino

        def other_owner(fd, *args, **kwargs):  # the owner is read from the descriptor the unit was read through
            info = real_fstat(fd, *args, **kwargs)
            if info.st_ino == inode:
                fields = list(info[:10])
                fields[4] = info.st_uid + 1
                return os.stat_result(fields)
            return info
        with mock.patch("os.fstat", side_effect=other_owner):
            with self.assertRaises(InstallRefused) as caught:
                install(self.roots, EXEC)
            self.assertEqual(caught.exception.kind, "unit_untrusted")
            self.assertIn(f"{self.unit} is owned by another user", caught.exception.detail)
            self.assertTrue(upgrade(self.roots, EXEC).changed)
        for mode in (0o600, 0o644, 0o640, 0o604):
            with self.subTest(mode=oct(mode)):
                os.chmod(self.unit, mode)
                self.assertFalse(install(self.roots, EXEC).changed)
                self.assertFalse(upgrade(self.roots, EXEC).changed)
                self.assertEqual(stat.S_IMODE(os.stat(self.unit).st_mode), mode)

    def test_a_unit_removed_or_replaced_while_it_is_read_is_refused_not_answered(self) -> None:
        # Review 4 of #382, DeepSeek L4-1, and review 5, M5-1: a concurrent uninstall removes the unit while
        # install or upgrade reads it. Neither raises nor answers for a file that is gone: the path must still
        # name the file read (its device and inode), or the command refuses and writes nothing.
        real_read = os.read

        def at_the_end(change):
            def read(fd, size):
                data = real_read(fd, size)
                if not data and os.path.lexists(self.unit):
                    change()
                return data
            return read

        def replace_it():
            os.unlink(self.unit)
            with open(self.unit, "wb") as handle:
                handle.write(render_unit(EXEC).encode())  # the same bytes, another file

        for change, code in ((lambda: os.unlink(self.unit), "unit_missing"), (replace_it, "unit_changed")):
            for step in (install, upgrade):
                with self.subTest(step=step.__name__, code=code):
                    if os.path.lexists(self.unit):
                        os.unlink(self.unit)
                    install(self.roots, EXEC)
                    with self.assertRaises(InstallRefused) as caught, mock.patch("os.read", side_effect=at_the_end(change)):
                        step(self.roots, EXEC)
                    self.assertEqual(caught.exception.kind, code)
                    self.assertIn(self.unit, caught.exception.detail)
        if os.path.lexists(self.unit):
            os.unlink(self.unit)
        install(self.roots, EXEC)
        os.chmod(self.unit, 0o666)
        with self.assertRaises(InstallRefused) as caught, mock.patch("os.read", side_effect=at_the_end(lambda: os.unlink(self.unit))):
            install(self.roots, EXEC)  # removed: no answer about the file that was read
        self.assertEqual(caught.exception.kind, "unit_missing")

    def test_a_unit_removed_before_it_is_opened_is_missing_not_of_the_wrong_type(self) -> None:
        # Review 5 of #382, DeepSeek L5-1: removed between the lexists and the open. install and upgrade
        # refuse unit_missing (not not_regular); uninstall finds nothing left to remove.
        real_open = os.open

        def remove_then_open(path, flags, *args, **kwargs):
            if os.fspath(path) == self.unit and os.path.lexists(self.unit):
                os.unlink(self.unit)
            return real_open(path, flags, *args, **kwargs)
        for step in (install, upgrade):
            with self.subTest(step=step.__name__):
                if not os.path.lexists(self.unit):
                    install(self.roots, EXEC)
                with self.assertRaises(InstallRefused) as caught, mock.patch("os.open", side_effect=remove_then_open):
                    step(self.roots, EXEC)
                self.assertEqual(caught.exception.kind, "unit_missing")
                self.assertEqual(caught.exception.detail, f"{self.unit} is not there; run the command again")
        install(self.roots, EXEC)
        with mock.patch("os.open", side_effect=remove_then_open):
            plan = uninstall(self.roots)
        self.assertEqual((plan.action, plan.changed), ("uninstall", False))
        self.assertFalse(os.path.lexists(self.unit))
        with open(self.unit, "w", encoding="utf-8") as handle:
            handle.write("[Service]\nExecStart=/bin/true\n")  # another's unit is still left in place
        with self.assertRaises(InstallRefused) as caught:
            uninstall(self.roots)
        self.assertEqual(caught.exception.kind, "unit_not_ours")

    def test_the_unit_is_looked_up_again_while_it_is_still_open(self) -> None:
        # Review 6 of #382, DeepSeek M6-1 and L6-2: looked up after its descriptor is closed, a unit removed and
        # made again in that window can take the freed inode number (189 of 200 in the read) and pass as the
        # file read. So the path is compared while the descriptor is open (the inode cannot be reused then),
        # and uninstall removes the unit while it still holds the file it read.
        real_open, real_close, real_lstat, real_unlink = os.open, os.close, os.lstat, os.unlink
        events, unit_fds = [], set()

        def opening(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if os.fspath(path) == self.unit:
                unit_fds.add(fd)
                events.append("open")
            return fd

        def closing(fd):
            if fd in unit_fds:
                unit_fds.discard(fd)
                events.append("close")
            return real_close(fd)

        def looking(path, *args, **kwargs):
            if os.fspath(path) == self.unit:
                events.append("lstat")
            return real_lstat(path, *args, **kwargs)

        def unlinking(path, *args, **kwargs):
            if os.fspath(path) == self.unit:
                events.append("unlink")
            return real_unlink(path, *args, **kwargs)
        install(self.roots, EXEC)
        for name, step in (("install", lambda: install(self.roots, EXEC)), ("upgrade", lambda: upgrade(self.roots, EXEC)),
                           ("uninstall", lambda: uninstall(self.roots))):
            with self.subTest(step=name):
                events.clear()
                with mock.patch("os.open", side_effect=opening), mock.patch("os.close", side_effect=closing), \
                        mock.patch("os.lstat", side_effect=looking), mock.patch("os.unlink", side_effect=unlinking):
                    step()
                opened, closed = events.index("open"), events.index("close")
                self.assertIn("lstat", events[opened:closed], events)
        self.assertLess(events.index("unlink"), events.index("close"), events)  # uninstall, the last step
        self.assertFalse(os.path.lexists(self.unit))

    def test_uninstall_refuses_a_unit_replaced_while_it_is_read_and_leaves_it(self) -> None:
        # Review 6 of #382, DeepSeek L6-2: uninstall removed by name a file it had not checked.
        install(self.roots, EXEC)
        real_read = os.read

        def replace_at_the_end(fd, size):
            data = real_read(fd, size)
            if not data and os.path.lexists(self.unit):
                os.unlink(self.unit)
                with open(self.unit, "w", encoding="utf-8") as handle:
                    handle.write("[Service]\nExecStart=/bin/true\n")
            return data
        with self.assertRaises(InstallRefused) as caught, mock.patch("os.read", side_effect=replace_at_the_end):
            uninstall(self.roots)
        self.assertEqual(caught.exception.kind, "unit_changed")
        self.assertEqual(_read(self.unit), "[Service]\nExecStart=/bin/true\n")  # another's file is left as it is

    def test_a_unit_gone_before_it_is_replaced_is_missing_not_of_the_wrong_type(self) -> None:
        # Review 6 of #382, DeepSeek L6-1: upgrade's write found the unit gone and answered not_regular.
        os.makedirs(self.unit_dir, 0o700)
        with self.assertRaises(InstallRefused) as caught:
            layout.replace_regular(self.unit, b"x", 0o644)
        self.assertEqual(caught.exception.kind, "unit_missing")
        self.assertFalse(os.path.lexists(self.unit))
        os.mkdir(self.unit)  # something there that is not a file is still of the wrong type
        with self.assertRaises(InstallRefused) as caught:
            layout.replace_regular(self.unit, b"x", 0o644)
        self.assertEqual(caught.exception.kind, "not_regular")

    def test_uninstall_removes_the_unit_and_nothing_else(self) -> None:
        before = _tree(self.home)
        install(self.roots, EXEC)
        neighbour = os.path.join(self.unit_dir, "other.service")
        with open(neighbour, "w", encoding="utf-8") as handle:
            handle.write("[Service]\nExecStart=/bin/true\n")
        self.assertEqual(leftovers(self.roots), (self.unit,))
        plan = uninstall(self.roots)
        self.assertEqual((plan.action, plan.changed), ("uninstall", True))
        self.assertEqual(plan.commands, plan_commands("uninstall"))
        self.assertEqual(leftovers(self.roots), ())
        after = _tree(self.home)
        self.assertEqual(set(after) - set(before),
                         {os.path.join(self.home, ".config"), os.path.dirname(self.unit_dir), self.unit_dir, neighbour})
        self.assertEqual({path: after[path] for path in before}, before)  # the state folder too
        again = uninstall(self.roots)
        self.assertEqual((again.action, again.changed), ("uninstall", False))

    def test_uninstall_refuses_while_the_unit_is_enabled(self) -> None:
        install(self.roots, EXEC)
        wants = os.path.join(self.unit_dir, "default.target.wants")
        os.mkdir(wants, 0o755)
        os.symlink(self.unit, os.path.join(wants, UNIT_NAME))
        with self.assertRaises(InstallRefused) as caught:
            uninstall(self.roots)
        self.assertEqual(caught.exception.kind, "unit_enabled")
        self.assertIn("disable --now", caught.exception.detail)
        self.assertTrue(os.path.isfile(self.unit))

    def test_uninstall_leaves_a_unit_it_did_not_write(self) -> None:
        os.makedirs(self.unit_dir, 0o700)
        with open(self.unit, "w", encoding="utf-8") as handle:
            handle.write("# hand placed\n[Service]\nExecStart=/old\n")
        with self.assertRaises(InstallRefused) as caught:
            uninstall(self.roots)
        self.assertEqual(caught.exception.kind, "unit_not_ours")
        self.assertTrue(os.path.isfile(self.unit))

    def test_a_link_in_the_unit_path_is_refused_not_followed(self) -> None:
        outside = os.path.join(self.case, "outside")
        os.mkdir(outside, 0o700)
        os.makedirs(os.path.join(self.home, ".config"), 0o700)
        os.symlink(outside, os.path.join(self.home, ".config", "systemd"))
        for call in (lambda: install(self.roots, EXEC), lambda: upgrade(self.roots, EXEC),
                     lambda: uninstall(self.roots)):
            with self.assertRaises(InstallRefused) as caught:
                call()
            self.assertEqual(caught.exception.kind, "symlink_refused")
        self.assertEqual(os.listdir(outside), [])

    def test_the_unit_mode_is_exact_under_a_restrictive_umask(self) -> None:
        # The new file's mode is set, not left to the umask (review 1, R6: M10).
        old = os.umask(0o077)
        try:
            install(self.roots, EXEC)
            self.assertEqual(stat.S_IMODE(os.stat(self.unit).st_mode), 0o644)
            upgrade(self.roots, EXEC_NEXT)
            self.assertEqual(stat.S_IMODE(os.stat(self.unit).st_mode), 0o644)
        finally:
            os.umask(old)

    def test_a_unit_folder_another_user_owns_or_can_write_is_refused(self) -> None:
        # Another user could put a unit there or take ours away (review 1, DeepSeek F12).
        install(self.roots, EXEC)
        unit = _read(self.unit)
        folders = (os.path.join(self.home, ".config"), os.path.dirname(self.unit_dir), self.unit_dir)
        calls = (lambda: install(self.roots, EXEC), lambda: upgrade(self.roots, EXEC_NEXT), lambda: uninstall(self.roots))
        for folder in folders:
            for mode in (0o777, 0o770, 0o702):
                with self.subTest(folder=folder, mode=oct(mode)):
                    os.chmod(folder, mode)
                    for call in calls:
                        with self.assertRaises(InstallRefused) as caught:
                            call()
                        self.assertEqual(caught.exception.kind, "folder_untrusted")
                        self.assertIn(folder, caught.exception.detail)
                    os.chmod(folder, 0o700)
            real_lstat = os.lstat

            def other_owner(path, *args, **kwargs):
                info = real_lstat(path, *args, **kwargs)
                if os.fspath(path) == folder:
                    fields = list(info[:10])
                    fields[4] = info.st_uid + 1
                    return os.stat_result(fields)
                return info
            with self.subTest(folder=folder, case="another owner"), mock.patch("os.lstat", side_effect=other_owner):
                for call in calls:
                    with self.assertRaises(InstallRefused) as caught:
                        call()
                    self.assertEqual(caught.exception.kind, "folder_untrusted")
        self.assertEqual(_read(self.unit), unit)  # nothing was rewritten or removed
        os.chmod(self.unit_dir, 0o755)  # read and search for others is fine
        self.assertFalse(install(self.roots, EXEC).changed)

    def test_the_folder_walk_refuses_a_path_outside_the_root_and_a_link_on_its_own(self) -> None:
        # Both callers build the path from the home today; the rule holds for any other (DeepSeek's list).
        for call in (layout.ensure_dir, layout.check_dirs):
            for outside in (self.case, os.path.join(self.case, "elsewhere"), self.home):
                with self.subTest(call=call.__name__, path=outside):
                    with self.assertRaises(InstallRefused) as caught:
                        call(self.home, outside)
                    self.assertEqual(caught.exception.kind, "path_escapes")
        target = os.path.join(self.case, "target")
        os.mkdir(target, 0o700)
        os.symlink(target, os.path.join(self.home, "linked"))
        with self.assertRaises(InstallRefused) as caught:
            layout.ensure_dir(self.home, os.path.join(self.home, "linked", "below"))
        self.assertEqual(caught.exception.kind, "symlink_refused")
        self.assertEqual(os.listdir(target), [])

    def test_unlink_regular_removes_only_a_regular_file(self) -> None:
        missing = os.path.join(self.home, "missing")
        self.assertFalse(layout.unlink_regular(missing))
        dangling = os.path.join(self.home, "dangling")
        os.symlink(missing, dangling)
        folder = os.path.join(self.home, "folder")
        os.mkdir(folder)
        for path in (dangling, folder):
            with self.subTest(path=path):
                with self.assertRaises(InstallRefused) as caught:
                    layout.unlink_regular(path)
                self.assertEqual(caught.exception.kind, "not_regular")
                self.assertTrue(os.path.lexists(path))
        regular = os.path.join(self.home, "regular")
        with open(regular, "wb"):
            pass
        self.assertTrue(layout.unlink_regular(regular))
        self.assertFalse(os.path.lexists(regular))

    def test_a_unit_larger_than_one_mib_is_refused(self) -> None:
        # The read stops at 1 MiB (review 1, Sol R5: M21); a file of exactly 1 MiB is still read.
        os.makedirs(self.unit_dir, 0o700)
        with open(self.unit, "wb") as handle:
            handle.write(b"#" * (1 << 20))
        with self.assertRaises(InstallRefused) as caught:
            install(self.roots, EXEC)
        self.assertEqual(caught.exception.kind, "unit_differs")  # read in full, then compared
        with open(self.unit, "ab") as handle:
            handle.write(b"#")
        for call in (lambda: install(self.roots, EXEC), lambda: upgrade(self.roots, EXEC), lambda: uninstall(self.roots)):
            with self.assertRaises(InstallRefused) as caught:
                call()
            self.assertEqual(caught.exception.kind, "too_large")
        self.assertEqual(os.path.getsize(self.unit), (1 << 20) + 1)

    def test_a_fifo_at_the_unit_is_refused_without_blocking(self) -> None:
        # The read opens with O_NONBLOCK, so a FIFO no writer holds open is refused by the type check
        # instead of holding the command at the open (review 1, R2).
        os.makedirs(self.unit_dir, 0o700)
        os.mkfifo(self.unit, 0o600)
        real_open, flags = os.open, []

        def recorded(path, mode, *args, **kwargs):
            flags.append(mode)
            return real_open(path, mode, *args, **kwargs)
        for call in (lambda: install(self.roots, EXEC), lambda: upgrade(self.roots, EXEC), lambda: uninstall(self.roots)):
            with self.assertRaises(InstallRefused) as caught:
                with mock.patch("os.open", side_effect=recorded):
                    call()
            self.assertEqual(caught.exception.kind, "not_regular")
        self.assertTrue(flags and all(mode & os.O_NONBLOCK and mode & os.O_NOFOLLOW for mode in flags), flags)
        self.assertTrue(stat.S_ISFIFO(os.lstat(self.unit).st_mode))

    def test_a_link_or_folder_at_the_unit_itself_is_refused(self) -> None:
        os.makedirs(self.unit_dir, 0o700)
        target = os.path.join(self.case, "target.service")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(render_unit(EXEC))
        os.symlink(target, self.unit)
        for call in (lambda: install(self.roots, EXEC), lambda: upgrade(self.roots, EXEC_NEXT),
                     lambda: uninstall(self.roots)):
            with self.assertRaises(InstallRefused) as caught:
                call()
            self.assertEqual(caught.exception.kind, "not_regular")
        self.assertEqual(_read(target), render_unit(EXEC))
        os.unlink(self.unit)
        os.mkdir(self.unit)
        with self.assertRaises(InstallRefused) as caught:
            install(self.roots, EXEC)
        self.assertEqual(caught.exception.kind, "not_regular")

    def test_a_file_where_a_folder_belongs_is_refused(self) -> None:
        os.makedirs(os.path.join(self.home, ".config"), 0o700)
        with open(os.path.join(self.home, ".config", "systemd"), "w", encoding="utf-8") as handle:
            handle.write("not a folder")
        with self.assertRaises(InstallRefused) as caught:
            install(self.roots, EXEC)
        self.assertEqual(caught.exception.kind, "not_a_directory")

    def test_a_relative_or_missing_root_is_refused_and_a_linked_one_is_resolved(self) -> None:
        with self.assertRaises(InstallRefused) as caught:
            install(Roots("home"), EXEC)
        self.assertEqual(caught.exception.kind, "relative_path")
        with self.assertRaises(InstallRefused) as caught:
            install(Roots(os.path.join(self.case, "nobody")), EXEC)
        self.assertEqual(caught.exception.kind, "root")
        with self.assertRaises(InstallRefused):
            install("not roots", EXEC)  # type: ignore[arg-type]
        # A link on the way to the home is followed (``/home`` -> ``/var/home``) and held to the rule.
        link = os.path.join(self.case, "home-link")
        os.symlink(self.home, link)
        plan = install(Roots(link), EXEC)
        self.assertEqual(plan.unit, self.unit)
        a_file = os.path.join(self.case, "a-file")
        with open(a_file, "wb"):
            pass
        with self.assertRaises(InstallRefused) as caught:
            install(Roots(os.path.join(a_file, "home")), EXEC)
        self.assertEqual(caught.exception.kind, "root")

    def test_the_home_and_the_folders_above_it_must_be_out_of_other_users_reach(self) -> None:
        # Review 1 of #401, Codex Sol R4 (for #382): the home itself and every folder above it, not
        # only the folders below it. Each case refuses folder_untrusted before any file is touched.
        real_lstat = os.lstat

        def owned_by_other(target):
            def fake(path, *args, **kwargs):
                info = real_lstat(path, *args, **kwargs)
                if os.fspath(path) == target:
                    fields = list(info[:10])
                    fields[4] = info.st_uid + 1
                    return os.stat_result(fields)
                return info
            return mock.patch("os.lstat", side_effect=fake)

        def refused(detail_part):
            with self.assertRaises(InstallRefused) as caught:
                install(self.roots, EXEC)
            self.assertEqual(caught.exception.kind, "folder_untrusted")
            self.assertIn(detail_part, caught.exception.detail)
            self.assertFalse(os.path.lexists(self.unit))  # refused before any file is written

        case = os.path.realpath(self.case)
        for mode in (0o720, 0o702, 0o770):  # the home: group or world write, even sticky
            with self.subTest(home_mode=oct(mode)):
                os.chmod(self.home, mode)
                refused("group or world")
                os.chmod(self.home, mode | stat.S_ISVTX)
                refused("group or world")
        os.chmod(self.home, 0o700)
        with self.subTest(case="home owned by another user"), owned_by_other(os.path.realpath(self.home)):
            refused("owned by another user")

        def owned_by(target, uid=None, world_writable=False):
            def fake(path, *args, **kwargs):
                info = real_lstat(path, *args, **kwargs)
                if os.fspath(path) == target:
                    fields = list(info[:10])
                    if uid is not None:
                        fields[4] = uid
                    if world_writable:
                        fields[0] = (info.st_mode | stat.S_IWOTH) & ~stat.S_ISVTX
                    return os.stat_result(fields)
                return info
            return mock.patch("os.lstat", side_effect=fake)
        # A home root owns passes the walk's folder rule (root's or ours) but is not the user's.
        with self.subTest(case="home owned by root"), owned_by(os.path.realpath(self.home), uid=0):
            refused("owned by another user")
        with self.subTest(case="/ writable by world, not sticky"), owned_by("/", world_writable=True):
            refused("/ is writable by group or world and not sticky")
        for mode in (0o720, 0o702):  # a folder above: group or world write, unless sticky
            with self.subTest(above_mode=oct(mode)):
                os.chmod(case, mode)
                refused("not sticky")
                os.chmod(case, mode | stat.S_ISVTX)
                self.assertTrue(install(self.roots, EXEC).changed)
                uninstall(self.roots)
                os.chmod(case, 0o700)
        with self.subTest(case="folder above owned by another user"), owned_by_other(case):
            refused("owned by another user")
        link = os.path.join(self.case, "home-link")
        os.symlink(self.home, link)
        with self.subTest(case="link owned by another user"), owned_by_other(os.path.join(case, "home-link")):
            with self.assertRaises(InstallRefused) as caught:
                install(Roots(link), EXEC)
            self.assertIn("link another user owns", caught.exception.detail)
        loop = os.path.join(self.case, "loop")
        os.symlink("loop", loop)
        with self.assertRaises(InstallRefused) as caught:
            install(Roots(os.path.join(loop, "home")), EXEC)
        self.assertIn("more than 40 links", caught.exception.detail)
        for raised in (OSError(5, "Input/output error"), FileNotFoundError(2, "gone")):
            with self.subTest(readlink=type(raised).__name__), mock.patch("os.readlink", side_effect=raised):
                with self.assertRaises(InstallRefused) as caught:
                    install(Roots(link), EXEC)
                self.assertEqual(caught.exception.kind, "folder_untrusted")
        os.chmod(case, 0o600)  # a folder on the way that cannot be searched
        try:
            with self.assertRaises(InstallRefused) as caught:
                install(self.roots, EXEC)
        finally:
            os.chmod(case, 0o700)
        self.assertEqual((caught.exception.kind, caught.exception.detail.endswith("PermissionError")),
                         ("folder_untrusted", True))

    def test_losing_the_race_for_the_unit_is_refused_and_leaves_no_temporary_file(self) -> None:
        with mock.patch("os.link", side_effect=FileExistsError):
            with self.assertRaises(InstallRefused) as caught:
                install(self.roots, EXEC)
        self.assertEqual(caught.exception.kind, "already_present")
        self.assertEqual(os.listdir(self.unit_dir), [])

    def test_a_leftover_temporary_file_does_not_block_and_is_not_touched(self) -> None:
        os.makedirs(self.unit_dir, 0o700)
        stale = os.path.join(self.unit_dir, f".{UNIT_NAME}.tmp")
        with open(stale, "w", encoding="utf-8") as handle:
            handle.write("from a crash")
        install(self.roots, EXEC)
        self.assertEqual(_read(stale), "from a crash")

    def test_each_write_fsyncs_the_file_and_its_folder(self) -> None:
        real_fsync = os.fsync
        synced = []

        def fsync(fd):
            synced.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
            return real_fsync(fd)
        with mock.patch("os.fsync", side_effect=fsync):
            install(self.roots, EXEC)
            self.assertEqual(synced, ["file", "dir"])
            upgrade(self.roots, EXEC_NEXT)
            self.assertEqual(synced, ["file", "dir", "file", "dir"])
            uninstall(self.roots)
            self.assertEqual(synced, ["file", "dir", "file", "dir", "dir"])


if __name__ == "__main__":
    unittest.main()
