"""Shared fixtures for the plugin-security tests. Keys are generated per run; none is real.

Signing needs the ``cryptography`` package. Where it is missing (this box's 3.14t), the
tests that sign are skipped with that reason; nothing pretends a signature verified.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import unittest

from recursion_core.plugin_security import (
    ApprovalGate, Authority, ConnectionBreaker, CoreHealth, CoreService, Grant, PluginLoader,
    ReservedEffects, TrustStore, canonical, create_breaker_store, create_ledger, parse, parse_manifest,
)
from recursion_core.plugin_security.signing import backend_available

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
except ImportError:  # pragma: no cover
    Ed25519PrivateKey = None

needs_crypto = unittest.skipUnless(backend_available() and Ed25519PrivateKey is not None,
                                   "cryptography not installed for this interpreter")

PLUGIN_SOURCE = b"VALUE = 41 + 1\n"
OPERATOR_TOKEN = "operator-ui-token-fake"
PLUGIN_TOKEN = "plugin-token-fake"
AGENT_TOKEN = "agent-token-fake"


class Key:
    def __init__(self) -> None:
        self._private = Ed25519PrivateKey.generate()
        self.public = self._private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()

    def sign(self, message: bytes) -> str:
        return self._private.sign(message).hex()


class FakeGilProbe:
    """SIMULATED GIL state for tests on a GIL build. Not evidence about 3.14t."""

    def __init__(self, states=None):
        self.states = list(states or [False])
        self.calls = 0

    def gil_enabled(self) -> bool:
        self.calls += 1
        return self.states[min(self.calls - 1, len(self.states) - 1)]


def manifest_bytes(key_public: str, *, plugin_id="demo.plugin", source=PLUGIN_SOURCE,
                   permissions=("read", "propose", "order"), network=(), credentials=(),
                   install_scripts=(), lock="0" * 64) -> bytes:
    return canonical({
        "schema": "csl-plugin-manifest/vNext",
        "plugin_id": plugin_id,
        "version": "1.0.0",
        "artifact_sha256": hashlib.sha256(source).hexdigest(),
        "dependency_lock_sha256": lock,
        "install_scripts": list(install_scripts),
        "developer_key": key_public,
        "permissions": list(permissions),
        "network_destinations": list(network),
        "credential_refs": list(credentials),
    })


def temp_dir(test: unittest.TestCase) -> str:
    path = tempfile.mkdtemp(prefix="csl-plugsec-")
    test.addCleanup(shutil.rmtree, path, True)
    return path


class Clock:
    def __init__(self, now: int = 1_000_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def profile(installer: Key, *, plugin_id="demo.plugin", source=PLUGIN_SOURCE, profile_id="prof-1",
            account="paper-acct", venue="paper", total=10_000, max_effect=5_000,
            not_before=0, expires_at=2_000_000, scope=None, **overrides) -> dict:
    body = {
        "schema": "csl-authority-profile/vNext",
        "profile_id": profile_id,
        "issuer": "installer",
        "plugin_id": plugin_id,
        "artifact_sha256": hashlib.sha256(source).hexdigest(),
        "scope": scope if scope is not None else scope_of(account=account, venue=venue),
        "provider": "fake-provider",
        "model_version": "fake-model-1",
        "actions": ["order.submit"],
        "permission": "order",
        "not_before": not_before,
        "expires_at": expires_at,
        "budgets": {"currency": "USD", "max_effect_minor": max_effect, "total_minor": total},
    }
    body.update(overrides)
    return {"profile": body, "signature": installer.sign(canonical(body))}


def scope_of(*, account="paper-acct", venue="paper", contract_family="fake-futures",
             adapter="fake-adapter", environment="paper") -> dict:
    return {"account": account, "venue": venue, "contract_family": contract_family,
            "adapter": adapter, "environment": environment}


def effect(*, kind="order.submit", plugin_id="demo.plugin", run_id="run-1", connection="conn-a",
           amount=1_000, account="paper-acct", venue="paper", payload=None, scope=None) -> dict:
    return {
        "schema": "csl-effect/vNext", "kind": kind, "plugin_id": plugin_id, "run_id": run_id,
        "connection": connection,
        "scope": scope if scope is not None else scope_of(account=account, venue=venue),
        "currency": "USD", "amount_minor": amount,
        "payload": payload if payload is not None else {"note": "fake"},
    }


def authenticate(credential: object):
    return {OPERATOR_TOKEN: "operator_ui", PLUGIN_TOKEN: "plugin", AGENT_TOKEN: "agent"}.get(credential)


class World:
    """A full core wired the way a composition root would, with fakes for the UI."""

    def __init__(self, test: unittest.TestCase, *, gil_probe=None, grant_permissions=("read", "propose", "order"),
                 signed=True, unsigned_ack=False) -> None:
        self.dir = temp_dir(test)
        self.developer = Key()
        self.installer = Key()
        self.clock = Clock()
        self.drawn: list = []
        self.health = CoreHealth()
        self.breaker_store = os.path.join(self.dir, "breaker.db")
        create_breaker_store(self.breaker_store)
        self.breaker = ConnectionBreaker(self.breaker_store)
        test.addCleanup(self.breaker.close)
        trust = TrustStore({"dev": self.developer.public} if signed else {})
        self.manifest = manifest_bytes(self.developer.public)
        kid = trust.classify(parse_manifest(self.manifest), self.developer.sign(self.manifest))[1]
        grant = Grant("demo.plugin", hashlib.sha256(PLUGIN_SOURCE).hexdigest(), kid,
                      tuple(grant_permissions), unsigned_ack)
        self.loader = PluginLoader(trust, {"demo.plugin": grant},
                                   ReservedEffects(frozenset({"venue.example"}), ("venue:",)),
                                   self.health, gil_probe or FakeGilProbe())
        self.gate = ApprovalGate(lambda *drawn: self.drawn.append(drawn), authenticate, self.clock)
        self.ledger = os.path.join(self.dir, "ledger.db")
        create_ledger(self.ledger, self.installer.public)
        self.authority = Authority(self.installer.public, self.ledger, self.clock)
        test.addCleanup(self.authority.close)
        self.core = CoreService(loader=self.loader, gate=self.gate, breaker=self.breaker,
                                health=self.health, authority=self.authority,
                                effect_permissions={"order": "order", "note": "propose"},
                                authority_required=frozenset({"order"}))
        self.host = self.core.client_for("host")
        self.plugin = self.core.client_for("demo.plugin")

    def load(self):
        return self.loader.load(self.manifest, self.developer.sign(self.manifest), PLUGIN_SOURCE)

    @staticmethod
    def call(client, op: str, **args) -> dict:
        return parse(client(canonical({"protocol": "csl-core/v0", "op": op, "args": args})))

    def bind(self, run_id="run-1", connection="conn-a", plugin_id="demo.plugin",
             provider="fake-provider", model_version="fake-model-1") -> None:
        out = self.call(self.host, "run.bind", run_id=run_id, connection=connection,
                        plugin_id=plugin_id, provider=provider, model_version=model_version)
        assert out["ok"], out

    def approve(self, eff: dict) -> str:
        out = self.call(self.plugin, "effect.request", effect=eff)
        assert out["ok"], out
        approval_id, digest = out["result"]["approval_id"], out["result"]["digest"]
        decided = self.call(self.host, "approval.decide", approval_id=approval_id, digest=digest,
                            credential=OPERATOR_TOKEN, decision="approve")
        assert decided["ok"], decided
        return approval_id

    def authorize(self, eff: dict, approval_id, envelope) -> dict:
        return self.call(self.plugin, "effect.authorize", effect=eff, approval_id=approval_id,
                         authority=envelope)
