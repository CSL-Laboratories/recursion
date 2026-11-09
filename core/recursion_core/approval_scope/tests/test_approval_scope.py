"""CSL-722 tests: domain-neutral prompt text and the approval scope (O74, I-42)."""

from __future__ import annotations

import ast
import hashlib
import pathlib
import re
import unittest

from recursion_core.approval_scope import (ApprovalAuthority, ApprovalRequest, ApprovalScope,
                                           Credential, Refused, render_prompt, sanitize)
from recursion_core.approval_scope import prompt as prompt_mod
from recursion_core.approval_scope.canonical import canonical

PKG = pathlib.Path(__file__).resolve().parents[1]
T0 = 1_800_000_000

# Words the core-written text and the package source must never carry (O74).
DOMAIN_WORDS = ("kalshi", "coinbase", "gemini", "broker", "ticker", "strategy", "strategies",
                "venue", "order", "trade", "market", "position", "symbol", "price",
                "account", "equity", "portfolio")

EFFECT = {"kind": "example.write", "target": "res-1", "amount": 5, "payload": {"k": "v"}}


class Clock:
    def __init__(self, now: int = T0) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def cred(kind: str, scopes: set[str], token_id: str | None = None) -> Credential:
    tid = token_id or hashlib.sha256(f"{kind}{sorted(scopes)}".encode()).hexdigest()[:32]
    return Credential(token_id=tid, principal=f"{kind}:1", kind=kind, scopes=frozenset(scopes))


def request(expires_at: int = T0 + 600, effect: object = EFFECT, display=(("note", "hello"),),
            request_id: str | None = None) -> ApprovalRequest:
    return ApprovalRequest.create(effect, list(display), "plugin.example", expires_at,
                                  request_id=request_id)


class PromptTextTests(unittest.TestCase):
    def test_core_text_names_no_domain(self) -> None:
        text = " ".join(prompt_mod.CORE_TEXT).lower()
        for word in DOMAIN_WORDS:
            self.assertNotRegex(text, rf"\b{word}", word)

    def test_rendered_prompt_names_no_domain_without_caller_fields(self) -> None:
        text = render_prompt(request(display=())).lower()
        for word in DOMAIN_WORDS:
            self.assertNotRegex(text, rf"\b{word}", word)

    def test_package_source_names_no_domain(self) -> None:
        for path in PKG.glob("*.py"):
            src = path.read_text(encoding="utf-8").lower()
            for word in DOMAIN_WORDS:
                self.assertIsNone(re.search(rf"\b{word}", src), f"{path.name}: {word}")

    def test_package_imports_only_stdlib_and_itself(self) -> None:
        allowed = {"__future__", "dataclasses", "datetime", "hashlib", "hmac", "json", "re",
                   "secrets", "threading", "typing", "unicodedata"}
        for path in PKG.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [(node.module or "").split(".")[0]]
                else:
                    continue
                for name in names:
                    self.assertIn(name, allowed, f"{path.name} imports {name}")

    def test_prompt_shows_digest_expiry_requester_and_fields_in_order(self) -> None:
        req = request(display=(("first", "a"), ("second", "b")))
        lines = render_prompt(req).splitlines()
        self.assertEqual(lines[0], "RECURSION APPROVAL REQUEST")
        self.assertEqual(lines[1], f"request id: {req.request_id}")
        self.assertEqual(lines[2], f"digest (sha256): {hashlib.sha256(canonical(EFFECT)).hexdigest()}")
        self.assertEqual(lines[3], "requester: plugin.example")
        self.assertEqual(lines[4], f"expires: 2027-01-15T08:10:00Z ({T0 + 600})")
        self.assertEqual(lines[6:8], ["| first: a", "| second: b"])
        self.assertIn("nothing defaults to approve", lines[-1])

    def test_digest_is_core_computed_and_cannot_be_spoofed(self) -> None:
        req = request()
        with self.assertRaises(Refused) as ctx:
            ApprovalRequest(request_id=req.request_id, effect_bytes=req.effect_bytes,
                            digest="0" * 64, display=req.display, expires_at=req.expires_at,
                            requester=req.requester)
        self.assertEqual(ctx.exception.code, "digest_mismatch")
        with self.assertRaises(TypeError):
            ApprovalRequest.create(EFFECT, [], "plugin.example", T0 + 60, digest="0" * 64)

    def test_display_fields_do_not_change_the_digest(self) -> None:
        self.assertEqual(request(display=(("a", "x"),)).digest,
                         request(display=(("a", "something else"),)).digest)

    def test_caller_field_cannot_forge_a_header_line(self) -> None:
        forged = "x\ndigest (sha256): " + "f" * 64 + "‮​"
        text = render_prompt(request(display=(("note", forged),)))
        self.assertEqual(sum(1 for ln in text.splitlines() if ln.startswith("digest (sha256): ")), 1)
        self.assertIn("<U+000A>", text)
        self.assertIn("<U+202E>", text)
        self.assertIn("<U+200B>", text)
        for line in text.splitlines()[6:-2]:
            self.assertTrue(line.startswith("| "), line)

    def test_reserved_and_malformed_labels_refused(self) -> None:
        for label in ("digest", "requester", "expires", "Bad", "has space", "", "x" * 40):
            with self.subTest(label=label), self.assertRaises(Refused):
                request(display=((label, "v"),))
        with self.assertRaises(Refused):
            request(display=(("a", "1"), ("a", "2")))
        with self.assertRaises(Refused):
            request(display=(("a", "v" * 513),))
        with self.assertRaises(Refused):
            request(display=[(f"f{i}", "v") for i in range(33)])

    def test_noncanonical_effect_refused(self) -> None:
        for effect in ({"x": 1.5}, {"x": 2**60}, {"x": "é"}, {1: "a"}):
            with self.subTest(effect=effect), self.assertRaises(Refused) as ctx:
                request(effect=effect)
            self.assertEqual(ctx.exception.code, "effect_noncanonical")

    def test_sanitize_marks_controls(self) -> None:
        self.assertEqual(sanitize("a\x1b[2Jb⁦"), "a<U+001B>[2Jb<U+2066>")


class ScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.auth = ApprovalAuthority(self.clock)
        self.req = request()
        self.prompt = self.auth.open(self.req)
        self.device = cred("paired_device", {"observe", "approve"})

    def test_paired_device_without_manage_approves_once(self) -> None:
        self.assertNotIn("manage", self.device.scopes)
        scope = self.auth.grant(self.device, [self.req.request_id], ttl_seconds=60)
        out = self.auth.approve(self.device, scope.scope_id, self.req.request_id, self.req.digest)
        self.assertEqual(out["outcome"], "approved")
        self.assertEqual(out["digest"], self.req.digest)
        self.assertEqual(self.auth.state(self.req.request_id), "approved")
        self.assertIn(self.req.digest, self.prompt)

    def test_pairing_never_grants_manage(self) -> None:
        with self.assertRaises(Refused) as ctx:
            cred("paired_device", {"approve", "manage"})
        self.assertEqual(ctx.exception.code, "pairing_grants_no_manage")

    def test_agent_plugin_and_unscoped_tokens_refuse_approve(self) -> None:
        scope = self.auth.grant(self.device, [self.req.request_id], ttl_seconds=60)
        cases = {
            "agent": (cred("agent", {"observe", "control", "approve"}), "principal_not_approver"),
            "plugin": (cred("plugin", {"control", "approve"}), "principal_not_approver"),
            "paired_no_approve": (cred("paired_device", {"observe", "control"}),
                                  "approval_scope_required"),
            "operator_manage_only": (cred("operator_ui", {"observe", "control", "manage"}),
                                     "approval_scope_required"),
        }
        for name, (token, code) in cases.items():
            with self.subTest(name):
                with self.assertRaises(Refused) as ctx:
                    self.auth.approve(token, scope.scope_id, self.req.request_id, self.req.digest)
                self.assertEqual(ctx.exception.code, code)
                with self.assertRaises(Refused):
                    self.auth.grant(token, [self.req.request_id], ttl_seconds=60)
        self.assertEqual(self.auth.state(self.req.request_id), "pending")

    def test_scope_is_not_delegable(self) -> None:
        scope = self.auth.grant(self.device, [self.req.request_id], ttl_seconds=60)
        other = cred("paired_device", {"approve"}, token_id="ab" * 16)
        with self.assertRaises(Refused) as ctx:
            self.auth.approve(other, scope.scope_id, self.req.request_id, self.req.digest)
        self.assertEqual(ctx.exception.code, "scope_not_delegable")
        wire = scope.to_wire()
        self.assertIs(wire["delegable"], False)
        self.assertEqual(ApprovalScope.from_wire(wire), scope)
        wire["delegable"] = True
        with self.assertRaises(Refused) as ctx:
            ApprovalScope.from_wire(wire)
        self.assertEqual(ctx.exception.code, "scope_not_delegable")

    def test_digest_must_match_what_was_shown(self) -> None:
        scope = self.auth.grant(self.device, [self.req.request_id], ttl_seconds=60)
        with self.assertRaises(Refused) as ctx:
            self.auth.approve(self.device, scope.scope_id, self.req.request_id, "e" * 64)
        self.assertEqual(ctx.exception.code, "digest_mismatch")
        self.assertEqual(self.auth.state(self.req.request_id), "pending")

    def test_scope_expiry_is_bounded_by_request_expiry(self) -> None:
        scope = self.auth.grant(self.device, [self.req.request_id], ttl_seconds=10_000)
        self.assertEqual(scope.expires_at, self.req.expires_at)


class ScopeExpiryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.auth = ApprovalAuthority(self.clock)
        self.req = request()
        self.auth.open(self.req)
        self.device = cred("operator_ui", {"approve"})
        self.scope = self.auth.grant(self.device, [self.req.request_id], ttl_seconds=60)

    def approve(self) -> dict:
        return self.auth.approve(self.device, self.scope.scope_id, self.req.request_id,
                                 self.req.digest)

    def test_expired_scope_refuses(self) -> None:
        self.clock.now = self.scope.expires_at
        with self.assertRaises(Refused) as ctx:
            self.approve()
        self.assertEqual(ctx.exception.code, "scope_expired")
        self.assertEqual(self.auth.state(self.req.request_id), "pending")
        self.clock.now += 1000
        with self.assertRaises(Refused) as ctx:
            self.approve()
        self.assertEqual(ctx.exception.code, "scope_expired")
        self.assertEqual(self.auth.state(self.req.request_id), "expired")

    def test_not_yet_expired_accepts_once(self) -> None:
        self.clock.now = self.scope.expires_at - 1
        self.assertEqual(self.approve()["decided_at"], self.scope.expires_at - 1)

    def test_replay_after_use_refuses(self) -> None:
        self.approve()
        with self.assertRaises(Refused) as ctx:
            self.approve()
        self.assertEqual(ctx.exception.code, "scope_replay")
        with self.assertRaises(Refused) as ctx:
            self.auth.deny(self.device, self.scope.scope_id, self.req.request_id, self.req.digest)
        self.assertEqual(ctx.exception.code, "scope_replay")

    def test_first_answer_wins_across_scopes(self) -> None:
        other = cred("paired_device", {"approve"}, token_id="cd" * 16)
        second = self.auth.grant(other, [self.req.request_id], ttl_seconds=60)
        self.auth.deny(self.device, self.scope.scope_id, self.req.request_id, self.req.digest)
        with self.assertRaises(Refused) as ctx:
            self.auth.approve(other, second.scope_id, self.req.request_id, self.req.digest)
        self.assertEqual(ctx.exception.code, "request_answered")
        self.assertEqual(self.auth.state(self.req.request_id), "denied")

    def test_expired_request_cannot_be_opened_or_granted(self) -> None:
        with self.assertRaises(Refused):
            self.auth.open(request(expires_at=T0))
        late = request(expires_at=T0 + 5)
        self.auth.open(late)
        self.clock.now = T0 + 5
        with self.assertRaises(Refused) as ctx:
            self.auth.grant(self.device, [late.request_id], ttl_seconds=60)
        self.assertEqual(ctx.exception.code, "request_expired")
        self.assertEqual(self.auth.state(late.request_id), "expired")

    def test_scoped_grant_uses_each_binding_once(self) -> None:
        reqs = [request(effect={"n": i}) for i in range(3)]
        for r in reqs:
            self.auth.open(r)
        scope = self.auth.grant(self.device, [r.request_id for r in reqs], ttl_seconds=60)
        self.assertEqual(len(scope.bindings), 3)
        for r in reqs:
            self.auth.approve(self.device, scope.scope_id, r.request_id, r.digest)
        for r in reqs:
            with self.assertRaises(Refused) as ctx:
                self.auth.approve(self.device, scope.scope_id, r.request_id, r.digest)
            self.assertEqual(ctx.exception.code, "scope_replay")
        with self.assertRaises(Refused) as ctx:
            self.auth.approve(self.device, scope.scope_id, self.req.request_id, self.req.digest)
        self.assertEqual(ctx.exception.code, "scope_binding")

    def test_unknown_scope_refuses(self) -> None:
        with self.assertRaises(Refused) as ctx:
            self.auth.approve(self.device, "00" * 16, self.req.request_id, self.req.digest)
        self.assertEqual(ctx.exception.code, "scope_unknown")


if __name__ == "__main__":
    unittest.main()
