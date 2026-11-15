"""The authorizer (seam S5) against a stand-in for SPINE's hook: scopes per principal, the Stop row, the run unit.

Phase c0: control/hooks.py and the method table are not on master yet. ``Principal`` below mirrors
control/hooks.py's (kind, id, in_run_unit) at SPINE's #369 head 08b4b1d6, and ``SCOPE_COLUMN`` is the
scope column of schemas/control/v1/methods.table.v1.json at #368's head c77aa2e0 (draft 10), copied
row for row. When the seams land these tests read the real table and go through every entry point.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from dataclasses import dataclass

from recursion_core.control import scopes
from recursion_core.control.credentials import CredentialStore
from recursion_core.control.scopes import Authorizer

POSIX = os.name != "nt" and hasattr(os, "getuid") and hasattr(os, "O_NOFOLLOW")

SCOPE_COLUMN = {
    "run.start": "run", "run.status": "read", "run.list": "read", "run.stop": "run", "route.list": "read",
    "plugin.list": "read", "session.start": "run", "session.turn": "run", "session.steer": "run",
    "session.queue": "run", "session.interrupt": "run", "session.resume": "run", "session.fork": "run",
    "session.export": "read", "session.list": "read", "session.status": "read", "session.stop": "run",
    "queue.update": "run", "queue.cancel": "run", "approval.list": "read", "approval.open": "approve",
    "approval.answer": "approve", "events.read": "read", "events.follow": "read", "route.explain": "read",
    "operator.read": "read", "budget.show": "read", "workspace.diff": "read", "workspace.checkpoint": "run",
    "workspace.restore": "run", "workspace.publish": "manage", "plugin.status": "read", "plugin.call": "run",
    "key.list": "read", "surface.prefs.get": "read", "surface.prefs.set": "run", "doctor": "read",
    "methods.list": "read",
}


@dataclass(frozen=True)
class Principal:
    """Mirror of control/hooks.py Principal (SPINE #369): made by the server, never read from params. ``grant``
    is the field this lane asks SPINE for: the verified token's grant id, for a principal made from a token."""
    kind: str
    id: str | None = None
    in_run_unit: bool = False
    grant: str | None = None


def g(kind: str, principal_id: str) -> str:
    """The stand-in grant id of the one grant ``Grants`` keeps for ``(kind, principal_id)``."""
    return f"g-{kind}-{principal_id}"


def bearer(kind: str, principal_id: str, in_run_unit: bool = False) -> Principal:
    """A principal as the server would make it from a verified token: it names its grant."""
    return Principal(kind, principal_id, in_run_unit, g(kind, principal_id))


class Grants:
    """Stands in for CredentialStore: ``held`` (an identity's live grants, for host-attributed plugins) and
    ``grant_scopes`` (the one grant a token principal names), one grant per ``(kind, id)``, id ``g(kind, id)``."""

    def __init__(self, table: dict) -> None:
        self.table = dict(table)

    def held(self, kind, principal_id) -> frozenset:
        return frozenset(self.table.get((kind, principal_id), ()))

    def grant_scopes(self, grant, kind, principal_id) -> frozenset:
        if grant != g(kind, principal_id):
            return frozenset()
        return frozenset(self.table.get((kind, principal_id), ()))


RUNS = {
    "run-legacy": {"run_id": "run-legacy", "state": "running"},   # from before version 1: no principal, no launch
    "run-host": {"run_id": "run-host", "launch": {"owner": "host", "session_id": None}},
    "run-alpha": {"run_id": "run-alpha", "launch": {"owner": "plugin.alpha", "session_id": None}},
    "run-beta": {"run_id": "run-beta", "launch": {"owner": "plugin.beta", "session_id": None}},
    "run-noowner": {"run_id": "run-noowner", "launch": {"session_id": None}},
    "run-emptyowner": {"run_id": "run-emptyowner", "launch": {"owner": "", "session_id": None}},
    "run-s1": {"run_id": "run-s1", "launch": {"owner": "host", "session_id": "s1"}},
    "run-s1-child": {"run_id": "run-s1-child", "launch": {"owner": "host", "session_id": "s1.child"}},
    "run-s2": {"run_id": "run-s2", "launch": {"owner": "host", "session_id": "s2"}},
}

PRINCIPALS = {
    "owner": Principal("owner", "local"),
    "internal": Principal("internal", "quota"),
    "page": Principal("device", "page"),
    "device-run": bearer("device", "phone-1"),
    "device-read": bearer("device", "phone-2"),
    "plugin": Principal("plugin", "plugin.alpha"),
    "session": bearer("session", "s1"),
    "agent": bearer("agent", "mcp"),
    "nobody": Principal("plugin", "plugin.unknown"),
}
GRANTS = Grants({
    ("device", "phone-1"): {"read", "run"},
    ("device", "phone-2"): {"read"},
    ("plugin", "plugin.alpha"): {"read", "run"},
    ("session", "s1"): {"read", "run"},
    ("agent", "mcp"): {"read", "run"},
})
# What each principal holds, as the rules give it: the expected column of the matrix.
HOLDS = {
    "owner": {"read", "run", "approve", "manage"}, "internal": {"read", "run", "approve", "manage"},
    "page": {"read", "run", "approve"}, "device-run": {"read", "run"}, "device-read": {"read"},
    "plugin": {"read", "run"}, "session": {"read", "run"}, "agent": {"read", "run"}, "nobody": set(),
}


def authorizer(**kwargs) -> Authorizer:
    kwargs.setdefault("scope_of", SCOPE_COLUMN.get)
    kwargs.setdefault("credentials", GRANTS)
    kwargs.setdefault("run_record", RUNS.get)
    return Authorizer(**kwargs)


def answer(verdict) -> str:
    return "ok" if verdict is None else verdict[0]


class MatrixTest(unittest.TestCase):
    """First proof (in-process part): each principal kind calls every row and gets exactly its scope's answers."""

    def test_every_principal_against_every_row(self) -> None:
        auth = authorizer()
        names = list(PRINCIPALS)
        lines = ["method".ljust(22) + "scope".ljust(9) + " ".join(n[:11].ljust(11) for n in names)]
        mismatches = []
        for method, scope in SCOPE_COLUMN.items():
            # run.stop's target here is the plugin's and session s1's run, owned by both stand-ins below.
            params = {"run_id": "run-s1"} if method == "run.stop" else {}
            row = []
            for name, principal in PRINCIPALS.items():
                got = auth(principal, method, params)
                want_ok = scope in HOLDS[name]
                if method == "run.stop" and name in ("internal", "agent", "plugin"):
                    want_ok = False   # internal and agent are not in the Stop row; run-s1 is host-owned
                if (got is None) != want_ok:
                    mismatches.append((method, name, got))
                if got is not None:
                    self.assertEqual(got[0], "scope_refused", (method, name, got))
                row.append(answer(got)[:11].ljust(11))
            lines.append(method.ljust(22) + scope.ljust(9) + " ".join(row))
        print("\nScope matrix (in-process stand-in; ok = allowed, scope_refused otherwise):\n" + "\n".join(lines))
        self.assertEqual(mismatches, [])

    def test_a_read_principal_is_refused_start_stop_and_approval_answer(self) -> None:
        auth = authorizer()
        reader = PRINCIPALS["device-read"]
        self.assertEqual(auth(reader, "run.start", {"tenant": "t"}), ("scope_refused", "needs_run"))
        self.assertEqual(auth(reader, "run.stop", {"run_id": "run-legacy"}), ("scope_refused", "needs_run"))
        self.assertEqual(auth(reader, "approval.answer", {}), ("scope_refused", "needs_approve"))
        for method in ("route.list", "plugin.list", "operator.read", "run.list", "run.status"):
            self.assertIsNone(auth(reader, method, {}), method)

    def test_route_list_plugin_list_and_operator_read_need_read(self) -> None:
        auth = authorizer()
        for method in ("route.list", "plugin.list", "operator.read"):
            with self.subTest(method=method):
                self.assertEqual(auth(PRINCIPALS["nobody"], method, {}), ("scope_refused", "needs_read"))
                self.assertIsNone(auth(PRINCIPALS["device-read"], method, {}))

    def test_every_verdict_is_one_s5_takes(self) -> None:
        # control/hooks.py Hooks.authorize takes None, a non-empty str, or (non-empty str, str); anything
        # else it refuses as authorizer_failed, which would hide this authorizer's reason.
        auth = authorizer()
        for method in [*SCOPE_COLUMN, "no.such.method", 5, None]:
            for principal in [*PRINCIPALS.values(), Principal("owner", "local", True)]:
                verdict = auth(principal, method, {"run_id": "run-alpha"})
                self.assertTrue(verdict is None or (type(verdict) is tuple and len(verdict) == 2
                                                    and type(verdict[0]) is str and verdict[0]
                                                    and type(verdict[1]) is str), verdict)

    def test_the_scope_is_read_from_the_table_on_every_call(self) -> None:
        asked = []

        def scope_of(method):
            asked.append(method)
            return SCOPE_COLUMN.get(method)

        auth = authorizer(scope_of=scope_of)
        for _ in range(3):
            auth(PRINCIPALS["owner"], "run.list", {})
        self.assertEqual(asked, ["run.list"] * 3)


class CeilingAtCallTest(unittest.TestCase):
    """The ceiling holds on every call, whatever a grant says (recursion #337's rule, kept)."""

    def test_no_plugin_session_or_agent_ever_approves_and_no_device_ever_manages(self) -> None:
        loose = Grants({(kind, "x"): {"read", "run", "approve", "manage"} for kind in ("plugin", "session", "agent", "device")})
        auth = authorizer(credentials=loose)
        for kind in ("plugin", "session", "agent"):
            with self.subTest(kind=kind):
                principal = Principal(kind, "x") if kind == "plugin" else bearer(kind, "x")
                self.assertEqual(auth(principal, "approval.answer", {}), ("scope_refused", "needs_approve"))
                self.assertEqual(auth(principal, "workspace.publish", {}), ("scope_refused", "needs_manage"))
                self.assertIsNone(auth(principal, "run.start", {}))
        self.assertIsNone(auth(bearer("device", "x"), "approval.answer", {}))
        self.assertEqual(auth(bearer("device", "x"), "workspace.publish", {}), ("scope_refused", "needs_manage"))

    def test_the_page_holds_read_run_approve_and_never_manage(self) -> None:
        auth = authorizer(page_scopes=frozenset({"read", "run", "approve", "manage"}))
        page = PRINCIPALS["page"]
        self.assertEqual(auth.held(page), frozenset({"read", "run", "approve"}))
        self.assertEqual(auth(page, "workspace.publish", {}), ("scope_refused", "needs_manage"))
        self.assertEqual(authorizer().held(page), frozenset({"read", "run", "approve"}))

    def test_an_unknown_kind_or_a_principal_without_credentials_holds_nothing(self) -> None:
        auth = authorizer(credentials=None)
        self.assertEqual(auth.held(Principal("plugin", "plugin.alpha")), frozenset())
        self.assertEqual(auth.held(Principal("device", "phone-1")), frozenset())
        self.assertEqual(auth.held(Principal("owner", "local")), frozenset({"read", "run", "approve", "manage"}))

        class Odd:
            kind, id, in_run_unit = "operator_ui", "x", False

        self.assertEqual(authorizer().held(Odd()), frozenset())
        self.assertEqual(authorizer()(Odd(), "run.list", {}), ("scope_refused", "needs_read"))
        # Only the DEVICE named page is the page: a plugin or session that calls itself "page" holds nothing.
        for kind in ("plugin", "session", "agent"):
            self.assertEqual(authorizer().held(Principal(kind, "page")), frozenset(), kind)


class RunUnitTest(unittest.TestCase):
    def test_a_run_unit_peer_or_one_whose_cgroup_cannot_be_read_is_refused_whoever_it_claims_to_be(self) -> None:
        auth = authorizer()
        for kind, pid in (("owner", "local"), ("device", "page"), ("internal", "x")):
            self.assertEqual(auth(Principal(kind, pid, True), "run.list", {}),
                             ("scope_refused", "peer is inside a run unit"))

        class NoFlag:
            kind, id = "owner", "local"

        class Truthy:
            kind, id, in_run_unit = "owner", "local", 1

        class Zero:
            kind, id, in_run_unit = "owner", "local", 0   # falsy, and still not exactly False

        for principal in (NoFlag(), Truthy(), Zero()):
            self.assertEqual(auth(principal, "run.list", {}), ("scope_refused", "peer is inside a run unit"))


class TableEdgeTest(unittest.TestCase):
    def test_a_name_not_in_the_table_passes_only_for_a_principal_holding_every_scope(self) -> None:
        auth = authorizer()
        self.assertIsNone(auth(PRINCIPALS["owner"], "no.such.method", {}))     # the API answers method_unknown
        self.assertIsNone(auth(PRINCIPALS["internal"], "no.such.method", {}))
        for name in ("page", "device-run", "plugin", "session", "agent"):
            self.assertEqual(auth(PRINCIPALS[name], "no.such.method", {}), ("scope_refused", "method_not_in_table"))
        for method in (None, 5, b"run.list"):
            self.assertEqual(auth(PRINCIPALS["page"], method, {}), ("scope_refused", "method_not_in_table"))

    def test_a_row_with_an_unknown_scope_opens_nothing(self) -> None:
        auth = authorizer(scope_of=lambda method: "superuser")
        self.assertEqual(auth(PRINCIPALS["owner"], "run.list", {}), ("scope_refused", "scope_unknown"))


class StopTest(unittest.TestCase):
    """The method table's row (4), "Who may Stop" (PROVISIONAL): each clause, and each way round it."""

    def test_an_owner_or_device_with_run_stops_any_run_including_one_with_no_recorded_principal(self) -> None:
        auth = authorizer()
        for name in ("owner", "page", "device-run"):
            for run_id in ("run-legacy", "run-host", "run-alpha", "run-s2", "run-not-recorded"):
                self.assertIsNone(auth(PRINCIPALS[name], "run.stop", {"run_id": run_id}), (name, run_id))

    def test_without_a_currently_held_run_scope_nobody_stops(self) -> None:
        grants = Grants({("plugin", "plugin.alpha"): {"read", "run"}, ("session", "s1"): {"run"}})
        auth = authorizer(credentials=grants)
        self.assertIsNone(auth(PRINCIPALS["plugin"], "run.stop", {"run_id": "run-alpha"}))
        self.assertIsNone(auth(PRINCIPALS["session"], "run.stop", {"run_id": "run-s1"}))
        grants.table[("plugin", "plugin.alpha")] = {"read"}       # downgraded: the creator rule does not survive it
        del grants.table[("session", "s1")]                       # revoked
        self.assertEqual(auth(PRINCIPALS["plugin"], "run.stop", {"run_id": "run-alpha"}), ("scope_refused", "needs_run"))
        self.assertEqual(auth(PRINCIPALS["session"], "run.stop", {"run_id": "run-s1"}), ("scope_refused", "needs_run"))
        self.assertEqual(auth(PRINCIPALS["device-read"], "run.stop", {"run_id": "run-legacy"}), ("scope_refused", "needs_run"))

    def test_a_session_stops_only_runs_of_its_own_session_and_the_sessions_it_started(self) -> None:
        auth = authorizer()
        session = PRINCIPALS["session"]
        self.assertIsNone(auth(session, "run.stop", {"run_id": "run-s1"}))
        for run_id in ("run-s2", "run-legacy", "run-host", "run-alpha", "run-not-recorded", "run-s1-child"):
            self.assertEqual(auth(session, "run.stop", {"run_id": run_id}), ("scope_refused", "run_outside_session")
                             if run_id in ("run-s2", "run-host", "run-alpha", "run-s1-child") else
                             ("scope_refused", "run_not_owned"), run_id)
        lineage = {"s1.child": "s1", "s1.child.grand": "s1.child", "s2": None}

        def in_subtree(run_session, ancestor):
            while run_session is not None:
                if run_session == ancestor:
                    return True
                run_session = lineage.get(run_session)
            return False

        with_lineage = authorizer(in_subtree=in_subtree, credentials=Grants({("session", "s1"): {"run"},
                                                                              ("session", "s1.child"): {"run"}}))
        self.assertIsNone(with_lineage(session, "run.stop", {"run_id": "run-s1-child"}))
        self.assertEqual(with_lineage(session, "run.stop", {"run_id": "run-s2"}), ("scope_refused", "run_outside_session"))
        self.assertEqual(with_lineage(bearer("session", "s1.child"), "run.stop", {"run_id": "run-s1"}),
                         ("scope_refused", "run_outside_session"))   # a child cannot stop its parent

        def broken(run_session, ancestor):
            raise RuntimeError("lineage unavailable")

        self.assertEqual(authorizer(in_subtree=broken)(session, "run.stop", {"run_id": "run-s1-child"}),
                         ("scope_refused", "run_outside_session"))
        self.assertEqual(authorizer(in_subtree=lambda a, b: 1)(session, "run.stop", {"run_id": "run-s1-child"}),
                         ("scope_refused", "run_outside_session"))   # only True widens the subtree
        # A session principal with no id is no session's, and a run with no session is in nobody's
        # subtree, whatever a lineage function would say.
        permissive = authorizer(credentials=Grants({("session", ""): {"run"}, ("session", "s1"): {"run"}}),
                                in_subtree=lambda a, b: True)
        self.assertEqual(permissive(bearer("session", ""), "run.stop", {"run_id": "run-s1"}),
                         ("scope_refused", "run_not_owned"))
        for run_id in ("run-host", "run-alpha", "run-noowner"):
            self.assertEqual(permissive(session, "run.stop", {"run_id": run_id}), ("scope_refused", "run_outside_session"))

    def test_a_plugin_stops_only_a_run_its_record_says_it_owns(self) -> None:
        auth = authorizer()
        plugin = PRINCIPALS["plugin"]
        self.assertIsNone(auth(plugin, "run.stop", {"run_id": "run-alpha"}))
        for run_id in ("run-host", "run-beta", "run-noowner", "run-emptyowner", "run-legacy", "run-not-recorded"):
            self.assertEqual(auth(plugin, "run.stop", {"run_id": run_id}), ("scope_refused", "run_not_owned"), run_id)
        host_named = Principal("plugin", "host")   # a plugin whose id is "host" does not own the host's runs
        hosted = authorizer(credentials=Grants({("plugin", "host"): {"run"}}))
        self.assertEqual(hosted(host_named, "run.stop", {"run_id": "run-host"}), ("scope_refused", "run_not_owned"))

    def test_a_target_that_cannot_be_read_is_not_owned(self) -> None:
        def broken(run_id):
            raise OSError("registry unavailable")

        for run_record in (broken, None, lambda run_id: "not a record", lambda run_id: {"launch": "x"}):
            auth = authorizer(run_record=run_record)
            self.assertEqual(auth(PRINCIPALS["plugin"], "run.stop", {"run_id": "run-alpha"}), ("scope_refused", "run_not_owned"))
            self.assertEqual(auth(PRINCIPALS["session"], "run.stop", {"run_id": "run-s1"}), ("scope_refused", "run_not_owned"))
            self.assertIsNone(auth(PRINCIPALS["owner"], "run.stop", {"run_id": "run-s1"}))
        auth = authorizer()
        for params in ({}, {"run_id": 5}, None, [], {"run_id": None}):
            self.assertEqual(auth(PRINCIPALS["plugin"], "run.stop", params), ("scope_refused", "run_not_owned"))
        self.assertEqual(auth(Principal("plugin", None), "run.stop", {"run_id": "run-alpha"}), ("scope_refused", "needs_run"))
        self.assertEqual(authorizer(credentials=Grants({("plugin", ""): {"run"}}))(Principal("plugin", ""), "run.stop",
                                                                                  {"run_id": "run-emptyowner"}),
                         ("scope_refused", "run_not_owned"))

    def test_internal_and_agent_principals_are_not_in_the_stop_row(self) -> None:
        auth = authorizer()
        for name in ("internal", "agent"):
            self.assertEqual(auth(PRINCIPALS[name], "run.stop", {"run_id": "run-legacy"}),
                             ("scope_refused", "stop_not_granted_to_kind"))
            self.assertIsNone(auth(PRINCIPALS[name], "run.start", {}))


@unittest.skipUnless(POSIX, "the credential store needs uids and O_NOFOLLOW (POSIX)")
class WithTheRealStoreTest(unittest.TestCase):
    def test_a_revoked_grant_stops_authorizing_at_once(self) -> None:
        base = tempfile.mkdtemp(prefix="scopes-")
        self.addCleanup(shutil.rmtree, base, True)
        store = CredentialStore(os.path.join(base, "state", "credentials.json"))
        _, cred = store.mint("plugin", "plugin.alpha", ["read", "run"])
        auth = authorizer(credentials=store)
        plugin = PRINCIPALS["plugin"]
        self.assertIsNone(auth(plugin, "run.stop", {"run_id": "run-alpha"}))
        self.assertIsNone(auth(plugin, "plugin.list", {}))
        store.revoke(cred.token_id)
        self.assertEqual(auth(plugin, "run.stop", {"run_id": "run-alpha"}), ("scope_refused", "needs_run"))
        self.assertEqual(auth(plugin, "plugin.list", {}), ("scope_refused", "needs_read"))


@unittest.skipUnless(POSIX, "the credential store needs uids and O_NOFOLLOW (POSIX)")
class GrantBoundTest(unittest.TestCase):
    """A token principal holds its own grant only (both reads of #377 and #385): never another grant of the
    same identity, never the page's scopes, nothing when it names no grant."""

    def setUp(self) -> None:
        base = tempfile.mkdtemp(prefix="scopes-grant-")
        self.addCleanup(shutil.rmtree, base, True)
        self.store = CredentialStore(os.path.join(base, "state", "credentials.json"))
        self.auth = authorizer(credentials=self.store)

    def test_a_read_token_is_not_widened_by_another_grant_of_its_identity(self) -> None:
        _, read = self.store.mint("device", "phone-1", ["read"])
        _, run = self.store.mint("device", "phone-1", ["read", "run"])
        reader = Principal("device", "phone-1", grant=read.token_id)
        runner = Principal("device", "phone-1", grant=run.token_id)
        self.assertEqual(self.auth(reader, "run.start", {}), ("scope_refused", "needs_run"))
        self.assertIsNone(self.auth(reader, "run.list", {}))
        self.assertIsNone(self.auth(runner, "run.start", {}))
        self.store.revoke(read.token_id)
        self.assertEqual(self.auth(reader, "run.list", {}), ("scope_refused", "needs_read"))
        self.assertIsNone(self.auth(runner, "run.list", {}))

    def test_a_grant_counts_only_for_the_identity_it_was_minted_for(self) -> None:
        _, cred = self.store.mint("device", "phone-1", ["read", "run"])
        for principal in (Principal("device", "phone-2", grant=cred.token_id),
                          Principal("session", "phone-1", grant=cred.token_id),
                          Principal("device", "phone-1", grant="ctk-not-a-grant"),
                          Principal("device", "phone-1", grant=5)):
            self.assertEqual(self.auth(principal, "run.list", {}), ("scope_refused", "needs_read"), principal)

    def test_a_bearer_kind_without_a_grant_holds_nothing_and_a_host_attributed_plugin_holds_its_identity(self) -> None:
        self.store.mint("device", "phone-1", ["read", "run"])
        self.store.mint("session", "s1", ["read", "run"])
        self.store.mint("agent", "mcp", ["read"])
        self.store.mint("plugin", "plugin.alpha", ["read"])
        self.store.mint("plugin", "plugin.alpha", ["run"])
        for principal in (Principal("device", "phone-1"), Principal("session", "s1"), Principal("agent", "mcp")):
            self.assertEqual(self.auth.held(principal), frozenset(), principal)
        self.assertEqual(self.auth.held(Principal("plugin", "plugin.alpha")), frozenset({"read", "run"}))

    def test_a_token_principal_named_page_never_holds_the_page_scopes(self) -> None:
        self.assertEqual(self.auth.held(Principal("device", "page", grant="ctk-anything")), frozenset())
        self.assertEqual(self.auth.held(Principal("device", "page")), frozenset({"read", "run", "approve"}))


class BreakItAnswersTest(unittest.TestCase):
    """The DeepSeek read of #377: Stop's own rule, a store that raises, principals and rows that are not what
    they should be."""

    def test_run_stop_needs_a_held_run_scope_whatever_the_table_row_says(self) -> None:
        def scope_of(method):
            return "read" if method == "run.stop" else SCOPE_COLUMN.get(method)

        auth = authorizer(scope_of=scope_of)
        self.assertEqual(auth(PRINCIPALS["device-read"], "run.stop", {"run_id": "run-legacy"}), ("scope_refused", "needs_run"))
        self.assertEqual(auth(PRINCIPALS["nobody"], "run.stop", {"run_id": "run-alpha"}), ("scope_refused", "needs_read"))
        for name in ("owner", "page", "device-run"):
            self.assertIsNone(auth(PRINCIPALS[name], "run.stop", {"run_id": "run-legacy"}), name)
        self.assertIsNone(auth(PRINCIPALS["plugin"], "run.stop", {"run_id": "run-alpha"}))

    def test_a_credential_store_that_raises_refuses_and_is_logged(self) -> None:
        class Broken:
            def held(self, kind, principal_id):
                raise RuntimeError("credential store unavailable")

            grant_scopes = held

        auth = authorizer(credentials=Broken())
        for name in ("plugin", "device-run", "session"):
            for method in ("run.list", "run.start", "run.stop"):
                with self.assertLogs("recursion_core.control.scopes", "ERROR"):
                    self.assertEqual(auth(PRINCIPALS[name], method, {"run_id": "run-alpha"}),
                                     ("scope_refused", "credentials_unavailable"), (name, method))
        self.assertIsNone(auth(PRINCIPALS["owner"], "run.stop", {"run_id": "run-alpha"}))   # no store needed

    def test_a_principal_whose_attributes_raise_holds_nothing(self) -> None:
        class NoKind:
            in_run_unit, id = False, "local"

            @property
            def kind(self):
                raise RuntimeError("no kind")

        class NoFlag:
            kind, id = "owner", "local"

            @property
            def in_run_unit(self):
                raise RuntimeError("no flag")

        auth = authorizer()
        self.assertEqual(auth(NoKind(), "run.list", {}), ("scope_refused", "needs_read"))
        self.assertEqual(auth.held(NoKind()), frozenset())
        self.assertEqual(auth(NoFlag(), "run.list", {}), ("scope_refused", "peer is inside a run unit"))

    def test_a_row_that_is_not_a_scope_word_opens_nothing_and_a_table_that_raises_is_left_to_s5(self) -> None:
        for bad in (["run"], 5, {"scope": "run"}, ""):
            auth = authorizer(scope_of=lambda method, bad=bad: bad)
            self.assertEqual(auth(PRINCIPALS["owner"], "run.list", {}), ("scope_refused", "scope_unknown"), repr(bad))

        def broken(method):
            raise RuntimeError("table unavailable")

        with self.assertRaises(RuntimeError):   # S5 (control/hooks.py) refuses it as authorizer_failed and logs it
            authorizer(scope_of=broken)(PRINCIPALS["owner"], "run.list", {})


class ConstructionTest(unittest.TestCase):
    def test_the_providers_must_be_callable(self) -> None:
        for kwargs in ({"scope_of": None}, {"scope_of": "read"}, {"scope_of": SCOPE_COLUMN.get, "in_subtree": None},
                       {"scope_of": SCOPE_COLUMN.get, "in_subtree": True}):
            with self.assertRaises(TypeError):
                Authorizer(**kwargs)


class RegistrantTest(unittest.TestCase):
    def test_register_exists_and_registers_nothing_in_this_phase(self) -> None:
        class Untouchable:
            def __getattribute__(self, name):
                raise AssertionError(f"register touched hooks.{name}")

        self.assertTrue(callable(scopes.register))
        self.assertIsNone(scopes.register(Untouchable()))


if __name__ == "__main__":
    unittest.main()
