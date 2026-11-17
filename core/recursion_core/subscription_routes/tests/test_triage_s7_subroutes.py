"""S7 triage (routes-core): witnesses for confinement and refusal checks in subscription_routes/.

SR-01, SR-02, SR-03, SR-05, SR-06, SR-07, SR-10 and SR-11. Scratch homes and packages only;
no engine, no network, no credential (every key value is a fake string).
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
    HERMES_ADOPT_EXTERNAL_LOGINS, SubscriptionRouteRefused, WrapperInstall, check_route,
    measure_package_pin,
)
from recursion_core.subscription_routes import own_loop, policy, wrapper_pin
from recursion_core.subscription_routes.wrapper_pin import WrapperPinRefused, verify_package_pin

MARK = "S7-TRIAGE-MARKER"


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

    def call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (SubscriptionRouteRefused, WrapperPinRefused, own_loop.OwnLoopRouteRefused):
            raise
        except Exception as exc:  # noqa: BLE001 - the defect is an untyped exception
            self.fail(f"untyped exception: {type(exc).__name__}: {exc}")


class OwnLoop(_Base):
    NOUS = {"engine": "nous", "auth": "subscription", "method": "hermes_local_proxy"}

    # SR-01: a refusal and the doctor must not disclose URL userinfo or parser text. SECRET-HANDLING.
    def test_sr01_proxy_url_secrets_are_not_echoed(self):
        with self.subTest("doctor_address"):
            out = own_loop.nous_proxy_doctor(f"http://u:{MARK}@127.0.0.1:8645/")
            self.assertNotIn(MARK, repr(out))
        for name, url in {"bad_scheme": f"ftp://u:{MARK}@127.0.0.1/", "bad_port": f"http://127.0.0.1:{MARK}/"}.items():
            with self.subTest(name):
                with self.assertRaises(own_loop.OwnLoopRouteRefused) as caught:
                    own_loop.check_nous_own_loop({**self.NOUS, "base_url": url}, {})
                self.assertNotIn(MARK, str(caught.exception))

    # SR-02: a command string is split into characters and launches executable "c".
    def test_sr02_string_argv_is_refused(self):
        bindir = self.root / "bin"
        bindir.mkdir(mode=0o700)
        exe = bindir / "c"
        exe.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        exe.chmod(0o700)
        route = {"engine": "copilot", "auth": "subscription", "method": "copilot_cli_sign_in"}
        with mock.patch("subprocess.Popen") as popen:
            try:
                own_loop.spawn_copilot_own_loop(route, {"PATH": str(bindir), "LANG": "C.UTF-8"}, "copilot",
                                                home=str(self.home), has_copilot_login=lambda: True)
            except Exception:  # noqa: BLE001 - any refusal before Popen is the desired outcome
                pass
            if popen.called:
                self.fail(f"Popen ran {popen.call_args.args[0]!r} from argv='copilot'")

    # SR-03: the doctor that promises never to raise does raise on a malformed user inventory.
    def test_sr03_doctor_never_raises(self):
        for users in (7, [["alice"]], ["alice", 7]):
            with self.subTest(users=users):
                self.call(own_loop.nous_proxy_doctor, "http://127.0.0.1:8645/v1", interactive_users=users)

    # F2 / F7: a refusal address is scheme and host only, or empty. Never the caller's string.
    def test_sr01_refused_address_drops_the_caller_string(self):
        secret = "S3KRET-PW"
        with self.subTest("missing_slashes"):
            out = own_loop.nous_proxy_doctor(f"http:u:{secret}@127.0.0.1:8645")
            self.assertEqual(out["address"], "")
            self.assertNotIn(secret, repr(out))
        with self.subTest("query_on_bad_scheme"):
            out = own_loop.nous_proxy_doctor(f"ftp://127.0.0.1/?token={secret}")
            self.assertEqual(out["address"], "ftp://127.0.0.1")
            self.assertEqual(out["cause"], "proxy_address_malformed")
            self.assertNotIn(secret, repr(out))
        with self.subTest("path_on_non_loopback"):
            out = own_loop.nous_proxy_doctor(f"http://10.1.2.3:8645/{secret}")
            self.assertEqual(out["ok"], False)
            self.assertEqual(out["cause"], "proxy_not_loopback")
            self.assertEqual(out["address"], "http://10.1.2.3:8645")
            self.assertNotIn(secret, repr(out))
        with self.subTest("bad_port"):
            out = own_loop.nous_proxy_doctor(f"http://127.0.0.1:{MARK}/")
            self.assertEqual(out["address"], "")
            self.assertNotIn(MARK, repr(out))
        with self.subTest("hostless_userinfo"):
            out = self.call(own_loop.nous_proxy_doctor, "http://u:pw@:8645/")
            self.assertEqual(out["address"], "")
            self.assertNotIn("pw", repr(out))

    # F3: a control character in the address is a refusal, and it is not a second line.
    def test_sr01_control_character_address_is_refused(self):
        injected = "http://127.0.0.1:8645/v1\nX-Injected: yes"
        out = own_loop.nous_proxy_doctor(injected)
        self.assertEqual(out["ok"], False)
        self.assertEqual(out["cause"], "proxy_address_malformed")
        self.assertNotIn("\n", out["address"])
        self.assertNotIn("X-Injected", repr(out))
        refused = own_loop.nous_proxy_doctor("ftp://127.0.0.1/\nX-Injected: yes")
        self.assertNotIn("\n", refused["address"])
        self.assertNotIn("X-Injected", repr(refused))

    # F1: an inventory that raises, and an address whose strip() raises, stay inside the doctor.
    def test_sr03_inventory_and_address_errors_stay_inside_the_doctor(self):
        class Boom(list):
            def __iter__(self):
                raise RuntimeError("inventory failed")

        class Stop(list):
            def __iter__(self):
                raise KeyboardInterrupt

        class BadStr(str):
            def strip(self, chars=None):
                raise RuntimeError("strip broke")

        with self.subTest("runtime"):
            out = self.call(own_loop.nous_proxy_doctor, "http://127.0.0.1:8645/v1", interactive_users=Boom())
            self.assertEqual(out["ok"], True)
            self.assertTrue(out["warnings"][0].startswith("host users unknown"))
            self.assertNotIn("inventory failed", repr(out))
        with self.subTest("strip"):
            out = self.call(own_loop.nous_proxy_doctor, BadStr("http://127.0.0.1:8645/v1"))
            self.assertEqual(out["ok"], False)
            self.assertEqual(out["address"], "")
            self.assertNotIn("strip broke", repr(out))
        with self.subTest("interrupt"):
            with self.assertRaises(KeyboardInterrupt):
                own_loop.nous_proxy_doctor("http://127.0.0.1:8645/v1", interactive_users=Stop())

    # F1 / F4 / F7: a string, a one-shot iterator, and an over-long list are not a clean host.
    def test_sr03_inventory_shape_is_not_a_clean_host(self):
        url = "http://127.0.0.1:8645/v1"
        with self.subTest("string"):
            out = self.call(own_loop.nous_proxy_doctor, url, interactive_users="alice")
            self.assertTrue(out["warnings"][0].startswith("host users unknown"))
            self.assertNotIn("a, c, e, i, l", out["warnings"][0])
        with self.subTest("generator"):
            def names():
                yield "alice"
            out = own_loop.nous_proxy_doctor(url, current_user="u", interactive_users=names())
            self.assertTrue(out["warnings"][0].startswith("host users unknown"))
            self.assertNotIn("alice", out["warnings"][0])
        with self.subTest("list_twice"):
            users = ["alice"]
            for _ in range(2):
                out = own_loop.nous_proxy_doctor(url, current_user="u", interactive_users=users)
                self.assertIn("alice", out["warnings"][0])
        with self.subTest("over_cap"):
            out = own_loop.nous_proxy_doctor(url, current_user="u", interactive_users=[f"user{i}" for i in range(10_001)])
            self.assertTrue(out["warnings"][0].startswith("host users unknown"))
        with self.subTest("name_newline"):
            out = own_loop.nous_proxy_doctor(url, current_user="u", interactive_users=["alice\nX-Injected: yes"])
            self.assertNotIn("\n", out["warnings"][0])
            self.assertNotIn("\r", out["warnings"][0])
            self.assertNotIn("X-Injected", out["warnings"][0])

    # F5 / F7: empty argv, a NUL argument, and a non-string item are one refusal type.
    def test_sr02_empty_nul_and_non_string_argv_are_one_refusal(self):
        route = {"engine": "copilot", "auth": "subscription", "method": "copilot_cli_sign_in"}
        parent = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        shapes = {"empty": [], "nul": ["copilot", "a\x00b"], "non_str": ["copilot", 1]}
        for name, argv in shapes.items():
            with self.subTest(name):
                with self.assertRaises(own_loop.OwnLoopRouteRefused) as caught:
                    own_loop.spawn_copilot_own_loop(route, parent, argv, home=str(self.home),
                                                    has_copilot_login=lambda: True)
                self.assertEqual(caught.exception.cause, "argv_malformed")


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

    # SR-05: refusals echo an invalid adopt flag and a provider that contains a raw newline.
    def test_sr05_refusal_text_does_not_echo_values(self):
        hermes = {"provider": "anthropic", "auth": "api_key", "credential_env": "ANTHROPIC_API_KEY",
                  HERMES_ADOPT_EXTERNAL_LOGINS: MARK}
        with self.subTest("adopt_flag_value"):
            with self.assertRaises(SubscriptionRouteRefused) as caught:
                check_route("hermes", hermes, {}, route_home=str(self.home), wrapper=self.wrapper)
            self.assertNotIn(MARK, str(caught.exception))
        with self.subTest("newline_provider"):
            with self.assertRaises(SubscriptionRouteRefused) as caught:
                check_route("opencode", {"provider": "bad\nforged", "auth": "api_key",
                                         "credential_env": "OPENAI_API_KEY"}, {}, route_home=str(self.home))
            self.assertNotIn("\n", str(caught.exception))
            self.assertEqual(caught.exception.detail, "")

    # F6 / F7: line and terminal controls are not echoed from provider, auth, or credential.
    def test_sr05_control_characters_are_not_a_detail(self):
        home = str(self.home)

        def refuse(profile):
            with self.assertRaises(SubscriptionRouteRefused) as caught:
                check_route("opencode", profile, {}, route_home=home)
            self.assertEqual(caught.exception.detail, "")
            self.assertNotIn("forged", str(caught.exception))
            self.assertNotIn("X: y", str(caught.exception))
            return caught.exception

        for name, provider in {
            "cr": "bad\rforged",
            "nul": "bad\x00forged",
            "vt": "bad\x0bforged",
            "ff": "bad\x0cforged",
            "line_sep": "bad\u2028forged",
            "esc": "bad\x1b[31mforged",
        }.items():
            with self.subTest(name):
                exc = refuse({"provider": provider, "auth": "api_key", "credential_env": "OPENAI_API_KEY"})
                self.assertEqual(exc.cause, "provider_not_allowed")
        with self.subTest("auth"):
            exc = refuse({"provider": "openai", "auth": "sign_in_with_chatgpt\nX: y",
                          "credential_env": "OPENAI_API_KEY"})
            self.assertEqual(exc.cause, "auth_not_api_key")
            self.assertNotIn("\n", str(exc))
        with self.subTest("credential"):
            exc = refuse({"provider": "openai", "auth": "api_key", "credential_env": "OPENAI_API_KEY\nX: y"})
            self.assertEqual(exc.cause, "credential_env_not_allowed")
            self.assertNotIn("\n", str(exc))
        with self.subTest("claude_provider"):
            exc = refuse({"provider": "claude-code\nx: y", "auth": "api_key",
                          "credential_env": "OPENAI_API_KEY"})
            self.assertEqual(exc.cause, "claude_subscription_provider")
            self.assertNotIn("\n", str(exc))
        with self.subTest("unknown_field"):
            exc = refuse({"provider": "openai", "auth": "api_key", "credential_env": "OPENAI_API_KEY",
                          "bad\nfield": "x"})
            self.assertEqual(exc.cause, "profile_unknown_field")
            self.assertNotIn("\n", str(exc))
        with self.subTest("model"):
            exc = refuse({"provider": "openai", "auth": "api_key", "credential_env": "OPENAI_API_KEY",
                          "model": "bad\nforged/name"})
            self.assertEqual(exc.cause, "model_provider_mismatch")
            self.assertNotIn("\n", str(exc))


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
