"""S7 triage (routes-core): witnesses for confinement checks in subscription_routes/.

SR-06, SR-07, SR-10 and SR-11. Scratch homes and packages only; no engine, no network,
no credential (every key value is a fake string).
"""

from __future__ import annotations

import errno
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from recursion_core.subscription_routes import (
    SubscriptionRouteRefused, WrapperInstall, check_route, measure_package_pin,
)
from recursion_core.subscription_routes import policy, wrapper_pin
from recursion_core.subscription_routes.wrapper_pin import WrapperPinRefused, verify_package_pin


class _Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="s7-sr-"))
        self.addCleanup(self._cleanup)
        self.operator = self.root / "operator"
        self.operator.mkdir(mode=0o700)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.install = self.root / "pkg"
        self.install.mkdir(mode=0o700)
        (self.install / "launcher.js").write_text("launcher-v1\n", encoding="ascii")
        (self.install / "lib.js").write_text("lib-v1\n", encoding="ascii")
        lp = str(self.install / "launcher.js")
        self.wrapper = WrapperInstall(lp, str(self.install), measure_package_pin(lp, str(self.install)))

    def _cleanup(self):
        for dirpath, dirnames, _ in os.walk(self.root):
            for name in dirnames:
                try:
                    os.chmod(os.path.join(dirpath, name), 0o700)
                except OSError:
                    pass
        shutil.rmtree(self.root, True)

    def parent(self):
        return {"PATH": "/usr/bin:/bin", "HOME": str(self.operator)}


