"""O11 approval gate and connection breaker, driven only through the ``csl-core/v0`` bytes.

These tests talk to ``CoreService`` through request and response bytes, the way a later
Rust core would be driven, not through Python internals.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

from recursion_core.plugin_security import (
    PROTOCOL, ApprovalGate, Authority, ConnectionBreaker, CoreHealth, CoreService, Grant, PluginLoader, Refused,
    ReservedEffects, TrustStore, canonical, create_breaker_store, create_ledger, parse_manifest,
)
from recursion_core.plugin_security.protocol import CommitFence
from recursion_core.plugin_security.tests.support import (
    AGENT_TOKEN, OPERATOR_TOKEN, PLUGIN_SOURCE, PLUGIN_TOKEN, Clock, FakeGilProbe, World, authenticate, effect,
    needs_crypto, profile, scope_of, temp_dir,
)
from recursion_core.plugin_security.signing import key_id


def _code(response: dict) -> str | None:
    return None if response["ok"] else response["refusal"]["code"]


@needs_crypto
class GateFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.w = World(self)
        self.w.load()
        self.w.bind()
        self.env = profile(self.w.installer)

    def test_happy_path_permission_approval_profile(self) -> None:
        eff = effect()
        approval = self.w.approve(eff)
        out = self.w.authorize(eff, approval, self.env)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["result"]["charge"]["spent_minor"], 1_000)
        # The core drew exactly the canonical bytes that were approved.
        (_, digest, raw), = self.w.drawn
        self.assertEqual(raw, canonical(eff))
        self.assertEqual(out["result"]["digest"], digest)

    def test_every_effect_needs_an_approval(self) -> None:
        for eff in (effect(), effect(amount=0), effect(kind="note.post", amount=0)):
            with self.subTest(kind=eff["kind"], amount=eff["amount_minor"]):
                for approval in (None, "apr_" + "0" * 32, ""):
                    out = self.w.authorize(eff, approval, self.env)
                    self.assertIn(_code(out), ("approval_required",))

    def test_non_order_effect_needs_approval_but_no_profile(self) -> None:
        eff = effect(kind="note.post", amount=0)
        out = self.w.authorize(eff, self.w.approve(eff), None)
        self.assertTrue(out["ok"], out)
        self.assertIsNone(out["result"]["charge"])

    def test_plugin_and_agent_tokens_cannot_approve(self) -> None:
        eff = effect()
        req = self.w.call(self.w.plugin, "effect.request", effect=eff)["result"]
        for client in (self.w.plugin, self.w.host):
            for token in (PLUGIN_TOKEN, AGENT_TOKEN, None, "", OPERATOR_TOKEN + "x"):
                with self.subTest(token=token):
                    out = self.w.call(client, "approval.decide", approval_id=req["approval_id"],
                                      digest=req["digest"], credential=token, decision="approve")
                    self.assertEqual(_code(out), "approval_requires_operator")
        self.assertEqual(_code(self.w.authorize(eff, req["approval_id"], self.env)), "approval_not_granted")

    def test_approval_is_bound_to_the_drawn_digest(self) -> None:
        eff = effect()
        req = self.w.call(self.w.plugin, "effect.request", effect=eff)["result"]
        out = self.w.call(self.w.host, "approval.decide", approval_id=req["approval_id"],
                          digest="0" * 64, credential=OPERATOR_TOKEN, decision="approve")
        self.assertEqual(_code(out), "approval_digest_mismatch")
        approval = self.w.approve(eff)
        for changed in (effect(amount=1_001), effect(payload={"note": "other"}), effect(account="x")):
            with self.subTest(changed=changed):
                self.assertEqual(_code(self.w.authorize(changed, approval, self.env)), "approval_digest_mismatch")
        self.assertTrue(self.w.authorize(eff, approval, self.env)["ok"])

    def test_one_time_approval_replay_denied(self) -> None:
        eff = effect()
        approval = self.w.approve(eff)
        self.assertTrue(self.w.authorize(eff, approval, self.env)["ok"])
        self.assertEqual(_code(self.w.authorize(eff, approval, self.env)), "approval_replayed")

    def test_veto_expiry_and_double_decision(self) -> None:
        eff = effect()
        req = self.w.call(self.w.plugin, "effect.request", effect=eff)["result"]
        decide = lambda d: self.w.call(self.w.host, "approval.decide", approval_id=req["approval_id"],
                                       digest=req["digest"], credential=OPERATOR_TOKEN, decision=d)
        self.assertEqual(decide("veto")["result"]["state"], "vetoed")
        self.assertEqual(_code(decide("approve")), "approval_already_decided")
        self.assertEqual(_code(self.w.authorize(eff, req["approval_id"], self.env)), "approval_not_granted")
        # A veto is final for that digest (CSL-743), so the expiry arm uses a new intent.
        eff = effect(payload={"note": "second intent"})
        approval = self.w.approve(eff)
        self.w.clock.now += 301
        self.assertEqual(_code(self.w.authorize(eff, approval, self.env)), "approval_expired")

    def test_authority_refusal_after_approval(self) -> None:
        eff = effect(amount=6_000)  # over max_effect 5_000
        self.assertEqual(_code(self.w.authorize(eff, self.w.approve(eff), self.env)),
                         "budget_max_effect_exceeded")
        eff = effect()
        self.assertEqual(_code(self.w.authorize(eff, self.w.approve(eff), profile(self.w.developer))),
                         "profile_signature_invalid")
        self.assertEqual(_code(self.w.authorize(eff, self.w.approve(eff), None)), "profile_envelope")

    def test_split_orders_over_budget_refused_through_gate(self) -> None:
        results = []
        for i in range(12):
            eff = effect(amount=1_000, payload={"n": i})
            results.append(_code(self.w.authorize(eff, self.w.approve(eff), self.env)))
        self.assertEqual(results[:10], [None] * 10)
        self.assertEqual(results[10:], ["budget_total_exceeded"] * 2)


@needs_crypto
class RevocationTests(unittest.TestCase):
    """Round 1 finding 1: revoking a key turns off order permission for a running plugin (4.4)."""

    def setUp(self) -> None:
        self.w = World(self)
        self.w.load()
        self.w.bind()
        self.env = profile(self.w.installer)
        self.kid = key_id(self.w.developer.public)

    def test_revocation_turns_off_order_for_an_active_plugin(self) -> None:
        eff = effect()
        approval = self.w.approve(eff)  # approved before the revocation
        out = self.w.call(self.w.host, "key.revoke", key_id=self.kid)
        self.assertEqual(out["result"]["order_off"], ["demo.plugin"])
        self.assertEqual(_code(self.w.authorize(eff, approval, self.env)), "permission_required")
        self.assertEqual(_code(self.w.call(self.w.plugin, "effect.request", effect=effect(amount=5))),
                         "permission_required")
        perms = self.w.call(self.w.host, "plugin.permissions", plugin_id="demo.plugin")["result"]
        self.assertEqual(perms["permissions"], ["read", "propose"])  # other grants stay (O12 open)
        self.assertIn("signer_key_revoked_order_off",
                      [f["code"] for f in self.w.loader._active["demo.plugin"].findings])
        # Idempotent, and a key with no active plugin turns nothing off.
        self.assertEqual(self.w.call(self.w.host, "key.revoke", key_id=self.kid)["result"]["order_off"], [])
        self.assertEqual(_code(self.w.call(self.w.host, "key.revoke", key_id="nope")), "invalid_key_id")

    def test_revoked_key_loads_unsigned_afterwards(self) -> None:
        self.w.call(self.w.host, "key.revoke", key_id=self.kid)
        trust, kid, reason = self.w.loader._trust.classify(parse_manifest(self.w.manifest),
                                                           self.w.developer.sign(self.w.manifest))
        self.assertEqual((trust, kid, reason), ("unsigned", None, "signer_key_revoked"))

    def test_revocation_survives_a_restart(self) -> None:
        """Round 2 finding 1: a restart rebuilds the trust store from installer config,
        which never saw the revocation; the durable store keeps the key revoked."""
        self.assertTrue(self.w.call(self.w.host, "key.revoke", key_id=self.kid)["ok"])
        # A daemon restart: the same installer config and the same revocation store.
        trust = TrustStore(self.w.trusted, revocation_store=self.w.revocation_store)
        self.addCleanup(trust.close)
        self.assertIn(self.kid, trust.revoked)
        loader = PluginLoader(trust, {"demo.plugin": self.w.grant}, self.w.reserved, CoreHealth(),
                              FakeGilProbe())
        loaded = loader.load(self.w.manifest, self.w.developer.sign(self.w.manifest), PLUGIN_SOURCE)
        self.assertEqual(loaded.trust, "unsigned")
        self.assertNotIn("order", loader.permissions("demo.plugin"))
        codes = [f["code"] for f in loaded.findings]
        self.assertIn("signer_key_revoked", codes)
        self.assertIn("grant_signer_mismatch", codes)  # the user's step is a fresh grant

    def _restart_load(self, trusted: dict, grant: Grant):
        # A daemon restart: installer config and the same revocation store.
        trust = TrustStore(trusted, revocation_store=self.w.revocation_store)
        self.addCleanup(trust.close)
        loader = PluginLoader(trust, {"demo.plugin": grant}, self.w.reserved, CoreHealth(), FakeGilProbe())
        loaded = loader.load(self.w.manifest, self.w.developer.sign(self.w.manifest), PLUGIN_SOURCE)
        return loader, loaded

    def test_manual_grant_does_not_get_order_back_after_a_restart(self) -> None:
        """Round 3 finding 1: a manual grant (no signer, unsigned order acknowledged) stays
        without order after a restart, with the key on the trusted list or off it."""
        self.assertTrue(self.w.call(self.w.host, "key.revoke", key_id=self.kid)["ok"])
        manual = Grant("demo.plugin", self.w.grant.artifact_sha256, None, ("read", "propose", "order"), True)
        for name, trusted in (("on_trusted_list", self.w.trusted), ("off_trusted_list", {})):
            with self.subTest(name):
                loader, loaded = self._restart_load(trusted, manual)
                self.assertEqual(loaded.trust, "unsigned")
                self.assertEqual(loader.permissions("demo.plugin"), ("read", "propose"))
                codes = [f["code"] for f in loaded.findings]
                self.assertIn("signer_key_revoked", codes)
                self.assertIn("signer_key_revoked_order_off", codes)

    def test_order_comes_back_only_through_a_grant_naming_the_revoked_key(self) -> None:
        self.assertTrue(self.w.call(self.w.host, "key.revoke", key_id=self.kid)["ok"])
        digest = self.w.grant.artifact_sha256
        other = "ed25519:" + "cd" * 16
        wrong = Grant("demo.plugin", digest, None, ("read", "propose", "order"), True, other)
        self.assertNotIn("order", self._restart_load({}, wrong)[0].permissions("demo.plugin"))
        # The user's step: a fresh manual grant behind the warning that names the revoked key.
        again = Grant("demo.plugin", digest, None, ("read", "propose", "order"), True, self.kid)
        loader, loaded = self._restart_load(self.w.trusted, again)
        self.assertEqual(loader.permissions("demo.plugin"), ("read", "propose", "order"))
        # Naming the key does not stand in for the unsigned-order acknowledgement.
        no_ack = Grant("demo.plugin", digest, None, ("read", "propose", "order"), False, self.kid)
        self.assertNotIn("order", self._restart_load({}, no_ack)[0].permissions("demo.plugin"))

    def test_a_failed_revocation_write_is_revocation_not_durable(self) -> None:
        self.w.loader._trust._db.execute("PRAGMA query_only=ON")
        out = self.w.call(self.w.host, "key.revoke", key_id=self.kid)
        self.assertEqual(out["refusal"]["code"], "revocation_not_durable")
        self.assertNotIn("order", self.w.loader.permissions("demo.plugin"))

    def test_a_second_revocation_does_not_reuse_the_order_grant(self) -> None:
        first = self.w.call(self.w.host, "key.revoke", key_id=self.kid)
        self.assertEqual(first["result"]["revocation_generation"], 1)
        digest = self.w.grant.artifact_sha256
        named = Grant("demo.plugin", digest, None, ("read", "propose", "order"), True, self.kid)
        self.assertEqual(self._restart_load(self.w.trusted, named)[0].permissions("demo.plugin"),
                         ("read", "propose", "order"))
        second = self.w.call(self.w.host, "key.revoke", key_id=self.kid)
        self.assertEqual(second["result"]["revocation_generation"], 2)
        self.assertEqual(self._restart_load(self.w.trusted, named)[0].permissions("demo.plugin"),
                         ("read", "propose"))
        stale = Grant("demo.plugin", digest, None, ("read", "propose", "order"), True, self.kid,
                      order_after_revocation_generation=1)
        self.assertNotIn("order", self._restart_load(self.w.trusted, stale)[0].permissions("demo.plugin"))
        fresh = Grant("demo.plugin", digest, None, ("read", "propose", "order"), True, self.kid,
                      order_after_revocation_generation=2)
        self.assertEqual(self._restart_load(self.w.trusted, fresh)[0].permissions("demo.plugin"),
                         ("read", "propose", "order"))

    def test_revocation_without_a_store_is_refused_but_order_is_off(self) -> None:
        trust = TrustStore(self.w.trusted)  # no durable store configured
        loader = PluginLoader(trust, {"demo.plugin": self.w.grant}, self.w.reserved, CoreHealth(),
                              FakeGilProbe())
        loader.load(self.w.manifest, self.w.developer.sign(self.w.manifest), PLUGIN_SOURCE)
        with self.assertRaises(Refused) as ctx:
            loader.revoke_key(self.kid)
        self.assertEqual(ctx.exception.code, "revocation_not_durable")
        self.assertNotIn("order", loader.permissions("demo.plugin"))
        self.assertIn(self.kid, trust.revoked)


@needs_crypto
class AuthorityFieldTests(unittest.TestCase):
    """Round 1 finding 5: the K3 field set is signed, digested and matched exactly."""

    def setUp(self) -> None:
        self.w = World(self)
        self.w.load()
        self.w.bind()

    def _authorize(self, eff, env) -> dict:
        return self.w.authorize(eff, self.w.approve(eff), env)

    def test_paper_profile_cannot_charge_a_live_effect(self) -> None:
        env = profile(self.w.installer)
        self.assertEqual(_code(self._authorize(effect(scope=scope_of(environment="live")), env)),
                         "profile_scope_mismatch")
        for field in ("contract_family", "adapter"):
            with self.subTest(field=field):
                eff = effect(scope=scope_of(**{field: "other"}))
                self.assertEqual(_code(self._authorize(eff, env)), "profile_scope_mismatch")
        self.assertTrue(self._authorize(effect(), env)["ok"])

    def test_environment_is_in_the_approval_digest(self) -> None:
        paper, live = effect(), effect(scope=scope_of(environment="live"))
        approval = self.w.approve(paper)
        self.assertEqual(_code(self.w.authorize(live, approval, profile(self.w.installer))),
                         "approval_digest_mismatch")

    def test_action_and_model_must_match_the_profile(self) -> None:
        env = profile(self.w.installer, actions=["order.cancel"])
        self.assertEqual(_code(self._authorize(effect(), env)), "profile_action_not_permitted")
        self.w.bind("run-2", "conn-a", model_version="fake-model-2")
        env = profile(self.w.installer, profile_id="prof-2")
        self.assertEqual(_code(self._authorize(effect(run_id="run-2"), env)), "profile_model_mismatch")
        old_scope = effect(scope={"account": "paper-acct", "venue": "paper"})  # pre-K3 effect shape
        self.assertEqual(_code(self.w.call(self.w.plugin, "effect.request", effect=old_scope)), "effect_scope")

    def test_a_bindable_model_name_can_be_named_in_a_profile(self) -> None:
        # Bot review 4511542761 at 7e5c3502: run.bind accepted "vendor/model" and "@", but profile validation
        # refused both, so no profile could authorise an order from such a run.
        from recursion_core.plugin_security import authority, breaker
        self.assertEqual(authority._PROVIDER_VALUE.pattern, breaker._ID.pattern)
        self.w.bind("run-3", "conn-a", provider="acme", model_version="acme/model-v1@2026-11")
        env = profile(self.w.installer, profile_id="prof-3", provider="acme", model_version="acme/model-v1@2026-11")
        self.assertTrue(self._authorize(effect(run_id="run-3"), env)["ok"])
        env = profile(self.w.installer, profile_id="prof-4", model_version="bad model")
        self.assertEqual(_code(self._authorize(effect(), env)), "profile_model")
        # A model the store records outside the id grammar: bound, named and authorised like the one above.
        self.w.bind("run-5", "conn-a", provider="acme", model_version="opus[1m]")
        env = profile(self.w.installer, profile_id="prof-5", provider="acme", model_version="opus[1m]")
        self.assertTrue(self._authorize(effect(run_id="run-5"), env)["ok"])
        # Naming it does not loosen the match: a run bound to another spelling is still refused.
        self.w.bind("run-6", "conn-a", provider="acme", model_version="opus")
        env = profile(self.w.installer, profile_id="prof-6", provider="acme", model_version="opus[1m]")
        self.assertEqual(_code(self._authorize(effect(run_id="run-6"), env)), "profile_model_mismatch")
        # The provider keeps the id grammar, in a profile as in the store.
        env = profile(self.w.installer, profile_id="prof-7", provider="acme[1]")
        self.assertEqual(_code(self._authorize(effect(), env)), "profile_model")

@needs_crypto
class PermissionAndBreakerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.w = World(self, grant_permissions=("read", "propose"))
        self.w.load()
        self.w.bind()

    def test_plugin_without_order_permission_cannot_reach_it(self) -> None:
        out = self.w.call(self.w.plugin, "effect.request", effect=effect())
        self.assertEqual(_code(out), "permission_required")
        self.assertEqual(self.w.drawn, [])  # nothing drawn for a refused effect
        self.assertEqual(_code(self.w.authorize(effect(), "apr_x", None)), "permission_required")

    def test_caller_must_be_the_effect_plugin_and_host_ops_are_host_only(self) -> None:
        other = self.w.core.client_for("other.plugin")
        self.assertEqual(_code(self.w.call(other, "effect.request", effect=effect(kind="note.post"))),
                         "caller_not_effect_plugin")
        for op, args in (("run.bind", dict(run_id="r", connection="c", plugin_id="demo.plugin",
                                           provider="p", model_version="m")),
                         ("run.fault", dict(run_id="run-1")),
                         ("breaker.report", dict(connection="conn-a", event_type="plan_limit")),
                         ("key.revoke", dict(key_id=key_id(self.w.developer.public))),
                         ("breaker.probe_begin", dict(connection="conn-a")),
                         ("breaker.probe_reset", dict(connection="conn-a", lease="x", probe_ok=True))):
            with self.subTest(op=op):
                self.assertEqual(_code(self.w.call(self.w.plugin, op, **args)), "host_only")

    def test_unmapped_effect_kind_refused(self) -> None:
        out = self.w.call(self.w.plugin, "effect.request", effect=effect(kind="shell.exec"))
        self.assertEqual(_code(out), "effect_kind_unmapped")

    def test_faulted_run_and_tripped_connection_refused(self) -> None:
        eff = effect(kind="note.post", amount=0)
        approval = self.w.approve(eff)
        self.assertTrue(self.w.call(self.w.host, "breaker.report", connection="conn-a",
                                    event_type="plan_limit")["result"]["tripped"])
        self.assertEqual(_code(self.w.authorize(eff, approval, None)), "connection_tripped")
        self.assertEqual(_code(self.w.call(self.w.plugin, "effect.request", effect=eff)), "connection_tripped")
        self.assertFalse(self.w.breaker.reset_after_probe("conn-a", None, True))  # no lease, no reset
        self.assertTrue(self.w.breaker.reset_after_probe("conn-a", self.w.breaker.begin_probe("conn-a"), True))
        self.assertTrue(self.w.authorize(eff, approval, None)["ok"])
        eff2 = effect(kind="note.post", amount=0, payload={"n": 2})
        approval2 = self.w.approve(eff2)
        self.w.call(self.w.host, "run.fault", run_id="run-1")
        self.assertEqual(_code(self.w.authorize(eff2, approval2, None)), "run_faulted")

    def test_effect_cannot_cite_a_run_its_plugin_does_not_own(self) -> None:
        # Round 1 finding 2: a plugin whose own run faulted, or whose connection tripped,
        # names another plugin's (or the host's) healthy run. Refused, not borrowed.
        self.w.bind("run-other", "conn-b", plugin_id="other.plugin")
        self.w.bind("run-host", "conn-b", plugin_id="host")
        self.w.call(self.w.host, "run.fault", run_id="run-1")
        self.w.call(self.w.host, "breaker.report", connection="conn-a", event_type="plan_limit")
        eff = effect(kind="note.post", amount=0)
        self.assertEqual(_code(self.w.call(self.w.plugin, "effect.request", effect=eff)), "run_faulted")
        for run_id in ("run-other", "run-host"):
            with self.subTest(run_id=run_id):
                borrowed = effect(kind="note.post", amount=0, run_id=run_id, connection="conn-b")
                self.assertEqual(_code(self.w.call(self.w.plugin, "effect.request", effect=borrowed)),
                                 "run_not_owned")
                self.assertEqual(_code(self.w.authorize(borrowed, "apr_x", None)), "run_not_owned")

    def test_run_binding_cannot_change_owner_or_model(self) -> None:
        for change in (dict(plugin_id="other.plugin"), dict(provider="other"), dict(model_version="m2")):
            with self.subTest(change=change):
                args = dict(run_id="run-1", connection="conn-a", plugin_id="demo.plugin",
                            provider="fake-provider", model_version="fake-model-1")
                args.update(change)
                self.assertEqual(_code(self.w.call(self.w.host, "run.bind", **args)), "run_binding_mismatch")

    def test_breaker_keys_on_connection_and_only_typed_limits_trip(self) -> None:
        self.w.bind("run-2", "conn-b")
        for event in ("core_timeout", "core_deadline", "caller_cancelled"):
            out = self.w.call(self.w.host, "breaker.report", connection="conn-a", event_type=event)
            self.assertFalse(out["result"]["tripped"])
        self.assertEqual(_code(self.w.call(self.w.host, "breaker.report", connection="conn-a",
                                           event_type="quota sounds bad")), "untyped_breaker_event")
        self.w.call(self.w.host, "breaker.report", connection="conn-a", event_type="rate_limit")
        ok = self.w.call(self.w.plugin, "effect.request", effect=effect(kind="note.post", run_id="run-2",
                                                                      connection="conn-b"))
        self.assertTrue(ok["ok"])
        self.assertFalse(self.w.breaker.reset_after_probe("conn-a", self.w.breaker.begin_probe("conn-a"), False))
        self.assertTrue(self.w.breaker.tripped("conn-a"))
        self.assertEqual(_code(self.w.call(self.w.plugin, "effect.request", effect=effect(
            kind="note.post", run_id="run-2", connection="conn-a"))), "run_connection_mismatch")
        self.assertEqual(_code(self.w.call(self.w.plugin, "effect.request", effect=effect(
            kind="note.post", run_id="run-9"))), "run_unknown")

    def test_poisoned_core_refuses_effects(self) -> None:
        eff = effect(kind="note.post", amount=0)
        approval = self.w.approve(eff)
        self.w.health.poison("test")
        self.assertEqual(_code(self.w.authorize(eff, approval, None)), "core_poisoned_restart_required")


@needs_crypto
class ProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.w = World(self)

    def test_malformed_requests(self) -> None:
        cases = {
            b"not json": "noncanonical_value",
            b'{"args":{},"op":"run.bind","protocol":"csl-core/v9"}': "protocol_request_invalid",
            b'{"args":{},"op":"order.place","protocol":"csl-core/v0"}': "protocol_unknown_op",
            b'{"args":{"x":1},"op":"run.fault","protocol":"csl-core/v0"}': "protocol_args_invalid",
            b'{"op":"run.fault", "args":{"run_id":"r"},"protocol":"csl-core/v0"}': "noncanonical_value",
        }
        for raw, code in cases.items():
            with self.subTest(raw=raw):
                out = json.loads(self.w.host(raw))
                self.assertEqual(out, {"ok": False, "protocol": "csl-core/v0", "refusal": {"code": code}})

    def test_wrong_argument_types_get_typed_refusals(self) -> None:
        cases = [
            ("approval.decide", dict(approval_id=[1], digest="d", credential="c", decision="approve"),
             "approval_unknown"),
            ("approval.decide", dict(approval_id="a", digest={"x": 1}, credential="c", decision="approve"),
             "approval_unknown"),
            ("run.bind", dict(run_id=7, connection="c", plugin_id="p", provider="p", model_version="m"),
             "invalid_run_id"),
            ("breaker.report", dict(connection=["c"], event_type="plan_limit"), "invalid_connection"),
            ("effect.request", dict(effect=[1]), "effect_fields"),
            ("plugin.permissions", dict(plugin_id={"a": 1}), "core_internal_error"),
        ]
        for op, args, code in cases:
            with self.subTest(op=op, args=args):
                out = self.w.call(self.w.host, op, **args)
                self.assertEqual(out["refusal"]["code"], code)

    def test_responses_are_canonical_and_ops_match_the_published_list(self) -> None:
        raw = self.w.host(canonical({"protocol": "csl-core/v0", "op": "plugin.permissions",
                                     "args": {"plugin_id": "demo.plugin"}}))
        self.assertEqual(raw, canonical(json.loads(raw)))
        published = json.loads(Path(__file__).resolve().parents[1].joinpath("protocol.json").read_text())
        self.assertEqual(published, PROTOCOL)
        for op in PROTOCOL["ops"]:
            self.assertTrue(hasattr(CoreService, "_op_" + op.replace(".", "_")), op)


@needs_crypto
class HaltToctouTests(unittest.TestCase):
    """An order effect in flight must not authorize or charge after a halt, revoke, fault, poison or veto."""

    def test_halt_during_authorize_refuses_and_writes_no_charge(self) -> None:
        cases = (
            ("key_revoke", "permission_required"),
            ("breaker_trip", "connection_tripped"),
            ("run_fault", "run_faulted"),
            ("sibling_veto", "approval_vetoed"),
            ("core_poison", "core_poisoned_restart_required"),
        )
        for mode, expected_code in cases:
            with self.subTest(mode=mode):
                w = World(self)
                w.load()
                w.bind()
                env = profile(w.installer)
                eff = effect(amount=1_000, payload={"mode": mode})
                approval_1 = w.approve(eff)
                sibling = w.call(w.plugin, "effect.request", effect=eff)["result"]

                entered = threading.Event()
                release = threading.Event()
                real_verify = w.authority._verify

                def paused_verify(sig, raw, _e=entered, _r=release, _rv=real_verify):
                    _e.set()
                    assert _r.wait(5.0)
                    return _rv(sig, raw)

                w.authority._verify = paused_verify
                holder: dict[str, dict] = {}
                worker = threading.Thread(
                    target=lambda: holder.__setitem__("out", w.authorize(eff, approval_1, env))
                )
                worker.start()
                try:
                    self.assertTrue(entered.wait(5.0))

                    if mode == "key_revoke":
                        self.assertTrue(w.call(w.host, "key.revoke", key_id=key_id(w.developer.public))["ok"])
                    elif mode == "breaker_trip":
                        self.assertTrue(
                            w.call(w.host, "breaker.report", connection="conn-a", event_type="plan_limit")["ok"]
                        )
                    elif mode == "run_fault":
                        self.assertTrue(w.call(w.host, "run.fault", run_id="run-1")["ok"])
                    elif mode == "sibling_veto":
                        self.assertTrue(
                            w.call(
                                w.host,
                                "approval.decide",
                                approval_id=sibling["approval_id"],
                                digest=sibling["digest"],
                                credential=OPERATOR_TOKEN,
                                decision="veto",
                            )["ok"]
                        )
                    else:
                        w.health.poison("gil_enabled")

                finally:
                    release.set()
                    worker.join(10.0)
                self.assertFalse(worker.is_alive(), "authorizer did not finish")
                self.assertEqual(_code(holder["out"]), expected_code)
                db = sqlite3.connect(w.ledger)
                try:
                    spent = db.execute("SELECT COUNT(*) FROM charges").fetchone()[0]
                finally:
                    db.close()
                self.assertEqual(spent, 0)


def _charges(w: World) -> int:
    db = sqlite3.connect(w.ledger)
    try:
        return db.execute("SELECT COUNT(*) FROM charges").fetchone()[0]
    finally:
        db.close()


def _halt(w: World, mode: str, sibling: dict) -> dict:
    """One acknowledged halt through the host's client, as the R4-1 modes name them."""
    if mode in ("key_revoke", "key_revoke_not_durable"):
        return w.call(w.host, "key.revoke", key_id=key_id(w.developer.public))
    if mode == "breaker_trip":
        return w.call(w.host, "breaker.report", connection="conn-a", event_type="plan_limit")
    if mode == "run_fault":
        return w.call(w.host, "run.fault", run_id="run-1")
    if mode == "sibling_veto":
        return w.call(w.host, "approval.decide", approval_id=sibling["approval_id"], digest=sibling["digest"],
                      credential=OPERATOR_TOKEN, decision="veto")
    raise AssertionError(mode)


