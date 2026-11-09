"""O11 approval gate and connection breaker, driven only through the ``csl-core/v0`` bytes.

These tests talk to ``CoreService`` through request and response bytes, the way a later
Rust core would be driven, not through Python internals.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from recursion_core.plugin_security import (
    PROTOCOL, CoreHealth, CoreService, PluginLoader, Refused, TrustStore, canonical, parse_manifest,
)
from recursion_core.plugin_security.tests.support import (
    AGENT_TOKEN, OPERATOR_TOKEN, PLUGIN_SOURCE, PLUGIN_TOKEN, FakeGilProbe, World, effect, needs_crypto,
    profile, scope_of,
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
                         ("key.revoke", dict(key_id=key_id(self.w.developer.public)))):
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


if __name__ == "__main__":
    unittest.main()
