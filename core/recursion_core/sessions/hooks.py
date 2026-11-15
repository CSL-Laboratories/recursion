# Model: Claude Opus 5.5 (claude-opus-5-5), lane SESSIONS.
"""The registrant of lane SESSIONS: ``recursion_core.sessions.hooks``, a name on ``control/hooks.py``'s fixed list.

``register(hooks)`` REGISTERS NOTHING, on purpose. ``compose()`` imports this module by that name in every
composition once it exists, so whatever ``register`` registered would be live wherever the seams are wired.
The lane's start note says: "Write sessions/hooks.py; it registers nothing until the seams merge", and "You
register nothing live: no row in a table, no factory in the engine table, no command, no hook a running daemon
would call." So ``register`` returns without touching ``hooks``, and ``control/tests/test_seams.py``'s
``test_nothing_is_installed_today_and_the_hooks_compose_with_their_defaults`` still holds with this file in
place (this lane's tests check it too).

``install(hooks, sessions)`` is what ``register`` will do once the seams have merged and the lane's adapters to
its ports exist: register the method handlers (S1) and the lane's service (S13). Today only the tests call it,
on a ``Hooks()`` of their own, as SPINE's seam file says a lane tests what it registers. Two things it cannot
do yet and that are questions for SPINE and EVENTS (questions.md): declare the lane's tables at composition
(no hook gives a registrant the store before services start; ``install`` takes a store that already has them),
and give the method rows a readiness service (the published rows have ``service: null``, and registering
another service name fails composition).
"""
from __future__ import annotations


def register(hooks) -> None:
    """Registers nothing until the seams merge (see the module's text)."""
    return None


def install(hooks, sessions):
    """Register the lane's handlers and its service on ``hooks``; returns the service. For tests only, until the
    seams merge. ``sessions`` is a ``service.Sessions`` over a store whose tables are declared."""
    from recursion_core.control.methods import session as session_methods
    from recursion_core.sessions.service import SessionsService

    session_methods.register(hooks.methods, sessions)
    service = SessionsService(sessions)
    hooks.service(service)
    return service