class _CountingLock:
    """A real lock that counts attempts to take it, so a test can wait for the Nth (a drain's)."""

    def __init__(self, real) -> None:
        self._real = real
        self._mutex = threading.Lock()
        self.attempts = 0
        self._events: dict[int, threading.Event] = {}

    def attempt(self, n: int) -> threading.Event:
        """An event set once the Nth attempt to take the lock has been made."""
        with self._mutex:
            event = self._events.setdefault(n, threading.Event())
            if self.attempts >= n:
                event.set()
            return event

    def acquire(self, *args, **kwargs) -> bool:
        with self._mutex:
            self.attempts += 1
            for n, event in self._events.items():
                if self.attempts >= n:
                    event.set()
        return self._real.acquire(*args, **kwargs)

    def release(self) -> None:
        self._real.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


class _NotifyingLock:
    """A real lock that sets an event when a thread other than the creator's tries to take it."""

    def __init__(self, real) -> None:
        self._real = real
        self._owner = threading.get_ident()
        self.attempted = threading.Event()

    def acquire(self, *args, **kwargs) -> bool:
        if threading.get_ident() != self._owner:
            self.attempted.set()
        return self._real.acquire(*args, **kwargs)

    def release(self) -> None:
        self._real.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


@needs_crypto
class CommitFenceTests(unittest.TestCase):
    """R4-1 under D3, beyond Astra's five modes: the effect that is not charged, the halt's answer
    waiting for a commit already under way, the lock order, and the injected fence."""

    def test_halt_after_take_refuses_an_effect_that_is_not_charged(self) -> None:
        # The answer is the commit for an effect with no authority profile: a halt acknowledged
        # between take and that answer refuses it, and the approval stays used.
        cases = (
            ("breaker_trip", "connection_tripped"),
            ("run_fault", "run_faulted"),
            ("sibling_veto", "approval_vetoed"),
            ("core_poison", "core_poisoned_restart_required"),
        )
        for mode, expected_code in cases:
            with self.subTest(mode=mode):
                w = World(self)
                w.load()
                w.bind()
                eff = effect(kind="note.post", amount=0, payload={"mode": mode})
                approval = w.approve(eff)
                sibling = w.call(w.plugin, "effect.request", effect=eff)["result"]
                entered, release = threading.Event(), threading.Event()
                real_take = w.gate.take

                def paused_take(approval_id, value, _e=entered, _r=release, _rt=real_take):
                    digest = _rt(approval_id, value)
                    _e.set()
                    if not _r.wait(10.0):
                        raise AssertionError("never released")
                    return digest

                w.gate.take = paused_take
                holder: dict[str, dict] = {}
                worker = threading.Thread(target=lambda: holder.__setitem__("out", w.authorize(eff, approval, None)))
                worker.start()
                try:
                    self.assertTrue(entered.wait(10.0), "authorizer never took the approval")
                    if mode == "core_poison":
                        w.health.poison("test")
                    else:
                        self.assertTrue(_halt(w, mode, sibling)["ok"])
                finally:
                    release.set()
                    worker.join(10.0)
                self.assertFalse(worker.is_alive(), "authorizer did not finish")
                self.assertEqual(_code(holder["out"]), expected_code)
                with self.assertRaises(Refused) as used:
                    real_take(approval, eff)
                self.assertEqual(used.exception.code, "approval_replayed")

    def test_halt_answers_only_after_a_commit_already_under_way(self) -> None:
        # A commit section that read the state before the halt holds the fence: the halt changes its
        # state, then waits, and answers only once that charge is committed.
        cases = (
            ("key_revoke", True),
            ("key_revoke_not_durable", False),  # the order is off, the write failed: refused, still drained
            ("breaker_trip", True),
            ("run_fault", True),
            ("sibling_veto", True),
        )
        for mode, halt_ok in cases:
            with self.subTest(mode=mode):
                w = World(self)
                counting = _CountingLock(w.core.commit_fence._lock)  # watch the core's own fence
                w.core.commit_fence._lock = counting
                w.load()
                w.bind()
                env = profile(w.installer)
                eff = effect(amount=1_000, payload={"mode": mode})
                approval = w.approve(eff)
                sibling = w.call(w.plugin, "effect.request", effect=eff)["result"]
                if mode == "key_revoke_not_durable":
                    w.loader._trust._db.execute("PRAGMA query_only=ON")
                entered, release = threading.Event(), threading.Event()
                real_is_vetoed = w.gate.is_vetoed

                def paused_is_vetoed(digest, _e=entered, _r=release, _rv=real_is_vetoed):
                    answer = _rv(digest)  # the commit section's last read, before the halt
                    _e.set()
                    if not _r.wait(10.0):
                        raise AssertionError("never released")
                    return answer

                w.gate.is_vetoed = paused_is_vetoed
                out: dict[str, object] = {}
                worker = threading.Thread(target=lambda: out.__setitem__("authorize", w.authorize(eff, approval, env)))
                answered = threading.Event()

                def halt() -> None:
                    out["halt"] = _halt(w, mode, sibling)
                    out["charges_at_answer"] = _charges(w)
                    answered.set()

                halter = threading.Thread(target=halt)
                worker.start()
                try:
                    self.assertTrue(entered.wait(10.0), "authorizer never reached its commit section")
                    drain_asked = counting.attempt(counting.attempts + 1)  # the next acquisition is the drain's
                    halter.start()
                    self.assertTrue(drain_asked.wait(10.0), "the halt never drained the fence")
                    self.assertFalse(answered.wait(0.3), "the halt answered while a commit held the fence")
                finally:
                    release.set()
                    worker.join(10.0)
                    if halter.ident is not None:
                        halter.join(10.0)
                self.assertFalse(worker.is_alive() or halter.is_alive(), "a thread did not finish")
                self.assertTrue(out["authorize"]["ok"], out["authorize"])
                self.assertEqual(out["authorize"]["result"]["charge"]["spent_minor"], 1_000)
                self.assertEqual(out["halt"]["ok"], halt_ok, out["halt"])
                if not halt_ok:
                    self.assertEqual(_code(out["halt"]), "revocation_not_durable")
                self.assertEqual(out["charges_at_answer"], 1)

    def test_key_revoke_holds_no_fence_while_it_waits_for_the_loader(self) -> None:
        # The loader's lock is held when the core is poisoned (CoreHealth.poison runs inside load).
        # A revocation waiting for that lock must not hold the fence, or a drain made under the
        # loader's lock would wait for it for ever.
        w = World(self)
        w.load()
        w.bind()
        real = w.loader._lock
        notifying = _NotifyingLock(real)
        w.loader._lock = notifying
        revoked: dict[str, dict] = {}
        revoker = threading.Thread(
            target=lambda: revoked.__setitem__("out", w.call(w.host, "key.revoke", key_id=key_id(w.developer.public))))
        drainer = threading.Thread(target=w.core.commit_fence.drain)
        real.acquire()
        try:
            revoker.start()
            self.assertTrue(notifying.attempted.wait(10.0), "the revocation never asked for the loader's lock")
            drainer.start()
            drainer.join(10.0)
            self.assertFalse(drainer.is_alive(), "a drain under the loader's lock waited for the revocation")
        finally:
            real.release()
            revoker.join(10.0)
            if drainer.ident is not None:
                drainer.join(10.0)
        self.assertFalse(revoker.is_alive(), "the revocation did not finish")
        self.assertTrue(revoked["out"]["ok"], revoked["out"])

    def test_a_halt_does_not_wait_while_a_charge_waits_for_the_ledger(self) -> None:
        # The commit section is entered only once the ledger's write transaction has begun, so a charge
        # waiting for the ledger's write lock (another connection holds it) holds no fence.
        w = World(self)
        w.load()
        w.bind()
        env = profile(w.installer)
        eff = effect(amount=1_000)
        approval = w.approve(eff)
        beginning = threading.Event()
        real_db = w.authority._db

        class _Db:
            def execute(self, sql, params=()):
                if sql == "BEGIN IMMEDIATE":
                    beginning.set()
                return real_db.execute(sql, params)

            def close(self) -> None:
                real_db.close()

        w.authority._db = _Db()
        holder = sqlite3.connect(w.ledger, isolation_level=None, timeout=30)
        holder.execute("BEGIN IMMEDIATE")
        out: dict[str, dict] = {}
        worker = threading.Thread(target=lambda: out.__setitem__("r", w.authorize(eff, approval, env)))
        drainer = threading.Thread(target=w.core.commit_fence.drain)
        try:
            worker.start()
            self.assertTrue(beginning.wait(10.0), "the charge never began its ledger transaction")
            drainer.start()
            drainer.join(10.0)
            self.assertFalse(drainer.is_alive(), "a drain waited while the charge waited for the ledger")
        finally:
            holder.execute("ROLLBACK")
            holder.close()
            worker.join(40.0)
            if drainer.ident is not None:
                drainer.join(10.0)
        self.assertFalse(worker.is_alive(), "the charge did not finish")
        self.assertTrue(out["r"]["ok"], out["r"])
        self.assertEqual(_charges(w), 1)

    def test_an_injected_fence_is_the_one_every_commit_passes(self) -> None:
        w = World(self)
        shared = CommitFence()
        core = CoreService(loader=w.loader, gate=w.gate, breaker=w.breaker, health=w.health,
                           authority=w.authority, effect_permissions={"order": "order", "note": "propose"},
                           authority_required=frozenset({"order"}), commit_fence=shared)
        self.assertIs(core.commit_fence, shared)
        w.load()
        w.bind()
        plugin = core.client_for("demo.plugin")
        eff = effect(kind="note.post", amount=0)
        approval = w.approve(eff)
        done = threading.Event()
        out: dict[str, dict] = {}

        def authorize() -> None:
            out["r"] = w.call(plugin, "effect.authorize", effect=eff, approval_id=approval, authority=None)
            done.set()

        worker = threading.Thread(target=authorize)
        with shared:
            worker.start()
            self.assertFalse(done.wait(0.3), "authorized while the shared fence was held")
        worker.join(10.0)
        self.assertFalse(worker.is_alive())
        self.assertTrue(out["r"]["ok"], out["r"])

    # -- a trip that a reset clears while an authorization is in flight (review 1 of #378, R1) ----------

    def test_a_trip_acknowledged_then_reset_still_refuses_an_authorization_in_flight(self) -> None:
        for where in ("order_in_verification", "uncharged_after_take", "order_waiting_for_the_ledger"):
            with self.subTest(where=where):
                w = World(self)
                w.load()
                w.bind()
                order = where != "uncharged_after_take"
                eff = effect(payload={"where": where}) if order else effect(kind="note.post", amount=0,
                                                                            payload={"where": where})
                env = profile(w.installer) if order else None
                approval = w.approve(eff)
                paused, release = threading.Event(), threading.Event()
                holder = None
                if where == "order_in_verification":
                    real_verify = w.authority._verify

                    def paused_verify(sig, raw, _p=paused, _r=release, _rv=real_verify):
                        _p.set()
                        if not _r.wait(10.0):
                            raise AssertionError("never released")
                        return _rv(sig, raw)

                    w.authority._verify = paused_verify
                elif where == "uncharged_after_take":
                    real_take = w.gate.take

                    def paused_take(approval_id, value, _p=paused, _r=release, _rt=real_take):
                        digest = _rt(approval_id, value)
                        _p.set()
                        if not _r.wait(10.0):
                            raise AssertionError("never released")
                        return digest

                    w.gate.take = paused_take
                else:
                    _signal_begin(w, paused)
                    holder = sqlite3.connect(w.ledger, isolation_level=None, timeout=30)
                    holder.execute("BEGIN IMMEDIATE")  # another connection holds the ledger's write lock
                out: dict[str, dict] = {}
                worker = threading.Thread(target=lambda: out.__setitem__("r", w.authorize(eff, approval, env)))
                worker.start()
                try:
                    self.assertTrue(paused.wait(10.0), "the authorization never paused")
                    _trip_and_reset(self, w)
                finally:
                    release.set()
                    if holder is not None:
                        holder.execute("ROLLBACK")
                        holder.close()
                    worker.join(40.0)
                self.assertFalse(worker.is_alive(), "the authorization did not finish")
                self.assertEqual(_code(out["r"]), "connection_tripped")
                self.assertEqual(_charges(w), 0)

    def test_a_reset_lets_a_fresh_authorization_through(self) -> None:
        w = World(self)
        w.load()
        w.bind()
        early = effect(payload={"n": "approved before the trip"})
        early_approval = w.approve(early)
        _trip_and_reset(self, w)
        late = effect(payload={"n": "approved after the reset"})
        for eff, approval in ((early, early_approval), (late, w.approve(late))):
            with self.subTest(payload=eff["payload"]["n"]):
                self.assertTrue(w.authorize(eff, approval, profile(w.installer))["ok"])
        self.assertEqual(_charges(w), 2)

    def test_another_connections_trip_does_not_refuse_this_authorization(self) -> None:
        w = World(self)
        w.load()
        w.bind()
        eff = effect()
        approval = w.approve(eff)
        paused, release = threading.Event(), threading.Event()
        real_verify = w.authority._verify

        def paused_verify(sig, raw):
            paused.set()
            if not release.wait(10.0):
                raise AssertionError("never released")
            return real_verify(sig, raw)

        w.authority._verify = paused_verify
        out: dict[str, dict] = {}
        worker = threading.Thread(target=lambda: out.__setitem__("r", w.authorize(eff, approval, profile(w.installer))))
        worker.start()
        try:
            self.assertTrue(paused.wait(10.0))
            self.assertTrue(w.call(w.host, "breaker.report", connection="conn-b", event_type="plan_limit")["ok"])
            _trip_and_reset(self, w, "conn-b")
        finally:
            release.set()
            worker.join(10.0)
        self.assertFalse(worker.is_alive())
        self.assertTrue(out["r"]["ok"], out["r"])
        self.assertEqual(_charges(w), 1)

    def test_a_reset_while_an_authorization_is_in_flight_refuses_it_too(self) -> None:
        # The trip was acknowledged before this call began; the reset lands before its first check.
        # A reset is a change of the trip state, so the call is refused rather than let through on a
        # state that changed under it (the case it guards: a call racing the trip's acknowledgement).
        w = World(self)
        w.load()
        w.bind()
        eff = effect()
        approval = w.approve(eff)
        self.assertTrue(w.call(w.host, "breaker.report", connection="conn-a", event_type="plan_limit")["ok"])
        paused, release = threading.Event(), threading.Event()
        real_healthy = w.health.require_healthy
        calls = []

        def paused_first_check():
            calls.append(1)
            if len(calls) == 1:
                paused.set()
                if not release.wait(10.0):
                    raise AssertionError("never released")
            return real_healthy()

        w.health.require_healthy = paused_first_check
        out: dict[str, dict] = {}
        worker = threading.Thread(target=lambda: out.__setitem__("r", w.authorize(eff, approval, profile(w.installer))))
        worker.start()
        try:
            self.assertTrue(paused.wait(10.0))
            lease = w.call(w.host, "breaker.probe_begin", connection="conn-a")["result"]["lease"]
            self.assertTrue(w.call(w.host, "breaker.probe_reset", connection="conn-a", lease=lease,
                                   probe_ok=True)["result"]["reset"])
        finally:
            release.set()
            worker.join(10.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(_code(out["r"]), "connection_tripped")
        self.assertEqual(_charges(w), 0)

    # -- the ledger holds the guard until its transaction ends (review 1 of #378, R2) -------------------

    def test_the_guard_is_held_until_the_ledger_commits_or_rolls_back(self) -> None:
        cases = (("commit", None), ("budget_refusal", Refused), ("sql_error", sqlite3.OperationalError),
                 ("base_exception", _Interrupt))
        for case, raised in cases:
            with self.subTest(case=case):
                w = World(self)
                w.load()
                env = profile(w.installer, total=1_000, max_effect=1_000)
                real = w.authority._db
                if case == "budget_refusal":
                    w.authority.charge(env, **_charge_args(w, "first"))  # spends the whole budget
                elif case != "commit":
                    error = sqlite3.OperationalError("injected") if case == "sql_error" else _Interrupt()
                    w.authority._db = _FailingInsert(real, error)
                guard = _RecordingGuard(real)
                try:
                    if raised is None:
                        self.assertEqual(w.authority.charge(env, **_charge_args(w, case), commit_guard=guard)
                                         ["spent_minor"], 1_000)
                    else:
                        with self.assertRaises(raised):
                            w.authority.charge(env, **_charge_args(w, case), commit_guard=guard)
                finally:
                    w.authority._db = real
                self.assertTrue(guard.entered)
                self.assertFalse(guard.in_transaction_at_exit, "the guard was released with the transaction open")
                self.assertFalse(real.in_transaction)
                self.assertEqual(_charges(w), 1 if case in ("commit", "budget_refusal") else 0)

    def test_a_guard_whose_exit_raises_after_commit_leaves_one_charge(self) -> None:
        # Review 1 of #378 (DeepSeek F2): the guard's exit runs after COMMIT. The charge stands, the
        # error reaches the caller, and a retry with the same idempotency key is not charged again.
        w = World(self)
        w.load()
        env = profile(w.installer)
        with self.assertRaises(_Interrupt):
            w.authority.charge(env, **_charge_args(w, "exit-raises"), commit_guard=_RaisingExitGuard())
        self.assertFalse(w.authority._db.in_transaction)
        self.assertEqual(_charges(w), 1)
        with self.assertRaises(Refused) as again:
            w.authority.charge(env, **_charge_args(w, "exit-raises"))
        self.assertEqual(again.exception.code, "effect_replayed")
        self.assertEqual(_charges(w), 1)

    def test_a_guard_that_refuses_on_entry_writes_nothing(self) -> None:
        w = World(self)
        w.load()
        env = profile(w.installer)
        with self.assertRaises(Refused) as refused:
            w.authority.charge(env, **_charge_args(w, "refused"), commit_guard=_RefusingGuard())
        self.assertEqual(refused.exception.code, "run_faulted")
        self.assertFalse(w.authority._db.in_transaction)
        self.assertEqual(_charges(w), 0)
        self.assertEqual(w.authority.charge(env, **_charge_args(w, "after"))["spent_minor"], 1_000)

    def test_a_guard_whose_exit_swallows_a_refusal_cannot_make_it_a_charge(self) -> None:
        # Review 2 of #378 (DeepSeek F1): a guard whose exit returns true swallowed the refusal the ledger
        # raised again after its rollback, and charge returned a success for a charge it never wrote.
        # The refusal or error stands, whatever the guard's exit returns.
        cases = (("budget_total_exceeded", Refused), ("effect_replayed", Refused),
                 ("sql_error", sqlite3.OperationalError), ("base_exception", _Interrupt))
        for case, raised in cases:
            with self.subTest(case=case):
                w = World(self)
                w.load()
                env = profile(w.installer, total=1_000, max_effect=1_000)
                real = w.authority._db
                if case == "budget_total_exceeded":
                    w.authority.charge(env, **_charge_args(w, "first"))  # spends the whole budget
                elif case == "effect_replayed":
                    w.authority.charge(env, **_charge_args(w, case))  # the same key, charged once already
                else:
                    error = sqlite3.OperationalError("injected") if case == "sql_error" else _Interrupt()
                    w.authority._db = _FailingInsert(real, error)
                guard = _SwallowingGuard()
                try:
                    with self.assertRaises(raised) as caught:
                        w.authority.charge(env, **_charge_args(w, case), commit_guard=guard)
                finally:
                    w.authority._db = real
                if raised is Refused:
                    self.assertEqual(caught.exception.code, case)
                self.assertIs(guard.swallowed, raised, "the guard's exit did not see the refusal")
                self.assertFalse(real.in_transaction)
                self.assertEqual(_charges(w), 1 if raised is Refused else 0)

    def test_a_rollback_that_fails_leaves_no_transaction_for_the_next_charge(self) -> None:
        # Review 3 of #378 (DeepSeek N2): a ROLLBACK that raised left the connection inside the transaction,
        # and every later charge failed to begin its own. Now the connection is closed, which discards what
        # the transaction wrote, and the ledger is opened again.
        for guard in (None, _SwallowingGuard()):
            with self.subTest(guard=type(guard).__name__):
                w = World(self)
                w.load()
                env = profile(w.installer, profile_id="prof-new", total=10_000, max_effect=1_000)
                failing = _FailingInsertAndRollback(w.authority._db)
                w.authority._db = failing
                expected = sqlite3.OperationalError
                with self.assertRaises(expected) as raised:
                    w.authority.charge(env, **_charge_args(w, "fails"), commit_guard=guard)
                # Without a guard the rollback's error reaches the caller; a swallowing guard leaves the first.
                self.assertIn("rollback" if guard is None else "insert", str(raised.exception))
                self.assertIsNot(w.authority._db, failing)
                self.assertFalse(w.authority._db.in_transaction)
                db = sqlite3.connect(w.ledger)
                try:  # the profile the failed charge wrote inside its transaction was discarded
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM profiles").fetchone()[0], 0)
                finally:
                    db.close()
                self.assertEqual(_charges(w), 0)
                self.assertEqual(w.authority.charge(env, **_charge_args(w, "after"))["spent_minor"], 1_000)
                self.assertEqual(_charges(w), 1)

    def test_a_charge_after_a_rollback_and_a_shutdown_that_both_raise_is_refused_at_once(self) -> None:
        # Review 1 of #426 (DeepSeek F1), through charge: the connection that may still hold the transaction is
        # kept, nothing is opened beside it, and the next charge is refused ledger_invalid at once.
        w = World(self)
        w.load()
        env = profile(w.installer, profile_id="prof-new", total=10_000, max_effect=1_000)
        real = w.authority._db
        failing = _FailingRollback(real, close_fails=True)
        w.authority._db = failing
        self.addCleanup(real.close)
        self.addCleanup(real.rollback)
        self.addCleanup(setattr, w.authority, "_db", real)
        with self.assertRaises(sqlite3.OperationalError):
            w.authority.charge(env, **_charge_args(w, "fails"), commit_guard=_RefusingGuard())
        self.assertIs(w.authority._db, failing, "a connection was opened beside the one holding the transaction")
        started = time.monotonic()
        with self.assertRaises(Refused) as refused:
            w.authority.charge(env, **_charge_args(w, "after"))
        self.assertEqual(refused.exception.code, "ledger_invalid")
        self.assertLess(time.monotonic() - started, 5.0, "the refusal waited for the ledger's lock")

    def test_recovery_keeps_spend_and_replay_on_the_ledger_a_relative_path_named(self) -> None:
        # Review 1 of #426 (astra R1, sol R1), signed: a relative path, a later change of directory, and a failed
        # rollback; the recovery stays on the first ledger, so its spend and its replays still count.
        w = World(self)
        w.load()
        env = profile(w.installer, profile_id="prof-new", total=10_000, max_effect=1_000)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(os.path.dirname(w.ledger))
        auth = Authority(w.installer.public, os.path.basename(w.ledger), w.clock)
        self.addCleanup(auth.close)
        auth.charge(env, **_charge_args(w, "prior"))
        elsewhere = temp_dir(self)
        create_ledger(os.path.join(elsewhere, os.path.basename(w.ledger)), w.installer.public)
        os.chdir(elsewhere)
        auth._db = _FailingInsertAndRollback(auth._db)
        with self.assertRaises(sqlite3.OperationalError):
            auth.charge(env, **_charge_args(w, "fails"))
        with self.assertRaises(Refused) as refused:
            auth.charge(env, **_charge_args(w, "prior"))
        self.assertEqual(refused.exception.code, "effect_replayed")
        self.assertEqual(auth.charge(env, **_charge_args(w, "after"))["spent_minor"], 2_000)

    def test_a_guard_whose_exit_raises_on_a_refusal_reports_its_error_with_the_refusal_as_context(self) -> None:
        # Review 2 of #378 (DeepSeek, section 6): the guard's error takes the refusal's place, with the
        # refusal as its context, and nothing is written. Either way the call is not authorized.
        w = World(self)
        w.load()
        env = profile(w.installer, total=1_000, max_effect=1_000)
        w.authority.charge(env, **_charge_args(w, "first"))
        with self.assertRaises(_Interrupt) as caught:
            w.authority.charge(env, **_charge_args(w, "second"), commit_guard=_RaisingOnRefusalGuard())
        self.assertIsInstance(caught.exception.__context__, Refused)
        self.assertEqual(caught.exception.__context__.code, "budget_total_exceeded")
        self.assertFalse(w.authority._db.in_transaction)
        self.assertEqual(_charges(w), 1)


class _FailingInsertAndRollback:
    """The ledger's connection, failing the charge's INSERT and then the ROLLBACK, which rolls nothing back."""

    def __init__(self, real) -> None:
        self._real = real

    @property
    def in_transaction(self) -> bool:
        return self._real.in_transaction

    def execute(self, sql, params=()):
        if sql.startswith("INSERT INTO charges"):
            raise sqlite3.OperationalError("injected insert failure")
        if sql == "ROLLBACK":
            raise sqlite3.OperationalError("injected rollback failure")
        return self._real.execute(sql, params)

    def close(self) -> None:
        self._real.close()


class _Interrupt(BaseException):
    """A BaseException that is not KeyboardInterrupt, so no runner treats it as a real interrupt."""


class _FailingInsert:
    """The ledger's connection, failing the charge's INSERT with a given error."""

    def __init__(self, real, error: BaseException) -> None:
        self._real, self._error = real, error

    def execute(self, sql, params=()):
        if sql.startswith("INSERT INTO charges"):
            raise self._error
        return self._real.execute(sql, params)

    def close(self) -> None:
        self._real.close()


class _RecordingGuard:
    """A commit guard that records whether the ledger's transaction was still open when it was released."""

    def __init__(self, connection) -> None:
        self._connection = connection
        self.entered = False
        self.in_transaction_at_exit = None

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc) -> None:
        self.in_transaction_at_exit = self._connection.in_transaction


