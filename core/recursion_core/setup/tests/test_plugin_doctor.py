"""``recursiond doctor --json`` reports each installed plugin and does not run it.

A profile with one signed and one unsigned fixture plugin. Before this draft the report
lists no plugin. After it, both are listed with trust and a registry label when the
signature backend can classify them. With the plugin config folder made group-writable,
every plugin is reported refused. The revocation store is reported on its own: a store
that is gone after its folder exists refuses every plugin. No plugin code runs.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from recursion_core import _entrypoints as entry
from recursion_core.daemon.tests import plugin_fixture as fx
from recursion_core.plugin_security.signing import backend_available
from recursion_core.setup.tests.test_doctor import _validate


def _rows(report, check_id):
    return [row for row in report["checks"] if row["id"] == check_id]


def _doctor_json(env):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, env, clear=False):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = entry.main(["doctor", "--json"])
    text = out.getvalue()
    report = json.loads(text)
    if text != json.dumps(report, sort_keys=True) + "\n":
        raise AssertionError("doctor --json did not print one object")
    if err.getvalue() != "":
        raise AssertionError(err.getvalue())
    if code == 2:
        raise AssertionError("doctor is still an unknown command")
    _validate(report)
    return report


def _mkdir(path, mode=0o700):
    os.mkdir(path, mode)
    os.chmod(path, mode)


class PluginDoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="plugin-doctor-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)
        self.home = os.path.join(self.tmp, "home")
        self.config_home = os.path.join(self.home, ".config")
        self.state_home = os.path.join(self.home, ".local", "state")
        self.runtime = os.path.join(self.tmp, "runtime")
        self.empty_path = os.path.join(self.tmp, "bin")
        for path in (self.home, self.config_home, self.state_home, self.runtime, self.empty_path):
            if path == self.state_home:
                _mkdir(os.path.join(self.home, ".local"))
            _mkdir(path)
        self.marker = os.path.join(self.tmp, "plugin-code-ran")
        self.source = ("open(%r, 'w').close()\nVALUE = 1\n" % self.marker).encode()

    def env(self):
        return {
            "HOME": self.home,
            "XDG_CONFIG_HOME": self.config_home,
            "XDG_STATE_HOME": self.state_home,
            "XDG_RUNTIME_DIR": self.runtime,
            "PATH": self.empty_path,
        }

    def profile(self, *, signed_key):
        """One signed and one unsigned plugin under ``$XDG_CONFIG_HOME/recursion/plugins``."""

        config = os.path.join(self.config_home, "recursion")
        trusted = {}
        grants = [fx.grant_for(self.source, plugin_id="fixture.unsigned", permissions=("read",))]
        if signed_key is not None:
            trusted["dev"] = signed_key.public
            grants.insert(0, fx.grant_for(
                self.source, key=signed_key, plugin_id="fixture.signed", permissions=("read",)))
        plugins = fx.write_install(config, trusted=trusted, grants=grants)
        if signed_key is not None:
            fx.write_plugin(plugins / "fixture.signed", self.source, key=signed_key,
                            plugin_id="fixture.signed", permissions=("read",))
        else:
            fx.write_plugin(plugins / "fixture.signed", self.source, plugin_id="fixture.signed",
                            permissions=("read",), signature="ab" * 64)
        fx.write_plugin(plugins / "fixture.unsigned", self.source, plugin_id="fixture.unsigned",
                        permissions=("read",))
        self.plugins = plugins
        self.install_bytes = (plugins / "install.json").read_bytes()
        return plugins

    def test_doctor_json_lists_each_plugin_and_an_untrusted_folder_refuses_every_plugin(self):
        key = None
        if backend_available():
            from recursion_core.plugin_security.tests.support import Key

            key = Key()
        plugins = self.profile(signed_key=key)
        report = _doctor_json(self.env())
        listed = _rows(report, "plugin.installed")
        self.assertEqual(sorted(row["subject"] for row in listed), ["fixture.signed", "fixture.unsigned"])
        by_subject = {row["subject"]: row for row in listed}
        if key is not None:
            signed, unsigned = by_subject["fixture.signed"], by_subject["fixture.unsigned"]
            self.assertEqual(signed["state"], "ok")
            self.assertIn("version 1.0.0", signed["detail"])
            self.assertIn("trust signed", signed["detail"])
            self.assertIn("key_id ed25519:", signed["detail"])
            self.assertIn("label unlisted", signed["detail"])
            self.assertIn("signature_valid_trusted_key", signed["detail"])
            self.assertIn("registry_label", signed["detail"])
            self.assertEqual(unsigned["state"], "warn")
            self.assertIn("trust unsigned", unsigned["detail"])
            self.assertIn("key_id none", unsigned["detail"])
            self.assertIn("label unlisted", unsigned["detail"])
            self.assertIn("signer_not_on_trusted_list", unsigned["detail"])
        else:
            for row in listed:
                self.assertEqual(row["state"], "fail")
                self.assertIn("refused", row["detail"])
                self.assertIn("plugin_verification_unavailable", row["detail"])
        folder = _rows(report, "plugin.config")
        self.assertEqual(len(folder), 1)
        self.assertEqual(folder[0]["state"], "ok")
        self.assertFalse(os.path.exists(self.marker), "plugin code ran")
        self.assertEqual((plugins / "install.json").read_bytes(), self.install_bytes)

        os.chmod(plugins, 0o770)
        self.addCleanup(os.chmod, plugins, 0o700)
        refused = _doctor_json(self.env())
        again = _rows(refused, "plugin.installed")
        self.assertEqual(sorted(row["subject"] for row in again), ["fixture.signed", "fixture.unsigned"])
        for row in again:
            self.assertEqual(row["state"], "fail")
            self.assertIn("refused", row["detail"])
            self.assertIn("plugin_directory_untrusted", row["detail"])
        self.assertEqual(_rows(refused, "plugin.config")[0]["state"], "fail")
        self.assertIn("plugin_directory_untrusted", _rows(refused, "plugin.config")[0]["detail"])
        self.assertFalse(os.path.exists(self.marker), "plugin code ran after the folder was group-writable")
        self.assertEqual((plugins / "install.json").read_bytes(), self.install_bytes)
        for name in ("fixture.signed", "fixture.unsigned"):
            self.assertIn(b"plugin-code-ran", (plugins / name / "plugin.py").read_bytes())

    def test_revocation_store_is_reported_and_a_missing_store_refuses_every_plugin(self):
        report = _doctor_json(self.env())
        self.assertEqual(_rows(report, "plugin.revocation")[0]["state"], "unknown")
        self.assertEqual(_rows(report, "plugin.installed"), [])

        store_dir = os.path.join(self.state_home, "recursion", "plugin-security")
        _mkdir(os.path.join(self.state_home, "recursion"))
        _mkdir(store_dir)
        store = os.path.join(store_dir, "revoked.sqlite3")
        from recursion_core.plugin_security import stores

        stores.create_revocation_store(store)
        present = _doctor_json(self.env())
        self.assertEqual(_rows(present, "plugin.revocation")[0]["state"], "ok")
        self.assertEqual(_rows(present, "plugin.installed"), [])

        os.unlink(store)
        config = os.path.join(self.config_home, "recursion")
        plugins = fx.write_install(config, grants=[])
        for name in ("fixture.signed", "fixture.unsigned"):
            directory = plugins / name
            directory.mkdir(mode=0o700)
            os.chmod(directory, 0o700)
            artifact = directory / "plugin.py"
            artifact.write_bytes(self.source)
            os.chmod(artifact, 0o600)
        gone = _doctor_json(self.env())
        revocation = _rows(gone, "plugin.revocation")[0]
        self.assertEqual(revocation["state"], "fail")
        self.assertIn("revocation_store_missing", revocation["detail"])
        listed = _rows(gone, "plugin.installed")
        self.assertEqual(sorted(row["subject"] for row in listed), ["fixture.signed", "fixture.unsigned"])
        for row in listed:
            self.assertEqual(row["state"], "fail")
            self.assertIn("refused", row["detail"])
            self.assertIn("revocation_store_missing", row["detail"])
        self.assertFalse(os.path.exists(self.marker), "plugin code ran without a revocation store")
        mode = stat.S_IMODE(os.lstat(plugins).st_mode)
        self.assertFalse(mode & stat.S_IWGRP)


if __name__ == "__main__":
    unittest.main()
