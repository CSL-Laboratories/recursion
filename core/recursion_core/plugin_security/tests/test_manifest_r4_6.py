"""Review R4, finding 6, under Lucas's D0 (Nov 12, 2026): normal form, ports, grants bound to a key.

D0: "take the stricter, fail-closed option that the reviewer's test already encodes", which lane C6
reads as: an entry not in normal form is refused as bad installer configuration, ports outside 1 to
65535 are refused, and a manual grant is bound to the developer key it was given for. The reviewer's
four tests (R4 ``fix-6.patch``, Codex GPT-6 Astra, SHA-256 88ad6d6f...) are kept here in their strict
reading. Raw IP destinations are refused by Lucas's decision B44 (Nov 14, 2026, C6's question): "a
plugin's manifest may NOT name a raw IP address as a network destination".

Keys are made up: ``K1``/``K2`` are well-formed public keys nothing signs with; the tests that need
a signature or the revocation store's host op make a fresh key per run and need ``cryptography``.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import socket
import string
import unittest
from pathlib import Path
from unittest import mock

from recursion_core.plugin_security.canonical import canonical
from recursion_core.plugin_security.errors import Refused
from recursion_core.plugin_security.loader import CoreHealth, PluginLoader
from recursion_core.plugin_security import manifest as manifest_module
from recursion_core.plugin_security.manifest import (
    PERMISSIONS_V1, Grant, ReservedEffects, TrustStore, destination_in_normal_form, effective_permissions,
    host_in_normal_form, parse_manifest,
)
from recursion_core.plugin_security.signing import key_id
from recursion_core.plugin_security.tests.support import (
    PLUGIN_SOURCE, FakeGilProbe, Key, World, manifest_bytes, needs_crypto, temp_dir,
)
from recursion_core.plugin_security.tests.test_manifest_v1 import v1

K1, K2 = "11" * 32, "22" * 32
DIGEST = hashlib.sha256(PLUGIN_SOURCE).hexdigest()
ALL = ("read", "propose", "order")
INSTALL_CONFIG = "plugin_install_config_invalid"


def refusal(call, *args, **kwargs) -> str:
    try:
        call(*args, **kwargs)
    except Refused as exc:
        return exc.code
    return "accepted"


class ReservedNormalFormTests(unittest.TestCase):
    def test_reserved_host_aliases_are_refused_as_installer_configuration(self) -> None:
        # R4 fix-6: "rejected or still block the destination"; D0 takes "rejected".
        for host in ("Venue.Example", "api.venue.example:443", "venue.example.", "127.0.0.1", "10.0.0.1:80",
                     "0x7f.1", "venue.0x", "localhost", "", " venue.example", "venue..example",
                     "-venue.example", "venue.example/", "*.venue.example", "venue_x.example",
                     "a" * 64 + ".example", None, 5, b"venue.example"):
            with self.subTest(host=host):
                self.assertEqual(refusal(ReservedEffects, hosts=frozenset({host})), INSTALL_CONFIG)

    def test_uppercase_or_malformed_credential_prefix_is_refused(self) -> None:
        for prefix in ("VENUE:", "Venue/", "", ":venue", " venue", "venue key", "v" * 129, None, b"venue:"):
            with self.subTest(prefix=prefix):
                self.assertEqual(refusal(ReservedEffects, credential_prefixes=(prefix,)), INSTALL_CONFIG)

    def test_entries_in_normal_form_reserve_the_host_its_subdomains_and_prefixed_names(self) -> None:
        policy = ReservedEffects(frozenset({"venue.example"}), ("venue:", "v"))
        for destination, reserved in (("venue.example", True), ("api.venue.example:443", True),
                                      ("othervenue.example", False), ("venue.example.org", False)):
            with self.subTest(destination=destination):
                self.assertIs(policy.host_reserved(destination), reserved)
        self.assertTrue(policy.credential_reserved("venue:secret"))
        self.assertTrue(policy.credential_reserved("vault/x"))
        self.assertFalse(ReservedEffects(credential_prefixes=("venue:",)).credential_reserved("other:secret"))

    def test_lists_are_taken_once_and_held_immutably(self) -> None:
        hosts, prefixes = ["venue.example"], ["venue:"]
        policy = ReservedEffects(hosts, prefixes)
        hosts.append("Bad.Example")
        prefixes.append("BAD")
        self.assertEqual((policy.hosts, policy.credential_prefixes), (frozenset({"venue.example"}), ("venue:",)))
        self.assertIs(type(policy.hosts), frozenset)
        self.assertEqual(ReservedEffects(), ReservedEffects(frozenset(), ()))

    def test_every_upper_case_letter_is_refused_in_hosts_and_prefixes(self) -> None:
        # Codex GPT-6.1 Sol: a test with "Venue.Example" alone let a pattern admitting "Z" through.
        for char in string.ascii_uppercase:
            with self.subTest(char=char):
                self.assertFalse(host_in_normal_form(char + ".example"))
                self.assertFalse(host_in_normal_form("api.x" + char + ".example"))
                self.assertFalse(host_in_normal_form("api.example." + char + "x"))  # in the last label too
                for last in ("x" + char + "y", "xy" + char):  # inside and at the end of it (Codex GPT-6.1 Sol, round 2)
                    self.assertFalse(host_in_normal_form("api." + last))
                    self.assertFalse(destination_in_normal_form("api." + last + ":443"))
                self.assertEqual(refusal(ReservedEffects, credential_prefixes=(char + ":",)), INSTALL_CONFIG)
                self.assertEqual(refusal(ReservedEffects, credential_prefixes=("vault" + char,)), INSTALL_CONFIG)

    def test_what_is_stored_is_what_was_checked(self) -> None:
        # Codex GPT-6.1 Sol's check, made deterministic (no thread, no timeout): the caller's lists change
        # while they are being checked; the stored values are the ones checked, for hosts and prefixes.
        hosts, prefixes = ["one.example", "two.example"], ["vault:", "other:"]
        check_host, prefix_pattern = manifest_module.host_in_normal_form, manifest_module._CRED_PREFIX

        def host_check(host):
            if host == "two.example":
                hosts[0] = "UPPER.invalid"
            return check_host(host)

        class PrefixPattern:
            def fullmatch(self, value):
                if value == "other:":
                    prefixes[0] = "UPPER:"
                return prefix_pattern.fullmatch(value)

        with mock.patch.object(manifest_module, "host_in_normal_form", host_check), \
                mock.patch.object(manifest_module, "_CRED_PREFIX", PrefixPattern()):
            policy = ReservedEffects(hosts, prefixes)
        self.assertEqual(hosts[0], "UPPER.invalid")  # the change did happen during the check
        self.assertEqual(policy.hosts, frozenset({"one.example", "two.example"}))
        self.assertEqual(policy.credential_prefixes, ("vault:", "other:"))

    def test_a_string_or_mapping_in_place_of_a_list_is_refused(self) -> None:
        # A string is iterable: "venue.example" would otherwise reserve its single letters.
        for value in ("venue.example", {"venue.example": True}, None, 3):
            with self.subTest(value=value):
                self.assertEqual(refusal(ReservedEffects, hosts=value), INSTALL_CONFIG)
                self.assertEqual(refusal(ReservedEffects, credential_prefixes=value), INSTALL_CONFIG)
        # Every letter of "venue" is itself a prefix in normal form, so only the type check refuses it.
        self.assertEqual(refusal(ReservedEffects, credential_prefixes="venue"), INSTALL_CONFIG)
        # A set is refused too (DeepSeek V4.1 Flash, round 2, M1): reading a set another thread is still changing
        # raises RuntimeError, not this refusal. A frozenset, tuple or list is taken.
        self.assertEqual(refusal(ReservedEffects, hosts={"venue.example"}), INSTALL_CONFIG)
        self.assertEqual(refusal(ReservedEffects, credential_prefixes={"venue:"}), INSTALL_CONFIG)
        for kind in (frozenset, tuple, list):
            with self.subTest(kind=kind.__name__):
                policy = ReservedEffects(kind(["venue.example"]), kind(["venue:"]))
                self.assertEqual((policy.hosts, policy.credential_prefixes), (frozenset({"venue.example"}), ("venue:",)))

    @unittest.skipUnless(os.name == "posix", "the daemon's plugin reader opens files with O_NOFOLLOW (Linux)")
    def test_the_daemon_refuses_an_install_file_with_a_reserved_entry_not_in_normal_form(self) -> None:
        from recursion_core.daemon.plugins import read_install  # PLUGIN-HOST's reader, unchanged
        directory = Path(temp_dir(self))
        for reserved in ({"hosts": ["Venue.Example"]}, {"hosts": ["venue.example:443"]},
                         {"credential_prefixes": ["VENUE:"]}):
            with self.subTest(reserved=reserved):
                path = directory / "install.json"
                path.write_text(json.dumps({"reserved": reserved}))
                os.chmod(path, 0o600)
                self.assertEqual(refusal(read_install, directory), INSTALL_CONFIG)
        path.write_text(json.dumps({"reserved": {"hosts": ["venue.example"], "credential_prefixes": ["venue:"]}}))
        self.assertEqual(read_install(directory)["reserved"].hosts, frozenset({"venue.example"}))


class PortAndAddressTests(unittest.TestCase):
    def destinations(self, *destinations: str):
        """The refusal code for each form of manifest declaring these destinations."""
        perms = ("read", "network")
        return (refusal(parse_manifest, manifest_bytes(K1, permissions=perms, network=destinations)),
                refusal(parse_manifest, v1(K1, permissions=perms, network=destinations)))

    def test_out_of_range_or_unnormal_ports_are_refused(self) -> None:
        # R4 fix-6 tests 0 and 99999; normal form also refuses a leading zero, a sign and an empty port.
        for port in ("0", "99999", "65536", "100000", "00443", "0443", "+443", "-1", "", "44a", "443:1"):
            with self.subTest(port=port):
                self.assertEqual(self.destinations("api.venue.example:" + port),
                                 ("manifest_network_destinations",) * 2)

    def test_ports_in_range_are_accepted(self) -> None:
        for destination in ("api.venue.example:1", "api.venue.example:443", "api.venue.example:65535",
                            "api.venue.example"):
            with self.subTest(destination=destination):
                self.assertEqual(self.destinations(destination), ("accepted", "accepted"))

    def test_raw_ip_destinations_are_refused(self) -> None:
        # Lucas's decision B44. The spellings of the marketplace lane's probe (q7_probe.py), each one a
        # resolver reads as an address, and more: a last label must begin with a letter.
        for destination in ("10.0.0.1:8443", "203.0.113.5", "127.0.0.1", "169.254.169.254:80", "127.1",
                            "0x7f.1", "0177.0.0.1", "0x7f.0.0.1", "10.0.0.1:99999", "2130706433", "[::1]:443",
                            "::1", "127.0.0.1:8765", "10.0.0.256", "1.2.3.4:443", "example.0x", "1.0x7f",
                            "example.1abc", "localhost"):
            with self.subTest(destination=destination):
                self.assertEqual(self.destinations(destination), ("manifest_network_destinations",) * 2)
        for destination in ("api.example.com", "api.example.com:443", "example.com", "x1.example.co",
                            "123.example.com", "a.b", "1.example", "api.3com.example", "0x7f.example",
                            "xn--80ak6aa92e.com"):
            with self.subTest(destination=destination):
                self.assertEqual(self.destinations(destination), ("accepted", "accepted"))

    def test_no_spelling_the_system_resolver_reads_as_ipv4_is_a_destination(self) -> None:
        # Ties the rule to the platform's own reading: whatever inet_aton takes as an address, the
        # grammar refuses, alone and with a port. (No name is resolved; inet_aton parses text only.)
        parts = ("0", "1", "127", "255", "256", "0177", "0x7f", "0x0", "4294967295", "08", "0x")
        candidates = {".".join(combo) for n in range(1, 5) for combo in itertools.product(parts, repeat=n)}
        taken = 0
        for host in sorted(candidates):
            try:
                socket.inet_aton(host)
            except OSError:
                continue
            taken += 1
            self.assertFalse(destination_in_normal_form(host), host)
            self.assertFalse(destination_in_normal_form(host + ":443"), host)
            self.assertFalse(host_in_normal_form(host), host)
        self.assertGreater(taken, 100)

    def test_port_range_edges(self) -> None:
        for port in ("1", "9", "10", "9999", "10000", "59999", "60000", "64999", "65000", "65499", "65500",
                     "65529", "65530", "65535"):
            with self.subTest(port=port):
                self.assertTrue(destination_in_normal_form("api.example.org:" + port))
        for port in ("0", "00", "01", "65536", "65539", "65540", "65599", "65600", "65999", "66000", "69999",
                     "70000", "99999", "100000", "1e3", " 443", "443 "):
            with self.subTest(port=port):
                self.assertFalse(destination_in_normal_form("api.example.org:" + port))

    def test_one_expression_decides_a_destination_and_one_a_reserved_host(self) -> None:
        # The marketplace checker copies these two patterns; the predicates are exactly them, so a
        # copy that matches the pattern agrees with the parser. The plugin schema (manifest.v1.json,
        # SPINE's) does not copy them yet: it still accepts ports the parser refuses.
        from recursion_core.plugin_security import manifest as m
        for value in ("api.example.org", "api.example.org:443", "a.b", "127.0.0.1", "api.example.org:0",
                      "API.example.org", "a" * 64 + ".org", "api.example.org.", "x.y:65535", "x.y:65536"):
            with self.subTest(value=value):
                self.assertEqual(destination_in_normal_form(value), m._HOST.fullmatch(value) is not None)
                self.assertEqual(host_in_normal_form(value), m._HOSTNAME.fullmatch(value) is not None)
        self.assertTrue(m._HOST.pattern.startswith(m._HOSTNAME.pattern))

    def test_a_host_name_is_at_most_253_characters(self) -> None:
        name = ".".join(("a" * 63, "b" * 63, "c" * 63, "d" * 61))
        self.assertEqual(len(name), 253)
        self.assertTrue(host_in_normal_form(name))
        self.assertEqual(self.destinations(name), ("accepted", "accepted"))
        self.assertFalse(host_in_normal_form(name + "d"))
        self.assertEqual(self.destinations(name[:-1] + "dd"), ("manifest_network_destinations",) * 2)
        # The 253 counts the host only, never its port.
        self.assertTrue(destination_in_normal_form(name + ":65535"))
        self.assertFalse(destination_in_normal_form(name[:-1] + "dd:1"))

    def test_a_label_is_at_most_63_characters(self) -> None:
        # The earlier pattern let a label of 64 characters through; DNS stops at 63.
        self.assertEqual(self.destinations("a" * 63 + ".example"), ("accepted", "accepted"))
        self.assertEqual(self.destinations("api." + "b" * 63), ("accepted", "accepted"))
        self.assertEqual(self.destinations("a" * 64 + ".example"), ("manifest_network_destinations",) * 2)
        self.assertEqual(self.destinations("api." + "b" * 64 + ":443"), ("manifest_network_destinations",) * 2)

    def test_the_predicates_refuse_what_is_not_a_string(self) -> None:
        class Text(str):  # a str subclass can change what comparing or matching it means
            pass

        class Named:
            def __str__(self) -> str:
                return "api.venue.example"

        for value in (None, 443, b"api.venue.example", ["api.venue.example"], Text("api.venue.example"), Named()):
            with self.subTest(value=value):
                self.assertFalse(host_in_normal_form(value))
                self.assertFalse(destination_in_normal_form(value))


class GrantBoundToDeveloperKeyTests(unittest.TestCase):
    """``effective_permissions`` alone: no signature, so these run without ``cryptography``."""

    def setUp(self) -> None:
        self.m1 = parse_manifest(manifest_bytes(K1))
        self.m2 = parse_manifest(manifest_bytes(K2))  # the same artifact, rewritten under another key

    def test_a_manual_grant_follows_the_developer_key_it_was_given_for(self) -> None:
        grant = Grant("demo.plugin", DIGEST, None, ALL, True, developer_key_id=key_id(K1))
        self.assertEqual(effective_permissions(self.m1, "unsigned", None, grant), (ALL, []))
        self.assertEqual(effective_permissions(self.m2, "unsigned", None, grant),
                         ((), [("grant_developer_key_mismatch", "grant is for another developer key; grant again")]))

    def test_an_unbound_manual_grant_grants_nothing_in_either_form(self) -> None:
        # Both reads of #374 (Codex GPT-6 Astra R2, GPT-6.1 Sol R1): D0 binds the whole manual grant.
        missing = ((), [("grant_developer_key_missing", "manual grant names no developer key; grant again")])
        wide = ("read", "network", "credential", "process", "order")
        for raw in (manifest_bytes(K1), manifest_bytes(K2), v1(K1, permissions=ALL),
                    manifest_bytes(K1, permissions=wide, network=("api.example.org",), credentials=("vault:test",))):
            manifest = parse_manifest(raw)
            with self.subTest(schema=manifest.schema, permissions=manifest.permissions):
                for ack in (True, False):
                    unbound = Grant("demo.plugin", DIGEST, None, manifest.permissions, ack)
                    self.assertEqual(effective_permissions(manifest, "unsigned", None, unbound), missing)
                bound = Grant("demo.plugin", DIGEST, None, manifest.permissions, True,
                              developer_key_id=key_id(manifest.developer_key))
                self.assertEqual(effective_permissions(manifest, "unsigned", None, bound), (manifest.permissions, []))
        read_only = Grant("demo.plugin", DIGEST, None, ("read",), developer_key_id=key_id(K1))
        self.assertEqual(effective_permissions(self.m1, "unsigned", None, read_only)[0], ("read",))

    def test_every_pair_of_version_1_permissions_keeps_the_canonical_order(self) -> None:
        # Codex GPT-6 Astra R3: a swap of two returned permissions must turn a test red.
        for first, second in itertools.combinations(PERMISSIONS_V1, 2):
            requested = (first, second)
            extra = {"network": ("api.example.org",)} if "network" in requested else {}
            creds = {"credentials": ("vault:test",)} if "credential" in requested else {}
            manifest = parse_manifest(v1(K1, permissions=requested, **extra, **creds))
            grant = Grant("demo.plugin", DIGEST, key_id(K1), (second, first))
            with self.subTest(pair=requested):
                self.assertEqual(effective_permissions(manifest, "signed", key_id(K1), grant), (requested, []))

    def test_the_key_order_was_turned_back_on_for_binds_a_manual_grant(self) -> None:
        grant = Grant("demo.plugin", DIGEST, None, ALL, True, order_after_revoked_key=key_id(K1))
        self.assertEqual(effective_permissions(self.m1, "unsigned", None, grant)[0], ALL)
        self.assertEqual(effective_permissions(self.m2, "unsigned", None, grant)[0], ())
        # An explicit developer key wins over the key named for order.
        both = Grant("demo.plugin", DIGEST, None, ALL, True, order_after_revoked_key=key_id(K1),
                     developer_key_id=key_id(K2))
        self.assertEqual(effective_permissions(self.m1, "unsigned", None, both)[0], ())
        self.assertEqual(effective_permissions(self.m2, "unsigned", None, both)[0], ALL)

    def test_a_signed_grant_naming_another_developer_key_grants_nothing(self) -> None:
        signer = key_id(K1)
        named_other = Grant("demo.plugin", DIGEST, signer, ALL, developer_key_id=key_id(K2))
        self.assertEqual(effective_permissions(self.m1, "signed", signer, named_other)[0], ())
        named_same = Grant("demo.plugin", DIGEST, signer, ALL, developer_key_id=signer)
        self.assertEqual(effective_permissions(self.m1, "signed", signer, named_same)[0], ALL)
        # A signed grant is bound through its signer; the key named for order does not unbind it.
        signed = Grant("demo.plugin", DIGEST, signer, ALL, order_after_revoked_key=key_id(K2))
        self.assertEqual(effective_permissions(self.m1, "signed", signer, signed)[0], ALL)

    def test_version_1_permissions_pass_when_requested_and_granted(self) -> None:
        # install.json cannot grant these yet (its reader takes the first seven); a Grant can.
        requested = ("read", "order", "surface", "command", "subscribe", "tool", "run")
        manifest = parse_manifest(v1(K1, permissions=requested))
        grant = Grant("demo.plugin", DIGEST, key_id(K1), ("read", "command", "run"))
        perms, dropped = effective_permissions(manifest, "signed", key_id(K1), grant)
        self.assertEqual(perms, ("read", "command", "run"))
        self.assertEqual(dropped, [("not_granted", "order"), ("not_granted", "surface"),
                                   ("not_granted", "subscribe"), ("not_granted", "tool")])

    def test_a_revoked_developer_key_still_turns_order_off_for_a_bound_grant(self) -> None:
        grant = Grant("demo.plugin", DIGEST, None, ALL, True, developer_key_id=key_id(K1))
        perms, dropped = effective_permissions(self.m1, "unsigned", None, grant, key_id(K1), 1)
        self.assertEqual(perms, ("read", "propose"))
        self.assertEqual(dropped, [("signer_key_revoked_order_off", key_id(K1))])

    def test_what_is_not_a_manifest_or_a_grant_is_refused_with_a_code(self) -> None:
        # DeepSeek V4.1 Flash, round 2, L3: an install.json entry as a dict, or raw manifest bytes.
        grant = Grant("demo.plugin", DIGEST, None, ALL, True, developer_key_id=key_id(K1))
        as_dict = {"plugin_id": "demo.plugin", "artifact_sha256": DIGEST, "signer_key_id": None, "permissions": list(ALL)}
        self.assertEqual(refusal(effective_permissions, self.m1, "unsigned", None, as_dict), "grant_invalid")
        self.assertEqual(refusal(effective_permissions, manifest_bytes(K1), "unsigned", None, grant), "manifest_invalid")
        self.assertEqual(refusal(effective_permissions, manifest_bytes(K1), "unsigned", None, None), "manifest_invalid")
        self.assertEqual(effective_permissions(self.m1, "unsigned", None, None)[1][0][0], "no_grant")

    def test_the_older_checks_still_come_first(self) -> None:
        grant = Grant("demo.plugin", DIGEST, None, ALL, True, developer_key_id=key_id(K2))
        self.assertEqual(effective_permissions(self.m1, "unsigned", None, None)[1][0][0], "no_grant")
        other_artifact = Grant("demo.plugin", "0" * 64, None, ALL, True, developer_key_id=key_id(K2))
        self.assertEqual(effective_permissions(self.m1, "unsigned", None, other_artifact)[1][0][0],
                         "grant_artifact_mismatch")
        self.assertEqual(effective_permissions(self.m1, "signed", key_id(K1), grant)[1][0][0],
                         "grant_signer_mismatch")


class LoaderBindingTests(unittest.TestCase):
    """Through today's loader. Unsigned manifests under an untrusted key need no signature check."""

    def load(self, raw: bytes, grant: Grant):
        loader = PluginLoader(TrustStore({}), {"demo.plugin": grant}, ReservedEffects(), CoreHealth(),
                              FakeGilProbe())
        return loader.load(raw, None, PLUGIN_SOURCE)

    def test_a_manual_grant_does_not_survive_a_change_of_the_declared_developer_key(self) -> None:
        grant = Grant("demo.plugin", DIGEST, None, ALL, True, developer_key_id=key_id(K1))
        self.assertEqual(self.load(manifest_bytes(K1), grant).permissions, ALL)
        swapped = self.load(manifest_bytes(K2), grant)
        self.assertEqual((swapped.trust, swapped.permissions), ("unsigned", ()))
        self.assertIn("grant_developer_key_mismatch", [f["code"] for f in swapped.findings])

    def test_an_unbound_manual_grant_loads_with_no_permission_and_says_why(self) -> None:
        loaded = self.load(manifest_bytes(K1), Grant("demo.plugin", DIGEST, None, ALL, True))
        self.assertEqual((loaded.trust, loaded.permissions), ("unsigned", ()))
        self.assertIn("grant_developer_key_missing", [f["code"] for f in loaded.findings])


