"""``csl-plugin-manifest/1``: the manifest that names every file of the package (lane PLUGIN-SIGN).

Built against SPINE's draft schema ``schemas/plugin/v1/manifest.v1.json`` (#368, 1.0-draft.1) and
the rules its README says a schema cannot state. vNext stays readable beside it. Keys here are
made up: ``FAKE_KEY`` is a well-formed public key nothing signs with; the signing tests make a
fresh key per run (``support.Key``) and need ``cryptography``.
"""

from __future__ import annotations

import hashlib
import json
import unittest

from recursion_core.plugin_security.canonical import canonical
from recursion_core.plugin_security.errors import Refused
from recursion_core.plugin_security.loader import CoreHealth, PluginLoader
from recursion_core.plugin_security.manifest import (
    ENTRY_FILE, MANIFEST_SCHEMA, MANIFEST_SCHEMA_V1, PERMISSIONS, PERMISSIONS_V1, Grant, ReservedEffects,
    TrustStore, parse_manifest,
)
from recursion_core.plugin_security.signing import key_id
from recursion_core.plugin_security.tests.support import (
    PLUGIN_SOURCE, FakeGilProbe, Key, manifest_bytes, needs_crypto,
)

FAKE_KEY = "11" * 32
NOTES_SOURCE = b"def search(query):\n    return []\n"
_DEFAULT_FILES = object()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def v1_fields(key: str = FAKE_KEY, *, source: bytes = PLUGIN_SOURCE, permissions=("read", "panel"),
              network=(), credentials=(), install_scripts=(), lock=None, files=_DEFAULT_FILES,
              **overrides) -> dict:
    body = {
        "schema": MANIFEST_SCHEMA_V1,
        "plugin_id": "demo.plugin",
        "version": "1.0.0",
        "artifact_sha256": sha(source),
        "dependency_lock_sha256": lock,
        "install_scripts": list(install_scripts),
        "developer_key": key,
        "permissions": list(permissions),
        "network_destinations": list(network),
        "credential_refs": list(credentials),
        "runtime": "python-inprocess/1",
        "interface": "csl-plugin-api/1",
        "files": files if files is not _DEFAULT_FILES else {
            ENTRY_FILE: sha(source), "notes/__init__.py": sha(b""), "notes/index.py": sha(NOTES_SOURCE)},
    }
    body.update(overrides)
    return body


def v1(key: str = FAKE_KEY, **kwargs) -> bytes:
    return canonical(v1_fields(key, **kwargs))


