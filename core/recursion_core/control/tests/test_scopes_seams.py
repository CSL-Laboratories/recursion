"""The authorizer registered into seam S5, through SPINE's real hooks (control/hooks.py, #369 08b4b1d6).

What a lane needs to test a hook (control/hooks.py, "BUILDING AGAINST THIS FILE"): make ``Hooks()``, call the
lane's own ``register(hooks)``, ``freeze()``, and call the hook. ``hooks.authorize`` is the call every entry
point makes (the socket, the page's three paths and in-process calls, once the seams' daemon edits land); its
answer here is the answer there. The entry points themselves are tested when those edits are on master.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
from dataclasses import dataclass

from recursion_core.control import hooks as H
from recursion_core.control import scopes
from recursion_core.control.credentials import CredentialStore
from recursion_core.control.methods import METHODS
from recursion_core.control.tests.test_scopes import SCOPE_COLUMN


@dataclass(frozen=True)
class Granted:
    """A principal made from a verified token, as this lane asks SPINE to let ``Principal`` carry it: SPINE's
    fields (kind, id, in_run_unit) and ``grant``, the verified grant's id. Until SPINE's ``Principal`` has that
    field, a token principal built with it holds nothing (``BearerWithoutGrantTest``)."""
    kind: str
    id: str | None = None
    in_run_unit: bool = False
    grant: str | None = None

POSIX = os.name != "nt" and hasattr(os, "getuid") and hasattr(os, "O_NOFOLLOW")
NEEDS_POSIX = unittest.skipUnless(POSIX, "the credential store needs uids and O_NOFOLLOW (POSIX)")

RECORDS = {
    "run-legacy": {"run_id": "run-legacy", "state": "running"},
    "run-alpha": {"run_id": "run-alpha", "principal": {"kind": "plugin", "id": "plugin.alpha"},
                  "launch": {"owner": "plugin.alpha", "session_id": None}},
    "run-host": {"run_id": "run-host", "principal": {"kind": "owner", "id": "local"},
                 "launch": {"owner": "host", "session_id": "s1"}},
}


class Registry:
    """The control registry as the service reads it: ``get(run_id) -> a copy of the record, or None``."""

    def get(self, run_id):
        record = RECORDS.get(run_id)
        return dict(record) if record is not None else None


def composed() -> H.Hooks:
    hooks = H.Hooks()
    scopes.register(hooks)
    hooks.freeze()
    return hooks


def with_service() -> tuple[H.Hooks, scopes.ScopesService]:
    """The authorizer in S5 and its service, as the change that registers the service will compose them."""
    hooks = H.Hooks()
    authorizer = scopes.Authorizer(scope_of=scopes.table_scope)
    service = scopes.ScopesService(authorizer)
    hooks.set("authorize", authorizer)
    hooks.service(service)
    hooks.freeze()
    return hooks, service


def refused(detail: str) -> dict:
    return {"code": "scope_refused", "detail": detail}


class RegistrationTest(unittest.TestCase):
    def test_register_sets_the_authorizer_and_nothing_else(self) -> None:
        hooks = H.Hooks()
        scopes.register(hooks)
        self.assertIsInstance(hooks._single.get("authorize"), scopes.Authorizer)
        self.assertEqual(set(hooks._single), {"authorize"})
        self.assertEqual(hooks._services, [])          # the service is not registered yet: the gate stays as it was
        self.assertEqual(hooks.methods.registered(), ())
        self.assertEqual(hooks.commands.names(), ())
        hooks.freeze()
        self.assertFalse(hooks.gate.enabled)

    def test_the_composition_imports_this_registrant_and_s5_is_this_authorizer(self) -> None:
        hooks = H.compose()
        authorizer = hooks._single.get("authorize")
        # By module and name: SPINE's own tests may load control.scopes again, which makes a new class object.
        self.assertEqual((type(authorizer).__module__, type(authorizer).__qualname__),
                         ("recursion_core.control.scopes", "Authorizer"))
        self.assertEqual(hooks.authorize(H.Principal("device", "phone-9"), "run.list", {}), refused("needs_read"))
        self.assertIsNone(hooks.authorize(H.OWNER, "run.list", {}))

    def test_the_scope_of_every_row_is_the_method_tables(self) -> None:
        for method, row in METHODS.items():
            self.assertEqual(scopes.table_scope(method), row.scope, method)
        self.assertIsNone(scopes.table_scope("no.such.method"))
        # The runtime table (#369, draft 9) and the published one this lane's first tests copied (#368 c77aa2e0,
        # draft 10) agree on every scope. A difference is a failure of the pins, not of this lane.
        self.assertEqual({method: row.scope for method, row in METHODS.items()}, SCOPE_COLUMN)


class AsRegisteredTest(unittest.TestCase):
    """As registered (no service): no credential is honoured and no record is read (the stricter state)."""

    def test_only_owner_internal_and_the_page_hold_scopes(self) -> None:
        hooks = composed()
        self.assertIsNone(hooks.authorize(H.OWNER, "workspace.publish", {}))
        self.assertIsNone(hooks.authorize(H.Principal("internal", "quota"), "approval.answer", {}))
        self.assertIsNone(hooks.authorize(H.DEVICE, "approval.answer", {}))
        self.assertEqual(hooks.authorize(H.DEVICE, "workspace.publish", {}), refused("needs_manage"))
        self.assertEqual(hooks.authorize(H.Principal("plugin", "plugin.alpha"), "run.list", {}), refused("needs_read"))
        self.assertEqual(hooks.authorize(H.Principal("session", "s1"), "run.stop", {"run_id": "run-host"}),
                         refused("needs_run"))

    def test_a_run_unit_peer_is_refused_whoever_it_is(self) -> None:
        hooks = composed()
        for principal in (H.Principal("owner", "local", in_run_unit=True), H.Principal("device", "page", True)):
            self.assertEqual(hooks.authorize(principal, "run.stop", {"run_id": "run-legacy"}),
                             refused("peer is inside a run unit"))

    def test_owner_and_page_stop_any_run_and_the_recovery_stop_is_not_a_client_call(self) -> None:
        hooks = composed()
        for principal in (H.OWNER, H.DEVICE):
            for run_id in ("run-legacy", "run-alpha", "run-host", "run-unknown"):
                self.assertIsNone(hooks.authorize(principal, "run.stop", {"run_id": run_id}))


@NEEDS_POSIX
class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="scopes-svc-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.state = os.path.join(self.base, "state")
        os.mkdir(self.state, 0o700)
        self.hooks, self.service = with_service()

    def context(self, **kwargs) -> H.ServiceContext:
        kwargs.setdefault("state_dir", self.state)
        kwargs.setdefault("control_registry", Registry())
        return H.ServiceContext(**kwargs)

    def mint(self, kind, principal_id, scopes_):
        store = CredentialStore(os.path.join(self.state, "control", "credentials.json"))
        return store.mint(kind, principal_id, scopes_)

    def test_started_it_honours_live_grants_and_reads_records_for_stop(self) -> None:
        self.mint("plugin", "plugin.alpha", ["read", "run"])
        _, session_grant = self.mint("session", "s1", ["read", "run"])
        self.service.start(self.context())
        self.assertTrue(self.service.ready)
        plugin, session = H.Principal("plugin", "plugin.alpha"), Granted("session", "s1", grant=session_grant.token_id)
        self.assertIsNone(self.hooks.authorize(plugin, "run.stop", {"run_id": "run-alpha"}))
        self.assertEqual(self.hooks.authorize(plugin, "run.stop", {"run_id": "run-host"}), refused("run_not_owned"))
        self.assertEqual(self.hooks.authorize(plugin, "run.stop", {"run_id": "run-legacy"}), refused("run_not_owned"))
        self.assertIsNone(self.hooks.authorize(session, "run.stop", {"run_id": "run-host"}))
        self.assertEqual(self.hooks.authorize(session, "run.stop", {"run_id": "run-alpha"}), refused("run_outside_session"))
        self.assertEqual(self.hooks.authorize(plugin, "approval.answer", {}), refused("needs_approve"))
        self.assertEqual(stat_mode(os.path.join(self.state, "control")), 0o700)
        self.service.stop(self.context())
        self.assertFalse(self.service.ready)
        self.assertEqual(self.hooks.authorize(plugin, "run.list", {}), refused("needs_read"))   # detached

    def test_a_store_that_cannot_be_used_never_stops_the_daemon_and_honours_no_credential(self) -> None:
        self.mint("plugin", "plugin.alpha", ["read", "run"])
        os.chmod(os.path.join(self.state, "control"), 0o755)
        with self.assertLogs("recursion_core.control.scopes", "ERROR"):
            self.service.start(self.context())
        self.assertTrue(self.service.ready)
        self.assertEqual(self.hooks.authorize(H.Principal("plugin", "plugin.alpha"), "run.list", {}), refused("needs_read"))
        self.assertIsNone(self.hooks.authorize(H.OWNER, "run.stop", {"run_id": "run-alpha"}))

    def test_no_state_directory_or_no_registry_is_the_stricter_state(self) -> None:
        with self.assertLogs("recursion_core.control.scopes", "ERROR"):
            self.service.start(self.context(state_dir=None, control_registry=None))
        self.assertTrue(self.service.ready)
        self.assertEqual(self.hooks.authorize(H.Principal("plugin", "plugin.alpha"), "run.stop", {"run_id": "run-alpha"}),
                         refused("needs_run"))
        self.assertIsNone(self.hooks.authorize(H.DEVICE, "run.stop", {"run_id": "run-alpha"}))

    def test_a_started_service_without_a_registry_reads_no_record(self) -> None:
        self.mint("plugin", "plugin.alpha", ["run"])
        self.service.start(self.context(control_registry=object()))
        self.assertEqual(self.hooks.authorize(H.Principal("plugin", "plugin.alpha"), "run.stop", {"run_id": "run-alpha"}),
                         refused("run_not_owned"))


@NEEDS_POSIX
class ServiceStoreFailureTest(unittest.TestCase):
    """The reads of #385 (Astra R2, Sol R4): a store the service cannot use, malformed included, is logged and
    leaves token principals holding nothing; start never raises."""

    def test_a_malformed_record_is_logged_and_the_service_still_starts(self) -> None:
        base = tempfile.mkdtemp(prefix="scopes-svc-bad-")
        self.addCleanup(shutil.rmtree, base, True)
        private = os.path.join(base, "state", "control")
        os.makedirs(private, 0o700)
        os.chmod(os.path.dirname(private), 0o700)
        path = os.path.join(private, "credentials.json")
        for bad_kind in ([], {}, 5):
            with self.subTest(kind=repr(bad_kind)):
                record = {"token_id": "ctk-x", "token_sha256": "0" * 64, "kind": bad_kind, "id": "x",
                          "scopes": ["read"], "issued_at": 0, "expires_at": None, "revoked_at": None}
                with open(path, "w", encoding="ascii") as handle:
                    json.dump({"version": 1, "credentials": [record]}, handle)
                os.chmod(path, 0o600)
                hooks, service = with_service()
                with self.assertLogs("recursion_core.control.scopes", "ERROR"):
                    service.start(H.ServiceContext(state_dir=os.path.join(base, "state"), control_registry=Registry()))
                self.assertTrue(service.ready)
                self.assertEqual(hooks.authorize(H.Principal("plugin", "x"), "run.list", {}), refused("needs_read"))
                self.assertIsNone(hooks.authorize(H.OWNER, "run.list", {}))


class ServiceStartNeverRaisesTest(unittest.TestCase):
    """The read of #385 by DeepSeek V4.1 Flash, F3: a state_dir that is not a path raised TypeError out of start."""

    def test_a_state_dir_that_is_not_a_path_is_logged_and_honours_no_credential(self) -> None:
        for state_dir in (5, b"x", ["x"], "/var/tmp/a\x00b"):
            with self.subTest(state_dir=repr(state_dir)):
                hooks, service = with_service()
                with self.assertLogs("recursion_core.control.scopes", "ERROR"):
                    service.start(H.ServiceContext(state_dir=state_dir, control_registry=Registry()))
                self.assertTrue(service.ready)
                self.assertEqual(hooks.authorize(H.Principal("plugin", "plugin.alpha"), "run.list", {}),
                                 refused("needs_read"))
                self.assertIsNone(hooks.authorize(H.OWNER, "run.list", {}))

    def test_a_method_that_is_not_a_str_has_no_row(self) -> None:
        for method in ([], {}, 5, None, ("run.list",)):
            self.assertIsNone(scopes.table_scope(method), repr(method))


