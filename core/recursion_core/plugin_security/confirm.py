"""The person confirms each sensitive use of a plugin written by a model, by default.

A plugin's origin is ``person``, ``model`` or ``unknown``; ``unknown`` counts as
``model``, and ``None`` (no origin recorded yet) is ``unknown``. Until the install record
carries an origin, every plugin is ``unknown``, so every use below asks.

``decide`` is one pure function. It answers ``"ask"`` or ``"go"`` for one use of a handle:

* ``credential``: ask on each use;
* ``network``: ask on the first use per plugin and destination since the daemon started,
  then go for that plugin and that destination until the daemon starts again; an approval
  given for one plugin never lets another plugin reach that destination unasked;
* ``process``: ask on each start;
* origin ``person``: go (the grant suffices);
* lifted by the person for this plugin: go.

``confirm`` acts on that answer. On ``"ask"`` it goes through the core's existing
``ApprovalGate``, unchanged: ``request`` draws the use in the approval area, the caller's
``wait`` returns once the person decided or the time passed, and ``take`` consumes the one
approval. A refusal or a time-out of that approval refuses the use with the approval's
own refusal, raised as the gate's ``take`` raises it: today ``approval_not_granted`` with
the approval's state (``vetoed`` after a refusal, ``pending`` when the person has not
answered in time), or ``approval_expired`` once the gate has retired it. The approval is an ordinary ``csl-effect/vNext`` effect whose kind
names the handle and whose payload names the handle and the target, so the person sees
which reference, destination or program is asked. The target is a name, never a secret.

The gate's own rule applies: a veto is final for that effect until the daemon starts
again, so once the person refuses a use, the same use by the same plugin in the same run
and connection is refused without asking again.

``NetworkFirstUses`` is the record of the network first uses the person approved since
the daemon started, each held as the pair of plugin and destination: the approval the
person was shown named one plugin, so it holds for that plugin only. ``decide`` lets a
network use go only when it is given the asking plugin's ``plugin_id`` and that pair is
recorded; without a ``plugin_id`` it asks. The record lives in memory only: the daemon
makes a new, empty one at each start, so a restart asks again. A pair is recorded only
after its approval was taken, never on a refusal or a time-out. The plugin and the
destination are compared exactly as given.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Container

from .approval import ApprovalGate, EFFECT_SCHEMA, SCOPE_FIELDS

__all__ = ["ASK", "GO", "HANDLES", "ORIGINS", "NetworkFirstUses", "confirm", "confirmation_effect", "decide"]

ASK = "ask"
GO = "go"
ORIGINS = frozenset({"person", "model", "unknown"})
HANDLES = frozenset({"credential", "network", "process"})

# The effect kind drawn for each handle.
_KINDS = {"credential": "confirm.credential_use", "network": "confirm.network_first_use",
          "process": "confirm.process_start"}
# A confirmation moves no money and names no account: the scope fields the effect schema
# requires are "none", the currency is ISO 4217 "XXX" (no currency), the amount is 0.
_NO_SCOPE = "none"
_NO_CURRENCY = "XXX"

LIFTED = "lifted by the person"
PERSON = "origin person"
APPROVED = "approved by the person"
KNOWN_DESTINATION = "destination approved since the daemon started"


class NetworkFirstUses:
    """The (plugin, destination) pairs the person approved since this daemon started.

    In memory only. ``(plugin_id, destination) in record`` says whether that plugin's first
    use of that destination was approved; ``destination in record`` says whether any
    plugin's was.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: set[tuple[str, str]] = set()

    def __contains__(self, key: object) -> bool:
        with self._lock:
            if type(key) is tuple:
                return key in self._seen
            return any(destination == key for _, destination in self._seen)

    def record(self, plugin_id: str, destination: str) -> None:
        with self._lock:
            self._seen.add((plugin_id, destination))


def _check(origin: object, handle: object, target: object, lifted: object) -> str:
    if origin is None:
        origin = "unknown"
    if origin not in ORIGINS:
        raise ValueError("origin must be 'person', 'model', 'unknown' or None")
    if handle not in HANDLES:
        raise ValueError("handle must be 'credential', 'network' or 'process'")
    if type(target) is not str or not target:
        raise TypeError("target must be a non-empty str")
    if type(lifted) is not bool:
        raise TypeError("lifted must be a bool")
    return origin


def decide(origin: str | None, handle: str, target: str, *, lifted: bool = False,
           first_uses: Container[object], plugin_id: str | None = None) -> str:
    """Answer ``"ask"`` or ``"go"`` for one use by the plugin ``plugin_id``. Pure: it changes nothing."""
    origin = _check(origin, handle, target, lifted)
    if origin == "person":
        return GO
    if lifted:
        return GO
    if handle == "network" and plugin_id is not None and (plugin_id, target) in first_uses:
        return GO
    return ASK


def confirmation_effect(handle: str, target: str, *, plugin_id: str, run_id: str, connection: str) -> dict:
    """The effect the approval area draws for one use."""
    return {
        "schema": EFFECT_SCHEMA, "kind": _KINDS[handle], "plugin_id": plugin_id, "run_id": run_id,
        "connection": connection, "scope": {field: _NO_SCOPE for field in SCOPE_FIELDS},
        "currency": _NO_CURRENCY, "amount_minor": 0, "payload": {"handle": handle, "target": target},
    }


def confirm(gate: ApprovalGate, wait: Callable[[str, str], None], *, origin: str | None, handle: str,
            target: str, plugin_id: str, run_id: str, connection: str, lifted: bool = False,
            first_uses: NetworkFirstUses) -> str:
    """Let one use go, asking the person first when ``decide`` says so.

    Returns why the use may go: ``"origin person"``, ``"lifted by the person"``,
    ``"destination approved since the daemon started"`` or ``"approved by the person"``.
    Raises the gate's ``Refused`` when the approval is refused, timed out or not given.
    """
    origin = _check(origin, handle, target, lifted)
    if decide(origin, handle, target, lifted=lifted, first_uses=first_uses, plugin_id=plugin_id) == GO:
        if origin == "person":
            return PERSON
        if lifted:
            return LIFTED
        return KNOWN_DESTINATION
    effect = confirmation_effect(handle, target, plugin_id=plugin_id, run_id=run_id, connection=connection)
    approval_id, digest = gate.request(effect)
    wait(approval_id, digest)
    gate.take(approval_id, effect)
    if handle == "network":
        first_uses.record(plugin_id, target)
    return APPROVED
