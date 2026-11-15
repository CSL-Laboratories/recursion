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


class RoundTwoScopesTest(unittest.TestCase):
    """The round-2 read of #377 by Codex GPT-6.1 Sol: 2 (required) and B10 (later coverage)."""

    def test_a_principal_whose_grant_cannot_be_read_holds_nothing(self) -> None:
        class Unreadable:
            kind, id, in_run_unit = "plugin", "plugin.alpha", False

            @property
            def grant(self):
                raise RuntimeError("the trusted grant context is unavailable")

        class UnreadableOwner(Unreadable):
            kind, id = "owner", "local"

        auth = authorizer(credentials=Grants({("plugin", "plugin.alpha"): {"read", "run"}}))
        self.assertEqual(auth.held(Principal("plugin", "plugin.alpha")), frozenset({"read", "run"}))  # absent grant
        for principal in (Unreadable(), UnreadableOwner()):
            with self.subTest(kind=principal.kind):
                self.assertEqual(auth.held(principal), frozenset())
                self.assertEqual(auth(principal, "run.start", {}), ("scope_refused", "needs_run"))

    def test_a_run_whose_session_is_empty_is_in_nobody_s_subtree(self) -> None:
        runs = {"run-empty": {"run_id": "run-empty", "launch": {"owner": "host", "session_id": ""}}}
        auth = authorizer(credentials=Grants({("session", "s1"): {"run"}}), run_record=runs.get,
                          in_subtree=lambda run_session, principal_session: True)
        self.assertEqual(auth(bearer("session", "s1"), "run.stop", {"run_id": "run-empty"}),
                         ("scope_refused", "run_outside_session"))


class RoundThreeScopesTest(unittest.TestCase):
    """The round-3 reads of #377 (Codex GPT-6 Astra 1 and 4, GPT-6.1 Sol 1 and 2)."""

    def test_a_declared_grant_accessor_raising_attributeerror_is_unreadable(self) -> None:
        # Astra 1, Sol 1 (STRICT): getattr(..., None) took the accessor's AttributeError for an absent field.
        class Declared:
            in_run_unit = False

            def __init__(self, kind, principal_id):
                self.kind, self.id = kind, principal_id

            @property
            def grant(self):
                raise AttributeError("the trusted grant context is unavailable")

        auth = authorizer(credentials=Grants({("plugin", "plugin.alpha"): {"read", "run"}}))
        for kind, principal_id in (("plugin", "plugin.alpha"), ("owner", "local"), ("device", "page")):
            with self.subTest(kind=kind):
                principal = Declared(kind, principal_id)
                self.assertEqual(auth.held(principal), frozenset())
                self.assertEqual(auth(principal, "run.start", {}), ("scope_refused", "needs_run"))

    def test_a_grant_that_is_truly_absent_is_still_absent(self) -> None:
        import types as _types

        class Dynamic:
            kind, id, in_run_unit = "plugin", "plugin.alpha", False

            def __getattr__(self, name):
                raise AttributeError(name)        # nothing declared: a genuinely missing field

        auth = authorizer(credentials=Grants({("plugin", "plugin.alpha"): {"read", "run"}}))
        for principal in (_types.SimpleNamespace(kind="plugin", id="plugin.alpha", in_run_unit=False), Dynamic(),
                          Principal("plugin", "plugin.alpha")):
            with self.subTest(principal=type(principal).__name__):
                self.assertEqual(auth.held(principal), frozenset({"read", "run"}))
        self.assertEqual(auth.held(_types.SimpleNamespace(kind="owner", id="local", in_run_unit=False)),
                         scopes.ALL_SCOPES)

    @unittest.skipUnless(POSIX, "the credential store needs uids and O_NOFOLLOW (POSIX)")
    def test_a_bearer_sees_another_store_s_revocation_at_its_first_check(self) -> None:
        # Astra 4, Sol 2 (STRICT proof): no other reader of this store runs between the revocation and the check.
        base = tempfile.mkdtemp(prefix="scopes-r3-")
        self.addCleanup(shutil.rmtree, base, True)
        path = os.path.join(base, "state", "credentials.json")
        writer = CredentialStore(path)
        _, grant = writer.mint("plugin", "plugin.alpha", ["read", "run"])
        auth = authorizer(credentials=CredentialStore(path))
        principal = Principal("plugin", "plugin.alpha", grant=grant.token_id)
        self.assertIsNone(auth(principal, "run.start", {}))
        writer.revoke(grant.token_id)
        self.assertEqual(auth(principal, "run.start", {}), ("scope_refused", "needs_run"))


