"""``python -m recursion_core.keys``: the key commands without a daemon (a client entry, not the kernel).

It runs in the user's own process against the user's own store and calls no daemon method. The same four
actions as ``recursion-control key`` (``cli.py``). Its parser never repeats an argument it could not use in
an error, so a key typed on the command line by mistake is not printed back.
"""
from __future__ import annotations

import argparse
import sys

from recursion_core.keys import cli


class _Usage(Exception):
    pass


class QuietParser(argparse.ArgumentParser):
    """An ``ArgumentParser`` whose usage errors are returned, not printed with an exit. What reaches ``error`` has
    already been made safe by ``cli.configure``, which keeps argparse's wording only where it repeats nothing
    typed (the same for this entry and for ``recursion-control key``)."""

    def error(self, message):
        raise _Usage(message)


def parser() -> QuietParser:
    p = QuietParser(prog='python -m recursion_core.keys',
                    description="Recursion's user-entered API keys: kept in the OS keychain, or an owner-only file "
                                'on a host with none. A key is never read from the command line.')
    cli.configure(p)
    return p


def main(argv=None, *, out=None, err=None, **for_tests) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        args = parser().parse_args(argv)
    except _Usage as exc:
        print(f'usage error: {exc}; see --help', file=err)
        return 2
    return cli.run(args, out, err, **for_tests)


if __name__ == '__main__':
    sys.exit(main())
