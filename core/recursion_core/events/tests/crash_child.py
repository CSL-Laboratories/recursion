"""The writer that test_durability.py kills. Not a test module: it is run as a child process.

    python -m recursion_core.events.tests.crash_child <path> <mode> <first k> [<budget bytes>]

Each transaction k writes, together, one event in log ``run:durability`` (payload ``{"k": k}``), one
receipt (command ``command-<k>``) and one row of lane ``durability``'s table, with a few kilobytes of text
so that a commit spans several WAL frames. After each commit returns it prints ``acked <k>``. Modes:

    run              commit k, k+1, ... until killed.
    before-commit    commit k, then write k+1 and print ``inside <k+1>`` with the transaction open, and wait.
    after-commit     commit k, then commit k+1, print ``committed <k+1>`` before ``acked``, and wait.
    file-limit       limit the size of every file this process writes to its present size plus <budget
                     bytes> (RLIMIT_FSIZE, SIGXFSZ ignored), commit until a commit fails, print
                     ``failed <k> <error class> <code> <cause>``, try one more transaction, print
                     ``after <error class> <code>``, and exit 0.

Waiting is a read of stdin that never gets an answer: the parent kills the process.
"""

from __future__ import annotations

import hashlib
import os
import sys

from recursion_core.events.db import EventStore

LANE_DDL = 'CREATE TABLE durable (k INTEGER PRIMARY KEY, note TEXT NOT NULL)'
LOG = 'run:durability'


def write(tx, k: int) -> None:
    note = (f'{k:08d}' * 512)[:4000]
    tx.append({'session_id': LOG, 'run_id': 'durability', 'turn_id': None, 'type': 'result', 'source': 'core',
               'trust': 'trusted', 'payload': {'k': k, 'note': note}})
    tx.receipt_put('tenant', 'durability.write', f'command-{k:08d}', hashlib.sha256(str(k).encode()).hexdigest(), {'k': k})
    tx.execute('INSERT INTO durable (k, note) VALUES (?, ?)', (k, note))


def say(text: str) -> None:
    sys.stdout.write(text + '\n')
    sys.stdout.flush()


def main(argv) -> int:
    path, mode, k = argv[1], argv[2], int(argv[3])
    store = EventStore(path)
    store.register_tables('durability', 1, LANE_DDL)
    say(f'open {store.epoch}')
    if mode == 'file-limit':
        import resource
        import signal
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        sizes = [os.path.getsize(p) for p in (path, path + '-wal') if os.path.exists(p)]
        limit = max(sizes) + int(argv[4])
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, resource.getrlimit(resource.RLIMIT_FSIZE)[1]))
        say(f'limit {limit}')
    while True:
        if mode == 'before-commit' and k > int(argv[3]):
            with store.transaction() as tx:
                write(tx, k)
                say(f'inside {k}')
                sys.stdin.readline()
            return 1
        try:
            with store.transaction() as tx:
                write(tx, k)
        except Exception as exc:
            if mode != 'file-limit':
                raise
            cause = exc.__cause__
            say(f'failed {k} {type(exc).__name__} {exc} {getattr(cause, "sqlite_errorname", None) or type(cause).__name__}')
            try:
                with store.transaction() as tx:
                    write(tx, k + 1)
                say('after committed')
            except Exception as again:
                say(f'after {type(again).__name__} {again}')
            store.close()
            return 0
        if mode == 'after-commit' and k > int(argv[3]):
            say(f'committed {k}')
            sys.stdin.readline()
            return 1
        say(f'acked {k}')
        k += 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