class RoundFourScopesTest(unittest.TestCase):
    """The round-4 read of #377 by Codex GPT-6 Astra, 1 (STRICT): a __getattr__ fallback hid a declared grant's
    failed read."""

    def test_a_fallback_never_stands_in_for_a_declared_grant_whose_read_failed(self) -> None:
        def principal(kind, principal_id, fallback):
            class Declared:
                in_run_unit = False

                def __init__(self):
                    self.kind, self.id = kind, principal_id

                @property
                def grant(self):
                    raise AttributeError("the trusted grant context is unavailable")

                def __getattr__(self, name):
                    return fallback                # what Python answers after the accessor's AttributeError

            return Declared()

        auth = authorizer(credentials=Grants({("plugin", "plugin.alpha"): {"read", "run"}}))
        for fallback in (None, g("plugin", "plugin.alpha")):
            for kind, principal_id in (("plugin", "plugin.alpha"), ("owner", "local"), ("device", "page")):
                with self.subTest(fallback=fallback, kind=kind):
                    who = principal(kind, principal_id, fallback)
                    self.assertEqual(auth.held(who), frozenset())
                    self.assertEqual(auth(who, "run.start", {}), ("scope_refused", "needs_run"))

    def test_a_grant_nothing_declares_is_read_the_dynamic_way(self) -> None:
        class Dynamic:
            kind, id, in_run_unit = "plugin", "plugin.alpha", False

            def __init__(self, answer):
                self.answer = answer

            def __getattr__(self, name):
                if name == "grant":
                    return self.answer
                raise AttributeError(name)

        auth = authorizer(credentials=Grants({("plugin", "plugin.alpha"): {"read", "run"}}))
        self.assertEqual(auth.held(Dynamic(None)), frozenset({"read", "run"}))            # absent: identity grants
        self.assertEqual(auth.held(Dynamic(g("plugin", "plugin.alpha"))), frozenset({"read", "run"}))   # its grant
        self.assertEqual(auth.held(Dynamic("g-plugin-other")), frozenset())             # a grant not its own


class _NoStore:
    """A store the authorizer must not consult: any read of it fails the test."""

    def held(self, *args):
        raise AssertionError("the identity's grants were read")

    def grant_scopes(self, *args):
        raise AssertionError("a grant was read")


class RoundFiveScopesTest(unittest.TestCase):
    """The round-5 reads of #377: Codex GPT-6 Astra 1 and GPT-6.1 Sol 1 (STRICT), DeepSeek V4.1 Flash F1, F5, F6."""

    def test_a_declared_grant_is_read_with_the_type_s_own_reader(self) -> None:
        # Astra 1, Sol 1: object.__getattribute__ skipped a custom __getattribute__, so a declared default (None)
        # stood in for its failed read, and a grant it would supply was ignored.
        def principal(kind, principal_id, answer):
            class Custom:
                grant = None                       # the declared default the type's reader overrides
                in_run_unit = False

                def __init__(self):
                    self.kind, self.id = kind, principal_id

                def __getattribute__(self, name):
                    if name == "grant":
                        if isinstance(answer, BaseException):
                            raise answer
                        return answer
                    return object.__getattribute__(self, name)

                def __getattr__(self, name):
                    return None                    # never consulted for a declared grant

            return Custom()

        refusing = authorizer(credentials=_NoStore())
        for failure in (AttributeError("context unavailable"), RuntimeError("context unavailable")):
            for kind, principal_id in (("plugin", "plugin.alpha"), ("owner", "local"), ("device", "page")):
                with self.subTest(failure=type(failure).__name__, kind=kind):
                    who = principal(kind, principal_id, failure)
                    self.assertEqual(refusing.held(who), frozenset())
                    self.assertEqual(refusing(who, "run.start", {}), ("scope_refused", "needs_run"))
        auth = authorizer(credentials=Grants({("plugin", "plugin.alpha"): {"read", "run"}}))
        self.assertEqual(auth.held(principal("plugin", "plugin.alpha", g("plugin", "plugin.alpha"))),
                         frozenset({"read", "run"}))                         # the supplied grant is the one used
        self.assertEqual(auth.held(principal("plugin", "plugin.alpha", "g-plugin-other")), frozenset())

    def test_a_grant_declared_on_the_metaclass_is_unreadable(self) -> None:
        # DeepSeek F6: getattr_static on the instance does not see a metaclass property, so it read as absent.
        class Meta(type):
            @property
            def grant(cls):
                raise RuntimeError("context unavailable")

        class OnMeta(metaclass=Meta):
            kind, id, in_run_unit = "plugin", "plugin.alpha", False

        auth = authorizer(credentials=Grants({("plugin", "plugin.alpha"): {"read", "run"}}))
        self.assertEqual(auth.held(OnMeta()), frozenset())

    def test_a_principal_naming_a_grant_is_never_owner_or_internal(self) -> None:
        # DeepSeek F5: owner and internal were tested before grant, so a token principal claiming them held all.
        auth = authorizer(credentials=Grants({("device", "d1"): {"read"}}))
        for kind in ("owner", "internal"):
            with self.subTest(kind=kind):
                self.assertEqual(auth.held(Principal(kind, "local", grant=g("device", "d1"))), frozenset())
                self.assertEqual(auth.held(Principal(kind, "local")), scopes.ALL_SCOPES)

    def test_a_stop_target_whose_reads_raise_is_not_owned(self) -> None:
        # DeepSeek F1: params.get, record.get and launch.get sat outside every try in the Stop fence.
        class Raising(dict):
            def get(self, *args, **kwargs):
                raise RuntimeError("unreadable")

        good_launch = {"owner": "plugin.alpha", "session_id": "s1"}
        cases = {
            "params": (Raising(run_id="r"), {"r": {"launch": good_launch}}),
            "record": ({"run_id": "r"}, {"r": Raising(launch=good_launch)}),
            "launch": ({"run_id": "r"}, {"r": {"launch": Raising(good_launch)}}),
        }
        for where, (params, runs) in cases.items():
            auth = authorizer(credentials=Grants({("plugin", "plugin.alpha"): {"run"}, ("session", "s1"): {"run"}}),
                              run_record=runs.get)
            with self.subTest(where=where):
                self.assertEqual(auth(Principal("plugin", "plugin.alpha"), "run.stop", params),
                                 ("scope_refused", "run_not_owned"))
                self.assertIn(auth(bearer("session", "s1"), "run.stop", params),
                              (("scope_refused", "run_not_owned"), ("scope_refused", "run_outside_session")))
                self.assertIsNone(auth(PRINCIPALS["owner"], "run.stop", params))


