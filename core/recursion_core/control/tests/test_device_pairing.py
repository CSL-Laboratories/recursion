"""Stop-only device scope, device.pair and device.revoke, and the device credential on the socket.

The running daemon (7a) binds a principal from the peer and asks the authorizer before the method
runs. This draft adds the stop scope, pairing and revocation, and a device token on the frame.
A peer with the owner's uid that is inside a run unit is refused every method: the test fails if
``in_run_unit`` is ignored. The relay's own hello checks are another lane.
"""
from __future__ import annotations

import inspect
import os
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

from recursion_core.control.credentials import CredentialRefused, CredentialStore, check_grant
from recursion_core.control.hooks import Principal
from recursion_core.control.methods import METHODS
from recursion_core.control.scopes import Authorizer
from recursion_core.control_transport.handlers import Connection
from recursion_core.control_transport.peercred import PeerCred
from recursion_core.daemon import Daemon, Registry
from recursion_core.daemon import control_adapter as adapter
from recursion_core.daemon.tests.test_composition import BOOT, ProcessUnitManager, Units

LINUX = sys.platform.startswith("linux")
LINUX_ONLY = ("Linux-only: the composed daemon needs AF_UNIX peer credentials")
DAY = 24 * 60 * 60


class Clock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _peer(uid, pid):
    return Connection(conn_id=1, peer=PeerCred(pid=pid, uid=uid, gid=os.getegid()))


class _Grants:
    def __init__(self, scopes):
        self._scopes = frozenset(scopes)

    def held(self, kind, principal_id):
        return frozenset()

    def grant_scopes(self, grant, kind, principal_id):
        return self._scopes if grant == "g" else frozenset()


def _device(scopes):
    return types.SimpleNamespace(kind="device", id="dev-1", in_run_unit=False, grant="g")


class StopScopeTest(unittest.TestCase):
    """``run.stop`` passes on ``stop`` or ``run``. Watch and Stop do not include run or approve."""

    def _auth(self, scopes):
        return Authorizer(scope_of=lambda method: getattr(METHODS.get(method), "scope", None),
                          credentials=_Grants(scopes))

    def test_stop_allowed_with_stop_scope(self) -> None:
        stop_only = self._auth({"read", "stop"})
        device = _device({"read", "stop"})
        self.assertIsNone(stop_only(device, "run.stop", {"run_id": "run-x"}))
        self.assertIsNone(stop_only(device, "run.list", {}))
        read_only = self._auth({"read"})
        self.assertEqual(read_only(device, "run.stop", {"run_id": "run-x"}), ("scope_refused", "needs_run"))

    def test_approve_refused_scope_refused(self) -> None:
        auth = self._auth({"read", "stop"})
        device = _device({"read", "stop"})
        self.assertEqual(auth(device, "approval.answer", {}), ("scope_refused", "needs_approve"))
        self.assertEqual(auth(device, "run.start", {}), ("scope_refused", "needs_run"))

    def test_stop_scope_can_be_minted_for_a_device(self) -> None:
        self.assertEqual(check_grant("device", ["read", "stop"]), frozenset({"read", "stop"}))
        with self.assertRaises(CredentialRefused) as caught:
            check_grant("plugin", ["stop"])
        self.assertEqual(caught.exception.code, "scope_refused")


class DigestGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="pair-digest-")
        self.addCleanup(shutil.rmtree, self.base, True)
        os.chmod(self.base, 0o700)
        self.path = os.path.join(self.base, "credentials.json")

    def test_a_digest_mismatch_is_refused(self) -> None:
        """After the hash lookup, the stored digest is compared again. A mismatch is not a live token."""
        store = CredentialStore(self.path)
        token, credential = store.mint("device", "dev-1", ["read"])
        store._grants[credential.token_id]["token_sha256"] = "0" * 64
        with self.assertRaises(CredentialRefused) as caught:
            store.verify(token, peer=os.getpid())
        self.assertEqual(caught.exception.detail, "token_unknown")


