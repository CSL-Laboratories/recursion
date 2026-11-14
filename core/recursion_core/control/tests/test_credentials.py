"""Credentials: the ceiling per principal kind, daemon-minted tokens kept hashed, revocation, the failed-check limit."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import unittest
from unittest import mock

from recursion_core.control import credentials as creds
from recursion_core.control.credentials import (CEILING, CredentialRefused, CredentialStore, CredentialStoreError,
                                                FailedAuthLimit, check_grant)

POSIX = os.name != "nt" and hasattr(os, "getuid") and hasattr(os, "O_NOFOLLOW")
NEEDS_POSIX = unittest.skipUnless(POSIX, "the credential store needs uids and O_NOFOLLOW (POSIX); it refuses elsewhere")


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class CeilingTest(unittest.TestCase):
    def test_the_ceiling_per_kind(self) -> None:
        self.assertEqual({k: sorted(v) for k, v in CEILING.items()}, {
            "owner": ["approve", "manage", "read", "run"],
            "internal": ["approve", "manage", "read", "run"],
            "device": ["approve", "read", "run"],
            "plugin": ["read", "run"],
            "session": ["read", "run"],
            "agent": ["read", "run"],
        })
        self.assertEqual(creds.ceiling("nobody"), frozenset())
        self.assertEqual(creds.ceiling(None), frozenset())

    def test_pairing_and_agent_credentials_never_hold_manage(self) -> None:
        for kind in ("device", "agent", "plugin", "session"):
            with self.subTest(kind=kind):
                with self.assertRaises(CredentialRefused) as caught:
                    check_grant(kind, {"read", "manage"})
                self.assertEqual((caught.exception.code, caught.exception.detail),
                                 ("scope_refused", "manage_never_granted"))

    def test_agent_plugin_and_session_credentials_never_hold_approve(self) -> None:
        for kind in ("agent", "plugin", "session"):
            with self.subTest(kind=kind):
                with self.assertRaises(CredentialRefused) as caught:
                    check_grant(kind, ["approve"])
                self.assertEqual(caught.exception.detail, "approve_never_granted")
        self.assertEqual(check_grant("device", ["read", "run", "approve"]), frozenset({"read", "run", "approve"}))

    def test_owner_and_internal_are_never_tokens_and_bad_grants_are_refused(self) -> None:
        cases = [("owner", ["read"], "kind_not_mintable"), ("internal", ["read"], "kind_not_mintable"),
                 ("operator_ui", ["read"], "kind_unknown"), (None, ["read"], "kind_unknown"),
                 ("device", [], "scopes_invalid"), ("device", "read", "scopes_invalid"),
                 ("device", ["read", "read"], "scopes_invalid"), ("device", ["observe"], "scopes_invalid"),
                 ("device", [1], "scopes_invalid"), ("device", None, "scopes_invalid")]
        for kind, scopes, detail in cases:
            with self.subTest(kind=kind, scopes=scopes):
                with self.assertRaises(CredentialRefused) as caught:
                    check_grant(kind, scopes)
                self.assertEqual(caught.exception.detail, detail)


@NEEDS_POSIX
class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="creds-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.dir = os.path.join(self.base, "state")
        self.path = os.path.join(self.dir, "credentials.json")
        self.wall = Clock()
        self.events: list[tuple[str, dict]] = []

    def store(self, **kwargs) -> CredentialStore:
        kwargs.setdefault("emit", lambda kind, payload: self.events.append((kind, payload)))
        kwargs.setdefault("wall", self.wall)
        return CredentialStore(self.path, **kwargs)

    def test_a_minted_token_verifies_and_only_its_digest_is_stored(self) -> None:
        store = self.store()
        token, cred = store.mint("device", "phone-1", ["read", "run"])
        self.assertRegex(token, r"^rct1_[A-Za-z0-9_-]{43}$")
        self.assertRegex(cred.token_id, r"^ctk-[A-Za-z0-9_-]{1,60}$")
        session_token, session_cred = store.mint("session", "sess-7", ["run"])
        self.assertRegex(session_token, r"^rst1_[A-Za-z0-9_-]{43}$")   # session.token.v1's form
        self.assertRegex(session_cred.token_id, r"^stk-[A-Za-z0-9_-]{1,60}$")
        self.assertEqual(stat.S_IMODE(os.lstat(self.dir).st_mode), 0o700)
        st = os.lstat(self.path)
        self.assertEqual((stat.S_IMODE(st.st_mode), st.st_uid), (0o600, os.getuid()))
        with open(self.path, encoding="ascii") as handle:
            text = handle.read()
        for secret in (token, session_token):
            self.assertNotIn(secret, text)
            self.assertNotIn(secret[5:], text)
            self.assertIn(hashlib.sha256(secret.encode()).hexdigest(), text)
        got = store.verify(token, peer="p")
        self.assertEqual((got.token_id, got.kind, got.id, got.scopes), (cred.token_id, "device", "phone-1",
                                                                         frozenset({"read", "run"})))
        self.assertEqual(store.held("device", "phone-1"), frozenset({"read", "run"}))
        self.assertEqual(self.store().verify(session_token, peer="p").id, "sess-7")   # read back from the file
        self.assertEqual(os.listdir(self.dir), ["credentials.json"])   # no temporary file left behind

    def test_a_revoked_token_is_refused_at_once_and_after_a_restart(self) -> None:
        store = self.store()
        token, cred = store.mint("plugin", "plugin.alpha", ["read", "run"])
        self.assertEqual(store.verify(token, peer="p").token_id, cred.token_id)
        self.wall.now += 5
        revoked = store.revoke(cred.token_id)
        self.assertEqual(revoked.revoked_at, int(self.wall.now))
        with self.assertRaises(CredentialRefused) as caught:
            store.verify(token, peer="p")
        self.assertEqual(caught.exception.detail, "token_revoked")
        self.assertEqual(store.held("plugin", "plugin.alpha"), frozenset())
        self.wall.now += 5
        self.assertEqual(store.revoke(cred.token_id).revoked_at, revoked.revoked_at)   # the first stands
        with self.assertRaises(CredentialRefused):
            self.store().verify(token, peer="q")
        with self.assertRaises(CredentialRefused) as caught:
            store.revoke("ctk-nosuchgrant")
        self.assertEqual(caught.exception.detail, "token_unknown")

    def test_a_revocation_whose_write_fails_still_refuses_and_says_so(self) -> None:
        store = self.store()
        token, cred = store.mint("agent", "mcp", ["read"])
        with mock.patch.object(store, "_write", side_effect=CredentialStoreError("disk full")):
            with self.assertRaises(CredentialStoreError):
                store.revoke(cred.token_id)
        with self.assertRaises(CredentialRefused) as caught:
            store.verify(token, peer="p")
        self.assertEqual(caught.exception.detail, "token_revoked")
        self.assertEqual([kind for kind, _ in self.events], ["credential.issued"])   # no notice of a revocation not written

    def test_a_mint_whose_write_fails_leaves_no_grant(self) -> None:
        store = self.store()
        with mock.patch.object(store, "_write", side_effect=CredentialStoreError("disk full")):
            with self.assertRaises(CredentialStoreError):
                store.mint("device", "phone-1", ["read"])
        self.assertEqual(store.grants(), [])
        self.assertEqual(store.held("device", "phone-1"), frozenset())
        self.assertEqual(self.events, [])
        self.assertFalse(os.path.exists(self.path))

    def test_a_real_write_failure_is_typed_and_leaves_no_temporary_file(self) -> None:
        store = self.store()
        with mock.patch.object(creds.os, "replace", side_effect=OSError("EXDEV")):
            with self.assertRaises(CredentialStoreError):
                store.mint("device", "phone-1", ["read"])
        self.assertEqual(os.listdir(self.dir), [])

    def test_mint_refusals_write_nothing(self) -> None:
        store = self.store()
        for args, kwargs, detail in (
                (("device", "phone-1", ["manage"]), {}, "manage_never_granted"),
                (("agent", "mcp", ["read", "manage"]), {}, "manage_never_granted"),
                (("plugin", "p", ["approve"]), {}, "approve_never_granted"),
                (("device", "bad\nid", ["read"]), {}, "principal_id_invalid"),
                (("device", "", ["read"]), {}, "principal_id_invalid"),
                (("device", "x" * 129, ["read"]), {}, "principal_id_invalid"),
                (("device", "phone-1", ["read"]), {"expires_at": int(self.wall.now)}, "expiry_invalid"),
                (("device", "phone-1", ["read"]), {"expires_at": self.wall.now + 60.5}, "expiry_invalid"),
                (("owner", "local", ["read"]), {}, "kind_not_mintable")):
            with self.subTest(args=args, kwargs=kwargs):
                with self.assertRaises(CredentialRefused) as caught:
                    store.mint(*args, **kwargs)
                self.assertEqual(caught.exception.detail, detail)
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(self.events, [])

    def test_an_expired_token_is_refused(self) -> None:
        store = self.store()
        token, _ = store.mint("device", "phone-1", ["read"], expires_at=int(self.wall.now) + 60)
        self.assertEqual(store.verify(token, peer="p").expires_at, int(self.wall.now) + 60)
        self.wall.now += 60
        with self.assertRaises(CredentialRefused) as caught:
            store.verify(token, peer="p")
        self.assertEqual(caught.exception.detail, "token_expired")
        self.assertEqual(store.held("device", "phone-1"), frozenset())

    def test_unknown_and_malformed_tokens_are_refused(self) -> None:
        store = self.store()
        store.mint("device", "phone-1", ["read"])
        for token, detail in (("rct1_" + "A" * 43, "token_unknown"), ("rst1_" + "A" * 43, "token_unknown"),
                              ("rct1_short", "token_malformed"), ("Bearer x", "token_malformed"), (None, "token_malformed"),
                              (b"rct1_" + b"A" * 43, "token_malformed"), ("rct1_" + "A" * 43 + "\n", "token_malformed")):
            with self.subTest(token=token):
                with self.assertRaises(CredentialRefused) as caught:
                    store.verify(token, peer=("peer", 1))
                self.assertEqual(caught.exception.detail, detail)

    def test_held_is_every_live_grant_of_the_principal_within_its_ceiling(self) -> None:
        store = self.store()
        _, first = store.mint("device", "phone-1", ["read"])
        store.mint("device", "phone-1", ["run", "approve"])
        store.mint("device", "phone-2", ["read"])
        self.assertEqual(store.held("device", "phone-1"), frozenset({"read", "run", "approve"}))
        store.revoke(first.token_id)
        self.assertEqual(store.held("device", "phone-1"), frozenset({"run", "approve"}))
        self.assertEqual(store.held("plugin", "phone-1"), frozenset())   # a grant is held by its kind only

    def test_events_carry_no_token_and_a_failing_notice_undoes_nothing(self) -> None:
        store = self.store()
        token, cred = store.mint("device", "phone-1", ["read", "run"], expires_at=int(self.wall.now) + 600)
        store.revoke(cred.token_id)
        self.assertEqual(self.events, [
            ("credential.issued", {"token_id": cred.token_id, "kind": "device", "id": "phone-1",
                                   "scopes": ["read", "run"], "issued_at": int(self.wall.now),
                                   "expires_at": int(self.wall.now) + 600}),
            ("credential.revoked", {"token_id": cred.token_id, "kind": "device", "id": "phone-1",
                                    "revoked_at": int(self.wall.now)}),
        ])
        dumped = json.dumps(self.events)
        self.assertNotIn(token, dumped)
        self.assertNotIn(hashlib.sha256(token.encode()).hexdigest(), dumped)

        def broken(kind, payload):
            raise RuntimeError("event log down")

        loud = self.store(emit=broken)
        with self.assertLogs("recursion_core.control.credentials", "ERROR"):
            token2, cred2 = loud.mint("device", "phone-3", ["read"])
        self.assertEqual(loud.verify(token2, peer="p").token_id, cred2.token_id)

    def test_a_store_that_is_not_owner_only_or_not_ours_is_refused(self) -> None:
        store = self.store()
        store.mint("device", "phone-1", ["read"])
        os.chmod(self.path, 0o640)
        with self.assertRaises(CredentialStoreError):
            self.store()
        os.chmod(self.path, 0o600)
        real = self.path + ".real"
        os.rename(self.path, real)
        os.symlink(real, self.path)
        with self.assertRaises(CredentialStoreError):
            self.store()
        os.unlink(self.path)
        os.rename(real, self.path)
        os.chmod(self.dir, 0o750)
        with self.assertRaises(CredentialStoreError):
            self.store()
        os.chmod(self.dir, 0o700)
        with mock.patch.object(creds.os, "getuid", lambda: os.geteuid() + 1):
            with self.assertRaises(CredentialStoreError):
                self.store()
        self.assertEqual(len(self.store().grants()), 1)

    def test_a_store_file_of_any_other_shape_is_refused(self) -> None:
        store = self.store()
        token, cred = store.mint("device", "phone-1", ["read", "run"])
        with open(self.path, encoding="ascii") as handle:
            good = json.load(handle)
        record = good["credentials"][0]

        def tampered(**changes):
            doc = json.loads(json.dumps(good))
            doc["credentials"][0].update(changes)
            return doc

        bad_docs = [
            {"version": 2, "credentials": []}, {"version": 1}, [], {"version": 1, "credentials": {}},
            tampered(scopes=["read", "manage"]),                 # a device holding manage
            tampered(kind="plugin", scopes=["approve"]),         # a plugin holding approve
            tampered(kind="owner"), tampered(token_sha256="X" * 64), tampered(token_id="tok-1"),
            tampered(id="bad id"), tampered(issued_at=-1), tampered(revoked_at=1.5), tampered(extra=1),
            {"version": 1, "credentials": [record, record]},
        ]
        for doc in bad_docs:
            with self.subTest(doc=json.dumps(doc)[:80]):
                with open(self.path, "w", encoding="ascii") as handle:
                    json.dump(doc, handle)
                with self.assertRaises(CredentialStoreError):
                    self.store()
        with open(self.path, "wb") as handle:
            handle.write(b"\xff not json")
        with self.assertRaises(CredentialStoreError):
            self.store()
        with open(self.path, "w", encoding="ascii") as handle:
            json.dump(good, handle)
        self.assertEqual(self.store().verify(token, peer="p").token_id, cred.token_id)

    def test_a_relative_path_is_refused(self) -> None:
        with self.assertRaises(CredentialStoreError):
            CredentialStore("credentials.json")

    def test_revocation_is_seen_by_every_thread_at_once(self) -> None:
        store = self.store()
        tokens = [store.mint("device", f"phone-{n}", ["read"]) for n in range(8)]
        for _, cred in tokens:
            store.revoke(cred.token_id)
        refused: list[str] = []
        lock = threading.Lock()

        def check(token: str) -> None:
            try:
                store.verify(token, peer=token[:12])
            except CredentialRefused as exc:
                with lock:
                    refused.append(exc.detail)

        threads = [threading.Thread(target=check, args=(token,)) for token, _ in tokens]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(refused, ["token_revoked"] * 8)


@NEEDS_POSIX
class FailedAuthLimitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="creds-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.clock = Clock(50.0)
        self.limit = FailedAuthLimit(max_failures=3, window_s=30.0, clock=self.clock)
        self.store = CredentialStore(os.path.join(self.base, "state", "credentials.json"), limit=self.limit)
        self.token, _ = self.store.mint("device", "phone-1", ["read"])

    def test_a_peer_over_the_limit_is_refused_even_with_a_valid_token_until_the_window_passes(self) -> None:
        with self.assertLogs("recursion_core.control.credentials", "WARNING") as logs:
            for _ in range(3):
                with self.assertRaises(CredentialRefused):
                    self.store.verify("rct1_" + "B" * 43, peer="peer-a")
            with self.assertRaises(CredentialRefused) as caught:
                self.store.verify(self.token, peer="peer-a")
        self.assertEqual(caught.exception.detail, "auth_rate_limited")
        self.assertEqual(self.store.verify(self.token, peer="peer-b").id, "phone-1")   # per peer
        text = "\n".join(logs.output)
        self.assertIn("token_unknown", text)
        self.assertIn("now refused", text)
        self.assertNotIn(self.token, text)
        self.assertNotIn("B" * 43, text)
        self.clock.now += 30.0
        self.assertEqual(self.store.verify(self.token, peer="peer-a").id, "phone-1")

    def test_failures_older_than_the_window_do_not_count(self) -> None:
        for _ in range(2):
            self.assertFalse(self.limit.failed("p"))
        self.clock.now += 31.0
        self.assertFalse(self.limit.failed("p"))
        self.assertFalse(self.limit.blocked("p"))
        self.assertFalse(self.limit.failed("p"))
        self.assertTrue(self.limit.failed("p"))
        self.assertTrue(self.limit.blocked("p"))

    def test_memory_is_bounded_by_max_peers(self) -> None:
        limit = FailedAuthLimit(max_failures=1, window_s=30.0, max_peers=4, clock=self.clock)
        for n in range(10):
            self.assertTrue(limit.failed(f"peer-{n}"))
        self.assertEqual(len(limit._failures), 4)
        self.assertTrue(limit.blocked("peer-9"))
        self.assertFalse(limit.blocked("peer-0"))   # the least recent was forgotten

    def test_limits_must_be_positive(self) -> None:
        for kwargs in ({"max_failures": 0}, {"window_s": 0}, {"window_s": float("nan")}, {"max_peers": 0}):
            with self.assertRaises(ValueError):
                FailedAuthLimit(**kwargs)


class PlatformTest(unittest.TestCase):
    def test_without_uids_the_store_refuses_to_open(self) -> None:
        with mock.patch.object(creds.os, "name", "nt"):
            with self.assertRaises(CredentialStoreError) as caught:
                CredentialStore("/nonexistent/credentials.json")
        self.assertTrue(str(caught.exception).startswith("platform_unsupported"))


class SourceTest(unittest.TestCase):
    def test_no_token_is_ever_logged_or_formatted_into_an_error(self) -> None:
        with open(creds.__file__, encoding="utf-8") as handle:
            source = handle.read()
        # Every log call and every raised message names a reason or a label, never the token variable.
        for line in source.splitlines():
            if re.search(r"log\.\w+\(|raise \w+\(", line):
                self.assertNotRegex(line, r"\btoken\b(?!_)", line)


if __name__ == "__main__":
    unittest.main()
