"""G2 loader: signatures, tampering, grant intersection, reserved effects, doctor, GIL poison.

GIL states in this file come from ``FakeGilProbe`` (simulated). The real-interpreter
GIL checks are in ``test_runtime.py``.
"""

from __future__ import annotations

import hashlib
import unittest

from recursion_core.plugin_security import (
    CoreHealth, Grant, PluginLoader, Refused, ReservedEffects, TrustStore, canonical, parse,
)
from recursion_core.plugin_security.tests.support import (
    PLUGIN_SOURCE, FakeGilProbe, Key, manifest_bytes, needs_crypto,
)

DIGEST = hashlib.sha256(PLUGIN_SOURCE).hexdigest()
ALL = ("read", "propose", "order")
RESERVED = ReservedEffects(frozenset({"venue.example"}), ("venue:",))


def _loader(dev: Key | None, grant: Grant | None, probe=None, revoked=frozenset(), health=None):
    trust = TrustStore({"dev": dev.public} if dev else {}, revoked)
    return PluginLoader(trust, {"demo.plugin": grant} if grant else {}, RESERVED,
                        health or CoreHealth(), probe or FakeGilProbe())


def _kid(key: Key) -> str:
    from recursion_core.plugin_security.signing import key_id
    return key_id(key.public)


def _codes(findings) -> list[str]:
    return [f["code"] for f in findings]


@needs_crypto
class SignatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dev = Key()
        self.raw = manifest_bytes(self.dev.public)
        self.grant = Grant("demo.plugin", DIGEST, _kid(self.dev), ALL)

    def test_trusted_signature_loads_signed_with_order(self) -> None:
        loaded = _loader(self.dev, self.grant).load(self.raw, self.dev.sign(self.raw), PLUGIN_SOURCE)
        self.assertEqual((loaded.trust, loaded.permissions), ("signed", ALL))
        self.assertEqual(loaded.module.VALUE, 42)

    def test_malicious_signatures_load_as_unsigned_without_order(self) -> None:
        other = Key()
        good = self.dev.sign(self.raw)
        flipped = format(int(good[:2], 16) ^ 1, "02x") + good[2:]
        bad_signatures = {
            "flipped_bit": flipped, "other_key": other.sign(self.raw), "other_bytes": self.dev.sign(b"x"),
            "zeros": "00" * 64, "short": good[:-2], "long": good + "00", "upper_hex": good.upper(),
            "not_hex": "zz" * 64, "empty": "", "none": None, "int": 7, "bytes": bytes.fromhex(good),
        }
        for name, sig in bad_signatures.items():
            with self.subTest(name):
                loader = _loader(self.dev, Grant("demo.plugin", DIGEST, None, ALL))
                loaded = loader.load(self.raw, sig, PLUGIN_SOURCE)
                self.assertEqual(loaded.trust, "unsigned")
                self.assertEqual(loaded.permissions, ("read", "propose"))
                self.assertIn("unsigned_order_requires_manual_grant", _codes(loaded.findings))

    def test_untrusted_valid_signer_installs_as_unsigned(self) -> None:
        stranger = Key()
        raw = manifest_bytes(stranger.public)
        loaded = _loader(self.dev, Grant("demo.plugin", DIGEST, None, ("read",))).load(
            raw, stranger.sign(raw), PLUGIN_SOURCE)
        self.assertEqual((loaded.trust, loaded.permissions), ("unsigned", ("read",)))
        self.assertIn("signer_not_on_trusted_list", _codes(loaded.findings))

    def test_revoked_key_counts_as_unsigned_and_loses_signed_grant(self) -> None:
        loader = _loader(self.dev, self.grant, revoked=frozenset({_kid(self.dev)}))
        loaded = loader.load(self.raw, self.dev.sign(self.raw), PLUGIN_SOURCE)
        self.assertEqual((loaded.trust, loaded.permissions), ("unsigned", ()))
        self.assertIn("grant_signer_mismatch", _codes(loaded.findings))

    def test_manifest_tampered_after_signing_loses_grant(self) -> None:
        sig = self.dev.sign(self.raw)
        data = parse(self.raw)
        data["version"] = "1.0.1"
        loaded = _loader(self.dev, self.grant).load(canonical(data), sig, PLUGIN_SOURCE)
        self.assertEqual((loaded.trust, loaded.permissions), ("unsigned", ()))

    def test_artifact_bytes_tampered_is_refused(self) -> None:
        loader = _loader(self.dev, self.grant)
        for tampered in (PLUGIN_SOURCE + b"\n", b"VALUE = 0\n", PLUGIN_SOURCE[:-1]):
            with self.subTest(tampered=tampered), self.assertRaises(Refused) as ctx:
                loader.load(self.raw, self.dev.sign(self.raw), tampered)
            self.assertEqual(ctx.exception.code, "artifact_digest_mismatch")
        self.assertEqual(loader.permissions("demo.plugin"), ())

    def test_update_with_new_digest_loses_grant(self) -> None:
        new_source = b"VALUE = 43\n"
        raw = manifest_bytes(self.dev.public, source=new_source)
        loaded = _loader(self.dev, self.grant).load(raw, self.dev.sign(raw), new_source)
        self.assertEqual((loaded.trust, loaded.permissions), ("signed", ()))
        self.assertIn("grant_artifact_mismatch", _codes(loaded.findings))

    def test_verified_is_a_label_not_a_gate(self) -> None:
        for label in ("verified", "non_verified", None, "forged-label"):
            with self.subTest(label=label):
                loaded = _loader(self.dev, self.grant).load(self.raw, self.dev.sign(self.raw),
                                                            PLUGIN_SOURCE, registry_label=label)
                self.assertEqual(loaded.permissions, ALL)
                self.assertEqual(loaded.registry_label, label if label in ("verified", "non_verified")
                                 else "unlisted")
        stranger = Key()
        raw = manifest_bytes(stranger.public)
        loaded = _loader(self.dev, Grant("demo.plugin", DIGEST, None, ALL)).load(
            raw, stranger.sign(raw), PLUGIN_SOURCE, registry_label="verified")
        self.assertEqual(loaded.trust, "unsigned")  # the label does not make a key trusted
        self.assertNotIn("order", loaded.permissions)