class ParseV1Tests(unittest.TestCase):
    def assertRefused(self, raw: bytes, code: str) -> None:
        with self.assertRaises(Refused) as ctx:
            parse_manifest(raw)
        self.assertEqual(ctx.exception.code, code)

    def test_a_version_1_manifest_parses_with_its_three_new_fields(self) -> None:
        raw = v1()
        manifest = parse_manifest(raw)
        self.assertEqual(manifest.schema, MANIFEST_SCHEMA_V1)
        self.assertEqual((manifest.runtime, manifest.interface), ("python-inprocess/1", "csl-plugin-api/1"))
        self.assertEqual(manifest.files, (("notes/__init__.py", sha(b"")), ("notes/index.py", sha(NOTES_SOURCE)),
                                          (ENTRY_FILE, sha(PLUGIN_SOURCE))))
        self.assertIs(type(manifest.files), tuple)
        self.assertTrue(manifest.names_every_file)
        self.assertEqual(manifest.raw, raw)
        self.assertEqual(manifest.permissions, ("read", "panel"))

    def test_the_parsed_lists_are_immutable_tuples(self) -> None:
        # DeepSeek V4.1 Flash, round 2, L4: what the manifest keeps cannot be changed after the checks.
        manifest = parse_manifest(v1(permissions=("read", "network", "credential"), network=("api.example.org",),
                                     credentials=("vault:test",), install_scripts=("setup",)))
        for name in ("permissions", "network_destinations", "credential_refs", "install_scripts", "files"):
            with self.subTest(field=name):
                self.assertIs(type(getattr(manifest, name)), tuple)

    def test_a_hand_built_version_1_manifest_with_no_files_does_not_name_every_file(self) -> None:
        # DeepSeek V4.1 Flash, round 2, L2.
        from recursion_core.plugin_security.manifest import Manifest
        empty = Manifest("demo.plugin", "1", sha(PLUGIN_SOURCE), None, (), FAKE_KEY, ("read",), (), (), b"",
                         MANIFEST_SCHEMA_V1, "python-inprocess/1", "csl-plugin-api/1", ())
        self.assertFalse(empty.names_every_file)
        self.assertTrue(parse_manifest(v1()).names_every_file)

    def test_vnext_still_parses_and_names_no_file_but_its_artifact(self) -> None:
        manifest = parse_manifest(manifest_bytes(FAKE_KEY))
        self.assertEqual(manifest.schema, MANIFEST_SCHEMA)
        self.assertEqual((manifest.runtime, manifest.interface, manifest.files), (None, None, ()))
        self.assertFalse(manifest.names_every_file)

    def test_a_missing_or_extra_field_is_refused(self) -> None:
        for name in v1_fields():
            with self.subTest(missing=name):
                body = v1_fields()
                del body[name]
                # Without its schema a manifest is read as vNext, whose ten fields it does not have.
                self.assertRefused(canonical(body), "manifest_fields")
        self.assertRefused(v1(trust_tier="verified"), "manifest_fields")
        vnext_fields_with_v1_schema = json.loads(manifest_bytes(FAKE_KEY))
        vnext_fields_with_v1_schema["schema"] = MANIFEST_SCHEMA_V1
        self.assertRefused(canonical(vnext_fields_with_v1_schema), "manifest_fields")
        self.assertRefused(v1(schema=MANIFEST_SCHEMA), "manifest_fields")
        self.assertRefused(v1(schema="csl-plugin-manifest/2"), "manifest_fields")

    def test_runtime_and_interface_refuse_a_value_not_in_their_lists(self) -> None:
        for value in ("python-inprocess/2", "python-subprocess/1", "", ["python-inprocess/1"], None, 1, {}):
            with self.subTest(runtime=value):
                self.assertRefused(v1(runtime=value), "manifest_runtime")
        for value in ("csl-plugin-api/2", "csl-core/v0", "", ["csl-plugin-api/1"], None, 1):
            with self.subTest(interface=value):
                self.assertRefused(v1(interface=value), "manifest_interface")

    def test_new_permissions_are_appended_after_order_in_a_fixed_order(self) -> None:
        self.assertEqual(PERMISSIONS_V1[:len(PERMISSIONS)], PERMISSIONS)
        every = ("read", "propose", "order", "surface", "command", "subscribe", "tool", "run")
        self.assertEqual(parse_manifest(v1(permissions=every)).permissions, every)
        for perms in (("read", "command", "order"), ("read", "run", "tool"), ("surface", "read"),
                      ("read", "read")):
            with self.subTest(perms=perms):
                self.assertRefused(v1(permissions=perms), "manifest_permission_order")
        for perms in (("read", "admin"), ("read", "Run"), ("read", None)):
            with self.subTest(perms=perms):
                self.assertRefused(v1(permissions=perms), "manifest_unknown_permission")

    def test_vnext_refuses_the_version_1_permission_names(self) -> None:
        for name in PERMISSIONS_V1[len(PERMISSIONS):]:
            with self.subTest(name=name):
                self.assertRefused(manifest_bytes(FAKE_KEY, permissions=("read", name)),
                                   "manifest_unknown_permission")

    def test_version_is_printable_ascii_of_at_most_64_characters(self) -> None:
        self.assertEqual(parse_manifest(v1(version="v" * 64)).version, "v" * 64)
        for value in ("", "v" * 65, "1.0\n", "1.0\t", "1.0\x7f", 1, None):
            with self.subTest(version=value):
                self.assertRefused(v1(version=value), "manifest_version")
        for number in list(range(32)) + [127]:  # every control character (Codex GPT-6.1 Sol: "\r" was untested)
            with self.subTest(control=number):
                self.assertRefused(v1(version="1" + chr(number)), "manifest_version")

    def test_every_field_of_another_type_gets_a_typed_refusal(self) -> None:
        # Codex GPT-6.1 Sol's check: no field value raises anything but Refused.
        baseline = v1_fields()
        optional_empty = ("permissions", "install_scripts", "network_destinations", "credential_refs",
                          "dependency_lock_sha256")
        for name, good in baseline.items():
            for value in (None, False, 0, {}, [], ["x"], {"x": 1}, ""):
                if type(value) is type(good) and value == good:
                    continue
                body = dict(baseline)
                body[name] = value
                with self.subTest(field=name, value=value):
                    try:
                        parse_manifest(canonical(body))
                    except Refused:
                        continue
                    self.assertIn(name, optional_empty)  # an empty list or a null lock is a valid value

    def test_the_vnext_rules_hold_for_version_1(self) -> None:
        self.assertRefused(v1(plugin_id="host"), "manifest_plugin_id_reserved")
        self.assertRefused(v1(plugin_id="Demo"), "manifest_plugin_id")
        self.assertRefused(v1(artifact_sha256="AB" * 32), "manifest_artifact_digest")
        self.assertRefused(v1(lock="0" * 63), "manifest_dependency_lock")
        self.assertRefused(v1(permissions=("read", "network")), "manifest_network_mismatch")
        self.assertRefused(v1(network=("api.example.org",)), "manifest_network_mismatch")
        self.assertRefused(v1(permissions=("read", "credential")), "manifest_credential_mismatch")
        self.assertRefused(v1(permissions=("network",), network=("b.example.org", "a.example.org")),
                           "manifest_network_destinations")
        self.assertRefused(v1(install_scripts=("setup", "setup")), "manifest_install_scripts")
        with self.assertRaises(Refused) as ctx:
            parse_manifest(v1(key="AB" * 32))
        self.assertEqual(ctx.exception.code, "malformed_public_key")

    def test_bytes_not_in_canonical_form_are_refused(self) -> None:
        pretty = json.dumps(v1_fields(), indent=2, sort_keys=True).encode("ascii")
        self.assertRefused(pretty, "noncanonical_value")


