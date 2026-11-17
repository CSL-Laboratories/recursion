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
``parser(family)``
    Adds exactly those names, each with ``family.add_parser(name, **kwargs)``, which takes what
    argparse's own ``add_parser`` takes (``help``, ``aliases`` and the rest) and returns the new
    command's parser for the family to give its arguments to.

    A family receives an add-parser facade that can create only its declared names. It cannot obtain
    or mutate existing parsers or their actions. The table verifies that every declared name was added
    once. Existing parser behavior is not authenticated by rendered help text.

    An alias is a name too: it is declared in ``names`` like any other. An option on another family's
    command (``--workspace`` on ``start``, say) is a request to that command's keeper. This is an
    ownership boundary between lane modules that cooperate, not a sandbox against Python reflection.
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


class FamilyParsers:
    """What a family's ``parser`` is given: it can add the family's declared names and nothing else."""

    def __init__(self, target, names) -> None:
        self._target, self._names, self.added = target, frozenset(names), set()

    def add_parser(self, name, **kwargs):
        claimed = {name, *kwargs.get('aliases', ())}
        if not claimed <= self._names or claimed & self.added:
            raise CommandTableError('parser claimed an undeclared or duplicate name')
        parser = self._target.add_parser(name, **kwargs)
        self.added.update(claimed)
        return parser


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

        Each family is handed a ``FamilyParsers`` over them, not the subparsers themselves. A family that adds
        a name it did not register, adds one twice, leaves one out, or reaches for anything else is refused.
        """
        for names, parser, _ in self._families:
            family = FamilyParsers(subparsers, names)
            try:
                parser(family)
            except CommandTableError:
                raise
            except Exception as exc:  # noqa: BLE001 - the facade has no way to another parser; whatever it tried failed
                raise CommandTableError(f'commands {names!r}: the parser failed: {type(exc).__name__}: {exc}') from exc
            if family.added != set(names):
                raise CommandTableError('parser did not add every declared name')

    def dispatch(self, args, client) -> int:
        """Run the registered command ``args.command``. Only called for a name that is in the table."""
        status = self._dispatch[args.command](args, client)
        if type(status) is not int:
            raise CommandTableError(f'command {args.command!r} returned {status!r}, not an exit status')
        return status