class RoundSixScopesTest(unittest.TestCase):
    """The round-6 reads of #377: Codex GPT-6.1 Sol 1 and GPT-6 Astra 1 (STRICT: a metaclass answered for the
    type's reader, so a declared default stood in for the real read), DeepSeek V4.1 Flash 3 and 5."""

    class SwapsReader(type):
        """Hands back object's own reader when asked for the class's, hiding a custom __getattribute__."""

        def __getattribute__(cls, name):
            if name == "__getattribute__":
                return object.__getattribute__
            return type.__getattribute__(cls, name)

    def principal(self, kind, principal_id, answer):
        class Swapped(metaclass=self.SwapsReader):
            grant = None                       # the declared default the metaclass's reader would read
            in_run_unit = False

            def __init__(self):
                self.kind, self.id = kind, principal_id

            def __getattribute__(self, name):
                if name == "grant":
                    if isinstance(answer, BaseException):
                        raise answer
                    return answer
                return object.__getattribute__(self, name)

        return Swapped()

    def test_declared_reader_is_not_replaced_by_metaclass_attribute_lookup(self) -> None:
        # Sol 1's test; Astra 1's ReaderSelection test is the same case. Here for every kind that holds scopes
        # without a grant, so an owner, internal or the page cannot keep theirs through the swapped reader either.
        refusing = authorizer(credentials=_NoStore())
        for failure in (AttributeError("context unavailable"), RuntimeError("context unavailable")):
            for kind, principal_id in (("plugin", "plugin.alpha"), ("owner", "local"), ("internal", "daemon"),
                                       ("device", "page")):
                with self.subTest(failure=type(failure).__name__, kind=kind):
                    who = self.principal(kind, principal_id, failure)
                    self.assertEqual(refusing.held(who), frozenset())
                    self.assertEqual(refusing(who, "run.start", {}), ("scope_refused", "needs_run"))

    def test_metaclass_lookup_cannot_discard_a_readable_named_context(self) -> None:
        # Sol 1's companion (Astra 1's supplied-value case): the value the type's reader supplies is the grant used.
        seen = []

        class RejectingStore:
            def held(self, *args):
                raise AssertionError("a supplied grant must be the only one read")

            def grant_scopes(self, *args):
                seen.append(args)
                return frozenset()

        auth = authorizer(credentials=RejectingStore())
        self.assertEqual(auth.held(self.principal("plugin", "synthetic", "synthetic-grant-id")), frozenset())
        self.assertEqual(seen, [("synthetic-grant-id", "plugin", "synthetic")])
        live = authorizer(credentials=Grants({("plugin", "plugin.alpha"): {"read", "run"}}))
        self.assertEqual(live.held(self.principal("plugin", "plugin.alpha", g("plugin", "plugin.alpha"))),
                         frozenset({"read", "run"}))
        self.assertEqual(live.held(self.principal("owner", "local", g("plugin", "plugin.alpha"))), frozenset())

    def test_a_grant_a_getattr_supplies_holds_only_that_grant(self) -> None:
        # DeepSeek 3: the module text said no __getattr__ is ever read. Nothing is declared here, so the ordinary
        # read is used; the grant it supplies counts as named, and never widens the principal.
        def dynamic(kind, principal_id):
            class Dynamic:
                in_run_unit = False

                def __init__(self):
                    self.kind, self.id = kind, principal_id

                def __getattr__(self, name):
                    if name == "grant":
                        return g("plugin", "plugin.alpha")
                    raise AttributeError(name)

            return Dynamic()

        auth = authorizer(credentials=Grants({("plugin", "plugin.alpha"): {"read"}}))
        self.assertEqual(auth.held(dynamic("plugin", "plugin.alpha")), frozenset({"read"}))
        for kind, principal_id in (("owner", "local"), ("internal", "daemon"), ("device", "page")):
            with self.subTest(kind=kind):
                self.assertEqual(auth.held(dynamic(kind, principal_id)), frozenset())

    def test_a_method_name_that_compares_unequal_cannot_step_past_the_stop_row(self) -> None:
        # DeepSeek 5: method == STOP_METHOD was the caller's own comparison, so a str subclass answering False
        # skipped the Stop row and a plugin stopped a host-owned run on its scope column alone.
        class Liar(str):
            def __eq__(self, other):
                return False

            def __ne__(self, other):
                return True

            __hash__ = str.__hash__

        asked = []

        def scope_of(method):
            asked.append(type(method))
            return "run"

        auth = Authorizer(scope_of=scope_of, credentials=Grants({("plugin", "plugin.alpha"): {"read", "run"}}),
                          run_record={"run-host": {"launch": {"owner": "host", "session_id": "s1"}}}.get)
        plugin = Principal("plugin", "plugin.alpha")
        for method in ("run.stop", Liar("run.stop")):
            with self.subTest(method=type(method).__name__):
                self.assertEqual(auth(plugin, method, {"run_id": "run-host"}), ("scope_refused", "run_not_owned"))
        self.assertEqual(asked, [str, str])


