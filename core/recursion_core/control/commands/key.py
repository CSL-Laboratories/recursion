"""The ``key`` command family of ``recursion-control`` (seam S2): add, list, remove and audit user-entered keys.

Registered by ``recursion_core.keys.hooks`` into the command table once SPINE's seams land. The actions run in
the CLI's own process against the user's own key store and make no daemon call, so ``client`` is not used: a
key never crosses the control socket. The actions themselves are in ``recursion_core.keys.cli``.
"""
from __future__ import annotations

from recursion_core.keys import cli

NAMES = ('key',)


def parser(family) -> None:
    command = family.add_parser('key', help='user-entered API keys: add, list, remove, audit',
                                description="Recursion's user-entered API keys, kept in the OS keychain or an "
                                            'owner-only file on a host with none. A key is never read from the '
                                            'command line.')
    cli.configure(command)


def dispatch(args, client) -> int:
    return cli.run(args, getattr(args, 'out', None), getattr(args, 'err', None))
