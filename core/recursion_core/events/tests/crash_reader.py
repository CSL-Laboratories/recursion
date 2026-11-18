"""The daemon process that test_reader_crash.py kills. Not a test module: it is run as a child process.

    python -m recursion_core.events.tests.crash_reader <state folder> <mode> [<seed>]

It composes what the daemon composes for lane EVENTS: the daemon's real ``Registry`` in <state folder>, the
real event store that ``changes.open_store`` opens in the same folder, SPINE's ``ChangeMarker`` as the
registry's ``mark_changed``, and the ``ChangeReader`` bound to both. The reader is stepped here, on this thread,
so the parent knows where the process is when it kills it.

Mode ``cut`` is different: on an empty folder it makes two runs (``run-aaaa``, ``run-bbbb``) with their marks
lost, steps a reader
whose page is one run once, so the first pass ever made on the store logs ``run-aaaa`` and is not complete,
prints ``cut <ready, 0 or 1>`` and waits.

Every other mode first makes the base runs if they are missing (``run-aaaa``, a daemon row; ``run-bbbb``, a control
record), then catches up: the reader steps until no scan is in progress and nothing is pending. It prints
``caught-up <number of events in the log>``. Then:

    published   publish three changes (a state, a control record, a new run), print ``published`` and wait.
                The reader is not stepped: the marks die with the process, and nothing of the three is logged.
    taken       publish one change to run-aaaa and step the reader: it takes the mark and reads the image, and
                stops before its transaction begins; print ``reading run-aaaa`` and wait.
    committed   publish one change to run-aaaa and step the reader; as soon as the store's first commit after
                that returns (the reader's: the event and the run's new last digest), print ``committed`` and
                wait, before the step returns. The point is the store's commit, not the reader's, so a reader
                that wrote the event and the digest in two transactions would be killed between them.
    unlogged    publish a control text that is not JSON to run-bbbb and catch up: the reader lists the run in
                its table ``events_unlogged``; print ``unlogged`` and wait.
    churn       forever: one registry write chosen by a generator seeded with <seed> (some of them control
                texts with no typed post-image), then zero to three reader steps; print ``wrote <n>`` after
                each write.

Waiting is a read of stdin that never gets an answer: the parent kills the process.
"""

from __future__ import annotations

import contextlib
import json
import random
import sqlite3
import sys
from pathlib import Path

from recursion_core.control import hooks as H
from recursion_core.daemon import Registry
from recursion_core.events import changes as C

RUN_A, RUN_B, RUN_C = 'run-aaaa', 'run-bbbb', 'run-cccc'
CHURN_RUNS = ('run-c1', 'run-c2', 'run-c3', 'run-c4')
STATES = ('running', 'stopping', 'terminated')
UNTYPED = ('not json', '[1, 2]', '{"n": NaN}')     # control texts with no typed post-image


def record(run_id: str, note: str) -> str:
    return json.dumps({'run_id': run_id, 'note': note})


def say(text: str) -> None:
    sys.stdout.write(text + '\n')
    sys.stdout.flush()


def wait() -> int:
    sys.stdin.readline()
    return 1


def catch_up(reader: C.ChangeReader) -> None:
    for _ in range(10_000):
        if not reader.step() and not reader._pending:
            return
    raise RuntimeError('the reader did not settle')


def events_in_log(path: str) -> int:
    with contextlib.closing(sqlite3.connect(path)) as conn:
        return conn.execute('SELECT count(*) FROM event_log').fetchone()[0]


def main(argv) -> int:
    folder, mode = Path(argv[1]), argv[2]
    registry = Registry(folder)
    store = C.open_store(folder)
    marker = H.ChangeMarker()
    registry.mark_changed = marker.mark
    if mode == 'cut':
        reader = C.ChangeReader(marker, page=1)
        reader.bind(registry, store)
        registry.mark_changed = None              # no marks: only the scan logs, one page at this step
        registry.add(RUN_A, 'boot-1')
        registry.add(RUN_B, 'boot-1')
        registry.mark_changed = marker.mark
        reader.step()
        say(f'cut {int(reader.ready)}')
        return wait()

    reader = C.ChangeReader(marker)
    reader.bind(registry, store)

    if registry.run_ids(None, 1) == []:
        registry.add(RUN_A, 'boot-1')
        registry.control_put(RUN_B, record(RUN_B, 'b0'))
    catch_up(reader)
    say(f'caught-up {events_in_log(store.path)}')

    if mode == 'published':
        registry.state(RUN_A, 'running')
        registry.control_put(RUN_B, record(RUN_B, 'b1'))
        registry.add(RUN_C, 'boot-1')
        say('published')
        return wait()

    if mode == 'unlogged':
        registry.control_put(RUN_B, 'not json')
        catch_up(reader)
        say('unlogged')
        return wait()

    if mode == 'taken':
        def reading(run_id, image, digest, reason):
            say(f'reading {run_id}')
            return wait()
        reader._commit = reading
        registry.state(RUN_A, 'running')
        catch_up(reader)
        raise RuntimeError('the reader never reached its commit')

    if mode == 'committed':
        real = store._commit

        def committed(tx):
            real(tx)
            say('committed')
            return wait()
        store._commit = committed
        registry.state(RUN_A, 'running')
        catch_up(reader)
        raise RuntimeError('the reader never committed')

    if mode == 'churn':
        rng = random.Random(int(argv[3]))
        present = set(registry.run_ids(None, 64))
        n = 0
        while True:
            run_id = rng.choice(CHURN_RUNS)
            kind = rng.randrange(5)
            if run_id not in present or kind == 0:
                if run_id not in present:
                    registry.add(run_id, 'boot-1')
                    present.add(run_id)
                else:
                    registry.control_put(run_id, record(run_id, f'note {rng.randrange(1_000_000)}'))
            elif kind == 1:
                registry.state(run_id, rng.choice(STATES), f'detail {rng.randrange(1_000)}')
            elif kind == 2:
                registry.request_stop(run_id)
            elif kind == 3:
                registry.control_put(run_id, rng.choice(UNTYPED))
            else:
                registry.control_put(run_id, record(run_id, f'note {rng.randrange(1_000_000)}'))
            n += 1
            say(f'wrote {n}')
            for _ in range(rng.randrange(4)):
                reader.step()

    raise SystemExit(f'unknown mode {mode}')


if __name__ == '__main__':
    sys.exit(main(sys.argv))