class RoundSevenScopesTest(unittest.TestCase):
    """The round-7 read of #377 by Codex GPT-6.1 Sol, 1 (STRICT): a metaclass answering __dict__ hid a declared
    grant from getattr_static, so its reader's AttributeError read as an absent grant."""

    def test_declared_context_cannot_be_hidden_by_metaclass_dictionary_lookup(self) -> None:
        # Sol's test, as written in its proposed-regression-test.diff.
        class RejectIdentity:
            def held(self, *args):
                raise AssertionError("declared unreadable context reached identity store")

        class Meta(type):
            def __getattribute__(cls, name):
                if name == "__dict__":
                    return {}
                return type.__getattribute__(cls, name)

        class Hidden(metaclass=Meta):
            kind, id = "plugin", "synthetic"
            grant = None

            def __getattribute__(self, name):
                if name == "grant":
                    raise AttributeError("synthetic context unavailable")
                return object.__getattribute__(self, name)

        class Child(Hidden):
            pass

        auth = Authorizer(scope_of=lambda _: None, credentials=RejectIdentity())
        for principal in (Hidden(), Child()):
            with self.subTest(inherited=type(principal) is Child):
                self.assertEqual(auth.held(principal), frozenset())


class RoundEightScopesTest(unittest.TestCase):
    """The round-8 read of #377 by DeepSeek V4.1 Flash, 2 (coverage): with the instance half of the declaration
    check removed, every test stayed green, and a grant declared on the instance whose read fails fell through to
    the plugin's identity grants."""

    def test_a_grant_declared_on_the_instance_whose_read_fails_holds_nothing(self) -> None:
        class RejectIdentity:
            def held(self, *args):
                raise AssertionError("an instance-declared unreadable grant reached the identity store")

        class InstanceDeclares:
            kind, id, in_run_unit = "plugin", "plugin.alpha", False

            def __getattribute__(self, name):
                if name == "grant":
                    raise AttributeError("synthetic context unavailable")
                return object.__getattribute__(self, name)

        principal = InstanceDeclares()
        object.__setattr__(principal, "grant", "g-plugin-plugin.alpha")   # declared on the instance only
        self.assertEqual(authorizer(credentials=RejectIdentity()).held(principal), frozenset())
        live = authorizer(credentials=Grants({("plugin", "plugin.alpha"): {"read", "run"}}))
        self.assertEqual(live.held(principal), frozenset())
        self.assertEqual(live(principal, "run.start", {}), ("scope_refused", "needs_run"))


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
