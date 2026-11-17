"""Whole-package verification and non-paper scope (D-08).

Two gaps on the tip this branch stacks on, shown before the change:

* a version-1 package whose second file is changed on disk is still accepted as signed,
  because the install path checks only ``plugin.py``;
* ``Authority.charge`` accepts a scope whose environment is not ``paper``.

The assertions below are the behaviour that must hold after the change. They fail on the
tip until ``verify_package`` and the non-paper refusal are in place.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from recursion_core.daemon.plugins import PluginHost
from recursion_core.plugin_security import Authority, Refused, TrustStore, canonical, create_ledger, parse_manifest
from recursion_core.plugin_security.manifest import ENTRY_FILE, MANIFEST_SCHEMA_V1
from recursion_core.plugin_security.package import VerifiedPackage, verify_package
from recursion_core.plugin_security.tests.support import (
    PLUGIN_SOURCE, Clock, FakeGilProbe, Key, manifest_bytes, needs_crypto, profile, scope_of, temp_dir,
)
from recursion_core.plugin_security.tests.test_manifest_v1 import sha

LINUX = sys.platform.startswith("linux")
SECOND = b"second = 1\n"
SECOND_NAME = "second.py"


def _tree(test: unittest.TestCase) -> Path:
    root = Path(tempfile.mkdtemp(prefix="csl-whole-"))
    test.addCleanup(shutil.rmtree, root, True)
    os.chmod(root, 0o700)
    return root


def _write(directory: Path, name: str, data: bytes) -> None:
    path = directory / name
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(path.parent, 0o700)
    path.write_bytes(data)
    os.chmod(path, 0o600)


def _v1_package(test: unittest.TestCase, key: Key, root: Path, *, anchor: str | None = None) -> bytes:
    """A signed version-1 package with ``plugin.py`` and a second file. Returns the manifest."""
    body = {
        "schema": MANIFEST_SCHEMA_V1,
        "plugin_id": "demo.plugin",
        "version": "1.0.0",
        "artifact_sha256": sha(PLUGIN_SOURCE),
        "dependency_lock_sha256": None,
        "install_scripts": [],
        "developer_key": key.public,
        "permissions": ["read"],
        "network_destinations": [],
        "credential_refs": [],
        "runtime": "python-inprocess/1",
        "interface": "csl-plugin-api/1",
        "files": {ENTRY_FILE: sha(PLUGIN_SOURCE), SECOND_NAME: sha(SECOND)},
    }
    raw = canonical(body)
    plugin = root / "plugins" / "demo"
    plugin.mkdir(parents=True, mode=0o700)
    os.chmod(root / "plugins", 0o700)
    os.chmod(plugin, 0o700)
    _write(plugin, "manifest.json", raw)
    _write(plugin, ENTRY_FILE, PLUGIN_SOURCE)
    _write(plugin, SECOND_NAME, SECOND)
    _write(plugin, "manifest.sig", (key.sign(raw) + "\n").encode())
    install = root / "plugins" / "install.json"
    body: dict = {"trusted_keys": {"dev": key.public}, "grants": []}
    if anchor is not None:
        body["authority_anchor"] = anchor
    install.write_text(json.dumps(body))
    os.chmod(install, 0o600)
    return raw


def _charge(auth: Authority, installer: Key, environment: str, key: str) -> dict:
    scope = scope_of(environment=environment)
    return auth.charge(
        profile(installer, scope=scope, profile_id=key),
        plugin_id="demo.plugin", artifact_sha256=sha(PLUGIN_SOURCE), scope=scope,
        kind="order.submit", provider="fake-provider", model_version="fake-model-1",
        currency="USD", amount_minor=100, idempotency_key=key)


def _code(fn) -> str:
    try:
        fn()
    except Refused as exc:
        return exc.code
    return "allowed"


@unittest.skipUnless(LINUX, "the plugin install path checks ownership by uid")
@needs_crypto
class ChangedSecondFileTests(unittest.TestCase):
    """The install path must not accept a package whose second file no longer matches."""

    def test_a_changed_second_file_is_not_accepted_as_signed(self) -> None:
        key = Key()
        root = _tree(self)
        _v1_package(self, key, root)
        plugin = root / "plugins" / "demo"
        _write(plugin, SECOND_NAME, b"second = 2\n")  # on disk, not the digest the manifest names
        config = root
        # PluginHost's config_dir is the parent of ``plugins/``.
        host = PluginHost(config, root / "state", gil_probe=FakeGilProbe())
        self.addCleanup(host.close)
        self.assertEqual(len(host.records), 1, host.records)
        record = host.records[0]
        self.assertNotEqual(record.get("trust"), "signed", record)
        self.assertNotEqual(
            hashlib.sha256((plugin / SECOND_NAME).read_bytes()).hexdigest(),
            sha(SECOND))


@needs_crypto
class NonPaperScopeTests(unittest.TestCase):
    """``charge`` refuses a scope whose environment is not exactly ``paper`` until the
    package is whole-package verified. With no verification, it refuses."""

    def setUp(self) -> None:
        self.installer = Key()
        self.clock = Clock()
        self.path = os.path.join(temp_dir(self), "ledger.db")
        create_ledger(self.path, self.installer.public)
        self.auth = Authority(self.installer.public, self.path, self.clock)
        self.addCleanup(self.auth.close)

    def test_a_non_paper_scope_is_refused_without_whole_package_verification(self) -> None:
        live = scope_of(environment="live")
        envelope = profile(self.installer, scope=live)
        with self.assertRaises(Refused) as ctx:
            self.auth.charge(
                envelope, plugin_id="demo.plugin",
                artifact_sha256=hashlib.sha256(PLUGIN_SOURCE).hexdigest(),
                scope=live, kind="order.submit", provider="fake-provider",
                model_version="fake-model-1", currency="USD", amount_minor=100,
                idempotency_key="live-1")
        self.assertEqual(ctx.exception.code, "package_not_signed_whole")

    def test_a_refused_non_paper_charge_writes_nothing(self) -> None:
        self.assertEqual(_code(lambda: _charge(self.auth, self.installer, "live", "live-1")),
                         "package_not_signed_whole")
        self.assertEqual(_code(lambda: _charge(self.auth, self.installer, "Paper", "paper-word")),
                         "package_not_signed_whole")
        spent = _charge(self.auth, self.installer, "paper", "paper-1")["spent_minor"]
        self.assertEqual(spent, 100)

    def test_a_whole_signed_package_may_charge_a_non_paper_scope(self) -> None:
        raw = canonical({
            "schema": MANIFEST_SCHEMA_V1, "plugin_id": "demo.plugin", "version": "1.0.0",
            "artifact_sha256": sha(PLUGIN_SOURCE), "dependency_lock_sha256": None,
            "install_scripts": [], "developer_key": "11" * 32, "permissions": ["read"],
            "network_destinations": [], "credential_refs": [], "runtime": "python-inprocess/1",
            "interface": "csl-plugin-api/1",
            "files": {ENTRY_FILE: sha(PLUGIN_SOURCE), SECOND_NAME: sha(SECOND)},
        })
        whole = VerifiedPackage(
            parse_manifest(raw), "signed", "ed25519:" + "ab" * 16, "signature_valid_trusted_key", {})
        auth = Authority(self.installer.public, self.path, self.clock, package_for=lambda _pid: whole)
        self.addCleanup(auth.close)
        self.assertEqual(_charge(auth, self.installer, "live", "live-ok")["spent_minor"], 100)

    def test_a_changed_or_plugin_py_only_package_still_refuses_non_paper(self) -> None:
        raw = canonical({
            "schema": MANIFEST_SCHEMA_V1, "plugin_id": "demo.plugin", "version": "1.0.0",
            "artifact_sha256": sha(PLUGIN_SOURCE), "dependency_lock_sha256": None,
            "install_scripts": [], "developer_key": "11" * 32, "permissions": ["read"],
            "network_destinations": [], "credential_refs": [], "runtime": "python-inprocess/1",
            "interface": "csl-plugin-api/1",
            "files": {ENTRY_FILE: sha(PLUGIN_SOURCE), SECOND_NAME: sha(SECOND)},
        })
        changed = VerifiedPackage(
            parse_manifest(raw), "unsigned", None, "package_files_changed", {}, changed=(SECOND_NAME,))
        other_source = b"other = 1\n"
        other_raw = canonical({
            "schema": MANIFEST_SCHEMA_V1, "plugin_id": "other.plugin", "version": "1.0.0",
            "artifact_sha256": sha(other_source), "dependency_lock_sha256": None,
            "install_scripts": [], "developer_key": "11" * 32, "permissions": ["read"],
            "network_destinations": [], "credential_refs": [], "runtime": "python-inprocess/1",
            "interface": "csl-plugin-api/1", "files": {ENTRY_FILE: sha(other_source)},
        })
        other_artifact = VerifiedPackage(
            parse_manifest(other_raw), "signed", "ed25519:" + "cd" * 16, "signature_valid_trusted_key", {})
        vnext = VerifiedPackage(
            parse_manifest(manifest_bytes("11" * 32)), "signed", "ed25519:" + "ab" * 16,
            "signature_valid_trusted_key", {})

        def auth_for(package):
            opened = Authority(self.installer.public, self.path, self.clock, package_for=lambda _pid: package)
            self.addCleanup(opened.close)
            return opened

        self.assertEqual(_code(lambda: _charge(auth_for(changed), self.installer, "live", "c1")),
                         "package_not_signed_whole")
        self.assertEqual(_code(lambda: _charge(auth_for(vnext), self.installer, "live", "c2")),
                         "plugin_py_only_signature")
        self.assertEqual(_code(lambda: _charge(auth_for(other_artifact), self.installer, "live", "c3")),
                         "package_not_signed_whole")
        # Paper does not consult the package. A later caller with a whole package can charge live.
        self.assertEqual(_charge(auth_for(changed), self.installer, "paper", "c4")["spent_minor"], 100)
        self.assertEqual(_charge(auth_for(whole_of(raw)), self.installer, "live", "c5")["spent_minor"], 100)


def whole_of(raw: bytes) -> VerifiedPackage:
    return VerifiedPackage(
        parse_manifest(raw), "signed", "ed25519:" + "ab" * 16, "signature_valid_trusted_key", {})


@needs_crypto
class VerifyPackageTests(unittest.TestCase):
    """``verify_package`` digest-checks every listed file and refuses an unsafe read."""

    def setUp(self) -> None:
        self.key = Key()
        self.root = _tree(self)
        self.raw = _v1_package(self, self.key, self.root)
        self.plugin = self.root / "plugins" / "demo"
        self.trust = TrustStore({"dev": self.key.public})
        self.addCleanup(self.trust.close)

    def test_an_intact_package_is_signed_and_returns_every_listed_file(self) -> None:
        result = verify_package(self.plugin, self.trust)
        self.assertEqual((result.trust, result.reason, result.intact), ("signed", "signature_valid_trusted_key", True))
        self.assertEqual(set(result.files), {ENTRY_FILE, SECOND_NAME})
        self.assertEqual(result.files[SECOND_NAME], SECOND)
        self.assertTrue(result.manifest.names_every_file)

    def test_a_changed_added_or_missing_file_is_not_accepted(self) -> None:
        _write(self.plugin, SECOND_NAME, b"second = 2\n")
        changed = verify_package(self.plugin, self.trust)
        self.assertEqual(changed.reason, "package_files_changed")
        self.assertEqual(changed.trust, "unsigned")
        self.assertEqual(changed.changed, (SECOND_NAME,))
        self.assertNotIn(SECOND_NAME, changed.files)
        self.assertIn(ENTRY_FILE, changed.files)

        _write(self.plugin, SECOND_NAME, SECOND)
        _write(self.plugin, "extra.py", b"extra = 1\n")
        added = verify_package(self.plugin, self.trust)
        self.assertEqual(added.added, ("extra.py",))
        self.assertEqual(added.trust, "unsigned")
        self.assertNotIn("extra.py", added.files)

        os.unlink(self.plugin / "extra.py")
        os.unlink(self.plugin / SECOND_NAME)
        missing = verify_package(self.plugin, self.trust)
        self.assertEqual(missing.missing, (SECOND_NAME,))
        self.assertEqual(missing.trust, "unsigned")

    def test_a_symlink_a_pipe_or_a_writable_file_is_refused(self) -> None:
        os.symlink(self.plugin / ENTRY_FILE, self.plugin / "linked.py")
        self.assertEqual(_code(lambda: verify_package(self.plugin, self.trust)), "package_entry_untrusted")
        os.unlink(self.plugin / "linked.py")
        os.mkfifo(self.plugin / "pipe.fifo")
        self.assertEqual(_code(lambda: verify_package(self.plugin, self.trust)), "package_entry_untrusted")
        os.unlink(self.plugin / "pipe.fifo")
        os.chmod(self.plugin / SECOND_NAME, 0o666)
        self.assertEqual(_code(lambda: verify_package(self.plugin, self.trust)), "package_entry_untrusted")

    def test_a_bad_signature_on_an_intact_package_is_unsigned(self) -> None:
        _write(self.plugin, "manifest.sig", (b"00" * 64) + b"\n")
        result = verify_package(self.plugin, self.trust)
        self.assertEqual((result.intact, result.trust, result.reason), (True, "unsigned", "signature_invalid"))


@unittest.skipUnless(LINUX, "the plugin install path checks ownership by uid")
@needs_crypto
class InstallPathScopeTests(unittest.TestCase):
    """The install path refuses a non-paper scope unless the package it just verified is whole."""

    def _host(self, root: Path) -> PluginHost:
        host = PluginHost(root, root / "state", gil_probe=FakeGilProbe(), clock=lambda: 1_000_000)
        self.addCleanup(host.close)
        return host

    def test_an_intact_version_1_package_loads_signed_and_may_charge_live(self) -> None:
        developer, installer = Key(), Key()
        root = _tree(self)
        _v1_package(self, developer, root, anchor=installer.public)
        store = root / "state" / "plugin-security"
        store.mkdir(parents=True, mode=0o700)
        os.chmod(root / "state", 0o700)
        os.chmod(store, 0o700)
        create_ledger(str(store / "ledger.sqlite3"), installer.public)
        host = self._host(root)
        record, = host.records
        self.assertEqual((record["state"], record["trust"]), ("loaded", "signed"))
        self.assertEqual(host.authority_state, "configured")
        self.assertEqual(_charge(host.authority, installer, "live", "live-1")["spent_minor"], 100)

    def test_a_changed_second_file_cannot_charge_live_and_paper_still_can(self) -> None:
        developer, installer = Key(), Key()
        root = _tree(self)
        _v1_package(self, developer, root, anchor=installer.public)
        _write(root / "plugins" / "demo", SECOND_NAME, b"second = 9\n")
        store = root / "state" / "plugin-security"
        store.mkdir(parents=True, mode=0o700)
        os.chmod(root / "state", 0o700)
        os.chmod(store, 0o700)
        create_ledger(str(store / "ledger.sqlite3"), installer.public)
        host = self._host(root)
        record, = host.records
        self.assertEqual(record["state"], "loaded")
        self.assertNotEqual(record["trust"], "signed")
        self.assertIn("package_files_changed", [item["code"] for item in record["findings"]])
        self.assertEqual(_code(lambda: _charge(host.authority, installer, "live", "live-no")),
                         "package_not_signed_whole")
        self.assertEqual(_charge(host.authority, installer, "paper", "paper-1")["spent_minor"], 100)


if __name__ == "__main__":
    unittest.main()