@needs_crypto
class GrantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dev = Key()

    def _load(self, requested, granted, signed=True, ack=False):
        raw = manifest_bytes(self.dev.public, permissions=requested)
        grant = Grant("demo.plugin", DIGEST, _kid(self.dev) if signed else None, granted, ack)
        return _loader(self.dev if signed else None, grant).load(raw, self.dev.sign(raw), PLUGIN_SOURCE)

    def test_intersection_narrows_in_canonical_order(self) -> None:
        cases = [
            (("read", "propose", "order"), ("order", "read"), ("read", "order")),
            (("read",), ("read", "propose", "order"), ("read",)),
            (("read", "order"), (), ()),
            (("propose", "order"), ("propose",), ("propose",)),
        ]
        for requested, granted, expected in cases:
            with self.subTest(requested=requested, granted=granted):
                self.assertEqual(self._load(requested, granted).permissions, expected)

    def test_unsigned_order_needs_manual_acknowledged_grant(self) -> None:
        self.assertEqual(self._load(ALL, ALL, signed=False).permissions, ("read", "propose"))
        self.assertEqual(self._load(ALL, ALL, signed=False, ack=True).permissions, ALL)

    def test_requested_permission_order_is_part_of_the_manifest(self) -> None:
        for perms in (("order", "read"), ("read", "read"), ("read", "admin")):
            with self.subTest(perms=perms), self.assertRaises(Refused) as ctx:
                raw = manifest_bytes(self.dev.public, permissions=perms)
                _loader(self.dev, None).load(raw, self.dev.sign(raw), PLUGIN_SOURCE)
            self.assertIn(ctx.exception.code, ("manifest_permission_order", "manifest_unknown_permission"))

    def test_no_reserved_credential_or_destination(self) -> None:
        cases = {
            "reserved_destination": dict(permissions=("network",), network=("api.venue.example",)),
            "reserved_credential": dict(permissions=("credential",), credentials=("venue:key",)),
        }
        for code, kwargs in cases.items():
            with self.subTest(code=code):
                raw = manifest_bytes(self.dev.public, **kwargs)
                loader = _loader(self.dev, Grant("demo.plugin", DIGEST, _kid(self.dev), kwargs["permissions"]))
                with self.assertRaises(Refused) as ctx:
                    loader.load(raw, self.dev.sign(raw), PLUGIN_SOURCE)
                self.assertEqual(ctx.exception.code, code)
                self.assertEqual(loader.doctor(raw, self.dev.sign(raw), PLUGIN_SOURCE)[-1]["code"], code)

    def test_unreserved_network_grant_is_reported(self) -> None:
        raw = manifest_bytes(self.dev.public, permissions=("network",), network=("docs.example.org",))
        loaded = _loader(self.dev, Grant("demo.plugin", DIGEST, _kid(self.dev), ("network",))).load(
            raw, self.dev.sign(raw), PLUGIN_SOURCE)
        self.assertIn("network_granted", _codes(loaded.findings))

    def test_malformed_manifests_refused(self) -> None:
        good = parse(manifest_bytes(self.dev.public))
        mutations = {
            "manifest_fields": {"extra": 1},
            "manifest_schema": {"schema": "csl-plugin-manifest/v1"},
            "manifest_plugin_id": {"plugin_id": "Bad Id"},
            "manifest_artifact_digest": {"artifact_sha256": "ab"},
            "malformed_public_key": {"developer_key": "zz"},
            "manifest_network_mismatch": {"network_destinations": ["a.example.org"]},
            "manifest_credential_mismatch": {"credential_refs": ["x:y"]},
        }
        for code, change in mutations.items():
            with self.subTest(code=code), self.assertRaises(Refused) as ctx:
                _loader(self.dev, None).load(canonical({**good, **change}), "", PLUGIN_SOURCE)
            self.assertEqual(ctx.exception.code, code)
        with self.assertRaises(Refused):
            _loader(self.dev, None).load(b'{"schema": 1}', "", PLUGIN_SOURCE)


