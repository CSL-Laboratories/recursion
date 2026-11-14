"""The per-user unit: install, upgrade, uninstall. No systemctl, no network; files under a temporary home."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from unittest import mock

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
        # A link above the unit's folders is the administrator's (``/home`` -> ``/var/home``).
        link = os.path.join(self.case, "home-link")
        os.symlink(self.home, link)
        plan = install(Roots(link), EXEC)
        self.assertEqual(plan.unit, self.unit)

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
