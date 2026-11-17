"""S7 triage (routes-core): witnesses for confinement and refusal checks in subscription_routes/.

SR-01, SR-02, SR-03, SR-05, SR-06, SR-07, SR-10 and SR-11. Scratch homes and packages only;
no engine, no network, no credential (every key value is a fake string).
"""

from __future__ import annotations

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
        with self.assertRaises(SubscriptionRouteRefused):
            self.chatgpt()

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


if __name__ == "__main__":
    unittest.main()
