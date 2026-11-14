"""The seams' own modules: the hooks and their defaults, the method, command and engine tables.

Every hook has a test that registers a fake and sees it called with the documented arguments, a test of
its default (which is what the daemon does today), and a test that a hook that fails does not fail open.
The calls from the daemon's own files into these hooks are tested where those files are.

``FakeStore`` is the one fake of the event store's transaction helper, for a lane that builds a handler
before the event store exists.

This fake implements the published EVENTS transaction protocol. Only declared tables can be accessed. A
transaction has one active owner; nested transaction entry in the same thread raises. Reads and writes
own their values. Rollback leaves rows and receipts unchanged. After transaction exit, transaction
methods refuse use. Receipt identity is immutable. Closed stores refuse access. Concurrent commits either
preserve both transactions or reject a conflict; they never silently replace committed work from a stale
snapshot. Tests that need unsupported store operations fail explicitly.

Lane tables are declared through ``db.register_tables(lane, version, ddl)`` and accessed through the EVENTS
transaction's ``execute(sql, params)``; events use ``tx.append(event)``. There is no shared
``tx.put(table, key, value)`` API. The seam tests use the same helper protocol as EVENTS. A test-only
convenience is not a contract for lane handlers.

What the fake is NOT: it is in memory of one process (a file in a temporary folder that goes with it), so
no test against it proves recovery from a crash or the resolution of an ambiguous commit; ``fail_commit``
models a failure before anything is published, and nothing else. Its event table keeps what was appended
and the order, and nothing of the ledger's own schema, which is the event lane's. The shapes the
published protocol leaves open (what ``receipt_get`` and ``execute`` return) are this fake's reading until
the event lane publishes them: ``(payload_digest, answer)`` or None, and a list of rows.

This is a provisional SQL subset, not a second EVENTS implementation. EVENTS owns the helper's return
shapes, supported schema/SQL features, value profiles and version behavior. Registration is
composition-only and atomic; it cannot alter helper-owned receipt/event tables. Every accepted schema
constraint is enforced, and unsupported features are rejected at registration. Replace this class with
the EVENTS-owned shared fake when it is available; consumer tests must not choose whichever fake passes.

This temporary fixture supports the declaration/SQL/value profile stated here. EVENTS owns migrations,
pragma policy, receipt/event validation and helper return shapes. A consumer requiring a view, migration
or another unsupported operation must use the EVENTS fake; passing this fixture is not ledger conformance.

The profile: a declaration is ``CREATE TABLE`` and ``CREATE INDEX`` statements and nothing else (no view,
no trigger, no change to a table that exists), once for a lane, all of it or none of it. Foreign keys a
declaration states are enforced. ``execute`` reads and writes rows of declared tables. A value that is
kept as JSON has text keys. The store closes only when no transaction is open: a close that finds one is
refused, and a transaction that waited for its turn while the store closed is refused when its turn comes.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import importlib
import importlib.util
import inspect
import io
import json
import logging
import os
import pathlib
import shutil
import signal
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import weakref
from unittest import mock

from recursion_core.control import commands, hooks as H, methods
from recursion_core.routes import ClaudeCodeAdapter, CodexAdapter, engines

REPO = pathlib.Path(__file__).resolve().parents[4]
BOUND_S = 30.0   # only ends a wait that would otherwise never end; it never decides a pass
SIGNAL_SETTLE_S = 2.0   # see the one test that sends a real signal; it never decides a pass either


def _wire(value) -> str:
    """A value as the store keeps it: JSON, or it is refused. So what is read back is owned by the reader. An
    object's keys are text: a key of another type is refused, where a serialiser would quietly make it text."""
    def keys_are_text(item) -> None:
        if isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                raise TypeError('an object key is text')
            for inner in item.values():
                keys_are_text(inner)
        elif isinstance(item, (list, tuple)):
            for inner in item:
                keys_are_text(inner)
    keys_are_text(value)
    return json.dumps(value, allow_nan=False, sort_keys=True)


class FakeTx:
    """One open transaction, with the published names: ``receipt_get``, ``receipt_put``, ``append``, ``execute``.

    Only the thread that opened it may use it, and only until it exits."""

    def __init__(self, store, db) -> None:
        self._store, self._db = store, db
        self._owner, self._active = threading.get_ident(), True

    def _use(self) -> None:
        # (A store cannot close under an open transaction, so there is no "closed" to ask about here.)
        if not self._active or threading.get_ident() != self._owner:
            raise RuntimeError('transaction_not_active_for_caller')

    def receipt_get(self, tenant, method, command_id):
        """None, or ``(payload_digest, answer)``; the answer is the reader's own copy."""
        self._use()
        row = self._db.execute('SELECT payload_digest, answer FROM receipts WHERE tenant = ? AND method = ? '
                               'AND command_id = ?', (tenant, method, command_id)).fetchone()
        return None if row is None else (row[0], json.loads(row[1]))

    def receipt_put(self, tenant, method, command_id, payload_digest, answer) -> None:
        """Keep the receipt. An identity that already has one is refused: a receipt is never replaced."""
        self._use()
        if not all(isinstance(part, str) and part for part in (tenant, method, command_id, payload_digest)):
            raise TypeError('a receipt is keyed by text')
        self._db.execute('INSERT INTO receipts VALUES (?, ?, ?, ?, ?)',
                         (tenant, method, command_id, payload_digest, _wire(answer)))

    def append(self, event) -> int:
        """One event; its position is assigned here and returned."""
        self._use()
        if not isinstance(event, dict):
            raise TypeError('an event is an object')
        return self._db.execute('INSERT INTO events (event) VALUES (?)', (_wire(event),)).lastrowid

    def execute(self, sql, params=()) -> list:
        """Run one statement on tables a lane declared; any other table, and anything that is not a read or a
        write of rows, is refused. Returns the rows."""
        self._use()
        self._db.set_authorizer(self._store._lane_tables_only)
        try:
            return self._db.execute(sql, params).fetchall()
        finally:
            self._db.set_authorizer(None)


class FakeStore:
    """``with store.transaction() as tx``: commits on a normal exit, rolls back when the block raises.

    By default a transaction takes the write lock when it begins, so one is open at a time and a second
    waits. ``serial=False`` is a store that does not: two transactions can be open at once, each over the
    state as it was when it first read, and the second to write what the first changed is REFUSED (a
    conflict), never silently applied over it. The dispatcher must not depend on which kind it is given.

    ``receipts``, ``rows``, ``events``, ``commits``, ``rollbacks`` and ``open_transactions`` are for a
    test's assertions: copies of what is committed, read outside every transaction. They are not part of the
    helper and nothing can be written through them.
    """

    def __init__(self, log: list | None = None, serial: bool = True) -> None:
        self._folder = tempfile.mkdtemp(prefix='seam-fake-store-')
        self._finalizer = weakref.finalize(self, shutil.rmtree, self._folder, True)
        self._path = os.path.join(self._folder, 'events.db')
        self._serial = serial
        self._write, self._guard, self._local = threading.Lock(), threading.Lock(), threading.local()
        self._lanes: dict = {}          # lane -> the version of its tables
        self._tables: set = set()       # every table a lane declared
        self.commits = self.rollbacks = self.open_transactions = 0
        self.fail_commit = False
        self.closed = False
        self._log = log
        with contextlib.closing(self._connect()) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE receipts (tenant TEXT NOT NULL, method TEXT NOT NULL, command_id TEXT NOT NULL, '
                       'payload_digest TEXT NOT NULL, answer TEXT NOT NULL, PRIMARY KEY (tenant, method, command_id))')
            db.execute('CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL)')

    def _connect(self):
        # A transaction that meets another's lock is refused at once where the store is not serial; where it is,
        # the write lock above means it never meets one.
        db = sqlite3.connect(self._path, isolation_level=None, timeout=0, check_same_thread=False)
        db.execute('PRAGMA foreign_keys=ON')    # a constraint a declaration states is enforced, on every connection
        return db

    def _names(self, db) -> set:
        return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    def _lane_tables_only(self, action, first, second, database, source) -> int:
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION):
            return sqlite3.SQLITE_OK
        if action in (sqlite3.SQLITE_READ, sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE) \
                and first in self._tables:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    def register_tables(self, lane, version, ddl) -> None:
        """Declare a lane's tables, once, at composition. Only tables declared here can be reached by ``execute``.

        A declaration can only declare: ``CREATE TABLE`` and ``CREATE INDEX`` on its own tables. Anything else
        in it (a write to the helper's own tables, a view, a trigger, a change to an existing table) is refused,
        and a declaration that fails leaves NOTHING behind: every statement of it is undone and the lane can
        declare again. It is made under the same guard as closing, so none is made on a closed store."""
        with self._guard:
            if self.closed:
                raise RuntimeError('store_closed')
            if not isinstance(lane, str) or not lane or lane in self._lanes:
                raise RuntimeError('a lane declares its tables once')
            created: set = set()
            indexes: set = set()

            def declaration_only(action, first, second, database, source) -> int:
                if action == sqlite3.SQLITE_CREATE_TABLE:
                    if first in ('receipts', 'events'):
                        return sqlite3.SQLITE_DENY
                    created.add(first)
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_CREATE_INDEX:
                    indexes.add(first)      # only on a table of its own: indexing a table READs it, refused below
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_REINDEX:        # what creating an index asks for, of that same index
                    return sqlite3.SQLITE_OK if first in indexes else sqlite3.SQLITE_DENY
                if first in ('sqlite_master', 'sqlite_schema'):
                    return sqlite3.SQLITE_OK        # what creating a table writes, and nothing a statement can name
                if action == sqlite3.SQLITE_READ and first in created:
                    return sqlite3.SQLITE_OK
                if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION):
                    return sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY

            with contextlib.closing(self._connect()) as db:
                before = self._names(db)
                db.execute('BEGIN IMMEDIATE')
                try:
                    db.set_authorizer(declaration_only)
                    statement = ''
                    for char in ddl:
                        statement += char
                        if sqlite3.complete_statement(statement):
                            db.execute(statement)
                            statement = ''
                    if statement.strip():
                        db.execute(statement)
                    db.set_authorizer(None)
                    added = self._names(db) - before
                    if not added:
                        raise RuntimeError('the declaration made no table')
                    db.execute('COMMIT')
                except BaseException:
                    db.set_authorizer(None)
                    with contextlib.suppress(sqlite3.Error):
                        db.execute('ROLLBACK')
                    raise
            self._lanes[lane] = version
            self._tables |= added

    @contextlib.contextmanager
    def transaction(self):
        if getattr(self._local, 'transaction', None) is not None:
            raise RuntimeError('nested_transaction')
        if self._serial:
            self._write.acquire()
        db = tx = None
        counted = False
        try:
            # ONE guard decides "is it closed" and counts the open transactions. So a transaction that waited
            # for the write lock while the store closed is refused here, when its turn comes; and close() is
            # refused from here until this transaction's connection is gone.
            with self._guard:
                if self.closed:
                    raise RuntimeError('store_closed')
                self.open_transactions += 1
                counted = True
            db = self._connect()
            db.execute('BEGIN IMMEDIATE' if self._serial else 'BEGIN')
            tx = FakeTx(self, db)
            self._local.transaction = tx
            try:
                yield tx
                if self.fail_commit:
                    raise OSError('the commit failed')
                db.execute('COMMIT')
            except BaseException:
                with self._guard:
                    self.rollbacks += 1
                with contextlib.suppress(sqlite3.Error):
                    db.execute('ROLLBACK')
                raise
            with self._guard:
                self.commits += 1
        finally:
            # Always, on exit: a commit, a rollback, a failed commit, a transaction that could not begin.
            if tx is not None:
                tx._active = False
                self._local.transaction = None
            if db is not None:
                db.close()
            if counted:
                with self._guard:
                    self.open_transactions -= 1
            if self._serial:
                self._write.release()

    def close(self) -> None:
        """Close the store. Refused (``transactions_active``) while a transaction is open: a close never returns
        with a commit still to come. The caller ends its transactions first; this fake does not wait for them."""
        with self._guard:
            if self.open_transactions:
                raise RuntimeError('transactions_active')
            self.closed = True
        if self._log is not None:
            self._log.append(('close', 'store', None))

    # -- for assertions only: copies of what is committed
    def _read(self, sql) -> list:
        with contextlib.closing(self._connect()) as db:
            return db.execute(sql).fetchall()

    @property
    def receipts(self) -> dict:
        return {(tenant, method, command_id): (digest, json.loads(answer))
                for tenant, method, command_id, digest, answer in self._read('SELECT * FROM receipts')}

    @property
    def events(self) -> list:
        return [json.loads(event) for (event,) in self._read('SELECT event FROM events ORDER BY seq')]

    @property
    def rows(self) -> dict:
        """Every row of every declared table: ``{(table, its first column): {the other columns}}``."""
        out = {}
        with contextlib.closing(self._connect()) as db:
            for table in sorted(self._tables):
                cursor = db.execute(f'SELECT * FROM "{table}"')
                names = [column[0] for column in cursor.description]
                for row in cursor.fetchall():
                    out[(table, row[0])] = dict(zip(names[1:], row[1:]))
        return out


WORK = 'CREATE TABLE work (command_id TEXT NOT NULL, operation_id TEXT NOT NULL, state TEXT NOT NULL)'


def work_store(**options) -> FakeStore:
    """A fake store with one lane's work table declared, as that lane would at composition."""
    store = FakeStore(**options)
    store.register_tables('sessions', 1, WORK)
    return store


class Cut(BaseException):
    """Stands for the process ending at this point: nothing after it runs."""


def quiet(test):
    """No log output from a test that makes a hook fail on purpose."""
    def wrapped(self):
        logging.disable(logging.CRITICAL)
        try:
            return test(self)
        finally:
            logging.disable(logging.NOTSET)
    wrapped.__name__, wrapped.__doc__ = test.__name__, test.__doc__
    return wrapped


class FakeLanes:
    """Registrant modules under their REAL names, for one test. The composition imports its registrants with
    literal statements, so a test cannot hand it a name: it writes the lane's package into a temporary folder
    and puts that folder on the path of the package the lane sits in (``recursion_core`` for a lane's own
    package), for as long as the test runs."""

    def __init__(self, case: unittest.TestCase) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._dir.name)
        self._mounted: list = []
        self._names: list = []
        case.addCleanup(self._clean)

    def _folder(self, package: str) -> pathlib.Path:
        """A folder that is searched for the submodules of ``package``, a package of the core that exists."""
        folder = self.root / package
        if not folder.exists():
            module = importlib.import_module(package)
            folder.mkdir()
            module.__path__.append(str(folder))
            self._mounted.append((module, str(folder)))
        return folder

    def lane(self, name: str, init: str = '', **modules: str) -> str:
        """The package ``recursion_core.<name>`` with this ``__init__`` and these modules; its full name."""
        parent, _, leaf = f'recursion_core.{name}'.rpartition('.')
        folder = self._folder(parent) / leaf
        folder.mkdir()
        (folder / '__init__.py').write_text(init, encoding='utf-8')
        for module, text in modules.items():
            (folder / f'{module}.py').write_text(text, encoding='utf-8')
        self._names.append(f'{parent}.{leaf}')
        importlib.invalidate_caches()
        return f'{parent}.{leaf}'

    def module(self, name: str, text: str) -> str:
        """The module ``recursion_core.<name>`` inside a package of the core that exists; its full name."""
        parent, _, leaf = f'recursion_core.{name}'.rpartition('.')
        (self._folder(parent) / f'{leaf}.py').write_text(text, encoding='utf-8')
        self._names.append(f'{parent}.{leaf}')
        importlib.invalidate_caches()
        return f'{parent}.{leaf}'

    def _clean(self) -> None:
        for module, folder in self._mounted:
            module.__path__.remove(folder)
        for name in self._names:
            for loaded in [key for key in sys.modules if key == name or key.startswith(name + '.')]:
                del sys.modules[loaded]
            parent, _, leaf = name.rpartition('.')
            if hasattr(sys.modules.get(parent), leaf):
                delattr(sys.modules[parent], leaf)
        self._dir.cleanup()
        importlib.invalidate_caches()


CORE_PACKAGES = ('recursion_core', 'recursion_core.control', 'recursion_core.routes', 'recursion_core.plugin_security')
REGISTERS_NOTHING = 'def register(hooks):\n    pass\n'


