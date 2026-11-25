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


class _SwapsReader(type):
    """Hands back object's own reader when asked for the class's, hiding a custom __getattribute__ (#377 round 6)."""

    def __getattribute__(cls, name):
        if name == "__getattribute__":
            return object.__getattribute__
        return type.__getattribute__(cls, name)


class _UnreadableOnTheMetaclass(type):
    @property
    def grant(cls):
        raise AttributeError("the trusted grant context is unavailable")


def _custom_reader(kind, principal_id, failure, metaclass=type):
    """A principal whose class declares ``grant = None`` and whose own ``__getattribute__`` fails reading it."""

    class CustomReader(metaclass=metaclass):
        grant = None
        in_run_unit = False

        def __init__(self):
            self.kind, self.id = kind, principal_id

        def __getattribute__(self, name):
            if name == "grant":
                raise failure
            return object.__getattribute__(self, name)

    return CustomReader()


@NEEDS_POSIX
class RoundFourSeamsTest(unittest.TestCase):
    """The round-4 reads of #385. Codex GPT-6 Astra 1 and GPT-6.1 Sol 1 (STRICT): object.__getattribute__ skipped a
    custom __getattribute__, so the declared default stood in for its failed read (the reader now comes with #377's
    26a5996c). DeepSeek V4.1 Flash F1: a grant declared on the metaclass read as absent (with #377's df0c5f3f), and
    F2: ScopesService.start raised for a state_dir whose read raised."""

    KINDS = (("plugin", "plugin.alpha"), ("owner", "local"), ("internal", "quota"), ("device", "page"))

    def setUp(self) -> None:
        base = tempfile.mkdtemp(prefix="scopes-r4-")
        self.addCleanup(shutil.rmtree, base, True)
        self.state = os.path.join(base, "state")
        os.mkdir(self.state, 0o700)
        store = CredentialStore(os.path.join(self.state, "control", "credentials.json"))
        _, self.grant = store.mint("plugin", "plugin.alpha", ["read", "run"])    # live grants a fall-through would use
        self.hooks, self.service = with_service()
        self.service.start(H.ServiceContext(state_dir=self.state, control_registry=Registry()))
        self.as_registered = composed()

    def assert_holds_nothing(self, make) -> None:
        for failure in (AttributeError("context unavailable"), RuntimeError("context unavailable")):
            for kind, principal_id in self.KINDS:
                with self.subTest(failure=type(failure).__name__, kind=kind):
                    who = make(kind, principal_id, failure)
                    self.assertEqual(self.service._authorizer.held(who), frozenset())
                    self.assertEqual(self.hooks.authorize(who, "run.list", {}), refused("needs_read"))
                    self.assertEqual(self.as_registered.authorize(who, "run.list", {}), refused("needs_read"))

    def assert_absence_still_holds(self) -> None:
        self.assertIsNone(self.hooks.authorize(H.Principal("plugin", "plugin.alpha"), "run.list", {}))
        self.assertIsNone(self.as_registered.authorize(H.OWNER, "workspace.publish", {}))
        self.assertIsNone(self.as_registered.authorize(H.Principal("internal", "quota"), "approval.answer", {}))
        self.assertIsNone(self.as_registered.authorize(H.DEVICE, "approval.list", {}))

    def test_declared_grant_custom_reader_failure_holds_nothing(self) -> None:
        # Astra 1: directly and through S5, with the service started and as registered.
        self.assert_holds_nothing(_custom_reader)
        self.assert_absence_still_holds()

    def test_a_declared_grant_read_failure_in_custom_getattribute_holds_nothing(self) -> None:
        # Sol 1, with the metaclass that hands back object's reader (#377 round 6), and DeepSeek F1: a grant declared
        # on the metaclass and unreadable.
        self.assert_holds_nothing(lambda kind, pid, failure: _custom_reader(kind, pid, failure, _SwapsReader))

        def on_the_metaclass(kind, principal_id, failure):
            class OnTheMetaclass(metaclass=_UnreadableOnTheMetaclass):
                in_run_unit = False

                def __init__(self):
                    self.kind, self.id = kind, principal_id

            return OnTheMetaclass()

        self.assert_holds_nothing(on_the_metaclass)
        self.assert_absence_still_holds()

    def test_a_reader_that_supplies_the_grant_is_the_grant_used(self) -> None:
        class Supplies(metaclass=_SwapsReader):
            grant = None
            kind, id, in_run_unit = "plugin", "plugin.alpha", False

            def __getattribute__(inner, name):
                if name == "grant":
                    return self.grant.token_id
                return object.__getattribute__(inner, name)

        self.assertIsNone(self.hooks.authorize(Supplies(), "run.list", {}))
        self.assertEqual(self.service._authorizer.held(Supplies()), frozenset({"read", "run"}))

    def test_a_context_whose_reads_raise_is_logged_and_start_returns(self) -> None:
        class NoPath:
            def __fspath__(self):
                raise RuntimeError("no path")

        class RaisingStateDir:
            control_registry = Registry()

            @property
            def state_dir(self):
                raise RuntimeError("no state directory")

        class RaisingRegistry:
            state_dir = None

            @property
            def control_registry(self):
                raise RuntimeError("no registry")

        class RaisingGet:
            @property
            def get(self):
                raise RuntimeError("no reader")

        contexts = {"__fspath__": H.ServiceContext(state_dir=NoPath(), control_registry=Registry()),
                    "state_dir": RaisingStateDir(), "control_registry": RaisingRegistry(),
                    "registry.get": H.ServiceContext(state_dir=None, control_registry=RaisingGet())}
        for where, context in contexts.items():
            with self.subTest(where=where):
                hooks, service = with_service()
                with self.assertLogs("recursion_core.control.scopes", "ERROR"):
                    service.start(context)
                self.assertTrue(service.ready)
                self.assertEqual(hooks.authorize(H.Principal("plugin", "plugin.alpha"), "run.stop",
                                                 {"run_id": "run-alpha"}), refused("needs_run"))
                self.assertIsNone(hooks.authorize(H.OWNER, "run.list", {}))


