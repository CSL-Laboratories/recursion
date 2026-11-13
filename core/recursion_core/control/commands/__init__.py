"""The command table (seam S2): the commands other lanes add to ``recursion-control``.

The five commands the CLI has today (``list``, ``status``, ``stop``, ``start``, ``conditions``) stay in
``control/cli.py`` exactly as they are: their names, their paging, their JSON output and their exit
statuses do not change, and they are not in this table.

A lane adds a FAMILY of commands with one module of its own under this package
(``control/commands/<family>.py``), which its registrant imports from ``register(hooks)`` and hands
to the table::

    hooks.commands.register(names, parser, dispatch)

``names``
    The top-level command names the family adds: a tuple of one or more.
``parser(subparsers)``
    Adds exactly those names to the CLI's argparse subparsers, with their own arguments. It adds
    nothing else and changes no other command: an option on another family's command (``--workspace``
    on ``start``, say) is a request to that command's keeper, and the table refuses it.
``dispatch(args, client) -> exit status``
    Runs one parsed command. ``args`` is the argparse namespace: ``args.command`` is the name, and
    ``args.out`` and ``args.err`` are the streams to print to. ``client`` is the control client the CLI
    was given: anything with ``call(method, params) -> dict``. It returns the process's exit status, an
    int; the CLI's own are 0 ok, 1 an API error, 2 a usage error, 3 pending, 4 unknown.

Only a registered name is dispatched. A name nobody registered is rejected by argparse as a usage error,
as today; it is never treated as a start.

Registration happens once, when the hooks are composed, before any command is parsed; ``freeze()`` then
closes the table. Two families that claim one name, or a family that claims one of the CLI's five,
fail composition.
"""
from __future__ import annotations

from collections.abc import Callable

BUILT_IN = ('list', 'status', 'stop', 'start', 'conditions')   # control/cli.py's own; never in this table


class CommandTableError(Exception):
    """A registration, or a parser, that the table refuses. The CLI does not run a command after one."""


class CommandTable:
    """The command families of one composition, in registration order."""

    def __init__(self) -> None:
        self._families: list[tuple] = []          # (names, parser, dispatch)
        self._dispatch: dict[str, Callable] = {}  # command name -> its family's dispatch
        self._frozen = False

    def register(self, names, parser: Callable, dispatch: Callable) -> None:
        if self._frozen:
            raise CommandTableError('the command table is closed; register at composition')
        if isinstance(names, str) or not names or not all(isinstance(name, str) and name for name in names):
            raise CommandTableError('a command family names its commands: a tuple of one or more names')
        names = tuple(names)
        if len(set(names)) != len(names):
            raise CommandTableError(f'a command family names {names!r}: one name twice')
        if not callable(parser) or not callable(dispatch):
            raise CommandTableError(f'commands {names!r}: parser and dispatch are functions')
        for name in names:
            if name in BUILT_IN:
                raise CommandTableError(f'command {name!r} is the CLI\'s own and cannot be registered')
            if name in self._dispatch:
                raise CommandTableError(f'command {name!r} is already registered')
        self._families.append((names, parser, dispatch))
        for name in names:
            self._dispatch[name] = dispatch

    def freeze(self) -> None:
        self._frozen = True

    def names(self) -> tuple:
        return tuple(self._dispatch)

    def __contains__(self, name) -> bool:
        return name in self._dispatch

    def add_parsers(self, subparsers) -> None:
        """Let each family add its commands to the CLI's subparsers, after the CLI added its own.

        A family that adds a name it did not register, leaves one out, or changes a command that was
        already there is refused.
        """
        for names, parser, _ in self._families:
            before = {name: sub.format_help() for name, sub in subparsers.choices.items()}
            parser(subparsers)
            after = subparsers.choices
            added = set(after) - set(before)
            if added != set(names):
                raise CommandTableError(f'commands {names!r}: the parser added {sorted(added)!r}')
            changed = sorted(name for name, text in before.items() if after[name].format_help() != text)
            if changed:
                raise CommandTableError(f'commands {names!r}: the parser changed {changed!r}, which another keeps')

    def dispatch(self, args, client) -> int:
        """Run the registered command ``args.command``. Only called for a name that is in the table."""
        status = self._dispatch[args.command](args, client)
        if type(status) is not int:
            raise CommandTableError(f'command {args.command!r} returned {status!r}, not an exit status')
        return status
