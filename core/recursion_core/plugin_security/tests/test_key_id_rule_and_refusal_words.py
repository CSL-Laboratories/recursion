"""Every key id of a grant is read by one rule, and the refusals name what a person needs.

signer_key_id is held to the key-id rule wherever a grant is read (install.json's reader and
``Grant``), through the one table of key-id fields; a malformed one is refused before any plugin
code runs, signed or not. The grant_unbound words name the verified developer key ID of a signed
manifest (never one taken from an unsigned manifest), and the unknown-runtime words name the
runtime that was refused.

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

from recursion_core.plugin_security import manifest as manifest_module
from recursion_core.plugin_security.errors import Refused
from recursion_core.plugin_security.loader import CoreHealth, PluginLoader
from recursion_core.plugin_security.manifest import Grant, ReservedEffects, TrustStore, parse_manifest
from recursion_core.plugin_security.signing import key_id
from recursion_core.plugin_security.tests.support import FakeGilProbe, Key, manifest_bytes, needs_crypto, temp_dir
from recursion_core.plugin_security.tests.test_manifest_v1 import v1

POSIX = unittest.skipUnless(os.name == "posix", "the install reader's file checks are POSIX here")
GOOD = "ed25519:" + "ab" * 16
KEY_ID_FIELDS = ("signer_key_id", "developer_key_id", "order_after_revoked_key")


class _Text(str):
    pass


# The three values of the first report and the nineteen of the second (None and a good id are kept).
REPORTED = ("", "not-a-key", " " + GOOD)
NINETEEN_REFUSED = (
    "ED25519:" + "AB" * 16, "ED25519:" + "ab" * 16, " " + GOOD, GOOD + " ", GOOD + "\n", "\t" + GOOD,
    "ed25519:" + "a" * 31, "ed25519:" + "a" * 33, "", "ed25519:",
    "ed25519:" + "０" * 32, "ed25519:" + "٠" * 32, "ed448:" + "ab" * 16, 0, False, [GOOD],
    _Text(GOOD),
)
KEPT = (None, GOOD)
JSON_REFUSED = tuple(v for v in REPORTED + NINETEEN_REFUSED if type(v) is not _Text)

SIGNER_WORDS = ("the grant for %s in %s has a signer_key_id that is not a developer key ID. A grant is read only "
                "when its keys are null or a key ID of the form ed25519: and 32 lower-case hex digits, so a hand "
                "edit cannot make a plugin run outside the rules for its grant. Set signer_key_id to null or to "
                "the developer key ID, or make the grant again with the install command; no plugin was loaded "
                "and none of its code ran.")
UNBOUND_UNSIGNED = ("grant_unbound: The grant for this plugin in install.json names no developer key ID "
                    "(developer_key_id), so the plugin was not loaded and none of its code ran. Make the grant again "
                    "with developer_key_id set to this plugin's developer key ID, then load the plugin again. This "
                    "manifest is not signed, so the ID is never taken from it: the plugin's developer gives it "
                    "(ed25519: and 32 lower-case hex digits).")
UNBOUND_SIGNED = ("grant_unbound: The grant for this plugin in install.json names no developer key ID "
                  "(developer_key_id), so the plugin was not loaded and none of its code ran. Its manifest is signed "
                  "by a key on this install's trusted list, developer key ID %s: make the grant again with "
                  "signer_key_id and developer_key_id set to %s, then load the plugin again.")
UNKNOWN = ("unknown runtime %s; this release accepts python-sandboxed/1, and python-inprocess/1 until 1.1.0 with the "
           "finding runtime_inprocess_retiring; write python-sandboxed/1 as the manifest's runtime")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def marker_source(test: unittest.TestCase) -> tuple[Path, bytes]:
    marker = Path(temp_dir(test)) / "plugin-code-ran"
    return marker, f'open({str(marker)!r}, "w").close()\n'.encode()


def grant_verdict(name: str, value: object) -> str:
    try:
        Grant("demo.plugin", "0" * 64, value if name == "signer_key_id" else None, ("read",),
              **({} if name == "signer_key_id" else {name: value}))
    except Refused as exc:
        return exc.code
    return "kept"


def read_install_entry(entry: dict) -> tuple[str, object]:
    from recursion_core.daemon.plugins import PluginRefused, read_install
    with tempfile.TemporaryDirectory(prefix="key-id-rule-") as directory:
        path = Path(directory) / "install.json"
        path.write_text(json.dumps({"grants": [entry]}), encoding="utf-8")
        path.chmod(0o600)
        try:
            return "kept", read_install(directory)["grants"]["demo.plugin"]
        except PluginRefused as exc:
            return exc.code, exc.detail.replace(str(path), "INSTALL")


BASE = {"plugin_id": "demo.plugin", "artifact_sha256": "0" * 64, "signer_key_id": None, "permissions": ["read"]}


class SignerKeyIdIsAKeyIdTests(unittest.TestCase):
    def test_a_grant_refuses_a_signer_that_is_not_a_key_id(self) -> None:
        for value in REPORTED + NINETEEN_REFUSED:
            with self.subTest(value=value):
                with self.assertRaises(Refused) as ctx:
                    Grant("demo.plugin", "0" * 64, value, ("read",))
                self.assertEqual((ctx.exception.code, ctx.exception.detail),
                                 ("grant_invalid", "signer_key_id: null, or a developer key ID "
                                                   "(ed25519: and 32 lower-case hex digits)"))

    def test_a_grant_keeps_a_null_signer_or_a_key_id(self) -> None:
        for value in KEPT:
            with self.subTest(value=value):
                self.assertEqual(Grant("demo.plugin", "0" * 64, value, ("read",)).signer_key_id, value)

    def test_the_three_key_id_fields_get_the_same_verdict_from_a_grant(self) -> None:
        for value in REPORTED + NINETEEN_REFUSED + KEPT:
            with self.subTest(value=value):
                verdicts = {grant_verdict(name, value) for name in KEY_ID_FIELDS}
                self.assertEqual(verdicts, {"kept"} if value in KEPT and type(value) is not _Text
                                 else {"grant_invalid"})

    @POSIX
    def test_the_install_reader_refuses_a_signer_that_is_not_a_key_id_with_words_that_say_what_to_do(self) -> None:
        for value in JSON_REFUSED:
            with self.subTest(value=value):
                self.assertEqual(read_install_entry(dict(BASE, signer_key_id=value)),
                                 ("plugin_install_config_invalid", SIGNER_WORDS % ("'demo.plugin'", "INSTALL")))

    @POSIX
    def test_the_install_reader_keeps_a_null_signer_or_a_key_id(self) -> None:
        for value in KEPT:
            with self.subTest(value=value):
                verdict, grant = read_install_entry(dict(BASE, signer_key_id=value))
                self.assertEqual((verdict, grant.signer_key_id), ("kept", value))

    @POSIX
    def test_the_install_reader_gives_the_three_key_id_fields_the_same_verdict(self) -> None:
        for value in JSON_REFUSED + KEPT:
            with self.subTest(value=value):
                verdicts = {read_install_entry(dict(BASE, **{name: value}))[0] for name in KEY_ID_FIELDS}
                self.assertEqual(verdicts, {"kept"} if value in KEPT else {"plugin_install_config_invalid"})

    @POSIX
    def test_a_text_subclass_is_refused_by_the_install_reader_too(self) -> None:
        # JSON gives only plain text, so the subclass is handed to the reader through its parser.
        from recursion_core.daemon import plugins
        real_loads = json.loads

        def loads(text, *args, **kwargs):
            data = real_loads(text, *args, **kwargs)
            for item in data.get("grants", []):
                item["signer_key_id"] = _Text(GOOD)
            return data

        with mock.patch.object(plugins.json, "loads", loads):
            verdict, _ = read_install_entry(dict(BASE, signer_key_id=GOOD))
        self.assertEqual(verdict, "plugin_install_config_invalid")


class OneTableOfKeyIdFieldsTests(unittest.TestCase):
    """install.json's key-id fields are read through one table, and the schema cannot outgrow it."""

    def schema_key_id_fields(self) -> set:
        root = Path(manifest_module.__file__).resolve().parents[3]
        schema = json.loads((root / "schemas" / "plugin" / "v1" / "install.v1.json").read_text(encoding="utf-8"))
        ref = "#/$defs/key_id"

        def names_key_id(node) -> bool:
            if isinstance(node, dict):
                return node.get("$ref") == ref or any(names_key_id(v) for v in node.values())
            if isinstance(node, list):
                return any(names_key_id(v) for v in node)
            return False

        return {name for name, node in schema["$defs"]["grant"]["properties"].items() if names_key_id(node)}

    def test_every_field_the_install_schema_calls_a_key_id_is_in_the_one_table(self) -> None:
        found = self.schema_key_id_fields()
        self.assertTrue({"signer_key_id", "order_after_revoked_key"} <= found, found)
        self.assertEqual(found - set(manifest_module.GRANT_KEY_ID_RULES), set())

    def test_the_table_names_the_three_fields_and_each_rule_is_the_one_key_id_rule(self) -> None:
        self.assertEqual(set(manifest_module.GRANT_KEY_ID_RULES), set(KEY_ID_FIELDS))
        for name, rule in manifest_module.GRANT_KEY_ID_RULES.items():
            with self.subTest(field=name):
                self.assertIs(rule, manifest_module.developer_key_id_ok)