@needs_crypto
class DoctorAndGilTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dev = Key()
        self.raw = manifest_bytes(self.dev.public)
        self.grant = Grant("demo.plugin", DIGEST, _kid(self.dev), ALL)

    def test_doctor_reports_every_stage(self) -> None:
        loaded = _loader(self.dev, self.grant).load(self.raw, self.dev.sign(self.raw), PLUGIN_SOURCE,
                                                     registry_label="non_verified")
        self.assertEqual({f["stage"] for f in loaded.findings}, {"verify", "admit", "stage", "active"})
        self.assertIn("in_process_not_contained", [f["detail"] for f in loaded.findings])

    def test_doctor_seeded_bad_vs_clean(self) -> None:
        loader = _loader(self.dev, self.grant)
        clean = loader.doctor(self.raw, self.dev.sign(self.raw), PLUGIN_SOURCE)
        self.assertFalse([f for f in clean if f["severity"] in ("warn", "refuse")])
        bad_raw = manifest_bytes(self.dev.public, install_scripts=("postinstall.sh",), lock=None)
        bad = loader.doctor(bad_raw, "00" * 64, PLUGIN_SOURCE)
        self.assertTrue({"signature_invalid", "install_scripts_present", "no_dependency_lock"}
                        <= set(_codes(bad)))

    def test_gil_turned_on_by_load_poisons_core(self) -> None:
        health = CoreHealth()
        probe = FakeGilProbe([True])
        loader = _loader(self.dev, self.grant, probe, health=health)
        with self.assertRaises(Refused) as ctx:
            loader.load(self.raw, self.dev.sign(self.raw), PLUGIN_SOURCE)
        self.assertEqual(ctx.exception.code, "core_poisoned_gil_enabled")
        self.assertEqual(loader.permissions("demo.plugin"), ())
        with self.assertRaises(Refused) as ctx:
            loader.load(self.raw, self.dev.sign(self.raw), PLUGIN_SOURCE)
        self.assertEqual(ctx.exception.code, "core_poisoned_restart_required")

    def test_gil_checked_after_failing_plugin(self) -> None:
        source = b"raise RuntimeError('boom')\n"
        raw = manifest_bytes(self.dev.public, source=source)
        for gil_on, code in ((False, "plugin_import_failed"), (True, "core_poisoned_gil_enabled")):
            with self.subTest(gil_on=gil_on):
                probe, health = FakeGilProbe([gil_on]), CoreHealth()
                with self.assertRaises(Refused) as ctx:
                    _loader(self.dev, None, probe, health=health).load(raw, self.dev.sign(raw), source)
                self.assertEqual(ctx.exception.code, code)
                self.assertEqual(probe.calls, 1)
                self.assertEqual(bool(health.poisoned), gil_on)

    def test_gil_checked_after_system_exit_in_plugin(self) -> None:
        source = b"raise SystemExit(3)\n"
        raw = manifest_bytes(self.dev.public, source=source)
        probe = FakeGilProbe([True])
        with self.assertRaises(Refused):
            _loader(self.dev, None, probe).load(raw, self.dev.sign(raw), source)
        self.assertEqual(probe.calls, 1)

    def test_poison_after_earlier_good_load_drops_all_permissions(self) -> None:
        health = CoreHealth()
        probe = FakeGilProbe([False, True])
        loader = _loader(self.dev, self.grant, probe, health=health)
        loader.load(self.raw, self.dev.sign(self.raw), PLUGIN_SOURCE)
        self.assertEqual(loader.permissions("demo.plugin"), ALL)
        other = b"Z = 1\n"
        raw2 = manifest_bytes(self.dev.public, plugin_id="other.plugin", source=other)
        with self.assertRaises(Refused):
            loader.load(raw2, self.dev.sign(raw2), other)
        self.assertEqual(loader.permissions("demo.plugin"), ())

    def test_compile_failure_and_duplicate(self) -> None:
        bad = b"def (:\n"
        raw = manifest_bytes(self.dev.public, source=bad)
        with self.assertRaises(Refused) as ctx:
            _loader(self.dev, None).load(raw, self.dev.sign(raw), bad)
        self.assertEqual(ctx.exception.code, "plugin_compile_failed")
        loader = _loader(self.dev, self.grant)
        loader.load(self.raw, self.dev.sign(self.raw), PLUGIN_SOURCE)
        with self.assertRaises(Refused) as ctx:
            loader.load(self.raw, self.dev.sign(self.raw), PLUGIN_SOURCE)
        self.assertEqual(ctx.exception.code, "plugin_already_active")


if __name__ == "__main__":
    unittest.main()
