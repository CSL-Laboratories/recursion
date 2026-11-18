"""The change reader under kill -9, with the daemon's real Registry and the real event store (events/changes.py).

A child process (crash_reader.py) composes the registry, the store and the reader as the daemon does, and is
killed with SIGKILL at a point it names. The parent then starts again as the daemon would (a new ``Registry``
on the same folder, ``changes.open_store``, a new marker and a new reader), lets the reader catch up, and checks
the log against the registry, reading the store's file on a connection of its own:

- every run the registry holds whose record has a typed post-image is in the log, and the last image logged
  for it is the registry's (``post_image(*registry.snapshot(run))``, compared by image and by digest); every
  other run it holds is listed in the lane's table ``events_unlogged`` with its code, and no run is listed
  there but those; the restarted reader's ``unlogged`` is that table;
- no image is logged twice in a row for a run (dedupe is against the last image), and each event's digest is
  its image's;
- every log's seq is 1..n with no hole, and ``coalesced`` is null;
- the lane's table holds, for every logged run, the digest of its last event;
- nothing logged before the restart has changed.

The points: while the first pass ever made on the store is cut short (one page logged); after a registry
publish and before the reader's append, with the mark not yet taken and with it taken and the image read;
just after the store's commit of the reader's transaction (the store's, so a reader
that split the event and the digest into two transactions is killed between them); after a run was listed as
unlogged; and anywhere, in seeded rounds of random writes (control texts with no typed post-image among them)
and reader steps. The stdout and result proofs are the tailer's and the S8 reader's
(slice 3).
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

try:
    from recursion_core.control import hooks as H
    from recursion_core.daemon import Registry
    from recursion_core.events import changes as C
    from recursion_core.events.tests import crash_reader as R
    POSIX = D.fcntl is not None and hasattr(signal, 'SIGKILL')
except Exception:                                   # noqa: BLE001 - the daemon is Linux only
    POSIX = False

BOUND_S = 60.0          # only ends a wait that would otherwise never end; it never decides a pass
CORE = pathlib.Path(__file__).resolve().parents[3]
SEED = int(os.environ.get('EVENTS_READER_CRASH_SEED', '20261114'))
ROUNDS = int(os.environ.get('EVENTS_READER_CRASH_ROUNDS', '10'))


def interpreter() -> list:
    """This interpreter with the same -X options (the free-threaded build keeps -X gil=0)."""
    options = [f'-X{key}' if value is True else f'-X{key}={value}' for key, value in sys._xoptions.items()]
    return [sys.executable, '-B', *options]


def read_log(path: str) -> list:
    with contextlib.closing(sqlite3.connect(path)) as conn:
        return [json.loads(text) for (text,) in conn.execute('SELECT event FROM event_log ORDER BY position')]


def every_run_id(registry) -> list:
    ids, after = [], None
    while True:
        page = registry.run_ids(after, 64)
        if not page:
            return ids
        ids.extend(page)
        after = page[-1]


class Child:
    def __init__(self, case: unittest.TestCase, folder, mode: str, *extra) -> None:
        argv = [*interpreter(), '-m', 'recursion_core.events.tests.crash_reader', str(folder), mode, *map(str, extra)]
        self._case = case
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, env=dict(os.environ, PYTHONPATH=str(CORE)))
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


@unittest.skipUnless(POSIX, 'kill -9 and the daemon registry: the daemon runs on Linux')
class ReaderCrashTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.mkdtemp(prefix='events-reader-crash-')
        self.addCleanup(shutil.rmtree, folder, True)
        self.state = pathlib.Path(folder) / 'state'

    def start(self, mode: str, *extra) -> Child:
        child = Child(self, self.state, mode, *extra)
        word, _count = child.line().split()
        self.assertEqual(word, 'caught-up')
        return child

    def restart(self, change=None) -> tuple:
        """Start again as the daemon would and let the reader catch up. Returns (what this start logged, the
        whole log), after checking the whole log against the registry. ``change(registry)``, when given, is a
        registry write made before the reader exists (its mark is lost, as at a daemon start)."""
        registry = Registry(self.state)
        try:
            store = C.open_store(self.state)
            try:
                if change is not None:
                    change(registry)
                before = read_log(store.path)
                marker = H.ChangeMarker()
                registry.mark_changed = marker.mark
                reader = C.ChangeReader(marker)
                reader.bind(registry, store)
                R.catch_up(reader)
                self.assertTrue(reader.ready, 'the first pass of this start has completed')
                after = read_log(store.path)
                self.assertEqual(after[:len(before)], before, 'nothing logged before the restart has changed')
                self.check_log(registry, store.path, after, reader.unlogged)
            finally:
                store.close()
        finally:
            registry.close()
        return after[len(before):], after

    def check_log(self, registry, path: str, events: list, unlogged: dict) -> None:
        seqs, digests, last = {}, {}, {}
        for event in events:
            seqs.setdefault(event['session_id'], []).append(event['seq'])
            if event['type'] != 'state.changed':
                continue
            payload = event['payload']
            self.assertIsNone(payload['coalesced'])
            self.assertEqual(payload['post_image_sha256'], C.image_digest(payload['post_image']))
            digests.setdefault(event['run_id'], []).append(payload['post_image_sha256'])
            last[event['run_id']] = payload['post_image']
        for log, numbers in seqs.items():
            self.assertEqual(numbers, list(range(1, len(numbers) + 1)), f'log {log}: seq 1..n with no hole')
        for run_id, logged in digests.items():
            for one, then in zip(logged, logged[1:]):
                self.assertNotEqual(one, then, f'{run_id}: the same image logged twice in a row')
        ids = every_run_id(registry)
        with contextlib.closing(sqlite3.connect(path)) as conn:
            table = dict(conn.execute('SELECT run_id, digest FROM events_run_images'))
            listed = dict(conn.execute('SELECT run_id, code FROM events_unlogged'))
        untyped = {}
        for run_id in ids:
            try:
                image = C.post_image(*registry.snapshot(run_id))
            except C.BadImage as exc:
                untyped[run_id] = str(exc)
                continue
            self.assertEqual(last.get(run_id), json.loads(json.dumps(image)), f'{run_id}: the last image is the registry\'s')
            self.assertEqual(digests[run_id][-1], C.image_digest(image))
        self.assertEqual(set(last) - set(ids), set(), 'the log holds no run the registry does not hold')
        self.assertEqual((listed, unlogged), (untyped, untyped), 'the runs with no typed image are listed, and no other')
        self.assertEqual(table, {run_id: logged[-1] for run_id, logged in digests.items()},
                         'the lane\'s table holds the digest of every run\'s last event')

    @staticmethod
    def changed(events: list) -> list:
        return [(e['run_id'], e['payload']['reason']) for e in events if e['type'] == 'state.changed']

    def test_kill_9_while_the_first_pass_is_cut_short_logs_a_run_already_logged_as_reconcile(self) -> None:
        child = Child(self, self.state, 'cut')
        self.assertEqual(child.line(), 'cut 0', 'one page of the first pass ever is logged; the pass is not complete')
        child.kill()
        new, log = self.restart(change=lambda registry: registry.state(R.RUN_A, 'running'))
        self.assertEqual(self.changed(log), [(R.RUN_A, 'snapshot'), (R.RUN_A, 'reconcile'), (R.RUN_B, 'snapshot')],
                         'a run this log already holds is not a first record again; one it never reached is')
        self.assertEqual(new[0]['payload']['post_image']['daemon_row']['state'], 'running')

    def test_kill_9_after_a_registry_publish_and_before_the_append_gives_one_reconcile_image_per_run(self) -> None:
        child = self.start('published')
        self.assertEqual(child.line(), 'published')
        child.kill()
        new, log = self.restart()
        self.assertEqual(self.changed(log[:len(log) - len(new)]), [(R.RUN_A, 'snapshot'), (R.RUN_B, 'snapshot')],
                         'the first pass ever logged the base runs')
        self.assertEqual(self.changed(new), [(R.RUN_A, 'reconcile'), (R.RUN_B, 'reconcile'), (R.RUN_C, 'reconcile')],
                         'one reconcile image for each run the publishes changed, none for the others')
        images = {e['run_id']: e['payload']['post_image'] for e in new}
        self.assertEqual(images[R.RUN_A]['daemon_row']['state'], 'running')
        self.assertEqual(images[R.RUN_B]['control_record']['note'], 'b1')
        self.assertEqual((images[R.RUN_C]['daemon_row']['state'], images[R.RUN_C]['control_record']), ('starting', None))

    def test_kill_9_after_the_reader_took_the_mark_and_read_the_image_gives_one_reconcile_image(self) -> None:
        child = self.start('taken')
        self.assertEqual(child.line(), f'reading {R.RUN_A}')
        child.kill()
        new, _log = self.restart()
        self.assertEqual(self.changed(new), [(R.RUN_A, 'reconcile')], 'the mark died with the process; the scan finds the run')
        self.assertEqual(new[0]['payload']['post_image']['daemon_row']['state'], 'running')

    def test_kill_9_after_the_readers_commit_logs_no_duplicate(self) -> None:
        child = self.start('committed')
        self.assertEqual(child.line(), 'committed')
        child.kill()
        new, log = self.restart()
        self.assertEqual(new, [], 'the commit holds the event and the last digest: nothing is logged again')
        mine = [e for e in log if e['type'] == 'state.changed' and e['run_id'] == R.RUN_A]
        self.assertEqual([(e['payload']['reason'], e['payload']['post_image']['daemon_row']['state']) for e in mine],
                         [('snapshot', 'starting'), ('commit', 'running')])

    def test_kill_9_after_a_run_was_listed_unlogged_keeps_the_listing(self) -> None:
        child = self.start('unlogged')
        self.assertEqual(child.line(), 'unlogged')
        child.kill()
        with contextlib.closing(sqlite3.connect(str(self.state / C.STORE_FILE))) as conn:
            self.assertEqual(dict(conn.execute('SELECT run_id, code FROM events_unlogged')),
                             {R.RUN_B: 'control_text_not_json'}, 'the listing is in the file the process left')
        new, _log = self.restart()
        self.assertEqual(new, [], 'the restart logs nothing for it and lists it again from the file')
        new, _log = self.restart(change=lambda registry: registry.control_put(R.RUN_B, R.record(R.RUN_B, 'b2')))
        self.assertEqual(self.changed(new), [(R.RUN_B, 'reconcile')])
        self.assertEqual(new[0]['payload']['post_image']['control_record']['note'], 'b2')

    def test_kill_9_anywhere_in_random_writes_leaves_the_log_equal_to_the_registry(self) -> None:
        rng = random.Random(SEED)
        logged = 0
        for round_ in range(ROUNDS):
            with self.subTest(round=round_, seed=SEED):
                child = self.start('churn', SEED * 100 + round_)
                for _ in range(rng.randint(1, 12)):
                    self.assertTrue(child.line().startswith('wrote '))
                child.kill()                 # lands wherever the child is: in a publish, a read, a commit, between
                new, log = self.restart()
                self.assertEqual({reason for _run, reason in self.changed(new)} - {'reconcile'}, set(),
                                 'a restart logs reconcile images only')
                logged = len(log)
        if os.environ.get('EVENTS_DURABILITY_REPORT'):
            sys.stderr.write(f'\n[reader crash] seed {SEED}: {ROUNDS} kills, {logged} events in the log\n')


if __name__ == '__main__':
    unittest.main()