class RegistrationTest(unittest.TestCase):
    """Absent is not broken, and absent is decided before the import."""

    def setUp(self) -> None:
        self.fake = FakeLanes(self)

    def test_nothing_is_installed_today_and_the_hooks_compose_with_their_defaults(self) -> None:
        composed = H.compose()
        self.assertFalse(composed.start_on)
        self.assertFalse(composed.gate.enabled)
        self.assertIsNone(composed.changes)
        self.assertEqual(composed.methods.registered(), ())
        self.assertEqual(composed.commands.names(), ())
        self.assertEqual(composed.engines.names(), ('claude_code', 'codex'))

    def test_the_registrants_are_guarded_static_imports_and_nothing_is_imported_by_a_computed_name(self) -> None:
        """The brief's words. ``compose()`` takes no list of names; the module calls nothing that imports by a
        computed name; and each registrant's own function holds, as literals, a ``find_spec`` for each package
        above it that is not part of the core, then for the module, and one ``import`` statement of exactly
        that module. Sixteen blocks written by hand: this is what catches a wrong name in one of them."""
        self.assertEqual(list(inspect.signature(H.compose).parameters), [])
        self.assertEqual(tuple(name for name, _ in H._LOADERS), H.REGISTRANTS)
        self.assertEqual(len(set(H.REGISTRANTS)), 16)
        tree = ast.parse(pathlib.Path(H.__file__).read_text(encoding='utf-8'))
        called = {node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, 'id', None)
                  for node in ast.walk(tree) if isinstance(node, ast.Call)}
        self.assertEqual(called & {'import_module', '__import__', 'exec', 'eval', 'reload'}, set())
        functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        for package in CORE_PACKAGES:
            self.assertIsNotNone(importlib.util.find_spec(package), 'a package of the core itself is always there')
        for name, load in H._LOADERS:
            with self.subTest(registrant=name):
                node = functions[load.__name__]
                specs = sorted((call for call in ast.walk(node) if isinstance(call, ast.Call)
                                and isinstance(call.func, ast.Attribute) and call.func.attr == 'find_spec'),
                               key=lambda call: (call.lineno, call.col_offset))
                self.assertTrue(all(len(call.args) == 1 and isinstance(call.args[0], ast.Constant) for call in specs),
                                'every name that is looked for is a literal')
                parts = name.split('.')
                above = ['.'.join(parts[:depth]) for depth in range(1, len(parts) + 1)]
                self.assertEqual([call.args[0].value for call in specs], [item for item in above if item not in CORE_PACKAGES],
                                 'each package above it, outermost first, then the module itself')
                imports = [alias.name for statement in ast.walk(node) if isinstance(statement, ast.Import)
                           for alias in statement.names]
                self.assertEqual(imports, [name], 'one literal import statement, of exactly this module')
                self.assertEqual([s for s in ast.walk(node) if isinstance(s, ast.ImportFrom)], [])

    def test_a_registrant_whose_parent_package_is_absent_is_skipped(self) -> None:
        self.assertIsNone(importlib.util.find_spec('recursion_core.keys'), 'no keys lane is installed in this tree')
        composed = H.compose()
        self.assertEqual(composed.methods.registered(), ())
        self.assertNotIn('recursion_core.keys', sys.modules)

    def test_a_package_with_no_hooks_module_is_skipped(self) -> None:
        package = self.fake.lane('keys')
        composed = H.compose()
        self.assertEqual(composed.methods.registered(), ())
        self.assertIn(package, sys.modules, 'the parent package is imported to look for its hooks module')
        self.assertNotIn(f'{package}.hooks', sys.modules)

    def test_a_hooks_module_with_a_missing_dependency_fails_composition(self) -> None:
        self.fake.lane('keys', hooks='import a_module_that_does_not_exist_anywhere\n\ndef register(hooks):\n    pass\n')
        with self.assertRaises(H.HookError) as caught:
            H.compose()
        self.assertIn('ModuleNotFoundError', str(caught.exception))
        self.assertIn('recursion_core.keys.hooks', str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, ModuleNotFoundError)

    def test_a_hooks_module_that_raises_on_import_fails_composition(self) -> None:
        self.fake.lane('keys', hooks='raise RuntimeError("at import")\n')
        with self.assertRaises(H.HookError):
            H.compose()

    def test_a_register_that_raises_fails_composition(self) -> None:
        self.fake.lane('keys', hooks='def register(hooks):\n    raise RuntimeError("in register")\n')
        with self.assertRaises(H.HookError):
            H.compose()

    def test_a_hooks_module_with_no_register_fails_composition(self) -> None:
        self.fake.lane('keys', hooks='VALUE = 1\n')
        with self.assertRaises(H.HookError):
            H.compose()

    def test_a_parent_package_that_raises_while_it_is_imported_fails_composition(self) -> None:
        self.fake.lane('keys', init='raise RuntimeError("the package itself")\n', hooks=REGISTERS_NOTHING)
        with self.assertRaises(H.HookError):
            H.compose()

    def test_register_runs_once_and_registers_everything_the_lane_adds(self) -> None:
        package = self.fake.lane('keys', hooks=(
            'CALLS = []\n'
            'def register(hooks):\n'
            '    CALLS.append(hooks)\n'
            '    hooks.set("read_result", lambda run_id: {"status": "ok", "code": None, "detail": run_id})\n'
            '    hooks.methods.register("session.list", lambda ctx, params: {"code": "ok", "sessions": []})\n'
            '    hooks.commands.register(("session",), lambda sub: sub.add_parser("session"), lambda args, client: 0)\n'
            '    hooks.engines.register("pi", lambda *a, **k: None)\n'))
        composed = H.compose()
        module = sys.modules[f'{package}.hooks']
        self.assertEqual(module.CALLS, [composed])
        self.assertEqual(composed.read_result('run-1'), {'status': 'ok', 'code': None, 'detail': 'run-1'})
        self.assertEqual(composed.methods.registered(), ('session.list',))
        self.assertEqual(composed.commands.names(), ('session',))
        self.assertEqual(composed.engines.names(), ('claude_code', 'codex', 'pi'))

    def test_the_registrants_register_in_the_fixed_order_and_one_deeper_in_the_core_is_found_too(self) -> None:
        engine = 'def register(hooks):\n    hooks.engines.register(%r, lambda *a, **k: None)\n'
        self.fake.lane('events', hooks=engine % 'hermes')            # written first, registered second
        self.fake.lane('keys', hooks=engine % 'pi')
        self.fake.lane('control.mcp', hooks=engine % 'cursor')       # a package below a package of the core
        self.fake.module('control.scopes', engine % 'grok')          # a module inside a package of the core
        self.fake.module('routes.driver_hooks', engine % 'opencode')
        composed = H.compose()
        self.assertEqual(composed.engines.names(), ('claude_code', 'codex', 'pi', 'hermes', 'cursor', 'grok', 'opencode'),
                         "in the order of the fixed list, whatever the order they were installed in")

    def test_two_registrants_for_one_hook_fail_composition(self) -> None:
        text = 'def register(hooks):\n    hooks.set("authorize", lambda principal, method, params: None)\n'
        self.fake.lane('keys', hooks=text)
        self.fake.lane('events', hooks=text)
        with self.assertRaises(H.HookError) as caught:
            H.compose()
        self.assertIn('authorize', str(caught.exception))

    def test_two_registrants_for_one_method_fail_composition(self) -> None:
        text = 'def register(hooks):\n    hooks.methods.register("session.list", lambda ctx, params: {"code": "ok"})\n'
        self.fake.lane('keys', hooks=text)
        self.fake.lane('events', hooks=text)
        with self.assertRaises(H.HookError):
            H.compose()

    def test_after_composition_nothing_can_be_registered(self) -> None:
        composed = H.bare()
        with self.assertRaises(H.HookError):
            composed.set('authorize', lambda principal, method, params: None)
        with self.assertRaises(H.HookError):
            composed.start_registrant('routing', (), (), lambda principal, request: {})
        with self.assertRaises(H.HookError):
            composed.service(Service('late'))
        with self.assertRaises(H.HookError):
            composed.enable_changes()
        with self.assertRaises(methods.MethodTableError):
            composed.methods.register('session.list', lambda ctx, params: {'code': 'ok'})
        with self.assertRaises(commands.CommandTableError):
            composed.commands.register(('x',), lambda sub: None, lambda args, client: 0)
        with self.assertRaises(engines.EngineTableError):
            composed.engines.register('pi', lambda *a, **k: None)

    def test_a_hook_nobody_defined_cannot_be_registered(self) -> None:
        with self.assertRaises(H.HookError):
            H.Hooks().set('approve_everything', lambda: True)

    def test_the_registrant_list_names_each_module_once(self) -> None:
        self.assertEqual(len(set(H.REGISTRANTS)), len(H.REGISTRANTS))
        self.assertTrue(all(name.startswith('recursion_core.') for name in H.REGISTRANTS))


class DefaultsTest(unittest.TestCase):
    """With nothing registered each hook does what the daemon does today."""

    def setUp(self) -> None:
        self.hooks = H.bare()

    def test_the_default_authorizer_refuses_only_a_peer_inside_a_run_unit(self) -> None:
        self.assertIsNone(self.hooks.authorize(H.OWNER, 'run.stop', {'run_id': 'run-1'}))
        self.assertIsNone(self.hooks.authorize(H.DEVICE, 'run.stop', {'run_id': 'run-1'}))
        self.assertIsNone(self.hooks.authorize(H.Principal('session', 'ses-1'), 'run.start', {}))
        self.assertEqual(self.hooks.authorize(H.Principal('owner', 'local', in_run_unit=True), 'run.list', {}),
                         {'code': 'scope_refused', 'detail': 'peer is inside a run unit'})

    def test_the_default_credential_source_has_none(self) -> None:
        with self.assertRaises(KeyError) as caught:
            self.hooks.credentials('api_key:work-key', 'ANTHROPIC_API_KEY')
        self.assertEqual(caught.exception.args, ('api_key:work-key',))

    def test_the_other_defaults_add_nothing(self) -> None:
        ctx = object()
        self.assertIsNone(self.hooks.working_directory(ctx))
        self.assertIsNone(self.hooks.launch_check(ctx, '/usr/bin/engine'))
        self.assertIsNone(self.hooks.read_result('run-1'))
        self.assertIsNone(self.hooks.drive_owner(ctx, {'request_id': 'run-1'}))
        self.assertEqual(self.hooks.page_methods(), frozenset())
        self.assertIsNone(self.hooks.mark_changed('run-1'))
        self.assertIsNone(self.hooks.open_store('/nowhere'))
        self.assertIsNone(self.hooks.methods.store)

    def test_a_principal_is_one_of_five_kinds_and_shows_only_its_kind_and_label(self) -> None:
        self.assertEqual(H.Principal('plugin', 'notes.search').public(), {'kind': 'plugin', 'id': 'notes.search'})
        self.assertEqual(H.Principal('owner', 'local', in_run_unit=True).public(), {'kind': 'owner', 'id': 'local'})
        with self.assertRaises(ValueError):
            H.Principal('root')


class HookCallTest(unittest.TestCase):
    """Each single-valued hook: the fake is called with the documented arguments, and a failure does not fail open."""

    def hooks_with(self, name, function):
        made = H.Hooks()
        made.set(name, function)
        made.freeze()
        return made

    def test_authorize_is_asked_with_the_principal_the_method_and_the_params(self) -> None:
        seen = []
        made = self.hooks_with('authorize', lambda principal, method, params: seen.append((principal, method, params)))
        peer = H.Principal('owner', 'local', in_run_unit=True)
        self.assertIsNone(made.authorize(peer, 'run.list', {'limit': 5}))
        self.assertEqual(seen, [(peer, 'run.list', {'limit': 5})])
        # The registered authorizer took over the whole decision, the run-unit one included.

    def test_an_authorizer_refuses_with_a_code_or_a_code_and_a_detail(self) -> None:
        self.assertEqual(self.hooks_with('authorize', lambda *a: 'scope_refused').authorize(H.OWNER, 'run.stop', {}),
                         {'code': 'scope_refused', 'detail': ''})
        self.assertEqual(self.hooks_with('authorize', lambda *a: ('scope_refused', 'run')).authorize(H.DEVICE, 'run.stop', {}),
                         {'code': 'scope_refused', 'detail': 'run'})

    @quiet
    def test_an_authorizer_that_raises_refuses_the_call(self) -> None:
        def broken(principal, method, params):
            raise RuntimeError('policy store unreadable')
        self.assertEqual(self.hooks_with('authorize', broken).authorize(H.OWNER, 'run.stop', {}),
                         {'code': 'scope_refused', 'detail': 'authorizer_failed'})

    @quiet
    def test_an_authorizer_that_answers_nonsense_refuses_the_call(self) -> None:
        for nonsense in (True, 0, '', {'code': 'ok'}, ('scope_refused',)):
            with self.subTest(answer=nonsense):
                self.assertEqual(self.hooks_with('authorize', lambda *a, n=nonsense: n).authorize(H.OWNER, 'run.stop', {}),
                                 {'code': 'scope_refused', 'detail': 'authorizer_failed'})

    def test_credentials_are_asked_by_connection_and_variable_name(self) -> None:
        seen = []

        def source(connection_id, env_name):
            seen.append((connection_id, env_name))
            return 'not-a-real-secret'
        self.assertEqual(self.hooks_with('credentials', source).credentials('api_key:work-key', 'ANTHROPIC_API_KEY'),
                         'not-a-real-secret')
        self.assertEqual(seen, [('api_key:work-key', 'ANTHROPIC_API_KEY')])

    @quiet
    def test_a_credential_source_never_gives_none_or_nothing(self) -> None:
        def raises_other(connection_id, env_name):
            raise OSError('keyring locked')

        def raises_key(connection_id, env_name):
            raise KeyError('something else')
        for source in (lambda c, e: None, lambda c, e: '', lambda c, e: b'bytes', lambda c, e: 7, raises_other, raises_key):
            with self.subTest(source=source):
                with self.assertRaises(KeyError) as caught:
                    self.hooks_with('credentials', source).credentials('api_key:work-key', 'ANTHROPIC_API_KEY')
                self.assertEqual(caught.exception.args, ('api_key:work-key',))

    def test_the_working_directory_provider_is_asked_with_the_run_context(self) -> None:
        seen = []
        ctx = object()

        def provider(given):
            seen.append(given)
            return '/home/dev/.local/state/recursion/worktrees/ses-1'
        made = self.hooks_with('working_directory', provider)
        self.assertEqual(made.working_directory(ctx), pathlib.Path('/home/dev/.local/state/recursion/worktrees/ses-1'))
        self.assertEqual(seen, [ctx])
        self.assertIsNone(self.hooks_with('working_directory', lambda given: None).working_directory(ctx))

    @quiet
    def test_a_working_directory_provider_that_fails_refuses_the_launch(self) -> None:
        def broken(ctx):
            raise OSError('disk full')
        for provider in (broken, lambda ctx: 7, lambda ctx: ''):
            with self.subTest(provider=provider):
                with self.assertRaises(H.HookRefused) as caught:
                    self.hooks_with('working_directory', provider).working_directory(object())
                self.assertEqual(caught.exception.code, 'working_directory_failed')

    def test_the_launch_check_is_asked_with_the_run_context_and_the_resolved_executable(self) -> None:
        seen = []
        ctx = object()
        made = self.hooks_with('launch_check', lambda given, executable: seen.append((given, executable)))
        self.assertIsNone(made.launch_check(ctx, '/usr/local/bin/claude'))
        self.assertEqual(seen, [(ctx, '/usr/local/bin/claude')])

    @quiet
    def test_a_launch_check_refuses_with_its_code_and_a_failing_one_refuses_too(self) -> None:
        with self.assertRaises(H.HookRefused) as caught:
            self.hooks_with('launch_check', lambda ctx, executable: 'binary_changed').launch_check(object(), '/bin/x')
        self.assertEqual((caught.exception.code, caught.exception.detail), ('launch_check', 'binary_changed'))

        def broken(ctx, executable):
            raise RuntimeError('cannot read the binary')
        for check in (broken, lambda ctx, executable: False, lambda ctx, executable: ''):
            with self.subTest(check=check):
                with self.assertRaises(H.HookRefused) as caught:
                    self.hooks_with('launch_check', check).launch_check(object(), '/bin/x')
                self.assertEqual(caught.exception.code, 'launch_check_failed')

    @quiet
    def test_the_result_reader_is_asked_by_run_id_and_a_failing_one_is_no_result(self) -> None:
        seen = []

        def reader(run_id):
            seen.append(run_id)
            return {'status': 'ok', 'code': None, 'detail': ''}
        self.assertEqual(self.hooks_with('read_result', reader).read_result('run-7'), {'status': 'ok', 'code': None, 'detail': ''})
        self.assertEqual(seen, ['run-7'])

        def broken(run_id):
            raise OSError('event store closed')
        self.assertIsNone(self.hooks_with('read_result', broken).read_result('run-7'))
        self.assertIsNone(self.hooks_with('read_result', lambda run_id: 'ok').read_result('run-7'))

    @quiet
    def test_the_drive_owner_is_asked_with_the_context_and_the_request(self) -> None:
        seen = []
        ctx, request = object(), {'request_id': 'run-7'}

        class Owner:
            def run(self, *args):
                pass
        owner = Owner()

        def chooser(given, asked):
            seen.append((given, asked))
            return owner
        self.assertIs(self.hooks_with('drive_owner', chooser).drive_owner(ctx, request), owner)
        self.assertEqual(seen, [(ctx, request)])
        self.assertIsNone(self.hooks_with('drive_owner', lambda given, asked: None).drive_owner(ctx, request))

        def broken(given, asked):
            raise RuntimeError('before accepting')
        self.assertIsNone(self.hooks_with('drive_owner', broken).drive_owner(ctx, request), 'today\'s path drives the run')
        self.assertIsNone(self.hooks_with('drive_owner', lambda given, asked: object()).drive_owner(ctx, request))

    @quiet
    def test_page_methods_is_a_frozen_set_and_a_failing_one_is_empty(self) -> None:
        made = self.hooks_with('page_methods', lambda: {'session.turn', 'session.status'})
        self.assertEqual(made.page_methods(), frozenset({'session.turn', 'session.status'}))
        self.assertIsInstance(made.page_methods(), frozenset)

        def broken():
            raise RuntimeError('no')
        for names in (broken, lambda: ['session.turn'], lambda: {1, 2}):
            with self.subTest(names=names):
                self.assertEqual(self.hooks_with('page_methods', names).page_methods(), frozenset())

    def test_the_event_store_factory_is_called_once_with_the_state_directory(self) -> None:
        seen, store = [], FakeStore()

        def factory(state_dir):
            seen.append(state_dir)
            return store
        made = self.hooks_with('event_store', factory)
        self.assertIs(made.open_store('/state'), store)
        self.assertEqual(seen, ['/state'])
        self.assertIs(made.methods.store, store)
        self.assertTrue(made.gate.enabled)


class Handlers:
    """Handlers for the dispatcher's tests: they count their calls and can be made to wait."""

    def __init__(self) -> None:
        self.calls: list = []
        self.callbacks: list = []
        self.entered, self.release = threading.Event(), threading.Event()
        self.wait = False

    def turn(self, ctx, params):
        self.calls.append((ctx.principal, ctx.tx is not None, dict(params)))
        if self.wait:
            self.entered.set()
            self.release.wait(BOUND_S)
        ctx.tx.execute('INSERT INTO work VALUES (?, ?, ?)', (params['command_id'], 'op-' + params['command_id'], 'pending'))
        ctx.after_commit(lambda: self.callbacks.append(('wake', params['command_id'])))
        return {'code': 'ok', 'turn_id': 'turn-' + params['command_id']}


def row_names_service(case, method, service) -> None:
    """For one test, the published row of ``method`` names ``service``. Only the two reads of a log name one in
    the published table, and a registration cannot name what its row does not, so a test of readiness on
    another method says what its row would say."""
    patched = mock.patch.dict(methods.METHODS, {method: methods.METHODS[method]._replace(service=service)})
    patched.start()
    case.addCleanup(patched.stop)


def table_with(store=None, tenant=methods.INSTALL_TENANT, **handlers):
    table = methods.MethodTable()
    for name, handler in handlers.items():
        keyed = methods.METHODS[name].idempotency == 'command_id'
        table.register(name, handler, service=methods.METHODS[name].service, **({'tenant': tenant} if keyed else {}))
    table.store = store
    return table


TURN = {'command_id': 'cmd-1', 'session_id': 'ses-1', 'expected_revision': 3, 'text': 'go on'}