class _RaisingExitGuard:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, *exc) -> None:
        if exc_type is None:
            raise _Interrupt()


class _SwallowingGuard:
    """A commit guard whose exit swallows whatever left the ledger's transaction, and records its type."""

    def __init__(self) -> None:
        self.swallowed = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *exc) -> bool:
        self.swallowed = exc_type
        return True


class _RaisingOnRefusalGuard:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, *exc) -> None:
        if exc_type is not None:
            raise _Interrupt()


class _RefusingGuard:
    def __enter__(self):
        raise Refused("run_faulted")

    def __exit__(self, *exc) -> None:  # pragma: no cover - never entered
        raise AssertionError("exited a guard that was never entered")


def _charge_args(w: World, key: str) -> dict:
    return dict(plugin_id="demo.plugin", artifact_sha256=w.loader.artifact("demo.plugin"), scope=scope_of(),
                kind="order.submit", provider="fake-provider", model_version="fake-model-1", currency="USD",
                amount_minor=1_000, idempotency_key=key)


def _signal_begin(w: World, event: threading.Event) -> None:
    """Wrap the ledger's connection so that ``event`` is set when a charge begins its write transaction."""
    real_db = w.authority._db

    class _Db:
        def execute(self, sql, params=()):
            if sql == "BEGIN IMMEDIATE":
                event.set()
            return real_db.execute(sql, params)

        def close(self) -> None:
            real_db.close()

    w.authority._db = _Db()


