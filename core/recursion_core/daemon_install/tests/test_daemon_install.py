"""CSL-758: user-unit install, upgrade, uninstall. No systemctl, no network."""

from __future__ import annotations

import json
import os
import socket
import stat
import tempfile
import unittest

from recursion_core.daemon_install import (
    GRANT_SCHEMA,
    InstallRefused,
    Roots,
    crash_restart,
    install,
    leftovers,
    plan_commands,
    render_unit,
    service_fields,
    uninstall,
    upgrade,
)

EXEC = ("%h/.local/lib/recursion/venv/bin/python3", "-m", "recursion_core.daemon")
EXEC_NEXT = ("%h/.local/lib/recursion/venv/bin/python3", "-m", "recursion_core.daemon", "--gui-port", "8765")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _snapshot(directory: str) -> dict[str, bytes]:
    found = {}
    for dirpath, dirnames, filenames in os.walk(directory):
        dirnames.sort()
        for name in filenames:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as handle:
                found[path] = handle.read()
    return found


class RenderTests(unittest.TestCase):
    def test_unit_restarts_on_failure_and_starts_at_login(self) -> None:
        text = render_unit(EXEC)
        fields = service_fields(text)
        self.assertEqual(fields["Service.Restart"], "on-failure")
        self.assertEqual(fields["Service.RestartSec"], "2")
        self.assertEqual(fields["Service.UMask"], "0077")
        self.assertEqual(fields["Install.WantedBy"], "default.target")
        self.assertNotIn("token", fields["Service.ExecStart"])
        self.assertNotIn("Service.ExecStartPre", fields)
        self.assertEqual(crash_restart(text, token_file_exists=True), "restarted")
        self.assertEqual(crash_restart(text, token_file_exists=False), "restarted")

    def test_token_gate_in_a_unit_still_refuses_a_stale_token(self) -> None:
        gated = (
            "[Service]\n"
            "ExecStartPre=/bin/test -f %h/token\n"
            "ExecStart=/bin/true\n"
            "Restart=on-failure\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
        self.assertEqual(crash_restart(gated, token_file_exists=True), "refused_stale_token")
        self.assertEqual(crash_restart(gated, token_file_exists=False), "restarted")

    def test_no_restart_stays_down(self) -> None:
        quiet = "[Service]\nExecStart=/bin/true\nRestart=no\n"
        self.assertEqual(crash_restart(quiet, token_file_exists=True), "stayed_down")

    def test_exec_args_are_closed(self) -> None:
        for bad in (
            (),
            ("python3", "-m", "recursion_core.daemon"),
            ("%h/bin/python3", "-m", "recursion_core.daemon;id"),
            ("%h/../../bin/python3",),
            ("%h/bin/python3", "has space"),
            ["%h/bin/python3"],
        ):
            with self.assertRaises(InstallRefused) as caught:
                render_unit(bad)  # type: ignore[arg-type]
            self.assertIn(caught.exception.kind, {"exec_args", "exec_arg"})


class CommandTests(unittest.TestCase):
    def test_plans_are_user_manager_only(self) -> None:
        for action in ("install", "upgrade", "uninstall"):
            for command in plan_commands(action):
                self.assertEqual(command[0], "systemctl")
                self.assertEqual(command[1], "--user")
                self.assertNotIn("sudo", command)
                self.assertNotIn("--system", command)
        self.assertIn("enable", plan_commands("install")[1])
        self.assertNotIn("--now", plan_commands("install")[1])
        self.assertEqual(plan_commands("uninstall")[0][2], "disable")
        self.assertIn("--now", plan_commands("uninstall")[0])

    def test_package_does_not_import_a_process_or_network_module(self) -> None:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        banned = ("subprocess", "urllib", "socket", "ctypes", "http.client")
        for dirpath, _, filenames in os.walk(root):
            if dirpath.endswith(os.sep + "tests"):
                continue
            for name in filenames:
                if not name.endswith(".py"):
                    continue
                text = _read(os.path.join(dirpath, name))
                for needle in banned:
                    self.assertNotIn(f"import {needle}", text)
                    self.assertNotIn(f"from {needle}", text)


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="csl758-")
        self.case = self._tmp.name
        self.roots = Roots(
            home=os.path.join(self.case, "home"),
            runtime_dir=os.path.join(self.case, "runtime"),
            state_dir=os.path.join(self.case, "state"),
        )
        for path in (self.roots.home, self.roots.runtime_dir, self.roots.state_dir):
            os.mkdir(path, 0o700)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_install_writes_unit_and_grant_and_keeps_registry(self) -> None:
        registry = os.path.join(self.roots.state_dir, "registry")
        os.mkdir(registry, 0o700)
        db = os.path.join(registry, "runs.db")
        with open(db, "wb") as handle:
            handle.write(b"rows-v1")
        before = _snapshot(registry)
        plan = install(self.roots, EXEC)
        self.assertEqual(plan.action, "install")
        self.assertEqual(_snapshot(registry), before)
        fields = service_fields(_read(plan.unit))
        self.assertEqual(fields["Service.Restart"], "on-failure")
        self.assertEqual(fields["Install.WantedBy"], "default.target")
        self.assertEqual(stat.S_IMODE(os.stat(plan.unit).st_mode), 0o644)
        grant = json.loads(_read(plan.grant))
        self.assertEqual(grant["schema"], GRANT_SCHEMA)
        self.assertEqual(grant["engine_user_grant"], "absent")
        self.assertEqual(grant["status"], "not_a_privilege")
        self.assertEqual(stat.S_IMODE(os.stat(plan.grant).st_mode), 0o600)
        self.assertFalse(os.path.lexists(plan.socket))
        again = install(self.roots, EXEC)
        self.assertEqual(again.unit, plan.unit)

    def test_install_refuses_a_different_existing_unit(self) -> None:
        install(self.roots, EXEC)
        with self.assertRaises(InstallRefused) as caught:
            install(self.roots, EXEC_NEXT)
        self.assertEqual(caught.exception.kind, "unit_differs")

    def test_upgrade_replaces_the_unit_and_leaves_the_registry_bytes(self) -> None:
        install(self.roots, EXEC)
        registry = os.path.join(self.roots.state_dir, "registry")
        db = os.path.join(registry, "runs.db")
        with open(db, "wb") as handle:
            handle.write(b"keep-me")
        os.chmod(registry, 0o750)
        before = _snapshot(registry)
        plan = upgrade(self.roots, EXEC_NEXT)
        self.assertEqual(_snapshot(registry), before)
        self.assertEqual(stat.S_IMODE(os.stat(registry).st_mode), 0o750)
        text = _read(plan.unit)
        self.assertIn("--gui-port", text)
        self.assertEqual(crash_restart(text, token_file_exists=True), "restarted")

    def test_uninstall_clears_unit_socket_and_grant_and_keeps_the_registry(self) -> None:
        plan = install(self.roots, EXEC)
        registry = plan.registry
        db = os.path.join(registry, "runs.db")
        with open(db, "wb") as handle:
            handle.write(b"still-here")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(plan.socket)
        sock.close()
        other = os.path.join(self.roots.state_dir, "grants", "note.txt")
        with open(other, "wb") as handle:
            handle.write(b"keep-note")
        token = os.path.join(self.roots.state_dir, "token")
        with open(token, "wb") as handle:
            handle.write(b"stale-token")
        self.assertTrue(leftovers(self.roots))
        uninstall(self.roots)
        self.assertEqual(leftovers(self.roots), ())
        with open(db, "rb") as handle:
            self.assertEqual(handle.read(), b"still-here")
        with open(other, "rb") as handle:
            self.assertEqual(handle.read(), b"keep-note")
        self.assertFalse(os.path.exists(plan.grant))
        self.assertTrue(os.path.isfile(token))
        self.assertEqual(
            crash_restart(render_unit(EXEC), token_file_exists=True),
            "restarted",
        )
        again = uninstall(self.roots)
        self.assertEqual(again.action, "uninstall")
        self.assertEqual(leftovers(self.roots), ())

    def test_upgrade_without_a_unit_is_refused(self) -> None:
        with self.assertRaises(InstallRefused) as caught:
            upgrade(self.roots, EXEC)
        self.assertEqual(caught.exception.kind, "unit_missing")

    def test_symlink_roots_and_components_are_refused(self) -> None:
        outside = os.path.join(self.case, "outside")
        os.mkdir(outside, 0o700)
        link = os.path.join(self.case, "home-link")
        os.symlink(self.roots.home, link)
        with self.assertRaises(InstallRefused) as caught:
            install(Roots(link, self.roots.runtime_dir, self.roots.state_dir), EXEC)
        self.assertEqual(caught.exception.kind, "symlink_refused")
        os.makedirs(os.path.join(self.roots.home, ".config"), 0o700)
        os.symlink(outside, os.path.join(self.roots.home, ".config", "systemd"))
        with self.assertRaises(InstallRefused) as caught:
            install(self.roots, EXEC)
        self.assertEqual(caught.exception.kind, "symlink_refused")
        self.assertEqual(os.listdir(outside), [])

    def test_relative_root_is_refused(self) -> None:
        with self.assertRaises(InstallRefused) as caught:
            install(Roots("home", self.roots.runtime_dir, self.roots.state_dir), EXEC)
        self.assertEqual(caught.exception.kind, "relative_path")


if __name__ == "__main__":
    unittest.main()