@needs_crypto
class ManualGrantIdentityTests(unittest.TestCase):
    """R4 fix-6's revocation test, with the grant the install commands will write (bound to the key)."""

    def setUp(self) -> None:
        self.world = World(self, signed=False, unsigned_ack=True)
        self.kid = key_id(self.world.developer.public)

    def reload(self, raw: bytes, grant: Grant):
        trust = TrustStore({}, revocation_store=self.world.revocation_store)
        self.addCleanup(trust.close)
        loader = PluginLoader(trust, {"demo.plugin": grant}, self.world.reserved, CoreHealth(), FakeGilProbe())
        return loader.load(raw, "", PLUGIN_SOURCE)

    def test_rewrapping_the_same_artifact_does_not_undo_its_key_revocation(self) -> None:
        bound = Grant("demo.plugin", DIGEST, None, ALL, unsigned_order_acknowledged=True,
                      developer_key_id=self.kid)
        self.assertIn("order", self.reload(self.world.manifest, bound).permissions)
        revoked = self.world.call(self.world.host, "key.revoke", key_id=self.kid)
        self.assertTrue(revoked["ok"], revoked)
        self.assertNotIn("order", self.reload(self.world.manifest, bound).permissions)
        replacement = Key()
        changed_manifest = manifest_bytes(replacement.public)
        # Same plugin id, artifact bytes and grant; only the unsigned manifest's developer_key changed.
        self.assertEqual(self.reload(changed_manifest, bound).permissions, ())
        # The reviewer's own grant names no key: it is unbound, so it grants nothing anywhere.
        unbound = Grant("demo.plugin", DIGEST, None, ALL, unsigned_order_acknowledged=True)
        self.assertEqual(self.reload(self.world.manifest, unbound).permissions, ())
        self.assertEqual(self.reload(changed_manifest, unbound).permissions, ())
        # Order under the new key comes back only through a new grant the user makes for that key.
        regranted = Grant("demo.plugin", DIGEST, None, ALL, unsigned_order_acknowledged=True,
                          developer_key_id=key_id(replacement.public))
        self.assertEqual(self.reload(changed_manifest, regranted).permissions, ALL)


