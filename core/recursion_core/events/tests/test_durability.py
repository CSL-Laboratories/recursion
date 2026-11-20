"""The real store under kill -9 and a write that the file system refuses (events/db.py, DURABILITY).

A child process (crash_child.py) commits transactions that each write an event, a receipt and a lane row
together, and says ``acked <k>`` after each commit has returned. The parent kills it with SIGKILL, opens the
store again (recovery, ``quick_check``, the epoch) and checks, on a connection of its own:

- every acknowledged transaction is there, with all three of its parts;
- no transaction is there in part: the events, the receipts and the rows name the same transactions;
- those transactions are 1..K with no hole (one writer, in order), and the log's seq is 1..K;
- ``PRAGMA integrity_check`` is ok and the epoch rose by one at each open.

A full file system is real: the writer runs in a private user and mount namespace (``unshare``, no root and
nothing installed) on a one-MiB tmpfs mounted only inside it, until SQLite reports SQLITE_FULL; the test is
skipped where such a namespace is refused. A file size limit (RLIMIT_FSIZE) gives a refused write of the
other kind, an I/O error.

What this does not show: a power cut or an operating-system crash (synchronous=FULL is what covers those, and
nothing here emulates one; that needs a block-level fault device and root).
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import queue
import random
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

from recursion_core.events import db as D
from recursion_core.events.tests import crash_child as C

BOUND_S = 60.0          # only ends a wait that would otherwise never end; it never decides a pass
CORE = pathlib.Path(__file__).resolve().parents[3]
SEED = int(os.environ.get('EVENTS_DURABILITY_SEED', '20261114'))
ROUNDS = int(os.environ.get('EVENTS_DURABILITY_ROUNDS', '12'))
POSIX = D.fcntl is not None and hasattr(signal, 'SIGKILL')


MOUNTED = 'namespace-ready'      # what the namespace prints once its tmpfs is mounted, before the writer starts


# What ``unshare`` says when the kernel or a security module refuses an unprivileged namespace: the namespace
# itself, or the uid or gid map (Ubuntu 24.04's restriction says the latter). Nothing else is a refusal.
REFUSALS = ('unshare: unshare failed: ', 'unshare: write failed /proc/self/uid_map: ', 'unshare: write failed /proc/self/gid_map: ')
REFUSED_BECAUSE = ('Operation not permitted', 'Permission denied')


def full_disk_verdict(run) -> tuple:
    """How a run of the full-file-system writer ended. ``('skip', why)`` only when the namespace or the mount was
    refused BEFORE the mount was confirmed: ``unshare`` names a refused namespace or map (``REFUSALS``, ending in
    ``REFUSED_BECAUSE``), or the mount failed (exit 77). ``('ran', the child's lines)`` once the mount was
    confirmed, whatever happened next: the test then holds those lines to the whole proof. ``('broken', why)``
    for anything else, a command that could not run or was given a wrong option among them, which fails the
    test (review 3 of #373)."""
    lines = run.stdout.splitlines()
    if lines and lines[0] == MOUNTED:
        return 'ran', lines[1:]
    first = (run.stderr.strip().splitlines() or [''])[0].rstrip()
    refused = run.returncode != 0 and first.startswith(REFUSALS) and first.endswith(REFUSED_BECAUSE)
    if run.returncode == 77 or refused:
        return 'skip', f'no private mount namespace here (exit {run.returncode}): {run.stderr.strip()[-200:]}'
    return 'broken', f'the namespace step ended unexpectedly (exit {run.returncode}): {run.stderr.strip()[-300:]}'


def assert_full_disk_proof(case: unittest.TestCase, run, lines: list) -> None:
    """The whole proof a mounted writer must give; anything less fails."""
    case.assertEqual(run.returncode, 0, run.stderr)
    case.assertEqual(len(lines), 4, f'the writer gave no complete proof: {lines!r} {run.stderr[-300:]!r}')
    case.assertTrue(lines[0].startswith('open '), lines[0])
    failure, after, survived = json.loads(lines[1]), lines[2], json.loads(lines[3])
    case.assertEqual((failure['size'], failure['free']), (1024 * 1024, 0), 'the file system was full')
    case.assertEqual((failure['error'], failure['code'], failure['cause']),
                     ('CommitUnknown', 'commit_outcome_unknown', 'SQLITE_FULL'), lines[1])
    case.assertEqual(after, 'after StoreError store_failed', 'a failed store refuses the next transaction')
    acked, rows = failure['acked'], survived['rows']
    case.assertGreaterEqual(acked, 1)
    case.assertEqual(survived['integrity'], [['ok']])
    case.assertIn(len(rows), (acked, acked + 1), 'the failed commit is there whole or not at all')
    case.assertEqual(rows, list(range(1, len(rows) + 1)))
    case.assertEqual((survived['seqs'], survived['ks'], survived['receipts']), (rows, rows, rows))


def interpreter() -> list:
    """This interpreter with the same -X options (the free-threaded build keeps -X gil=0)."""
    options = [f'-X{key}' if value is True else f'-X{key}={value}' for key, value in sys._xoptions.items()]
    return [sys.executable, '-B', *options]


class Child:
    def __init__(self, case: unittest.TestCase, path: str, mode: str, first: int, *extra) -> None:
        argv = [*interpreter(), '-m', 'recursion_core.events.tests.crash_child', path, mode, str(first), *map(str, extra)]
        env = dict(os.environ, PYTHONPATH=str(CORE))
        self._case = case
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, env=env)
        self._lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        case.addCleanup(self._reap)

    def _read(self) -> None:
        for line in self.proc.stdout:
            self._lines.put(line.rstrip('\n'))
        self._lines.put(None)

    def line(self) -> str:
        try:
            line = self._lines.get(timeout=BOUND_S)
        except queue.Empty:
            self._case.fail('the child said nothing within the bound')
        if line is None:
            self.proc.wait()
            self._case.fail(f'the child ended early ({self.proc.returncode}): {self.proc.stderr.read()}')
        return line

    def kill(self) -> None:
        os.kill(self.proc.pid, signal.SIGKILL)
        self.proc.wait()
        self._case.assertEqual(self.proc.returncode, -signal.SIGKILL)

    def _reap(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            with contextlib.suppress(Exception):
                stream.close()


@unittest.skipUnless(POSIX, 'kill -9 and POSIX file locks: the daemon runs on Linux')
class DurabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.mkdtemp(prefix='events-durability-')
        self.addCleanup(shutil.rmtree, folder, True)
        self.path = os.path.join(folder, 'events.sqlite3')
        self.epoch = 0

    def opened(self, child: Child) -> None:
        word, epoch = child.line().split()
        self.assertEqual((word, int(epoch)), ('open', self.epoch + 1), 'the epoch rises by one at every open')
        self.epoch = int(epoch)

    def check(self) -> int:
        """Open the store again as the daemon would, then read what survived; return K."""
        store = D.EventStore(self.path)
        self.assertEqual(store.epoch, self.epoch + 1)
        self.epoch = store.epoch
        store.register_tables('durability', 1, C.LANE_DDL)
        with store.transaction() as tx:
            rows = [k for (k,) in tx.execute('SELECT k FROM durability_rows ORDER BY k')]
        store.close()
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
            events = [json.loads(text) for (text,) in
                      conn.execute('SELECT event FROM event_log WHERE log_id = ? ORDER BY position', (C.LOG,))]
            receipts = sorted(int(command[len('command-'):]) for (command,) in
                              conn.execute("SELECT command_id FROM receipts WHERE method = 'durability.write'"))
        k = len(rows)
        self.assertEqual(rows, list(range(1, k + 1)), 'the rows are 1..K with no hole')
        self.assertEqual(receipts, rows, 'a receipt for exactly the transactions whose row is there')
        self.assertEqual([event['payload']['k'] for event in events], rows, 'an event for exactly those transactions')
        self.assertEqual([event['seq'] for event in events], rows, 'and the log\'s seq is 1..K')
        return k

    def test_kill_9_anywhere_loses_no_acknowledged_commit_and_leaves_no_partial_one(self) -> None:
        rng = random.Random(SEED)
        k, past_the_last_ack = 0, 0
        for round_ in range(ROUNDS):
            with self.subTest(round=round_, seed=SEED):
                child = Child(self, self.path, 'run', k + 1)
                self.opened(child)
                acked = k
                for _ in range(rng.randint(1, 6)):
                    word, number = child.line().split()
                    self.assertEqual((word, int(number)), ('acked', acked + 1))
                    acked += 1
                child.kill()                 # lands wherever the child is: between, inside or at the end of a commit
                survived = self.check()
                self.assertGreaterEqual(survived, acked, 'every acknowledged transaction survived')
                past_the_last_ack += survived > acked
                k = survived
        if os.environ.get('EVENTS_DURABILITY_REPORT'):
            sys.stderr.write(f'\n[durability] seed {SEED}: {ROUNDS} kills, {k} transactions survived, '
                             f'{past_the_last_ack} kills landed after a commit the parent had not read yet\n')

    def test_kill_9_inside_an_open_transaction_leaves_none_of_it(self) -> None:
        child = Child(self, self.path, 'before-commit', 1)
        self.opened(child)
        self.assertEqual(child.line(), 'acked 1')
        self.assertEqual(child.line(), 'inside 2')
        child.kill()
        self.assertEqual(self.check(), 1, 'transaction 2 was written but not committed: none of it is there')

    def test_kill_9_after_a_commit_returned_keeps_all_of_it(self) -> None:
        child = Child(self, self.path, 'after-commit', 1)
        self.opened(child)
        self.assertEqual(child.line(), 'acked 1')
        self.assertEqual(child.line(), 'committed 2')
        child.kill()
        self.assertEqual(self.check(), 2, 'transaction 2 had committed: all of it is there')

    def test_a_refused_write_fails_the_commit_and_the_store_and_loses_nothing_committed(self) -> None:
        store = D.EventStore(self.path)        # made first, so that the limit is measured on a store that exists
        self.epoch = store.epoch
        store.close()
        child = Child(self, self.path, 'file-limit', 1, 96 * 1024)
        self.opened(child)
        self.assertEqual(child.line().split()[0], 'limit')
        acked = 0
        while True:
            line = child.line()
            if not line.startswith('acked '):
                break
            acked += 1
            self.assertEqual(line, f'acked {acked}')
        failed = line.split()
        self.assertEqual(failed[:2], ['failed', str(acked + 1)], line)
        self.assertEqual(failed[2:4], ['CommitUnknown', 'commit_outcome_unknown'], line)
        self.assertTrue(failed[4].startswith('SQLITE_IOERR') or failed[4] == 'SQLITE_FULL', line)
        self.assertEqual(child.line(), 'after StoreError store_failed', 'a failed store refuses the next transaction')
        child.proc.wait()
        self.assertEqual(child.proc.returncode, 0)
        self.assertGreaterEqual(acked, 1, 'at least one commit fitted under the limit')
        survived = self.check()
        self.assertIn(survived, (acked, acked + 1), 'the failed commit is there whole or not at all')
        store = D.EventStore(self.path)        # with room again, the store is usable
        self.epoch = store.epoch
        store.register_tables('durability', 1, C.LANE_DDL)
        with store.transaction() as tx:
            C.write(tx, survived + 1)
        store.close()
        self.assertEqual(self.check(), survived + 1)


    def test_a_full_file_system_fails_the_commit_and_the_store_and_loses_nothing_committed(self) -> None:
        if shutil.which('unshare') is None or shutil.which('mount') is None:
            self.skipTest('no unshare or mount on this machine')
        folder = tempfile.mkdtemp(prefix='events-full-disk-')
        self.addCleanup(shutil.rmtree, folder, True)
        inner = f'mount -n -t tmpfs -o size=1m,mode=700 tmpfs "$1" || exit 77; echo {MOUNTED}; shift; exec "$@"'
        argv = ['unshare', '--user', '--map-root-user', '--mount', '--propagation', 'private', '/bin/sh', '-c', inner, 'sh',
                folder, *interpreter(), '-m', 'recursion_core.events.tests.crash_child',
                os.path.join(folder, 'events.sqlite3'), 'full-disk', '1']
        run = subprocess.run(argv, capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=str(CORE)),
                             timeout=BOUND_S)
        verdict, detail = full_disk_verdict(run)
        if verdict == 'skip':
            self.skipTest(detail)
        self.assertEqual(verdict, 'ran', detail)
        assert_full_disk_proof(self, run, detail)


class FullDiskVerdictTest(unittest.TestCase):
    """Only a refused namespace or mount skips the full-file-system test; a writer that fails after the mount
    fails it (review 2 of #373). Runs everywhere: it needs no namespace."""

    @staticmethod
    def run_of(code: int, out: str, err: str = '') -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(['unshare'], code, out, err)

    def test_only_a_refused_namespace_or_mount_is_a_skip(self) -> None:
        cases = [
            (self.run_of(1, '', 'unshare: unshare failed: Operation not permitted\n'), 'skip'),
            (self.run_of(1, '', 'unshare: write failed /proc/self/uid_map: Operation not permitted\n'), 'skip'),
            (self.run_of(1, '', 'unshare: write failed /proc/self/gid_map: Permission denied\n'), 'skip'),
            (self.run_of(77, '', 'mount: /x: permission denied.\n'), 'skip'),
            # Review 3: a broken command is not a refused namespace.
            (self.run_of(127, '', 'unshare: failed to execute /missing/shell: No such file or directory\n'), 'broken'),
            (self.run_of(1, '', "unshare: unrecognized option '--bad-option'\nTry 'unshare --help' for more information.\n"),
             'broken'),
            (self.run_of(1, '', 'unshare: unshare failed: Invalid argument\n'), 'broken'),
            (self.run_of(2, '', 'sh: 1: cannot create /var/tmp/x: Permission denied\n'), 'broken'),   # not unshare's
            (self.run_of(77, f'{MOUNTED}\n', 'mount: permission denied.\n'), 'ran'),       # after the mount, never a skip
            (self.run_of(1, f'{MOUNTED}\n', 'unshare: unshare failed: Operation not permitted\n'), 'ran'),
            (self.run_of(0, f'{MOUNTED}\n'), 'ran'),                          # mounted, and the writer said nothing
            (self.run_of(1, f'{MOUNTED}\nopen 1\n', 'Traceback (most recent call last):\n'), 'ran'),
            (self.run_of(1, '', 'Traceback (most recent call last):\n'), 'broken'),
            (self.run_of(0, 'open 1\n'), 'broken'),                           # output without the mount's word
        ]
        for run, expected in cases:
            with self.subTest(code=run.returncode, out=run.stdout, err=run.stderr):
                self.assertEqual(full_disk_verdict(run)[0], expected)

    def test_a_whole_looking_proof_with_the_wrong_first_line_fails(self) -> None:
        failure = {'size': 1024 * 1024, 'free': 0, 'error': 'CommitUnknown', 'code': 'commit_outcome_unknown',
                   'cause': 'SQLITE_FULL', 'acked': 1}
        survived = {'rows': [1], 'integrity': [['ok']], 'seqs': [1], 'ks': [1], 'receipts': [1]}
        good = ['open 1', json.dumps(failure), 'after StoreError store_failed', json.dumps(survived)]
        run = self.run_of(0, MOUNTED + '\n' + '\n'.join(good) + '\n')
        assert_full_disk_proof(self, run, full_disk_verdict(run)[1])      # the whole proof passes as given
        bad = ['not the open line'] + good[1:]
        run = self.run_of(0, MOUNTED + '\n' + '\n'.join(bad) + '\n')
        with self.assertRaises(AssertionError):
            assert_full_disk_proof(self, run, full_disk_verdict(run)[1])

    def test_a_mounted_writer_without_its_whole_proof_fails(self) -> None:
        for out, code in ((f'{MOUNTED}\n', 0), (f'{MOUNTED}\nopen 1\n', 0), (f'{MOUNTED}\nopen 1\n', 1)):
            with self.subTest(out=out, code=code):
                run = self.run_of(code, out)
                verdict, lines = full_disk_verdict(run)
                self.assertEqual(verdict, 'ran')
                with self.assertRaises(AssertionError):
                    assert_full_disk_proof(self, run, lines)


def setUpModule():
    # Every store refuses an SQLite with the WAL reset bug unless the person accepts the risk
    # (daemon/tests/test_sqlite_wal_bug.py). This module tests other behaviour, so it runs on a stubbed
    # repaired version, whatever SQLite this Python has.
    import sqlite3
    import unittest
    from unittest import mock
    stub = mock.patch.object(sqlite3, 'sqlite_version', '3.51.3')
    stub.start()
    unittest.addModuleCleanup(stub.stop)


if __name__ == '__main__':
    unittest.main()
