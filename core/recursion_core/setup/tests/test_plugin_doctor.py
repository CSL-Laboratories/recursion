"""``recursiond doctor --json`` reports each installed plugin and does not run it.

A profile with one signed and one unsigned fixture plugin. Before this draft the report
lists no plugin. After it, both are listed with trust and a registry label when the
signature backend can classify them. With the plugin config folder made group-writable,
every plugin is reported refused. The revocation store is reported on its own: a store
that is gone after its folder exists refuses every plugin. No plugin code runs.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import io
import json
import os
import shutil
import sqlite3
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
    """Run ``doctor --json`` with account probes bound to ``env["HOME"]``.

    The unit, linger and user-manager probes take that directory. A call that
    reads the password database, or that runs loginctl or systemctl, fails the test.

    On a non-Linux platform the command refuses before the doctor. The plugin
    checks still run, through ``doctor.run`` with the same root. The command's
    refusal is left as it is. The password-database tripwire is installed only
    when ``pwd`` can be imported: a host that has hidden it cannot be asked to
    import it.
    """
    import subprocess
    import sys

    import recursion_core.daemon as daemon_module
    import recursion_core.setup.doctor as doctor

    root = env["HOME"]
    real_run = doctor.run
    real_subprocess_run = subprocess.run

    def run_at_root(probes=None):
        return real_run(probes, root=root)

    def refuse_pwd(*_args, **_kwargs):
        raise AssertionError("pwd.getpwuid was called")

    def guard_subprocess(*args, **kwargs):
        command = args[0] if args else kwargs.get("args", ())
        if isinstance(command, (list, tuple)) and command:
            name = os.path.basename(str(command[0]))
            if name in ("loginctl", "systemctl"):
                raise AssertionError(f"{name} was called")
        return real_subprocess_run(*args, **kwargs)

    def refuse_linger(*_args, **_kwargs):
        raise AssertionError("loginctl linger probe was called")

    try:
        import pwd as pwd_module
    except ImportError:
        pwd_module = None

    out, err = io.StringIO(), io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.dict(os.environ, env, clear=False))
        stack.enter_context(mock.patch("subprocess.run", guard_subprocess))
        stack.enter_context(mock.patch.object(daemon_module, "check_operator_linger", refuse_linger))
        if pwd_module is not None:
            stack.enter_context(mock.patch.object(pwd_module, "getpwuid", refuse_pwd))
        if sys.platform.startswith("linux"):
            stack.enter_context(mock.patch.object(doctor, "run", run_at_root))
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
        else:
            report = real_run(None, root=root)
    try:
        _validate(report)
    except Exception as exc:
        cause = exc.__cause__
        # A pretended win32 platform makes this host's ssl call enum_certificates,
        # which was not imported. jsonschema then cannot open the local schema.
        # The plain report rules already ran. Linux keeps the full schema check.
        if not (
            not sys.platform.startswith("linux")
            and type(exc).__name__ == "_RefResolutionError"
            and isinstance(cause, NameError)
            and "enum_certificates" in str(cause)
        ):
            raise
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
        if hasattr(os, "geteuid"):
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
        else:
            # Owner checks call os.geteuid. This host has none, so the doctor
            # must not classify a plugin as trust signed. The mode-bit refusal
            # below still runs: that check does not need geteuid.
            self.assertNotIn("trust signed", json.dumps(report))
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

        try:
            stores.create_revocation_store(store)
        except AttributeError as exc:
            # The publish step opens the folder with os.O_DIRECTORY. A host
            # without that flag raises here, after the store file is linked.
            # The doctor still reads that file. A host that has the flag is
            # not excused.
            self.assertFalse(hasattr(os, "O_DIRECTORY"))
            self.assertIn("O_DIRECTORY", str(exc))
            self.assertTrue(os.path.isfile(store))
        present = _doctor_json(self.env())
        self.assertEqual(_rows(present, "plugin.revocation")[0]["state"], "ok")
        self.assertEqual(_rows(present, "plugin.installed"), [])

        os.unlink(store)
        # A normal create then close leaves no side file (measured: the directory
        # is empty after the main file is removed). The lost-file signal is a
        # leftover side file of revoked.sqlite3 itself, the same suffixes
        # stores.py already checks. The breaker file is not that signal.
        side = store + "-journal"
        fd = os.open(side, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
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
        self.assertFalse(os.path.lexists(store))
        self.assertTrue(os.path.lexists(side))

    def signed_plugin(self):
        """One signed plugin under the temp config root. No plugin code is the source."""

        key = None
        if backend_available():
            from recursion_core.plugin_security.tests.support import Key

            key = Key()
        config = os.path.join(self.config_home, "recursion")
        trusted = {}
        grants = []
        if key is not None:
            trusted["dev"] = key.public
            grants.append(fx.grant_for(
                self.source, key=key, plugin_id="fixture.signed", permissions=("read",)))
        else:
            grants.append(fx.grant_for(self.source, plugin_id="fixture.signed", permissions=("read",)))
        plugins = fx.write_install(config, trusted=trusted, grants=grants)
        if key is not None:
            fx.write_plugin(plugins / "fixture.signed", self.source, key=key,
                            plugin_id="fixture.signed", permissions=("read",))
        else:
            fx.write_plugin(plugins / "fixture.signed", self.source, plugin_id="fixture.signed",
                            permissions=("read",), signature="ab" * 64)
        return plugins

    def legacy_store(self, mode=0o600):
        """A v0 revocation store: magic set, ``revoked(key_id)`` only, journal mode DELETE."""

        parent = os.path.join(self.state_home, "recursion")
        _mkdir(parent)
        store_dir = os.path.join(parent, "plugin-security")
        _mkdir(store_dir)
        path = os.path.join(store_dir, "revoked.sqlite3")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        db = sqlite3.connect(path, isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
            db.execute("CREATE TABLE revoked (key_id TEXT PRIMARY KEY)")
            db.execute("INSERT INTO meta VALUES ('magic', 'csl-revocation-store/v0')")
        finally:
            db.close()
        os.chmod(path, mode)
        return store_dir, path

    def store_facts(self, store_dir, path):
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            digest.update(handle.read())
        uri = Path(path).as_uri() + "?mode=rw"
        db = sqlite3.connect(uri, uri=True, isolation_level=None)
        try:
            db.execute("PRAGMA query_only=ON")
            columns = [row[1] for row in db.execute("PRAGMA table_info(revoked)")]
        finally:
            db.close()
        return digest.hexdigest(), columns, sorted(os.listdir(store_dir))

    def assert_legacy_unchanged(self, store_dir, path, before, mode):
        after = self.store_facts(store_dir, path)
        self.assertEqual(after, before)
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), mode)
        report = _doctor_json(self.env())
        again = self.store_facts(store_dir, path)
        self.assertEqual(again, before)
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), mode)
        found = [row for row in report["checks"] if "revocation_store_legacy" in row["detail"]]
        self.assertTrue(found, report["checks"])
        for row in found:
            self.assertNotIn("revocation_store_invalid", row["detail"])
        for row in _rows(report, "plugin.installed"):
            self.assertNotIn("trust signed", row["detail"])
            self.assertIn("revocation_store_legacy", row["detail"])
        return report

    def test_legacy_revocation_store_is_reported_and_not_migrated(self):
        store_dir, path = self.legacy_store()
        before = self.store_facts(store_dir, path)
        self.signed_plugin()
        self.assert_legacy_unchanged(store_dir, path, before, 0o600)

    def test_mode_0400_legacy_store_is_reported_and_not_rewritten(self):
        store_dir, path = self.legacy_store(0o400)
        before = self.store_facts(store_dir, path)
        self.signed_plugin()
        self.assert_legacy_unchanged(store_dir, path, before, 0o400)

    def test_breaker_directory_without_a_revocation_file_is_not_lost(self):
        from recursion_core.daemon import PlatformUnsupported
        from recursion_core.daemon.breaker import open_breaker_store

        state = os.path.join(self.state_home, "recursion")
        try:
            stored, _created = open_breaker_store(state, clock=lambda: 0)
        except PlatformUnsupported as refusal:
            # The launcher refuses before any directory on a non-Linux host.
            # That refusal is the result. The directory assertions below stay
            # on a host where the launcher opens the store.
            text = str(refusal)
            self.assertIn("platform_unsupported", text)
            self.assertIn("Linux", text)
            return
        stored.close()
        store_dir = os.path.join(state, "plugin-security")
        self.assertTrue(os.path.isfile(os.path.join(store_dir, "breaker.sqlite3")))
        self.assertFalse(os.path.lexists(os.path.join(store_dir, "revoked.sqlite3")))
        names_before = sorted(os.listdir(store_dir))
        self.signed_plugin()
        report = _doctor_json(self.env())
        for row in report["checks"]:
            self.assertNotIn("revocation_store_missing", row["detail"])
        self.assertFalse(os.path.lexists(os.path.join(store_dir, "revoked.sqlite3")))
        self.assertEqual(sorted(os.listdir(store_dir)), names_before)
        revocation = _rows(report, "plugin.revocation")[0]
        self.assertEqual(revocation["state"], "unknown")

    def test_symlinked_store_directory_refuses_every_plugin(self):
        target = os.path.join(self.tmp, "other-store")
        _mkdir(target)
        parent = os.path.join(self.state_home, "recursion")
        _mkdir(parent)
        link = os.path.join(parent, "plugin-security")
        os.symlink(target, link)
        self.signed_plugin()
        report = _doctor_json(self.env())
        revocation = _rows(report, "plugin.revocation")[0]
        self.assertEqual(revocation["state"], "fail")
        self.assertIn("revocation_store_untrusted", revocation["detail"])
        self.assertIn("plugin-security is a symlink", revocation["detail"])
        listed = _rows(report, "plugin.installed")
        self.assertEqual([row["subject"] for row in listed], ["fixture.signed"])
        for row in listed:
            self.assertNotIn("trust signed", row["detail"])
            self.assertEqual(row["state"], "fail")
        self.assertEqual(os.listdir(target), [])
        self.assertFalse(os.path.lexists(os.path.join(target, "revoked.sqlite3")))

    def test_a_vanished_revocation_file_is_not_replaced(self):
        from recursion_core.plugin_security.errors import Refused
        from recursion_core.plugin_security.manifest import TrustStore

        path = os.path.join(self.tmp, "revoked.sqlite3")
        kwargs = {"revocation_store": path}
        if "read_only" in inspect.signature(TrustStore.__init__).parameters:
            kwargs["read_only"] = True
        with mock.patch("recursion_core.plugin_security.manifest.os.path.isfile", return_value=True):
            with self.assertRaises(Refused):
                TrustStore({}, **kwargs)
        self.assertFalse(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