class RoundFiveSeamsTest(unittest.TestCase):
    """The round-5 reads of #385. Codex GPT-6.1 Sol, 1 (coverage): reader selection relaxed to cls.__mro__ or
    base.__dict__ left every test green. DeepSeek V4.1 Flash: F1 (STRICT), a kind that is a str subclass comparing
    equal to owner or internal held all four scopes; F2, an id comparing equal to page held the page's; F3, a table
    row whose scope is such a subclass opened a call; F4, a store built with a wrong limit or wall raised untyped."""

    class Liar(str):
        """Compares equal to anything; hashes as the text it is told to pose as."""

        def __new__(cls, text, poses_as):
            made = super().__new__(cls, text)
            made.poses_as = poses_as
            return made

        def __eq__(self, other):
            return True

        def __ne__(self, other):
            return False

        def __hash__(self):
            return hash(self.poses_as)

    def test_special_lookup_never_asks_the_metaclass_for_mro_or_dict(self) -> None:
        # Sol's test, as written in its required-coverage.patch.
        seen = []

        class Meta(type):
            def __getattribute__(cls, name):
                if name in ("__mro__", "__dict__"):
                    seen.append(name)
                return type.__getattribute__(cls, name)

        def reader(instance, name):
            return "fixture-marker"

        class Parent(metaclass=Meta):
            __getattribute__ = reader

        class Child(Parent):
            pass

        self.assertIs(scopes._special(Child, "__getattribute__"), reader)
        self.assertEqual(seen, [], "reader selection must use type's own MRO and dictionary descriptors")

    def test_a_kind_or_id_that_is_not_exactly_a_str_holds_nothing(self) -> None:
        Liar = self.Liar
        as_registered = composed()
        authorizer = scopes.Authorizer(scope_of=scopes.table_scope)
        for kind, principal_id in ((Liar("plugin", "owner"), "local"), (Liar("plugin", "internal"), "quota"),
                                   ("device", Liar("phone-1", "page")), (Liar("device", "device"), "page")):
            principal = Granted(kind, principal_id)
            with self.subTest(kind=str.__str__(kind), id=str.__str__(principal_id)):
                self.assertEqual(authorizer.held(principal), frozenset())
                self.assertEqual(as_registered.authorize(principal, "run.list", {}), refused("needs_read"))
                self.assertEqual(as_registered.authorize(principal, "run.stop", {"run_id": "run-host"}),
                                 refused("needs_run"))
        self.assertIsNone(as_registered.authorize(H.OWNER, "workspace.publish", {}))    # exact kinds still hold
        self.assertIsNone(as_registered.authorize(H.DEVICE, "approval.list", {}))

    def test_a_table_row_whose_scope_is_not_exactly_a_str_opens_nothing(self) -> None:
        authorizer = scopes.Authorizer(scope_of=lambda method: self.Liar("read", "read"))
        for principal in (H.DEVICE, H.OWNER):
            with self.subTest(kind=principal.kind):
                self.assertEqual(authorizer(principal, "workspace.publish", {}), ("scope_refused", "scope_unknown"))

    @NEEDS_POSIX
    def test_a_store_built_with_a_wrong_limit_wall_or_emit_is_refused_at_once(self) -> None:
        from recursion_core.control.credentials import CredentialStoreError, FailedAuthLimit
        base = tempfile.mkdtemp(prefix="scopes-r5-")
        self.addCleanup(shutil.rmtree, base, True)
        path = os.path.join(base, "state", "credentials.json")
        for kwargs in ({"limit": 5}, {"limit": object()}, {"wall": "x"}, {"emit": 5}):
            with self.subTest(**{key: repr(value) for key, value in kwargs.items()}):
                with self.assertRaises(CredentialStoreError):
                    CredentialStore(path, **kwargs)
        store = CredentialStore(path, limit=FailedAuthLimit(max_failures=3), wall=lambda: 1000.0, emit=None)
        token, _ = store.mint("plugin", "p1", ["read"])
        self.assertEqual(store.verify(token, peer="p").id, "p1")

    @NEEDS_POSIX
    def test_each_limiter_method_is_required_at_construction(self) -> None:
        # The round-6 read of #385 by Codex GPT-6 Astra, 1 (its required-coverage.patch): removing either check
        # left every test green. And DeepSeek's round-6 F5: a limiter whose method raises when read.
        from types import SimpleNamespace
        from recursion_core.control.credentials import CredentialStoreError

        class Raising:
            def __init__(self, broken):
                self.broken = broken

            def __getattr__(self, name):
                if name == self.broken:
                    raise RuntimeError(f"the {name} attribute cannot be read")
                return lambda peer: False

        base = tempfile.mkdtemp(prefix="scopes-limit-interface-")
        self.addCleanup(shutil.rmtree, base, True)
        path = os.path.join(base, "state", "credentials.json")
        for limit in (SimpleNamespace(blocked=lambda peer: False),
                      SimpleNamespace(failed=lambda peer: None),
                      SimpleNamespace(blocked=lambda peer: False, failed=0),
                      SimpleNamespace(blocked=0, failed=lambda peer: None),
                      Raising("blocked"), Raising("failed")):
            with self.subTest(limit=repr(limit)):
                with self.assertRaises(CredentialStoreError):
                    CredentialStore(path, limit=limit)