class _DetachedAfterOneRead(scopes.Authorizer):
    """The authorizer, except that its store and its run reader each answer one read and then read as None: a
    service stop landing between two reads of one call. Deterministic, where the race needs 3.14t and luck."""

    @property
    def _credentials(self):
        value, self._store_once = self._store_once, None
        return value

    @_credentials.setter
    def _credentials(self, value):
        self._store_once = value

    @property
    def _run_record(self):
        value, self._reader_once = self._reader_once, None
        return value

    @_run_record.setter
    def _run_record(self, value):
        self._reader_once = value


@NEEDS_POSIX
class DetachWhileCallingTest(unittest.TestCase):
    """The read of #385 by DeepSeek V4.1 Flash, F4: held() read the store twice, so a stop between the reads
    raised AttributeError and S5 answered authorizer_failed (credentials_unavailable since bc3ea902)."""

    def setUp(self) -> None:
        base = tempfile.mkdtemp(prefix="scopes-detach-")
        self.addCleanup(shutil.rmtree, base, True)
        state = os.path.join(base, "state")
        os.mkdir(state, 0o700)
        self.store = CredentialStore(os.path.join(state, "control", "credentials.json"))
        self.store.mint("plugin", "plugin.alpha", ["read", "run"])
        _, self.session_grant = self.store.mint("session", "s1", ["read", "run"])

    def test_a_token_principal_reads_the_store_once(self) -> None:
        session = Granted("session", "s1", grant=self.session_grant.token_id)
        authorizer = _DetachedAfterOneRead(scope_of=scopes.table_scope)
        authorizer.attach(credentials=self.store, run_record=None)
        self.assertIsNone(authorizer(session, "run.list", {}))

    def test_one_call_reads_the_store_and_the_run_reader_once(self) -> None:
        plugin = H.Principal("plugin", "plugin.alpha")
        authorizer = _DetachedAfterOneRead(scope_of=scopes.table_scope)
        authorizer.attach(credentials=self.store, run_record=Registry().get)
        self.assertIsNone(authorizer(plugin, "run.list", {}))
        authorizer.attach(credentials=self.store, run_record=Registry().get)
        with self.assertNoLogs("recursion_core.control.scopes", "ERROR"):
            self.assertEqual(authorizer.held(plugin), frozenset({"read", "run"}))
        self.assertEqual(authorizer(plugin, "run.list", {}), ("scope_refused", "needs_read"))   # detached now

    def test_the_stop_fence_reads_the_run_reader_once(self) -> None:
        plugin = H.Principal("plugin", "plugin.alpha")
        authorizer = _DetachedAfterOneRead(scope_of=scopes.table_scope)
        authorizer.attach(credentials=None, run_record=Registry().get)
        authorizer._credentials = self.store            # the store read by held(), the reader by the fence
        self.assertIsNone(authorizer(plugin, "run.stop", {"run_id": "run-alpha"}))

    def test_calls_racing_a_stop_and_start_never_fail_the_authorizer(self) -> None:
        hooks, service = with_service()
        authorizer = service._authorizer   # the one with_service() put into S5
        authorizer.attach(credentials=self.store, run_record=None)
        plugin, answers, done = H.Principal("plugin", "plugin.alpha"), [], threading.Event()

        def toggle() -> None:
            while not done.is_set():
                authorizer.attach(credentials=None, run_record=None)
                authorizer.attach(credentials=self.store, run_record=None)

        toggler = threading.Thread(target=toggle)
        toggler.start()
        try:
            for _ in range(2000):
                answers.append(hooks.authorize(plugin, "run.list", {}))
        finally:
            done.set()
            toggler.join(10)
        self.assertLessEqual({str(a) for a in answers}, {"None", str(refused("needs_read"))})


