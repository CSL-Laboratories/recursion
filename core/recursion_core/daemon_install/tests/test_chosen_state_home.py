"""A person may choose the state folder through XDG_STATE_HOME, and every refusal checks the chosen place.

The owner answered that the state folder is the person's choice at the release: ``XDG_STATE_HOME`` when it is
set to an absolute path, else ``<home>/.local/state``, the standard's default, with ``recursion`` under it. One
resolver names it (``daemon_install.layout.state_home``); an empty value is the default and a relative value is
refused ``state_home_relative`` by install, upgrade and the daemon's start alike. No store is ever moved or
removed. Every path here is under one temporary folder each test makes; no systemctl, no network.
"""

from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import recursion_core
from recursion_core.plugin_security.signing import backend_available
from recursion_core.control.commands import setup
from recursion_core.daemon_install import InstallRefused, Roots, install, owned_paths, render_unit, uninstall, upgrade
from recursion_core.daemon_install.install import ensure_ledger
from recursion_core.daemon_install.tests.test_daemon_install import EXEC, EXEC_NEXT, POSIX_PATHS, PY
from recursion_core.daemon_install.unit import UNIT_NAME, state_elsewhere
from recursion_core.plugin_security import stores

TEST_ANCHOR = "11" * 32  # a test-only public key, not one the installer chooses
LINUX = os.name == "posix" and POSIX_PATHS


def _probe(interpreter):
    return {"implementation": "cpython", "version": [3, 14], "free_threaded_build": True,
            "package": recursion_core.__file__}


def _refusing_probe(interpreter):
    raise AssertionError("a refused value must be answered before the interpreter is started")


def _tree(root):
    found = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            info = os.lstat(path)
            found[path] = (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), info.st_ino, info.st_size)
    return found


