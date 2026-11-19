"""The stores refuse to open on an SQLite with the WAL-reset bug, and a person's own setting accepts the risk.

SQLite's page (wal.html, section 11) names the bug in 3.7.0 through 3.51.2 and the repair in 3.51.3 and later and
in the backports 3.44.6 and 3.50.7; 3.52.0 is a withdrawn release that SQLite's pages do not say is repaired, so it
is refused. The version is STUBBED in each test, never the machine's own, and every store, settings file and folder
a test makes lives in its own temporary folder.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from recursion_core import settings
from recursion_core.events import db as D
from recursion_core.plugin_security import (
    Authority, ConnectionBreaker, Refused, TrustStore,
    create_breaker_store, create_ledger, create_revocation_store,
)

POSIX_ONLY = "the stores and the settings file's owner check need a POSIX system"
ANCHOR = "11" * 32  # a public value in the anchor's format; nothing signs with it or verifies against it
REST = ("Recursion does not open its stores with it. Use a Python whose SQLite is 3.51.3 or later (or 3.50.7, or "
        "3.44.6), and check it with: python3 -c \"import sqlite3; print(sqlite3.sqlite_version)\". To accept the "
        "risk and go on, set stores.sqlite_wal_bug = \"accept_risk\" in $XDG_CONFIG_HOME/recursion/settings.toml.")
HAS_THE_BUG = "This Python uses SQLite {found}, which has a known bug that can damage a database in WAL mode. " + REST
WITHDRAWN = ("This Python uses SQLite {found}, a withdrawn release that SQLite's pages do not say is repaired: it may "
             "have a known bug that can damage a database in WAL mode. " + REST)
REFUSED = {"3.45.1": HAS_THE_BUG, "3.51.2": HAS_THE_BUG, "3.50.6": HAS_THE_BUG, "3.44.5": HAS_THE_BUG,
           "3.7.0": HAS_THE_BUG, "3.46.1": HAS_THE_BUG, "3.52.0": WITHDRAWN}
OPENS = ("3.51.3", "3.51.9", "3.50.7", "3.50.8", "3.44.6", "3.44.9", "3.53.0", "3.53.1", "3.60.2")


def stub(version: str):
    """The SQLite version every check in this test sees; the library itself is the machine's."""
    return mock.patch.object(sqlite3, "sqlite_version", version)


def code_and_detail(exc: BaseException) -> tuple[str, str]:
    return getattr(exc, "code", str(exc)), getattr(exc, "detail", "")


def _ledger(folder: Path, **kw):
    path = str(folder / "ledger.sqlite3")
    with stub("3.51.3"):
        create_ledger(path, ANCHOR)
    return Authority(ANCHOR, path, lambda: 0, **kw)


def _breaker(folder: Path, **kw):
    path = str(folder / "breaker.sqlite3")
    with stub("3.51.3"):
        create_breaker_store(path)
    return ConnectionBreaker(path, **kw)


def _revoked(folder: Path, **kw):
    path = str(folder / "revoked.sqlite3")
    with stub("3.51.3"):
        create_revocation_store(path)
    return TrustStore({}, revocation_store=path, **kw)


# Each open path of the four stores (the event store and its fake, the authority ledger, the breaker store, the
# revocation store): the store's own opener, given the folder and the person's choice as a keyword (none: default).
OPENERS = {
    "event store": lambda folder, **kw: D.EventStore(folder / "events.sqlite3", **kw),
    "event fake store": lambda folder, **kw: D.FakeStore(**kw),
    "authority ledger, created": lambda folder, **kw: create_ledger(str(folder / "new-ledger.sqlite3"), ANCHOR, **kw),
    "authority ledger, opened": _ledger,
    "breaker store, created": lambda folder, **kw: create_breaker_store(str(folder / "new-breaker.sqlite3"), **kw),
    "breaker store, opened": _breaker,
    "revocation store, created": lambda folder, **kw: create_revocation_store(str(folder / "new-revoked.sqlite3"),
                                                                             **kw),
    "revocation store, opened": _revoked,
}


def _close(store) -> None:
    close = getattr(store, "close", None)
    if close is not None:
        close()


def _listing(folder: Path) -> list[str]:
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*"))