class RoundSixSeamsTest(unittest.TestCase):
    """The round-6 read of #385 by DeepSeek V4.1 Flash. F1 (STRICT): one call read the principal's kind and id
    twice, and the second read chose the Stop rule, so a principal answering plugin and then device or owner
    stopped the host's run, another plugin's, or an unattributed one. F2: the Stop's run_id and params were read
    through the caller's own types."""

    RUNS = {
        "run-host": {"launch": {"owner": "host", "session_id": None}},
        "run-other": {"launch": {"owner": "plugin.gamma", "session_id": None}},
        "run-bare": {"launch": {"session_id": None}},
        "run-beta": {"launch": {"owner": "plugin.beta", "session_id": None}},
        "run-s9": {"launch": {"owner": "host", "session_id": "s9"}},
    }

    class Store:
        """plugin.beta holds read and run by identity; session s1's grant g-s1 holds read and run."""

        def held(self, kind, principal_id):
            return frozenset({"read", "run"}) if (kind, principal_id) == ("plugin", "plugin.beta") else frozenset()

        def grant_scopes(self, grant, kind, principal_id):
            return frozenset({"read", "run"}) if (grant, kind, principal_id) == ("g-s1", "session", "s1") \
                else frozenset()

    def authorizer(self):
        return scopes.Authorizer(scope_of=SCOPE_COLUMN.get, credentials=self.Store(), run_record=self.RUNS.get)

    @staticmethod
    def answering(kinds, ids, grant=None):
        """A principal whose kind and id answer each read with the next value (the last one repeats)."""

        class Answering:
            in_run_unit = False

            def __init__(self):
                self.reads = {"kind": list(kinds), "id": list(ids)}

            def _next(self, name):
                values = self.reads[name]
                return values.pop(0) if len(values) > 1 else values[0]

            @property
            def kind(self):
                return self._next("kind")

            @property
            def id(self):
                return self._next("id")

        if grant is not None:
            Answering.grant = grant
        return Answering()

    def test_a_principal_that_answers_twice_is_judged_on_its_first_answer(self) -> None:
        auth = self.authorizer()
        cases = [
            (("plugin", "device"), ("plugin.beta",), "run-host", "run_not_owned"),
            (("plugin", "device"), ("plugin.beta",), "run-bare", "run_not_owned"),
            (("plugin", "owner"), ("plugin.beta",), "run-host", "run_not_owned"),
            (("plugin", "owner"), ("plugin.beta",), "run-bare", "run_not_owned"),
            (("plugin",), ("plugin.beta", "plugin.gamma"), "run-other", "run_not_owned"),
            (("session",), ("s1", "s9"), "run-s9", "run_outside_session"),
        ]
        for kinds, ids, run_id, detail in cases:
            grant = "g-s1" if kinds == ("session",) else None
            with self.subTest(kinds=kinds, ids=ids, run=run_id):
                principal = self.answering(kinds, ids, grant)
                self.assertEqual(auth(principal, "run.stop", {"run_id": run_id}), ("scope_refused", detail))
        self.assertIsNone(auth(self.answering(("plugin",), ("plugin.beta",)), "run.stop", {"run_id": "run-beta"}))

    def test_a_kind_flipped_by_another_thread_never_widens_the_stop(self) -> None:
        # DeepSeek saw 49 allows in 200 rounds on 3.14t with the GIL off; one read leaves none.
        auth = self.authorizer()

        class Flipping:
            in_run_unit = False
            kind, id = "plugin", "plugin.beta"

        principal, done = Flipping(), threading.Event()

        def flip():
            while not done.is_set():
                principal.kind = "device"
                principal.kind = "plugin"

        flipper = threading.Thread(target=flip)
        flipper.start()
        try:
            answers = [auth(principal, "run.stop", {"run_id": "run-host"}) for _ in range(2000)]
        finally:
            done.set()
            flipper.join(30)
        self.assertNotIn(None, answers)

    def test_the_stop_names_exactly_the_run_sent(self) -> None:
        class Liar(str):
            def __eq__(self, other):
                return True

            def __hash__(self):
                return hash("run-beta")

        class SaysBeta(dict):
            def get(self, key, default=None):
                return "run-beta" if key == "run_id" else default

        auth = self.authorizer()
        principal = self.answering(("plugin",), ("plugin.beta",))
        for params in ({"run_id": Liar("zzz")}, SaysBeta()):
            with self.subTest(params=type(params).__name__):
                self.assertEqual(auth(principal, "run.stop", params), ("scope_refused", "run_not_owned"))
        self.assertIsNone(auth(principal, "run.stop", {"run_id": "run-beta"}))


