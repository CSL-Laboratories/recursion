"""A manual grant bound to no developer key, a binding that is not a key id, the words of the runtime
refusal, and the package walk that follows no symbolic link.

Keys are made per run (``support.Key``); the signed cases need ``cryptography``. Every path a test
uses lies in a temporary folder the test made.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from recursion_core.plugin_security import loader as loader_module
from recursion_core.plugin_security.errors import Refused
from recursion_core.plugin_security.loader import CoreHealth, PluginLoader
from recursion_core.plugin_security.manifest import ENTRY_FILE, Grant, ReservedEffects, TrustStore, parse_manifest
from recursion_core.plugin_security.signing import key_id
from recursion_core.plugin_security.tests.support import (
    PLUGIN_SOURCE, FakeGilProbe, Key, manifest_bytes, needs_crypto, temp_dir,
)
from recursion_core.plugin_security.tests.test_manifest_v1 import NOTES_SOURCE, v1

FAKE_KEY = "11" * 32
UNBOUND = ("grant_unbound: The grant for this plugin in install.json names no developer key ID (developer_key_id), "
           "so the plugin was not loaded and none of its code ran. Make the grant again with developer_key_id set "
           "to this plugin's developer key ID, then load the plugin again. This manifest is not signed, so the ID "
           "is never taken from it: the plugin's developer gives it (ed25519: and 32 lower-case hex digits).")
UNBOUND_SIGNED = ("grant_unbound: The grant for this plugin in install.json names no developer key ID "
                  "(developer_key_id), so the plugin was not loaded and none of its code ran. Its manifest is signed "
                  "by a key on this install's trusted list, developer key ID %s: make the grant again with "
                  "signer_key_id and developer_key_id set to %s, then load the plugin again.")
FOLDER = ("package_file_missing: the plugin's folder is missing, is a symbolic link or is not a folder, and the "
          "loader follows no link in a package, so the plugin was not loaded and none of its code ran: give the "
          "folder itself, or install the package again from its source.")
UNKNOWN = ("unknown runtime %s; this release accepts python-sandboxed/1, and python-inprocess/1 until 1.1.0 with the "
           "finding runtime_inprocess_retiring; write python-sandboxed/1 as the manifest's runtime")
NOT_KEY_IDS = ("not-a-key", "", "ed25519:", "ed25519:" + "AB" * 16, "ed25519:" + "ab" * 16 + " ")
POSIX = unittest.skipUnless(os.name == "posix", "symbolic links and the install reader's file checks are POSIX here")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def marker_source(test: unittest.TestCase) -> tuple[Path, bytes]:
    marker = Path(temp_dir(test)) / "plugin-code-ran"
    return marker, f'open({str(marker)!r}, "w").close()\n'.encode()


def loader_for(trust: TrustStore, grant: Grant | None) -> PluginLoader:
    return PluginLoader(trust, {"demo.plugin": grant} if grant else {}, ReservedEffects(), CoreHealth(),
                        FakeGilProbe())


@needs_crypto
class SignedManifestWithAnUnboundManualGrantTests(unittest.TestCase):
    """The unbound refusal holds for a signed manifest as it does for an unsigned one."""

    def setUp(self) -> None:
        self.dev = Key()
        self.kid = key_id(self.dev.public)
        self.trust = TrustStore({"dev": self.dev.public})

    def assert_refused_before_code(self, raw: bytes, source: bytes, marker: Path, grant: Grant) -> None:
        loader = loader_for(self.trust, grant)
        try:
            with self.assertRaises(Refused) as ctx:
                loader.load(raw, self.dev.sign(raw), source)
        finally:
            self.assertFalse(marker.exists(), "plugin code ran before the refusal")
        self.assertEqual(str(ctx.exception), UNBOUND_SIGNED % (self.kid, self.kid))
        self.assertEqual(loader.doctor(raw, self.dev.sign(raw), source)[-1]["code"], "grant_unbound")

    def test_a_signed_manifest_asking_a_permission_with_an_unbound_manual_grant_is_refused(self) -> None:
        marker, source = marker_source(self)
        raw = manifest_bytes(self.dev.public, source=source, permissions=("read",))
        self.assert_refused_before_code(raw, source, marker, Grant("demo.plugin", sha(source), None, ("read",)))

    def test_the_same_for_a_signed_version_1_manifest(self) -> None:
        marker, source = marker_source(self)
        raw = v1(self.dev.public, source=source, permissions=("read",), files={ENTRY_FILE: sha(source)})
        self.assert_refused_before_code(raw, source, marker, Grant("demo.plugin", sha(source), None, ("read",)))

    def test_a_signed_manifest_with_a_manual_grant_bound_to_its_key_still_loads_with_no_permission(self) -> None:
        raw = manifest_bytes(self.dev.public, permissions=("read",))
        grant = Grant("demo.plugin", sha(PLUGIN_SOURCE), None, ("read",), developer_key_id=self.kid)
        loaded = loader_for(self.trust, grant).load(raw, self.dev.sign(raw), PLUGIN_SOURCE)
        self.assertEqual((loaded.trust, loaded.permissions), ("signed", ()))
        self.assertIn("grant_signer_mismatch", [f["code"] for f in loaded.findings])

    def test_a_signed_manifest_asking_nothing_loads_with_an_unbound_manual_grant(self) -> None:
        raw = manifest_bytes(self.dev.public, permissions=())
        grant = Grant("demo.plugin", sha(PLUGIN_SOURCE), None, ("read",))
        loaded = loader_for(self.trust, grant).load(raw, self.dev.sign(raw), PLUGIN_SOURCE)
        self.assertEqual((loaded.trust, loaded.permissions, loaded.module.VALUE), ("signed", (), 42))


class OrderAfterRevokedKeyIsAKeyIdTests(unittest.TestCase):
    """order_after_revoked_key is held to developer_key_id's shape; anything else binds nothing."""

    def test_a_grant_refuses_a_value_that_is_not_a_key_id(self) -> None:
        for value in NOT_KEY_IDS + (5, b"ed25519:"):
            with self.subTest(value=value):
                with self.assertRaises(Refused) as ctx:
                    Grant("demo.plugin", "0" * 64, None, ("read",), order_after_revoked_key=value)
                self.assertEqual(ctx.exception.code, "grant_invalid")
                self.assertTrue(ctx.exception.detail.startswith("order_after_revoked_key"), ctx.exception.detail)

    def test_a_grant_keeps_null_or_a_key_id(self) -> None:
        self.assertIsNone(Grant("demo.plugin", "0" * 64, None, ("read",)).order_after_revoked_key)
        kept = Grant("demo.plugin", "0" * 64, None, ("read",), order_after_revoked_key=key_id(FAKE_KEY))
        self.assertEqual(kept.order_after_revoked_key, key_id(FAKE_KEY))

    def test_a_value_that_is_not_a_key_id_never_counts_as_a_binding(self) -> None:
        # A grant made some other way than its constructor still binds nothing with such a value.
        marker, source = marker_source(self)
        raw = manifest_bytes(FAKE_KEY, source=source, permissions=("read",))
        for value in ("not-a-key", ""):
            with self.subTest(value=value):
                grant = Grant("demo.plugin", sha(source), None, ("read",))
                object.__setattr__(grant, "order_after_revoked_key", value)
                try:
                    with self.assertRaises(Refused) as ctx:
                        loader_for(TrustStore({}), grant).load(raw, None, source)
                finally:
                    self.assertFalse(marker.exists(), "plugin code ran before the refusal")
                self.assertEqual(str(ctx.exception), UNBOUND)

    @POSIX
    def test_the_install_reader_refuses_a_value_that_is_not_a_key_id(self) -> None:
        from recursion_core.daemon.plugins import PluginRefused, read_install
        base = {"plugin_id": "demo.plugin", "artifact_sha256": "0" * 64, "signer_key_id": None, "permissions": ["read"]}

        def read(entry):
            with tempfile.TemporaryDirectory(prefix="grant-binding-") as directory:
                path = Path(directory) / "install.json"
                path.write_text(json.dumps({"grants": [entry]}), encoding="utf-8")
                path.chmod(0o600)
                return read_install(directory)["grants"]["demo.plugin"]

        for value in NOT_KEY_IDS + (5,):
            with self.subTest(value=value):
                with self.assertRaises(PluginRefused) as caught:
                    read(dict(base, order_after_revoked_key=value))
                self.assertEqual((caught.exception.code, caught.exception.detail),
                                 ("plugin_install_config_invalid", "grant value"))
        self.assertIsNone(read(dict(base, order_after_revoked_key=None)).order_after_revoked_key)
        good = "ed25519:" + "ab" * 16
        self.assertEqual(read(dict(base, order_after_revoked_key=good)).order_after_revoked_key, good)