_NO_FALLBACK = object()


class _UnreadableGrant:
    """A principal whose grant context cannot be read (Sol's round-2 C2 on #385): not a principal without one."""
    kind, id, in_run_unit = "plugin", "plugin.alpha", False

    @property
    def grant(self):
        raise RuntimeError("the trusted grant context is unavailable")


class _UnreadableOwnerGrant(_UnreadableGrant):
    kind, id = "owner", "local"


@NEEDS_POSIX
class UnreadableGrantThroughS5Test(unittest.TestCase):
    """The round-2 reads of #385 (Codex GPT-6.1 Sol, C2; the rule came with #377's 6c109b31): through S5, with the
    service started on a real store, a principal whose grant cannot be read holds nothing, even where its identity
    has live grants, and as registered an owner-kind one holds nothing either."""

    def test_an_unreadable_grant_holds_nothing_through_s5(self) -> None:
        base = tempfile.mkdtemp(prefix="scopes-unreadable-")
        self.addCleanup(shutil.rmtree, base, True)
        state = os.path.join(base, "state")
        os.mkdir(state, 0o700)
        CredentialStore(os.path.join(state, "control", "credentials.json")).mint("plugin", "plugin.alpha",
                                                                                   ["read", "run"])
        hooks, service = with_service()
        service.start(H.ServiceContext(state_dir=state, control_registry=Registry()))
        self.assertIsNone(hooks.authorize(H.Principal("plugin", "plugin.alpha"), "run.list", {}))   # absent grant
        self.assertEqual(hooks.authorize(_UnreadableGrant(), "run.list", {}), refused("needs_read"))
        self.assertEqual(hooks.authorize(_UnreadableGrant(), "run.stop", {"run_id": "run-alpha"}), refused("needs_run"))
        as_registered = composed()
        self.assertIsNone(as_registered.authorize(H.OWNER, "run.list", {}))
        self.assertEqual(as_registered.authorize(_UnreadableOwnerGrant(), "run.list", {}), refused("needs_read"))

    def test_a_declared_grant_failing_with_attributeerror_holds_nothing_through_s5(self) -> None:
        # The round-3 reads of #385 (Astra 1, Sol 1, STRICT): getattr(..., None) took the accessor's AttributeError
        # for an absent grant, and a __getattr__ fallback could hand back a normal value in its place.
        base = tempfile.mkdtemp(prefix="scopes-attributeerror-")
        self.addCleanup(shutil.rmtree, base, True)
        state = os.path.join(base, "state")
        os.mkdir(state, 0o700)
        store = CredentialStore(os.path.join(state, "control", "credentials.json"))
        _, grant = store.mint("plugin", "plugin.alpha", ["read", "run"])
        hooks, service = with_service()
        service.start(H.ServiceContext(state_dir=state, control_registry=Registry()))
        as_registered = composed()

        def principal(kind, principal_id, fallback):
            class Declared:
                in_run_unit = False

                def __init__(self):
                    self.kind, self.id = kind, principal_id

                @property
                def grant(self):
                    raise AttributeError("the trusted grant context is unavailable")

                if fallback is not _NO_FALLBACK:
                    def __getattr__(self, name):
                        return fallback

            return Declared()

        for fallback in (_NO_FALLBACK, None, grant.token_id):
            for kind, principal_id in (("plugin", "plugin.alpha"), ("owner", "local"), ("device", "page")):
                with self.subTest(fallback=fallback, kind=kind):
                    self.assertEqual(hooks.authorize(principal(kind, principal_id, fallback), "run.list", {}),
                                     refused("needs_read"))
                    self.assertEqual(as_registered.authorize(principal(kind, principal_id, fallback), "run.list", {}),
                                     refused("needs_read"))
        self.assertIsNone(hooks.authorize(H.Principal("plugin", "plugin.alpha"), "run.list", {}))   # absent grant
        self.assertIsNone(as_registered.authorize(H.DEVICE, "approval.list", {}))                     # the page