def _trip_and_reset(test: unittest.TestCase, w: World, connection: str = "conn-a") -> None:
    """Trip a connection through the host, then clear it through one successful leased probe."""
    trip = w.call(w.host, "breaker.report", connection=connection, event_type="plan_limit")
    test.assertTrue(trip["ok"] and trip["result"]["tripped"], trip)
    lease = w.call(w.host, "breaker.probe_begin", connection=connection)
    test.assertTrue(lease["ok"], lease)
    reset = w.call(w.host, "breaker.probe_reset", connection=connection, lease=lease["result"]["lease"],
                   probe_ok=True)
    test.assertTrue(reset["ok"] and reset["result"]["reset"], reset)


def _light_core(test: unittest.TestCase, **overrides) -> CoreService:
    """A CoreService over real components that need no cryptography (no plugin loaded, no ledger)."""
    path = os.path.join(temp_dir(test), "breaker.db")
    create_breaker_store(path)
    breaker = ConnectionBreaker(path)
    test.addCleanup(breaker.close)
    health = CoreHealth()
    trust = TrustStore({})
    test.addCleanup(trust.close)
    loader = PluginLoader(trust, {}, ReservedEffects(frozenset(), ()), health, FakeGilProbe())
    args = dict(loader=loader, gate=ApprovalGate(lambda *drawn: None, authenticate, Clock()), breaker=breaker,
                health=health, authority=None, effect_permissions={"order": "order"},
                authority_required=frozenset({"order"}))
    args.update(overrides)
    return CoreService(**args)


