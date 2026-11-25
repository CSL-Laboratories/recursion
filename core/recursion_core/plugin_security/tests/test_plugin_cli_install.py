"""Local registry install, list and remove (PRODUCT.md 1185-1188, 1226-1233, 1366-1369, 1900).

``recursion plugin install`` reads a registry folder in the marketplace layout, checks the
entry, the index, the manifest digest and the artifact digest, and checks the signature
against this install's trusted keys. It prints the checks a developer sees and the registry
label, then writes the plugin folder and ``install.json`` as a new generation. ``list`` and
``remove`` are the other two commands. Nothing here fetches. A failed signature is not a
trusted install. Removal leaves a profile the daemon can start on.

The marketplace's own invalid registries are not copied into this tree. Each refusal below
is a small registry built to that format, one rule at a time. Set ``CSL_MARKETPLACE_REF``
to a marketplace checkout to refuse that tree's ``fixtures/registries/invalid`` as well.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import tempfile
import threading
import unittest

from recursion_core.plugin_security import canonical
from recursion_core.plugin_security.signing import backend_available, key_id
from recursion_core.plugin_security.tests.support import PLUGIN_SOURCE, FakeGilProbe, Key, needs_crypto

LINUX = os.name == "posix" and os.uname().sysname == "Linux"
LINUX_ONLY = ("Linux-only: install writes an owner-only profile and the plugin host checks uid; "
              "Windows refuses the host before it opens a plugin file")

# Phrases the installer must show. They are the contract lines, not a second copy of the loader.
DEVELOPER_PHRASES = (
    "launch engine binaries past the launch check",
    "stall other work",
    "GIL",
    "untrusted plugin config folder refuses every plugin",
    "missing revocation store reads as empty",
    "a label, not a gate",
    "loads as unsigned",
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _entry(plugin_id, version, manifest, source, *, label="non_verified", signature=None, **release_over):
    release = {
        "version": version,
        "status": "active",
        "status_reason": None,
        "label": label,
        "label_basis": None,
        "trust_tier": "trusted_in_process",
        "manifest_sha256": _sha(manifest),
        "artifact_sha256": _sha(source),
        "signature": signature,
        "package": None,
        "source": {
            "repository": "https://example.invalid/fixtures/" + plugin_id,
            "commit": "a0c3713d92acd5bc3cd3e3b6469b51e250b94715",
            "path": "plugin.py",
        },
        "compatibility": {
            "manifest_schema": "csl-plugin-manifest/vNext",
            "interface": "csl-core/v0",
            "runtime": "python-inprocess",
            "recursion_commit": "ed3598e6576b1fa4700c948552bd4880d7416ba9",
            "recursion_versions": None,
        },
    }
    release.update(release_over)
    return {
        "schema": "csl-marketplace-entry/v0",
        "plugin_id": plugin_id,
        "name": "Fixture " + plugin_id,
        "summary": "A made-up plugin used only to test install.",
        "publisher": {"name": "Fixture publisher", "url": None},
        "releases": [release],
        "ratings": None,
        "install_count": None,
    }


def _manifest(source, plugin_id, *, developer_key, permissions=("read", "panel"), version="1.0.0"):
    return canonical({
        "schema": "csl-plugin-manifest/vNext",
        "plugin_id": plugin_id,
        "version": version,
        "artifact_sha256": _sha(source),
        "dependency_lock_sha256": None,
        "install_scripts": [],
        "developer_key": developer_key,
        "permissions": list(permissions),
        "network_destinations": [],
        "credential_refs": [],
    })


class ProfileCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rc-plugin-cli-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)
        self.config = self.tmp / "config" / "recursion"
        self.state = self.tmp / "state"
        self.registry = self.tmp / "registry-root"

    def write_registry(self, plugin_id="fixture.unsigned", *, source=PLUGIN_SOURCE, key=None,
                       permissions=("read", "panel"), signature="omit", version="1.0.0",
                       extra_releases=(), entry_over=None, release_over=None, index="build",
                       manifest=None):
        """Write one registry root. ``signature`` ``omit`` writes none; ``bad`` writes a bad file."""
        from recursion_core.plugin_security.registry import build_index, index_bytes

        developer = key.public if key is not None else "11" * 32
        manifest = manifest if manifest is not None else _manifest(
            source, plugin_id, developer_key=developer, permissions=permissions, version=version)
        sig_field = None
        if signature == "real":
            sig_field = {"format": "ed25519-hex", "key_id": key_id(developer)}
        elif signature == "bad":
            sig_field = {"format": "ed25519-hex", "key_id": key_id(developer)}
        entry = _entry(plugin_id, version, manifest, source, signature=sig_field)
        if release_over:
            entry["releases"][0].update(release_over)
        if extra_releases:
            entry["releases"].extend(extra_releases)
        if entry_over:
            entry.update(entry_over)
        plugin = self.registry / "registry" / plugin_id / version
        plugin.mkdir(parents=True, mode=0o700)
        (plugin / "manifest.json").write_bytes(manifest)
        (plugin / "plugin.py").write_bytes(source)
        if signature == "real":
            (plugin / "manifest.sig").write_text(key.sign(manifest) + "\n")
        elif signature == "bad":
            (plugin / "manifest.sig").write_text("not-a-signature\n")
        (self.registry / "registry" / plugin_id / "entry.json").write_text(
            json.dumps(entry, indent=2, sort_keys=True) + "\n")
        if index == "build":
            raw = index_bytes(build_index([entry]))
        elif index == "empty":
            raw = index_bytes({"releases": [], "schema": "csl-marketplace-index/v0"})
        else:
            raw = index
        (self.registry / "registry" / "index.json").write_bytes(raw)
        return entry

    def install(self, plugin_id="fixture.unsigned", **kwargs):
        from recursion_core.plugin_security.install import install_plugin

        return install_plugin(self.config, self.registry, plugin_id, state=self.state, **kwargs)

    def listed(self):
        from recursion_core.plugin_security.install import list_plugins

        return list_plugins(self.config, state=self.state)

    def remove(self, plugin_id="fixture.unsigned"):
        from recursion_core.plugin_security.install import remove_plugin

        return remove_plugin(self.config, plugin_id, state=self.state)

    def codes_of(self, root):
        from recursion_core.plugin_security.registry import check_registry

        report = check_registry(root)
        return report.codes()


@unittest.skipUnless(LINUX, LINUX_ONLY)
class RegistryRefusalTests(ProfileCase):
    """Every marketplace invalid-registry rule this cog covers, on a small registry."""

    def test_each_invalid_rule_is_refused_and_nothing_is_written(self):
        cases = {
            "artifact-digest-mismatch": ("release_artifact_digest_mismatch", {"artifact_sha256": "ab" * 32}),
            "entry-example-in-registry": ("entry_example_flag", None),
            "entry-ratings-not-null": ("entry_reserved_field", None),
            "entry-unknown-field": ("entry_fields", None),
            "index-stale": ("index_stale", None),
            "manifest-digest-mismatch": ("release_manifest_digest_mismatch", {"manifest_sha256": "cd" * 32}),
            "manifest-plugin-id-host": ("manifest_plugin_id_reserved", None),
            "manifest-pretty-printed": ("noncanonical_value", None),
            "release-interface-not-yet-defined": ("release_compatibility", None),
            "release-label-unlisted": ("release_label", {"label": "unlisted"}),
            "release-label-verified": ("release_label_verified_undefined", {"label": "verified"}),
            "release-package-not-null": ("release_package_reserved", {"package": {"format": "none"}}),
            "release-runtime-not-yet-defined": ("release_compatibility", None),
            "release-source-short-commit": ("release_source", None),
            "release-trust-tier-reserved": ("release_trust_tier_reserved", {"trust_tier": "restricted_wasm"}),
            "release-version-duplicate": ("release_version", None),
            "signature-malformed": ("signature_malformed", None),
            "vendored-artifact-tampered": ("artifact_digest_mismatch", None),
        }
        for name, (code, over) in cases.items():
            with self.subTest(name=name):
                shutil.rmtree(self.registry, ignore_errors=True)
                self._mutate(name, over)
                self.assertEqual(self.codes_of(self.registry), [code])
                from recursion_core.plugin_security.install import InstallRefused
                with self.assertRaises(InstallRefused) as caught:
                    self.install()
                self.assertEqual(caught.exception.code, code)
                self.assertFalse((self.config / "plugins").exists())
                self.assertFalse((self.config / "plugin-generations").exists())

    def _mutate(self, name, over):
        if name == "entry-example-in-registry":
            self.write_registry(entry_over={"example": True})
            return
        if name == "entry-ratings-not-null":
            self.write_registry(entry_over={"ratings": {"average": 5}})
            return
        if name == "entry-unknown-field":
            self.write_registry(entry_over={"homepage": "https://example.invalid/"})
            return
        if name == "index-stale":
            self.write_registry(index="empty")
            return
        if name == "manifest-plugin-id-host":
            source = PLUGIN_SOURCE
            manifest = canonical({
                "schema": "csl-plugin-manifest/vNext", "plugin_id": "host", "version": "1.0.0",
                "artifact_sha256": _sha(source), "dependency_lock_sha256": None, "install_scripts": [],
                "developer_key": "11" * 32, "permissions": ["read", "panel"],
                "network_destinations": [], "credential_refs": [],
            })
            self.write_registry(manifest=manifest)
            return
        if name == "manifest-pretty-printed":
            source = PLUGIN_SOURCE
            body = {
                "schema": "csl-plugin-manifest/vNext", "plugin_id": "fixture.unsigned", "version": "1.0.0",
                "artifact_sha256": _sha(source), "dependency_lock_sha256": None, "install_scripts": [],
                "developer_key": "11" * 32, "permissions": ["read", "panel"],
                "network_destinations": [], "credential_refs": [],
            }
            pretty = (json.dumps(body, indent=2) + "\n").encode()
            self.write_registry(manifest=pretty)
            return
        if name == "release-interface-not-yet-defined":
            self.write_registry()
            self._patch_release(lambda release: release["compatibility"].__setitem__("interface", "csl-core/1"))
            return
        if name == "release-runtime-not-yet-defined":
            self.write_registry()
            self._patch_release(lambda release: release["compatibility"].__setitem__("runtime", "wasm-component"))
            return
        if name == "release-source-short-commit":
            self.write_registry()
            self._patch_release(lambda release: release["source"].__setitem__("commit", "a0c3713"))
            return
        if name == "release-version-duplicate":
            self.write_registry()
            entry_path = self.registry / "registry" / "fixture.unsigned" / "entry.json"
            entry = json.loads(entry_path.read_text())
            entry["releases"].append(json.loads(json.dumps(entry["releases"][0])))
            self._rewrite_entry(entry)
            return
        if name == "signature-malformed":
            self.write_registry(signature="bad")
            return
        if name == "vendored-artifact-tampered":
            self.write_registry()
            (self.registry / "registry" / "fixture.unsigned" / "1.0.0" / "plugin.py").write_bytes(b"TAMPERED = True\n")
            return
        self.write_registry(release_over=over)

    def _patch_release(self, change):
        entry_path = self.registry / "registry" / "fixture.unsigned" / "entry.json"
        entry = json.loads(entry_path.read_text())
        change(entry["releases"][0])
        self._rewrite_entry(entry)

    def _rewrite_entry(self, entry):
        from recursion_core.plugin_security.registry import build_index, index_bytes

        path = self.registry / "registry" / "fixture.unsigned" / "entry.json"
        path.write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n")
        (self.registry / "registry" / "index.json").write_bytes(index_bytes(build_index([entry])))

    def test_a_signature_by_the_wrong_key_is_refused_when_it_can_be_checked(self):
        if not backend_available():
            self.write_registry(signature="bad")
            # A malformed signature is the case that does not need the backend.
            self.assertEqual(self.codes_of(self.registry), ["signature_malformed"])
            return
        signer = Key()
        other = Key()
        self.write_registry(key=signer, signature="real")
        sig_path = self.registry / "registry" / "fixture.unsigned" / "1.0.0" / "manifest.sig"
        manifest = (self.registry / "registry" / "fixture.unsigned" / "1.0.0" / "manifest.json").read_bytes()
        sig_path.write_text(other.sign(manifest) + "\n")
        self.assertEqual(self.codes_of(self.registry), ["signature_invalid"])
        from recursion_core.plugin_security.install import InstallRefused
        with self.assertRaises(InstallRefused) as caught:
            self.install()
        self.assertEqual(caught.exception.code, "signature_invalid")
        self.assertFalse((self.config / "plugins").exists())

    def test_a_valid_registry_has_no_error(self):
        self.write_registry()
        self.assertEqual(self.codes_of(self.registry), [])

    def test_marketplace_invalid_fixtures_when_a_checkout_is_named(self):
        root = os.environ.get("CSL_MARKETPLACE_REF")
        if not root:
            self.skipTest("CSL_MARKETPLACE_REF is not set")
        invalid = Path(root) / "fixtures" / "registries" / "invalid"
        self.assertTrue(invalid.is_dir(), invalid)
        seen = {}
        for folder in sorted(path for path in invalid.iterdir() if path.is_dir()):
            expect = json.loads((folder / "expect.json").read_text())
            # expect.json is the LF fixture. This checkout stores those text files
            # with CR LF, which changes the artifact digest and the index bytes.
            source = self._lf_copy(folder)
            codes = self.codes_of(source)
            if folder.name == "signature-wrong-key" and not backend_available():
                self.assertEqual(codes, [])
                from recursion_core.plugin_security.registry import check_registry
                notes = [item.code for item in check_registry(source).notes]
                self.assertIn("signature_unchecked", notes)
            else:
                self.assertEqual(codes, expect["errors"], folder.name)
            from recursion_core.plugin_security.install import InstallRefused, install_plugin
            with self.assertRaises(InstallRefused) as caught:
                install_plugin(self.config, source, "fixture.unsigned", state=self.state)
            self.assertIn(caught.exception.code, codes or ["signature_backend_unavailable"])
            seen[folder.name] = codes or [caught.exception.code]
            shutil.rmtree(self.config, ignore_errors=True)
        self.assertGreaterEqual(len(seen), 18)

    def _lf_copy(self, folder: Path) -> Path:
        """Copy one registry with CR LF turned into LF. Digests were taken over LF bytes."""
        dest = self.tmp / "lf-fixtures" / folder.name
        if dest.exists():
            shutil.rmtree(dest)
        for dirpath, _dirnames, filenames in os.walk(folder):
            rel = Path(dirpath).relative_to(folder)
            (dest / rel).mkdir(parents=True, exist_ok=True)
            for name in filenames:
                data = (Path(dirpath) / name).read_bytes().replace(b"\r\n", b"\n")
                (dest / rel / name).write_bytes(data)
        return dest


@unittest.skipUnless(LINUX, LINUX_ONLY)
class InstallCommandTests(ProfileCase):
    """The proof line, generations, trust, and the paths that must not write."""

    def test_install_list_shows_non_verified_then_remove_leaves_the_daemon_able_to_start(self):
        source = b"raise SystemExit('plugin code ran during install')\n"
        self.write_registry(source=source, permissions=("read", "panel"))
        opened = []

        def refuse_socket(*_args, **_kwargs):
            opened.append(1)
            raise AssertionError("install opened a socket")

        real = socket.socket
        socket.socket = refuse_socket
        try:
            result = self.install()
        finally:
            socket.socket = real
        self.assertEqual(opened, [])
        self.assertEqual(result["registry_label"], "non_verified")
        self.assertEqual(result["trust"], "unsigned")
        self.assertEqual(result["generation"], 1)
        self.assertNotIn("order", result["requested_permissions"])
        text = "\n".join(result["developer_checks"])
        for phrase in DEVELOPER_PHRASES:
            self.assertIn(phrase, text)
        self.assertIn("A missing revocation store reads as empty: no key counts as revoked.", result["developer_checks"])
        self.assertIn("by hand", text)
        self.assertIn("warning", text)
        shown = self.listed()
        self.assertEqual(shown["plugins"], [{
            "plugin_id": "fixture.unsigned", "version": "1.0.0", "registry_label": "non_verified",
        }])
        # The label is bound to these manifest bytes, and no grant was written.
        from recursion_core.daemon import plugins
        install = plugins.read_install(self.config / "plugins")
        manifest = (self.config / "plugins" / "fixture.unsigned" / "manifest.json").read_bytes()
        self.assertEqual(install["registry_labels"], {_sha(manifest): "non_verified"})
        self.assertEqual(install["grants"], {})
        self.assertEqual(install["trusted_keys"], {})
        removed = self.remove()
        self.assertEqual(removed["plugins"], [])
        self.assertEqual(self.listed()["plugins"], [])
        self.assertEqual(removed["generation"], 2)
        # The previous generation is still on disk for a later roll-back. It is not what the daemon loads.
        previous = self.config / "plugin-generations" / "1" / "fixture.unsigned" / "plugin.py"
        self.assertEqual(previous.read_bytes(), source)
        self.assertFalse((self.config / "plugins" / "fixture.unsigned").exists())
        from recursion_core.daemon.plugins import PluginHost
        from recursion_core.daemon.tests.plugin_fixture import ensure_revocation_store
        ensure_revocation_store(self.state)
        host = PluginHost(self.config, self.state, gil_probe=FakeGilProbe())
        self.addCleanup(host.close)
        self.assertEqual(host.records, [])

    @needs_crypto
    def test_the_running_host_lists_non_verified_and_starts_again_after_remove(self):
        source = b"VALUE = 1\n"
        self.write_registry(source=source)
        self.install()
        from recursion_core.daemon import plugins
        from recursion_core.daemon import control_adapter as adapter
        from recursion_core.daemon.tests.plugin_fixture import ensure_revocation_store
        ensure_revocation_store(self.state)
        host = plugins.PluginHost(self.config, self.state, gil_probe=FakeGilProbe())
        self.addCleanup(host.close)
        self.assertEqual(host.records[0]["state"], "loaded")
        self.assertEqual(host.records[0]["registry_label"], "non_verified")
        self.assertEqual(host.records[0]["trust"], "unsigned")
        self.assertNotIn("order", host.records[0]["permissions"])
        control = adapter.DaemonControl(None, None, None, None, self.state, plugins=host)
        listed = control.call("plugin.list")["plugins"]
        self.assertEqual(listed[0]["registry_label"], "non_verified")
        host.close()
        self.remove()
        from recursion_core.daemon.tests.test_composition import ComposedDaemonTest
        case = ComposedDaemonTest("test_route_and_plugin_inventory_are_reads_of_the_running_daemon")
        case.setUp()
        try:
            # The composed daemon reads the profile this test already removed the plugin from.
            config = Path(case.environ["XDG_CONFIG_HOME"]) / "recursion"
            shutil.rmtree(config, ignore_errors=True)
            shutil.copytree(self.config, config)
            _daemon, composed = case.boot(plugin_options={"gil_probe": FakeGilProbe()})
            api = case.api(composed)
            self.assertEqual(api.call("plugin.list")["plugins"], [])
        finally:
            case.doCleanups()

    def test_an_order_request_is_not_granted(self):
        self.write_registry(permissions=("read", "order"))
        result = self.install()
        self.assertIn("by hand", "\n".join(result["developer_checks"]))
        self.assertIn("warning", "\n".join(result["developer_checks"]))
        from recursion_core.daemon import plugins
        self.assertEqual(plugins.read_install(self.config / "plugins")["grants"], {})

    @needs_crypto
    def test_a_trusted_signature_is_signed_and_order_is_asked_once_and_not_granted(self):
        signer = Key()
        self.write_registry(key=signer, signature="real", permissions=("read", "order"))
        from recursion_core.daemon.tests import plugin_fixture as fx
        fx.write_install(self.config, trusted={"developer": signer.public})
        result = self.install()
        self.assertEqual(result["trust"], "signed")
        self.assertEqual(result["signature"], "signature_valid_trusted_key")
        text = "\n".join(result["developer_checks"])
        self.assertIn("asked once", text)
        from recursion_core.daemon import plugins
        install = plugins.read_install(self.config / "plugins")
        self.assertEqual(list(install["trusted_keys"]), ["developer"])
        # The key that was already trusted is kept. Install does not add another label for it.
        self.assertEqual(install["trusted_keys"]["developer"], signer.public)
        self.assertEqual(install["grants"], {})
        self.assertEqual(result["registry_label"], "non_verified")

    @needs_crypto
    def test_a_valid_signature_by_an_untrusted_key_installs_as_unsigned(self):
        signer = Key()
        self.write_registry(key=signer, signature="real")
        result = self.install()
        self.assertEqual(result["trust"], "unsigned")
        self.assertEqual(result["signature"], "signer_not_on_trusted_list")
        self.assertEqual(result["registry_label"], "non_verified")
        self.assertTrue((self.config / "plugins" / "fixture.unsigned" / "manifest.sig").is_file())

    def test_a_second_install_is_a_new_generation_and_a_refusal_keeps_the_old_one(self):
        self.write_registry()
        first = self.install()
        self.assertEqual(first["generation"], 1)
        again = self.install()
        self.assertEqual(again["generation"], 2)
        self.assertTrue((self.config / "plugin-generations" / "1" / "install.json").is_file())
        # A later registry that fails its digest check does not replace the installed plugin.
        plugin = self.registry / "registry" / "fixture.unsigned" / "1.0.0" / "plugin.py"
        plugin.write_bytes(b"TAMPERED = True\n")
        from recursion_core.plugin_security.install import InstallRefused
        with self.assertRaises(InstallRefused) as caught:
            self.install()
        self.assertEqual(caught.exception.code, "artifact_digest_mismatch")
        self.assertEqual(self.listed()["plugins"][0]["plugin_id"], "fixture.unsigned")
        self.assertNotIn(b"TAMPERED", (self.config / "plugins" / "fixture.unsigned" / "plugin.py").read_bytes())
        self.assertTrue((self.config / "plugin-generations" / "2").is_dir())
        self.assertFalse((self.config / "plugin-generations" / "3").exists())

    def test_a_publish_that_raises_is_finished_by_the_next_list(self):
        self.write_registry()
        from recursion_core.plugin_security import install as install_mod

        real = install_mod._copy_generation_to_live

        def boom(*_args, **_kwargs):
            raise OSError("interrupted")

        install_mod._copy_generation_to_live = boom
        try:
            with self.assertRaises(install_mod.InstallRefused) as caught:
                self.install()
            self.assertEqual("filesystem_refused", caught.exception.code)
        finally:
            install_mod._copy_generation_to_live = real
        self.assertFalse((self.config / "plugins" / "fixture.unsigned").exists())
        shown = self.listed()
        self.assertEqual(shown["plugins"][0]["registry_label"], "non_verified")
        self.assertTrue((self.config / "plugins" / "fixture.unsigned" / "plugin.py").is_file())

    def test_a_second_caller_waits_and_does_not_drop_the_first_plugin(self):
        self.write_registry()
        self.install()
        other = self.tmp / "other-root"
        self.registry = other
        self.write_registry(plugin_id="fixture.beta", source=b"VALUE = 2\n")
        from recursion_core.plugin_security import install as install_mod

        held = threading.Event()
        release = threading.Event()

        def hold():
            fd = install_mod._lock(self.config, wait=True)
            held.set()
            release.wait(5)
            os.close(fd)

        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(held.wait(5))
        from recursion_core.plugin_security.install import InstallRefused
        with self.assertRaises(InstallRefused) as caught:
            self.install("fixture.beta", wait=False)
        self.assertEqual(caught.exception.code, "install_busy")
        self.assertTrue((self.config / "plugins" / "fixture.unsigned" / "plugin.py").is_file())
        self.assertFalse((self.config / "plugins" / "fixture.beta").exists())
        release.set()
        thread.join(5)
        self.install("fixture.beta")
        self.assertEqual(
            [row["plugin_id"] for row in self.listed()["plugins"]],
            ["fixture.beta", "fixture.unsigned"],
        )

    def test_remove_of_one_plugin_leaves_the_other_and_an_unknown_plugin_writes_nothing(self):
        self.write_registry()
        self.install()
        generation = (self.config / "plugin-generations" / "current").read_text()
        other = self.tmp / "other-root"
        saved = self.registry
        self.registry = other
        self.write_registry(plugin_id="fixture.beta", source=b"VALUE = 2\n")
        self.install("fixture.beta")
        self.registry = saved
        self.remove("fixture.unsigned")
        self.assertEqual([row["plugin_id"] for row in self.listed()["plugins"]], ["fixture.beta"])
        from recursion_core.plugin_security.install import InstallRefused
        with self.assertRaises(InstallRefused) as caught:
            self.remove("fixture.unsigned")
        self.assertEqual(caught.exception.code, "plugin_not_installed")
        self.assertGreater(int((self.config / "plugin-generations" / "current").read_text()), int(generation))
        # The failed remove did not take another generation.
        current = int((self.config / "plugin-generations" / "current").read_text())
        with self.assertRaises(InstallRefused):
            self.remove("fixture.missing")
        self.assertEqual(int((self.config / "plugin-generations" / "current").read_text()), current)

    def test_a_release_without_vendored_code_is_not_fetched(self):
        self.write_registry()
        plugin_py = self.registry / "registry" / "fixture.unsigned" / "1.0.0" / "plugin.py"
        plugin_py.unlink()
        from recursion_core.plugin_security.install import InstallRefused
        with self.assertRaises(InstallRefused) as caught:
            self.install()
        self.assertEqual(caught.exception.code, "source_unavailable")
        self.assertFalse((self.config / "plugins").exists())

    def test_files_are_owner_only(self):
        self.write_registry()
        self.install()
        plugin = self.config / "plugins" / "fixture.unsigned"
        for path in (plugin, plugin / "plugin.py", plugin / "manifest.json", self.config / "plugins" / "install.json"):
            mode = path.stat().st_mode
            self.assertEqual(stat.S_IMODE(mode) & 0o077, 0, path)
            self.assertEqual(path.stat().st_uid, os.geteuid())

    def test_cli_proof_and_usage(self):
        self.write_registry()
        from recursion_core.plugin_security._entrypoints import main

        out, err = io.StringIO(), io.StringIO()
        status = main(["plugin", "install", "--registry", str(self.registry), "--plugin", "fixture.unsigned",
                       "--config", str(self.config), "--state", str(self.state)], out, err)
        self.assertEqual(status, 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["registry_label"], "non_verified")
        out, err = io.StringIO(), io.StringIO()
        status = main(["plugin", "list", "--config", str(self.config), "--state", str(self.state)], out, err)
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out.getvalue())["plugins"][0]["registry_label"], "non_verified")
        out, err = io.StringIO(), io.StringIO()
        status = main(["plugin", "remove", "--plugin", "fixture.unsigned", "--config", str(self.config),
                       "--state", str(self.state)], out, err)
        self.assertEqual(status, 0)
        out, err = io.StringIO(), io.StringIO()
        status = main(["plugin", "list", "--config", str(self.config), "--state", str(self.state)], out, err)
        self.assertEqual(json.loads(out.getvalue())["plugins"], [])
        out, err = io.StringIO(), io.StringIO()
        status = main(["plugin"], out, err)
        self.assertEqual(status, 2)
        self.assertIn("usage", err.getvalue())


if __name__ == "__main__":
    unittest.main()
