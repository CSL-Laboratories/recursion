"""Lane KEYS' registrant: what ``control/hooks.py`` (SPINE's seams) calls once, at composition.

``register(hooks)`` registers the credential source into seam S7 (``hooks.set('credentials', ...)``) and the
``key`` command family into the command table (S2). It only registers: it opens nothing, starts no thread
and reads no state; the store is first read when an API-key route asks for its key.

``control.hooks.compose()`` calls it, and SPINE's seams are on master, but nothing outside them calls ``compose()``
yet: until the daemon and ``recursion-control`` call the composed hooks, the daemon still hands both engine
adapters ``no_custody``, every API-key route still refuses "no credential", and ``recursion-control`` has no
``key`` command.
"""
from __future__ import annotations

from recursion_core.control.commands import key as key_commands
from recursion_core.keys.store import credentials


def register(hooks) -> None:
    commands = hooks.commands       # a hooks object with no command table fails here, before anything is set
    hooks.set('credentials', credentials)
    commands.register(key_commands.NAMES, key_commands.parser, key_commands.dispatch)