@unittest.skipUnless(hasattr(os, "geteuid") and os.name == "posix", POSIX_ONLY)
class TheFourStoresOpenPaths(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.made = 0

    def folder(self) -> Path:
        self.made += 1
        path = self.root / f"case-{self.made}"
        path.mkdir(mode=0o700)
        return path

    def prepared(self, name: str) -> Path:
        """A folder holding what the opener needs before it opens (a created store for an 'opened' case)."""
        folder = self.folder()
        if name.endswith(", opened"):
            with stub("3.51.3"):
                {"authority ledger, opened": lambda: create_ledger(str(folder / "ledger.sqlite3"), ANCHOR),
                 "breaker store, opened": lambda: create_breaker_store(str(folder / "breaker.sqlite3")),
                 "revocation store, opened": lambda: create_revocation_store(str(folder / "revoked.sqlite3")),
                 }[name]()
        return folder

    def open_only(self, name: str, folder: Path, **kw):
        """The opener alone (the store an 'opened' case needs is already in the folder)."""
        if name == "authority ledger, opened":
            return Authority(ANCHOR, str(folder / "ledger.sqlite3"), lambda: 0, **kw)
        if name == "breaker store, opened":
            return ConnectionBreaker(str(folder / "breaker.sqlite3"), **kw)
        if name == "revocation store, opened":
            return TrustStore({}, revocation_store=str(folder / "revoked.sqlite3"), **kw)
        return OPENERS[name](folder, **kw)

    def assert_refused(self, name: str, version: str, **kw) -> None:
        folder = self.prepared(name)
        before = _listing(folder)
        made = mock.patch.object(D.tempfile, "mkdtemp", wraps=D.tempfile.mkdtemp)
        with stub(version), made as mkdtemp, self.assertRaises((Refused, D.StoreError)) as caught:
            _close(self.open_only(name, folder, **kw))
        self.assertEqual(("store_sqlite_wal_bug", REFUSED[version].format(found=version)),
                         code_and_detail(caught.exception))
        self.assertEqual(before, _listing(folder), "no file is created")
        self.assertEqual(0, mkdtemp.call_count, "no folder is made for a fake store")

    def test_an_affected_version_is_refused_with_the_message_and_no_file_is_made(self) -> None:
        for name in OPENERS:
            for version in REFUSED:
                with self.subTest(store=name, version=version):
                    self.assert_refused(name, version)

    def test_the_default_is_refuse(self) -> None:
        for name in OPENERS:
            for given in ({}, {"sqlite_wal_bug": "refuse"}, {"sqlite_wal_bug": "acept_risk"}):
                with self.subTest(store=name, given=given):
                    self.assert_refused(name, "3.45.1", **given)

    def test_a_repaired_version_opens_as_today(self) -> None:
        for name in OPENERS:
            for version in OPENS:
                with self.subTest(store=name, version=version):
                    folder = self.prepared(name)
                    with stub(version):
                        _close(self.open_only(name, folder))

    def test_accept_risk_opens_an_affected_version_and_logs_a_warning_naming_it(self) -> None:
        for name in OPENERS:
            with self.subTest(store=name):
                folder = self.prepared(name)
                with stub("3.45.1"), self.assertLogs(level="WARNING") as logs:
                    _close(self.open_only(name, folder, sqlite_wal_bug="accept_risk"))
                warned = [r.getMessage() for r in logs.records if r.levelname == "WARNING"]
                self.assertEqual(1, len(warned), warned)
                self.assertIn("SQLite 3.45.1", warned[0])
                self.assertIn("stores.sqlite_wal_bug = 'accept_risk'", warned[0])


class TheOneRule(unittest.TestCase):
    """One function and one table decide from the version text alone."""

    def test_the_table_of_versions(self) -> None:
        for version in OPENS + ("3.6.23",):  # before 3.7.0 there is no WAL and SQLite's page names no bug
            with self.subTest(version=version):
                self.assertIs(False, D.sqlite_wal_affected(version))
        for version in (*REFUSED, "3.52.1", "3.51.2.9", "three", ""):
            with self.subTest(version=version):
                self.assertIs(True, D.sqlite_wal_affected(version))

    def test_the_table_carries_its_rows_and_is_the_one_place(self) -> None:
        self.assertEqual({(3, 44): 6, (3, 50): 7, (3, 51): 3, (3, 52): None}, dict(D.SQLITE_WAL_RESET_REPAIRED))


class _PersonsOwnFolder(unittest.TestCase):
    """The person's config folder and home inside the test's own temporary folder."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.config = self.tmp / "config"
        (self.config / "recursion").mkdir(parents=True, mode=0o700)
        (self.tmp / "home").mkdir(mode=0o700)
        self.environ = {"XDG_CONFIG_HOME": str(self.config), "HOME": str(self.tmp / "home")}

    def put(self, text: str, path: Path | None = None) -> None:
        path = path or self.config / "recursion" / "settings.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        os.chmod(path, 0o600)


@unittest.skipUnless(hasattr(os, "geteuid") and os.name == "posix", POSIX_ONLY)
class TheSettingRow(_PersonsOwnFolder):
    def test_the_row_defaults_to_refuse_and_takes_only_its_two_texts(self) -> None:
        row = settings.DECLARED["stores.sqlite_wal_bug"]
        self.assertEqual((str, "refuse", ("refuse", "accept_risk")), (row.type, row.default, row.allowed))
        loaded = settings.load(self.environ)
        self.assertEqual(("refuse", settings.SOURCE_DEFAULT),
                         (loaded["stores.sqlite_wal_bug"], loaded.values["stores.sqlite_wal_bug"].source))
        self.assertIn("stores.sqlite_wal_bug = 'refuse' (default 'refuse'; source: default)", settings.lines(loaded))

    def test_accept_risk_in_the_persons_file_is_in_force_with_its_source(self) -> None:
        self.put('version = 1\n\n[stores]\nsqlite_wal_bug = "accept_risk"\n')
        loaded = settings.load(self.environ)
        self.assertEqual([], list(loaded.notes))
        self.assertIn("stores.sqlite_wal_bug = 'accept_risk' (default 'refuse'; source: settings file)",
                      settings.lines(loaded))

    def test_a_mistyped_text_keeps_refuse_with_one_typed_note_naming_the_key(self) -> None:
        for found in ('"acept_risk"', '"ACCEPT_RISK"', '"accept_risk "', '""', "true"):
            with self.subTest(found=found):
                self.put(f"version = 1\n\n[stores]\nsqlite_wal_bug = {found}\n")
                loaded = settings.load(self.environ)
                self.assertEqual("refuse", loaded["stores.sqlite_wal_bug"])
                self.assertEqual([settings.OUT_OF_RANGE], [note.code for note in loaded.notes])
                self.assertIn("stores.sqlite_wal_bug", loaded.notes[0].detail)
                self.assertIn("allowed: one of 'refuse' or 'accept_risk'", loaded.notes[0].detail)


class _Reached(Exception):
    pass


@unittest.skipUnless(hasattr(os, "geteuid") and os.name == "posix", POSIX_ONLY)
class TheDaemonsOneLoad(_PersonsOwnFolder):
    """The daemon reads the setting once, at start, and hands that value to the stores it opens."""

    def start(self, cwd: Path | None = None) -> tuple[list, dict]:
        from recursion_core.daemon import __main__ as daemon_main
        loads, real, given = [], settings.load, {}

        def counted(environ):
            loads.append(real(environ))
            return loads[-1]

        def composed(*args, **kwargs):
            given.update(kwargs)
            raise _Reached()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, self.environ))
            stack.enter_context(mock.patch.object(settings, "load", side_effect=counted))
            for name in ("require_posix", "require_free_threaded", "refuse_engine_user", "check_operator_linger",
                         "Registry", "EngineUnitManager", "Daemon"):
                stack.enter_context(mock.patch.object(daemon_main, name))
            stack.enter_context(mock.patch.object(daemon_main.logging, "basicConfig"))
            stack.enter_context(mock.patch.object(daemon_main.signal, "signal"))
            stack.enter_context(mock.patch.object(daemon_main, "UserLock",
                                                  return_value=contextlib.nullcontext()))
            stack.enter_context(mock.patch.object(daemon_main, "compose_control", side_effect=composed))
            stack.enter_context(self.assertLogs("recursion_core.daemon", "INFO"))
            if cwd is not None:
                here = os.getcwd()
                os.chdir(cwd)
                stack.callback(os.chdir, here)
            with self.assertRaises(_Reached):
                daemon_main.main()
        return loads, given

    def test_the_persons_value_reaches_the_composition_from_the_one_load(self) -> None:
        loads, given = self.start()
        self.assertEqual((1, "refuse"), (len(loads), given["sqlite_wal_bug"]))
        self.put('version = 1\n\n[stores]\nsqlite_wal_bug = "accept_risk"\n')
        loads, given = self.start()
        self.assertEqual((1, "accept_risk"), (len(loads), given["sqlite_wal_bug"]))

    def test_a_settings_file_in_a_project_folder_leaves_the_refusal_in_force(self) -> None:
        project = self.tmp / "project"
        text = 'version = 1\n\n[stores]\nsqlite_wal_bug = "accept_risk"\n'
        for place in (project / "settings.toml", project / "recursion" / "settings.toml",
                      project / ".recursion" / "settings.toml", project / ".config" / "recursion" / "settings.toml"):
            self.put(text, place)
        loads, given = self.start(cwd=project)
        self.assertEqual((1, "refuse"), (len(loads), given["sqlite_wal_bug"]))
        with stub("3.45.1"), self.assertRaises(D.StoreError) as caught:
            D.EventStore(project / "events.sqlite3", sqlite_wal_bug=given["sqlite_wal_bug"])
        self.assertEqual("store_sqlite_wal_bug", caught.exception.code)

    def test_the_composition_hands_the_value_to_the_breaker_store_and_the_plugin_host(self) -> None:
        from recursion_core.daemon import control_adapter as ca
        seen = {}

        def breaker_store(state_dir, **kwargs):
            seen["breaker store"] = kwargs.get("sqlite_wal_bug")
            raise Refused("store_sqlite_wal_bug", "stand-in")

        def host(*args, **kwargs):
            seen["plugin host"] = kwargs.get("sqlite_wal_bug")
            raise _Reached()
        daemon = mock.Mock(manager=mock.Mock(spec=ca.EngineUnitManager))
        for choice in ("refuse", "accept_risk"):
            with self.subTest(choice=choice), \
                    mock.patch.object(ca, "DurableRunRegistry"), \
                    mock.patch.object(ca, "open_breaker_store", side_effect=breaker_store), \
                    mock.patch.object(ca, "PluginHost", side_effect=host), \
                    self.assertLogs("recursion_core.daemon", "ERROR"), \
                    self.assertRaises(_Reached):
                ca.compose_control(daemon, self.tmp / "state", environ=self.environ, routes={},
                                   sqlite_wal_bug=choice)
            self.assertEqual({"breaker store": choice, "plugin host": choice}, seen)

    def test_the_daemons_breaker_store_and_the_plugin_hosts_revocation_store_take_the_value(self) -> None:
        from recursion_core.daemon import breaker as daemon_breaker
        from recursion_core.daemon import plugins
        state = self.tmp / "state"
        state.mkdir(mode=0o700)
        host = plugins.PluginHost.__new__(plugins.PluginHost)  # only the state folder and the value it was given
        host._state, host._sqlite_wal_bug = state, "refuse"
        with stub("3.45.1"):
            for opener in (lambda: daemon_breaker.open_breaker_store(state, clock=lambda: 0.0),
                           lambda: daemon_breaker.open_breaker_store(state, clock=lambda: 0.0,
                                                                     sqlite_wal_bug="refuse"),
                           host._revocation_store):
                with self.assertRaises(Refused) as caught:
                    opener()
                self.assertEqual("store_sqlite_wal_bug", caught.exception.code)
            self.assertEqual([], [p.name for p in state.rglob("*") if p.is_file()], "no store file is made")
            host._sqlite_wal_bug = "accept_risk"
            with self.assertLogs(level="WARNING"):
                store, created = daemon_breaker.open_breaker_store(state, clock=lambda: 0.0,
                                                                   sqlite_wal_bug="accept_risk")
            store.close()
            with self.assertLogs(level="WARNING"):
                host._revocation_store()
        self.assertTrue(created)


if __name__ == "__main__":
    unittest.main()
