"""The authorizer registered into seam S5, through SPINE's real hooks (control/hooks.py, #369 08b4b1d6).

What a lane needs to test a hook (control/hooks.py, "BUILDING AGAINST THIS FILE"): make ``Hooks()``, call the
lane's own ``register(hooks)``, ``freeze()``, and call the hook. ``hooks.authorize`` is the call every entry
point makes (the socket, the page's three paths and in-process calls, once the seams' daemon edits land); its
answer here is the answer there. The entry points themselves are tested when those edits are on master.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from recursion_core.control import hooks as H
from recursion_core.control import scopes
from recursion_core.control.credentials import CredentialStore
from recursion_core.control.methods import METHODS
from recursion_core.control.tests.test_scopes import SCOPE_COLUMN

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
        self.mint("session", "s1", ["read", "run"])
        self.service.start(self.context())
        self.assertTrue(self.service.ready)
        plugin, session = H.Principal("plugin", "plugin.alpha"), H.Principal("session", "s1")
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

        hooks = composed()
        hooks._single["authorize"].attach(credentials=Grants(), run_record=Registry().get)
        principals = {
            "owner": (H.OWNER, {"read", "run", "approve", "manage"}),
            "internal": (H.Principal("internal", "quota"), {"read", "run", "approve", "manage"}),
            "page": (H.DEVICE, {"read", "run", "approve"}),
            "device-run": (H.Principal("device", "phone-1"), {"read", "run"}),
            "device-read": (H.Principal("device", "phone-2"), {"read"}),
            "plugin": (H.Principal("plugin", "plugin.alpha"), {"read", "run"}),
            "session": (H.Principal("session", "s1"), {"read", "run"}),
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
