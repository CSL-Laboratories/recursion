"""The person confirms each sensitive use of a plugin written by a model, by default.

A plugin's origin is ``person``, ``model`` or ``unknown``; ``unknown`` counts as
``model``, and ``None`` (no origin recorded yet) is ``unknown``. Until the install record
carries an origin, every plugin is ``unknown``, so every use below asks.

``decide`` is one pure function. It answers ``"ask"`` or ``"go"`` for one use of a handle:

* ``credential``: ask on each use;
* ``network``: ask on the first use per plugin and destination since the daemon started,
  then go for that plugin and that destination until the daemon starts again, unless the
  person refused this use (below); an approval given for one plugin never lets another
  plugin reach that destination unasked;
* ``process``: ask on each start;
* origin ``person``: go (the grant suffices), whatever was refused before;
* lifted by the person for this plugin: go, whatever was refused before.

The last two are go paths of their own: a lift is the person's own word for that plugin,
and a plugin the person wrote asks nothing, so neither consults a veto.

``confirm`` acts on that answer. On ``"ask"`` it goes through the core's existing
``ApprovalGate``, unchanged: ``request`` draws the use in the approval area, the caller's
``wait`` returns once the person decided or the time passed, and ``take`` consumes the one
approval. A refusal or a time-out of that approval refuses the use with the approval's
own refusal, raised as the gate's ``take`` raises it: today ``approval_not_granted`` with
the approval's state (``vetoed`` after a refusal, ``pending`` when the person has not
answered in time), or ``approval_expired`` once the gate has retired it. The approval is an ordinary ``csl-effect/vNext`` effect whose kind
names the handle and whose payload names the handle and the target, so the person sees
which reference, destination or program is asked. The target is a name, never a secret:
a network target that carries a user, a password, or a query or fragment key that looks
like a secret is refused with ``target_holds_secret`` before anything is drawn.

Two rules stand side by side. An ALLOW is kept per plugin and destination: the approval
the person was shown named one plugin, so it holds for that plugin only, in every run and
connection. A VETO is per use (the plugin, the destination, the run and the connection,
which the approval's digest binds) and is final for that use until the daemon starts
again: once the person refuses a use, that same use is refused with ``approval_vetoed``
without asking again, whatever any other run of that plugin was allowed, and nothing that
happens after the refusal, in any order and on any thread, keeps an allow for it.

``NetworkFirstUses`` is the record of the allows, and the gate keeps them: an allow is
kept only as the effect of the person's yes to one approval. ``confirm`` asks the gate
for that approval with the record's place for its allow, and the gate's ``take`` keeps
the allow in the same critical section as its veto check, so a refusal lands either
before the store (then nothing is kept and the use is refused) or after it, and nothing
can come between the take and the store. ``record`` keeps the allow of an approval
taken earlier, once per approval. The gate counts only the allows it kept, never a list
a caller holds. An allow stands only while at least one approval behind it is not
vetoed, and ``decide`` lets a network use go only when it names the plugin, the run and
the connection, an allow stands for that plugin and destination and no veto covers that
use (``ApprovalGate.standing``, the same critical section); otherwise it asks, and the
gate refuses a vetoed use without drawing it. The digest of each use asked about is
computed once and kept beside the allow. A record answers for one gate only. It lives in
memory only: the daemon makes a new, empty one at each start, so a restart asks again.
The plugin and the destination are compared exactly as given.

A use let go before a refusal is not withdrawn by it, however long the caller holds the
go answer before it acts, unless the caller makes the last check: ``confirm_use`` hands
back a ``Use`` whose ``check()``, called just before the act, refuses with
``approval_vetoed`` a use the person refused since the answer.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Container
from urllib.parse import parse_qsl, urlsplit

from .approval import ApprovalGate, EFFECT_SCHEMA, SCOPE_FIELDS
from .canonical import canonical, parse
from .errors import Refused

__all__ = ["ASK", "GO", "HANDLES", "ORIGINS", "NetworkFirstUses", "Use", "confirm", "confirm_use",
           "confirmation_effect", "decide"]

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

# The words a person reads with a refused use and a refused target. Never secret.
VETOED_WORDS = ("you refused this use earlier in this run and connection; nothing was done, "
                "and it stays refused until the daemon starts again")
SECRET_TARGET_WORDS = ("the target holds what looks like a secret (a user, a password or a secret-looking "
                       "query key); nothing was asked or done: name only the place, such as "
                       "api.example.com:443, and pass the secret as a credential")
# Parts of a query or fragment key that make it look like a secret (compared in lower
# case, as a part of the key). A refusal on a harmless key only asks the plugin to name
# the place alone; it never lets a use go. A list of words, not a figure.
SECRET_KEY_PARTS = ("token", "secret", "password", "passwd", "pwd", "key", "auth", "sig",
                    "credential", "session", "cookie", "bearer")


class NetworkFirstUses:
    """The network allows the person gave since this daemon started, per plugin and destination.

    In memory only, and only through one gate: the first ``record`` (or ``confirm``) binds
    the record to that gate, and every store and every answer runs inside the gate's
    critical section. ``(plugin_id, destination) in record`` says whether an allow stands
    for that plugin and destination (some approval behind it is not vetoed);
    ``destination in record`` says whether one stands for any plugin. ``stands`` answers
    for one use.
    """

    def __init__(self) -> None:
        self._bind_lock = threading.Lock()
        self._gate: ApprovalGate | None = None
        # The record's own book in its gate: the gate keeps the allows under it, each as the
        # effect of the person's yes; the record keeps no list of its own.
        self._book = object()

    @property
    def _allows(self) -> dict[tuple[str, str], set[str]]:
        """A copy of the allows the gate kept for this record, (plugin, destination) -> digests.
        For status and tests: writing into it changes nothing, and it lets nothing go."""
        gate = self._bound()
        return {} if gate is None else gate._kept_copy(self._book)

    def _key(self, plugin_id: str, destination: str) -> tuple[object, tuple[str, str]]:
        return self._book, (plugin_id, destination)

    def _use(self, plugin_id: str, destination: str, run_id: str, connection: str):
        return (run_id, connection), lambda: confirmation_effect("network", destination, plugin_id=plugin_id,
                                                                 run_id=run_id, connection=connection)

    def _bind(self, gate: ApprovalGate) -> None:
        with self._bind_lock:
            if self._gate is None:
                self._gate = gate
            elif self._gate is not gate:
                raise Refused("first_uses_other_gate",
                              "this record of allows answers for another approval gate; nothing was done")

    def _bound(self) -> ApprovalGate | None:
        with self._bind_lock:
            return self._gate

    def record(self, gate: ApprovalGate, approval_id: str, effect: dict) -> None:
        """Keep an allow for one network first use the gate approved and that was taken.

        The one way in. Refuses, and keeps nothing, when the effect is not a network first
        use, when ``approval_id`` is not an approval of this gate for exactly this effect,
        when it was not taken, or when the person vetoed this use, checked in the same
        critical section as the store.
        """
        try:
            copy = parse(canonical(effect))  # keyed on a private copy, never the caller's dict
        except Refused:
            raise Refused("effect_noncanonical") from None
        payload = copy.get("payload") if type(copy) is dict else None
        if type(payload) is not dict or copy.get("kind") != _KINDS["network"] \
                or payload.get("handle") != "network" or type(payload.get("target")) is not str \
                or type(copy.get("plugin_id")) is not str:
            raise Refused("allow_not_network")
        self._bind(gate)
        gate.keep(approval_id, copy, self._key(copy["plugin_id"], payload["target"]))

    def stands(self, plugin_id: str, destination: str, *, run_id: str, connection: str) -> bool:
        """Whether this one use may go unasked: an allow stands and no veto covers the use."""
        gate = self._bound()
        if gate is None:
            return False
        use, effect = self._use(plugin_id, destination, run_id, connection)
        return gate.standing(self._key(plugin_id, destination), use, effect)

    def _use_digest(self, plugin_id: str, destination: str, run_id: str, connection: str) -> str:
        use, effect = self._use(plugin_id, destination, run_id, connection)
        return self._bound()._use_digest(self._key(plugin_id, destination), use, effect)[0]

    def __contains__(self, key: object) -> bool:
        gate = self._bound()
        if gate is None:
            return False
        if type(key) is tuple:
            return gate.standing((self._book, key))
        return gate.standing_any(self._book, lambda entry: entry[1] == key)


def _check_destination(target: str) -> None:
    """Refuse a network target that carries what looks like a secret."""
    try:
        split = urlsplit(target if "://" in target else "//" + target)
    except ValueError:
        raise Refused("target_holds_secret", SECRET_TARGET_WORDS) from None
    if "@" in split.netloc:
        raise Refused("target_holds_secret", SECRET_TARGET_WORDS)
    for part in (split.query, split.fragment):
        for name, _ in parse_qsl(part, keep_blank_values=True):
            if any(word in name.lower() for word in SECRET_KEY_PARTS):
                raise Refused("target_holds_secret", SECRET_TARGET_WORDS)


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
           first_uses: Container[object], plugin_id: str | None = None, run_id: str | None = None,
           connection: str | None = None) -> str:
    """Answer ``"ask"`` or ``"go"`` for one use by the plugin ``plugin_id``. It changes nothing.

    A network use goes only when it names the plugin, the run and the connection and the
    ``NetworkFirstUses`` record says an allow stands for it with no veto covering it.
    """
    origin = _check(origin, handle, target, lifted)
    if origin == "person":
        return GO
    if lifted:
        return GO
    if handle == "network" and plugin_id is not None and run_id is not None and connection is not None \
            and isinstance(first_uses, NetworkFirstUses) \
            and first_uses.stands(plugin_id, target, run_id=run_id, connection=connection):
        return GO
    return ASK


def confirmation_effect(handle: str, target: str, *, plugin_id: str, run_id: str, connection: str) -> dict:
    """The effect the approval area draws for one use."""
    return {
        "schema": EFFECT_SCHEMA, "kind": _KINDS[handle], "plugin_id": plugin_id, "run_id": run_id,
        "connection": connection, "scope": {field: _NO_SCOPE for field in SCOPE_FIELDS},
        "currency": _NO_CURRENCY, "amount_minor": 0, "payload": {"handle": handle, "target": target},
    }


class Use:
    """One use let go, as ``confirm_use`` answers it: why it may go, and the last check before the act.

    The go answer is a point in time. ``check()`` asks the gate again, under its lock and
    just before the caller acts, whether the person refused this use since the answer: it
    raises ``approval_vetoed`` if so (then the caller does not act) and returns ``reason``
    otherwise. A use that goes because its plugin's origin is ``person`` or because it is
    lifted consults no veto, as ``decide`` says, so its check only returns ``reason``.
    """

    __slots__ = ("reason", "_gate", "_digest")

    def __init__(self, reason: str, gate: ApprovalGate | None, digest: Callable[[], str] | None) -> None:
        self.reason = reason
        self._gate = gate
        self._digest = digest

    def check(self) -> str:
        if self._gate is not None and self._digest is not None and self._gate.is_vetoed(self._digest()):
            raise Refused("approval_vetoed", VETOED_WORDS)
        return self.reason


def confirm_use(gate: ApprovalGate, wait: Callable[[str, str], None], *, origin: str | None, handle: str,
                target: str, plugin_id: str, run_id: str, connection: str, lifted: bool = False,
                first_uses: NetworkFirstUses) -> Use:
    """Let one use go, asking the person first when ``decide`` says so, and hand back its ``Use``.

    Raises as ``confirm`` does. The caller calls the ``Use``'s ``check()`` just before it acts.
    """
    origin = _check(origin, handle, target, lifted)
    if handle == "network":
        _check_destination(target)
        first_uses._bind(gate)
    if decide(origin, handle, target, lifted=lifted, first_uses=first_uses, plugin_id=plugin_id,
              run_id=run_id, connection=connection) == GO:
        if origin == "person":
            return Use(PERSON, None, None)
        if lifted:
            return Use(LIFTED, None, None)
        return Use(KNOWN_DESTINATION, gate,
                   lambda: first_uses._use_digest(plugin_id, target, run_id, connection))
    effect = confirmation_effect(handle, target, plugin_id=plugin_id, run_id=run_id, connection=connection)
    keep = first_uses._key(plugin_id, target) if handle == "network" else None
    try:
        approval_id, digest = gate.request(effect, keep=keep)
        wait(approval_id, digest)
        # For a network use the take also keeps the allow of this yes, in the same critical
        # section as its veto check: the take and the store are one step of the gate.
        gate.take(approval_id, effect)
        if gate.is_vetoed(digest):
            # A refusal of this same use that the person gave after the take returned is
            # final too, as the core's commit section asks it; an allow kept for it stands no more.
            raise Refused("approval_vetoed")
    except Refused as refused:
        if refused.code == "approval_vetoed":
            raise Refused("approval_vetoed", VETOED_WORDS) from None
        raise
    return Use(APPROVED, gate, lambda: digest)


def confirm(gate: ApprovalGate, wait: Callable[[str, str], None], *, origin: str | None, handle: str,
            target: str, plugin_id: str, run_id: str, connection: str, lifted: bool = False,
            first_uses: NetworkFirstUses) -> str:
    """Let one use go, asking the person first when ``decide`` says so.

    Returns why the use may go: ``"origin person"``, ``"lifted by the person"``,
    ``"destination approved since the daemon started"`` or ``"approved by the person"``.
    Raises ``target_holds_secret`` for a network target that looks like it carries a
    secret, before anything is drawn; the gate's ``Refused`` when the approval is refused,
    timed out or not given; and ``approval_vetoed`` when the person refused this same use,
    before this call or during it: then no allow stands for it.
    """
    return confirm_use(gate, wait, origin=origin, handle=handle, target=target, plugin_id=plugin_id,
                       run_id=run_id, connection=connection, lifted=lifted, first_uses=first_uses).reason