class FilesTests(unittest.TestCase):
    def assertRefused(self, files: object, code: str, **kwargs) -> None:
        with self.assertRaises(Refused) as ctx:
            parse_manifest(v1(files=files, **kwargs))
        self.assertEqual(ctx.exception.code, code)

    def files(self, **extra) -> dict:
        out = {ENTRY_FILE: sha(PLUGIN_SOURCE)}
        out.update(extra)
        return out

    def test_the_entry_file_is_listed_with_the_artifact_digest(self) -> None:
        self.assertRefused({"notes/index.py": sha(NOTES_SOURCE)}, "manifest_files_entry")
        self.assertRefused({ENTRY_FILE: sha(NOTES_SOURCE)}, "manifest_files_entry")
        self.assertRefused({"Plugin.py": sha(PLUGIN_SOURCE)}, "manifest_files_entry")
        self.assertRefused({"src/plugin.py": sha(PLUGIN_SOURCE)}, "manifest_files_entry")
        self.assertEqual(parse_manifest(v1(files=self.files())).files, ((ENTRY_FILE, sha(PLUGIN_SOURCE)),))

    def test_a_path_that_leaves_or_hides_in_the_package_is_refused(self) -> None:
        for path in ("../../etc/passwd", "../x.py", "./x.py", "/abs.py", ".hidden.py", "notes/.git/config",
                     "notes/..", "a//b.py", "a/", "", "a\\b.py", "a b.py", "C:/x.py", "notes/../plugin.py",
                     "x\n.py", "~/x.py", "a/*.py"):
            with self.subTest(path=path):
                self.assertRefused(self.files(**{path: sha(b"")}), "manifest_files_path")

    def test_path_and_segment_lengths_are_bounded(self) -> None:
        longest = "a" * 255 + "/" + "b" * 254 + "/c"
        self.assertEqual(len(longest), 512)
        self.assertIn(longest, dict(parse_manifest(v1(files=self.files(**{longest: sha(b"")}))).files))
        too_long = "a" * 255 + "/" + "b" * 255 + "/c"
        self.assertRefused(self.files(**{too_long: sha(b"")}), "manifest_files_path")
        self.assertRefused(self.files(**{"a" * 256: sha(b"")}), "manifest_files_path")
        self.assertRefused(self.files(**{"d/" + "a" * 256: sha(b"")}), "manifest_files_path")

    def test_a_digest_that_is_not_lower_case_sha256_is_refused(self) -> None:
        for digest in (sha(b"").upper(), sha(b"")[:63], sha(b"") + "0", "", None, 7, [sha(b"")]):
            with self.subTest(digest=digest):
                self.assertRefused(self.files(**{"notes/index.py": digest}), "manifest_files_digest")
        for char in "ghijklmnopqrstuvwxyzGHIJKLMNOPQRSTUVWXYZ-_: ":  # Codex GPT-6.1 Sol: "g" was untested
            with self.subTest(char=char):
                self.assertRefused(self.files(**{"notes/index.py": char * 64}), "manifest_files_digest")

    def test_a_path_that_is_not_portable_is_refused(self) -> None:
        # Both reads of #374: Windows drops a trailing '.' and keeps device names, so these would name other
        # files, or devices, there.
        for extra in ({"data": sha(b""), "data.": sha(b"")}, {"lib./a.py": sha(b""), "lib/a.py": sha(b"")},
                      {"manifest.json.": sha(b"")}, {"notes/x.": sha(b"")}):
            with self.subTest(paths=sorted(extra)):
                self.assertRefused(self.files(**extra), "manifest_files_path")
        for name in ("CON.txt", "con", "Nul", "aux.py", "prn.tar.gz", "COM1", "lpt9.log", "notes/com9",
                     "con/x.py", "notes/AUX/y.py", "a/nul.d/b.py", "lpt1/com2/z.py", "notes./x.py"):
            # a device name or a trailing dot in any segment, folders included (Codex GPT-6 Astra, round 2)
            with self.subTest(name=name):
                self.assertRefused(self.files(**{name: sha(b"")}), "manifest_files_path")
        # Windows reserves COM1-COM9 and LPT1-LPT9, not COM0 or LPT0 (DeepSeek V4.1 Flash, round 2, L1).
        for name in ("console.py", "conn", "nullable.py", "com10", "com11", "lpt99.txt", "lpt", "auxiliary/x.py", "x.con",
                     "com0", "lpt0.py", "notes/com0/x.py"):
            with self.subTest(accepted=name):
                self.assertIn(name, dict(parse_manifest(v1(files=self.files(**{name: sha(b"")}))).files))

    def test_the_signature_envelope_cannot_be_listed(self) -> None:
        for path in ("manifest.json", "manifest.sig", "Manifest.JSON", "MANIFEST.SIG", "manifest.json/x.py",
                     "Manifest.Sig/a"):
            with self.subTest(path=path):
                self.assertRefused(self.files(**{path: sha(b"")}), "manifest_files_envelope")
        nested = parse_manifest(v1(files=self.files(**{"docs/manifest.json": sha(b"{}")})))
        self.assertIn("docs/manifest.json", dict(nested.files))

    def test_paths_one_file_system_could_confuse_are_refused(self) -> None:
        for extra in ({"Notes.py": sha(b""), "notes.py": sha(b"")},
                      {"lib": sha(b""), "lib/x.py": sha(b"")},
                      {"LIB": sha(b""), "lib/x.py": sha(b"")},
                      {"lib": sha(b""), "LIB/x.py": sha(b"")},
                      {"lib/a": sha(b""), "lib/a/b/c.py": sha(b"")},
                      {"PLUGIN.PY": sha(b"")},
                      {"plugin.py/x.py": sha(b"")}):
            with self.subTest(extra=sorted(extra)):
                self.assertRefused(self.files(**extra), "manifest_files_conflict")

    def test_the_map_holds_one_to_4096_entries(self) -> None:
        self.assertRefused({}, "manifest_files")
        self.assertRefused([[ENTRY_FILE, sha(PLUGIN_SOURCE)]], "manifest_files")
        self.assertRefused(None, "manifest_files")
        # The count ceiling is the map check. A canonical manifest of 4096 files is over
        # the 64 KiB bound, so parse_manifest refuses it before the map is read.
        from recursion_core.plugin_security.manifest import _parse_files
        many = self.files(**{"m/f%04d.py" % i: sha(b"") for i in range(4095)})
        self.assertEqual(len(_parse_files(many, sha(PLUGIN_SOURCE))), 4096)
        extra = dict(many)
        extra["m/f4095.py"] = sha(b"")
        with self.assertRaises(Refused) as caught:
            _parse_files(extra, sha(PLUGIN_SOURCE))
        self.assertEqual(caught.exception.code, "manifest_files")

    def test_a_manifest_over_the_daemon_byte_bound_is_refused_before_the_file_map(self) -> None:
        """The daemon refuses a manifest above 64 KiB. The same bound sits on the parser,
        beside package verification, so a legal file map that does not fit is not parsed."""
        from recursion_core.daemon.plugins import MAX_MANIFEST_BYTES
        files = self.files()
        raw = v1(files=files)
        i = 0
        while len(raw) <= MAX_MANIFEST_BYTES:
            files["m/f%04d.py" % i] = sha(b"")
            i += 1
            raw = v1(files=files)
            if i > 4096:
                self.fail("a 4096-file manifest still fit the daemon bound")
        self.assertGreater(len(raw), MAX_MANIFEST_BYTES)
        self.assertLess(i, 4096)
        with self.assertRaises(Refused) as caught:
            parse_manifest(raw)
        self.assertEqual((caught.exception.code, caught.exception.detail),
                         ("plugin_file_too_large", "manifest.json"))
        del files["m/f%04d.py" % (i - 1)]
        smaller = v1(files=files)
        self.assertLessEqual(len(smaller), MAX_MANIFEST_BYTES)
        self.assertEqual(len(parse_manifest(smaller).files), len(files))


