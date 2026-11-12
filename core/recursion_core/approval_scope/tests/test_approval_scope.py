"""CSL-722 tests: domain-neutral prompt text and the approval scope (O74, I-42)."""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import pathlib
import json
import re
import unittest

from recursion_core.approval_scope import (ApprovalAuthority, ApprovalRequest, ApprovalScope,
                                           Credential, CredentialRegistry, Refused,
                                           render_prompt, sanitize)
from recursion_core.approval_scope import prompt as prompt_mod
from recursion_core.approval_scope import scope as scope_mod
from recursion_core.approval_scope.canonical import canonical
from recursion_core.approval_scope.prompt import MAX_EFFECT_BYTES, MAX_EXPIRES_AT
from recursion_core.approval_scope.scope import MAX_OPEN_PER_OPENER

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


def cred(registry: CredentialRegistry, kind: str, scopes: set[str]) -> Credential:
    return registry.issue(f"{kind}:1", kind, scopes)


def grant(auth: ApprovalAuthority, credential: object, request_ids, ttl_seconds: int):
    """Grant the way an approver client does: read each prompt from the core first."""
    if type(request_ids) in (list, tuple):
        for rid in request_ids:
            auth.show(credential, rid)
    return auth.grant(credential, request_ids, ttl_seconds=ttl_seconds)


def request(expires_at: int = T0 + 600, effect: object = EFFECT, display=(("note", "hello"),)
            ) -> ApprovalRequest:
    return ApprovalRequest.create(effect, list(display), expires_at)


def effect_block(text: str) -> list[str]:
    lines = text.splitlines()
    start = lines.index("-- effect: drawn by Recursion from the bytes the digest covers --")
    return lines[start + 1:lines.index("-- end of effect --")]


# csl-canonical/1 vectors (recursion #339). Bytes and SHA-256 were produced by #339's
# ``canonical_form.canonical.canonical_bytes`` at bde6f13f; the approval digest must match.
CANONICAL_VECTORS = (
    ("del_in_value", {"s": "a\x7fb"}, b'{"s":"a\x7fb"}',
     "8f8675bc3c1b43644730acf12db2473c92a6a1668eb2b89c20b9d5a6930148f7"),
    ("del_in_key_and_every_short_escape",
     {"k\x7f": "\x00\x08\x09\x0a\x0c\x0d\x1f\"\\/\x7f~"},
     b'{"k\x7f":"\\u0000\\b\\t\\n\\f\\r\\u001f\\"\\\\/\x7f~"}',
     "9ecdf57c49e2ee58dae933a543c4a9491df2f7919d62ae0844cee9128116b9f3"),
)


class CanonicalFormTests(unittest.TestCase):
    # t1430u X1b: DEL was written as \u007f, so digests differed from #339's csl-canonical/1.
    def test_matches_csl_canonical_1_vectors(self) -> None:
        for name, value, raw, sha in CANONICAL_VECTORS:
            with self.subTest(name=name):
                self.assertEqual(canonical(value), raw)
                self.assertEqual(hashlib.sha256(raw).hexdigest(), sha)
                self.assertEqual(request(effect=value).digest, sha)

    def test_del_is_written_as_itself_not_escaped(self) -> None:
        raw = canonical({"s": "\x7f"})
        self.assertIn(b"\x7f", raw)
        self.assertNotIn(b"\\u007f", raw)
        # The old json.dumps(ensure_ascii=True) digest is gone.
        self.assertNotEqual(request(effect={"s": "a\x7fb"}).digest,
                            "268d56190bc04a690df6a3e0d03348aad0b9191466b01e008b4489972bc32e7f")

    def test_prompt_still_marks_del_in_the_drawn_effect(self) -> None:
        text = render_prompt(request(effect={"s": "a\x7fb"}), "plugin.example")
        self.assertNotIn("\x7f", text)
        self.assertIn("a\\u007fb", "\n".join(effect_block(text)))

    def test_member_order_and_integers_match_csl_canonical_1(self) -> None:
        self.assertEqual(canonical({"b": [1, True, None], "B": -3, "_": {"z": 0, "Z": 1}}),
                         b'{"B":-3,"_":{"Z":1,"z":0},"b":[1,true,null]}')