@unittest.skipUnless(__import__("os").name == "posix", "the install reader uses POSIX file checks")
class InstallReaderBindingTests(unittest.TestCase):
    """The integration read of #374 against master ff3f8071, F1: master's install reader refused a
    manual grant's developer_key_id, so the binding this draft requires could not be installed."""

    def _read(self, entry):
        import json
        import tempfile
        from pathlib import Path
        from recursion_core.daemon.plugins import read_install
        with tempfile.TemporaryDirectory(prefix="r4-6-reader-") as directory:
            path = Path(directory) / "install.json"
            path.write_text(json.dumps({"grants": [entry]}), encoding="utf-8")
            path.chmod(0o600)
            return read_install(directory)["grants"]

    def test_the_install_reader_keeps_a_manual_grant_bound_to_its_developer_key(self) -> None:
        from recursion_core.plugin_security.manifest import effective_permissions, parse_manifest
        from recursion_core.plugin_security.signing import key_id
        from recursion_core.plugin_security.tests.support import manifest_bytes
        public = "11" * 32
        manifest = parse_manifest(manifest_bytes(public, permissions=("read", "panel")))
        grant = self._read({"plugin_id": manifest.plugin_id, "artifact_sha256": manifest.artifact_sha256,
                            "signer_key_id": None, "permissions": ["read", "panel"],
                            "developer_key_id": key_id(public)})[manifest.plugin_id]
        self.assertEqual(grant.developer_key_id, key_id(public))
        self.assertEqual(effective_permissions(manifest, "unsigned", None, grant), (("read", "panel"), []))

    def test_the_install_reader_refuses_a_developer_key_that_is_not_text(self) -> None:
        from recursion_core.daemon.plugins import PluginRefused
        with self.assertRaises(PluginRefused) as caught:
            self._read({"plugin_id": "demo.plugin", "artifact_sha256": "0" * 64, "signer_key_id": None,
                        "permissions": ["read"], "developer_key_id": 5})
        self.assertEqual((caught.exception.code, caught.exception.detail),
                         ("plugin_install_config_invalid", "grant value"))


if __name__ == "__main__":
    unittest.main()