@POSIX
class AMalformedSignerRunsNoCodeTests(unittest.TestCase):
    """End to end through the daemon: the whole install file is refused and no plugin code runs."""

    def run_host(self, signer: object, *, key: Key | None):
        from recursion_core.daemon import plugins
        from recursion_core.daemon.tests import plugin_fixture as fx
        tmp = Path(temp_dir(self))
        os.chmod(tmp, 0o700)
        config, state = tmp / "config" / "recursion", tmp / "state"
        marker = tmp / "plugin-code-ran"
        source = f'open({str(marker)!r}, "w").close()\n'.encode() + fx.OPERATOR_SOURCE
        trusted = {"dev": key.public} if key is not None else {}
        install = fx.write_install(config, trusted=trusted, grants=[fx.grant_for(source, key=key, signer_key_id=signer)])
        fx.write_plugin(config / "plugins" / "fixture", source, key=key)
        with mock.patch.object(plugins, "backend_available", return_value=True), \
                self.assertLogs("recursion_core.daemon.plugins", "ERROR"):
            host = plugins.PluginHost(config, state, gil_probe=FakeGilProbe())
        self.addCleanup(host.close)
        self.assertFalse(marker.exists(), "plugin code ran")
        return [(r["state"], r["code"], r["detail"].replace(str(install / "install.json"), "INSTALL"))
                for r in host.records]

    def test_an_unsigned_plugin_with_a_malformed_signer_is_refused_before_code(self) -> None:
        for value in REPORTED:
            with self.subTest(value=value):
                self.assertEqual(self.run_host(value, key=None),
                                 [("refused", "plugin_install_config_invalid",
                                   SIGNER_WORDS % ("'fixture.operator'", "INSTALL"))])

    @needs_crypto
    def test_a_signed_plugin_with_a_malformed_signer_is_refused_before_code(self) -> None:
        key = Key()
        for value in REPORTED + (key_id(key.public).upper(),):
            with self.subTest(value=value):
                self.assertEqual(self.run_host(value, key=key),
                                 [("refused", "plugin_install_config_invalid",
                                   SIGNER_WORDS % ("'fixture.operator'", "INSTALL"))])