_ANCHOR = "ab" * 32  # a public key in its hex form: the ledger's open and checks need no signature


class _FailingRollback:
    """The ledger's connection, failing its ROLLBACK (which rolls nothing back) and, if asked, its close."""

    def __init__(self, real, *, close_fails: bool = False, error: BaseException | None = None) -> None:
        self._real, self._close_fails, self._error = real, close_fails, error

    @property
    def in_transaction(self) -> bool:
        return self._real.in_transaction

    def execute(self, sql, params=()):
        if sql == "ROLLBACK":
            raise self._error or sqlite3.OperationalError("injected rollback failure")
        return self._real.execute(sql, params)

    def close(self) -> None:
        if self._close_fails:
            raise sqlite3.OperationalError("injected close failure")
        self._real.close()


def _ledger_rows(path: str, table: str) -> int:
    db = sqlite3.connect(path, timeout=5)
    try:
        return db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
    finally:
        db.close()


class LedgerRepairTests(unittest.TestCase):
    """The repair of a failed ROLLBACK and its own failure branches, on a real ledger and with no signing, so on
    both interpreters (review 1 of #426, DeepSeek F1, F2, F4 and F6)."""

    def setUp(self) -> None:
        self.path = os.path.join(temp_dir(self), "ledger.db")
        create_ledger(self.path, _ANCHOR)
        self.auth = Authority(_ANCHOR, self.path, lambda: 0)
        self.addCleanup(self.auth.close)

    def _fail_the_rollback(self, *, close_fails: bool = False, before=None):
        """A write inside the ledger's transaction, then a ROLLBACK that raises; returns (real, stand-in)."""
        real = self.auth._db
        real.execute("BEGIN IMMEDIATE")
        real.execute("INSERT INTO charges VALUES ('written', 'prof', 1000)")
        failing = _FailingRollback(real, close_fails=close_fails)
        self.auth._db = failing
        if before is not None:
            before()
        with self.assertRaises(sqlite3.OperationalError) as raised:
            self.auth._roll_back()
        self.assertIn("rollback", str(raised.exception), "another error took the rollback's place")
        return real, failing

    def _assert_pragmas(self, db) -> None:
        self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], 2, "not synchronous=FULL")
        self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], 30000)
        self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def _assert_out_of_use(self) -> None:
        started = time.monotonic()
        with self.assertRaises(Refused) as refused:
            self.auth._require_usable()
        self.assertEqual(refused.exception.code, "ledger_invalid")
        self.assertLess(time.monotonic() - started, 5.0, "the refusal waited for the ledger's lock")

    def test_the_first_open_is_synchronous_full_with_a_busy_timeout_and_the_index(self) -> None:  # F6
        self._assert_pragmas(self.auth._db)
        self.auth.close()
        db = sqlite3.connect(self.path)
        try:  # a ledger made before the index gains it at the open
            db.execute("DROP INDEX charges_by_profile")
        finally:
            db.close()
        auth = Authority(_ANCHOR, self.path, lambda: 0)
        self.addCleanup(auth.close)
        self.assertEqual(auth._db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index'"
                                          " AND name='charges_by_profile'").fetchone()[0], 1)

    def test_a_failed_rollback_opens_the_ledger_again_with_nothing_written(self) -> None:  # N2, F4, F6
        real, failing = self._fail_the_rollback()
        self.assertIsNot(self.auth._db, failing)
        self.assertFalse(self.auth._db.in_transaction)
        self.assertEqual(_ledger_rows(self.path, "charges"), 0)  # closing discarded what it wrote
        self.auth._require_usable()
        self._assert_pragmas(self.auth._db)  # the reopen makes every check and setting of the first open
        self.auth._db.execute("BEGIN IMMEDIATE")
        self.auth._db.execute("INSERT INTO charges VALUES ('later', 'prof', 1000)")
        self.auth._db.execute("COMMIT")
        self.assertEqual(_ledger_rows(self.path, "charges"), 1)

    def test_a_shutdown_that_raises_opens_nothing_beside_the_transaction_and_refuses_at_once(self) -> None:  # F1
        real, failing = self._fail_the_rollback(close_fails=True)
        self.addCleanup(real.close)
        self.addCleanup(real.rollback)
        self.addCleanup(setattr, self.auth, "_db", real)  # first: the authority's own close must not raise
        self.assertIs(self.auth._db, failing, "a connection was opened beside the one holding the transaction")
        self.assertTrue(real.in_transaction)
        self._assert_out_of_use()

    @unittest.skipIf(os.name == "nt", "a ledger open in this process cannot be removed on Windows")
    def test_a_ledger_removed_is_not_created_again_and_refuses_with_a_code(self) -> None:  # F2, F5
        real, failing = self._fail_the_rollback(before=lambda: os.unlink(self.path))
        self.assertFalse(os.path.exists(self.path), "the reopen created an empty ledger")
        with self.assertRaises(sqlite3.ProgrammingError):  # the old handle was closed all the same
            real.execute("SELECT 1")
        self._assert_out_of_use()
        with self.assertRaises(Refused) as refused:
            Authority(_ANCHOR, self.path, lambda: 0)
        self.assertEqual(refused.exception.code, "ledger_missing")
        self.assertFalse(os.path.exists(self.path))

    @unittest.skipIf(os.name == "nt", "a ledger open in this process cannot be replaced on Windows")
    def test_a_ledger_replaced_by_another_anchors_is_refused_on_the_reopen(self) -> None:  # F5
        def replace() -> None:
            os.unlink(self.path)
            create_ledger(self.path, "cd" * 32)

        self._fail_the_rollback(before=replace)
        self._assert_out_of_use()
        self.assertEqual(_ledger_rows(self.path, "charges"), 0)  # the other anchor's ledger is untouched

    @unittest.skipIf(os.name == "nt", "a ledger open in this process cannot be replaced on Windows")
    def test_a_ledger_replaced_by_another_stores_magic_is_refused_on_the_reopen(self) -> None:  # astra R2.4
        def replace() -> None:
            os.unlink(self.path)
            create_ledger(self.path, _ANCHOR)
            db = sqlite3.connect(self.path)
            try:
                db.execute("UPDATE meta SET v = 'another-store/v0' WHERE k = 'magic'")
                db.commit()
            finally:
                db.close()

        self._fail_the_rollback(before=replace)
        self._assert_out_of_use()

    def test_an_interruption_in_the_rollback_still_opens_the_ledger_again(self) -> None:  # astra R2.1
        real = self.auth._db
        real.execute("BEGIN IMMEDIATE")
        real.execute("INSERT INTO charges VALUES ('written', 'prof', 1000)")
        self.auth._db = _FailingRollback(real, error=KeyboardInterrupt("synthetic interruption in ROLLBACK"))
        with self.assertRaises(KeyboardInterrupt):
            self.auth._roll_back()
        self.assertFalse(self.auth._db.in_transaction)
        self.assertEqual(_ledger_rows(self.path, "charges"), 0)
        self.auth._require_usable()

    def test_recovery_stays_on_the_ledger_a_relative_path_named_at_construction(self) -> None:  # astra, sol R1
        self.addCleanup(os.chdir, os.getcwd())
        first, second = os.path.dirname(self.path), temp_dir(self)
        create_ledger(os.path.join(second, "ledger.db"), _ANCHOR)  # the same anchor, another directory
        os.chdir(first)
        auth = Authority(_ANCHOR, "ledger.db", lambda: 0)
        self.addCleanup(auth.close)
        os.chdir(second)
        real = auth._db
        real.execute("BEGIN IMMEDIATE")
        auth._db = _FailingRollback(real)
        with self.assertRaises(sqlite3.OperationalError):
            auth._roll_back()
        auth._require_usable()
        self.assertTrue(os.path.samefile(auth._db.execute("PRAGMA database_list").fetchone()[2], self.path))
        auth._db.execute("INSERT INTO charges VALUES ('after', 'prof', 1000)")
        self.assertEqual((_ledger_rows(self.path, "charges"), _ledger_rows(os.path.join(second, "ledger.db"),
                                                                           "charges")), (1, 0))

    def test_a_ledger_removed_after_the_check_is_not_created_by_the_open(self) -> None:  # F2: the open never creates
        missing = os.path.join(temp_dir(self), "gone.db")
        with mock.patch("recursion_core.plugin_security.authority.os.path.isfile", return_value=True):
            with self.assertRaises(Refused) as refused:
                Authority(_ANCHOR, missing, lambda: 0)
        self.assertEqual(refused.exception.code, "ledger_invalid")
        self.assertFalse(os.path.exists(missing), "the open created an empty ledger")

    def test_a_charge_once_the_authority_is_shut_down_is_refused_with_a_code(self) -> None:  # DeepSeek's consumer table
        self.auth.close()
        self._assert_out_of_use()