class TheRuntimeRefusalSaysWhatThisReleaseAcceptsTests(unittest.TestCase):
    def test_an_unknown_runtime_is_refused_with_the_runtimes_this_release_accepts(self) -> None:
        with self.assertRaises(Refused) as ctx:
            parse_manifest(v1(runtime="python-subprocess/1"))
        self.assertEqual((ctx.exception.code, ctx.exception.detail), ("manifest_runtime", UNKNOWN % '"python-subprocess/1"'))

    def test_what_the_refusal_names_as_accepted_is_accepted(self) -> None:
        for runtime in ("python-sandboxed/1", "python-inprocess/1"):
            with self.subTest(runtime=runtime):
                self.assertIn(runtime, UNKNOWN)
                self.assertEqual(parse_manifest(v1(runtime=runtime)).runtime, runtime)


@POSIX
class PackageWalkFollowsNoLinkTests(unittest.TestCase):
    """The package folder itself is not followed when it is a link, nor a folder swapped for one mid-walk."""

    def setUp(self) -> None:
        self.root = Path(temp_dir(self))
        self.real = self.root / "real"
        (self.real / "notes").mkdir(parents=True)
        (self.real / "notes" / "index.py").write_bytes(NOTES_SOURCE)
        (self.real / ENTRY_FILE).write_bytes(PLUGIN_SOURCE)
        self.link = self.root / "link"
        self.link.symlink_to(self.real, target_is_directory=True)

    def test_a_package_folder_that_is_a_link_holds_no_listed_file(self) -> None:
        self.assertTrue(loader_module._package_file_present(self.real, "notes/index.py"))
        self.assertFalse(loader_module._package_file_present(self.link, "notes/index.py"))
        self.assertFalse(loader_module._package_file_present(self.link, ENTRY_FILE))

    def test_a_folder_swapped_for_a_link_between_two_steps_is_not_followed(self) -> None:
        # The real notes folder holds no index.py; a folder elsewhere does. Right after the walk has
        # looked at notes, notes is renamed away and a link to that other folder takes its name.
        (self.real / "notes" / "index.py").unlink()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "index.py").write_bytes(NOTES_SOURCE)
        notes = self.real / "notes"
        swapped = []

        def swap() -> None:
            if not swapped:
                swapped.append(True)
                notes.rename(self.real / "notes.before")
                notes.symlink_to(elsewhere, target_is_directory=True)

        real_lstat, real_open = Path.lstat, os.open

        def lstat(path, *args, **kwargs):
            result = real_lstat(path, *args, **kwargs)
            if Path(path) == notes:
                swap()
            return result

        def open_(path, *args, **kwargs):
            result = real_open(path, *args, **kwargs)
            if os.fspath(path) == "notes":
                swap()
            return result

        with mock.patch.object(Path, "lstat", lstat), mock.patch.object(loader_module.os, "open", open_):
            present = loader_module._package_file_present(self.real, "notes/index.py")
        self.assertEqual(swapped, [True], "the swap did not happen, so the test proves nothing")
        self.assertFalse(present)

    @needs_crypto
    def test_a_signed_load_from_a_package_folder_that_is_a_link_is_refused_with_words_that_say_so(self) -> None:
        dev = Key()
        raw = v1(dev.public, permissions=(), files={ENTRY_FILE: sha(PLUGIN_SOURCE), "notes/index.py": sha(NOTES_SOURCE)})
        loader = loader_for(TrustStore({"dev": dev.public}), None)
        with self.assertRaises(Refused) as ctx:
            loader.load(raw, dev.sign(raw), PLUGIN_SOURCE, package_dir=self.link)
        self.assertEqual(str(ctx.exception), FOLDER)
        self.assertEqual(loader.load(raw, dev.sign(raw), PLUGIN_SOURCE, package_dir=self.real).trust, "signed")


if __name__ == "__main__":
    unittest.main()
