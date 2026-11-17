"""The integration read of #385 against master (s6-int, GPT-6.1 Sol), as triaged by the Liaison on Nov 16.

F1, F2 and F3 are the read's ready tests (``s6-int-385-gpt-6.1-sol-evidence/test_s6_regressions.py``), kept as
written except that F1's schema read is skipped where the schema is not in the tree; the tests after them hold
the same rules without the schema. Synthetic stores only, under a temporary folder; no real credential.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import types
import unittest
from unittest import mock

from recursion_core.control import scopes
from recursion_core.control.credentials import CredentialStore
from recursion_core.control_transport import UnixControlServer
from recursion_core.control_transport import framing

POSIX = os.name != "nt" and hasattr(os, "getuid") and hasattr(os, "O_NOFOLLOW")
SCHEMA = Path(scopes.__file__).resolve().parents[3] / "schemas/control/v1/session.token.v1.json"
OUTSIDE = ("run.start", "approval.list", "workspace.restore", "key.list")


def _session(store, scopes_wanted=("read", "run")):
    _, grant = store.mint("session", "ses-a", list(scopes_wanted))
    return types.SimpleNamespace(kind="session", id="ses-a", in_run_unit=False, grant=grant.token_id)


@unittest.skipUnless(POSIX, "synthetic credential stores require POSIX")
class SessionContractTest(unittest.TestCase):
    def test_f1_session_grant_cannot_authorize_methods_outside_delegable_list(self):
        if not SCHEMA.exists():
            self.skipTest("schemas/control/v1/session.token.v1.json is not in this tree (SPINE's #368)")
        schema = json.loads(SCHEMA.read_text())
        delegable = set(schema["$defs"]["delegable_method"]["enum"])
        with tempfile.TemporaryDirectory(prefix="s6-session-") as folder:
            store = CredentialStore(os.path.join(folder, "synthetic.json"), wall=lambda: 1000)
            principal = _session(store)
            authorizer = scopes.Authorizer(scope_of=scopes.table_scope, credentials=store)
            for method in OUTSIDE:
                with self.subTest(method=method):
                    self.assertNotIn(method, delegable)
                    self.assertIsNotNone(authorizer(principal, method, {}))

    def test_the_delegable_set_is_the_schemas_list(self):
        if not SCHEMA.exists():
            self.skipTest("schemas/control/v1/session.token.v1.json is not in this tree (SPINE's #368)")
        schema = json.loads(SCHEMA.read_text())
        self.assertEqual(scopes.SESSION_DELEGABLE, frozenset(schema["$defs"]["delegable_method"]["enum"]))

    def test_a_session_grant_reaches_only_delegable_methods_and_its_stop(self):
        with tempfile.TemporaryDirectory(prefix="s6-session-") as folder:
            store = CredentialStore(os.path.join(folder, "synthetic.json"), wall=lambda: 1000)
            principal = _session(store)
            authorizer = scopes.Authorizer(scope_of=lambda method: "read", credentials=store)
            for method in OUTSIDE + ("run.status", "run.list", "approval.answer", "key.create"):
                with self.subTest(method=method):
                    self.assertEqual(authorizer(principal, method, {}), ("scope_refused", "method_not_delegable"))
            for method in sorted(scopes.SESSION_DELEGABLE):
                with self.subTest(method=method):
                    self.assertIsNone(authorizer(principal, method, {}))
            # run.stop is not refused as undelegable: it keeps the Stop row's own rule (no run record here).
            self.assertEqual(authorizer(principal, "run.stop", {"run_id": "r1"}), ("scope_refused", "run_not_owned"))

    def test_a_plugin_grant_is_not_held_to_the_session_list(self):
        with tempfile.TemporaryDirectory(prefix="s6-session-") as folder:
            store = CredentialStore(os.path.join(folder, "synthetic.json"), wall=lambda: 1000)
            store.mint("plugin", "p1", ["read", "run"])
            plugin = types.SimpleNamespace(kind="plugin", id="p1", in_run_unit=False)
            authorizer = scopes.Authorizer(scope_of=lambda method: "run", credentials=store)
            self.assertIsNone(authorizer(plugin, "run.start", {}))


@unittest.skipUnless(POSIX, "synthetic credential stores require POSIX")
class RefreshExpiryTest(unittest.TestCase):
    def test_f2_expiry_is_checked_after_refresh_finishes(self):
        for method in ("grant_scopes", "held"):
            with self.subTest(method=method):
                with tempfile.TemporaryDirectory(prefix="s6-expiry-") as folder:
                    now = [1000]
                    store = CredentialStore(os.path.join(folder, "synthetic.json"), wall=lambda: now[0])
                    _, grant = store.mint("plugin", "p1", ["read"], expires_at=1001)
                    refresh = store._refresh

                    def refresh_finishing_after_expiry():
                        refresh()
                        now[0] = 1002  # models stat/read being delayed across the expiry

                    with mock.patch.object(store, "_refresh", side_effect=refresh_finishing_after_expiry):
                        if method == "grant_scopes":
                            answer = store.grant_scopes(grant.token_id, "plugin", "p1")
                        else:
                            answer = store.held("plugin", "p1")
                    self.assertEqual(answer, frozenset())


@unittest.skipUnless(sys.platform.startswith("linux"), "real Linux Unix transport")
class FallbackFrameCapTest(unittest.TestCase):
    def serve(self, cap, handler):
        folder = tempfile.TemporaryDirectory(prefix="s6-frame-", dir=os.environ.get("TMPDIR"))
        self.addCleanup(folder.cleanup)
        server = UnixControlServer(os.path.join(folder.name, "run"), handler, max_frame_bytes=cap)
        server.start()
        self.addCleanup(server.close)
        return server

    def exchange(self, server, request, cap):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(2)
            conn.connect(server.path)
            conn.sendall(framing.encode_frame(request, max_bytes=cap))
            return framing.read_frame(conn, max_bytes=cap)

    def test_f3_error_reply_obeys_the_configured_frame_cap(self):
        seen = []

        def handler(message, connection):
            seen.append(message)
            return {"ok": True, "blob": "x" * 200}

        cap = 128
        request = {"op": "big", "id": "i" * 40}
        encoded = framing.encode_frame(request, max_bytes=cap)
        self.assertLess(len(encoded) - framing.HEADER.size, cap)
        server = self.serve(cap, handler)
        reply = self.exchange(server, request, cap)
        self.assertEqual(seen, [request])
        self.assertEqual(reply, {"ok": False, "error": {"code": "response_too_large"}, "id": request["id"]})
        self.assertLessEqual(len(framing.encode_frame(reply)) - framing.HEADER.size, cap)

    def test_an_id_too_long_to_echo_is_refused_before_the_handler(self):
        seen = []
        server = self.serve(128, lambda message, connection: seen.append(message) or {"ok": True})
        reply = self.exchange(server, {"op": "x", "id": "i" * 100}, 128)
        self.assertEqual(reply, {"ok": False, "error": {"code": "bad_frame", "detail": "id_too_large"}})
        self.assertEqual(seen, [])

    def test_an_id_that_fits_keeps_its_fallback_within_the_cap(self):
        seen = []

        def handler(message, connection):
            seen.append(message)
            return {"ok": True, "blob": "x" * 200}

        cap = 128
        request = {"op": "big", "id": "i" * 40}
        reply = self.exchange(self.serve(cap, handler), request, cap)
        self.assertEqual(reply, {"ok": False, "error": {"code": "response_too_large"}, "id": request["id"]})
        self.assertEqual(len(seen), 1)
        self.assertLessEqual(len(framing.encode_frame(reply)) - framing.HEADER.size, cap)

    def test_internal_error_fallback_at_a_small_cap_echoes_the_id(self):
        cap = 128
        request = {"op": "bad", "id": "i" * 40}

        def handler(message, connection):
            return {"ok": True, "n": float("nan")}

        reply = self.exchange(self.serve(cap, handler), request, cap)
        self.assertEqual(reply, {"ok": False, "error": {"code": "internal_error"}, "id": request["id"]})
        self.assertLessEqual(len(framing.encode_frame(reply)) - framing.HEADER.size, cap)

    def test_a_cap_that_cannot_carry_the_typed_refusal_is_rejected(self):
        from recursion_core.control_transport import unix as unix_mod

        folder = tempfile.TemporaryDirectory(prefix="s6-floor-", dir=os.environ.get("TMPDIR"))
        self.addCleanup(folder.cleanup)
        path = os.path.join(folder.name, "run")
        for cap in (40, 60, 64):
            with self.subTest(cap=cap):
                with self.assertRaises(ValueError):
                    UnixControlServer(path, lambda message, connection: {"ok": True}, max_frame_bytes=cap)
        floor = unix_mod._MIN_FRAME_BYTES
        seen: list = []
        server = self.serve(floor, lambda message, connection: seen.append(message) or {"ok": True})
        reply = self.exchange(server, {"op": "x", "id": "i" * 40}, floor)
        self.assertEqual(seen, [])
        self.assertEqual(reply, {"ok": False, "error": {"code": "bad_frame", "detail": "id_too_large"}})
        self.assertLessEqual(len(framing.encode_frame(reply)) - framing.HEADER.size, floor)


if __name__ == "__main__":
    unittest.main()