class MethodTableTest(unittest.TestCase):
    def test_the_table_lists_the_six_built_in_methods_and_every_other_one_once(self) -> None:
        self.assertEqual(tuple(methods.METHODS)[:6], methods.BUILT_IN)
        self.assertEqual(methods.MANDATORY, (*methods.BUILT_IN, 'methods.list'))
        self.assertEqual(len(methods.METHODS), 38)
        self.assertEqual(methods.METHODS['run.start'].idempotency, 'client_key')
        self.assertEqual(methods.METHODS['run.stop'], methods.Row('run', True, 'none', False))
        self.assertEqual([name for name, row in methods.METHODS.items() if not row.beta], list(methods.BUILT_IN),
                         'only the six methods the daemon answers today are not beta')
        keyed = [name for name, row in methods.METHODS.items() if row.idempotency == 'command_id']
        self.assertTrue(all(methods.METHODS[name].mutating for name in keyed))
        self.assertEqual(sorted(name for name, row in methods.METHODS.items() if row.mutating and name not in keyed),
                         ['run.start', 'run.stop'], 'every mutating method but the two run ones takes a command id')
        scopes = {name: row.scope for name, row in methods.METHODS.items()}
        self.assertEqual({name for name, scope in scopes.items() if scope == 'approve'}, {'approval.open', 'approval.answer'})
        self.assertEqual({name for name, scope in scopes.items() if scope == 'manage'}, {'workspace.publish'})
        self.assertEqual(set(scopes.values()), {'read', 'run', 'approve', 'manage'})
        self.assertTrue(all((row.scope == 'read') == (not row.mutating) for row in methods.METHODS.values()),
                        'a method that changes nothing needs read, and only such a method does')

    def test_the_table_is_the_published_method_table(self) -> None:
        published = REPO / 'schemas' / 'control' / 'v1' / 'methods.table.v1.json'
        if not published.is_file():
            self.skipTest('schemas/control/v1/methods.table.v1.json is not in this checkout yet')
        table = json.loads(published.read_text(encoding='utf-8'))
        rows = table['methods']
        self.assertEqual({row['name']: methods.Row(row['scope'], row['mutating'], row['idempotency'], row['beta'], row['service'])
                          for row in rows}, methods.METHODS, 'scope, mutation, keying, beta and the service of every row')
        # What a handler must have, and what it falls back to, are the handlers' to honour: pinned here so that a
        # change of the published rows is seen where the handlers are registered.
        self.assertEqual({row['name']: row['requires'] for row in rows if row['requires']},
                         {'session.resume': ['resume'], 'session.fork': ['fork']})
        self.assertEqual({row['name']: row['falls_back'] for row in rows if row['falls_back']},
                         {'session.steer': [{'flag': 'mid_turn_input', 'then': 'next_turn'}],
                          'session.interrupt': [{'flag': 'interrupt', 'then': 'stop_then_resume'}]})
        self.assertEqual([row['name'] for row in rows], list(methods.METHODS))
        self.assertEqual(table['table_version'], methods.TABLE_VERSION)
        self.assertEqual([row['name'] for row in rows if 'unsupported' not in row['codes']], list(methods.MANDATORY),
                         'the mandatory built-ins are exactly the rows that cannot answer unsupported')

    def test_a_name_that_is_not_in_the_table_is_method_unknown_and_an_unbuilt_one_is_unsupported(self) -> None:
        table = methods.MethodTable()
        self.assertEqual(table.dispatch('session.kill', {}, H.OWNER, None), {'code': 'method_unknown', 'detail': 'session.kill'})
        self.assertEqual(table.dispatch('device.pair_begin', {}, H.OWNER, None),
                         {'code': 'method_unknown', 'detail': 'device.pair_begin'}, 'a reserved name has no row')
        self.assertEqual(table.dispatch(None, {}, H.OWNER, None), {'code': 'method_unknown', 'detail': 'None'})
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, None),
                         {'code': 'unsupported', 'detail': 'not_implemented'})
        self.assertEqual(table.implemented(), methods.MANDATORY)

    def test_methods_list_is_answered_by_the_table_itself_and_says_what_is_implemented_now(self) -> None:
        table = methods.MethodTable()
        table.register('session.list', lambda ctx, params: {'code': 'ok', 'sessions': []})
        answer = table.dispatch('methods.list', {}, H.DEVICE, None)
        self.assertEqual((answer['code'], answer['table_version']), ('ok', '1.0-draft.9'))
        self.assertEqual([row['name'] for row in answer['methods']], list(methods.METHODS), 'every row, in the table\'s order')
        self.assertEqual([row['name'] for row in answer['methods'] if row['implemented']],
                         [*methods.BUILT_IN, 'session.list', 'methods.list'])
        self.assertEqual({row['name'] for row in answer['methods'] if not row['beta']}, set(methods.BUILT_IN))
        self.assertTrue(all(set(row) == {'name', 'implemented', 'beta'} for row in answer['methods']))
        self.assertNotIn('device.pair_begin', [row['name'] for row in answer['methods']], 'a reserved name is never listed')

    def test_a_handler_cannot_replace_a_built_in_method_or_take_a_name_outside_the_table(self) -> None:
        table = methods.MethodTable()
        for name in (*methods.MANDATORY, 'run.resume', 'session.kill', 'device.list'):
            with self.subTest(name=name):
                with self.assertRaises(methods.MethodTableError):
                    table.register(name, lambda ctx, params: {'code': 'ok'})
        table.register('session.list', lambda ctx, params: {'code': 'ok', 'sessions': []})
        with self.assertRaises(methods.MethodTableError):
            table.register('session.list', lambda ctx, params: {'code': 'ok', 'sessions': []})
        with self.assertRaises(methods.MethodTableError):
            table.register('session.status', 'not a function')
        self.assertEqual(table.implemented(), (*methods.BUILT_IN, 'session.list', 'methods.list'))

    def test_a_read_only_handler_gets_the_principal_and_no_transaction(self) -> None:
        seen = []

        def listing(ctx, params):
            seen.append((ctx.principal, ctx.tx, params))
            return {'code': 'ok', 'sessions': []}
        table = table_with(FakeStore(), **{'session.list': listing})
        self.assertEqual(table.dispatch('session.list', {'limit': 5}, H.DEVICE, None), {'code': 'ok', 'sessions': []})
        self.assertEqual(seen, [(H.DEVICE, None, {'limit': 5})])
        self.assertEqual(table.store.commits, 0, 'the dispatcher opens no transaction for a method with no command id')

    def test_with_no_event_store_a_command_is_unsupported_and_the_handler_is_not_called(self) -> None:
        handlers = Handlers()
        table = table_with(None, **{'session.turn': handlers.turn})
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, None), {'code': 'unsupported', 'detail': 'store_absent'})
        self.assertEqual(handlers.calls, [])

    def test_a_command_runs_in_the_dispatchers_transaction_and_leaves_a_receipt(self) -> None:
        handlers, store = Handlers(), work_store()
        table = table_with(store, **{'session.turn': handlers.turn})
        answer = table.dispatch('session.turn', dict(TURN), H.OWNER, None)
        self.assertEqual(answer, {'code': 'ok', 'turn_id': 'turn-cmd-1'})
        self.assertEqual(handlers.calls, [(H.OWNER, True, TURN)])
        self.assertEqual((store.commits, store.rollbacks), (1, 0))
        digest = hashlib.sha256(b'{"expected_revision":3,"session_id":"ses-1","text":"go on"}').hexdigest()
        self.assertEqual(store.receipts, {('install', 'session.turn', 'cmd-1'): (digest, answer)})
        self.assertEqual(store.rows, {('work', 'cmd-1'): {'operation_id': 'op-cmd-1', 'state': 'pending'}},
                         'the work intent is in the same commit as the receipt')
        self.assertEqual(handlers.callbacks, [('wake', 'cmd-1')])

    def test_the_same_command_again_returns_the_first_answer_and_runs_nothing(self) -> None:
        handlers, store = Handlers(), work_store()
        table = table_with(store, **{'session.turn': handlers.turn})
        first = table.dispatch('session.turn', dict(TURN), H.OWNER, None)
        again = table.dispatch('session.turn', dict(TURN), H.OWNER, None)
        self.assertEqual(again, first)
        self.assertEqual(len(handlers.calls), 1)
        self.assertEqual(handlers.callbacks, [('wake', 'cmd-1')], 'a replay stages no callback')
        self.assertEqual(len(store.rows), 1, 'and no second work intent')

    def test_the_same_id_with_another_payload_is_a_conflict(self) -> None:
        handlers, store = Handlers(), work_store()
        table = table_with(store, **{'session.turn': handlers.turn})
        table.dispatch('session.turn', dict(TURN), H.OWNER, None)
        self.assertEqual(table.dispatch('session.turn', dict(TURN, text='something else'), H.OWNER, None),
                         {'code': 'conflict', 'detail': 'command_id_reused'})
        self.assertEqual(len(handlers.calls), 1)

    def test_the_identity_is_the_tenant_of_the_target_the_method_and_the_id(self) -> None:
        handlers, store, asked = Handlers(), work_store(), []
        owners = {'ses-1': 't1', 'ses-2': 't2'}

        def tenant_of(principal, params):
            asked.append((principal, params['session_id']))
            return owners[params['session_id']]
        table = table_with(store, tenant_of, **{'session.turn': handlers.turn, 'session.queue': handlers.turn})
        table.dispatch('session.turn', dict(TURN), H.OWNER, None)
        table.dispatch('session.queue', dict(TURN), H.OWNER, None)
        table.dispatch('session.turn', dict(TURN, session_id='ses-2'), H.DEVICE, None)
        self.assertEqual(sorted(store.receipts), [('t1', 'session.queue', 'cmd-1'), ('t1', 'session.turn', 'cmd-1'),
                                                  ('t2', 'session.turn', 'cmd-1')])
        self.assertEqual(len(handlers.calls), 3)
        self.assertEqual(asked[-1], (H.DEVICE, 'ses-2'))
        # A tenant the params name is not the target's tenant: it changes the payload, never the namespace.
        self.assertEqual(table.dispatch('session.turn', dict(TURN, tenant='t9'), H.OWNER, None),
                         {'code': 'conflict', 'detail': 'command_id_reused'})
        self.assertNotIn('t9', [tenant for tenant, _, _ in store.receipts])
        # The principal is not part of the key: the same command from another caller is the same command.
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.DEVICE, None), {'code': 'ok', 'turn_id': 'turn-cmd-1'})
        self.assertEqual(len(handlers.calls), 3)

    @quiet
    def test_a_command_whose_target_has_no_tenant_runs_nothing(self) -> None:
        handlers, store = Handlers(), work_store()

        def unknown(principal, params):
            raise methods.NotAccepted({'code': 'session_unknown', 'session_id': params['session_id']})

        def broken(principal, params):
            raise OSError('session store unreadable')
        for tenant_of, expected in ((unknown, {'code': 'session_unknown', 'session_id': 'ses-1'}),
                                    (broken, {'code': 'unavailable', 'detail': 'tenant:OSError'}),
                                    (lambda principal, params: None, {'code': 'unavailable', 'detail': 'tenant:TypeError'}),
                                    (lambda principal, params: '', {'code': 'unavailable', 'detail': 'tenant:TypeError'})):
            table = table_with(store, tenant_of, **{'session.turn': handlers.turn})
            self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, None), expected)
        self.assertEqual((handlers.calls, store.commits, store.rollbacks), ([], 0, 0))

    def test_a_method_that_keeps_a_receipt_names_its_tenant_and_no_other_method_does(self) -> None:
        table = methods.MethodTable()
        for tenant in (None, 'install-wide', '', 7):
            with self.subTest(tenant=tenant):
                with self.assertRaises(methods.MethodTableError):
                    table.register('session.turn', lambda ctx, params: {'code': 'ok'}, tenant=tenant)
        with self.assertRaises(methods.MethodTableError):
            table.register('session.list', lambda ctx, params: {'code': 'ok'}, tenant=methods.INSTALL_TENANT)
        table.register('surface.prefs.set', lambda ctx, params: {'code': 'ok'}, tenant=methods.INSTALL_TENANT)
        table.register('session.turn', lambda ctx, params: {'code': 'ok'}, tenant=lambda principal, params: 't1')
        self.assertEqual(table.registered(), ('session.turn', 'surface.prefs.set'))

    def test_a_command_with_no_id_or_with_params_that_have_no_text_is_refused_before_the_handler(self) -> None:
        handlers = Handlers()
        table = table_with(work_store(), **{'session.turn': handlers.turn})
        for params, detail in (({'session_id': 'ses-1'}, 'command_id'), (dict(TURN, command_id=''), 'command_id'),
                               (dict(TURN, command_id=7), 'command_id'), (dict(TURN, budget=2**60), 'params'),
                               (dict(TURN, weight=float('nan')), 'params'), (dict(TURN, text='\ud800'), 'params')):
            with self.subTest(params=params):
                self.assertEqual(table.dispatch('session.turn', params, H.OWNER, None), {'code': 'request_invalid', 'detail': detail})
        self.assertEqual(handlers.calls, [])
        self.assertEqual(table.store.commits, 0)

    def test_two_identical_commands_at_once_run_the_handler_once(self) -> None:
        # The store here lets two transactions be open at once, so nothing but the dispatcher's own slot
        # keeps the second command out of the handler while the first is in it.
        handlers, store = Handlers(), work_store(serial=False)
        handlers.wait = True
        self.addCleanup(handlers.release.set)
        table = table_with(store, **{'session.turn': handlers.turn})
        answers = []
        threads = [threading.Thread(target=lambda: answers.append(table.dispatch('session.turn', dict(TURN), H.OWNER, None)),
                                    daemon=True) for _ in range(2)]
        threads[0].start()
        self.assertTrue(handlers.entered.wait(BOUND_S), 'the first command reached its handler')
        threads[1].start()
        key = ('install', 'session.turn', 'cmd-1')
        waiting = lambda: table._slots.get(key, (None, 0))[1]   # noqa: E731
        deadline = time.monotonic() + BOUND_S
        while waiting() < 2 and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(waiting(), 2, 'the second command has arrived and waits for the first')
        self.assertEqual(len(handlers.calls), 1, 'the second command did not enter the handler beside the first')
        handlers.release.set()
        for thread in threads:
            thread.join(BOUND_S)
        self.assertEqual(len(handlers.calls), 1)
        self.assertEqual(answers, [{'code': 'ok', 'turn_id': 'turn-cmd-1'}] * 2)
        self.assertEqual(handlers.callbacks, [('wake', 'cmd-1')])
        self.assertEqual(store.commits, 2, 'the second commit is the replay\'s own, with nothing in it')
        self.assertEqual(table._slots, {}, 'the slot goes when nobody waits on it')

    @quiet
    def test_a_handler_that_raises_is_rolled_back_and_no_callback_runs(self) -> None:
        ran, store = [], work_store()

        def broken(ctx, params):
            ctx.tx.execute('INSERT INTO work VALUES (?, ?, ?)', ('x', 'op-x', 'half done'))
            ctx.after_commit(lambda: ran.append('callback'))
            raise RuntimeError('half way')
        table = table_with(store, **{'session.turn': broken})
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, None), {'code': 'unavailable', 'detail': 'handler:RuntimeError'})
        self.assertEqual((store.commits, store.rollbacks, store.receipts, store.rows), (0, 1, {}, {}))
        self.assertEqual(ran, [])
        self.assertEqual(table._slots, {})

    def test_a_handler_that_does_not_accept_the_command_leaves_no_receipt(self) -> None:
        ran, store, busy = [], work_store(), [True]

        def turn(ctx, params):
            ctx.after_commit(lambda: ran.append('callback'))
            if busy[0]:
                raise methods.NotAccepted({'code': 'busy', 'session_id': params['session_id']})
            return {'code': 'ok'}
        table = table_with(store, **{'session.turn': turn})
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, None), {'code': 'busy', 'session_id': 'ses-1'})
        self.assertEqual((store.receipts, ran), ({}, []))
        busy[0] = False
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, None), {'code': 'ok'}, 'the same id may be sent again')
        self.assertEqual(ran, ['callback'])

    @quiet
    def test_a_commit_that_fails_answers_unavailable_and_runs_no_callback(self) -> None:
        handlers, store = Handlers(), work_store()
        store.fail_commit = True
        table = table_with(store, **{'session.turn': handlers.turn})
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, None), {'code': 'unavailable', 'detail': 'store:OSError'})
        self.assertEqual((store.receipts, store.rows, handlers.callbacks), ({}, {}, []))

    @quiet
    def test_a_handler_that_returns_no_answer_is_rolled_back(self) -> None:
        store = work_store()
        table = table_with(store, **{'session.turn': lambda ctx, params: None, 'session.list': lambda ctx, params: 'ok'})
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, None), {'code': 'unavailable', 'detail': 'handler:TypeError'})
        self.assertEqual(table.dispatch('session.list', {}, H.OWNER, None), {'code': 'unavailable', 'detail': 'handler:TypeError'})
        self.assertEqual(store.receipts, {})

    @quiet
    def test_a_callback_that_fails_leaves_the_receipt_and_the_later_callbacks_run(self) -> None:
        ran, store = [], work_store()

        def turn(ctx, params):
            ctx.after_commit(lambda: ran.append('first'))
            ctx.after_commit(lambda: 1 / 0)
            ctx.after_commit(lambda: ran.append('third'))
            return {'code': 'ok'}
        table = table_with(store, **{'session.turn': turn})
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, None), {'code': 'ok'})
        self.assertEqual(ran, ['first', 'third'])
        self.assertEqual(list(store.receipts), [('install', 'session.turn', 'cmd-1')])

    def test_callbacks_run_after_the_commit_and_outside_the_commands_slot(self) -> None:
        seen, store = [], work_store()

        def turn(ctx, params):
            seen.append(('handler', store.commits, len(table._slots), store.open_transactions))
            ctx.after_commit(lambda: seen.append(('callback', store.commits, len(table._slots), store.open_transactions)))
            return {'code': 'ok'}
        table = table_with(store, **{'session.turn': turn})
        table.dispatch('session.turn', dict(TURN), H.OWNER, None)
        self.assertEqual(seen, [('handler', 0, 1, 1), ('callback', 1, 0, 0)],
                         'the handler runs inside the slot and the transaction; the callback after both')

    def test_a_committed_intent_is_still_there_when_the_process_is_cut_before_the_callback(self) -> None:
        handlers, store = Handlers(), work_store()
        woken = []

        def turn(ctx, params):
            ctx.tx.execute('INSERT INTO work VALUES (?, ?, ?)', (params['command_id'], 'op-1', 'pending'))
            ctx.after_commit(lambda: (_ for _ in ()).throw(Cut()))
            ctx.after_commit(lambda: woken.append('worker'))
            handlers.calls.append(params['command_id'])
            return {'code': 'ok', 'accepted': True}
        table = table_with(store, **{'session.turn': turn})
        with self.assertRaises(Cut):
            table.dispatch('session.turn', dict(TURN), H.OWNER, None)
        self.assertEqual(woken, [], 'the callback that would have woken the worker never ran')
        self.assertEqual(store.rows, {('work', 'cmd-1'): {'operation_id': 'op-1', 'state': 'pending'}},
                         'the intent is committed: a service that scans for pending work finds it')
        self.assertIn(('install', 'session.turn', 'cmd-1'), store.receipts)
        # The client retries after the restart: the receipt answers, and the work is not staged again.
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, None), {'code': 'ok', 'accepted': True})
        self.assertEqual(handlers.calls, ['cmd-1'])
        self.assertEqual(woken, [])
        self.assertEqual(len(store.rows), 1)

    def test_a_nested_call_from_a_read_reaches_the_other_methods_and_never_a_command(self) -> None:
        asked = []

        def call(method, params):
            asked.append((method, params))
            if method != 'run.stop':
                return {'code': 'method_unknown', 'detail': method}
            return {'code': 'stopped', 'run_id': params['run_id']}

        def status(ctx, params):
            return {'code': 'ok', 'run': ctx.call('run.stop', {'run_id': 'run-1'}),
                    'nested': ctx.call('session.turn', dict(TURN)), 'unknown': ctx.call('session.kill')}
        table = table_with(work_store(), **{'session.status': status})
        answer = table.dispatch('session.status', {'session_id': 'ses-1'}, H.OWNER, call)
        self.assertEqual(answer['run'], {'code': 'stopped', 'run_id': 'run-1'},
                         'outside a command a read may call a mutating built-in: there is no transaction to escape')
        self.assertEqual(answer['nested'], {'code': 'request_invalid', 'detail': 'nested_command'})
        self.assertEqual(asked[0], ('run.stop', {'run_id': 'run-1'}))
        self.assertEqual(asked[1], ('session.kill', None), 'a name with no row goes to the caller, which answers as today')
        self.assertEqual(answer['unknown'], {'code': 'method_unknown', 'detail': 'session.kill'})
        self.assertEqual(len(asked), 2, 'the nested command never reached the caller')

    def test_a_method_waits_for_its_service_before_any_transaction_is_opened(self) -> None:
        handlers, store, ready = Handlers(), work_store(), [False]
        row_names_service(self, 'session.turn', 'sessions')
        table = methods.MethodTable()
        table.register('session.turn', handlers.turn, service='sessions', tenant=methods.INSTALL_TENANT)
        table.register('session.list', lambda ctx, params: {'code': 'ok', 'sessions': []})
        table.store, table.service_ready = store, lambda name: name == 'sessions' and ready[0]
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, None), {'code': 'unavailable', 'detail': 'service_starting'})
        self.assertEqual((handlers.calls, store.commits, store.rollbacks), ([], 0, 0))
        self.assertEqual(table.dispatch('session.list', {}, H.OWNER, None)['code'], 'ok', 'a method that needs no service does not wait')
        ready[0] = True
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, None)['code'], 'ok')
        self.assertEqual(table.services_needed(), {'session.turn': 'sessions'})

    def test_a_handler_is_given_the_closing_signal(self) -> None:
        seen = []
        table = table_with(None, **{'events.follow': lambda ctx, params: seen.append(ctx.closing) or {'code': 'ok'}})
        table.dispatch('events.follow', {}, H.OWNER, None)
        self.assertIs(seen[0], table.closing)
        self.assertFalse(seen[0].is_set())

    def test_the_payload_digest_is_over_one_text_whatever_the_spelling(self) -> None:
        text = methods.canonical_text
        self.assertEqual(text({'b': 1.0, 'a': [True, None, 'x']}), '{"a":[true,null,"x"],"b":1}')
        self.assertEqual(text([1e-7, 1E3, -0.0, 0.1, 1.5e-300, 100.50, 2**53 - 1, 9007199254740991.0, -9007199254740991.0]),
                         '[1e-7,1000,0,0.1,1.5e-300,100.5,9007199254740991,9007199254740991,-9007199254740991]')
        self.assertEqual(text({'': 1, '\U00010000': 2}), '{"\U00010000":2,"":1}', 'keys in UTF-16 order')
        self.assertEqual(text('café — "q"\n\x01'), '"café — \\"q\\"\\n\\u0001"')
        for value in (2**53, -2**53, 2.0**53, -2.0**53, 1e21, 1.5e300, float('inf'), '\udc00', {1: 'x'}, object()):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    text(value)
        one = methods.payload_digest({'command_id': 'a', 'n': 1.0, 'text': 'x'})
        self.assertEqual(one, methods.payload_digest({'text': 'x', 'n': 1, 'command_id': 'another id'}))
        self.assertNotEqual(one, methods.payload_digest({'text': 'x', 'n': 2}))