@NEEDS_POSIX
class BearerWithoutGrantTest(unittest.TestCase):
    """Until SPINE's Principal carries ``grant``, a device, session or agent principal holds nothing, live grants
    or not; a host-attributed plugin holds its identity's grants."""

    def test_a_real_principal_of_a_bearer_kind_holds_nothing(self) -> None:
        base = tempfile.mkdtemp(prefix="scopes-bearer-")
        self.addCleanup(shutil.rmtree, base, True)
        state = os.path.join(base, "state")
        os.mkdir(state, 0o700)
        store = CredentialStore(os.path.join(state, "control", "credentials.json"))
        store.mint("device", "phone-1", ["read", "run"])
        store.mint("session", "s1", ["read", "run"])
        store.mint("plugin", "plugin.alpha", ["read"])
        hooks, service = with_service()
        service.start(H.ServiceContext(state_dir=state, control_registry=Registry()))
        for principal in (H.Principal("device", "phone-1"), H.Principal("session", "s1")):
            self.assertEqual(hooks.authorize(principal, "run.list", {}), refused("needs_read"), principal)
        self.assertIsNone(hooks.authorize(H.Principal("plugin", "plugin.alpha"), "run.list", {}))


def stat_mode(path: str) -> int:
    return os.lstat(path).st_mode & 0o7777