@needs_crypto
class SignatureCoversTheFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dev = Key()
        self.trust = TrustStore({"dev": self.dev.public})
        self.raw = v1(self.dev.public)
        self.signature = self.dev.sign(self.raw)

    def test_a_trusted_signature_over_a_version_1_manifest_is_signed(self) -> None:
        trust, kid, reason = self.trust.classify(parse_manifest(self.raw), self.signature)
        self.assertEqual((trust, kid, reason), ("signed", key_id(self.dev.public), "signature_valid_trusted_key"))

    def test_a_changed_added_or_removed_file_entry_breaks_the_signature(self) -> None:
        listed = v1_fields(self.dev.public)["files"]
        variants = {"removed notes/index.py": {k: v for k, v in listed.items() if k != "notes/index.py"},
                    "added notes/extra.py": dict(listed, **{"notes/extra.py": sha(b"x")})}
        for path in listed:
            if path != ENTRY_FILE:  # the entry's digest is artifact_sha256, which must change with it
                variants["changed " + path] = dict(listed, **{path: sha(b"tampered")})
        for name, files in variants.items():
            with self.subTest(name):
                tampered = parse_manifest(v1(self.dev.public, files=files))
                self.assertEqual(self.trust.classify(tampered, self.signature)[::2],
                                 ("unsigned", "signature_invalid"))

    def test_todays_loader_loads_a_version_1_package_by_its_entry_file(self) -> None:
        raw = v1(self.dev.public, permissions=("read", "propose", "order"))
        grant = Grant("demo.plugin", sha(PLUGIN_SOURCE), key_id(self.dev.public), ("read", "propose", "order"))
        loader = PluginLoader(self.trust, {"demo.plugin": grant}, ReservedEffects(), CoreHealth(), FakeGilProbe())
        loaded = loader.load(raw, self.dev.sign(raw), PLUGIN_SOURCE)
        self.assertEqual((loaded.trust, loaded.permissions), ("signed", ("read", "propose", "order")))
        self.assertEqual(loaded.module.VALUE, 42)
        with self.assertRaises(Refused) as ctx:
            PluginLoader(self.trust, {}, ReservedEffects(), CoreHealth(), FakeGilProbe()).load(
                raw, self.dev.sign(raw), NOTES_SOURCE)
        self.assertEqual(ctx.exception.code, "artifact_digest_mismatch")


if __name__ == "__main__":
    unittest.main()