class CommandTableTest(unittest.TestCase):
    def parser(self):
        parser = argparse.ArgumentParser(prog='recursion-control')
        sub = parser.add_subparsers(dest='command', required=True)
        sub.add_parser('list')
        start = sub.add_parser('start')
        start.add_argument('--tenant', required=True)
        return parser, sub

    def test_a_family_adds_its_commands_and_is_dispatched_with_the_parsed_arguments_and_the_client(self) -> None:
        seen = []

        def add(sub):
            sub.add_parser('session').add_argument('session_id')
            sub.add_parser('sessions')

        def dispatch(args, client):
            seen.append((args.command, getattr(args, 'session_id', None), client))
            return 3
        table = commands.CommandTable()
        table.register(('session', 'sessions'), add, dispatch)
        table.freeze()
        parser, sub = self.parser()
        table.add_parsers(sub)
        client = object()
        self.assertEqual(table.dispatch(parser.parse_args(['session', 'ses-1']), client), 3)
        self.assertEqual(seen, [('session', 'ses-1', client)])
        self.assertIn('sessions', table)
        self.assertNotIn('start', table, 'the CLI\'s own commands are not in the table')
        self.assertEqual(table.names(), ('session', 'sessions'))

    def test_the_clis_own_names_and_a_name_already_taken_cannot_be_registered(self) -> None:
        table = commands.CommandTable()
        for name in commands.BUILT_IN:
            with self.subTest(name=name):
                with self.assertRaises(commands.CommandTableError):
                    table.register((name,), lambda sub: None, lambda args, client: 0)
        table.register(('session',), lambda sub: sub.add_parser('session'), lambda args, client: 0)
        for names in (('session',), ('queue', 'session'), ('a', 'a'), (), 'session', ('',)):
            with self.subTest(names=names):
                with self.assertRaises(commands.CommandTableError):
                    table.register(names, lambda sub: None, lambda args, client: 0)
        with self.assertRaises(commands.CommandTableError):
            table.register(('queue',), 'not a function', lambda args, client: 0)

    def test_a_parser_that_adds_another_name_or_leaves_one_out_is_refused(self) -> None:
        for names, add in ((('session',), lambda sub: (sub.add_parser('session'), sub.add_parser('extra'))),
                           (('session', 'queue'), lambda sub: sub.add_parser('session')),
                           (('session',), lambda sub: None)):
            with self.subTest(names=names):
                table = commands.CommandTable()
                table.register(names, add, lambda args, client: 0)
                with self.assertRaises(commands.CommandTableError):
                    table.add_parsers(self.parser()[1])

    def test_a_parser_cannot_add_an_option_to_a_command_another_keeps(self) -> None:
        def add(sub):
            sub.add_parser('workspace')
            sub.choices['start'].add_argument('--workspace')
        table = commands.CommandTable()
        table.register(('workspace',), add, lambda args, client: 0)
        parser, sub = self.parser()
        with self.assertRaises(commands.CommandTableError) as caught:
            table.add_parsers(sub)
        self.assertIn('AttributeError', str(caught.exception), 'what a family is handed has no way to another parser')
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(['start', '--tenant', 't', '--workspace', '/a'])

    def test_a_dispatch_returns_an_exit_status(self) -> None:
        table = commands.CommandTable()
        table.register(('session',), lambda sub: sub.add_parser('session'), lambda args, client: 'ok')
        with self.assertRaises(commands.CommandTableError):
            table.dispatch(argparse.Namespace(command='session'), None)

    def test_an_unregistered_name_is_still_a_usage_error_of_the_parser(self) -> None:
        table = commands.CommandTable()
        table.register(('session',), lambda sub: sub.add_parser('session'), lambda args, client: 0)
        parser, sub = self.parser()
        table.add_parsers(sub)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            parser.parse_args(['sesion'])
        self.assertEqual(caught.exception.code, 2)


class EngineTableTest(unittest.TestCase):
    def test_a_new_table_holds_todays_two_adapters_in_todays_order(self) -> None:
        table = engines.EngineTable()
        self.assertEqual(list(table.items()), [('claude_code', ClaudeCodeAdapter), ('codex', CodexAdapter)])
        self.assertEqual(table.names(), engines.SEEDED)
        self.assertIn('codex', table)
        self.assertNotIn('pi', table)

    def test_the_catalogue_has_ten_names_and_a_name_alone_adds_nothing(self) -> None:
        self.assertEqual(len(engines.CATALOGUE), 10)
        self.assertEqual(len(set(engines.CATALOGUE)), 10)
        self.assertEqual(engines.CATALOGUE[:2], engines.SEEDED)
        self.assertEqual(engines.EngineTable().names(), ('claude_code', 'codex'))

    def test_an_engine_lane_registers_its_factory_after_todays_two(self) -> None:
        table, factory = engines.EngineTable(), lambda launcher, clock, credentials, environ, **options: None
        table.register('pi', factory)
        self.assertEqual(table.names(), ('claude_code', 'codex', 'pi'))
        self.assertEqual(list(table.items())[2], ('pi', factory))

    def test_a_second_factory_for_a_name_is_refused_unless_it_replaces_a_seeded_one_on_purpose(self) -> None:
        table, factory = engines.EngineTable(), lambda *a, **k: None
        table.register('pi', factory)
        for name, replace in (('pi', False), ('pi', True), ('codex', False), ('not_an_engine', False), ('hermes', True)):
            with self.subTest(name=name, replace=replace):
                with self.assertRaises(engines.EngineTableError):
                    table.register(name, factory, replace=replace)
        table.register('claude_code', factory, replace=True)
        self.assertEqual(list(table.items())[0], ('claude_code', factory), 'a replaced engine keeps its place')
        self.assertEqual(table.names(), ('claude_code', 'codex', 'pi'))
        with self.assertRaises(engines.EngineTableError):
            table.register('claude_code', factory, replace=True)
        with self.assertRaises(engines.EngineTableError):
            table.register('hermes', 'not a function')

    def test_two_compositions_do_not_share_a_table(self) -> None:
        one, two = H.bare(), H.bare()
        self.assertIsNot(one.engines, two.engines)
        self.assertEqual(two.engines.names(), ('claude_code', 'codex'))


class ChangeMarkerTest(unittest.TestCase):
    def test_marks_are_taken_once_as_a_set(self) -> None:
        marker = H.ChangeMarker()
        for run_id in ('run-a', 'run-b', 'run-a'):
            marker.mark(run_id)
        self.assertEqual(marker.take(), ({'run-a', 'run-b'}, False))
        self.assertEqual(marker.take(), (set(), False))
        self.assertEqual(marker.poll_interval_s, 0.1)

    def test_past_the_cap_a_mark_is_dropped_and_said_so(self) -> None:
        marker = H.ChangeMarker()
        for number in range(H.ChangeMarker.CAP):
            marker.mark(f'run-{number}')
        marker.mark('run-0')
        self.assertFalse(marker._dropped, 'an id that is already there is not a drop')
        marker.mark('run-one-more')
        ids, dropped = marker.take()
        self.assertEqual((len(ids), dropped, 'run-one-more' in ids), (4096, True, False))
        self.assertEqual(marker.take(), (set(), False), 'the flag is cleared with the set')

    def test_a_mark_never_waits_for_a_consumer_that_holds_the_markers_lock(self) -> None:
        marker = H.ChangeMarker()
        marker.mark('run-before')
        marker._lock.acquire()          # the consumer is paused inside take()
        try:
            marker.mark('run-during')   # returns at once: this line would never return if it waited
        finally:
            marker._lock.release()
        self.assertEqual(marker.take(), ({'run-before'}, True), 'the mark is dropped, and the flag asks for a scan')

    def test_the_hook_marks_nothing_until_a_lane_turns_the_marker_on_and_never_raises(self) -> None:
        made = H.Hooks()
        self.assertIsNone(made.mark_changed('run-1'))
        marker = made.enable_changes()
        with self.assertRaises(H.HookError):
            made.enable_changes()
        made.freeze()
        made.mark_changed('run-1')
        self.assertIsNone(made.mark_changed(['not', 'hashable']), 'a mark that cannot be kept is swallowed')
        self.assertEqual(marker.take(), ({'run-1'}, False))
        self.assertIs(made.changes, marker)


class Service:
    """A service with no worker. ``fail_stop`` is how many of its first stops raise; a stop that returns is the
    one that says it has stopped."""

    def __init__(self, name, log=None, fail_start=False, fail_stop=0) -> None:
        self.name, self.ready = name, False
        self._log = log if log is not None else []
        self._fail_start, self._fail_stop = fail_start, int(fail_stop)

    def start(self, context) -> None:
        self._log.append(('start', self.name, context))
        if self._fail_start:
            raise RuntimeError(f'{self.name} cannot start')

    def stop(self, context) -> None:
        self._log.append(('stop', self.name, context))
        if self._fail_stop:
            self._fail_stop -= 1
            raise RuntimeError(f'{self.name} cannot stop')


class ServicesTest(unittest.TestCase):
    def hooks_with(self, *services, **methods_needing):
        made = H.Hooks()
        for service in services:
            made.service(service)
        for method, needs in methods_needing.items():
            row_names_service(self, method.replace('_', '.'), needs)
            made.methods.register(method.replace('_', '.'), lambda ctx, params: {'code': 'ok'}, service=needs)
        made.freeze()
        return made

    def test_services_start_in_order_with_the_context_and_stop_in_reverse(self) -> None:
        log, context = [], H.ServiceContext(state_dir=pathlib.Path('/state'))
        made = self.hooks_with(Service('events', log), Service('sessions', log), Service('approvals', log))
        made.start_services(context)
        made.stop_services(context)
        self.assertEqual(log, [('start', 'events', context), ('start', 'sessions', context), ('start', 'approvals', context),
                               ('stop', 'approvals', context), ('stop', 'sessions', context), ('stop', 'events', context)])
        made.stop_services(context)
        self.assertEqual(len(log), 6, 'a service is stopped once')

    @quiet
    def test_a_service_that_does_not_start_stops_the_ones_before_it_and_fails_composition(self) -> None:
        log, context = [], H.ServiceContext()
        made = self.hooks_with(Service('events', log), Service('sessions', log, fail_start=True), Service('approvals', log))
        with self.assertRaises(RuntimeError):
            made.start_services(context)
        # The one that failed part way is stopped too: it was tracked before its start was tried.
        self.assertEqual([(what, name) for what, name, _ in log],
                         [('start', 'events'), ('start', 'sessions'), ('stop', 'sessions'), ('stop', 'events')])
        self.assertFalse(made.service_ready('events'))

    @quiet
    def test_every_service_is_stopped_even_when_one_fails_and_the_failure_is_raised_last(self) -> None:
        log, context = [], H.ServiceContext()
        made = self.hooks_with(Service('events', log), Service('sessions', log, fail_stop=1), Service('approvals', log))
        made.stop_retry_s = 0
        made.start_services(context)
        with self.assertRaises(RuntimeError) as caught:
            made.stop_services(context)
        self.assertIn('sessions', str(caught.exception))
        # A stop that raised is not a stop: it is tried again, and only then is the service before it stopped.
        self.assertEqual([name for what, name, _ in log if what == 'stop'], ['approvals', 'sessions', 'sessions', 'events'])

    def test_a_method_that_needs_a_service_is_unavailable_until_it_is_ready(self) -> None:
        sessions = Service('sessions')
        made = self.hooks_with(sessions, session_list='sessions')
        answer = lambda: made.methods.dispatch('session.list', {}, H.OWNER, None)   # noqa: E731
        self.assertEqual(answer(), {'code': 'unavailable', 'detail': 'service_starting'}, 'registered and not started')
        made.start_services(H.ServiceContext())
        self.assertEqual(answer(), {'code': 'unavailable', 'detail': 'service_starting'}, 'started and not ready')
        sessions.ready = True
        self.assertEqual(answer(), {'code': 'ok'})
        made.stop_services(H.ServiceContext())
        self.assertEqual(answer(), {'code': 'unavailable', 'detail': 'service_starting'}, 'stopped')

    def test_a_method_cannot_need_a_service_nobody_registered(self) -> None:
        with self.assertRaises(H.HookError) as caught:
            self.hooks_with(Service('events'), session_list='sessions')
        self.assertIn('sessions', str(caught.exception))

    def test_a_service_has_a_name_a_start_a_stop_and_ready_and_a_name_is_used_once(self) -> None:
        made = H.Hooks()
        made.service(Service('events'))
        for bad in (Service('events'), Service(''), object(), type('NoReady', (), {'name': 'x', 'start': print, 'stop': print})()):
            with self.subTest(service=bad):
                with self.assertRaises(H.HookError):
                    made.service(bad)

    def test_the_service_context_names_everything_a_service_is_given(self) -> None:
        self.assertEqual(list(H.ServiceContext.__dataclass_fields__),
                         ['daemon', 'registry', 'control_registry', 'control', 'store', 'state_dir', 'clock', 'closing'])


class AdmissionTest(unittest.TestCase):
    def test_with_nothing_registered_the_gate_refuses_nobody_and_waits_for_nothing(self) -> None:
        made = H.bare()
        self.assertFalse(made.gate.enabled)
        self.assertTrue(made.gate.enter())
        made.begin_close()
        self.assertTrue(made.gate.enter(), 'as today: a call after closing began is still answered')
        made.finish_close(H.ServiceContext())   # returns: nothing to wait for, although nobody left
        self.assertTrue(made.closing.is_set())

    def test_a_handler_a_service_or_a_store_each_turn_the_gate_on(self) -> None:
        for register in (lambda made: made.methods.register('session.list', lambda ctx, params: {'code': 'ok'}),
                         lambda made: made.service(Service('events')),
                         lambda made: made.set('event_store', lambda state_dir: FakeStore())):
            made = H.Hooks()
            register(made)
            made.freeze()
            self.assertTrue(made.gate.enabled)
        made = H.Hooks()
        made.set('authorize', lambda principal, method, params: None)
        made.freeze()
        self.assertFalse(made.gate.enabled, 'a hook that holds no resource does not need the gate')

    def test_no_call_is_admitted_after_closing_began(self) -> None:
        gate = H.Admission()
        gate.enabled = True
        self.assertTrue(gate.enter())
        gate.close()
        self.assertFalse(gate.enter())
        self.assertEqual(gate.admitted, 1, 'the call admitted before is still inside')
        gate.leave()
        self.assertEqual(gate.admitted, 0)

    def test_closing_waits_for_an_admitted_call_with_its_services_and_store_alive(self) -> None:
        log = []
        store = FakeStore(log)
        made = H.Hooks()
        made.service(Service('sessions', log))
        made.set('event_store', lambda state_dir: store)
        made.freeze()
        context = H.ServiceContext()
        made.open_store('/state')
        made.start_services(context)
        self.assertTrue(made.gate.enter())          # a handler, or its after-commit callback, is running
        made.begin_close()
        self.assertFalse(made.gate.enter())
        finished, at_the_drain, drain = threading.Event(), threading.Event(), made.gate.drain
        made.gate.drain = lambda: (at_the_drain.set(), drain())
        closer = threading.Thread(target=lambda: (made.finish_close(context), finished.set()), daemon=True)
        closer.start()
        self.assertTrue(at_the_drain.wait(BOUND_S), 'the close reached its wait')
        # The closer has reached the drain. Everything it did before it is in the log: nothing was stopped
        # or closed under the admitted call, and it cannot get past the drain while the call is inside.
        self.assertEqual([what for what, _, _ in log], ['start'])
        self.assertFalse(store.closed)
        self.assertFalse(finished.is_set())
        made.gate.leave()
        closer.join(BOUND_S)
        self.assertTrue(finished.is_set())
        self.assertEqual([(what, name) for what, name, _ in log], [('start', 'sessions'), ('stop', 'sessions'), ('close', 'store')],
                         'the services are stopped after the drain, and the store is closed last')
        self.assertIsNone(made.methods.store)

    @quiet
    def test_a_stalled_drain_is_logged_and_goes_on_waiting(self) -> None:
        gate = H.Admission()
        gate.enabled, gate.stall_log_s = True, 0.001
        gate.enter()
        gate.close()
        waits, original = [], gate._changed.wait

        def wait(timeout):
            waits.append(timeout)
            return original(timeout)
        gate._changed.wait = wait
        drained = threading.Event()
        closer = threading.Thread(target=lambda: (gate.drain(), drained.set()), daemon=True)
        closer.start()
        deadline = time.monotonic() + BOUND_S
        while len(waits) < 3 and closer.is_alive() and time.monotonic() < deadline:   # two stall intervals have passed
            time.sleep(0.001)
        self.assertGreaterEqual(len(waits), 3, 'the drain went on waiting, interval after interval')
        self.assertTrue(closer.is_alive(), 'a drain that stalled is still waiting')
        self.assertFalse(drained.is_set())
        gate.leave()
        closer.join(BOUND_S)
        self.assertTrue(drained.is_set())

    @quiet
    def test_the_drain_itself_keeps_an_interruption_until_the_last_call_has_left(self) -> None:
        """``drain()`` with no close around it, on one thread, so no schedule decides it. It is interrupted as it
        takes the condition and again while it waits, with one call inside. It goes on both times, returns
        control only after the call has left, and raises the FIRST interruption then."""
        gate = H.Admission()
        gate.enabled = True
        gate.enter()
        gate.close()
        condition = gate._changed
        first, second = KeyboardInterrupt('as it takes the condition'), KeyboardInterrupt('while it waits')
        seen = []

        class Interrupted:
            def __enter__(self):
                seen.append(('take', gate.admitted))
                if len(seen) == 1:
                    raise first
                return condition.__enter__()

            def __exit__(self, *exc):
                return condition.__exit__(*exc)

            def wait(self, timeout=None):
                seen.append(('wait', gate.admitted))
                if [what for what, _ in seen].count('wait') == 1:
                    raise second
                gate._changed = condition      # the call leaves while the drain waits, as another thread's would
                gate.leave()
                return True
        gate._changed = Interrupted()
        with self.assertRaises(KeyboardInterrupt) as raised:
            gate.drain()
        self.assertEqual(seen, [('take', 1), ('take', 1), ('wait', 1), ('take', 1), ('wait', 1)],
                         'after each interruption it asked again, with the call still inside')
        self.assertEqual(gate.admitted, 0, 'it raised only after the last call had left')
        self.assertIs(raised.exception, first, 'and what it raises is the first interruption')

    @quiet
    def test_a_teardown_error_is_raised_only_after_everything_was_stopped_and_closed(self) -> None:
        log = []
        store = FakeStore(log)
        made = H.Hooks()
        made.service(Service('events', log))
        made.service(Service('sessions', log, fail_stop=1))
        made.set('event_store', lambda state_dir: store)
        made.freeze()
        made.stop_retry_s = 0
        context = H.ServiceContext()
        made.open_store('/state')
        made.start_services(context)
        made.begin_close()
        with self.assertRaises(RuntimeError):
            made.finish_close(context)
        self.assertEqual([(what, name) for what, name, _ in log if what != 'start'],
                         [('stop', 'sessions'), ('stop', 'sessions'), ('stop', 'events'), ('close', 'store')])
        self.assertTrue(store.closed)