@unittest.skipUnless(LINUX, "the installer is Linux-only")
class ChosenStateHomeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="chosen-state-")
        self.addCleanup(self._tmp.cleanup)
        self.case = os.path.realpath(self._tmp.name)
        os.chmod(self.case, 0o700)
        self.home = os.path.join(self.case, "home")
        os.mkdir(self.home, 0o700)
        self.chosen = os.path.join(self.case, "chosen-state")  # not there yet: install makes it
        self.unit = os.path.join(self.home, ".config", "systemd", "user", UNIT_NAME)
        self.state = os.path.join(self.chosen, "recursion")
        self.store_dir = os.path.join(self.state, "plugin-security")
        self.breaker = os.path.join(self.store_dir, "breaker.sqlite3")
        self.revoked = os.path.join(self.store_dir, "revoked.sqlite3")
        self.ledger = os.path.join(self.store_dir, "ledger.sqlite3")
        self.homes = os.path.join(self.state, "homes")
        self.default_state = os.path.join(self.home, ".local", "state", "recursion")

    @property
    def roots(self):
        return Roots(home=self.home, state_home=self.chosen)

    def run_action(self, action, environ, probe=_probe, interpreter=PY):
        out = io.StringIO()
        code = setup.run(action, out=out, roots=Roots(home=self.home), interpreter=interpreter,
                         package=recursion_core.__file__, probe=probe, environ=environ)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        return code, json.loads(lines[0])

    def refused(self, kind, call, *args, **kwargs):
        with self.assertRaises(InstallRefused) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.kind, kind, caught.exception.detail)
        return caught.exception

    def start_daemon(self, state):
        """Start the composed daemon on ``state`` with this test's home; a ``SystemExit`` is its refusal."""
        from recursion_core.daemon import Daemon, Registry
        from recursion_core.daemon import control_adapter as adapter
        from recursion_core.daemon.tests.test_installed_profile_stores import QuietManager

        runtime = os.path.join(self.case, "rt")
        os.makedirs(runtime, 0o700, exist_ok=True)
        environ = {"PATH": "/usr/bin:/bin", "HOME": self.home, "LANG": "C.UTF-8", "XDG_RUNTIME_DIR": runtime,
                   "XDG_CONFIG_HOME": os.path.join(self.case, "config")}
        daemon = Daemon(Registry(state), QuietManager(), boot_id=lambda: "test-boot")
        daemon.sweep()
        try:
            composed = adapter.compose_control(daemon, state, environ=environ, routes={})
        except BaseException:
            daemon.close()
            raise
        self.addCleanup(daemon.close)
        self.addCleanup(composed.close)
        return composed

    # The place in use, the unit and the answer.

    def test_install_makes_the_stores_in_the_chosen_place_and_names_it(self):
        code, answer = self.run_action("install", {"XDG_STATE_HOME": self.chosen})
        self.assertEqual((code, answer["code"]), (0, "ok"), answer)
        self.assertEqual(answer["state"], self.state)
        self.assertEqual(answer["stores"], [
            {"kind": "breaker_store", "path": self.breaker, "created": True},
            {"kind": "revocation_store", "path": self.revoked, "created": True}])
        self.assertEqual(answer["ledger"]["path"], self.ledger)
        self.assertNotIn("stores_left", answer)
        self.assertFalse(os.path.lexists(os.path.join(self.home, ".local")))  # nothing in the default place
        self.assertEqual(stat.S_IMODE(os.stat(self.chosen).st_mode), 0o700)
        with open(self.unit, "rb") as handle:
            unit = handle.read()
        self.assertEqual(unit, render_unit(EXEC, self.chosen).encode())
        self.assertIn(f'Environment="XDG_STATE_HOME={self.chosen}"\n'.encode(), unit)
        self.assertIsNone(state_elsewhere(unit, self.home, self.chosen))
        code, again = self.run_action("install", {"XDG_STATE_HOME": self.chosen})
        self.assertEqual((code, again["changed"]), (0, False))

    def test_the_default_is_unchanged_when_the_variable_is_unset_or_empty(self):
        default_store = os.path.join(self.default_state, "plugin-security")
        for environ in ({}, {"XDG_STATE_HOME": ""}, {"XDG_STATE_HOME": os.path.join(self.home, ".local", "state")}):
            with self.subTest(environ=environ):
                code, answer = self.run_action("install", environ)
                self.assertEqual((code, answer["code"]), (0, "ok"), answer)
                self.assertNotIn("state", answer)
                self.assertNotIn("stores_left", answer)
                self.assertEqual([store["path"] for store in answer["stores"]],
                                 [os.path.join(default_store, "breaker.sqlite3"), os.path.join(default_store, "revoked.sqlite3")])
                with open(self.unit, encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), render_unit(EXEC))
                self.assertIn("Environment=XDG_STATE_HOME=%h/.local/state\n", render_unit(EXEC))
        self.assertEqual(owned_paths(Roots(home=self.home)).state, self.default_state)
        self.assertFalse(os.path.lexists(self.chosen))

    def test_a_missing_chosen_place_and_its_missing_parents_are_made_owner_only(self):
        deeper = os.path.join(self.case, "a", "b", "state")
        install(Roots(home=self.home, state_home=deeper), EXEC)
        for folder in (os.path.join(self.case, "a"), os.path.join(self.case, "a", "b"), deeper,
                       os.path.join(deeper, "recursion"), os.path.join(deeper, "recursion", "plugin-security")):
            self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700, folder)

    def test_a_chosen_place_with_quotes_spaces_and_percent_is_written_so_systemd_reads_it_back(self):
        odd = os.path.join(self.case, 'my "state" 100% \\ here')
        install(Roots(home=self.home, state_home=odd), EXEC)
        with open(self.unit, "rb") as handle:
            unit = handle.read()
        self.assertIsNone(state_elsewhere(unit, self.home, odd))
        self.assertIsNotNone(state_elsewhere(unit, self.home, self.chosen))
        plan = upgrade(Roots(home=self.home, state_home=odd), EXEC)  # our own unit is current, not elsewhere
        self.assertFalse(plan.changed)

    # state_home_relative: install, upgrade and the daemon's start.

    def test_a_relative_value_is_refused_by_install_and_upgrade_before_anything_is_read(self):
        here = os.getcwd()  # a relative value taken as a folder would land here: inside this test's folder
        os.chdir(self.case)
        self.addCleanup(os.chdir, here)
        for value in ("state", "./state", "~/state"):
            for action in ("install", "upgrade"):
                with self.subTest(value=value, action=action):
                    code, answer = self.run_action(action, {"XDG_STATE_HOME": value}, probe=_refusing_probe)
                    self.assertEqual((code, answer["code"], answer["action"]), (1, "state_home_relative", action))
                    self.assertIn(f"XDG_STATE_HOME is {value}, a relative path", answer["detail"])
                    self.assertIn("Set it to an absolute path, or unset it", answer["detail"])
                    self.assertEqual(os.listdir(self.home), [])
        self.refused("state_home_relative", install, Roots(home=self.home, state_home="state"), EXEC)
        self.assertEqual(os.listdir(self.home), [])

    def test_the_daemon_refuses_to_start_on_a_relative_value_and_takes_the_folder_from_the_resolver(self):
        from recursion_core.daemon import __main__ as daemon_main

        here = os.getcwd()
        os.chdir(self.case)
        self.addCleanup(os.chdir, here)
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            self.assertEqual(daemon_main.state_dir({}), Path(self.default_state))
            self.assertEqual(daemon_main.state_dir({"XDG_STATE_HOME": ""}), Path(self.default_state))
            self.assertEqual(daemon_main.state_dir({"XDG_STATE_HOME": self.chosen}), Path(self.state))
            with self.assertRaises(SystemExit) as caught:
                daemon_main.state_dir({"XDG_STATE_HOME": "relative/state"})
        text = str(caught.exception)
        self.assertIn("recursion daemon refused to start: state_home_relative", text)
        self.assertIn("relative/state", text)
        self.assertNotIn("\n", text)
        # main() refuses before any lock, state, socket or GUI.
        with mock.patch.dict(os.environ, {"HOME": self.home, "XDG_STATE_HOME": "relative/state"}), \
                mock.patch.object(daemon_main, "require_posix"), \
                mock.patch.object(daemon_main, "require_free_threaded"), \
                mock.patch.object(daemon_main, "refuse_engine_user"), \
                mock.patch.object(daemon_main, "check_operator_linger"), \
                mock.patch.object(daemon_main, "UserLock", side_effect=AssertionError("lock taken")), \
                mock.patch.object(daemon_main.logging, "basicConfig"):
            with self.assertRaises(SystemExit) as caught:
                daemon_main.main()
        self.assertIn("state_home_relative", str(caught.exception))
        self.assertEqual(os.listdir(self.home), [])

    # Every refusal that looks at the state folder looks at the chosen place: one test each.

    def test_breaker_store_untrusted_in_the_chosen_place(self):
        elsewhere = os.path.join(self.case, "elsewhere.sqlite3")
        stores.create_breaker_store(elsewhere)
        os.makedirs(self.store_dir, 0o700)
        os.chmod(self.chosen, 0o700)
        os.symlink(elsewhere, self.breaker)
        error = self.refused("breaker_store_untrusted", install, self.roots, EXEC)
        self.assertIn(self.breaker, error.detail)
        self.assertFalse(os.path.lexists(self.unit))

    def test_revocation_store_invalid_in_the_chosen_place(self):
        os.makedirs(self.store_dir, 0o700)
        os.chmod(self.chosen, 0o700)
        stores.create_breaker_store(self.revoked)  # a breaker store where the revocation store belongs
        error = self.refused("revocation_store_invalid", install, self.roots, EXEC)
        self.assertIn(self.revoked, error.detail)
        self.assertFalse(os.path.lexists(self.unit))

    def test_store_not_created_in_the_chosen_place(self):
        with mock.patch.object(stores, "create_revocation_store", side_effect=OSError(28, "No space left on device")):
            error = self.refused("store_not_created", install, self.roots, EXEC)
        self.assertIn(self.revoked, error.detail)
        self.assertFalse(os.path.lexists(self.unit))

    def test_folder_untrusted_for_the_route_homes_root_in_the_chosen_place(self):
        os.makedirs(self.homes, 0o700)
        os.chmod(self.chosen, 0o700)
        os.chmod(self.homes, 0o755)
        error = self.refused("folder_untrusted", install, self.roots, EXEC)
        self.assertIn(self.homes, error.detail)
        self.assertFalse(os.path.lexists(self.store_dir))  # before anything is made
        self.assertFalse(os.path.lexists(self.unit))

    def test_folder_untrusted_for_a_chosen_place_group_or_world_can_write(self):
        os.mkdir(self.chosen, 0o700)
        os.chmod(self.chosen, 0o770)
        error = self.refused("folder_untrusted", install, self.roots, EXEC)
        self.assertIn(self.chosen, error.detail)
        self.assertEqual(os.listdir(self.chosen), [])
        self.assertFalse(os.path.lexists(self.unit))

    def test_unit_state_elsewhere_compares_with_the_chosen_place(self):
        install(self.roots, EXEC)
        for line in ("Environment=XDG_STATE_HOME=%h/.local/state\n", ""):
            with self.subTest(line=line):
                with open(self.unit, "w", encoding="utf-8") as handle:
                    handle.write(f"[Service]\n{line}ExecStart=/old\n")
                code, answer = self.run_action("upgrade", {"XDG_STATE_HOME": self.chosen}, interpreter=PY)
                self.assertEqual((code, answer["code"]), (1, "unit_state_elsewhere"), answer)
                self.assertIn(f"puts it in {self.state}", answer["detail"])
        with open(self.unit, "w", encoding="utf-8") as handle:
            handle.write(render_unit(EXEC, self.chosen))
        self.assertTrue(upgrade(self.roots, EXEC_NEXT).changed)  # the unit naming the chosen place is replaced

    def test_the_ledger_step_in_the_chosen_place(self):
        install(self.roots, EXEC)
        path, created, detail = ensure_ledger(owned_paths(self.roots), TEST_ANCHOR)
        self.assertEqual((path, created, detail), (self.ledger, True, ""))
        os.unlink(self.ledger)
        with open(self.ledger, "wb") as handle:
            handle.write(b"not a ledger" * 16)
        error = self.refused("ledger_invalid", install, self.roots, EXEC)
        self.assertIn(self.ledger, error.detail)

    def test_the_installed_profile_check_counts_a_chosen_folder_the_installers_unit_names(self):
        from recursion_core.daemon import control_adapter as adapter

        install(self.roots, EXEC)
        environ = {"HOME": self.home}
        refusal = adapter._installed_profile_store_refusal(self.state, environ)
        self.assertIsNotNone(refusal)
        self.assertIn(f"the authority ledger is missing ({self.ledger})", refusal)
        os.unlink(self.breaker)
        self.assertIn(f"the breaker store is missing ({self.breaker})",
                      adapter._installed_profile_store_refusal(self.state, environ))
        other = os.path.join(self.case, "other-state", "recursion")  # a folder the unit does not name
        self.assertIsNone(adapter._installed_profile_store_refusal(other, environ))
        with open(self.unit, "w", encoding="utf-8") as handle:
            handle.write(render_unit(EXEC))  # the installer's unit, naming the default
        self.assertIsNone(adapter._installed_profile_store_refusal(self.state, environ))

    def test_a_chosen_place_ending_in_local_state_is_an_installed_profile(self):
        # A second disk laid out as a home: the daemon decides "the default" from HOME, never from the path's shape.
        chosen = os.path.join(self.case, "disk2", "me", ".local", "state")
        install(Roots(home=self.home, state_home=chosen), EXEC)
        state = os.path.join(chosen, "recursion")
        breaker = os.path.join(state, "plugin-security", "breaker.sqlite3")
        with self.assertRaises(SystemExit) as caught:
            self.start_daemon(state)
        self.assertIn("the authority ledger is missing", str(caught.exception))
        os.unlink(breaker)  # removed by hand: the daemon refuses and makes no new store
        with self.assertRaises(SystemExit) as caught:
            self.start_daemon(state)
        self.assertIn(f"the breaker store is missing ({breaker})", str(caught.exception))
        self.assertFalse(os.path.lexists(breaker))

    def test_a_home_with_a_percent_sign_upgrades_to_another_interpreter(self):
        for name in ("ho%me", "100%%home", "%h"):
            with self.subTest(home=name):
                home = os.path.join(self.case, name)
                os.mkdir(home, 0o700)
                install(Roots(home=home), EXEC)
                self.assertFalse(upgrade(Roots(home=home), EXEC).changed)
                self.assertTrue(upgrade(Roots(home=home), EXEC_NEXT).changed)
                with open(os.path.join(home, ".config", "systemd", "user", UNIT_NAME), encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), render_unit(EXEC_NEXT))

    def test_the_printed_way_out_of_unit_state_elsewhere_works_when_followed(self):
        # A default install, then a chosen place: the default's unit is kept, and the words name only ways that work.
        code, answer = self.run_action("install", {})
        self.assertEqual((code, answer["code"]), (0, "ok"), answer)
        code, answer = self.run_action("upgrade", {"XDG_STATE_HOME": self.chosen})
        self.assertEqual((code, answer["code"]), (1, "unit_state_elsewhere"), answer)
        detail = answer["detail"]
        self.assertNotIn("take the line out", detail)
        self.assertNotIn("move that state folder", detail)
        self.assertIn("To keep the state where it is, run `recursiond upgrade` again with XDG_STATE_HOME unset", detail)
        self.assertIn(f"To start in {self.state} instead, run `recursiond uninstall`, move the state folder that "
                      f"unit names (with the ledger in it) to {self.state}, where nothing may be yet, then run "
                      f"`recursiond install` with XDG_STATE_HOME set to {self.chosen}", detail)
        # The first way, as printed: the state stays where it is.
        code, answer = self.run_action("upgrade", {})
        self.assertEqual((code, answer["code"]), (0, "ok"), answer)
        # The second way, as printed: the state folder moved to the chosen place, the ledger with it.
        code, answer = self.run_action("uninstall", {})
        self.assertEqual((code, answer["code"]), (0, "ok"), answer)
        self.hold_the_key_alone_refusal({"XDG_STATE_HOME": self.chosen})
        os.makedirs(self.chosen, 0o700)
        os.rename(self.default_state, self.state)
        code, answer = self.run_action("install", {"XDG_STATE_HOME": self.chosen})
        self.assertEqual((code, answer["code"], answer["state"]), (0, "ok", self.state), answer)
        self.assertNotIn("stores_left", answer)  # nothing is left in the default folder
        code, answer = self.run_action("upgrade", {"XDG_STATE_HOME": self.chosen})
        self.assertEqual((code, answer["code"]), (0, "ok"), answer)
        ensure_ledger(owned_paths(self.roots), TEST_ANCHOR)  # the anchor key's own step, with a test-only key
        self.assertTrue(self.start_daemon(self.state).server.path)

    def test_an_upgrade_without_the_variable_is_not_told_to_move_the_state(self):
        code, answer = self.run_action("install", {"XDG_STATE_HOME": self.chosen})
        self.assertEqual((code, answer["code"]), (0, "ok"), answer)
        code, answer = self.run_action("upgrade", {})
        self.assertEqual((code, answer["code"]), (1, "unit_state_elsewhere"), answer)
        detail = answer["detail"]
        self.assertNotIn("take the line out", detail)
        self.assertNotIn("move that state folder", detail)
        self.assertIn(f"sets XDG_STATE_HOME={self.chosen}", detail)
        self.assertIn("To keep the state where it is, run `recursiond upgrade` again with XDG_STATE_HOME set to the "
                      "folder that unit names", detail)
        self.assertIn(f"To start in {self.default_state} instead, run `recursiond uninstall`, move the state folder "
                      f"that unit names (with the ledger in it) to {self.default_state}, where nothing may be yet, then "
                      "run `recursiond install` with XDG_STATE_HOME unset", detail)
        code, answer = self.run_action("upgrade", {"XDG_STATE_HOME": self.chosen})  # the first way, as printed
        self.assertEqual((code, answer["code"]), (0, "ok"), answer)
        code, answer = self.run_action("uninstall", {})  # the second way, as printed
        self.assertEqual((code, answer["code"]), (0, "ok"), answer)
        self.hold_the_key_alone_refusal({})
        os.makedirs(os.path.dirname(self.default_state), 0o700, exist_ok=True)
        os.rename(self.state, self.default_state)
        for action in ("install", "upgrade"):
            code, answer = self.run_action(action, {})
            self.assertEqual((code, answer["code"]), (0, "ok"), (action, answer))

    def hold_the_key_alone_refusal(self, environ):
        """With the install's key made, an install in a state folder with no ledger is refused and writes
        nothing: the state folder must be moved first, as the printed way says."""
        if not backend_available():
            return  # no key is made without cryptography: nothing to refuse
        target = os.path.join(environ.get("XDG_STATE_HOME") or os.path.join(self.home, ".local", "state"), "recursion")
        there = os.path.lexists(target)
        code, answer = self.run_action("install", environ)
        self.assertEqual((code, answer["code"]), (1, "authority_anchor_disagrees"), answer)
        self.assertIn("no ledger is at", answer["detail"])
        self.assertEqual(os.path.lexists(target), there)

    def test_a_link_another_user_owns_on_the_way_to_the_chosen_place_is_refused(self):
        target = os.path.join(self.case, "target")
        os.mkdir(target, 0o700)
        link = os.path.join(self.case, "link")
        os.symlink(target, link)
        chosen = os.path.join(link, "state")
        real_lstat = os.lstat

        def lstat(path, *args, **kwargs):  # a stand-in owner: the link is another user's
            info = real_lstat(path, *args, **kwargs)
            if os.fspath(path) == link:
                fields = list(info[:10])
                fields[4] = os.geteuid() + 1
                return os.stat_result(fields)
            return info

        with mock.patch("os.lstat", lstat):
            error = self.refused("folder_untrusted", install, Roots(home=self.home, state_home=chosen), EXEC)
        self.assertIn(f"{link} is a link another user owns", error.detail)
        self.assertEqual(os.listdir(target), [])
        self.assertFalse(os.path.lexists(self.unit))
        # The person's own link on the way is followed, and the unit names the real folder.
        install(Roots(home=self.home, state_home=chosen), EXEC)
        with open(self.unit, "rb") as handle:
            self.assertEqual(handle.read(), render_unit(EXEC, os.path.join(target, "state")).encode())
        self.assertTrue(os.path.lexists(os.path.join(target, "state", "recursion", "plugin-security", "breaker.sqlite3")))

    # No store is moved or removed.

    def test_stores_in_the_default_place_are_left_where_they_are_and_named(self):
        install(Roots(home=self.home), EXEC)
        default_store = os.path.join(self.default_state, "plugin-security")
        before = _tree(self.default_state)
        code, answer = self.run_action("upgrade", {"XDG_STATE_HOME": self.chosen})
        self.assertEqual((code, answer["code"]), (1, "unit_state_elsewhere"), answer)  # the default's unit is kept
        os.unlink(self.unit)
        code, answer = self.run_action("install", {"XDG_STATE_HOME": self.chosen})
        self.assertEqual((code, answer["code"]), (0, "ok"), answer)
        self.assertEqual(answer["stores_left"],
                         f"{default_store} holds breaker.sqlite3, revoked.sqlite3 from the default state folder; they "
                         f"are left where they are and not used, since XDG_STATE_HOME chose {self.chosen}")
        self.assertEqual(_tree(self.default_state), before)
        self.assertTrue(os.path.lexists(self.breaker) and os.path.lexists(self.revoked))
        uninstall(self.roots)
        self.assertEqual(_tree(self.default_state), before)
        self.assertTrue(os.path.lexists(self.breaker) and os.path.lexists(self.revoked))


if __name__ == "__main__":
    unittest.main()