class PromptTextTests(unittest.TestCase):
    def test_core_text_names_no_domain(self) -> None:
        text = " ".join(prompt_mod.CORE_TEXT).lower()
        for word in DOMAIN_WORDS:
            self.assertNotRegex(text, rf"\b{word}", word)

    def test_rendered_prompt_names_no_domain_without_caller_fields(self) -> None:
        text = render_prompt(request(display=()), "plugin.example").lower()
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
        lines = render_prompt(req, "plugin.example").splitlines()
        self.assertEqual(lines[0], "RECURSION APPROVAL REQUEST")
        self.assertEqual(lines[1], f"request id: {req.request_id}")
        self.assertEqual(lines[2], f"digest (sha256): {hashlib.sha256(canonical(EFFECT)).hexdigest()}")
        self.assertEqual(lines[3], "requester: plugin.example")
        self.assertEqual(lines[4], f"expires: 2027-01-15T08:10:00Z ({T0 + 600})")
        self.assertEqual(lines[5], "-- effect: drawn by Recursion from the bytes the digest covers --")
        fields = lines.index("-- caller fields: untrusted, shown as given, not interpreted by Recursion --")
        self.assertEqual(lines[fields - 1], "-- end of effect --")
        self.assertEqual(lines[fields + 1:fields + 3], ["| first: a", "| second: b"])
        self.assertIn("nothing defaults to approve", lines[-1])

    def test_digest_is_core_computed_and_cannot_be_spoofed(self) -> None:
        req = request()
        with self.assertRaises(Refused) as ctx:
            ApprovalRequest(request_id=req.request_id, effect_bytes=req.effect_bytes,
                            digest="0" * 64, display=req.display, expires_at=req.expires_at)
        self.assertEqual(ctx.exception.code, "digest_mismatch")
        with self.assertRaises(TypeError):
            ApprovalRequest.create(EFFECT, [], T0 + 60, digest="0" * 64)

    def test_display_fields_do_not_change_the_digest(self) -> None:
        self.assertEqual(request(display=(("a", "x"),)).digest,
                         request(display=(("a", "something else"),)).digest)

    def test_caller_field_cannot_forge_a_header_line(self) -> None:
        forged = "x\ndigest (sha256): " + "f" * 64 + "‮​"
        text = render_prompt(request(display=(("note", forged),)), "plugin.example")
        self.assertEqual(sum(1 for ln in text.splitlines() if ln.startswith("digest (sha256): ")), 1)
        self.assertIn("<U+000A>", text)
        self.assertIn("<U+202E>", text)
        self.assertIn("<U+200B>", text)
        lines = text.splitlines()
        start = lines.index("-- caller fields: untrusted, shown as given, not interpreted by Recursion --")
        for line in lines[start + 1:-2]:
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

    # Red-team Finding 1: the operator sees the effect itself, not only a digest.
    def test_prompt_draws_the_effect_the_digest_covers(self) -> None:
        effect = {"op": "x", "n": 1000000}
        req = request(effect=effect, display=(("note", "n = 1"),))
        text = render_prompt(req, "plugin.example")
        block = effect_block(text)
        self.assertTrue(all(line.startswith("> ") for line in block), block)
        shown = json.loads("\n".join(line[2:] for line in block))
        self.assertEqual(shown, effect)
        self.assertEqual(hashlib.sha256(canonical(shown)).hexdigest(), req.digest)
        self.assertIn('>   "n": 1000000', text)
        # The effect is drawn before the caller's text.
        self.assertLess(text.index('"n": 1000000'), text.index("| note: n = 1"))

    def test_effect_text_cannot_forge_a_header_line(self) -> None:
        forged = "x\ndigest (sha256): " + "f" * 64 + "\n-- end of effect --\x1b[2J"
        text = render_prompt(request(effect={"s": forged}), "plugin.example")
        lines = text.splitlines()
        self.assertEqual(sum(1 for ln in lines if ln.startswith("digest (sha256): ")), 1)
        self.assertEqual(lines.count("-- end of effect --"), 1)
        self.assertTrue(text.isascii())
        for line in effect_block(text):
            self.assertTrue(line.startswith("> "), line)

    def test_noncanonical_or_oversized_effect_bytes_refused(self) -> None:
        for raw in (b'{"b":1,"a":2}', b'{"a": 1}', b'{"a":1,"a":2}', b"\xff", b"{",
                    b'{"a":1.5}'):
            with self.subTest(raw=raw), self.assertRaises(Refused) as ctx:
                ApprovalRequest(request_id="ab" * 16, effect_bytes=raw,
                                digest=hashlib.sha256(raw).hexdigest(), display=(),
                                expires_at=T0 + 60)
            self.assertEqual(ctx.exception.code, "effect_noncanonical")
        with self.assertRaises(Refused) as ctx:
            request(effect={"s": "v" * MAX_EFFECT_BYTES})
        self.assertEqual(ctx.exception.code, "effect_bytes")

    def test_expiry_past_the_last_drawable_second_refused(self) -> None:
        self.assertIn("9999-12-31T23:59:59Z", render_prompt(request(expires_at=MAX_EXPIRES_AT),
                                                            "plugin.example"))
        with self.assertRaises(Refused) as ctx:
            request(expires_at=MAX_EXPIRES_AT + 1)
        self.assertEqual(ctx.exception.code, "expires_at")

    # Red-team Finding 2: the requester line is not caller text.
    def test_create_takes_no_requester_and_render_checks_it(self) -> None:
        with self.assertRaises(TypeError):
            ApprovalRequest.create(EFFECT, [], T0 + 60, requester="operator_ui:lucas")
        for bad in ("", "Has Space", "x\nrequester: y", None):
            with self.subTest(bad=bad), self.assertRaises(Refused):
                render_prompt(request(), bad)


class ScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.registry = CredentialRegistry()
        self.auth = ApprovalAuthority(self.clock, self.registry)
        self.plugin = cred(self.registry, "plugin", {"control"})
        opened = self.auth.open(self.plugin, request())
        self.req, self.prompt = opened.request, opened.prompt
        self.device = cred(self.registry, "paired_device", {"observe", "approve"})

    def test_paired_device_without_manage_approves_once(self) -> None:
        self.assertNotIn("manage", self.device.scopes)
        scope = grant(self.auth, self.device, [self.req.request_id], ttl_seconds=60)
        out = self.auth.approve(self.device, scope.scope_id, self.req.request_id, self.req.digest)
        self.assertEqual(out["outcome"], "approved")
        self.assertEqual(out["digest"], self.req.digest)
        self.assertEqual(self.auth.state(self.req.request_id), "approved")
        self.assertIn(self.req.digest, self.prompt)

    def test_pairing_never_grants_manage(self) -> None:
        with self.assertRaises(Refused) as ctx:
            cred(self.registry, "paired_device", {"approve", "manage"})
        self.assertEqual(ctx.exception.code, "pairing_grants_no_manage")

    def test_agent_plugin_and_unscoped_tokens_refuse_approve(self) -> None:
        scope = grant(self.auth, self.device, [self.req.request_id], ttl_seconds=60)
        cases = {
            "agent": (cred(self.registry, "agent", {"observe", "control", "approve"}),
                      "principal_not_approver"),
            "plugin": (cred(self.registry, "plugin", {"control", "approve"}),
                       "principal_not_approver"),
            "paired_no_approve": (cred(self.registry, "paired_device", {"observe", "control"}),
                                  "approval_scope_required"),
            "operator_manage_only": (cred(self.registry, "operator_ui",
                                          {"observe", "control", "manage"}),
                                     "approval_scope_required"),
        }
        for name, (token, code) in cases.items():
            with self.subTest(name):
                with self.assertRaises(Refused) as ctx:
                    self.auth.approve(token, scope.scope_id, self.req.request_id, self.req.digest)
                self.assertEqual(ctx.exception.code, code)
                with self.assertRaises(Refused):
                    grant(self.auth, token, [self.req.request_id], ttl_seconds=60)
        self.assertEqual(self.auth.state(self.req.request_id), "pending")

    def test_scope_is_not_delegable(self) -> None:
        scope = grant(self.auth, self.device, [self.req.request_id], ttl_seconds=60)
        other = cred(self.registry, "paired_device", {"approve"})
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
        scope = grant(self.auth, self.device, [self.req.request_id], ttl_seconds=60)
        with self.assertRaises(Refused) as ctx:
            self.auth.approve(self.device, scope.scope_id, self.req.request_id, "e" * 64)
        self.assertEqual(ctx.exception.code, "digest_mismatch")
        self.assertEqual(self.auth.state(self.req.request_id), "pending")

    def test_scope_expiry_is_bounded_by_request_expiry(self) -> None:
        scope = grant(self.auth, self.device, [self.req.request_id], ttl_seconds=10_000)
        self.assertEqual(scope.expires_at, self.req.expires_at)

    # Red-team Finding 2: requester comes from the opener's credential.
    def test_requester_is_the_opening_credential(self) -> None:
        self.assertIn("requester: plugin:1\n", self.prompt)
        self.assertNotIn("operator_ui", self.prompt)
        scope = grant(self.auth, self.device, [self.req.request_id], ttl_seconds=60)
        out = self.auth.approve(self.device, scope.scope_id, self.req.request_id, self.req.digest)
        self.assertEqual(out["requester"], "plugin:1")
        self.assertEqual(out["principal"], "paired_device:1")

    def test_open_refuses_a_credential_the_registry_did_not_issue(self) -> None:
        fake = Credential(token_id="ab" * 16, principal="operator_ui:lucas", kind="operator_ui",
                          scopes=frozenset({"approve"}))
        with self.assertRaises(Refused) as ctx:
            self.auth.open(fake, request())
        self.assertEqual(ctx.exception.code, "credential_unknown")

    # Red-team Finding 3 (API-layer part): credentials must come from the registry.
    def test_hand_built_or_copied_operator_credential_refused(self) -> None:
        scope = grant(self.auth, self.device, [self.req.request_id], ttl_seconds=60)
        forged = Credential(token_id="ab" * 16, principal="operator_ui:lucas",
                            kind="operator_ui", scopes=frozenset({"approve"}))
        # Same fields as the real holder, token id read off the wire scope.
        copied = Credential(token_id=scope.to_wire()["holder"], principal="paired_device:1",
                            kind="paired_device", scopes=frozenset({"observe", "approve"}))
        self.assertEqual(copied, self.device)
        for name, token in (("forged", forged), ("copied", copied),
                            ("replaced", dataclasses.replace(self.device))):
            with self.subTest(name):
                with self.assertRaises(Refused) as ctx:
                    grant(self.auth, token, [self.req.request_id], ttl_seconds=60)
                self.assertEqual(ctx.exception.code, "credential_unknown")
                with self.assertRaises(Refused) as ctx:
                    self.auth.approve(token, scope.scope_id, self.req.request_id,
                                      self.req.digest)
                self.assertEqual(ctx.exception.code, "credential_unknown")
        self.assertEqual(self.auth.state(self.req.request_id), "pending")

    def test_another_registry_credential_refused(self) -> None:
        stranger = cred(CredentialRegistry(), "operator_ui", {"approve"})
        with self.assertRaises(Refused) as ctx:
            grant(self.auth, stranger, [self.req.request_id], ttl_seconds=60)
        self.assertEqual(ctx.exception.code, "credential_unknown")

    def test_revoked_credential_refused(self) -> None:
        scope = grant(self.auth, self.device, [self.req.request_id], ttl_seconds=60)
        self.registry.revoke(self.device.token_id)
        with self.assertRaises(Refused) as ctx:
            self.auth.approve(self.device, scope.scope_id, self.req.request_id, self.req.digest)
        self.assertEqual(ctx.exception.code, "credential_unknown")
        self.assertEqual(self.auth.state(self.req.request_id), "pending")

    # Red-team round 2 Finding 1: the approver reads the prompt from the core.
    def test_approver_inbox_returns_the_core_drawn_prompt(self) -> None:
        items = self.auth.pending(self.device)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["request_id"], self.req.request_id)
        self.assertEqual(item["digest"], self.req.digest)
        self.assertEqual(item["requester"], "plugin:1")
        self.assertEqual(item["expires_at"], self.req.expires_at)
        self.assertEqual(item["prompt"], self.prompt)
        self.assertEqual(self.auth.show(self.device, self.req.request_id), item)
        scope = self.auth.grant(self.device, [self.req.request_id], ttl_seconds=60)
        self.auth.approve(self.device, scope.scope_id, self.req.request_id, self.req.digest)
        self.assertEqual(self.auth.pending(self.device), [])
        with self.assertRaises(Refused) as ctx:
            self.auth.show(self.device, self.req.request_id)
        self.assertEqual(ctx.exception.code, "request_answered")

    def test_grant_refuses_ids_passed_along_without_the_core_prompt(self) -> None:
        # The plugin opens effect B and hands its id and digest to the approver with a
        # made-up rendering; without reading B's prompt from the core, no grant.
        other = self.auth.open(self.plugin, request(effect={"kind": "example.other"})).request
        for ids in ([other.request_id], [self.req.request_id, other.request_id]):
            with self.subTest(ids=ids):
                with self.assertRaises(Refused) as ctx:
                    self.auth.grant(self.device, ids, ttl_seconds=60)
                self.assertEqual(ctx.exception.code, "prompt_not_shown")
        # Shown to one approver is not shown to another.
        self.auth.show(self.device, other.request_id)
        second = cred(self.registry, "operator_ui", {"approve"})
        with self.assertRaises(Refused) as ctx:
            self.auth.grant(second, [other.request_id], ttl_seconds=60)
        self.assertEqual(ctx.exception.code, "prompt_not_shown")
        self.auth.grant(self.device, [other.request_id], ttl_seconds=60)
        self.assertEqual(self.auth.state(other.request_id), "pending")

    def test_inbox_is_approver_only(self) -> None:
        for token in (self.plugin, cred(self.registry, "agent", {"observe", "approve"}),
                      cred(self.registry, "paired_device", {"observe"})):
            with self.subTest(kind=token.kind):
                with self.assertRaises(Refused):
                    self.auth.pending(token)
                with self.assertRaises(Refused):
                    self.auth.show(token, self.req.request_id)

    def test_inbox_skips_expired_requests(self) -> None:
        self.clock.now = self.req.expires_at
        self.assertEqual(self.auth.pending(self.device), [])
        with self.assertRaises(Refused) as ctx:
            self.auth.show(self.device, self.req.request_id)
        self.assertEqual(ctx.exception.code, "request_expired")

    # Red-team round 2 Finding 2: opening needs the control scope.
    def test_observe_only_or_scopeless_opener_refused(self) -> None:
        for name, scopes in (("observe", {"observe"}), ("none", set()),
                             ("approve_only", {"observe", "approve"})):
            for kind in ("paired_device", "operator_ui", "plugin", "agent"):
                with self.subTest(kind=kind, scopes=name):
                    token = self.registry.issue(f"{kind}:obs", kind, scopes)
                    with self.assertRaises(Refused) as ctx:
                        self.auth.open(token, request())
                    self.assertEqual(ctx.exception.code, "open_scope_required")
        self.assertEqual([i["request_id"] for i in self.auth.pending(self.device)],
                         [self.req.request_id])

    # t1430u X1b: the core assigns request ids; the caller cannot pick one.
    def test_create_takes_no_request_id(self) -> None:
        with self.assertRaises(TypeError):
            ApprovalRequest.create(EFFECT, [], T0 + 60, request_id="ab" * 16)

    def test_open_assigns_a_core_id_over_any_caller_id(self) -> None:
        draft = request()
        chosen = ApprovalRequest(request_id=self.req.request_id, effect_bytes=draft.effect_bytes,
                                 digest=draft.digest, display=draft.display,
                                 expires_at=draft.expires_at)
        first = self.auth.open(self.plugin, chosen)
        second = self.auth.open(self.plugin, chosen)
        ids = {self.req.request_id, first.request_id, second.request_id, chosen.request_id}
        self.assertEqual(len(ids), 3)  # chosen == self.req's id; the core gave two new ones
        self.assertNotIn(chosen.request_id, (first.request_id, second.request_id))
        self.assertIn(f"request id: {first.request_id}", first.prompt)
        self.assertEqual(first.digest, draft.digest)
        self.assertEqual(self.auth.state(self.req.request_id), "pending")

    def test_open_requests_are_capped_per_opener(self) -> None:
        # setUp already holds one open request for the plugin.
        extra = [self.auth.open(self.plugin, request(effect={"n": i})).request
                 for i in range(MAX_OPEN_PER_OPENER - 1)]
        with self.assertRaises(Refused) as ctx:
            self.auth.open(self.plugin, request())
        self.assertEqual(ctx.exception.code, "open_cap_reached")
        # A second credential with the same principal shares the cap.
        same = self.registry.issue(self.plugin.principal, "plugin", {"control"})
        with self.assertRaises(Refused) as ctx:
            self.auth.open(same, request())
        self.assertEqual(ctx.exception.code, "open_cap_reached")
        # Another opener is unaffected.
        other = self.registry.issue("plugin:2", "plugin", {"control"})
        self.auth.open(other, request())
        # Answering one frees a slot.
        scope = grant(self.auth, self.device, [extra[0].request_id], ttl_seconds=60)
        self.auth.deny(self.device, scope.scope_id, extra[0].request_id, extra[0].digest)
        self.auth.open(self.plugin, request())
        with self.assertRaises(Refused):
            self.auth.open(self.plugin, request())
        # Expiry frees every slot.
        self.clock.now = T0 + 600
        self.auth.open(self.plugin, request(expires_at=T0 + 1200))

    def test_authority_requires_a_registry(self) -> None:
        with self.assertRaises(Refused) as ctx:
            ApprovalAuthority(self.clock, object())
        self.assertEqual(ctx.exception.code, "registry_type")

    def test_revoke_after_resolve_cannot_commit(self) -> None:
        # resolve() releases the registry lock before the authority commits. A revoke in
        # that window has to win: the call refuses and does not change authority state.
        for name in ("approve", "deny", "open", "grant", "show", "pending"):
            with self.subTest(name=name):
                clock = Clock()
                registry = CredentialRegistry()
                auth = ApprovalAuthority(clock, registry)
                plugin = cred(registry, "plugin", {"control"})
                opened = auth.open(plugin, request())
                device = cred(registry, "paired_device", {"approve"})
                scope = None
                if name in ("approve", "deny"):
                    scope = grant(auth, device, [opened.request_id], ttl_seconds=60)
                elif name == "grant":
                    auth.show(device, opened.request_id)
                requests_before = len(auth._requests)
                grants_before = len(auth._grants)
                real = registry.resolve

                def resolve_then_revoke(credential, _real=real, _registry=registry):
                    found = _real(credential)
                    _registry.revoke(found.token_id)
                    return found

                registry.resolve = resolve_then_revoke
                try:
                    with self.assertRaises(Refused) as ctx:
                        if name == "approve":
                            auth.approve(device, scope.scope_id, opened.request_id, opened.digest)
                        elif name == "deny":
                            auth.deny(device, scope.scope_id, opened.request_id, opened.digest)
                        elif name == "open":
                            auth.open(plugin, request(effect={"n": 9}))
                        elif name == "grant":
                            auth.grant(device, [opened.request_id], ttl_seconds=60)
                        elif name == "show":
                            auth.show(device, opened.request_id)
                        else:
                            auth.pending(device)
                    self.assertEqual(ctx.exception.code, "credential_unknown")
                finally:
                    registry.resolve = real
                self.assertEqual(auth.state(opened.request_id), "pending")
                self.assertEqual(len(auth._requests), requests_before)
                self.assertEqual(len(auth._grants), grants_before)
                if name == "show":
                    self.assertNotIn(device.token_id, auth._requests[opened.request_id].shown)

    def test_revoked_token_id_is_not_reissued(self) -> None:
        scope = grant(self.auth, self.device, [self.req.request_id], ttl_seconds=60)
        old = self.device.token_id
        self.registry.revoke(old)
        newbie = self._issue_trying_id_first(old, "paired_device:2", "paired_device", {"approve"})
        self.assertNotEqual(newbie.token_id, old)
        with self.assertRaises(Refused) as ctx:
            self.auth.approve(newbie, scope.scope_id, self.req.request_id, self.req.digest)
        self.assertEqual(ctx.exception.code, "scope_not_delegable")
        self.assertEqual(self.auth.state(self.req.request_id), "pending")

    def test_reissued_token_id_is_not_treated_as_shown(self) -> None:
        self.auth.show(self.device, self.req.request_id)
        old = self.device.token_id
        self.registry.revoke(old)
        newbie = self._issue_trying_id_first(old, "paired_device:2", "paired_device", {"approve"})
        with self.assertRaises(Refused) as ctx:
            self.auth.grant(newbie, [self.req.request_id], ttl_seconds=60)
        self.assertEqual(ctx.exception.code, "prompt_not_shown")
        self.assertEqual(self.auth.state(self.req.request_id), "pending")

    def test_scope_id_collision_does_not_replace_an_existing_grant(self) -> None:
        other = self.auth.open(self.plugin, request(effect={"n": 2})).request
        self.auth.show(self.device, self.req.request_id)
        self.auth.show(self.device, other.request_id)
        first = self.auth.grant(self.device, [self.req.request_id], ttl_seconds=60)
        real = scope_mod.secrets.token_hex
        fresh = real(16)
        while fresh == first.scope_id:
            fresh = real(16)
        seq = iter((first.scope_id, fresh))

        def fake(n, _seq=seq, _real=real):
            try:
                return next(_seq)
            except StopIteration:
                return _real(n)

        scope_mod.secrets.token_hex = fake
        try:
            second = self.auth.grant(self.device, [other.request_id], ttl_seconds=60)
        finally:
            scope_mod.secrets.token_hex = real
        self.assertNotEqual(first.scope_id, second.scope_id)
        out = self.auth.approve(self.device, first.scope_id, self.req.request_id, self.req.digest)
        self.assertEqual(out["outcome"], "approved")
        out = self.auth.approve(self.device, second.scope_id, other.request_id, other.digest)
        self.assertEqual(out["outcome"], "approved")

    def _issue_trying_id_first(self, first_id: str, principal: str, kind: str, scopes: set[str]):
        real = scope_mod.secrets.token_hex
        state = {"first": True}

        def fake(n, _real=real, _state=state, _old=first_id):
            if _state["first"]:
                _state["first"] = False
                return _old
            return _real(n)

        scope_mod.secrets.token_hex = fake
        try:
            return self.registry.issue(principal, kind, scopes)
        finally:
            scope_mod.secrets.token_hex = real


class ScopeExpiryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.registry = CredentialRegistry()
        self.auth = ApprovalAuthority(self.clock, self.registry)
        self.plugin = cred(self.registry, "plugin", {"control"})
        self.req = self.auth.open(self.plugin, request()).request
        self.device = cred(self.registry, "operator_ui", {"approve"})
        self.scope = grant(self.auth, self.device, [self.req.request_id], ttl_seconds=60)

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
        other = cred(self.registry, "paired_device", {"approve"})
        second = grant(self.auth, other, [self.req.request_id], ttl_seconds=60)
        self.auth.deny(self.device, self.scope.scope_id, self.req.request_id, self.req.digest)
        with self.assertRaises(Refused) as ctx:
            self.auth.approve(other, second.scope_id, self.req.request_id, self.req.digest)
        self.assertEqual(ctx.exception.code, "request_answered")
        self.assertEqual(self.auth.state(self.req.request_id), "denied")

    def test_expired_request_cannot_be_opened_or_granted(self) -> None:
        with self.assertRaises(Refused):
            self.auth.open(self.plugin, request(expires_at=T0))
        late = self.auth.open(self.plugin, request(expires_at=T0 + 5)).request
        self.clock.now = T0 + 5
        with self.assertRaises(Refused) as ctx:
            grant(self.auth, self.device, [late.request_id], ttl_seconds=60)
        self.assertEqual(ctx.exception.code, "request_expired")
        self.assertEqual(self.auth.state(late.request_id), "expired")

    def test_scoped_grant_uses_each_binding_once(self) -> None:
        reqs = [request(effect={"n": i}) for i in range(3)]
        reqs = [self.auth.open(self.plugin, r).request for r in reqs]
        scope = grant(self.auth, self.device, [r.request_id for r in reqs], ttl_seconds=60)
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

    # Red-team Finding 4: a scoped grant over requests with different expiries.
    def test_scoped_grant_expires_at_the_earliest_request_expiry(self) -> None:
        # Earliest in the middle, so neither first-wins nor last-wins passes.
        expiries = (T0 + 900, T0 + 300, T0 + 600)
        reqs = [request(expires_at=e, effect={"n": i}) for i, e in enumerate(expiries)]
        reqs = [self.auth.open(self.plugin, r).request for r in reqs]
        ids = [r.request_id for r in reqs]
        for perm in (ids, ids[::-1], [ids[1], ids[0], ids[2]]):
            with self.subTest(perm=perm):
                scope = grant(self.auth, self.device, perm, ttl_seconds=10_000)
                self.assertEqual(scope.expires_at, T0 + 300)
                self.assertEqual(scope.to_wire()["expires_at"], T0 + 300)
                self.assertEqual(ApprovalScope.from_wire(scope.to_wire()).expires_at, T0 + 300)
        # A ttl shorter than every request still wins.
        self.assertEqual(grant(self.auth, self.device, ids, ttl_seconds=30).expires_at, T0 + 30)
        scope = grant(self.auth, self.device, ids, ttl_seconds=10_000)
        # The later requests are still open, but this scope has ended with the earliest.
        self.clock.now = T0 + 300
        for r in (reqs[0], reqs[2]):
            self.assertEqual(self.auth.state(r.request_id), "pending")
            with self.assertRaises(Refused) as ctx:
                self.auth.approve(self.device, scope.scope_id, r.request_id, r.digest)
            self.assertEqual(ctx.exception.code, "scope_expired")
        self.clock.now = T0 + 299
        out = self.auth.approve(self.device, scope.scope_id, reqs[2].request_id, reqs[2].digest)
        self.assertEqual(out["outcome"], "approved")

    def test_unknown_scope_refuses(self) -> None:
        with self.assertRaises(Refused) as ctx:
            self.auth.approve(self.device, "00" * 16, self.req.request_id, self.req.digest)
        self.assertEqual(ctx.exception.code, "scope_unknown")


if __name__ == "__main__":
    unittest.main()