def routing(principal, request):
    return {'route_entry': {'route': {'engine': 'codex', 'model': 'gpt-6', 'connection': {'kind': 'subscription', 'id': 'plus'}},
                            'deadline_s': 600.0, 'binary': None},
            'route_decision': {'chosen': 'codex', 'reason': 'hand_pick'}}


class StartRegistrantTest(unittest.TestCase):
    """The parts of the versioned start path that live here: who declared what, the identity, the snapshot."""

    def hooks_with(self, *registrants):
        made = H.Hooks()
        for name, fields, keys, normalize, *more in registrants:
            made.start_registrant(name, fields, keys, normalize, chooses_engine=bool(more and more[0]))
        made.freeze()
        return made

    def test_registering_one_turns_the_versioned_path_on(self) -> None:
        self.assertFalse(H.bare().start_on)
        made = self.hooks_with(('workspace', ('workspace',), ('workspace',), lambda principal, request: {}))
        self.assertTrue(made.start_on)
        self.assertEqual(made.declared_fields(), frozenset({'workspace'}))

    def test_registrants_run_routing_first_then_workspace_then_the_others_by_name(self) -> None:
        order = []

        def named(name):
            return lambda principal, request: order.append(name) or {}
        made = self.hooks_with(*((name, (), (), named(name)) for name in ('sessions', 'workspace', 'quota', 'routing', 'containment')))
        self.assertEqual(made.start_order(), ('routing', 'workspace', 'containment', 'quota', 'sessions'))
        made.launch_snapshot(H.OWNER, {})
        self.assertEqual(order, ['routing', 'workspace', 'containment', 'quota', 'sessions'])

    def test_a_registrant_is_asked_with_the_principal_and_the_request_and_sets_only_its_keys(self) -> None:
        seen = []

        def workspace(principal, request):
            seen.append((principal, request))
            return {'workspace': {'mode': 'worktree', 'source_path': '/home/dev/projects/atlas', 'base_ref': 'refs/heads/main',
                                  'worktree_path': None}}
        made = self.hooks_with(('routing', ('hand_pick',), ('route_entry', 'route_decision'), routing, True),
                               ('workspace', ('workspace',), ('workspace',), workspace))
        request = {'tenant': 't1', 'prompt': 'x', 'workspace': {'path': '/home/dev/projects/atlas'},
                   'owner': 'notes.search', 'session_id': 'ses-forged', 'budget': {'cap_usd': 99}}
        launch, refusal = made.launch_snapshot(H.DEVICE, request)
        self.assertIsNone(refusal)
        self.assertEqual(seen, [(H.DEVICE, request)])
        self.assertEqual(set(launch), set(H.LAUNCH_KEYS))
        self.assertEqual(launch['workspace']['source_path'], '/home/dev/projects/atlas')
        self.assertEqual(launch['route_entry']['deadline_s'], 600.0)
        # Nothing is copied from the request: what no registrant set is what the server alone knows.
        self.assertEqual((launch['owner'], launch['session_id'], launch['turn_id'], launch['containment_level'], launch['budget']),
                         ('host', None, None, None, {'cap_usd': 0}))
        self.assertTrue(made.engine_may_be_chosen)

    def test_the_owner_is_the_plugin_only_when_the_principal_is_that_plugin(self) -> None:
        made = self.hooks_with(('quota', (), (), lambda principal, request: {}))
        self.assertEqual(made.launch_snapshot(H.Principal('plugin', 'notes.search'), {'owner': 'host'})[0]['owner'], 'notes.search')
        for principal in (H.OWNER, H.DEVICE, H.Principal('session', 'ses-1'), H.Principal('internal', 'sessions')):
            self.assertEqual(made.launch_snapshot(principal, {'owner': 'notes.search'})[0]['owner'], 'host')

    @quiet
    def test_a_registrant_refuses_with_the_methods_own_code_and_a_failing_one_refuses_the_start(self) -> None:
        def refuse(principal, request):
            return H.Refusal('workspace_invalid', 'not a repository')

        def broken(principal, request):
            raise OSError('cannot stat')
        later = []
        for normalize, expected in ((refuse, {'code': 'workspace_invalid', 'detail': 'not a repository'}),
                                    (lambda principal, request: H.Refusal('workspace_busy'), {'code': 'workspace_busy'}),
                                    (broken, {'code': 'unavailable', 'detail': 'start_registrant_failed'}),
                                    (lambda principal, request: {'owner': 'notes.search'}, {'code': 'unavailable', 'detail': 'start_registrant_failed'}),
                                    (lambda principal, request: None, {'code': 'unavailable', 'detail': 'start_registrant_failed'})):
            made = self.hooks_with(('workspace', ('workspace',), ('workspace',), normalize),
                                   ('zlast', (), (), lambda principal, request: later.append(1) or {}))
            self.assertEqual(made.launch_snapshot(H.OWNER, {}), (None, expected))
        self.assertEqual(later, [], 'a refusal ends the start: no later registrant runs')

    def test_two_registrants_cannot_set_one_key_and_none_can_set_the_owner(self) -> None:
        made = H.Hooks()
        made.start_registrant('routing', ('hand_pick',), ('route_entry', 'route_decision'), routing, chooses_engine=True)
        for name, fields, keys, chooses in (('quota', (), ('route_entry',), False), ('host', (), ('owner',), False),
                                            ('routing', (), (), False), ('other', (), (), True), ('', (), (), False),
                                            ('workspace', ('prompt',), (), False), ('workspace', ('owner',), (), False),
                                            ('workspace', ('principal',), (), False), ('workspace', ('',), (), False)):
            with self.subTest(name=name, fields=fields, keys=keys):
                with self.assertRaises(H.HookError):
                    made.start_registrant(name, fields, keys, lambda principal, request: {}, chooses_engine=chooses)
        with self.assertRaises(H.HookError):
            made.start_registrant('workspace', ('workspace',), ('workspace',), 'not a function')

    def test_the_identity_covers_the_engine_the_prompt_and_only_the_declared_fields_that_are_present(self) -> None:
        made = self.hooks_with(('routing', ('hand_pick',), ('route_entry', 'route_decision'), routing, True),
                               ('workspace', ('workspace',), ('workspace',), lambda principal, request: {}))
        digest = 'a' * 64
        base = {'tenant': 't1', 'engine': 'codex', 'prompt': 'x', 'client_key': 'k-1'}
        one = made.start_intent(base, digest)
        self.assertEqual(made.start_intent_text(base, digest), '{"engine":"codex","prompt_sha256":"%s"}' % digest)
        self.assertEqual(one, hashlib.sha256(('{"engine":"codex","prompt_sha256":"%s"}' % digest).encode()).hexdigest())
        no_engine = {key: value for key, value in base.items() if key != 'engine'}
        self.assertEqual(made.start_intent_text(no_engine, digest), '{"engine":null,"prompt_sha256":"%s"}' % digest,
                         'a request that names no engine says so: the member is there, and null')
        self.assertEqual(made.start_intent_text(dict(base, engine=None), digest), made.start_intent_text(no_engine, digest))
        # A missing field, a null one and one nobody declared are the same request.
        for same in (dict(base, workspace=None), dict(base, labels={'team': 'x'}), dict(base, containment_level='contained'),
                     dict(base, tenant='t2', client_key='k-2')):
            self.assertEqual(made.start_intent(same, digest), one)
        # A declared field that is present makes it another request; so does the engine, and the prompt.
        with_workspace = made.start_intent(dict(base, workspace={'path': '/a'}), digest)
        self.assertEqual(with_workspace, hashlib.sha256(
            ('{"engine":"codex","prompt_sha256":"%s","workspace":{"path":"/a"}}' % digest).encode()).hexdigest())
        others = {with_workspace, made.start_intent(dict(base, workspace={'path': '/b'}), digest),
                  made.start_intent(dict(base, hand_pick={'engine': 'codex', 'model': 'gpt-6'}), digest),
                  made.start_intent(dict(base, engine='claude_code'), digest), made.start_intent(base, 'b' * 64),
                  made.start_intent({key: value for key, value in base.items() if key != 'engine'}, digest)}
        self.assertEqual(len(others | {one}), 7)
        self.assertEqual(made.declared_present(dict(base, workspace={'path': '/a'}, hand_pick=None)), {'workspace': {'path': '/a'}})
        with self.assertRaises(ValueError):
            made.start_intent(dict(base, workspace={'depth': 2**60}), digest)

    def test_the_launch_context_is_the_stored_launch_object_and_the_records_own_principal(self) -> None:
        launch, _ = self.hooks_with(('routing', ('hand_pick',), ('route_entry', 'route_decision'), routing, True)) \
            .launch_snapshot(H.OWNER, {})
        record = {'run_id': 'run-1', 'launch': launch, 'principal': H.OWNER.public()}
        context = H.LaunchContext.from_record(record)
        self.assertEqual({key: getattr(context, key) for key in H.LAUNCH_KEYS}, launch)
        self.assertIs(context.route_entry, launch['route_entry'], 'the values are the record\'s, not copies or recomputed')
        self.assertEqual(context.principal, H.Principal('owner', 'local'))
        self.assertIsNone(H.LaunchContext.from_record({'run_id': 'run-0'}), 'a record of the path that is off has no launch')
        for one_sided in ({'run_id': 'run-1', 'launch': launch}, {'run_id': 'run-1', 'principal': H.OWNER.public()},
                          {'run_id': 'run-1', 'launch': launch, 'principal': None},
                          {'run_id': 'run-1', 'launch': None, 'principal': H.OWNER.public()},
                          {'run_id': 'run-1', 'launch': launch, 'principal': {'kind': 'owner'}},
                          {'run_id': 'run-1', 'launch': launch, 'principal': {'kind': 'owner', 'id': 'local', 'scope': 'all'}}):
            with self.subTest(record=one_sided):
                with self.assertRaises(ValueError):
                    H.LaunchContext.from_record(one_sided)
        kept = H.OWNER.public()
        for broken in ({'launch': dict(launch, principal={'kind': 'owner'}), 'principal': kept},
                       {'launch': {'owner': 'host'}, 'principal': kept}, {'launch': 'x', 'principal': kept},
                       {'launch': {key: value for key, value in launch.items() if key != 'budget'}, 'principal': kept},
                       {'launch': launch, 'principal': 'owner'}, {'launch': launch, 'principal': {'kind': 'root', 'id': 'x'}}):
            with self.subTest(record=broken):
                with self.assertRaises(ValueError):
                    H.LaunchContext.from_record(broken)


# ======================================================================================================
# The witnesses of the first cross-family read of these modules, each as a test. Every test below fails
# on the head that read saw (b5c7ee28).

class PublishedStoreProtocolTest(unittest.TestCase):
    """R1: the dispatcher and the fake use the event store helper's published names, and no others."""

    def test_the_dispatcher_needs_only_the_two_published_receipt_methods(self) -> None:
        used, kept, calls = [], {}, []

        class Tx:
            def receipt_get(self, tenant, method, command_id):
                used.append('receipt_get')
                return kept.get((tenant, method, command_id))

            def receipt_put(self, tenant, method, command_id, payload_digest, answer):
                used.append('receipt_put')
                kept[(tenant, method, command_id)] = (payload_digest, answer)

        class Store:
            @contextlib.contextmanager
            def transaction(self):
                yield Tx()
        table = table_with(Store(), **{'session.turn': lambda ctx, params: calls.append(1) or {'code': 'ok'}})
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, None), {'code': 'ok'})
        self.assertEqual((calls, used), ([1], ['receipt_get', 'receipt_put']))
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, None), {'code': 'ok'})
        self.assertEqual((calls, used), ([1], ['receipt_get', 'receipt_put', 'receipt_get']), 'a replay reads the receipt and writes nothing')

    def test_the_fake_has_the_published_names_and_no_other_way_in(self) -> None:
        store = work_store()
        with store.transaction() as tx:
            self.assertEqual({name for name in dir(tx) if not name.startswith('_')},
                             {'receipt_get', 'receipt_put', 'append', 'execute'})
        for name in ('transaction', 'register_tables', 'close'):
            self.assertTrue(callable(getattr(store, name)))
        # What a test reads for its assertions is a copy: nothing is written through it.
        store.receipts[('t', 'm', 'c')] = ('d', {'code': 'ok'})
        store.rows[('work', 'c')] = {'state': 'made up'}
        self.assertEqual((store.receipts, store.rows, store.events), ({}, {}, []))


class FakeStoreTest(unittest.TestCase):
    """R4: the one shared fake is a transaction boundary: what the real helper would refuse, it refuses."""

    def filled(self, **options):
        store = work_store(**options)
        with store.transaction() as tx:
            tx.receipt_put('t1', 'session.turn', 'c1', 'digest-1', {'code': 'ok', 'items': ['first']})
            tx.execute('INSERT INTO work VALUES (?, ?, ?)', ('c1', 'op-1', 'pending'))
            self.assertEqual(tx.append({'type': 'turn.started'}), 1)
        return store

    def test_a_rollback_leaves_rows_receipts_and_events_as_they_were(self) -> None:
        store = self.filled()
        before = (store.receipts, store.rows, store.events)
        with self.assertRaises(RuntimeError):
            with store.transaction() as tx:
                digest, answer = tx.receipt_get('t1', 'session.turn', 'c1')
                answer['items'].append('rolled-back')       # the reader's own copy: not the stored answer
                answer['code'] = 'rolled-back'
                tx.execute('UPDATE work SET state = ? WHERE command_id = ?', ('rolled-back', 'c1'))
                tx.receipt_put('t1', 'session.turn', 'c2', 'digest-2', {'code': 'ok'})
                tx.append({'type': 'turn.ended'})
                raise RuntimeError('undo')
        self.assertEqual((store.receipts, store.rows, store.events), before)
        self.assertEqual((store.commits, store.rollbacks), (1, 1))

    def test_what_is_written_and_what_is_read_are_owned_copies(self) -> None:
        store, answer, event = work_store(), {'code': 'ok', 'items': []}, {'type': 'turn.started', 'ids': []}
        with store.transaction() as tx:
            tx.receipt_put('t1', 'session.turn', 'c1', 'digest-1', answer)
            tx.append(event)
            answer['items'].append('changed after the write')
            event['ids'].append('changed after the write')
        self.assertEqual(store.receipts[('t1', 'session.turn', 'c1')], ('digest-1', {'code': 'ok', 'items': []}))
        self.assertEqual(store.events, [{'type': 'turn.started', 'ids': []}])
        with store.transaction() as tx:
            tx.receipt_get('t1', 'session.turn', 'c1')[1]['items'].append('changed after the read')
        self.assertEqual(store.receipts[('t1', 'session.turn', 'c1')][1], {'code': 'ok', 'items': []})

    def test_a_transaction_is_used_by_its_owner_and_only_until_it_exits(self) -> None:
        store = work_store()
        with store.transaction() as kept:
            other = []
            thread = threading.Thread(target=lambda: other.append(self.refused(lambda: kept.receipt_get('t', 'm', 'c'))), daemon=True)
            thread.start()
            thread.join(BOUND_S)
            self.assertEqual(other, ['transaction_not_active_for_caller'], 'another thread is not the owner')
        for use in (lambda: kept.receipt_get('t', 'm', 'c'), lambda: kept.receipt_put('t', 'm', 'c', 'd', {'code': 'ok'}),
                    lambda: kept.append({'type': 'x'}), lambda: kept.execute('INSERT INTO work VALUES (?, ?, ?)', ('c', 'o', 's'))):
            self.assertEqual(self.refused(use), 'transaction_not_active_for_caller')
        self.assertEqual((store.receipts, store.rows, store.events), ({}, {}, []))

    def refused(self, use) -> str:
        with self.assertRaises(RuntimeError) as caught:
            use()
        return str(caught.exception)

    def test_a_second_transaction_in_the_same_thread_raises_and_does_not_wait(self) -> None:
        for serial in (True, False):
            with self.subTest(serial=serial):
                store = work_store(serial=serial)
                with store.transaction():
                    with self.assertRaises(RuntimeError) as caught:
                        with store.transaction():
                            self.fail('a second transaction was opened inside the first')
                    self.assertEqual(str(caught.exception), 'nested_transaction')
                self.assertEqual((store.commits, store.rollbacks, store.open_transactions), (1, 0, 0))
                with store.transaction():      # and the thread can open one again afterwards
                    pass

    def test_a_close_is_refused_while_a_transaction_is_open_and_a_closed_store_refuses_access(self) -> None:
        for serial in (True, False):
            with self.subTest(serial=serial):
                store = self.filled(serial=serial)
                with store.transaction() as tx:
                    tx.execute('UPDATE work SET state = ? WHERE command_id = ?', ('written before the close', 'c1'))
                    self.assertEqual(self.refused(store.close), 'transactions_active')
                    self.assertFalse(store.closed, 'a refused close closed nothing')
                    tx.append({'type': 'still usable'})
                self.assertEqual(store.rows[('work', 'c1')]['state'], 'written before the close', 'and the transaction committed')
                self.assertEqual((store.commits, store.open_transactions), (2, 0))
                store.close()
                self.assertTrue(store.closed)
                self.assertEqual(self.refused(lambda: store.transaction().__enter__()), 'store_closed')
                self.assertEqual(self.refused(lambda: store.register_tables('queue', 1, 'CREATE TABLE q (a TEXT)')), 'store_closed')
                self.assertEqual(store.open_transactions, 0, 'a refused entry is not counted as open')
                self.assertEqual(self.refused(lambda: tx.append({'type': 'x'})), 'transaction_not_active_for_caller')

    def test_a_receipt_is_never_replaced(self) -> None:
        store = self.filled()
        with self.assertRaises(sqlite3.IntegrityError):
            with store.transaction() as tx:
                tx.receipt_put('t1', 'session.turn', 'c1', 'another-digest', {'code': 'conflict'})
        self.assertEqual(store.receipts[('t1', 'session.turn', 'c1')], ('digest-1', {'code': 'ok', 'items': ['first']}))

    def test_only_declared_tables_and_only_values_with_a_wire_form(self) -> None:
        store = self.filled()
        before = (store.receipts, store.rows, store.events)
        for statement in ('INSERT INTO undeclared VALUES (1)', 'SELECT * FROM receipts', 'DELETE FROM receipts', 'DELETE FROM events',
                          'CREATE TABLE made_in_a_handler (a TEXT)', 'DROP TABLE work', 'PRAGMA journal_mode=DELETE'):
            with self.subTest(statement=statement):
                with self.assertRaises(sqlite3.Error):
                    with store.transaction() as tx:
                        tx.execute(statement)
        for write in (lambda tx: tx.receipt_put('t1', 'session.turn', 'c9', 'd', {'x': object()}), lambda tx: tx.append(object()),
                      lambda tx: tx.append({'n': float('nan')}), lambda tx: tx.receipt_put('t1', 'session.turn', 7, 'd', {'code': 'ok'}),
                      lambda tx: tx.receipt_put('t1', 'session.turn', 'c9', 'd', {'when': {1, 2}})):
            with self.assertRaises((TypeError, ValueError)):
                with store.transaction() as tx:
                    write(tx)
        self.assertEqual((store.receipts, store.rows, store.events), before)
        with store.transaction() as tx:     # a declared table is read and written, with parameters
            tx.execute('UPDATE work SET state = ? WHERE command_id = ?', ('done', 'c1'))
            self.assertEqual(tx.execute('SELECT command_id, state FROM work'), [('c1', 'done')])

    def test_a_lane_declares_its_tables_once_and_events_are_numbered_in_order(self) -> None:
        store = work_store()
        with self.assertRaises(RuntimeError):
            store.register_tables('sessions', 2, 'CREATE TABLE more (a TEXT)')
        with self.assertRaises(RuntimeError):
            store.register_tables('queue', 1, 'SELECT 1')
        store.register_tables('queue', 1, 'CREATE TABLE queue_items (item_id TEXT PRIMARY KEY, state TEXT)')
        with store.transaction() as tx:
            self.assertEqual([tx.append({'type': name}) for name in ('a', 'b', 'c')], [1, 2, 3])
            tx.execute('INSERT INTO queue_items VALUES (?, ?)', ('item-1', 'queued'))
        self.assertEqual([event['type'] for event in store.events], ['a', 'b', 'c'])
        self.assertEqual(store.rows, {('queue_items', 'item-1'): {'state': 'queued'}})

    def test_two_transactions_open_at_once_never_lose_a_commit(self) -> None:
        # Where the store lets two be open at once, the second to write what the first changed is refused.
        store = self.filled(serial=False)
        results, read = {}, {'A': threading.Event(), 'B': threading.Event()}
        go = {'A': threading.Event(), 'B': threading.Event()}

        def work(name):
            try:
                with store.transaction() as tx:
                    tx.execute('SELECT state FROM work WHERE command_id = ?', ('c1',))
                    read[name].set()
                    go[name].wait(BOUND_S)
                    tx.execute('UPDATE work SET state = ? WHERE command_id = ?', (name, 'c1'))
                results[name] = 'committed'
            except sqlite3.OperationalError:
                results[name] = 'conflict'
        threads = {name: threading.Thread(target=work, args=(name,), daemon=True) for name in ('A', 'B')}
        for name in ('A', 'B'):
            threads[name].start()
            self.assertTrue(read[name].wait(BOUND_S))
        for name in ('A', 'B'):               # both have read the same state; A writes and commits, then B tries
            go[name].set()
            threads[name].join(BOUND_S)
        self.assertEqual(results, {'A': 'committed', 'B': 'conflict'})
        self.assertEqual(store.rows[('work', 'c1')]['state'], 'A', 'the committed write is still there')
        self.assertEqual((store.commits, store.rollbacks), (2, 1))


