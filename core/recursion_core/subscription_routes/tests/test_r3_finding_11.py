"""R3 regression: dummy data only; no provider, network or operator credential use.

Each refusal test first shows that the same fixture without the defect passes the guard, then asserts the exact
cause and detail, so an unrelated refusal (for example ``route_home_is_operator_home``) cannot pass it. The fixture
controls the passwd home lookup, so the result does not depend on where TMPDIR is.
"""
import os
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from recursion_core.subscription_routes import policy as route_policy


class PrivateHome(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="r3-regression-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.root.chmod(0o700)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.operator = self.root / "operator"
        self.operator.mkdir(mode=0o700)
        # The only operator home is this fixture's own folder, whatever the real passwd entry says.
        if route_policy.pwd is not None:
            passwd = types.SimpleNamespace(getpwuid=lambda _uid: types.SimpleNamespace(pw_dir=str(self.operator)))
            patcher = mock.patch.object(route_policy, "pwd", passwd)
            patcher.start()
            self.addCleanup(patcher.stop)

from recursion_core.subscription_routes import (SubscriptionRouteRefused, WrapperInstall, WrapperPinRefused,
    check_route, check_cursor_route, measure_package_pin)

ROUTE = {"provider": "openai", "auth": "api_key", "credential_env": "OPENAI_API_KEY", "model": "openai/m"}


@unittest.skipIf(os.name == "nt", "POSIX route homes and modes")
class StorePrefixTest(PrivateHome):
    def opencode_route(self):
        return check_route("opencode", ROUTE, {"PATH": "/usr/bin", "HOME": str(self.home), "OPENAI_API_KEY": "FAKE-key"},
                           route_home=str(self.home), parent_env={"HOME": str(self.operator)},
                           operator_home=str(self.operator))

    def cursor_route(self, wrapper, env=None):
        return check_cursor_route(env if env is not None else {"PATH": "/usr/bin", "HOME": str(self.home)},
                                  route_home=str(self.home), wrapper=wrapper,
                                  parent_env={"HOME": str(self.operator)}, operator_home=str(self.operator))

    def package(self, name="package"):
        package = self.root / name
        package.mkdir(mode=0o700)
        script = package / "launcher.js"
        script.write_text("// dummy launcher")
        script.chmod(0o700)
        return package, script

    def assertRefusal(self, exc_type, call, cause, detail=None):
        with self.assertRaises(exc_type) as caught:
            call()
        self.assertEqual(caught.exception.cause, cause)
        if detail is not None:
            self.assertEqual(caught.exception.detail, detail)

    def test_store_parent_link_is_refused_even_when_target_leaf_is_absent(self):
        self.opencode_route()  # the clean fixture passes the guard
        target = self.operator / ".config"
        target.mkdir()
        (target / "dummy-unrelated-store").mkdir()
        (self.home / ".config").symlink_to(target)
        self.assertRefusal(SubscriptionRouteRefused, self.opencode_route, "own_store_is_link", ".config")

    def test_group_writable_package_is_refused_at_route_verification(self):
        package, script = self.package()
        wrapper = WrapperInstall(str(script), str(package), measure_package_pin(str(script), str(package)))
        self.cursor_route(wrapper)  # the clean fixture passes the guard
        for path, detail in ((package, ".: value_chain_unsafe"), (script, "launcher.js: value_file_unsafe")):
            with self.subTest(path=path.name):
                path.chmod(0o777)
                try:
                    self.assertRefusal((SubscriptionRouteRefused, WrapperPinRefused), lambda: self.cursor_route(wrapper),
                                       "wrapper_path_unsafe", detail)
                finally:
                    path.chmod(0o700)

    def test_empty_writable_package_directory_is_refused(self):
        package, script = self.package()
        empty = package / "node_modules"
        empty.mkdir(mode=0o700)
        wrapper = WrapperInstall(str(script), str(package), measure_package_pin(str(script), str(package)))
        env = {"HOME": str(self.home)}
        self.cursor_route(wrapper, env)  # the clean fixture passes the guard
        empty.chmod(0o777)
        self.assertRefusal((SubscriptionRouteRefused, WrapperPinRefused), lambda: self.cursor_route(wrapper, env),
                           "wrapper_path_unsafe", "node_modules: value_chain_unsafe")

    def test_malformed_install_roots_keep_the_typed_shape_refusal(self):
        from recursion_core.subscription_routes.wrapper_pin import verify_package_pin
        package, script = self.package()
        pin = measure_package_pin(str(script), str(package))
        for root in (None, "relative"):
            with self.subTest(root=root), self.assertRaises(WrapperPinRefused) as caught:
                verify_package_pin("cursor", WrapperInstall(str(script), root, pin))
            self.assertEqual(caught.exception.cause, "install_root_not_absolute")

    def test_writable_directory_symlink_alias_is_refused_before_launcher(self):
        from recursion_core.subscription_routes import guard_cursor_then_spawn

        package, pinned_launcher = self.package("owned-package")
        wrapper = WrapperInstall(
            str(pinned_launcher), str(package), measure_package_pin(str(pinned_launcher), str(package))
        )
        child_env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home)}
        kwargs = dict(route_home=str(self.home), wrapper=wrapper, parent_env={"HOME": str(self.operator)},
                      operator_home=str(self.operator))

        clean = mock.Mock()
        guard_cursor_then_spawn(clean, [str(pinned_launcher)], child_env, **kwargs)  # the pinned spelling launches
        clean.assert_called_once()

        writable_bin = self.root / "writable-bin"
        writable_bin.mkdir(mode=0o777)
        writable_bin.chmod(0o777)
        alias = writable_bin / "cursor-alias"
        alias.symlink_to(pinned_launcher)

        launcher = mock.Mock()
        self.assertRefusal(SubscriptionRouteRefused,
                           lambda: guard_cursor_then_spawn(launcher, [str(alias)], child_env, **kwargs),
                           "argv0_not_pinned")
        launcher.assert_not_called()