class SettledValueTest(unittest.TestCase):
    """The control side names the same pairing values, in one place, and uses them."""

    def _pairing(self):
        from recursion_core.control import device_pairing as pairing
        return pairing

    def test_settled_pairing_values_are_named_once(self) -> None:
        pairing = self._pairing()
        self.assertEqual(pairing.PAIRING_MAX_AGE_S, 30 * DAY)
        self.assertEqual(pairing.PAIRING_UNUSED_AGE_S, 7 * DAY)
        self.assertEqual(pairing.DANGEROUS_ASSERTION_TTL_S, 60.0)
        self.assertEqual(pairing.DEVICE_SCOPES, ("read", "stop"))
        self.assertEqual(pairing.DANGEROUS_ACTIONS, ("run", "approve"))
        self.assertIs(pairing.pairing_lapsed.__module__, pairing.__name__)

    def test_pairing_lapses_at_the_bounds(self) -> None:
        lapsed = self._pairing().pairing_lapsed
        self.assertFalse(lapsed(0, None, 7 * DAY - 1))
        self.assertTrue(lapsed(0, None, 7 * DAY))
        self.assertFalse(lapsed(0, 7 * DAY - 1, 14 * DAY - 2))
        self.assertTrue(lapsed(0, 7 * DAY - 1, 14 * DAY - 1))
        self.assertFalse(lapsed(0, 24 * DAY, 30 * DAY - 1))
        self.assertTrue(lapsed(0, 29 * DAY, 30 * DAY))

    def test_assertion_is_one_action_and_expires_at_sixty_seconds(self) -> None:
        pairing = self._pairing()
        clock = Clock(1_000.0)
        book = pairing.AssertionBook(clock=clock)
        issued = book.issue("run")
        clock.now = 1_000.0 + 59
        book.accept(issued, "run")
        with self.assertRaises(pairing.PairingRefused) as replayed:
            book.accept(issued, "run")
        self.assertEqual(replayed.exception.code, "assertion_replayed")
        other = book.issue("approve")
        with self.assertRaises(pairing.PairingRefused) as wrong:
            book.accept(other, "run")
        self.assertEqual(wrong.exception.code, "assertion_wrong_action")
        # A wrong action does not spend the assertion: the matching action still can, inside the window.
        book.accept(other, "approve")
        late = book.issue("run")
        clock.now = clock.now + 60
        with self.assertRaises(pairing.PairingRefused) as expired:
            book.accept(late, "run")
        self.assertEqual(expired.exception.code, "assertion_expired")
        with self.assertRaises(pairing.PairingRefused) as not_dangerous:
            book.issue("stop")
        self.assertEqual(not_dangerous.exception.code, "action_not_dangerous")


def _refusal(reply):
    if reply.get("ok") is False:
        error = reply.get("error") or {}
        return error.get("code"), error.get("detail")
    result = reply.get("result") if isinstance(reply.get("result"), dict) else {}
    return result.get("code"), result.get("detail")