@unittest.skipIf(os.name == "nt", "Linux-only calls (os.geteuid, os.mkfifo, symlinks) and POSIX modes")
class Policy(_Base):
    def chatgpt(self):
        return check_route("opencode", {"provider": "chatgpt", "auth": "subscription",
                                        "method": "sign_in_with_chatgpt", "model": "chatgpt/m"},
                           {"PATH": "/usr/bin:/bin", "HOME": str(self.home)}, route_home=str(self.home),
                           parent_env=self.parent(), operator_home=str(self.operator))

    # SR-06: an unreadable own-store subtree is silently skipped, so an outside link passes.
    def test_sr06_unreadable_store_subtree_refuses(self):
        outside = self.root / "outside"
        outside.mkdir()
        sub = self.home / ".config" / "opencode" / "unreadable"
        sub.mkdir(parents=True)
        (sub / "link").symlink_to(outside)
        with self.assertRaises(SubscriptionRouteRefused):  # control: readable, refused
            self.chatgpt()
        sub.chmod(0)
        if os.access(sub, os.R_OK):
            self.skipTest("running as a user that reads mode-000 directories")
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            self.chatgpt()
        self.assertEqual(caught.exception.cause, "own_store_unreadable")
        self.assertNotIn("Permission", str(caught.exception))

    def _refuse_unreadable(self, call):
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            call()
        self.assertEqual(caught.exception.cause, "own_store_unreadable")
        text = str(caught.exception)
        self.assertNotIn("Permission", text)
        self.assertNotIn("Errno", text)

    # F1: an ancestor that cannot be searched is not "the store is absent".
    def test_sr06_unreadable_ancestor_is_not_an_absent_store(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory mode bits")
        outside = self.root / "outside"
        outside.mkdir()
        decision = self.chatgpt()
        self.assertEqual(decision.engine, "opencode")

        store = self.home / ".config" / "opencode"
        store.mkdir(parents=True)
        (store / "link").symlink_to(outside)
        config = self.home / ".config"
        config.chmod(0o000)
        if os.access(config, os.X_OK):
            self.skipTest("this user can search a mode-000 directory")
        with self.subTest("config-mode-000"):
            self._refuse_unreadable(self.chatgpt)
        config.chmod(0o700)
        config.chmod(0o400)
        if os.access(config, os.X_OK):
            self.skipTest("this user can search a mode-0400 directory")
        with self.subTest("config-mode-0400"):
            self._refuse_unreadable(self.chatgpt)
        config.chmod(0o700)
        shutil.rmtree(config)

        local = self.home / ".local"
        deep = local / "share" / "opencode"
        deep.mkdir(parents=True)
        (deep / "link").symlink_to(outside)
        local.chmod(0o000)
        with self.subTest("local-mode-000"):
            self._refuse_unreadable(self.chatgpt)
        local.chmod(0o700)
        shutil.rmtree(local)

        # Pi does not walk OpenCode's store. An unsearchable .config must still
        # hide neither a Claude store nor the fact that the path cannot be checked.
        hidden = self.home / ".config"
        hidden.mkdir()
        (hidden / "claude").mkdir()
        hidden.chmod(0o000)
        with self.subTest("pi-claude-behind-unsearchable-config"):
            self._refuse_unreadable(lambda: check_route(
                "pi",
                {"provider": "chatgpt", "auth": "subscription",
                 "method": "sign_in_with_chatgpt", "model": "chatgpt/m"},
                {"PATH": "/usr/bin:/bin", "HOME": str(self.home)},
                route_home=str(self.home), parent_env=self.parent(),
                operator_home=str(self.operator), wrapper=self.wrapper))

    # F2: a directory that can be listed but not searched must not hide a link.
    def test_sr06_listable_unsearchable_directory_hides_no_link(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory mode bits")
        outside = self.root / "outside"
        outside.mkdir()
        sub = self.home / ".config" / "opencode" / "r-only"
        sub.mkdir(parents=True)
        (sub / "link").symlink_to(outside)
        sub.chmod(0o400)
        if os.access(sub, os.X_OK):
            self.skipTest("this user can search a mode-0400 directory")
        with self.subTest("symlink"):
            self._refuse_unreadable(self.chatgpt)
        sub.chmod(0o700)
        (sub / "link").unlink()
        (sub / "note.txt").write_text("not-a-link\n", encoding="ascii")
        sub.chmod(0o400)
        with self.subTest("plain-file"):
            self._refuse_unreadable(self.chatgpt)
        sub.chmod(0o700)
        (sub / "note.txt").unlink()
        sub.chmod(0o400)
        with self.subTest("empty"):
            self.chatgpt()

    # F6: a name that disappears during the walk is not reported as unreadable.
    def test_sr06_walk_enoent_is_its_own_cause(self):
        store = self.home / ".config" / "opencode"
        store.mkdir(parents=True)

        def gone(top, topdown=True, onerror=None, followlinks=False):
            err = OSError(errno.ENOENT, "No such file or directory")
            err.filename = top
            onerror(err)
            return iter(())

        with mock.patch.object(policy.os, "walk", gone):
            with self.assertRaises(SubscriptionRouteRefused) as caught:
                self.chatgpt()
        self.assertEqual(caught.exception.cause, "own_store_changed")
        self.assertNotIn("No such file", str(caught.exception))

    # SR-07: ordinary directories consume none of the store-walk budget.
    def test_sr07_walk_budget_counts_directories(self):
        store = self.home / ".config" / "opencode"
        for i in range(3):
            (store / f"d{i}").mkdir(parents=True)
        with mock.patch.object(policy, "_STORE_WALK_CAP", 2):
            with self.assertRaises(SubscriptionRouteRefused) as caught:
                self.chatgpt()
        self.assertEqual(caught.exception.cause, "own_store_too_large")


@unittest.skipIf(os.name == "nt", "Linux-only calls (os.geteuid, os.mkfifo, symlinks) and POSIX modes")
class WrapperPin(_Base):
    # SR-10: an unreadable package file escapes measurement/verification as PermissionError.
    def test_sr10_unreadable_file_is_a_refusal(self):
        (self.install / "lib.js").chmod(0)
        if os.access(self.install / "lib.js", os.R_OK):
            self.skipTest("running as a user that reads mode-000 files")
        for name, fn in {"measure": lambda: measure_package_pin(self.wrapper.launcher_path, str(self.install)),
                         "verify": lambda: verify_package_pin("cursor", self.wrapper)}.items():
            with self.subTest(name):
                try:
                    fn()
                except WrapperPinRefused:
                    pass
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"untyped exception: {type(exc).__name__}")

    # SR-11: a regular file swapped for a FIFO between check and open blocks hashing forever.
    def test_sr11_fifo_swap_does_not_block(self):
        lib = str(self.install / "lib.js")
        real_isfile = os.path.isfile
        swapped = threading.Event()

        def isfile(path):
            answer = real_isfile(path)
            if path == lib and not swapped.is_set():
                os.unlink(lib)
                os.mkfifo(lib, 0o600)
                swapped.set()
            return answer

        outcome = []

        def worker():
            try:
                measure_package_pin(self.wrapper.launcher_path, str(self.install))
                outcome.append("returned")
            except Exception as exc:  # noqa: BLE001
                outcome.append(type(exc).__name__)

        with mock.patch.object(wrapper_pin.os.path, "isfile", isfile):
            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            thread.join(2.0)
        blocked = thread.is_alive()
        if blocked:  # release the reader so the test leaves nothing behind
            write_fd = os.open(lib, os.O_WRONLY | os.O_NONBLOCK)
            os.close(write_fd)
            thread.join(2.0)
        self.assertFalse(blocked, "measure_package_pin blocked on a FIFO for 2 s")
        self.assertEqual(outcome, ["WrapperPinRefused"])

    # F4: a symlink swapped in at the regular-file check must not be hashed.
    def test_sr11_symlink_swap_is_not_followed(self):
        lib = str(self.install / "lib.js")
        outside = self.root / "outside.txt"
        outside.write_text("outside-bytes\n", encoding="ascii")
        real_isfile = os.path.isfile

        def isfile(path):
            answer = real_isfile(path)
            if path == lib:
                os.unlink(lib)
                os.symlink(outside, lib)
            return answer

        with mock.patch.object(wrapper_pin.os.path, "isfile", isfile):
            with self.assertRaises(WrapperPinRefused) as caught:
                measure_package_pin(self.wrapper.launcher_path, str(self.install))
        self.assertEqual(caught.exception.cause, "wrapper_file_unreadable")
        self.assertNotIn("outside-bytes", str(caught.exception))

    # F4: a FIFO swapped in at the same step is not pinned as an empty file.
    def test_sr11_fifo_swap_is_not_a_regular_file(self):
        lib = str(self.install / "lib.js")
        real_isfile = os.path.isfile

        def isfile(path):
            answer = real_isfile(path)
            if path == lib:
                os.unlink(lib)
                os.mkfifo(lib, 0o600)
            return answer

        with mock.patch.object(wrapper_pin.os.path, "isfile", isfile):
            try:
                with self.assertRaises(WrapperPinRefused) as caught:
                    measure_package_pin(self.wrapper.launcher_path, str(self.install))
            finally:
                if os.path.exists(lib):
                    try:
                        write_fd = os.open(lib, os.O_WRONLY | os.O_NONBLOCK)
                    except OSError:
                        write_fd = -1
                    if write_fd >= 0:
                        os.close(write_fd)
        self.assertEqual(caught.exception.cause, "wrapper_package_not_a_file")

    # F3: a name that vanishes between the listing and the stat is a typed refusal.
    def test_sr10_vanished_file_is_a_refusal(self):
        real_lstat = wrapper_pin.os.lstat

        def lstat(path, *args, **kwargs):
            if os.path.basename(path) == "lib.js":
                raise FileNotFoundError(errno.ENOENT, "No such file or directory", path)
            return real_lstat(path, *args, **kwargs)

        with mock.patch.object(wrapper_pin.os, "lstat", lstat):
            for name, fn in {
                "measure": lambda: measure_package_pin(self.wrapper.launcher_path, str(self.install)),
                "verify": lambda: verify_package_pin("cursor", self.wrapper),
            }.items():
                with self.subTest(name):
                    with self.assertRaises(WrapperPinRefused) as caught:
                        fn()
                    self.assertEqual(caught.exception.cause, "wrapper_tree_unreadable")
                    self.assertNotIn("No such file", str(caught.exception))

        (self.install / "link.js").symlink_to(self.install / "lib.js")
        real_readlink = wrapper_pin.os.readlink

        def readlink(path, *args, **kwargs):
            if os.path.basename(path) == "link.js":
                raise FileNotFoundError(errno.ENOENT, "No such file or directory", path)
            return real_readlink(path, *args, **kwargs)

        with mock.patch.object(wrapper_pin.os, "readlink", readlink):
            with self.subTest("readlink"):
                with self.assertRaises(WrapperPinRefused) as caught:
                    measure_package_pin(self.wrapper.launcher_path, str(self.install))
                self.assertEqual(caught.exception.cause, "wrapper_tree_unreadable")

        def interrupt(path, *args, **kwargs):
            if os.path.basename(path) == "lib.js":
                raise KeyboardInterrupt
            return real_lstat(path, *args, **kwargs)

        with mock.patch.object(wrapper_pin.os, "lstat", interrupt):
            with self.subTest("interrupt"):
                with self.assertRaises(KeyboardInterrupt):
                    measure_package_pin(self.wrapper.launcher_path, str(self.install))


if __name__ == "__main__":
    unittest.main()