class RoundSevenSeamsTest(unittest.TestCase):
    """The round-7 read of #385 by DeepSeek V4.1 Flash. F1: the Stop compared the record's owner and session_id
    with their own __eq__, so a str subclass in the record let a plugin stop a host-owned run and one session
    stop another's. F2: a subclass overriding held() does not change what a call decides."""

    class Sneaky(str):
        """Text that answers both equal and unequal to anything (DeepSeek's SneakyOwner and SneakySession)."""

        def __eq__(self, other):
            return True

        def __ne__(self, other):
            return True

        __hash__ = str.__hash__

    def authorizer(self, runs):
        return scopes.Authorizer(scope_of=SCOPE_COLUMN.get, credentials=RoundSixSeamsTest.Store(), run_record=runs.get)

    def test_record_fields_that_are_str_subclasses_open_no_stop(self) -> None:
        runs = {"run-host": {"launch": {"owner": self.Sneaky("host"), "session_id": None}},
                "run-s9": {"launch": {"owner": "host", "session_id": self.Sneaky("s9")}}}
        auth = self.authorizer(runs)
        plugin = RoundSixSeamsTest.answering(("plugin",), ("plugin.beta",))
        session = RoundSixSeamsTest.answering(("session",), ("s1",), grant="g-s1")
        self.assertEqual(auth(plugin, "run.stop", {"run_id": "run-host"}), ("scope_refused", "run_not_owned"))
        self.assertEqual(auth(session, "run.stop", {"run_id": "run-s9"}), ("scope_refused", "run_outside_session"))
        honest = self.authorizer({"run-beta": {"launch": {"owner": "plugin.beta", "session_id": None}},
                                  "run-s1": {"launch": {"owner": "host", "session_id": "s1"}}})
        self.assertIsNone(honest(plugin, "run.stop", {"run_id": "run-beta"}))
        self.assertIsNone(honest(session, "run.stop", {"run_id": "run-s1"}))

    def test_overriding_held_changes_only_what_held_answers(self) -> None:
        class Refusing(scopes.Authorizer):
            def held(self, principal):
                return frozenset()

        auth = Refusing(scope_of=SCOPE_COLUMN.get)
        self.assertEqual(auth.held(H.OWNER), frozenset())
        self.assertIsNone(auth(H.OWNER, "run.list", {}))      # the call decides from _held, as documented


@NEEDS_POSIX
class BearerWithoutGrantTest(unittest.TestCase):
    """A device or session principal that names no grant holds nothing, live grants or not. ``Principal.grant``
    defaults to None, which is an absent grant. A host-attributed plugin holds its identity's grants."""

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