def loader_for(trust: TrustStore, grant: Grant | None) -> PluginLoader:
    return PluginLoader(trust, {"demo.plugin": grant} if grant else {}, ReservedEffects(), CoreHealth(),
                        FakeGilProbe())


class TheUnboundWordsNameTheKeyTests(unittest.TestCase):
    @needs_crypto
    def test_a_signed_manifest_names_its_verified_developer_key_id_and_the_words_work(self) -> None:
        dev = Key()
        kid = key_id(dev.public)
        trust = TrustStore({"dev": dev.public})
        marker, source = marker_source(self)
        raw = manifest_bytes(dev.public, source=source, permissions=("read",))
        try:
            with self.assertRaises(Refused) as ctx:
                loader_for(trust, Grant("demo.plugin", sha(source), None, ("read",))).load(raw, dev.sign(raw), source)
        finally:
            self.assertFalse(marker.exists(), "plugin code ran before the refusal")
        self.assertEqual(str(ctx.exception), UNBOUND_SIGNED % (kid, kid))
        # Doing what the words say gives the plugin its permission.
        done = Grant("demo.plugin", sha(source), kid, ("read",), developer_key_id=kid)
        self.assertEqual(loader_for(trust, done).load(raw, dev.sign(raw), source).permissions, ("read",))

    def test_an_unsigned_manifest_names_no_key_id_it_would_have_to_take_from_the_manifest(self) -> None:
        developer = "11" * 32
        marker, source = marker_source(self)
        raw = manifest_bytes(developer, source=source, permissions=("read",))
        try:
            with self.assertRaises(Refused) as ctx:
                loader_for(TrustStore({}), Grant("demo.plugin", sha(source), None, ("read",))).load(raw, None, source)
        finally:
            self.assertFalse(marker.exists(), "plugin code ran before the refusal")
        self.assertEqual(str(ctx.exception), UNBOUND_UNSIGNED)
        self.assertNotIn(key_id(developer), str(ctx.exception))

    @needs_crypto
    def test_a_manifest_signed_by_an_untrusted_key_is_unsigned_and_names_no_key_id(self) -> None:
        dev = Key()
        marker, source = marker_source(self)
        raw = manifest_bytes(dev.public, source=source, permissions=("read",))
        with self.assertRaises(Refused) as ctx:
            loader_for(TrustStore({}), Grant("demo.plugin", sha(source), None, ("read",))).load(raw, dev.sign(raw),
                                                                                              source)
        self.assertEqual(str(ctx.exception), UNBOUND_UNSIGNED)
        self.assertFalse(marker.exists())