class FakeStoreBoundaryTest(unittest.TestCase):
    """The second reads' B3: closing and committing share one guard, a declaration can only declare, and what a
    declaration states is enforced."""

    def refused(self, use) -> str:
        with self.assertRaises(RuntimeError) as caught:
            use()
        return str(caught.exception)

    def test_a_commit_cannot_come_after_a_close_has_returned(self) -> None:
        """A transaction is past its block and about to COMMIT, held there. Another thread closes the store.
        The close is refused; it does not return and let the commit follow it."""
        store = work_store()
        at_commit, go, done = threading.Event(), threading.Event(), []
        self.addCleanup(go.set)
        connect = store._connect

        class HeldAtCommit:
            def __init__(self, db) -> None:
                self._db = db

            def execute(self, sql, *args):
                if sql == 'COMMIT':
                    at_commit.set()
                    go.wait(BOUND_S)
                return self._db.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self._db, name)
        store._connect = lambda: HeldAtCommit(connect())

        def writer():
            with store.transaction() as tx:
                tx.execute('INSERT INTO work VALUES (?, ?, ?)', ('c1', 'op-1', 'pending'))
            done.append('committed')
        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        self.assertTrue(at_commit.wait(BOUND_S), 'the transaction is at its commit')
        self.assertEqual(self.refused(store.close), 'transactions_active')
        self.assertEqual((store.closed, store.open_transactions), (False, 1))
        go.set()
        thread.join(BOUND_S)
        self.assertEqual((done, store.commits, store.open_transactions), (['committed'], 1, 0))
        store._connect = connect
        self.assertEqual(store.rows, {('work', 'c1'): {'operation_id': 'op-1', 'state': 'pending'}})
        store.close()
        self.assertTrue(store.closed)

    def test_a_transaction_that_waited_while_the_store_closed_is_refused_when_its_turn_comes(self) -> None:
        store = work_store()
        asked, outcome, write = threading.Event(), [], store._write

        class Announcing:
            def acquire(self):
                asked.set()
                return write.acquire()

            def release(self):
                write.release()
        write.acquire()                         # stands for a transaction in flight: the next one waits its turn
        store._write = Announcing()

        def later():
            try:
                with store.transaction():
                    outcome.append('entered')
            except RuntimeError as exc:
                outcome.append(str(exc))
        thread = threading.Thread(target=later, daemon=True)
        thread.start()
        self.assertTrue(asked.wait(BOUND_S), 'it has asked for its turn')
        store.close()                           # nothing is open: the close succeeds
        write.release()                         # and now its turn comes
        thread.join(BOUND_S)
        self.assertEqual((outcome, store.open_transactions, store.commits, store.rollbacks), (['store_closed'], 0, 0, 0))

    def test_a_declaration_can_only_declare_and_leaves_nothing_behind_when_it_fails(self) -> None:
        store = work_store()
        with store.transaction() as tx:
            tx.receipt_put('t1', 'session.turn', 'c1', 'digest-1', {'code': 'ok'})
            tx.execute('INSERT INTO work VALUES (?, ?, ?)', ('c1', 'op-1', 'pending'))
            tx.append({'type': 'turn.started'})
        before = (store.receipts, store.rows, store.events)
        refused = {
            "a write to the helper's own table": "UPDATE receipts SET payload_digest = 'replaced'; CREATE TABLE own (id TEXT)",
            'a second statement that is not SQL': 'CREATE TABLE leaked (id TEXT); THIS IS NOT SQL',
            'a statement that fails after a table was made': 'CREATE TABLE leaked (id TEXT); CREATE TABLE leaked (id TEXT)',
            'a view': 'CREATE TABLE seen (id TEXT); CREATE VIEW peek AS SELECT * FROM receipts',
            'a trigger': 'CREATE TABLE t1 (id TEXT); CREATE TRIGGER sneak AFTER INSERT ON t1 BEGIN DELETE FROM receipts; END',
            "a change to another lane's table": 'CREATE TABLE mine (id TEXT); ALTER TABLE work ADD COLUMN extra TEXT',
            "another lane's table dropped": 'CREATE TABLE mine (id TEXT); DROP TABLE work',
            "an index on another lane's table": 'CREATE TABLE mine (id TEXT); CREATE INDEX on_work ON work (state)',
            'rows written by a declaration': "CREATE TABLE mine (id TEXT); INSERT INTO mine VALUES ('x')",
            "the helper's own table read": 'CREATE TABLE mine AS SELECT * FROM receipts',
            "the helper's own table declared again, quietly, and indexed":
                'CREATE TABLE mine (id TEXT); CREATE TABLE IF NOT EXISTS receipts (id TEXT); '
                'CREATE INDEX on_receipts ON receipts (tenant)',
            'no table at all': 'SELECT 1',
        }
        made = {'own', 'leaked', 'seen', 'peek', 't1', 'sneak', 'mine', 'on_work', 'on_receipts'}
        for what, ddl in refused.items():
            with self.subTest(declaration=what):
                with self.assertRaises((sqlite3.Error, RuntimeError)):
                    store.register_tables('queue', 1, ddl)
                names = {row[0] for row in store._read('SELECT name FROM sqlite_master')}
                self.assertEqual(names & made, set(), 'nothing of a refused declaration is left')
                self.assertEqual((store.receipts, store.rows, store.events), before)
        # The lane was never recorded as declared, so it can still declare: a table and an index of its own.
        store.register_tables('queue', 1, 'CREATE TABLE queue_items (item_id TEXT PRIMARY KEY, state TEXT); '
                                          'CREATE INDEX by_state ON queue_items (state)')
        with store.transaction() as tx:
            tx.execute('INSERT INTO queue_items VALUES (?, ?)', ('item-1', 'queued'))
            self.assertEqual(tx.execute('SELECT item_id FROM queue_items WHERE state = ?', ('queued',)), [('item-1',)])

    def test_a_foreign_key_a_declaration_states_is_enforced(self) -> None:
        store = FakeStore()
        store.register_tables('queue', 1, 'CREATE TABLE parents (id TEXT PRIMARY KEY); '
                                          'CREATE TABLE children (id TEXT PRIMARY KEY, parent TEXT NOT NULL REFERENCES parents (id))')
        with self.assertRaises(sqlite3.IntegrityError):
            with store.transaction() as tx:
                tx.execute('INSERT INTO children VALUES (?, ?)', ('child-1', 'no-such-parent'))
        self.assertEqual(store.rows, {})
        with store.transaction() as tx:
            tx.execute('INSERT INTO parents VALUES (?)', ('parent-1',))
            tx.execute('INSERT INTO children VALUES (?, ?)', ('child-1', 'parent-1'))
        self.assertEqual(set(store.rows), {('parents', 'parent-1'), ('children', 'child-1')})

    def test_an_object_key_that_is_not_text_is_refused_and_not_turned_into_text(self) -> None:
        store = work_store()
        for write in (lambda tx: tx.append({'type': 'x', 'by_id': {1: 'one'}}),
                      lambda tx: tx.receipt_put('t1', 'session.turn', 'c9', 'd', {'code': 'ok', 'items': [{2: 'two'}]})):
            with self.assertRaises(TypeError):
                with store.transaction() as tx:
                    write(tx)
        self.assertEqual((store.receipts, store.events), ({}, []))


def composed(table, built_in):
    """What a composition hands the dispatcher as ``call``: the built-ins are answered here, the rest by the table."""
    def call(method, params=None):
        if method in methods.BUILT_IN:
            return built_in(method, params)
        return table.dispatch(method, params or {}, H.OWNER, call)
    return call


class NestedDispatchTest(unittest.TestCase):
    """R2: a nested call inherits the restrictions of the command it is inside."""

    def setUp(self) -> None:
        self.asked, self.store = [], work_store()

    def built_in(self, method, params):
        self.asked.append(method)
        return {'code': 'ok', 'runs': []} if method == 'run.list' else {'code': 'stopped' if method == 'run.stop' else 'started'}

    def table(self, **handlers):
        table = table_with(self.store, **handlers)
        return table, composed(table, self.built_in)

    def test_a_command_cannot_reach_a_mutating_built_in_directly_or_through_a_read(self) -> None:
        opened = []

        def status(ctx, params):
            opened.append(('read', table.command_open()))
            return {'code': 'ok', 'stop': ctx.call('run.stop', {'run_id': 'run-1'}), 'list': ctx.call('run.list')}

        def turn(ctx, params):
            opened.append(('command', table.command_open()))
            return {'code': 'ok', 'direct': ctx.call('run.stop', {'run_id': 'run-1'}),
                    'start': ctx.call('run.start', {'tenant': 't1', 'engine': 'codex', 'prompt': 'p'}),
                    'through': ctx.call('session.status', {}), 'listed': ctx.call('run.list')}
        table, call = self.table(**{'session.turn': turn, 'session.status': status})
        answer = table.dispatch('session.turn', dict(TURN), H.OWNER, call)
        refused = {'code': 'request_invalid', 'detail': 'nested_effect'}
        self.assertEqual((answer['direct'], answer['start'], answer['through']['stop']), (refused, refused, refused))
        self.assertEqual((answer['listed']['code'], answer['through']['list']['code']), ('ok', 'ok'), 'a read that changes nothing is not an effect')
        self.assertEqual(self.asked, ['run.list', 'run.list'], 'no Stop and no start was made while the transaction was open')
        self.assertEqual(opened, [('command', True), ('read', True)])
        self.assertFalse(table.command_open())
        # The same read outside a command is not inside one: the Stop goes through.
        del self.asked[:], opened[:]
        self.assertEqual(table.dispatch('session.status', {}, H.OWNER, call)['stop'], {'code': 'stopped'})
        self.assertEqual((self.asked, opened), (['run.stop', 'run.list'], [('read', False)]))

    def test_the_callbacks_of_nested_reads_wait_for_the_commands_commit_and_keep_their_order(self) -> None:
        ran, during = [], []

        def noting(name):
            return lambda: ran.append((name, self.store.commits, self.store.open_transactions, len(table._slots)))

        def status(ctx, params):
            self.assertIsNone(ctx.tx)
            ctx.after_commit(noting('status'))
            return {'code': 'ok'}

        def listing(ctx, params):
            ctx.after_commit(noting('list-1'))
            ctx.after_commit(noting('list-2'))
            return {'code': 'ok', 'sessions': []}

        def turn(ctx, params):
            ctx.after_commit(noting('own-1'))
            ctx.call('session.status', {})
            ctx.after_commit(noting('own-2'))
            ctx.call('session.list', {})
            ctx.after_commit(noting('own-3'))
            during.extend(ran)
            return {'code': 'ok'}
        table, call = self.table(**{'session.turn': turn, 'session.status': status, 'session.list': listing})
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, call), {'code': 'ok'})
        self.assertEqual(during, [], 'nothing ran while the command\'s transaction was open')
        self.assertEqual(ran, [(name, 1, 0, 0) for name in ('own-1', 'status', 'own-2', 'list-1', 'list-2', 'own-3')],
                         'in the order they were registered, after the commit, outside the transaction and the slot')

    @quiet
    def test_a_command_that_does_not_commit_discards_its_nested_reads_callbacks(self) -> None:
        ran = []

        def status(ctx, params):
            ctx.after_commit(lambda: ran.append('nested'))
            return {'code': 'ok'}

        def raising(ctx, params):
            ctx.call('session.status', {})
            raise RuntimeError('half way')

        def refusing(ctx, params):
            ctx.call('session.status', {})
            raise methods.NotAccepted({'code': 'busy'})

        def accepting(ctx, params):
            ctx.call('session.status', {})
            return {'code': 'ok'}
        for handler, expected, fail_commit in ((raising, {'code': 'unavailable', 'detail': 'handler:RuntimeError'}, False),
                                               (refusing, {'code': 'busy'}, False),
                                               (accepting, {'code': 'unavailable', 'detail': 'store:OSError'}, True)):
            with self.subTest(handler=handler.__name__):
                self.store = work_store()
                self.store.fail_commit = fail_commit
                table, call = self.table(**{'session.turn': handler, 'session.status': status})
                self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, call), expected)
                self.assertEqual((ran, self.store.receipts), ([], {}))

    @quiet
    def test_a_nested_read_that_fails_contributes_no_callback(self) -> None:
        ran = []

        def broken(ctx, params):
            ctx.after_commit(lambda: ran.append('from the read that failed'))
            raise RuntimeError('no')

        def declining(ctx, params):
            ctx.after_commit(lambda: ran.append('from the read that declined'))
            raise methods.NotAccepted({'code': 'busy'})

        def turn(ctx, params):
            ctx.after_commit(lambda: ran.append('own-1'))
            answers = [ctx.call('session.status', {}), ctx.call('session.list', {})]
            ctx.after_commit(lambda: ran.append('own-2'))
            return {'code': 'ok', 'nested': answers}
        table, call = self.table(**{'session.turn': turn, 'session.status': broken, 'session.list': declining})
        answer = table.dispatch('session.turn', dict(TURN), H.OWNER, call)
        self.assertEqual(answer['nested'], [{'code': 'unavailable', 'detail': 'handler:RuntimeError'}, {'code': 'busy'}])
        self.assertEqual(ran, ['own-1', 'own-2'])

    def test_a_nested_read_has_no_transaction_and_cannot_open_a_second_one(self) -> None:
        seen = []

        def status(ctx, params):
            try:
                with self.store.transaction():
                    seen.append('opened a second transaction')
            except RuntimeError as exc:
                seen.append((ctx.tx, str(exc)))
            return {'code': 'ok'}

        def turn(ctx, params):
            ctx.call('session.status', {})
            ctx.tx.execute('INSERT INTO work VALUES (?, ?, ?)', (params['command_id'], 'op-1', 'pending'))
            return {'code': 'ok'}
        table, call = self.table(**{'session.turn': turn, 'session.status': status})
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, call), {'code': 'ok'})
        self.assertEqual(seen, [(None, 'nested_transaction')])
        self.assertEqual((self.store.commits, len(self.store.rows)), (1, 1), 'the command\'s own transaction was not disturbed')

    def test_outside_a_command_a_reads_callbacks_run_when_its_handler_returns(self) -> None:
        ran = []

        def listing(ctx, params):
            ctx.after_commit(lambda: ran.append('inner'))
            return {'code': 'ok', 'sessions': []}

        def status(ctx, params):
            ctx.after_commit(lambda: ran.append('outer'))
            ctx.call('session.list', {})
            ran.append('outer handler goes on')
            return {'code': 'ok'}
        table, call = self.table(**{'session.status': status, 'session.list': listing})
        table.dispatch('session.status', {}, H.OWNER, call)
        self.assertEqual(ran, ['inner', 'outer handler goes on', 'outer'])

    def test_a_call_made_from_a_callback_is_after_the_commit_and_one_kept_longer_is_refused(self) -> None:
        kept, from_callback = [], []

        def turn(ctx, params):
            kept.append(ctx)
            ctx.after_commit(lambda: from_callback.append((self.store.commits, ctx.call('run.stop', {'run_id': 'run-1'}))))
            return {'code': 'ok'}
        table, call = self.table(**{'session.turn': turn})
        table.dispatch('session.turn', dict(TURN), H.OWNER, call)
        self.assertEqual(from_callback, [(1, {'code': 'stopped'})], 'after the commit the transaction no longer restricts the call')
        self.assertEqual(kept[0].call('run.list'), {'code': 'request_invalid', 'detail': 'dispatch_ended'})
        self.assertEqual(self.asked, ['run.stop'], 'the call kept past its dispatch reached nothing')

    def test_a_nested_call_made_from_another_thread_is_still_inside_the_command(self) -> None:
        answers, ran = [], []

        def status(ctx, params):
            ctx.after_commit(lambda: ran.append(self.store.commits))
            return {'code': 'ok', 'stop': ctx.call('run.stop', {'run_id': 'run-1'}), 'inside': table.command_open()}

        def turn(ctx, params):
            def elsewhere():
                answers.append(ctx.call('run.stop', {'run_id': 'run-1'}))
                answers.append(ctx.call('session.status', {}))      # a read reached from the other thread
            thread = threading.Thread(target=elsewhere, daemon=True)
            thread.start()
            thread.join(BOUND_S)
            ran.append('the handler goes on')
            return {'code': 'ok'}
        table, call = self.table(**{'session.turn': turn, 'session.status': status})
        table.dispatch('session.turn', dict(TURN), H.OWNER, call)
        refused = {'code': 'request_invalid', 'detail': 'nested_effect'}
        self.assertEqual(answers, [refused, {'code': 'ok', 'stop': refused, 'inside': True}],
                         'the command\'s restriction went with the call to the other thread, and on through the read')
        self.assertEqual((self.asked, ran), ([], ['the handler goes on', 1]), 'and that read\'s callback waited for the commit')