class MatrixThroughS5Test(unittest.TestCase):
    """First proof, at S5: every principal kind against every row of the runtime method table, through the
    composed hooks, gets exactly its scope's answers and scope_refused otherwise."""

    def test_every_principal_kind_against_every_row(self) -> None:
        grants = {("device", "phone-1"): {"read", "run"}, ("device", "phone-2"): {"read"},
                  ("plugin", "plugin.alpha"): {"read", "run"}, ("session", "s1"): {"read", "run"}}

        class Grants:
            def held(self, kind, principal_id):
                return frozenset(grants.get((kind, principal_id), ()))

            def grant_scopes(self, grant, kind, principal_id):
                return self.held(kind, principal_id) if grant == f"g-{kind}-{principal_id}" else frozenset()

        hooks = composed()
        hooks._single["authorize"].attach(credentials=Grants(), run_record=Registry().get)
        principals = {
            "owner": (H.OWNER, {"read", "run", "approve", "manage"}),
            "internal": (H.Principal("internal", "quota"), {"read", "run", "approve", "manage"}),
            "page": (H.DEVICE, {"read", "run", "approve"}),
            "device-run": (Granted("device", "phone-1", grant="g-device-phone-1"), {"read", "run"}),
            "device-read": (Granted("device", "phone-2", grant="g-device-phone-2"), {"read"}),
            "plugin": (H.Principal("plugin", "plugin.alpha"), {"read", "run"}),
            "session": (Granted("session", "s1", grant="g-session-s1"), {"read", "run"}),
            "nobody": (H.Principal("plugin", "plugin.none"), set()),
        }
        lines = ["method".ljust(22) + "scope".ljust(9) + " ".join(name[:11].ljust(11) for name in principals)]
        mismatches = []
        for method, row in METHODS.items():
            params = {"run_id": "run-host"} if method == "run.stop" else {}   # host-owned, session s1's
            cells = []
            for name, (principal, holds) in principals.items():
                got = hooks.authorize(principal, method, params)
                want_ok = row.scope in holds and not (method == "run.stop" and name in ("internal", "plugin"))
                if (got is None) != want_ok or (got is not None and got["code"] != "scope_refused"):
                    mismatches.append((method, name, got))
                cells.append(("ok" if got is None else got["code"])[:11].ljust(11))
            lines.append(method.ljust(22) + row.scope.ljust(9) + " ".join(cells))
        print("\nScope matrix through S5 (control/hooks.py Hooks.authorize, the runtime METHODS):\n" + "\n".join(lines))
        self.assertEqual(mismatches, [])


if __name__ == "__main__":
    unittest.main()