class CommitFencePrimitiveTests(unittest.TestCase):
    """The fence's own rules, with no plugin and no signature, so they run on every interpreter."""

    def test_drain_waits_for_a_held_section(self) -> None:
        fence = CommitFence()
        trying, done = threading.Event(), threading.Event()

        def drain() -> None:
            trying.set()
            fence.drain()
            done.set()

        with fence:
            worker = threading.Thread(target=drain)
            worker.start()
            self.assertTrue(trying.wait(10.0))
            self.assertFalse(done.wait(0.3), "a drain returned while a section held the fence")
        worker.join(10.0)
        self.assertTrue(done.is_set())

    def test_an_exception_in_a_section_releases_the_fence(self) -> None:
        fence = CommitFence()
        with self.assertRaises(ValueError):
            with fence:
                raise ValueError("inside the section")
        drainer = threading.Thread(target=fence.drain)
        drainer.start()
        drainer.join(10.0)
        self.assertFalse(drainer.is_alive(), "the fence stayed held after the section raised")

    def test_a_drain_or_a_second_entry_on_the_holding_thread_is_refused(self) -> None:
        # Review 1 of #378 (DeepSeek F3): either would wait for itself for ever. The checks run in a
        # daemon thread, so a fence that hung would fail this test, not hold the runner.
        fence = CommitFence()
        seen: list[str] = []

        def inside() -> None:
            with fence:
                for name, call in (("drain", fence.drain), ("enter", fence.__enter__)):
                    try:
                        call()
                    except RuntimeError:
                        seen.append(name)
            fence.drain()  # once the section has ended, the same thread may drain and enter again
            with fence:
                seen.append("again")

        worker = threading.Thread(target=inside, daemon=True)
        worker.start()
        worker.join(10.0)
        self.assertFalse(worker.is_alive(), "the fence waited for itself")
        self.assertEqual(seen, ["drain", "enter", "again"])
        drainer = threading.Thread(target=fence.drain, daemon=True)  # released after the section
        drainer.start()
        drainer.join(10.0)
        self.assertFalse(drainer.is_alive())

    def test_trip_changes_count_per_connection_and_only_after_a_position(self) -> None:
        fence = CommitFence()
        fence.mark_trip_change("conn-a")
        started = fence.position()
        self.assertFalse(fence.trip_changed_since("conn-a", started))  # changed before the call began
        fence.mark_trip_change("conn-b")
        self.assertFalse(fence.trip_changed_since("conn-a", started))  # another connection
        self.assertTrue(fence.trip_changed_since("conn-b", started))
        fence.mark_trip_change("conn-a")
        self.assertTrue(fence.trip_changed_since("conn-a", started))
        self.assertFalse(fence.trip_changed_since("conn-a", fence.position()))

    def test_marks_from_many_threads_are_each_counted(self) -> None:
        # Review 2 of #378 (DeepSeek F2, Astra's N17): without the marks lock a free-threaded build loses
        # increments, and a lost mark is a trip that trip_changed_since does not see.
        fence = CommitFence()
        threads, marks = 8, 4_000
        start = threading.Barrier(threads)
        unseen: list[str] = []

        def mark(connection: str) -> None:
            start.wait(10.0)
            for _ in range(marks):
                before = fence.position()
                fence.mark_trip_change(connection)
                if not fence.trip_changed_since(connection, before):
                    unseen.append(connection)

        workers = [threading.Thread(target=mark, args=("conn-%d" % n,), daemon=True) for n in range(threads)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(60.0)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(fence.position(), threads * marks)
        self.assertEqual(unseen, [])

    def test_the_marks_are_read_under_the_marks_lock(self) -> None:
        # Review 3 of #378 (DeepSeek N1): the writer's lock is pinned by the concurrent marks; this pins the
        # two readers. While the marks lock is held, neither position() nor trip_changed_since() answers.
        fence = CommitFence()
        fence.mark_trip_change("conn-a")
        for name, read, expected in (("position", fence.position, 1),
                                     ("trip_changed_since", lambda: fence.trip_changed_since("conn-a", 0), True)):
            with self.subTest(read=name):
                done = threading.Event()
                answer: list = []
                def reading(answer=answer, read=read, done=done) -> None:  # this round's, even after a join that timed out
                    answer.append(read())
                    done.set()

                reader = threading.Thread(target=reading, daemon=True)
                with fence._marks_lock:
                    reader.start()
                    self.assertFalse(done.wait(0.3), name + " answered while the marks lock was held")
                reader.join(10.0)
                self.assertEqual(answer, [expected])

    def test_a_thread_waiting_to_enter_does_not_take_the_holders_place(self) -> None:
        # Review 2 of #378 (DeepSeek F3): the holder's slot is set only once the fence is taken. Set
        # before, a thread blocked on entry would own the slot, and the holder's own drain would wait
        # for itself for ever. Daemon threads, so a hang fails this test and does not hold the runner.
        fence = CommitFence()
        counting = _CountingLock(fence._lock)
        fence._lock = counting
        seen: list[str] = []
        holding, entered = threading.Event(), threading.Event()

        def holder() -> None:
            with fence:
                holding.set()
                if not counting.attempt(2).wait(10.0):  # the second attempt: the blocker waits for the fence
                    seen.append("no blocker")
                    return
                try:
                    fence.drain()
                    seen.append("drained")
                except RuntimeError:
                    seen.append("refused")

        def blocker() -> None:
            with fence:
                entered.set()

        first = threading.Thread(target=holder, daemon=True)
        first.start()
        self.assertTrue(holding.wait(10.0))
        second = threading.Thread(target=blocker, daemon=True)
        second.start()
        first.join(10.0)
        self.assertFalse(first.is_alive(), "the holder's own drain waited for itself")
        self.assertEqual(seen, ["refused"])
        second.join(10.0)
        self.assertTrue(entered.is_set(), "the waiting thread never entered once the holder left")

    def test_is_vetoed_reads_under_the_gates_lock(self) -> None:
        gate = ApprovalGate(lambda *drawn: None, authenticate, Clock())
        done = threading.Event()
        answer: list[bool] = []
        reader = threading.Thread(target=lambda: (answer.append(gate.is_vetoed("0" * 64)), done.set()))
        with gate._lock:
            reader.start()
            self.assertFalse(done.wait(0.3), "is_vetoed answered while the gate's lock was held")
        reader.join(10.0)
        self.assertEqual(answer, [False])

    def test_the_core_takes_only_a_commit_fence(self) -> None:
        shared = CommitFence()
        self.assertIs(_light_core(self, commit_fence=shared).commit_fence, shared)
        self.assertIs(type(_light_core(self).commit_fence), CommitFence)

        class Subclass(CommitFence):
            def drain(self) -> None:  # a fence that would answer at once
                pass

        for wrong in (object(), threading.Lock(), Subclass()):
            with self.subTest(wrong=type(wrong).__name__):
                with self.assertRaises(TypeError):
                    _light_core(self, commit_fence=wrong)
        core = _light_core(self, commit_fence=shared)
        for replacement in (CommitFence(), Subclass(), None):  # review 1 of #378 (DeepSeek F7)
            with self.subTest(replacement=type(replacement).__name__):
                with self.assertRaises(AttributeError):
                    core.commit_fence = replacement
        self.assertIs(core.commit_fence, shared)

    def test_a_report_that_cannot_trip_does_not_wait_for_a_commit_section(self) -> None:
        # Review 1 of #378 (DeepSeek F5): the core's own timeouts change nothing, so the host's report
        # of one answers while a commit section holds the fence; a report that trips waits for it.
        core = _light_core(self)
        host = core.client_for("host")
        answers: dict[str, dict] = {}

        def report(event: str) -> None:
            answers[event] = World.call(host, "breaker.report", connection="conn-a", event_type=event)

        with core.commit_fence:
            quiet = threading.Thread(target=report, args=("core_timeout",), daemon=True)
            quiet.start()
            quiet.join(10.0)
            self.assertFalse(quiet.is_alive(), "a report that cannot trip waited for the commit section")
            tripping = threading.Thread(target=report, args=("plan_limit",), daemon=True)
            tripping.start()
            tripping.join(0.3)
            self.assertTrue(tripping.is_alive(), "a report that trips answered while a commit section held the fence")
        tripping.join(10.0)
        self.assertFalse(tripping.is_alive())
        self.assertEqual(answers["core_timeout"]["result"], {"tripped": False})
        self.assertEqual(answers["plan_limit"]["result"], {"tripped": True})

    def test_a_trip_and_a_reset_through_the_host_mark_the_connection(self) -> None:
        core = _light_core(self)
        host = core.client_for("host")
        started = core.commit_fence.position()
        for event in ("core_timeout", "core_deadline", "caller_cancelled"):  # never trips: no mark
            self.assertFalse(World.call(host, "breaker.report", connection="conn-a", event_type=event)
                             ["result"]["tripped"])
        self.assertFalse(core.commit_fence.trip_changed_since("conn-a", started))
        self.assertTrue(World.call(host, "breaker.report", connection="conn-a", event_type="plan_limit")
                        ["result"]["tripped"])
        self.assertTrue(core.commit_fence.trip_changed_since("conn-a", started))
        after_trip = core.commit_fence.position()
        lease = World.call(host, "breaker.probe_begin", connection="conn-a")["result"]["lease"]
        self.assertFalse(World.call(host, "breaker.probe_reset", connection="conn-a", lease=lease,
                                    probe_ok=False)["result"]["reset"])  # a failed probe changes nothing
        self.assertFalse(core.commit_fence.trip_changed_since("conn-a", after_trip))
        lease = World.call(host, "breaker.probe_begin", connection="conn-a")["result"]["lease"]
        self.assertTrue(World.call(host, "breaker.probe_reset", connection="conn-a", lease=lease,
                                   probe_ok=True)["result"]["reset"])
        self.assertTrue(core.commit_fence.trip_changed_since("conn-a", after_trip))
        self.assertFalse(core.commit_fence.trip_changed_since("conn-b", started))


if __name__ == "__main__":
    unittest.main()
