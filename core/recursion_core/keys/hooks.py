"""Lane KEYS' registrant: what ``control/hooks.py`` (SPINE's seams) calls once, at composition.

``register(hooks)`` registers:

- the credential source into seam S7 (``hooks.set('credentials', ...)``);
- the ``key.list`` handler into the method table (S1; a read: no receipt, no service);
- the ``key`` command family into the command table (S2).

It only registers: it opens nothing, starts no thread and reads no state; the store is first read when an
API-key route asks for its key or a client calls ``key.list``.

Until SPINE's seams pull request lands, nothing calls this function and nothing is registered: today's daemon
still hands both engine adapters ``no_custody``, and every API-key route still refuses "no credential".
"""
from __future__ import annotations

from recursion_core.control.commands import key as key_commands
from recursion_core.control.methods import key as key_methods
from recursion_core.keys.store import credentials


def register(hooks) -> None:
    hooks.set('credentials', credentials)
    hooks.methods.register('key.list', key_methods.key_list)
    hooks.commands.register(key_commands.NAMES, key_commands.parser, key_commands.dispatch)
