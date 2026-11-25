"""Which release ``plugin install`` takes, which ids it refuses, and what its result and install.json say.

Only a release under ``registry/`` can be installed: ``examples/`` is checked, never indexed and never
installed (the marketplace format, Layout). A plugin id that names a file of the profile itself is
refused before anything is written. Every good install.v1 fixture is read and every bad one refused,
and a grant's ``developer_key_id`` is written back as it was read. The result's ``order`` says what the
written install.json gives the plugin, and the result carries the chosen release's status and reason.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import unittest

from recursion_core.plugin_security.tests.support import PLUGIN_SOURCE
from recursion_core.plugin_security.tests.test_plugin_cli_install import (
    LINUX, LINUX_ONLY, ProfileCase, _entry, _manifest, _sha,
)

FIXTURES = Path(__file__).resolve().parents[4] / "schemas" / "plugin" / "v1" / "fixtures"


@unittest.skipUnless(LINUX, LINUX_ONLY)
class ReleaseChoiceTests(ProfileCase):
    def write_example(self, plugin_id, *, version="1.0.0", source=b"EXAMPLE = True\n", example=True):
        manifest = _manifest(source, plugin_id, developer_key="11" * 32, version=version)
        entry = _entry(plugin_id, version, manifest, source)
        entry["example"] = example
        folder = self.registry / "examples" / plugin_id / version
        folder.mkdir(parents=True, mode=0o700)
        (folder / "manifest.json").write_bytes(manifest)
        (folder / "plugin.py").write_bytes(source)
        (self.registry / "examples" / plugin_id / "entry.json").write_text(
            json.dumps(entry, indent=2, sort_keys=True) + "\n")

    def write_live_install(self, data):
        live = self.config / "plugins"
        live.mkdir(parents=True, mode=0o700)
        os.chmod(self.config, 0o700)
        os.chmod(self.config.parent, 0o700)
        path = live / "install.json"
        path.write_bytes((json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8"))
        os.chmod(path, 0o600)
        from recursion_core.daemon.tests import plugin_fixture as fx
        fx.ensure_revocation_store(self.state)

    def assert_refused_and_nothing_written(self, code, plugin_id="fixture.unsigned", **kwargs):
        from recursion_core.plugin_security.install import InstallRefused

        with self.assertRaises(InstallRefused) as caught:
            self.install(plugin_id, **kwargs)
        self.assertEqual(code, caught.exception.code)
        self.assertFalse((self.config / "plugins").exists())
        self.assertFalse((self.config / "plugin-generations").exists())

    # (P2) examples/ is never a source of an install.

    def test_an_id_only_under_examples_is_not_in_the_registry(self):
        self.write_registry()
        self.write_example("fixture.example")
        self.assertEqual([], self.codes_of(self.registry))
        self.assert_refused_and_nothing_written("plugin_not_in_registry", "fixture.example")

    def test_a_version_only_under_examples_is_not_in_the_registry(self):
        self.write_registry()
        self.write_example("fixture.unsigned", version="2.0.0")
        self.assertEqual([], self.codes_of(self.registry))
        self.assert_refused_and_nothing_written("release_not_in_registry", version="2.0.0")

    def test_the_same_id_in_both_areas_installs_the_registry_bytes(self):
        registry_source = b"FROM_REGISTRY = True\n"
        self.write_registry(source=registry_source)
        self.write_example("fixture.unsigned", source=b"FROM_EXAMPLES = True\n")
        self.assertEqual([], self.codes_of(self.registry))
        result = self.install()
        self.assertEqual("1.0.0", result["version"])
        installed = self.config / "plugins" / "fixture.unsigned" / "plugin.py"
        self.assertEqual(registry_source, installed.read_bytes())

    def test_an_error_under_examples_still_refuses_the_install(self):
        self.write_registry()
        self.write_example("fixture.example", example=False)
        self.assertEqual(["entry_example_flag"], self.codes_of(self.registry))
        self.assert_refused_and_nothing_written("entry_example_flag")

    # (P7) a plugin id that the profile itself uses.

    def test_a_plugin_id_the_profile_uses_is_refused_before_any_write(self):
        self.write_registry("install.json")
        self.assertEqual([], self.codes_of(self.registry))
        self.assert_refused_and_nothing_written("plugin_id_reserved", "install.json")
        self.assertFalse(self.config.exists())

    # (P9) the pinned install.v1 fixtures, and developer_key_id kept.

    def test_every_good_install_fixture_is_read_and_every_bad_one_refused(self):
        from recursion_core.plugin_security.install import InstallRefused

        good = sorted((FIXTURES / "good" / "install.v1").glob("*.json"))
        bad = sorted((FIXTURES / "bad" / "install.v1").glob("*.json"))
        self.assertTrue(good and bad)
        for path in good + bad:
            with self.subTest(fixture=f"{path.parent.parent.name}/{path.name}"):
                if self.config.parent.exists():
                    shutil.rmtree(self.config.parent)
                self.write_live_install(json.loads(path.read_text(encoding="utf-8")))
                if path in good:
                    self.assertEqual([], self.listed()["plugins"])
                else:
                    with self.assertRaises(InstallRefused) as caught:
                        self.listed()
                    self.assertEqual("install_unreadable", caught.exception.code)

    def test_a_developer_key_id_that_was_read_is_written_back_unchanged(self):
        data = json.loads((FIXTURES / "good" / "install.v1" / "h03-manual-grant-to-an-unsigned-plugin.json")
                          .read_text(encoding="utf-8"))
        other = dict(data["grants"][0], plugin_id="other.desk", developer_key_id="ed25519:" + "ab" * 16)
        plain = {key: value for key, value in data["grants"][0].items() if key != "developer_key_id"}
        plain["plugin_id"] = "plain.desk"
        data["grants"] += [other, plain]
        self.write_live_install(data)
        self.write_registry()
        self.install()
        written = json.loads((self.config / "plugins" / "install.json").read_text(encoding="utf-8"))
        grants = {item["plugin_id"]: item for item in written["grants"]}
        self.assertIsNone(grants["paper-desk"]["developer_key_id"])
        self.assertIn("developer_key_id", grants["paper-desk"])
        self.assertEqual("ed25519:" + "ab" * 16, grants["other.desk"]["developer_key_id"])
        self.assertNotIn("developer_key_id", grants["plain.desk"])
        from recursion_core.daemon import plugins
        self.assertEqual({"paper-desk", "other.desk", "plain.desk"},
                         set(plugins.read_install(self.config / "plugins")["grants"]))

    # (P10) the result's order field.

    def test_a_kept_manual_order_grant_gives_order_granted(self):
        self.write_registry(permissions=("read", "order"))
        self.write_live_install({"grants": [{
            "plugin_id": "fixture.unsigned", "artifact_sha256": _sha(PLUGIN_SOURCE), "signer_key_id": None,
            "permissions": ["read", "order"], "unsigned_order_acknowledged": True,
        }]})
        result = self.install()
        self.assertEqual("granted", result["order"])
        from recursion_core.daemon import plugins
        grant = plugins.read_install(self.config / "plugins")["grants"]["fixture.unsigned"]
        self.assertIn("order", grant.permissions)

    def test_without_a_grant_order_is_not_granted(self):
        self.write_registry(permissions=("read", "order"))
        self.assertEqual("not granted", self.install()["order"])

    # (P11, the least) the chosen release's status and status_reason.

    def test_a_withdrawn_release_named_by_version_shows_its_status_and_reason(self):
        self.write_registry(version="0.9.0", release_over={"status": "withdrawn",
                                                           "status_reason": "A made-up reason to withdraw."})
        self.assert_refused_and_nothing_written("release_not_active")
        result = self.install(version="0.9.0")
        self.assertEqual(("withdrawn", "A made-up reason to withdraw."),
                         (result["status"], result["status_reason"]))
        from recursion_core.plugin_security._entrypoints import main
        out, err = io.StringIO(), io.StringIO()
        status = main(["plugin", "install", "--registry", str(self.registry), "--plugin", "fixture.unsigned",
                       "--version", "0.9.0", "--config", str(self.config), "--state", str(self.state)], out, err)
        self.assertEqual(0, status, err.getvalue())
        printed = json.loads(out.getvalue())
        self.assertEqual(("withdrawn", "A made-up reason to withdraw."),
                         (printed["status"], printed["status_reason"]))

    def test_an_active_release_shows_active_and_no_reason(self):
        self.write_registry()
        result = self.install()
        self.assertEqual(("active", None), (result["status"], result["status_reason"]))


if __name__ == "__main__":
    unittest.main()
