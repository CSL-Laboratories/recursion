"""The engine table (seam S3): the engines the product names, and the ones a daemon can build an adapter for.

Two things live here and nowhere else.

``CATALOGUE`` is the list of engine NAMES, ten of them. A name that is only in the catalogue changes no
answer: nothing is launched for it, no route may name it, and the route inventory does not list it.

An ``EngineTable`` holds the ACTIVE FACTORIES of one composition. A new table is seeded with exactly the
two engines the daemon builds today, in today's order, and each seeded factory is the adapter class
itself, so it is called with exactly today's arguments::

    factory(launcher, clock, credentials, environ, **options) -> adapter

``options`` is ``{"binary": <absolute path>}`` when the route entry names a binary and empty otherwise.
The three readers of "which engines are wired" take the names from the table and give what they give
today while nothing else is registered: the route file's loader, the route inventory, and the loop
that builds the adapters.

An engine lane registers its factory from its registrant module's ``register(hooks)``, through
``hooks.engines.register(name, factory)``. That happens once, at composition, before anything is
served; ``freeze()`` then closes the table, so nothing changes it while calls are served. A second
registration for a name fails composition, with one exception made on purpose: the two seeded
factories may each be replaced ONCE by a registration that says ``replace=True`` (the lanes that build
the persistent drivers for those two engines). A name that is not in the catalogue cannot be registered.

A factory alone does not make an engine launchable. The launcher refuses an engine that has no entry in
its own profile table (``daemon.launcher.ENGINE_PROFILES``); that entry is asked of that table's keeper.

Nothing here imports an adapter until a table is made, so this module can be imported from anywhere in
``routes`` without a cycle.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator

CATALOGUE = ('claude_code', 'codex', 'opencode', 'grok', 'antigravity', 'cursor', 'gemini_cli', 'pi', 'hermes',
             'recursion_loop')
SEEDED = ('claude_code', 'codex')   # the engines a daemon builds an adapter for today, in today's order


class EngineTableError(Exception):
    """A registration the table refuses. Raised at composition; the daemon does not start."""


def _seed() -> list:
    from recursion_core.routes import ClaudeCodeAdapter, CodexAdapter   # today's pair, today's order
    return [('claude_code', ClaudeCodeAdapter), ('codex', CodexAdapter)]


class EngineTable:
    """The active factories of one composition. Not shared between compositions."""

    def __init__(self) -> None:
        self._factories: dict[str, Callable] = dict(_seed())
        assert tuple(self._factories) == SEEDED
        self._replaced: set[str] = set()
        self._frozen = False

    def register(self, name: str, factory: Callable, *, replace: bool = False) -> None:
        """Add the factory of an engine, or replace a seeded one on purpose (``replace=True``, once)."""
        if self._frozen:
            raise EngineTableError(f'engine {name!r}: the table is closed; register at composition')
        if name not in CATALOGUE:
            raise EngineTableError(f'engine {name!r} is not in the catalogue')
        if not callable(factory):
            raise EngineTableError(f'engine {name!r}: the factory is not callable')
        if replace:
            if name not in SEEDED or name in self._replaced:
                raise EngineTableError(f'engine {name!r}: only a seeded factory can be replaced, and only once')
            self._replaced.add(name)
        elif name in self._factories:
            raise EngineTableError(f'engine {name!r} already has a factory')
        self._factories[name] = factory   # a replaced name keeps its place in the order

    def freeze(self) -> None:
        self._frozen = True

    def names(self) -> tuple:
        """The engines with a factory, in order: today's two first, then each registered one."""
        return tuple(self._factories)

    def items(self) -> Iterator[tuple]:
        """``(name, factory)`` in the same order."""
        return iter(tuple(self._factories.items()))

    def __contains__(self, name) -> bool:
        return name in self._factories
