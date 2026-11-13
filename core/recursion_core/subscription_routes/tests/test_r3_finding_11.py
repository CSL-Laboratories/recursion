"""R3 regression: dummy data only; no provider, network or operator credential use."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

class PrivateHome(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="r3-regression-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.root.chmod(0o700)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.operator = self.root / "operator"
        self.operator.mkdir(mode=0o700)

from recursion_core.subscription_routes import (SubscriptionRouteRefused, WrapperInstall, WrapperPinRefused,
    check_route, check_cursor_route, measure_package_pin)

@unittest.skipIf(os.name == "nt", "POSIX route homes and modes")
class StorePrefixTest(PrivateHome):
    def test_store_parent_link_is_refused_even_when_target_leaf_is_absent(self):
        target = self.operator / ".config"
        target.mkdir()
        (target / "dummy-unrelated-store").mkdir()
        (self.home / ".config").symlink_to(target)
        with self.assertRaises(SubscriptionRouteRefused):
            check_route("opencode",{"provider":"openai","auth":"api_key","credential_env":"OPENAI_API_KEY","model":"openai/m"},
                {"PATH":"/usr/bin","HOME":str(self.home),"OPENAI_API_KEY":"FAKE-key"},route_home=str(self.home),
                parent_env={"HOME":str(self.operator)},operator_home=str(self.operator))

    def test_group_writable_package_is_refused_at_route_verification(self):
        package = self.root / "package"
        package.mkdir(mode=0o700)
        script = package / "launcher.js"
        script.write_text("// dummy launcher")
        pin = measure_package_pin(str(script),str(package))
        for path in (package,script):
            with self.subTest(path=path.name):
                path.chmod(0o777)
                try:
                    with self.assertRaises((SubscriptionRouteRefused,WrapperPinRefused)):
                        check_cursor_route({"PATH":"/usr/bin","HOME":str(self.home)},route_home=str(self.home),
                            wrapper=WrapperInstall(str(script),str(package),pin),parent_env={"HOME":str(self.operator)},operator_home=str(self.operator))
                finally:
                    path.chmod(0o700)

    def test_empty_writable_package_directory_is_refused(self):
        package = self.root / "package"
        package.mkdir(mode=0o700)
        script = package / "launcher.js"
        script.write_text("// dummy launcher")
        empty = package / "node_modules"
        empty.mkdir(mode=0o777)
        empty.chmod(0o777)
        pin = measure_package_pin(str(script), str(package))
        with self.assertRaises((SubscriptionRouteRefused, WrapperPinRefused)):
            check_cursor_route({"HOME":str(self.home)}, route_home=str(self.home),
                wrapper=WrapperInstall(str(script),str(package),pin),
                parent_env={"HOME":str(self.operator)},operator_home=str(self.operator))

    def test_malformed_install_roots_keep_the_typed_shape_refusal(self):
        from recursion_core.subscription_routes.wrapper_pin import verify_package_pin
        package = self.root / "package"
        package.mkdir(mode=0o700)
        script = package / "launcher.js"
        script.write_text("// dummy launcher")
        pin = measure_package_pin(str(script),str(package))
        for root in (None, "relative"):
            with self.subTest(root=root), self.assertRaises(WrapperPinRefused) as caught:
                verify_package_pin("cursor",WrapperInstall(str(script),root,pin))
            self.assertEqual(caught.exception.cause,"install_root_not_absolute")

    def test_writable_directory_symlink_alias_is_refused_before_launcher(self):
        from recursion_core.subscription_routes import guard_cursor_then_spawn

        package = self.root / "owned-package"
        package.mkdir(mode=0o700)
        pinned_launcher = package / "launcher.js"
        pinned_launcher.write_text("// safe dummy launcher\n", encoding="ascii")
        pinned_launcher.chmod(0o700)
        wrapper = WrapperInstall(
            str(pinned_launcher), str(package), measure_package_pin(str(pinned_launcher), str(package))
        )

        writable_bin = self.root / "writable-bin"
        writable_bin.mkdir(mode=0o777)
        writable_bin.chmod(0o777)
        alias = writable_bin / "cursor-alias"
        alias.symlink_to(pinned_launcher)

        launcher = mock.Mock()
        child_env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home)}
        with self.assertRaises(SubscriptionRouteRefused):
            guard_cursor_then_spawn(
                launcher, [str(alias)], child_env,
                route_home=str(self.home), wrapper=wrapper,
                parent_env={"HOME": str(self.operator)}, operator_home=str(self.operator),
            )
        launcher.assert_not_called()