class CallbackOwnershipTest(unittest.TestCase):
    """The second reads' B1: a callback belongs to the dispatch that registered it, not to a place in a list."""

    def setUp(self) -> None:
        self.store = work_store()

    def table(self, **handlers):
        table = table_with(self.store, **handlers)
        return table, composed(table, lambda method, params: {'code': 'ok'})

    @quiet
    def test_a_read_that_fails_discards_its_own_callbacks_and_leaves_its_siblings(self) -> None:
        """Read A is made on another thread: it registers a callback and waits. Read B, made by the command
        itself, registers its own and returns. Then A fails. The command joins A and commits. B's callback
        runs, and the command's own on both sides of it; A's does not. (A used to cut the shared list from
        the place it had found it at, and took B's with it.)"""
        for failure in (ValueError('A fails after B registered'), methods.NotAccepted({'code': 'busy'})):
            ran, a_registered, a_may_fail = [], threading.Event(), threading.Event()
            self.addCleanup(a_may_fail.set)

            def failing(ctx, params, failure=failure, ran=ran, a_registered=a_registered, a_may_fail=a_may_fail):
                ctx.after_commit(lambda: ran.append('A'))
                a_registered.set()
                a_may_fail.wait(BOUND_S)
                raise failure

            def succeeding(ctx, params, ran=ran):
                ctx.after_commit(lambda: ran.append('B'))
                return {'code': 'ok', 'sessions': []}

            def turn(ctx, params, ran=ran, a_registered=a_registered, a_may_fail=a_may_fail):
                answers = {}
                ctx.after_commit(lambda: ran.append('own-1'))
                thread = threading.Thread(target=lambda: answers.update(a=ctx.call('session.status', {})), daemon=True)
                thread.start()
                self.assertTrue(a_registered.wait(BOUND_S))
                answers['b'] = ctx.call('session.list', {})
                ctx.after_commit(lambda: ran.append('own-2'))
                a_may_fail.set()
                thread.join(BOUND_S)
                return {'code': 'ok', 'a': answers['a']['code'], 'b': answers['b']['code']}
            with self.subTest(failure=type(failure).__name__):
                self.store = work_store()
                table, call = self.table(**{'session.turn': turn, 'session.status': failing, 'session.list': succeeding})
                answer = table.dispatch('session.turn', dict(TURN), H.OWNER, call)
                self.assertEqual((answer['code'], answer['b']), ('ok', 'ok'))
                self.assertIn(answer['a'], ('unavailable', 'busy'))
                self.assertEqual((self.store.commits, ran), (1, ['own-1', 'B', 'own-2']))

    def test_a_read_that_leaves_by_an_interruption_contributes_no_callback(self) -> None:
        ran = []

        def interrupted(ctx, params):
            ctx.after_commit(lambda: ran.append('the interrupted read'))
            raise KeyboardInterrupt

        def turn(ctx, params):
            try:
                ctx.call('session.status', {})
            except KeyboardInterrupt:
                pass                    # the command's handler catches it and goes on to commit
            ctx.after_commit(lambda: ran.append('own'))
            return {'code': 'ok'}
        table, call = self.table(**{'session.turn': turn, 'session.status': interrupted})
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, call), {'code': 'ok'})
        self.assertEqual((self.store.commits, ran), (1, ['own']))

    @quiet
    def test_what_a_failed_read_called_is_discarded_with_it(self) -> None:
        """A calls a read that succeeds and registers; then A fails. The descendant's callback goes with A's,
        and a sibling of A keeps its own."""
        ran = []

        def descendant(ctx, params):
            ctx.after_commit(lambda: ran.append('descendant of A'))
            return {'code': 'ok', 'sessions': []}

        def failing(ctx, params):
            ctx.after_commit(lambda: ran.append('A'))
            self.assertEqual(ctx.call('session.list', {})['code'], 'ok')
            raise ValueError('A fails after its descendant succeeded')

        def sibling(ctx, params):
            ctx.after_commit(lambda: ran.append('sibling'))
            return {'code': 'ok'}

        def turn(ctx, params):
            self.assertEqual(ctx.call('session.status', {})['code'], 'unavailable')
            self.assertEqual(ctx.call('session.export', {})['code'], 'ok')
            self.assertEqual(ctx.call('session.list', {})['code'], 'ok')      # the same read, called by the command: kept
            return {'code': 'ok'}
        table, call = self.table(**{'session.turn': turn, 'session.status': failing, 'session.list': descendant,
                                    'session.export': sibling})
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, call), {'code': 'ok'})
        self.assertEqual(ran, ['sibling', 'descendant of A'], 'only the one the command itself called')

    def test_after_commit_belongs_to_a_running_handler(self) -> None:
        """Registering from a callback, or through a context kept after its handler returned, is rejected: it
        is not accepted and then never run."""
        for method, params in (('session.turn', dict(TURN)), ('session.status', {})):
            kept, ran, refused = [], [], []

            def handler(ctx, params, kept=kept, ran=ran, refused=refused):
                kept.append(ctx)

                def callback():
                    ran.append('callback')
                    try:
                        ctx.after_commit(lambda: ran.append('registered from a callback'))
                    except RuntimeError as exc:
                        refused.append(str(exc))
                ctx.after_commit(callback)
                return {'code': 'ok'}
            with self.subTest(method=method):
                table, call = self.table(**{method: handler})
                self.assertEqual(table.dispatch(method, params, H.OWNER, call), {'code': 'ok'})
                self.assertEqual((ran, len(refused)), (['callback'], 1))
                self.assertIn('running handler', refused[0])
                with self.assertRaises(RuntimeError):
                    kept[0].after_commit(lambda: ran.append('after the handler returned'))
                self.assertEqual(ran, ['callback'])

    @quiet
    def test_a_registration_that_arrives_after_the_command_finished_is_refused_and_not_lost_in_silence(self) -> None:
        """What the contract forbids: a handler returns without joining a thread it made a nested call on. That
        read's registration arrives after the command has committed. It is refused, loudly."""
        entered, go, outcome, ran, threads = threading.Event(), threading.Event(), [], [], []
        self.addCleanup(go.set)

        def slow(ctx, params):
            entered.set()
            go.wait(BOUND_S)
            try:
                ctx.after_commit(lambda: ran.append('late'))
                outcome.append('accepted')
            except RuntimeError:
                outcome.append('refused')
                raise
            return {'code': 'ok'}

        def turn(ctx, params):
            threads.append(threading.Thread(target=lambda: ctx.call('session.status', {}), daemon=True))
            threads[0].start()
            self.assertTrue(entered.wait(BOUND_S))
            return {'code': 'ok'}                # without joining it
        table, call = self.table(**{'session.turn': turn, 'session.status': slow})
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, call), {'code': 'ok'})
        go.set()
        threads[0].join(BOUND_S)
        self.assertEqual((outcome, ran, self.store.commits), (['refused'], [], 1))


class OwnedAnswerTest(unittest.TestCase):
    """R5: the committed response is an owned snapshot."""

    @quiet
    def test_a_callback_that_changes_the_handlers_answer_changes_no_ones_answer(self) -> None:
        store = work_store()

        def turn(ctx, params):
            answer = {'code': 'ok', 'revision': 1, 'items': ['a']}

            def spoil():
                answer['revision'] = 2
                answer['items'].append('b')
                raise RuntimeError('and then fails')
            ctx.after_commit(spoil)
            return answer
        table = table_with(store, **{'session.turn': turn})
        expected = {'code': 'ok', 'revision': 1, 'items': ['a']}
        first = table.dispatch('session.turn', dict(TURN), H.OWNER, None)
        self.assertEqual(first, expected, 'the first caller gets what was committed')
        self.assertEqual(store.receipts[('install', 'session.turn', 'cmd-1')][1], expected)
        first['items'].append('changed by the first caller')
        replay = table.dispatch('session.turn', dict(TURN), H.OWNER, None)
        self.assertEqual(replay, expected)
        replay['revision'] = 9
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, None), expected)

    def test_a_refusal_and_a_read_with_a_callback_are_owned_too(self) -> None:
        refusal = {'code': 'busy', 'why': ['turn in flight']}

        def turn(ctx, params):
            raise methods.NotAccepted(refusal)

        def status(ctx, params):
            answer = {'code': 'ok', 'items': ['a']}
            ctx.after_commit(lambda: answer['items'].append('b'))
            return answer
        table = table_with(work_store(), **{'session.turn': turn, 'session.status': status})
        got = table.dispatch('session.turn', dict(TURN), H.OWNER, None)
        got['why'].append('changed by the caller')
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, None), {'code': 'busy', 'why': ['turn in flight']})
        self.assertEqual(table.dispatch('session.status', {}, H.OWNER, None), {'code': 'ok', 'items': ['a']})


class ReceiptReadTest(unittest.TestCase):
    """A replay is the caller's own copy whatever the store hands back, the store's own object included. (The
    fake decodes a fresh answer at every read, so it cannot show this; a store that keeps one object can.)"""

    def test_a_replay_from_a_store_that_hands_out_its_own_object_cannot_be_changed_through_it(self) -> None:
        kept = {'code': 'ok', 'items': ['first']}
        digest = methods.payload_digest(TURN)

        class Tx:
            def receipt_get(self, tenant, method, command_id):
                return digest, kept              # the very object, every time

            def receipt_put(self, *args):
                raise AssertionError('a replay stores nothing')

        class Store:
            @contextlib.contextmanager
            def transaction(self):
                yield Tx()
        table = table_with(Store(), **{'session.turn': lambda ctx, params: self.fail('a replay calls no handler')})
        first = table.dispatch('session.turn', dict(TURN), H.OWNER, None)
        first['items'].append('changed by the first caller')
        first['code'] = 'changed'
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, None), {'code': 'ok', 'items': ['first']})
        self.assertEqual(kept, {'code': 'ok', 'items': ['first']})

    def test_a_refusal_from_the_tenant_function_is_the_callers_own_copy(self) -> None:
        refusal = methods.NotAccepted({'code': 'session_unknown', 'session_id': 'ses-1'})

        def tenant_of(principal, params):
            raise refusal
        table = table_with(work_store(), tenant=tenant_of, **{'session.turn': lambda ctx, params: {'code': 'ok'}})
        first = table.dispatch('session.turn', dict(TURN), H.OWNER, None)
        first['code'] = 'changed by the caller'
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, None), {'code': 'session_unknown', 'session_id': 'ses-1'})


class NumberRuleTest(unittest.TestCase):
    """R10: the receipt's numbers are judged by value, as the approval records' are."""

    def test_an_integral_value_outside_the_exact_range_has_no_text_however_it_is_spelled(self) -> None:
        for value in (2**53, 2.0**53, 9007199254740992.0, -9007199254740992.0, 1e16, 1e20, 1e21, 2.0**60, 1.7976931348623157e308,
                      -1e300):
            with self.subTest(value=value):
                with self.assertRaises(methods.NoCanonicalText):
                    methods.canonical_text(value)
                with self.assertRaises(ValueError):
                    methods.payload_digest({'command_id': 'c', 'n': value})

    def test_every_text_that_is_written_reads_back_to_the_same_text(self) -> None:
        # Closed under its own text: whatever the helper admits, the text it writes is admitted again and is the
        # same text. The candidates include the spellings it must refuse; a refused one has no text to read back.
        admitted = []
        for value in (0, -0.0, 1, 1.0, -1.0, 100.5, 1e-7, 5e-324, 0.1, 1e15, 123456789.125, 4503599627370496.5,
                      2**53 - 1, 9007199254740991.0, -9007199254740991.0, [1.0, {'n': 2.50}],
                      9007199254740992.0, -9007199254740992.0, 2**53, 1e16, 1e20, 1e21, 1.5e300, [1, {'n': 2.0**60}]):
            with self.subTest(value=value):
                try:
                    text = methods.canonical_text(value)
                except methods.NoCanonicalText:
                    continue
                admitted.append(value)
                self.assertEqual(methods.canonical_text(json.loads(text)), text)
        self.assertEqual(len(admitted), 16, 'the sixteen values inside the exact range, and no other')
        for whole in (1.0, 1e15, 9007199254740991.0, -0.0):
            self.assertIs(type(json.loads(methods.canonical_text(whole))), int, 'an integral value is written as a whole number')
        self.assertEqual(methods.payload_digest({'n': 1.0}), methods.payload_digest({'n': 1}))

    def test_a_command_whose_params_hold_such_a_number_is_refused_before_the_handler(self) -> None:
        handlers = Handlers()
        table = table_with(work_store(), **{'session.turn': handlers.turn})
        for value in (9007199254740992.0, 1e20):
            self.assertEqual(table.dispatch('session.turn', dict(TURN, budget=value), H.OWNER, None),
                             {'code': 'request_invalid', 'detail': 'params'})
        self.assertEqual(handlers.calls, [])


class PublishedRowTest(unittest.TestCase):
    """R9: a method's service is its published row's, and a registration cannot name another."""

    def test_only_the_two_reads_of_a_log_name_a_service_and_no_other_row_can_be_registered_with_one(self) -> None:
        self.assertEqual({name: row.service for name, row in methods.METHODS.items() if row.service is not None},
                         {'events.read': 'events', 'events.follow': 'events'}, 'the published table, draft 9')
        self.assertEqual(methods.Row._fields, ('scope', 'mutating', 'idempotency', 'beta', 'service'))
        table = methods.MethodTable()
        with self.assertRaises(methods.MethodTableError) as caught:
            table.register('session.list', lambda ctx, params: {'code': 'ok'}, service='not-in-published-rows')
        self.assertIn('published row', str(caught.exception))
        self.assertEqual(table.registered(), ())
        # The real rows: a read of a log is registered with the service its row names, and with no other.
        for service in (None, 'sessions', ''):
            with self.subTest(service=service):
                with self.assertRaises(methods.MethodTableError):
                    methods.MethodTable().register('events.follow', lambda ctx, params: {'code': 'ok'}, service=service)
        table.register('events.follow', lambda ctx, params: {'code': 'ok'}, service='events')
        table.register('events.read', lambda ctx, params: {'code': 'ok'}, service='events')
        self.assertEqual(table.services_needed(), {'events.follow': 'events', 'events.read': 'events'})
        table.service_ready = lambda name: False
        self.assertEqual(table.dispatch('events.read', {}, H.OWNER, None), {'code': 'unavailable', 'detail': 'service_starting'})
        table.service_ready = lambda name: name == 'events'
        self.assertEqual(table.dispatch('events.read', {}, H.OWNER, None), {'code': 'ok'})

    def test_a_row_that_names_a_service_is_registered_with_that_service_and_no_other(self) -> None:
        row_names_service(self, 'session.list', 'sessions')
        for service in (None, 'events', ''):
            with self.subTest(service=service):
                with self.assertRaises(methods.MethodTableError):
                    methods.MethodTable().register('session.list', lambda ctx, params: {'code': 'ok'}, service=service)
        table = methods.MethodTable()
        table.register('session.list', lambda ctx, params: {'code': 'ok'}, service='sessions')
        self.assertEqual(table.services_needed(), {'session.list': 'sessions'})


class FamilyParsersTest(unittest.TestCase):
    """R8: a family adds its own names through a facade and reaches no other parser."""

    def parser(self):
        parser = argparse.ArgumentParser(prog='recursion-control')
        sub = parser.add_subparsers(dest='command', required=True)
        start = sub.add_parser('start')
        start.add_argument('--tenant', required=True)
        start.set_defaults(depth=1)
        return parser, sub

    def test_a_family_cannot_change_another_commands_defaults_arguments_or_help(self) -> None:
        handed = []
        for reach in (lambda sub: sub.choices['start'].set_defaults(depth=999),
                      lambda sub: sub._name_parser_map['start'].add_argument('--workspace'),
                      lambda sub: sub._target.choices['start'].set_defaults(depth=999) if not hasattr(sub, '_target') else sub.choices):
            def add(sub, reach=reach):
                handed.append(type(sub).__name__)
                sub.add_parser('workspace')
                reach(sub)
            table = commands.CommandTable()
            table.register(('workspace',), add, lambda args, client: 0)
            parser, sub = self.parser()
            with self.assertRaises(commands.CommandTableError):
                table.add_parsers(sub)
            self.assertEqual(parser.parse_args(['start', '--tenant', 't']).depth, 1, 'the other command parses as it did')
        self.assertEqual(set(handed), {'FamilyParsers'})
        self.assertFalse(any(hasattr(commands.FamilyParsers(None, ()), name) for name in ('choices', 'add_argument', 'set_defaults')))

    def test_a_family_adds_each_declared_name_once_with_argparses_own_options(self) -> None:
        def add(sub):
            session = sub.add_parser('session', aliases=['s'], help='one session')
            session.add_argument('session_id')
            session.set_defaults(depth=7)          # its own command is its own to shape
        table = commands.CommandTable()
        table.register(('session', 's'), add, lambda args, client: 0)
        parser, sub = self.parser()
        table.add_parsers(sub)
        for name in ('session', 's'):
            args = parser.parse_args([name, 'ses-1'])
            self.assertEqual((args.command, args.session_id, args.depth), (name, 'ses-1', 7))
        self.assertIn('one session', parser.format_help())
        for names, add in ((('session',), lambda sub: sub.add_parser('session', aliases=['s'])),           # an alias nobody declared
                           (('session',), lambda sub: (sub.add_parser('session'), sub.add_parser('session'))),  # one name twice
                           (('session', 's'), lambda sub: (sub.add_parser('session', aliases=['s']), sub.add_parser('s'))),
                           (('session',), lambda sub: sub.add_parser('start')),                            # another's name
                           (('session', 'queue'), lambda sub: sub.add_parser('session'))):                 # one left out
            with self.subTest(names=names):
                table = commands.CommandTable()
                table.register(names, add, lambda args, client: 0)
                parser, sub = self.parser()
                with self.assertRaises(commands.CommandTableError):
                    table.add_parsers(sub)
        # A claim that is refused adds nothing: the undeclared alias was not made, nor the parser that carried it.
        table = commands.CommandTable()
        table.register(('session',), lambda sub: sub.add_parser('session', aliases=['s']), lambda args, client: 0)
        parser, sub = self.parser()
        with self.assertRaises(commands.CommandTableError):
            table.add_parsers(sub)
        self.assertEqual(sorted(sub.choices), ['start'])


class IntentMembersTest(unittest.TestCase):
    """R6: the server's members of a start's identity cannot be declared, and they win."""

    def test_the_servers_members_cannot_be_declared_as_extension_fields(self) -> None:
        for field in ('prompt_sha256', 'intent_version', 'intent_sha256', 'prompt', 'engine'):
            with self.subTest(field=field):
                with self.assertRaises(H.HookError):
                    H.Hooks().start_registrant('workspace', (field,), (), lambda principal, request: {})

    def test_the_prompts_hash_is_the_servers_whatever_a_registrant_declared(self) -> None:
        made = H.Hooks()
        made.start_registrant('workspace', ('workspace',), ('workspace',), lambda principal, request: {})
        # A registrant that got such a field past registration, by whatever fault: the server's members still win.
        made._start['faulty'] = (('prompt_sha256', 'engine'), (), lambda principal, request: {}, False)
        made.freeze()
        params = {'engine': 'codex', 'prompt_sha256': 'extension-value', 'workspace': {'path': '/a'}}
        one, other = made.start_intent(params, 'a' * 64), made.start_intent(params, 'b' * 64)
        self.assertNotEqual(one, other, 'another prompt is another request')
        self.assertEqual(made.start_intent_text(params, 'a' * 64),
                         '{"engine":"codex","prompt_sha256":"%s","workspace":{"path":"/a"}}' % ('a' * 64))
        self.assertNotIn('extension-value', made.start_intent_text(params, 'a' * 64))


class WatchedLock:
    """The lifecycle's lock, which says when a thread asks for it while another holds it, and can be made to
    interrupt the next thread that asks (once), before or after that thread has taken it."""

    def __init__(self) -> None:
        self._inner, self.contended = threading.RLock(), threading.Event()
        self._held = threading.Event()
        self.interrupt_next = None        # 'before' or 'after': raise KeyboardInterrupt in the next acquire, once

    def acquire(self, *args, **kwargs):
        if self._held.is_set() and not self._inner._is_owned():
            self.contended.set()
        when, self.interrupt_next = self.interrupt_next, None
        if when == 'before':
            raise KeyboardInterrupt
        taken = self._inner.acquire(*args, **kwargs)
        self._held.set()
        if when == 'after':
            raise KeyboardInterrupt       # it is held: the interruption arrived on the way back
        return taken

    def release(self) -> None:
        self._held.clear()
        self._inner.release()

    def _is_owned(self) -> bool:
        return self._inner._is_owned()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


