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
import types
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
        self.assertEqual(sorted(os.listdir(self.dir)), [".credentials.json.lock", "credentials.json"])   # no temp file

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
        self.assertEqual(os.listdir(self.dir), [".credentials.json.lock"])

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


@NEEDS_POSIX
class ReviewAnswersTest(unittest.TestCase):
    """The reads of #377 and #385 (Codex GPT-6 Astra and GPT-6.1 Sol): durability, typed refusals, the page's door."""

    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="creds-r1-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.dir = os.path.join(self.base, "state")
        self.path = os.path.join(self.dir, "credentials.json")
        self.wall = Clock()
        self.events: list[tuple[str, dict]] = []

    def store(self, **kwargs) -> CredentialStore:
        kwargs.setdefault("emit", lambda kind, payload: self.events.append((kind, payload)))
        kwargs.setdefault("wall", self.wall)
        return CredentialStore(self.path, **kwargs)

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.events]

    # -- revocation that is answered only once written --------------------------------------------------
    def test_a_failed_revocation_is_written_by_the_retry_and_notified_once(self) -> None:
        store = self.store()
        token, cred = store.mint("device", "phone-1", ["read"])
        real_write = store._write
        with mock.patch.object(store, "_write", side_effect=CredentialStoreError("disk full")):
            with self.assertRaises(CredentialStoreError):
                store.revoke(cred.token_id)
        with self.assertRaises(CredentialRefused):
            store.verify(token, peer="p")                     # refused in memory from the first call
        first = store.grants()[0].revoked_at
        self.wall.now += 30
        with mock.patch.object(store, "_write", side_effect=CredentialStoreError("still full")):
            with self.assertRaises(CredentialStoreError):
                store.revoke(cred.token_id)                   # a second outage: still not answered as done
        with mock.patch.object(store, "_write", wraps=real_write) as writer:
            self.assertEqual(store.revoke(cred.token_id).revoked_at, first)   # the first time is kept
            self.assertEqual(writer.call_count, 1)
        with self.assertRaises(CredentialRefused):
            self.store().verify(token, peer="p")              # and the file holds it
        self.assertEqual(self.kinds(), ["credential.issued", "credential.revoked"])
        with mock.patch.object(store, "_write") as writer:
            store.revoke(cred.token_id)                       # written: answered at once, no write, no notice
            self.assertEqual(writer.call_count, 0)
        self.assertEqual(self.kinds(), ["credential.issued", "credential.revoked"])

    def test_a_pending_revocation_is_made_durable_by_the_next_write_and_then_notified(self) -> None:
        store = self.store()
        token, cred = store.mint("device", "phone-1", ["read"])
        with mock.patch.object(store, "_write", side_effect=CredentialStoreError("disk full")):
            with self.assertRaises(CredentialStoreError):
                store.revoke(cred.token_id)
        store.mint("device", "phone-2", ["read"])            # this write carries the revocation too
        self.assertEqual(self.kinds(), ["credential.issued", "credential.issued", "credential.revoked"])
        with self.assertRaises(CredentialRefused):
            self.store().verify(token, peer="p")

    # -- the rename is the commit point ---------------------------------------------------------------
    def test_the_file_is_synced_then_renamed_then_its_directory_synced(self) -> None:
        store = self.store()
        operations, real_sync, real_replace = [], os.fsync, os.replace

        def sync(fd):
            operations.append("file_sync" if stat.S_ISREG(os.fstat(fd).st_mode) else "directory_sync")
            return real_sync(fd)

        def replace(source, target):
            operations.append("replace")
            return real_replace(source, target)

        with mock.patch.object(creds.os, "fsync", side_effect=sync), \
                mock.patch.object(creds.os, "replace", side_effect=replace):
            store.mint("device", "phone-1", ["read"])
        self.assertEqual(operations, ["file_sync", "replace", "directory_sync"])

    def test_a_failure_before_the_rename_changes_nothing(self) -> None:
        store = self.store()
        store.mint("device", "phone-1", ["read"])
        before = open(self.path, "rb").read()
        self.events.clear()
        with mock.patch.object(creds.os, "fsync", side_effect=OSError("EIO")):
            with self.assertRaises(CredentialStoreError) as caught:
                store.mint("device", "phone-1", ["run"])
        self.assertNotIsInstance(caught.exception, creds.CredentialStoreUncertain)
        self.assertEqual(open(self.path, "rb").read(), before)
        self.assertEqual(store.held("device", "phone-1"), frozenset({"read"}))
        self.assertEqual(self.store().held("device", "phone-1"), frozenset({"read"}))
        self.assertEqual(sorted(os.listdir(self.dir)), [".credentials.json.lock", "credentials.json"])
        self.assertEqual(self.events, [])

    def test_a_directory_sync_failure_after_the_rename_leaves_a_mint_issued_in_memory_and_on_disk(self) -> None:
        store = self.store()
        store.mint("device", "phone-1", ["read"])
        real_sync = os.fsync

        def sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("EIO on the directory")
            return real_sync(fd)

        with mock.patch.object(creds.os, "fsync", side_effect=sync):
            with self.assertLogs("recursion_core.control.credentials", "ERROR"):
                token, cred = store.mint("device", "phone-1", ["approve"])
            with self.assertRaises(creds.CredentialStoreUncertain):
                store._write(store._grants.values())       # what _write itself answers there
        reopened = self.store()
        self.assertEqual(store.held("device", "phone-1"), frozenset({"read", "approve"}))
        self.assertEqual(reopened.held("device", "phone-1"), store.held("device", "phone-1"))
        self.assertEqual(reopened.verify(token, peer="p").token_id, cred.token_id)

    def test_a_directory_sync_failure_on_revoke_is_uncertain_and_the_retry_settles_it(self) -> None:
        store = self.store()
        token, cred = store.mint("device", "phone-1", ["read"])
        real_sync = os.fsync

        def sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("EIO on the directory")
            return real_sync(fd)

        with mock.patch.object(creds.os, "fsync", side_effect=sync):
            with self.assertRaises(creds.CredentialStoreUncertain):
                store.revoke(cred.token_id)
        self.assertEqual(self.kinds(), ["credential.issued"])   # not durable yet: no notice
        with self.assertRaises(CredentialRefused):
            store.verify(token, peer="p")
        store.revoke(cred.token_id)
        self.assertEqual(self.kinds(), ["credential.issued", "credential.revoked"])

    def test_a_temporary_name_that_exists_is_never_written_through(self) -> None:
        store = self.store()
        store.mint("device", "phone-1", ["read"])
        temp = os.path.join(self.dir, ".credentials.json.ffffffffffffffff.tmp")
        with open(temp, "w", encoding="ascii") as handle:
            handle.write("not ours")
        with mock.patch.object(creds.secrets, "token_hex", return_value="ffffffffffffffff"):
            with self.assertRaises(CredentialStoreError):
                store.mint("device", "phone-2", ["read"])
        with open(temp, encoding="ascii") as handle:
            self.assertEqual(handle.read(), "not ours")
        self.assertEqual(store.held("device", "phone-2"), frozenset())

    def test_a_fresh_grant_that_collides_with_a_stored_one_is_refused(self) -> None:
        store = self.store()
        _, cred = store.mint("device", "phone-1", ["read"])
        with mock.patch.object(creds.secrets, "token_urlsafe", return_value=cred.token_id[len("ctk-"):]):
            with self.assertRaises(CredentialStoreError):
                store.mint("device", "phone-2", ["read"])
        self.assertEqual(len(store.grants()), 1)
        self.assertEqual(len(self.store().grants()), 1)

    # -- typed refusals for malformed input -------------------------------------------------------------
    def test_scope_elements_that_are_not_strings_are_a_typed_refusal(self) -> None:
        for bad in ([["read"]], [{"scope": "read"}], [[]], [None], ("read", 1)):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(CredentialRefused) as caught:
                    check_grant("device", bad)
                self.assertEqual(caught.exception.detail, "scopes_invalid")

    def test_a_stored_kind_that_is_not_a_string_is_a_store_error(self) -> None:
        store = self.store()
        store.mint("device", "phone-1", ["read"])
        doc = json.load(open(self.path, encoding="ascii"))
        for bad in ([], {}, 5):
            with self.subTest(kind=repr(bad)):
                doc["credentials"][0]["kind"] = bad
                with open(self.path, "w", encoding="ascii") as handle:
                    json.dump(doc, handle)
                with self.assertRaises(CredentialStoreError):
                    self.store()

    def test_unreadable_or_odd_store_paths_are_store_errors(self) -> None:
        store = self.store()
        store.mint("device", "phone-1", ["read"])
        real_lstat = os.lstat

        def lstat(path, *args, **kwargs):
            if path == self.dir:
                raise PermissionError("EACCES")
            return real_lstat(path, *args, **kwargs)

        with mock.patch.object(creds.os, "lstat", side_effect=lstat):
            with self.assertRaises(CredentialStoreError):
                self.store()
        real_fstat = os.fstat
        foreign = lambda fd: types.SimpleNamespace(st_mode=real_fstat(fd).st_mode, st_uid=os.getuid() + 1)  # noqa: E731
        fifo = lambda fd: types.SimpleNamespace(st_mode=stat.S_IFIFO | 0o600, st_uid=os.getuid())          # noqa: E731
        for fake in (foreign, fifo):   # the directory is this user's; only the file's own stat says no
            with mock.patch.object(creds.os, "fstat", side_effect=fake):
                with self.assertRaises(CredentialStoreError):
                    self.store()
        os.unlink(self.path)
        os.mkdir(self.path, 0o700)     # a directory where the file should be
        with self.assertRaises(CredentialStoreError):
            self.store()

    def test_a_store_version_or_record_that_only_looks_right_is_refused(self) -> None:
        # Astra's stricter checks on #377: a version that is True or 1.0, an id that does not match its kind,
        # times out of order.
        store = self.store()
        store.mint("device", "phone-1", ["read"])
        good = json.load(open(self.path, encoding="ascii"))

        def write(doc):
            with open(self.path, "w", encoding="ascii") as handle:
                json.dump(doc, handle)

        for version in (True, 1.0):
            write({"version": version, "credentials": good["credentials"]})
            with self.assertRaises(CredentialStoreError):
                self.store()
        record = good["credentials"][0]
        issued = record["issued_at"]
        for changes in ({"kind": "session"},                                   # a ctk- id on a session grant
                        {"token_id": "stk-" + record["token_id"][4:]},          # an stk- id on a device grant
                        {"expires_at": issued}, {"expires_at": issued - 1},     # expiring before it was issued
                        {"revoked_at": issued - 1}):                            # revoked before it was issued
            with self.subTest(changes=changes):
                write({"version": 1, "credentials": [{**record, **changes}]})
                with self.assertRaises(CredentialStoreError):
                    self.store()
        write({"version": 1, "credentials": [{**record, "revoked_at": issued, "expires_at": issued + 1}]})
        self.assertEqual(len(self.store().grants()), 1)                         # the boundaries themselves are fine

    def test_an_unhashable_peer_is_a_typed_refusal(self) -> None:
        # The DeepSeek read of #377, F9: failed checks are counted per peer, so a peer must key.
        store = self.store()
        token, _ = store.mint("device", "phone-1", ["read"])
        for peer in ([], {}, {"pid": 1}, ("a", [])):
            with self.subTest(peer=repr(peer)):
                with self.assertRaises(CredentialRefused) as caught:
                    store.verify(token, peer=peer)
                self.assertEqual(caught.exception.detail, "peer_invalid")
        self.assertEqual(store.verify(token, peer=("pid", 1)).id, "phone-1")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "no FIFOs on this platform")
    def test_a_fifo_at_the_store_path_is_refused_and_the_open_never_waits_for_a_writer(self) -> None:
        # The DeepSeek read of #377, F6. The open is non-blocking (checked on its flags, since a test that
        # blocked would hang the run), and the FIFO is refused by type.
        self.store()                      # makes the owner-only directory
        os.mkfifo(self.path, 0o600)
        writer = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)   # so this test itself cannot wait either
        self.addCleanup(os.close, writer)
        real_open, flags_seen = os.open, []

        def recording_open(path, flags, *args, **kwargs):
            if path == self.path:
                flags_seen.append(flags)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(creds.os, "open", side_effect=recording_open):
            with self.assertRaises(CredentialStoreError):
                self.store()
        self.assertTrue(flags_seen and all(flags & os.O_NONBLOCK for flags in flags_seen), flags_seen)

    # -- the page's own door ----------------------------------------------------------------------------
    def test_no_grant_may_name_the_pages_own_door(self) -> None:
        store = self.store()
        with self.assertRaises(CredentialRefused) as caught:
            store.mint("device", "page", ["read"])
        self.assertEqual(caught.exception.detail, "principal_id_reserved")
        store.mint("plugin", "page", ["read"])            # only the device id is the door
        _, cred = store.mint("device", "phone-1", ["read"])
        doc = json.load(open(self.path, encoding="ascii"))
        for record in doc["credentials"]:
            if record["token_id"] == cred.token_id:
                record["id"] = "page"
        with open(self.path, "w", encoding="ascii") as handle:
            json.dump(doc, handle)
        with self.assertRaises(CredentialStoreError):
            self.store()

    # -- one grant's scopes, and the public record ----------------------------------------------------
    def test_grant_scopes_are_one_live_grant_of_exactly_that_identity(self) -> None:
        store = self.store()
        _, read = store.mint("device", "phone-1", ["read"])
        _, run = store.mint("device", "phone-1", ["read", "run"], expires_at=int(self.wall.now) + 60)
        self.assertEqual(store.grant_scopes(read.token_id, "device", "phone-1"), frozenset({"read"}))
        self.assertEqual(store.grant_scopes(run.token_id, "device", "phone-1"), frozenset({"read", "run"}))
        for args in ((read.token_id, "device", "phone-2"), (read.token_id, "agent", "phone-1"),
                     ("ctk-unknown", "device", "phone-1"), (None, "device", "phone-1")):
            self.assertEqual(store.grant_scopes(*args), frozenset(), args)
        self.wall.now += 60
        self.assertEqual(store.grant_scopes(run.token_id, "device", "phone-1"), frozenset())
        store.revoke(read.token_id)
        self.assertEqual(store.grant_scopes(read.token_id, "device", "phone-1"), frozenset())

    def test_the_public_record_names_every_field_and_no_secret(self) -> None:
        store = self.store()
        token, cred = store.mint("device", "phone-1", ["run", "read"], expires_at=int(self.wall.now) + 5)
        store.revoke(cred.token_id)
        (public,) = [c.public() for c in store.grants()]
        self.assertEqual(public, {"token_id": cred.token_id, "kind": "device", "id": "phone-1",
                                  "scopes": ["read", "run"], "issued_at": int(self.wall.now),
                                  "expires_at": int(self.wall.now) + 5, "revoked_at": int(self.wall.now)})
        self.assertNotIn(token, json.dumps(public))