@unittest.skipUnless(LINUX, LINUX_ONLY)
class DaemonDevicePairingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rc-device-pair-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)
        self.state = self.tmp / "state"
        self.runtime = self.tmp / "rt"
        self.runtime.mkdir(mode=0o700)
        self.environ = {"PATH": "/usr/bin:/bin", "HOME": str(self.tmp), "LANG": "C.UTF-8",
                        "XDG_RUNTIME_DIR": str(self.runtime), "XDG_CONFIG_HOME": str(self.tmp / "config")}
        self.units = Units()
        self.clock = Clock()
        self.open = []
        self.addCleanup(self.shutdown)
        self.calls = []

    def boot(self):
        daemon = Daemon(Registry(self.state), ProcessUnitManager(self.units), boot_id=lambda: BOOT)
        daemon.sweep()
        kwargs = {}
        if "wall" in inspect.signature(adapter.compose_control).parameters:
            kwargs["wall"] = self.clock
        composed = adapter.compose_control(daemon, self.state, environ=self.environ, routes={}, **kwargs)
        composed.control.reconcile()
        real = composed.control.api.call

        def wrapped(method, params=None, _real=real):
            self.calls.append(method)
            return _real(method, params)

        composed.control.api.call = wrapped
        self.open.append((daemon, composed))
        return composed

    def shutdown(self):
        while self.open:
            daemon, composed = self.open.pop()
            composed.close()
            daemon.close()

    def owner(self):
        return _peer(os.geteuid(), os.getpid())

    def other(self):
        return _peer(os.geteuid() + 1, os.getpid())

    def pair(self, composed, scopes=("read", "stop"), label="phone", expires_at=None):
        params = {"label": label, "scopes": list(scopes)}
        if expires_at is not None:
            params["expires_at"] = expires_at
        return composed.control.handle(
            {"op": "control", "method": "device.pair", "params": params, "id": "pair"}, self.owner())

    def token_of(self, reply):
        self.assertTrue(reply.get("ok") is True, reply)
        result = reply["result"]
        self.assertEqual(result.get("code"), "ok", reply)
        self.assertTrue(result.get("token") and result.get("device_id"), reply)
        return result["device_id"], result["token"]

    def frame(self, composed, token, method, *, claim, params=None, peer=None, assertion=None, extra=None):
        message = {"op": "control", "method": method, "params": {} if params is None else params,
                   "id": "frame", "auth": token, "device_claim": claim}
        if assertion is not None:
            message["assertion"] = assertion
        if extra:
            message.update(extra)
        return composed.control.handle(message, self.owner() if peer is None else peer)

    def test_frame_claiming_other_device_refused(self) -> None:
        composed = self.boot()
        device_id, token = self.token_of(self.pair(composed))
        self.calls.clear()
        reply = self.frame(composed, token, "run.list", claim="dev-other")
        self.assertEqual(_refusal(reply), ("device_claim_mismatch", "device_claim_mismatch"))
        self.assertEqual(self.calls, [])
        allowed = self.frame(composed, token, "run.list", claim=device_id)
        self.assertEqual(_refusal(allowed)[0], "ok")

    def test_approve_and_run_refused_for_a_paired_device(self) -> None:
        composed = self.boot()
        device_id, token = self.token_of(self.pair(composed))
        self.calls.clear()
        approve = self.frame(composed, token, "approval.answer", claim=device_id, params={})
        start = self.frame(composed, token, "run.start", claim=device_id, params={})
        self.assertEqual(_refusal(approve), ("scope_refused", "needs_approve"))
        self.assertEqual(_refusal(start), ("scope_refused", "needs_run"))
        self.assertEqual(self.calls, [])

    def test_stop_allowed_with_stop_scope_on_the_socket(self) -> None:
        composed = self.boot()
        device_id, token = self.token_of(self.pair(composed))
        self.calls.clear()
        stopped = self.frame(composed, token, "run.stop", claim=device_id, params={"run_id": "run-absent"})
        self.assertEqual(_refusal(stopped)[0], "run_unknown")
        self.assertEqual(self.calls, ["run.stop"])
        read_id, read_token = self.token_of(self.pair(composed, scopes=("read",), label="watch"))
        self.calls.clear()
        refused = self.frame(composed, read_token, "run.stop", claim=read_id, params={"run_id": "run-absent"})
        self.assertEqual(_refusal(refused), ("scope_refused", "needs_run"))
        self.assertEqual(self.calls, [])

    def test_next_frame_after_revoke_refused(self) -> None:
        composed = self.boot()
        device_id, token = self.token_of(self.pair(composed))
        first = self.frame(composed, token, "run.list", claim=device_id)
        self.assertEqual(_refusal(first)[0], "ok")
        revoked = composed.control.handle(
            {"op": "control", "method": "device.revoke", "params": {"device_id": device_id}, "id": "revoke"},
            self.owner())
        self.assertEqual(_refusal(revoked)[0], "ok", revoked)
        second = self.frame(composed, token, "run.list", claim=device_id)
        self.assertEqual(_refusal(second)[0], "token_revoked")
        self.assertNotEqual(_refusal(second)[0], "ok")

    def test_replayed_token_of_revoked_device_refused(self) -> None:
        composed = self.boot()
        device_id, token = self.token_of(self.pair(composed))
        composed.control.handle(
            {"op": "control", "method": "device.revoke", "params": {"device_id": device_id}}, self.owner())
        path = os.path.join(str(self.state), "control", "credentials.json")
        fresh = CredentialStore(path, wall=self.clock)
        with self.assertRaises(CredentialRefused) as caught:
            fresh.verify(token, peer=os.getpid())
        self.assertEqual(caught.exception.detail, "token_revoked")
        with self.assertRaises(Exception) as again:
            composed.control._pairing.mint_device(device_id, ["read", "stop"], label="phone", expires_at=None)
        self.assertEqual(getattr(again.exception, "code", None), "device_revoked")

    def test_request_field_named_like_principal_ignored_or_refused(self) -> None:
        composed = self.boot()
        device_id, token = self.token_of(self.pair(composed))
        seen = []
        real = composed.control._hooks.authorize

        def spy(principal, method, params, _real=real):
            seen.append((principal, method, dict(params)))
            return _real(principal, method, params)

        composed.control._hooks.authorize = spy
        reply = self.frame(
            composed, token, "run.list", claim=device_id,
            params={"principal": "owner", "device_id": "other", "grant": "not-the-grant"})
        self.assertEqual(_refusal(reply)[0], "ok")
        principal, method, params = seen[-1]
        self.assertEqual(principal.kind, "device")
        self.assertEqual(principal.id, device_id)
        self.assertNotEqual(principal.id, "other")
        self.assertNotEqual(getattr(principal, "grant", None), "not-the-grant")
        self.assertEqual(principal.public(), {"kind": "device", "id": device_id})
        self.assertEqual(params["principal"], "owner")  # the params are unchanged and unread for identity
        forged = composed.control.handle(
            {"op": "control", "method": "run.list", "params": {}, "principal": "owner"}, self.owner())
        self.assertEqual(_refusal(forged), ("request_invalid", "envelope_field"))

    def test_device_pair_refused_to_device_and_plugin(self) -> None:
        composed = self.boot()
        self.calls.clear()
        device = composed.control.handle(
            {"op": "control", "method": "device.pair", "params": {"label": "phone", "scopes": ["read", "stop"]}},
            self.other())
        self.assertEqual(_refusal(device)[0], "scope_refused")
        self.assertEqual(self.calls, [])
        plugin = composed.control._hooks.authorize(Principal("plugin", "plugin.alpha"), "device.pair", {})
        self.assertEqual(plugin["code"], "scope_refused")
        revoked = composed.control.handle(
            {"op": "control", "method": "device.revoke", "params": {"device_id": "dev-nope"}}, self.other())
        self.assertEqual(_refusal(revoked)[0], "scope_refused")

    def test_pair_refuses_run_approve_manage(self) -> None:
        composed = self.boot()
        for scopes in (["run"], ["approve"], ["manage"], ["read", "run"], ["read", "approve"], ["stop", "manage"],
                       ["observe"], []):
            with self.subTest(scopes=scopes):
                reply = self.pair(composed, scopes=scopes)
                self.assertEqual(_refusal(reply)[0], "scope_refused", reply)
        allowed = self.pair(composed, scopes=["stop", "read"])
        self.assertEqual(_refusal(allowed)[0], "ok", allowed)

    def test_store_unreadable_refuses(self) -> None:
        composed = self.boot()
        device_id, token = self.token_of(self.pair(composed))
        path = os.path.join(str(self.state), "control", "credentials.json")
        os.chmod(path, 0o644)
        os.utime(path, ns=(1, 1))
        self.calls.clear()
        reply = self.frame(composed, token, "run.list", claim=device_id)
        self.assertEqual(_refusal(reply)[0], "store_unavailable", reply)
        self.assertEqual(self.calls, [])
        # The owner, with no token, is still the owner. A bad store is not read as him, and it does not
        # take the socket's owner path away.
        listed = composed.control.handle({"op": "control", "method": "run.list", "params": {}}, self.owner())
        self.assertEqual(_refusal(listed)[0], "ok")
        os.chmod(path, 0o600)

    def test_engine_peer_with_valid_token_refused(self) -> None:
        composed = self.boot()
        device_id, token = self.token_of(self.pair(composed))
        self.calls.clear()
        with mock.patch.object(adapter, "peer_is_engine", return_value=True):
            reply = self.frame(composed, token, "run.list", claim=device_id)
        self.assertEqual(_refusal(reply), ("scope_refused", "peer is inside a run unit"))
        self.assertEqual(self.calls, [])

    def test_owner_uid_inside_a_run_unit_is_refused_every_method(self) -> None:
        """7a's check. Fails if the daemon drops ``in_run_unit`` or the authorizer ignores it.

        The socket's accept path also closes an engine peer. This call goes through ``handle``, so that
        earlier close is not what refuses the method.
        """
        composed = self.boot()
        methods = ("run.list", "run.status", "run.start", "run.stop", "route.list", "plugin.list",
                   "approval.answer", "approval.list", "workspace.publish", "device.pair", "device.revoke",
                   "no.such.method")
        self.calls.clear()
        with mock.patch.object(adapter, "peer_is_engine", return_value=True):
            for method in methods:
                reply = composed.control.handle(
                    {"op": "control", "method": method, "params": {"run_id": "run-x"}}, self.owner())
                self.assertEqual(_refusal(reply), ("scope_refused", "peer is inside a run unit"), method)
        self.assertEqual(self.calls, [])

    def test_bad_auth_is_never_the_owner(self) -> None:
        composed = self.boot()
        self.calls.clear()
        reply = composed.control.handle(
            {"op": "control", "method": "run.list", "params": {}, "auth": "not-a-token"}, self.owner())
        self.assertNotEqual(_refusal(reply)[0], "ok")
        self.assertIn(_refusal(reply)[0], ("token_malformed", "token_unknown", "store_unavailable"))
        self.assertEqual(self.calls, [])

    def test_same_uid_with_a_device_token_is_that_device(self) -> None:
        composed = self.boot()
        device_id, token = self.token_of(self.pair(composed))
        reply = self.frame(composed, token, "device.pair", claim=device_id,
                           params={"label": "second", "scopes": ["read"]})
        self.assertEqual(_refusal(reply)[0], "scope_refused")
        watched = self.frame(composed, token, "run.list", claim=device_id)
        self.assertEqual(_refusal(watched)[0], "ok")

    def test_pairing_lapses_at_seven_days_unused_and_at_thirty_days(self) -> None:
        composed = self.boot()
        issued = int(self.clock.now)
        # Both devices are paired before the clock moves. The idle one is not used at all.
        used_id, used_token = self.token_of(self.pair(composed, label="phone"))
        idle_id, idle_token = self.token_of(self.pair(composed, label="idle"))
        self.clock.now = issued + 7 * DAY - 1
        self.assertEqual(_refusal(self.frame(composed, used_token, "run.list", claim=used_id))[0], "ok")
        self.clock.now = issued + 7 * DAY
        self.calls.clear()
        refused = self.frame(composed, idle_token, "run.list", claim=idle_id)
        self.assertEqual(_refusal(refused)[0], "pairing_lapsed", refused)
        self.assertEqual(self.calls, [])
        # The used device was accepted one second earlier, so it is still inside seven days.
        self.assertEqual(_refusal(self.frame(composed, used_token, "run.list", claim=used_id))[0], "ok")
        self.clock.now = issued + 14 * DAY
        self.assertEqual(_refusal(self.frame(composed, used_token, "run.list", claim=used_id))[0], "pairing_lapsed")
        # A device that keeps being used still ends at 30 days from its own pairing.
        start = int(self.clock.now)
        kept_id, kept_token = self.token_of(self.pair(composed, label="kept"))
        self.assertEqual(_refusal(self.frame(composed, kept_token, "run.list", claim=kept_id))[0], "ok")
        for step in (6, 12, 18, 24):
            self.clock.now = start + step * DAY
            self.assertEqual(
                _refusal(self.frame(composed, kept_token, "run.list", claim=kept_id))[0], "ok", step)
        self.clock.now = start + 30 * DAY - 1
        self.assertEqual(_refusal(self.frame(composed, kept_token, "run.list", claim=kept_id))[0], "ok")
        self.clock.now = start + 30 * DAY
        self.assertEqual(
            _refusal(self.frame(composed, kept_token, "run.list", claim=kept_id))[0], "pairing_lapsed")
        too_long = self.pair(composed, expires_at=int(self.clock.now) + 30 * DAY + 1)
        self.assertEqual(_refusal(too_long)[0], "expiry_invalid", too_long)
        capped = self.pair(composed)
        _, credential_token = self.token_of(capped)
        store = composed.control._pairing.store
        credential = store.verify(credential_token, peer=os.getpid())
        self.assertEqual(credential.expires_at - credential.issued_at, 30 * DAY)

    def test_a_second_caller_still_runs_after_a_refusal(self) -> None:
        composed = self.boot()
        device_id, token = self.token_of(self.pair(composed))
        self.frame(composed, token, "run.start", claim=device_id)
        listed = composed.control.handle({"op": "control", "method": "run.list", "params": {}}, self.owner())
        self.assertEqual(_refusal(listed)[0], "ok")


if __name__ == "__main__":
    unittest.main()