class WorkerService:
    """A service with one worker that uses the store until the service's stop tells it to end."""

    def __init__(self, name, store, *, fail_start=False, fail_stops=0, failure=RuntimeError) -> None:
        self.name, self.ready, self.store = name, False, store
        self.stop_calls, self.used_a_closed_store, self.store_closed_at_stop = 0, False, []
        self._fail_start, self._fail_stops, self._failure = fail_start, fail_stops, failure
        self._quit, self._thread = threading.Event(), None
        self.in_stop, self.go_on = threading.Event(), None     # set ``go_on`` to an Event to hold a stop inside

    def start(self, context) -> None:
        self._thread = threading.Thread(target=self._work, daemon=True)
        self._thread.start()
        if self._fail_start:
            raise RuntimeError(f'{self.name} failed after it started its worker')
        self.ready = True

    def _work(self) -> None:
        while not self._quit.wait(0.001):
            if self.store.closed:
                self.used_a_closed_store = True

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self, context) -> None:
        self.stop_calls += 1
        self.store_closed_at_stop.append(self.store.closed)
        if self._fail_stops:
            self._fail_stops -= 1
            raise self._failure(f'{self.name} could not stop')
        self.in_stop.set()
        if self.go_on is not None:
            self.go_on.wait(BOUND_S)
        self._quit.set()
        if self._thread is not None:
            self._thread.join(BOUND_S)
            if self._thread.is_alive():
                raise AssertionError('the test worker did not stop within BOUND_S')


class LifecycleTest(unittest.TestCase):
    """R3: the teardown owns every service until its stop has returned, and has one owner."""

    def made(self, *services_of, serial_lock=False):
        store = FakeStore()
        services = [make(store) for make in services_of]
        alive_at_close, close = [], store.close

        def closing():
            alive_at_close.append([service.name for service in services if service.alive()])
            close()
        store.close = closing
        made = H.Hooks()
        for service in services:
            made.service(service)
        made.set('event_store', lambda state_dir: store)
        made.freeze()
        made.stop_retry_s = 0
        if serial_lock:
            made._lifecycle = WatchedLock()
        self.addCleanup(lambda: [service._quit.set() for service in services])
        return made, store, services, alive_at_close

    @quiet
    def test_a_service_that_fails_after_starting_a_worker_is_stopped_before_the_store_closes(self) -> None:
        made, store, (events, sessions), alive_at_close = self.made(
            lambda store: WorkerService('events', store), lambda store: WorkerService('sessions', store, fail_start=True))
        context = H.ServiceContext()
        made.open_store('/state')
        with self.assertRaises(RuntimeError):
            made.start_services(context)
        self.assertEqual((sessions.stop_calls, events.stop_calls), (1, 1), 'the one that failed part way was stopped too')
        self.assertFalse(sessions.alive() or events.alive())
        made.finish_close(context)
        self.assertEqual((alive_at_close, store.closed), ([[]], True))
        self.assertFalse(sessions.used_a_closed_store or events.used_a_closed_store)

    @quiet
    def test_a_stop_that_fails_is_not_a_stop_and_the_store_stays_open_until_one_returns(self) -> None:
        made, store, (events,), alive_at_close = self.made(lambda store: WorkerService('events', store, fail_stops=3))
        context = H.ServiceContext()
        made.open_store('/state')
        made.start_services(context)
        with self.assertRaises(RuntimeError) as caught:
            made.finish_close(context)
        self.assertIn('could not stop', str(caught.exception), 'the first failure is raised, after the teardown')
        self.assertEqual((events.stop_calls, events.store_closed_at_stop), (4, [False] * 4),
                         'its stop was tried until it returned, with the store open each time')
        self.assertEqual((alive_at_close, store.closed, events.used_a_closed_store), ([[]], True, False))

    @quiet
    def test_an_interruption_of_a_stop_does_not_end_the_teardown(self) -> None:
        made, store, (events,), alive_at_close = self.made(
            lambda store: WorkerService('events', store, fail_stops=1, failure=KeyboardInterrupt))
        context = H.ServiceContext()
        made.open_store('/state')
        made.start_services(context)
        with self.assertRaises(KeyboardInterrupt):
            made.finish_close(context)
        self.assertEqual((events.stop_calls, alive_at_close, store.closed), (2, [[]], True))

    @quiet
    def test_stopping_the_services_keeps_an_interruption_and_stops_every_one_before_it_raises(self) -> None:
        """``stop_services`` on its own, with no close around it that would come back. The newest service's stop
        fails once in the ordinary way; the older one's is interrupted once. When the call ends every service
        has stopped, and what it raises is the interruption, although the ordinary failure came first."""
        made, store, (events, sessions), _ = self.made(
            lambda store: WorkerService('events', store, fail_stops=1, failure=KeyboardInterrupt),
            lambda store: WorkerService('sessions', store, fail_stops=1))
        context = H.ServiceContext()
        made.open_store('/state')
        made.start_services(context)
        with self.assertRaises(KeyboardInterrupt):
            made.stop_services(context)
        self.assertEqual((sessions.stop_calls, events.stop_calls, sessions.alive(), events.alive(), made._started),
                         (2, 2, False, False, []))
        self.assertFalse(store.closed, 'stopping the services closes no store')
        made.finish_close(context)

    @quiet
    def test_an_interruption_of_the_wait_for_admitted_calls_does_not_end_the_wait(self) -> None:
        made, store, (events,), alive_at_close = self.made(lambda store: WorkerService('events', store))
        context = H.ServiceContext()
        made.open_store('/state')
        made.start_services(context)
        self.assertTrue(made.gate.enter())                 # a call is inside
        waits, wait = [], made.gate._changed.wait
        # No schedule decides this test. The close moves on in one of two ways after the interruption: it waits
        # again (right), or it begins to tear down (wrong). Either sets ``moved_on``, and what the teardown saw
        # is written down at the moment it begins, so a wrong close is caught however the threads are scheduled.
        moved_on, admitted_at_teardown, stop = threading.Event(), [], events.stop

        def interrupted_once(timeout=None):
            waits.append(timeout)
            if len(waits) == 1:
                raise KeyboardInterrupt
            moved_on.set()
            return wait(timeout)

        def stopping(context):
            admitted_at_teardown.append(made.gate.admitted)
            moved_on.set()
            return stop(context)
        made.gate._changed.wait = interrupted_once
        events.stop = stopping
        outcome = []

        def close():
            try:
                made.finish_close(context)
                outcome.append('returned')
            except KeyboardInterrupt:
                outcome.append('interrupted')
        closer = threading.Thread(target=close, daemon=True)
        closer.start()
        self.assertTrue(moved_on.wait(BOUND_S), 'the close went on after the interruption, one way or the other')
        self.assertEqual((admitted_at_teardown, store.closed, outcome), ([], False, []),
                         'it went on WAITING: nothing is torn down and the close has not ended while the call is inside')
        made.gate.leave()
        closer.join(BOUND_S)
        self.assertEqual((outcome, events.stop_calls, alive_at_close, store.closed), (['interrupted'], 1, [[]], True),
                         'it is raised once everything has been torn down')
        self.assertEqual(admitted_at_teardown, [0], 'and the teardown began only when no call was inside')

    def test_a_second_close_waits_for_the_first_and_closes_nothing_itself(self) -> None:
        made, store, (events,), alive_at_close = self.made(lambda store: WorkerService('events', store), serial_lock=True)
        context = H.ServiceContext()
        made.open_store('/state')
        made.start_services(context)
        events.go_on = threading.Event()
        self.addCleanup(events.go_on.set)
        done = []
        # What each closer finds at the moment its own call returns is written down then, so no schedule decides
        # the test: a second close that returned early would say so however the threads are run.
        first = threading.Thread(target=lambda: (made.finish_close(context), done.append(('first', store.closed))), daemon=True)
        first.start()
        self.assertTrue(events.in_stop.wait(BOUND_S), 'the first close is inside the service\'s stop')
        second = threading.Thread(target=lambda: (made.finish_close(context), done.append(('second', store.closed, events.alive()))),
                                  daemon=True)
        second.start()
        self.assertTrue(made._lifecycle.contended.wait(BOUND_S), 'the second close has arrived')
        self.assertEqual((store.closed, events.alive()), (False, True), 'nothing is closed under the first')
        events.go_on.set()
        first.join(BOUND_S)
        second.join(BOUND_S)
        self.assertEqual((sorted(done), events.stop_calls, alive_at_close),
                         ([('first', True), ('second', True, False)], 1, [[]]),
                         'the second returned only when the teardown was complete; the service was stopped once and '
                         'the store closed once')
        made.finish_close(context)       # and a close after it returns at once
        self.assertEqual(len(alive_at_close), 1)

    def test_once_closing_has_begun_nothing_is_opened_or_started(self) -> None:
        made, store, (events,), _ = self.made(lambda store: WorkerService('events', store))
        context = H.ServiceContext()
        made.begin_close()
        with self.assertRaises(H.HookError):
            made.open_store('/state')
        with self.assertRaises(H.HookError):
            made.start_services(context)
        self.assertEqual((made.store, events.alive(), made.service_ready('events')), (None, False, False))

    @quiet
    def test_closing_that_begins_while_services_start_stops_the_ones_that_did_and_starts_no_more(self) -> None:
        log = []

        class Closes(Service):
            def start(inner, context):
                super().start(context)
                made.begin_close()
        made = H.Hooks()
        made.service(Closes('events', log))
        made.service(Service('sessions', log))
        made.freeze()
        with self.assertRaises(H.HookError):
            made.start_services(H.ServiceContext())
        self.assertEqual([(what, name) for what, name, _ in log], [('start', 'events'), ('stop', 'events')])


    @quiet
    def test_an_interruption_while_the_drain_takes_its_condition_does_not_end_the_wait(self) -> None:
        """The second reads' witness. The drain is interrupted as it TAKES the admission condition, not while it
        waits on it, with one call inside. The close goes on waiting: nothing is stopped and the store stays
        open until the call has left. (The close used to take the interruption as the end of the drain.)"""
        made, store, (events,), alive_at_close = self.made(lambda store: WorkerService('events', store))
        context = H.ServiceContext()
        made.open_store('/state')
        made.start_services(context)
        self.assertTrue(made.gate.enter())                 # a call is inside
        condition, drain, stop = made.gate._changed, made.gate.drain, events.stop
        moved_on, admitted_at_teardown, state = threading.Event(), [], {'draining': False, 'taken': 0}

        class InterruptedOnce:
            def __enter__(self):
                if state['draining']:
                    state['taken'] += 1
                    if state['taken'] == 1:
                        raise KeyboardInterrupt        # as a signal would, while the drain asks for the condition
                    moved_on.set()                     # the drain asked again: it went on
                return condition.__enter__()

            def __exit__(self, *exc):
                return condition.__exit__(*exc)

            def __getattr__(self, name):
                return getattr(condition, name)

        def draining():
            state['draining'] = True
            return drain()

        def stopping(context):
            admitted_at_teardown.append(made.gate.admitted)
            moved_on.set()
            return stop(context)
        made.gate._changed, made.gate.drain, events.stop = InterruptedOnce(), draining, stopping
        outcome = []

        def close():
            try:
                made.finish_close(context)
                outcome.append('returned')
            except KeyboardInterrupt:
                outcome.append('interrupted')
        closer = threading.Thread(target=close, daemon=True)
        closer.start()
        self.assertTrue(moved_on.wait(BOUND_S), 'the close went on after the interruption, one way or the other')
        self.assertEqual((admitted_at_teardown, store.closed, outcome), ([], False, []),
                         'it went on WAITING: nothing is torn down while the call is inside')
        state['draining'] = False                          # the test's own leave is not the drain
        made.gate.leave()
        closer.join(BOUND_S)
        self.assertEqual((outcome, admitted_at_teardown, events.stop_calls, alive_at_close, store.closed),
                         (['interrupted'], [0], 1, [[]], True), 'the interruption is raised once everything is torn down')

    def test_a_second_close_that_is_interrupted_while_it_waits_for_the_first_goes_on_waiting(self) -> None:
        made, store, (events,), alive_at_close = self.made(lambda store: WorkerService('events', store), serial_lock=True)
        context = H.ServiceContext()
        made.open_store('/state')
        made.start_services(context)
        events.go_on = threading.Event()
        self.addCleanup(events.go_on.set)
        seen = {}
        first = threading.Thread(target=lambda: (made.finish_close(context), seen.update(first='returned')), daemon=True)
        first.start()
        self.assertTrue(events.in_stop.wait(BOUND_S), "the first close is inside the service's stop")
        made._lifecycle.interrupt_next = 'before'          # the next thread that asks for the lifecycle is interrupted

        def second_close():
            # What the second close finds at the moment it leaves, however the threads are scheduled.
            try:
                made.finish_close(context)
                seen['second'] = ('returned', store.closed, events.alive())
            except KeyboardInterrupt:
                seen['second'] = ('interrupted', store.closed, events.alive())
        second = threading.Thread(target=second_close, daemon=True)
        second.start()
        self.assertTrue(made._lifecycle.contended.wait(BOUND_S), 'the second close has arrived, and was interrupted there')
        events.go_on.set()
        first.join(BOUND_S)
        second.join(BOUND_S)
        self.assertEqual(seen, {'first': 'returned', 'second': ('interrupted', True, False)},
                         'its interruption is raised only after the owner finished: the store closed, the worker gone')
        self.assertEqual((events.stop_calls, alive_at_close), (1, [[]]))

    def test_an_interruption_that_arrives_as_the_lifecycle_is_taken_leaves_it_free_afterwards(self) -> None:
        """The interruption lands after the lock was taken and before the caller knows it. The teardown runs
        once, the interruption is raised after it, and the lifecycle is not left held."""
        made, store, (events,), alive_at_close = self.made(lambda store: WorkerService('events', store), serial_lock=True)
        context = H.ServiceContext()
        made.open_store('/state')
        made.start_services(context)
        made._lifecycle.interrupt_next = 'after'
        with self.assertRaises(KeyboardInterrupt):
            made.finish_close(context)
        self.assertEqual((events.stop_calls, alive_at_close, store.closed), (1, [[]], True))
        again = threading.Thread(target=lambda: made.finish_close(context), daemon=True)
        again.start()
        again.join(BOUND_S)
        self.assertFalse(again.is_alive(), 'another caller is not left waiting for a lifecycle nobody holds')

    @unittest.skipUnless(hasattr(signal, 'pthread_kill'), 'needs a signal that can be aimed at one thread')
    def test_a_real_interrupt_while_a_close_waits_for_the_lifecycle_is_kept_until_the_owner_has_finished(self) -> None:
        """The same with a real SIGINT, sent to this thread while it waits for the lifecycle that another close
        owns. On a runtime where a blocked lock is interrupted by a signal, the interruption is kept and the
        wait begins again; on one where it is not (free-threaded 3.14 on this machine), the interruption
        arrives once the lock has been taken. Either way it is raised only after the owner has finished."""
        if threading.current_thread() is not threading.main_thread():
            self.skipTest('a signal is handled by the main thread only')
        made, store, (events,), alive_at_close = self.made(lambda store: WorkerService('events', store), serial_lock=True)
        context = H.ServiceContext()
        made.open_store('/state')
        made.start_services(context)
        events.go_on = threading.Event()
        self.addCleanup(events.go_on.set)
        let_go = []
        first = threading.Thread(target=lambda: made.finish_close(context), daemon=True)
        first.start()
        self.assertTrue(events.in_stop.wait(BOUND_S))
        asked_again, asks, acquire = threading.Event(), [], made._lifecycle.acquire

        def counting(*args, **kwargs):
            if threading.current_thread() is threading.main_thread():
                asks.append(True)
                if len(asks) == 2:
                    asked_again.set()              # the interruption was kept, and the wait began again
            return acquire(*args, **kwargs)
        made._lifecycle.acquire = counting

        def interrupt_then_let_the_owner_finish():
            if made._lifecycle.contended.wait(BOUND_S):
                signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)
                # Time for a runtime that interrupts a blocked lock to show that the wait began again. It never
                # decides a pass: where nothing is interrupted the owner is simply let go after it.
                asked_again.wait(SIGNAL_SETTLE_S)
            let_go.append(True)
            events.go_on.set()
        # The signal interrupts as Python's own handler does, but only while this thread is inside the close: a
        # close that did not wait has returned by then, and the signal must fail this test, not end the run.
        inside, landed = [False], []

        def interrupt(signum, frame):
            landed.append(inside[0])
            if inside[0]:
                raise KeyboardInterrupt
        self.addCleanup(signal.signal, signal.SIGINT, signal.signal(signal.SIGINT, interrupt))
        helper = threading.Thread(target=interrupt_then_let_the_owner_finish, daemon=True)
        helper.start()
        inside[0] = True
        try:
            with self.assertRaises(KeyboardInterrupt):
                made.finish_close(context)         # blocks behind the first close; the SIGINT lands here
        finally:
            inside[0] = False
        at_the_raise = (list(let_go), store.closed, events.alive())
        first.join(BOUND_S)
        helper.join(BOUND_S)
        self.assertEqual(landed, [True], 'one signal, and it landed inside the close')
        self.assertEqual(at_the_raise, ([True], True, False),
                         'the interruption was raised only after the owner had finished: the store closed, the worker gone')
        self.assertIn(len(asks), (1, 2), 'asked once, or again after an interruption of the wait')
        self.assertEqual((events.stop_calls, alive_at_close), (1, [[]]))

    @quiet
    def test_a_later_interruption_of_a_stop_is_not_lost_behind_an_earlier_ordinary_failure(self) -> None:
        failures = [RuntimeError('could not stop'), KeyboardInterrupt()]

        class Stubborn(WorkerService):
            def stop(inner, context) -> None:
                if failures:
                    inner.stop_calls += 1
                    raise failures.pop(0)
                super().stop(context)
        made, store, (events,), alive_at_close = self.made(lambda store: Stubborn('events', store))
        context = H.ServiceContext()
        made.open_store('/state')
        made.start_services(context)
        with self.assertRaises(KeyboardInterrupt):
            made.finish_close(context)
        self.assertEqual((events.stop_calls, alive_at_close, store.closed), (3, [[]], True))

    @quiet
    def test_a_service_whose_stop_failed_is_still_tracked_while_its_stop_is_tried_again(self) -> None:
        tracked = []

        class Asks(WorkerService):
            def stop(inner, context) -> None:
                tracked.append((made.service_ready('events'), [service.name for service in made._started]))
                super().stop(context)
        made, store, (events,), _ = self.made(lambda store: Asks('events', store, fail_stops=2))
        context = H.ServiceContext()
        made.open_store('/state')
        made.start_services(context)
        with self.assertRaises(RuntimeError):
            made.finish_close(context)
        self.assertEqual(tracked, [(True, ['events'])] * 3, 'tracked, and ready, until a stop has returned')
        self.assertEqual((made._started, made.service_ready('events')), ([], False))

    def test_a_lifecycle_method_called_from_inside_another_is_refused_and_does_not_wait_for_itself(self) -> None:
        made, store, (events,), alive_at_close = self.made(lambda store: WorkerService('events', store))
        context = H.ServiceContext()
        made.open_store('/state')
        made.start_services(context)
        seen, asked, stop = [], [], events.stop

        def stopping(context):
            if not asked:
                asked.append(True)       # once, whatever comes of it: a teardown that did re-enter ends, and is seen
                for method in (made.finish_close, made.stop_services):
                    try:
                        method(context)
                        seen.append('ran')
                    except H.HookError:
                        seen.append('refused')
            return stop(context)
        events.stop = stopping
        made.finish_close(context)
        self.assertEqual((seen, events.stop_calls, alive_at_close, store.closed), (['refused', 'refused'], 1, [[]], True))


class AncestorDiscoveryTest(unittest.TestCase):
    """A registrant whose parent's own parent is not installed is absent, not broken (the read's note 4)."""

    def test_an_absent_ancestor_is_absence_and_a_broken_installed_one_fails(self) -> None:
        fake = FakeLanes(self)
        self.assertIsNone(importlib.util.find_spec('recursion_core.control.mcp'), 'that ancestor is absent in this tree')
        made = H.compose()
        self.assertEqual(made.methods.registered(), ())
        fake.lane('control.mcp', init='raise RuntimeError("this package does not import")\n')
        with self.assertRaises(H.HookError):
            H.compose()


if __name__ == "__main__":
    unittest.main()