@NEEDS_POSIX
class RoundTwoTest(unittest.TestCase):
    """The round-2 reads of #377 (Codex GPT-6 Astra 1-3, GPT-6.1 Sol 1 and 3)."""

    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="creds-r2-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.path = os.path.join(self.base, "state", "credentials.json")
        self.wall = Clock()
        self.events: list[tuple[str, dict]] = []

    def store(self, **kwargs) -> CredentialStore:
        kwargs.setdefault("emit", lambda kind, payload: self.events.append((kind, payload)))
        kwargs.setdefault("wall", self.wall)
        return CredentialStore(self.path, **kwargs)

    def revoked_notices(self) -> list[dict]:
        return [payload for kind, payload in self.events if kind == "credential.revoked"]

    def test_a_revocation_after_the_wall_clock_steps_back_writes_a_store_that_loads(self) -> None:
        # Astra 1, Sol 1: revoked_at fell before issued_at, and the load rule then refused the whole store.
        store = self.store()
        token, cred = store.mint("device", "one", ["read"])
        issued = cred.issued_at
        self.wall.now -= 3600
        with mock.patch.object(store, "_write", side_effect=CredentialStoreError("disk full")):
            with self.assertRaises(CredentialStoreError):
                store.revoke(cred.token_id)               # refused at once, not yet written
        self.wall.now -= 60                               # a retry keeps the first time
        self.assertEqual(store.revoke(cred.token_id).revoked_at, issued)
        (reopened,) = self.store().grants()
        self.assertEqual(reopened.revoked_at, issued)
        with self.assertRaises(CredentialRefused) as caught:
            self.store().verify(token, peer="p")
        self.assertEqual(caught.exception.detail, "token_revoked")
        self.assertEqual([p["revoked_at"] for p in self.revoked_notices()], [issued])

    def test_a_digest_that_collides_under_a_fresh_token_id_mints_nothing(self) -> None:
        # Astra 2, Sol 3: the earlier test forced an id collision only; this one keeps the ids distinct.
        store = self.store()
        with mock.patch.object(creds.secrets, "token_bytes", return_value=b"\x01" * 32), \
                mock.patch.object(creds.secrets, "token_urlsafe", side_effect=["record-a", "record-b"]):
            store.mint("device", "first", ["read"])
            with self.assertRaises(CredentialStoreError):
                store.mint("device", "second", ["run"])
        self.assertEqual([c.id for c in store.grants()], ["first"])
        self.assertEqual([c.id for c in self.store().grants()], ["first"])
        self.assertEqual([kind for kind, _ in self.events], ["credential.issued"])

    def test_an_uncertain_mint_settles_no_pending_revocation(self) -> None:
        # Astra 3, Sol 3: a mint whose directory sync fails has written the pending revocations, but not
        # durably: their notices wait for the next write that succeeds fully, and go out once.
        store = self.store()
        ids = [store.mint("device", name, ["read"])[1].token_id for name in ("a", "b")]
        for token_id in ids:
            with mock.patch.object(store, "_write", side_effect=CredentialStoreError("disk full")):
                with self.assertRaises(CredentialStoreError):
                    store.revoke(token_id)
        real_fsync = creds.os.fsync

        def directory_sync_fails(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("EIO")
            return real_fsync(fd)

        with mock.patch.object(creds.os, "fsync", side_effect=directory_sync_fails):
            with self.assertLogs(creds.log, "ERROR"):
                store.mint("device", "c", ["read"])
        self.assertEqual(self.revoked_notices(), [])
        self.assertEqual(sorted(store._unpersisted), sorted(ids))
        store.mint("device", "d", ["read"])               # a write that succeeds fully settles both, once
        self.assertEqual(sorted(p["token_id"] for p in self.revoked_notices()), sorted(ids))
        store.revoke(ids[0])
        self.assertEqual(len(self.revoked_notices()), 2)
        self.assertEqual(sum(c.revoked_at is not None for c in self.store().grants()), 2)

    def test_a_descriptor_whose_check_fails_is_closed_the_store_s_and_the_lock_s(self) -> None:
        # Sol's C11 and C12 (later coverage, not required): an fstat that fails is a typed refusal, and the
        # descriptor it was asked of is closed. The lock is taken first, so each descriptor is tried on its own.
        self.store().mint("device", "first", ["read"])
        lock = os.path.join(self.base, "state", ".credentials.json.lock")
        real_open, real_fstat = creds.os.open, creds.os.fstat
        for target in (self.path, lock):
            with self.subTest(target=os.path.basename(target)):
                opened: list[int] = []

                def record(path, flags, *args, **kwargs):
                    fd = real_open(path, flags, *args, **kwargs)
                    if path == target:
                        opened.append(fd)
                    return fd

                def fstat(fd):
                    if fd in opened:
                        raise OSError("EIO")
                    return real_fstat(fd)

                with mock.patch.object(creds.os, "open", side_effect=record), \
                        mock.patch.object(creds.os, "fstat", side_effect=fstat):
                    with self.assertRaises(CredentialStoreError):
                        self.store()
                self.assertEqual(len(opened), 1)
                with self.assertRaises(OSError):
                    os.fstat(opened[0])



@NEEDS_POSIX
class RoundThreeTest(unittest.TestCase):
    """The round-3 reads of #377 (Codex GPT-6 Astra 2-4 and its later coverage, GPT-6.1 Sol 2), and Sol's NUL path
    from the round-2 read of #385."""

    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="creds-r3-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.path = os.path.join(self.base, "state", "credentials.json")
        self.wall = Clock(1000.0)
        self.events: list[tuple[str, dict]] = []

    def store(self, **kwargs) -> CredentialStore:
        kwargs.setdefault("emit", lambda kind, payload: self.events.append((kind, payload)))
        kwargs.setdefault("wall", self.wall)
        return CredentialStore(self.path, **kwargs)

    def revoked_notices(self) -> list[dict]:
        return [payload for kind, payload in self.events if kind == "credential.revoked"]

    def test_a_lock_that_cannot_be_opened_still_leaves_the_grant_refused_at_once(self) -> None:
        # Astra 2: the writers' lock came before the revocation in memory, so its failure left the grant live.
        store = self.store()
        token, cred = store.mint("device", "one", ["read"])
        real_open = creds.os.open

        def no_lock(path, flags, *args, **kwargs):
            if path.endswith(".lock"):
                raise OSError("EMFILE")
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(creds.os, "open", side_effect=no_lock):
            with self.assertRaises(CredentialStoreError):
                store.revoke(cred.token_id)
            with self.assertRaises(CredentialRefused) as caught:
                store.verify(token, peer="p")      # the file is unchanged, so this check reads memory
        self.assertEqual(caught.exception.detail, "token_revoked")
        self.assertEqual(self.revoked_notices(), [])
        self.wall.now += 60
        self.assertEqual(store.revoke(cred.token_id).revoked_at, 1000)   # the retry writes the first time
        self.assertEqual(self.store().grants()[0].revoked_at, 1000)
        self.assertEqual([p["revoked_at"] for p in self.revoked_notices()], [1000])

    def test_a_pending_revocation_keeps_its_first_time_after_another_store_revokes(self) -> None:
        # Astra 3: the other store's later time (1100) replaced this store's first one (1000).
        store = self.store()
        _, cred = store.mint("device", "one", ["read"])
        with mock.patch.object(store, "_write", side_effect=CredentialStoreError("disk full")):
            with self.assertRaises(CredentialStoreError):
                store.revoke(cred.token_id)
        self.store(wall=lambda: 1100.0).revoke(cred.token_id)
        self.wall.now = 1200
        self.assertEqual(store.revoke(cred.token_id).revoked_at, 1000)
        self.assertEqual(self.store().grants()[0].revoked_at, 1000)

    def test_revoking_from_a_stale_memory_what_another_store_revoked_writes_and_notifies_nothing_here(self) -> None:
        store = self.store()
        _, cred = store.mint("device", "one", ["read"])
        self.events.clear()
        self.store(wall=lambda: 900.0, emit=None).revoke(cred.token_id)   # 900 is clamped to issue: 1000
        self.wall.now = 1500
        with mock.patch.object(store, "_write", wraps=store._write) as writer:
            self.assertEqual(store.revoke(cred.token_id).revoked_at, 1000)
        self.assertEqual(writer.call_count, 0)
        self.assertEqual(self.revoked_notices(), [])
        self.assertEqual(store._unpersisted, frozenset())

    def test_each_reader_is_the_first_to_see_another_store_s_write(self) -> None:
        # Astra 4, Sol 2 (STRICT proof): grant_scopes rereads a changed file itself, not through another reader.
        store = self.store()
        _, cred = store.mint("plugin", "plugin.alpha", ["read"])
        other = self.store()
        self.assertEqual(store.grant_scopes(cred.token_id, "plugin", "plugin.alpha"), frozenset({"read"}))
        other.revoke(cred.token_id)
        self.assertEqual(store.grant_scopes(cred.token_id, "plugin", "plugin.alpha"), frozenset())
        other.mint("device", "late", ["read"])
        self.assertEqual(sorted(c.id for c in store.grants()), ["late", "plugin.alpha"])   # grants() first
        other.mint("plugin", "plugin.beta", ["run"])
        self.assertEqual(store.held("plugin", "plugin.beta"), frozenset({"run"}))           # held() first

    def test_without_fcntl_the_store_refuses_to_open(self) -> None:
        with mock.patch.object(creds, "fcntl", None):
            with self.assertRaises(CredentialStoreError) as caught:
                self.store()
        self.assertTrue(str(caught.exception).startswith("platform_unsupported"))

    def test_a_lock_timeout_that_is_not_a_finite_positive_number_is_refused(self) -> None:
        for value in (0, -1, True, float("nan"), float("inf"), "1", None):
            with self.subTest(value=repr(value)):
                with self.assertRaises(CredentialStoreError):
                    self.store(lock_timeout_s=value)
        self.store(lock_timeout_s=0.5)

    def test_a_lock_owned_by_another_user_is_refused_on_its_own(self) -> None:
        self.store()
        lock = os.path.join(self.base, "state", ".credentials.json.lock")
        real_open, real_fstat = creds.os.open, creds.os.fstat
        lock_fds: list[int] = []

        def record(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if path == lock:
                lock_fds.append(fd)
            return fd

        def foreign_lock(fd):
            st = real_fstat(fd)
            if fd in lock_fds:
                return types.SimpleNamespace(st_mode=st.st_mode, st_uid=os.getuid() + 1)
            return st

        with mock.patch.object(creds.os, "open", side_effect=record), \
                mock.patch.object(creds.os, "fstat", side_effect=foreign_lock):
            with self.assertRaises(CredentialStoreError) as caught:
                self.store()
        self.assertIn("lock", str(caught.exception))

    def test_a_store_path_holding_a_nul_is_a_typed_refusal(self) -> None:
        with self.assertRaises(CredentialStoreError) as caught:
            CredentialStore(os.path.join(self.base, "sta\x00te", "credentials.json"))
        self.assertIn("NUL", str(caught.exception))



@NEEDS_POSIX
class RoundFourTest(unittest.TestCase):
    """The round-4 reads of #377 (Codex GPT-6 Astra 2 and 3, GPT-6.1 Sol 1)."""

    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="creds-r4-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.path = os.path.join(self.base, "state", "credentials.json")
        self.wall = Clock(1000.0)
        self.events: list[tuple[str, dict]] = []

    def store(self, **kwargs) -> CredentialStore:
        kwargs.setdefault("emit", lambda kind, payload: self.events.append((kind, payload)))
        kwargs.setdefault("wall", self.wall)
        return CredentialStore(self.path, **kwargs)

    def pending_at_1200_then_another_writer_at_1100(self) -> tuple[CredentialStore, str]:
        store = self.store()
        _, cred = store.mint("device", "one", ["read"])
        self.wall.now = 1200
        with mock.patch.object(store, "_write", side_effect=CredentialStoreError("disk full")):
            with self.assertRaises(CredentialStoreError):
                store.revoke(cred.token_id)
        self.store(wall=lambda: 1100.0, emit=None).revoke(cred.token_id)   # its clock is behind
        self.wall.now = 1300
        return store, cred.token_id

    def test_a_pending_revocation_keeps_the_time_first_recorded_when_the_next_writer_s_clock_is_behind(self) -> None:
        # Astra 2, Sol 1: the earlier of the two times (1100, the other store's) replaced the first recorded here.
        for path in ("retry", "refresh, then another write"):
            with self.subTest(path=path):
                store, token_id = self.pending_at_1200_then_another_writer_at_1100()
                if path == "retry":
                    self.assertEqual(store.revoke(token_id).revoked_at, 1200)
                else:
                    self.assertEqual([c.revoked_at for c in store.grants()], [1200])   # rereads the file
                    store.mint("device", "two", ["read"])
                self.assertEqual({c.id: c.revoked_at for c in self.store().grants()}["one"], 1200)
                self.assertEqual([p["revoked_at"] for k, p in self.events if k == "credential.revoked"], [1200])
                shutil.rmtree(os.path.join(self.base, "state"))
                self.events.clear()
                self.wall.now = 1000

    def test_a_grant_this_store_first_finds_under_the_lock_is_revoked(self) -> None:
        # Astra 3 (STRICT proof): with the mark under the lock removed, such a grant was written back live and all
        # the lane's tests still passed.
        here, there = self.store(), self.store()
        token, cred = there.mint("device", "theirs", ["read"])     # here has not read the file since
        revoked = here.revoke(cred.token_id)
        self.assertEqual(revoked.revoked_at, 1000)
        with self.assertRaises(CredentialRefused) as caught:
            self.store().verify(token, peer="p")
        self.assertEqual(caught.exception.detail, "token_revoked")
        self.assertEqual([p["token_id"] for k, p in self.events if k == "credential.revoked"], [cred.token_id])



@NEEDS_POSIX
class StoreFailureGuardsTest(unittest.TestCase):
    """The round-3 read of #385 by Codex GPT-6 Astra, 4 (coverage of PR 1's store): each guard below was correct,
    and removing it left every lane test green."""

    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="creds-guards-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.path = os.path.join(self.base, "state", "credentials.json")

    def test_a_file_whose_identity_cannot_be_read_is_a_typed_refusal(self) -> None:
        store = CredentialStore(self.path)
        store.mint("device", "d1", ["read"])
        real_stat = creds.os.stat

        def stat(path, *args, **kwargs):
            if path == self.path:
                raise OSError("EIO")
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(creds.os, "stat", side_effect=stat):
            with self.assertRaises(CredentialStoreError):
                store.grants()

    def test_a_mint_stays_issued_when_the_file_cannot_be_reread_after_its_write(self) -> None:
        store = CredentialStore(self.path)
        with mock.patch.object(store, "_identity", side_effect=CredentialStoreError("EIO")):
            token, cred = store.mint("device", "d1", ["read"])
        self.assertEqual([c.token_id for c in store.grants()], [cred.token_id])
        self.assertEqual(CredentialStore(self.path).verify(token, peer="p").id, "d1")

    def test_a_lock_that_cannot_be_taken_for_another_reason_is_a_typed_refusal(self) -> None:
        with mock.patch.object(creds.fcntl, "flock", side_effect=OSError("ENOLCK")):
            with self.assertRaises(CredentialStoreError) as caught:
                CredentialStore(self.path)
        self.assertIn("lock", str(caught.exception))

    def test_a_directory_that_cannot_be_listed_still_opens(self) -> None:
        with mock.patch.object(creds.os, "listdir", side_effect=OSError("EACCES")):
            store = CredentialStore(self.path)
        self.assertEqual(store.grants(), [])

    def test_an_orphan_that_cannot_be_removed_still_opens(self) -> None:
        CredentialStore(self.path)
        orphan = os.path.join(self.base, "state", ".credentials.json.0123456789abcdef.tmp")
        with open(orphan, "w", encoding="ascii") as handle:
            handle.write("{}")
        real_unlink = creds.os.unlink

        def unlink(path, *args, **kwargs):
            if path == orphan:
                raise PermissionError("EPERM")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(creds.os, "unlink", side_effect=unlink):
            store = CredentialStore(self.path)
        self.assertTrue(os.path.exists(orphan))
        self.assertEqual(store.grants(), [])



class PeerRefusalTest(unittest.TestCase):
    """The round-5 read of #377 by DeepSeek V4.1 Flash, F2: only TypeError was caught around the peer's hash, and the
    limit's own public methods raised for an unhashable peer."""

    class HashRaises:
        def __hash__(self):
            raise RuntimeError("the connection is gone")

    def test_a_peer_that_cannot_key_the_count_is_refused_peer_invalid(self) -> None:
        limit = creds.FailedAuthLimit()
        for peer in ([], {"a": 1}, self.HashRaises()):
            for method in (limit.blocked, limit.failed):
                with self.subTest(peer=type(peer).__name__, method=method.__name__):
                    with self.assertRaises(CredentialRefused) as caught:
                        method(peer)
                    self.assertEqual(caught.exception.detail, "peer_invalid")

    @NEEDS_POSIX
    def test_verify_refuses_a_peer_whose_hash_raises_anything(self) -> None:
        base = tempfile.mkdtemp(prefix="creds-peer-")
        self.addCleanup(shutil.rmtree, base, True)
        store = CredentialStore(os.path.join(base, "state", "credentials.json"))
        token, _ = store.mint("device", "d1", ["read"])
        with self.assertRaises(CredentialRefused) as caught:
            store.verify(token, peer=self.HashRaises())
        self.assertEqual(caught.exception.detail, "peer_invalid")


@NEEDS_POSIX
class TwoStoresTest(unittest.TestCase):
    """The audited read of #377 by DeepSeek V4.1 Flash, F4: two stores on one file each wrote their own memory,
    so the second write dropped the first store's grants. F13: a killed writer's temporary file stayed."""

    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="creds-two-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.dir = os.path.join(self.base, "state")
        self.path = os.path.join(self.dir, "credentials.json")

    def store(self, **kwargs) -> CredentialStore:
        return CredentialStore(self.path, **kwargs)

    def on_disk(self) -> list[str]:
        with open(self.path, encoding="ascii") as handle:
            return sorted(record["id"] for record in json.load(handle)["credentials"])

    def test_two_stores_on_one_file_keep_each_others_grants(self) -> None:
        a, b = self.store(), self.store()          # both opened before either writes, as in DeepSeek's attack
        token_a, _ = a.mint("device", "sa", ["read"])
        token_b, _ = b.mint("device", "sb", ["read"])
        self.assertEqual(self.on_disk(), ["sa", "sb"])
        for store in (a, b, self.store()):
            for token in (token_a, token_b):
                self.assertEqual(store.verify(token, peer="p").kind, "device")

    def test_a_revocation_through_one_store_keeps_the_grant_another_store_minted(self) -> None:
        a, b = self.store(), self.store()
        token_a, cred_a = a.mint("device", "sa", ["read"])
        token_b, _ = b.mint("device", "sb", ["read"])     # a has not read the file since
        a.revoke(cred_a.token_id)
        self.assertEqual(self.on_disk(), ["sa", "sb"])
        fresh = self.store()
        self.assertEqual(fresh.verify(token_b, peer="p").id, "sb")
        with self.assertRaises(CredentialRefused):
            fresh.verify(token_a, peer="p")

    def test_a_revocation_through_one_store_is_refused_by_the_other_at_its_next_check(self) -> None:
        a, b = self.store(), self.store()
        token, cred = a.mint("plugin", "plugin.alpha", ["read", "run"])
        self.assertEqual(b.held("plugin", "plugin.alpha"), frozenset({"read", "run"}))
        a.revoke(cred.token_id)
        with self.assertRaises(CredentialRefused) as caught:
            b.verify(token, peer="p")
        self.assertEqual(caught.exception.detail, "token_revoked")
        self.assertEqual(b.held("plugin", "plugin.alpha"), frozenset())
        self.assertEqual(b.grant_scopes(cred.token_id, "plugin", "plugin.alpha"), frozenset())
        self.assertIsNotNone(b.revoke(cred.token_id).revoked_at)   # already written: answered from the file
        self.assertEqual([c.revoked_at is not None for c in b.grants()], [True])

    def test_writers_in_two_threads_lose_nothing(self) -> None:
        stores, tokens = (self.store(), self.store()), []

        def mint(store: CredentialStore, prefix: str) -> None:
            for n in range(25):
                tokens.append(store.mint("device", f"{prefix}{n}", ["read"])[0])

        threads = [threading.Thread(target=mint, args=(store, prefix)) for store, prefix in zip(stores, "ab")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)
        fresh = self.store()
        self.assertEqual(len(self.on_disk()), 50)
        self.assertEqual(len(tokens), 50)
        for token in tokens:
            fresh.verify(token, peer="p")

    def test_a_writer_that_cannot_have_the_lock_is_refused_and_changes_nothing(self) -> None:
        import fcntl
        store = self.store(lock_timeout_s=0.2)
        store.mint("device", "first", ["read"])
        holder = os.open(os.path.join(self.dir, ".credentials.json.lock"), os.O_RDWR)
        try:
            fcntl.flock(holder, fcntl.LOCK_EX)
            for attempt in (lambda: store.mint("device", "second", ["read"]),
                            lambda: store.revoke(store.grants()[0].token_id)):
                with self.assertRaises(CredentialStoreError) as caught:
                    attempt()
                self.assertIn("held by another writer", str(caught.exception))
        finally:
            os.close(holder)
        self.assertEqual(self.on_disk(), ["first"])
        self.assertEqual([c.revoked_at for c in self.store().grants()], [None])
        store.mint("device", "second", ["read"])
        self.assertEqual(self.on_disk(), ["first", "second"])

    def test_a_lock_that_is_not_an_owner_only_file_of_this_user_is_refused(self) -> None:
        self.store()
        lock = os.path.join(self.dir, ".credentials.json.lock")
        os.chmod(lock, 0o644)
        with self.assertRaises(CredentialStoreError):
            self.store()
        os.unlink(lock)
        os.symlink(self.path, lock)
        with self.assertRaises(CredentialStoreError):
            self.store()
        os.unlink(lock)
        os.mkfifo(lock, 0o600)
        with self.assertRaises(CredentialStoreError):
            self.store()
        with self.assertRaises(CredentialStoreError):
            self.store(lock_timeout_s=0)

    def test_a_store_whose_file_turns_unreadable_refuses_every_token_without_counting_it(self) -> None:
        limit = creds.FailedAuthLimit(max_failures=1)
        store = self.store(limit=limit)
        token, _ = store.mint("device", "d1", ["read"])
        writer = self.store()
        os.chmod(self.path, 0o644)                  # another hand changes the file: no longer owner-only
        os.utime(self.path, ns=(1, 1))
        with self.assertRaises(CredentialRefused) as caught:
            store.verify(token, peer="p")
        self.assertEqual(caught.exception.detail, "store_unavailable")
        self.assertFalse(limit.blocked("p"))
        with self.assertRaises(CredentialStoreError):
            store.held("device", "d1")
        os.chmod(self.path, 0o600)
        self.assertEqual(store.verify(token, peer="p").id, "d1")
        del writer

    def test_opening_a_store_removes_a_killed_writers_temporary_files_only(self) -> None:
        self.store().mint("device", "d1", ["read"])
        orphans = [".credentials.json.0123456789abcdef.tmp", ".credentials.json.fedcba9876543210.tmp"]
        kept = [".credentials.json.notes.tmp", ".other.json.0123456789abcdef.tmp", "credentials.json.bak"]
        for name in orphans + kept:
            with open(os.path.join(self.dir, name), "w", encoding="ascii") as handle:
                handle.write("{}")
        self.store()
        left = sorted(os.listdir(self.dir))
        self.assertEqual(left, sorted([".credentials.json.lock", "credentials.json"] + kept))

    @unittest.skipUnless(os.path.isdir("/proc/self/fd"), "counts descriptors through /proc")
    def test_a_directory_at_the_store_path_is_refused_and_leaks_no_descriptor(self) -> None:
        os.makedirs(self.path, 0o700)                # the audited read's F3
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(20):
            with self.assertRaises(CredentialStoreError):
                self.store()
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)


class RoundSixCredentialsTest(unittest.TestCase):
    """The round-6 read of #377 by DeepSeek V4.1 Flash, 1 and 2: a peer, a token or a token id whose own comparison,
    hash or encode raised escaped with that error. And the round-4 reads of #385 (Codex GPT-6 Astra 2, GPT-6.1
    Sol 2): no test linked the writers' lock to an existing valid file."""

    class EqRaises(str):
        def __eq__(self, other):
            raise RuntimeError("the connection is gone")

        def __ne__(self, other):
            raise RuntimeError("the connection is gone")

        __hash__ = str.__hash__

    class HashRaises(str):
        def __hash__(self):
            raise RuntimeError("the id cannot be hashed")

    class EncodeRaises(str):
        def encode(self, *args, **kwargs):
            raise RuntimeError("the text cannot be encoded")

    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="creds-r6-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.path = os.path.join(self.base, "state", "credentials.json")

    def test_a_peer_that_is_a_str_subclass_is_counted_as_its_text(self) -> None:
        limit = creds.FailedAuthLimit(max_failures=2)
        self.assertEqual([limit.failed(self.EqRaises("peer-a")) for _ in range(2)], [False, True])
        self.assertTrue(limit.blocked(self.EqRaises("peer-a")))
        self.assertTrue(limit.blocked("peer-a"))                # the same peer, as plain text
        self.assertFalse(limit.blocked("peer-b"))

    def test_a_peer_whose_comparison_raises_in_the_count_is_refused_peer_invalid(self) -> None:
        class Peer:                                             # not text: it hashes, but comparing it raises
            def __hash__(self):
                return 7

            def __eq__(self, other):
                raise RuntimeError("the connection is gone")

        limit = creds.FailedAuthLimit()
        self.assertFalse(limit.failed(Peer()))                  # nothing to compare it with yet
        for method in (limit.blocked, limit.failed):
            with self.subTest(method=method.__name__):
                with self.assertRaises(CredentialRefused) as caught:
                    method(Peer())
                self.assertEqual(caught.exception.detail, "peer_invalid")

    @NEEDS_POSIX
    def test_verify_counts_a_peer_whose_comparison_raises(self) -> None:
        store = CredentialStore(self.path, limit=creds.FailedAuthLimit(max_failures=1))
        token, cred = store.mint("device", "d1", ["read"])
        self.assertEqual(store.verify(token, peer=self.EqRaises("p")).token_id, cred.token_id)
        with self.assertRaises(CredentialRefused) as caught:
            store.verify("rct1_" + "B" * 43, peer=self.EqRaises("p"))
        self.assertEqual(caught.exception.detail, "token_unknown")
        with self.assertRaises(CredentialRefused) as caught:
            store.verify(token, peer=self.EqRaises("p"))
        self.assertEqual(caught.exception.detail, "auth_rate_limited")

    @NEEDS_POSIX
    def test_a_token_or_an_id_that_is_a_str_subclass_is_read_as_its_text(self) -> None:
        store = CredentialStore(self.path)
        token, cred = store.mint("plugin", "p1", ["read"])
        self.assertEqual(store.verify(self.EncodeRaises(token), peer="p").token_id, cred.token_id)
        self.assertEqual(store.grant_scopes(self.HashRaises(cred.token_id), self.EqRaises("plugin"),
                                            self.EqRaises("p1")), frozenset({"read"}))
        self.assertEqual(store.held(self.EqRaises("plugin"), self.EqRaises("p1")), frozenset({"read"}))
        self.assertIsNotNone(store.revoke(self.HashRaises(cred.token_id)).revoked_at)
        with self.assertRaises(CredentialRefused) as caught:
            store.verify(token, peer="p")
        self.assertEqual(caught.exception.detail, "token_revoked")

    @NEEDS_POSIX
    def test_a_lock_symlink_to_an_existing_valid_target_is_refused(self) -> None:
        # Sol 2 and Astra 2 on #385 (round 4): the older fixture linked the lock to a missing file, so taking
        # O_NOFOLLOW off the writers' lock left every test green.
        store = CredentialStore(self.path)
        target = os.path.join(self.base, "state", "valid-lock-target")
        os.close(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        os.unlink(store._lock_path)
        os.symlink(target, store._lock_path)
        with self.assertRaises(CredentialStoreError):
            CredentialStore(self.path)


class RoundSevenCredentialsTest(unittest.TestCase):
    """The round-7 read of #377 by DeepSeek V4.1 Flash: 1, a str subclass whose hash raises still escaped ceiling,
    check_grant and mint; 2, an OSError at the release of the writers' lock escaped mint untyped."""

    class HashRaises(str):
        def __hash__(self):
            raise RuntimeError("the text cannot be hashed")

    def test_a_kind_scope_or_id_that_is_a_str_subclass_is_read_as_its_text(self) -> None:
        H = self.HashRaises
        self.assertEqual(creds.ceiling(H("plugin")), CEILING["plugin"])
        self.assertEqual(creds.check_grant(H("plugin"), [H("read")]), frozenset({"read"}))
        with self.assertRaises(CredentialRefused) as caught:
            creds.check_grant(H("owner"), ["read"])
        self.assertEqual(caught.exception.detail, "kind_not_mintable")
        with self.assertRaises(CredentialRefused) as caught:
            creds.check_grant("plugin", [H("manage")])
        self.assertEqual(caught.exception.detail, "manage_never_granted")

    @NEEDS_POSIX
    def test_mint_reads_a_str_subclass_kind_id_and_scope_as_their_text(self) -> None:
        base = tempfile.mkdtemp(prefix="creds-r7-")
        self.addCleanup(shutil.rmtree, base, True)
        store = CredentialStore(os.path.join(base, "state", "credentials.json"))
        H = self.HashRaises
        token, cred = store.mint(H("plugin"), H("p1"), [H("read")])
        self.assertEqual((type(cred.kind), type(cred.id)), (str, str))
        self.assertEqual(store.verify(token, peer="p").scopes, frozenset({"read"}))
        with self.assertRaises(CredentialRefused) as caught:
            store.mint("device", H("page"), ["read"])
        self.assertEqual(caught.exception.detail, "principal_id_reserved")

    @NEEDS_POSIX
    def test_a_failed_release_of_the_writers_lock_changes_no_answer(self) -> None:
        base = tempfile.mkdtemp(prefix="creds-r7-")
        self.addCleanup(shutil.rmtree, base, True)
        path = os.path.join(base, "state", "credentials.json")
        store = CredentialStore(path)
        real_open, real_close = creds.os.open, creds.os.close
        lock_fds: list[int] = []
        failed: list[int] = []

        def recording_open(name, flags, *args, **kwargs):
            fd = real_open(name, flags, *args, **kwargs)
            if str(name).endswith(".lock"):
                lock_fds.append(fd)
            return fd

        def failing_close(fd):
            if fd in lock_fds and not failed:                # the worst case: the descriptor stays open
                failed.append(fd)
                self.addCleanup(real_close, fd)
                raise OSError(5, "Input/output error")
            return real_close(fd)

        with mock.patch.object(creds.os, "open", side_effect=recording_open), \
                mock.patch.object(creds.os, "close", side_effect=failing_close):
            token, cred = store.mint("device", "d1", ["read"])
        self.assertEqual(len(failed), 1)
        self.assertEqual(store.verify(token, peer="p").token_id, cred.token_id)
        again = CredentialStore(path)                        # the lock is free: another store opens and writes
        self.assertEqual(len(again.grants()), 1)
        again.mint("device", "d2", ["read"])


class RoundEightCredentialsTest(unittest.TestCase):
    """The round-8 read of #377 by DeepSeek V4.1 Flash, 1: check_grant read the scopes container with the caller's
    own __iter__, __len__ and __bool__, so a subclass that raised left check_grant and mint untyped."""

    def containers(self):
        def raising(base, method):
            def fail(self, *args):
                raise RuntimeError(f"{method} failed")
            return type(f"{base.__name__}_{method}", (base,), {method: fail})

        made = []
        for base, value in ((list, ["read"]), (tuple, ("read",)), (set, {"read"}), (frozenset, frozenset({"read"}))):
            for method in ("__iter__", "__len__", "__bool__", "__contains__"):
                made.append(raising(base, method)(value))
        made.append(type("PlainSubclass", (list,), {})(["read"]))     # a subclass that raises nothing is refused too
        return made

    def test_a_scopes_container_that_is_a_subclass_is_refused_scopes_invalid(self) -> None:
        for scopes in self.containers():
            with self.subTest(container=type(scopes).__name__):
                with self.assertRaises(CredentialRefused) as caught:
                    creds.check_grant("plugin", scopes)
                self.assertEqual(caught.exception.detail, "scopes_invalid")
        for scopes in (["read"], ("read",), {"read"}, frozenset({"read"})):
            self.assertEqual(creds.check_grant("plugin", scopes), frozenset({"read"}))

    @NEEDS_POSIX
    def test_mint_refuses_such_a_container_typed_and_writes_nothing(self) -> None:
        base = tempfile.mkdtemp(prefix="creds-r8-")
        self.addCleanup(shutil.rmtree, base, True)
        store = CredentialStore(os.path.join(base, "state", "credentials.json"))
        for scopes in self.containers():
            with self.subTest(container=type(scopes).__name__):
                with self.assertRaises(CredentialRefused) as caught:
                    store.mint("plugin", "p1", scopes)
                self.assertEqual(caught.exception.detail, "scopes_invalid")
        self.assertEqual(store.grants(), [])


class RoundEightCodexCredentialsTest(unittest.TestCase):
    """The round-8 reads of #377: Codex GPT-6 Astra 2 and 3, GPT-6.1 Sol 2 and 3. A container type compared with ``in``
    asked the type's metaclass; mint's isinstance read the caller's __class__; and a wall clock below zero wrote an
    issue time that made the whole store unloadable (#385's round-6 F3)."""

    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="creds-r8c-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.path = os.path.join(self.base, "state", "synthetic-store.json")

    def test_container_type_membership_does_not_use_metaclass_equality(self) -> None:
        # Astra's ContainerContracts test.
        class Meta(type):
            def __eq__(cls, other):
                return other is list

            __hash__ = type.__hash__

        class Container(list, metaclass=Meta):
            def __iter__(self):
                raise RuntimeError("synthetic container iteration")

        with self.assertRaises(CredentialRefused) as caught:
            creds.check_grant("plugin", Container(["read"]))
        self.assertEqual(caught.exception.detail, "scopes_invalid")

    @NEEDS_POSIX
    def test_mint_refuses_a_container_without_reading_its_class(self) -> None:
        # Sol's test.
        class OtherCollection:
            @property
            def __class__(self):
                raise RuntimeError("synthetic class reader failed")

            def __iter__(self):
                return iter(("read",))

            def __len__(self):
                return 1

            def __contains__(self, item):
                return item == "read"

        store = CredentialStore(self.path)
        with self.assertRaises(CredentialRefused) as caught:
            store.mint("plugin", "synthetic", OtherCollection())
        self.assertEqual(caught.exception.detail, "scopes_invalid")
        self.assertEqual(store.grants(), [])

    @NEEDS_POSIX
    def test_mint_with_negative_clock_keeps_the_store_loadable(self) -> None:
        # Sol's test; Astra's original_negative_clock_mint_must_reload is the same case.
        store = CredentialStore(self.path, wall=lambda: -1.0)
        store.mint("plugin", "synthetic", ["read"])
        reopened = CredentialStore(self.path)
        self.assertEqual(len(reopened.grants()), 1)
        self.assertEqual(reopened.grants()[0].issued_at, 0)

    @NEEDS_POSIX
    def test_rollback_must_not_damage_existing_store(self) -> None:
        # Astra's ClockContracts test: a clock that steps back below zero after a valid grant.
        now = [1000]
        store = CredentialStore(self.path, wall=lambda: now[0])
        store.mint("device", "existing", ["read"])
        now[0] = -2.5
        store.mint("plugin", "later", ["read"])
        self.assertEqual(len(CredentialStore(self.path).grants()), 2)

    @NEEDS_POSIX
    def test_rollback_expiry_cannot_write_negative_expiry(self) -> None:
        # Astra's ClockContracts test.
        store = CredentialStore(self.path, wall=lambda: -10)
        with self.assertRaises(CredentialRefused) as caught:
            store.mint("plugin", "synthetic", ["read"], expires_at=-1)
        self.assertEqual(caught.exception.detail, "expiry_invalid")
        self.assertEqual(store.grants(), [])

    @NEEDS_POSIX
    def test_a_wall_clock_that_does_not_answer_a_number_is_a_typed_store_error(self) -> None:
        # #385's round-6 F5 (DeepSeek), owed here: a wall answering a non-number left int()'s error untyped.
        good = CredentialStore(self.path, wall=lambda: 1000.0)
        token, cred = good.mint("plugin", "synthetic", ["read"], expires_at=5000)
        for answer in ("x", None, float("nan"), float("inf")):
            with self.subTest(answer=repr(answer)):
                bad = CredentialStore(self.path, wall=lambda a=answer: a)
                with self.assertRaises(CredentialStoreError):
                    bad.mint("plugin", "other", ["read"])
                with self.assertRaises(CredentialStoreError):
                    bad.grant_scopes(cred.token_id, "plugin", "synthetic")
                with self.assertRaises(CredentialRefused) as caught:
                    bad.verify(token, peer="p")
                self.assertEqual(caught.exception.detail, "store_unavailable")
        self.assertEqual(len(CredentialStore(self.path).grants()), 1)
        self.assertEqual(good.verify(token, peer="p").token_id, cred.token_id)


@NEEDS_POSIX
class RoundNineCredentialsTest(unittest.TestCase):
    """Round 9 of #377: DeepSeek F2, Codex GPT-6 Astra 2 and GPT-6.1 Sol 2 (P3): a revocation asked for while the
    wall clock cannot be read is kept, dated at the grant's issue, and still persisted and noticed once."""

    def test_a_failed_clock_does_not_leave_a_requested_revocation_live(self):
        with tempfile.TemporaryDirectory() as folder:
            now = [1000]
            store = CredentialStore(os.path.join(folder, "synthetic.json"), wall=lambda: now[0])
            _, grant = store.mint("plugin", "synthetic", ["read"])
            now[0] = "broken"
            try:
                store.revoke(grant.token_id)
            except CredentialStoreError:
                pass
            now[0] = 1000
            self.assertEqual(store.grant_scopes(grant.token_id, "plugin", "synthetic"), frozenset())

    def test_a_failed_clock_and_a_failed_write_keep_the_revocation_for_a_retry(self):
        def unavailable():
            raise OSError("synthetic clock unavailable")
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "state", "synthetic.json")
            notices = []
            store = CredentialStore(path, wall=lambda: 1000, emit=lambda name, value: notices.append(name))
            _, grant = store.mint("device", "synthetic", ["read"])
            with mock.patch.object(store, "_wall", side_effect=unavailable), \
                    mock.patch.object(store, "_write", side_effect=CredentialStoreError("synthetic write failure")):
                with self.assertRaises(CredentialStoreError):
                    store.revoke(grant.token_id)
            self.assertIn(grant.token_id, store._unpersisted)
            first = store._grants[grant.token_id]["revoked_at"]
            self.assertEqual(first, grant.issued_at)
            self.assertNotIn("credential.revoked", notices)
            self.assertEqual(store.grant_scopes(grant.token_id, "device", "synthetic"), frozenset())
            with mock.patch.object(store, "_wall", return_value=1300):
                self.assertEqual(store.revoke(grant.token_id).revoked_at, first)
            self.assertEqual(notices.count("credential.revoked"), 1)
            self.assertEqual(CredentialStore(path).grants()[0].revoked_at, first)


class _ObservedLock:
    """Stands in for a store's lock: says when a caller starts to wait for it, then waits on the real one."""

    def __init__(self, real):
        self.real, self.waiting = real, threading.Event()

    def __enter__(self):
        self.waiting.set()
        self.real.acquire()
        return self

    def __exit__(self, *exc):
        self.real.release()
        return False

    def acquire(self, *args, **kwargs):
        return self.real.acquire(*args, **kwargs)

    def release(self):
        self.real.release()


@NEEDS_POSIX
class ClockInsideTheLockTest(unittest.TestCase):
    """The Devin review thread on #385: grant_scopes and held judged expiry by a clock read before they waited
    for the lock, so a grant that expired during the wait still answered its scopes."""

    def _store(self, folder, now):
        store = CredentialStore(os.path.join(folder, "synthetic.json"), wall=lambda: now[0])
        token, grant = store.mint("plugin", "synthetic", ["read"], expires_at=1100)
        return store, grant

    def _judged_after_a_wait(self, ask):
        with tempfile.TemporaryDirectory() as folder:
            now = [1000]
            store, grant = self._store(folder, now)
            real = store._lock
            store._lock = _ObservedLock(real)
            answer = []
            real.acquire()
            try:
                caller = threading.Thread(target=lambda: answer.append(ask(store, grant)))
                caller.start()
                self.assertTrue(store._lock.waiting.wait(10))
                now[0] = 2000   # the grant expires while the caller waits for the lock
            finally:
                real.release()
            caller.join(10)
            self.assertFalse(caller.is_alive())
            return answer

    def test_grant_scopes_judges_expiry_when_the_lock_is_taken(self):
        self.assertEqual(self._judged_after_a_wait(
            lambda store, grant: store.grant_scopes(grant.token_id, "plugin", "synthetic")), [frozenset()])

    def test_held_judges_expiry_when_the_lock_is_taken(self):
        self.assertEqual(self._judged_after_a_wait(lambda store, grant: store.held("plugin", "synthetic")),
                         [frozenset()])

    def test_a_clock_that_raises_inside_the_lock_leaves_it_free_and_the_store_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            now = [1000]
            store, grant = self._store(folder, now)
            path = os.path.join(folder, "synthetic.json")
            with open(path, "rb") as handle:
                before = handle.read()
            grants = store.grants()
            now[0] = "broken"
            for ask in (lambda: store.grant_scopes(grant.token_id, "plugin", "synthetic"),
                        lambda: store.held("plugin", "synthetic")):
                with self.assertRaises(CredentialStoreError):
                    ask()
                free = []
                other = threading.Thread(target=lambda: free.append(store._lock.acquire(timeout=5)))
                other.start()
                other.join(10)
                self.assertEqual(free, [True])
                store._lock.release()
            now[0] = 1000
            self.assertEqual(store.grants(), grants)
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), before)
            self.assertEqual(store.grant_scopes(grant.token_id, "plugin", "synthetic"), frozenset({"read"}))


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


@NEEDS_POSIX
class StorePathRefusalTest(unittest.TestCase):
    """The read of #385 by DeepSeek V4.1 Flash: an absolute pathlib path was refused as "must be absolute"."""

    def test_a_path_that_is_not_a_str_and_a_relative_path_are_refused_each_for_its_reason(self) -> None:
        import pathlib
        for path, reason in ((pathlib.PurePosixPath("/var/tmp/x/credentials.json"), "must be a str"),
                             (b"/var/tmp/x/credentials.json", "must be a str"), (None, "must be a str"),
                             ("credentials.json", "must be absolute")):
            with self.subTest(path=repr(path)):
                with self.assertRaises(CredentialStoreError) as caught:
                    CredentialStore(path)
                self.assertIn(reason, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