class TheRuntimeWordsNameTheRefusedRuntimeTests(unittest.TestCase):
    def detail(self, value: object) -> str:
        with self.assertRaises(Refused) as ctx:
            parse_manifest(v1(runtime=value))
        self.assertEqual(ctx.exception.code, "manifest_runtime")
        return ctx.exception.detail

    def test_the_refused_runtime_is_named_as_json(self) -> None:
        for value, shown in (("python-subprocess/1", '"python-subprocess/1"'), ("", '""'), (None, "null"),
                             (1, "1"), (["python-sandboxed/1"], '["python-sandboxed/1"]'), ({}, "{}")):
            with self.subTest(runtime=value):
                self.assertEqual(self.detail(value), UNKNOWN % shown)

    def test_the_shown_runtime_is_ascii_and_cut_at_the_shown_value_length(self) -> None:
        cut = manifest_module.SHOWN_VALUE_CHARS
        # A canonical manifest holds ASCII text only, so the ASCII step is seen on the shown form itself.
        self.assertEqual(manifest_module._shown("café/1"), '"caf\\u00e9/1"')
        detail = self.detail('python-"quoted"\\/1')
        self.assertEqual(detail, UNKNOWN % '"python-\\"quoted\\"\\\\/1"')
        self.assertTrue(detail.isascii())
        long_name = "x" * (cut * 3)
        self.assertEqual(self.detail(long_name), UNKNOWN % ('"' + long_name)[:cut])

    def test_the_cut_is_the_one_the_reserved_effects_show(self) -> None:
        cut = manifest_module.SHOWN_VALUE_CHARS
        with self.assertRaises(Refused) as ctx:
            ReservedEffects(hosts=("X" * (cut * 3),))
        self.assertIn("X" * (cut - 1), ctx.exception.detail)
        self.assertNotIn("X" * cut, ctx.exception.detail)


if __name__ == "__main__":
    unittest.main()
