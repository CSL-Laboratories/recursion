"""The method table (seam S1): the control methods other lanes answer, and the one dispatcher that runs them.

Six methods are answered by the code that answers them today and are never in this table: ``run.start``,
``run.status``, ``run.list``, ``run.stop``, ``route.list`` and ``plugin.list`` (``BUILT_IN``). A handler
cannot replace one.

``METHODS`` lists every method of the published table (``schemas/control/v1/methods.table.v1.json``) with
the three facts the dispatcher needs: its scope, whether it mutates, and how it is keyed. A name that is
not in ``METHODS`` is answered ``method_unknown``, as today, a name some plan has reserved included. A
name that is in ``METHODS`` and has no handler in this daemon is answered ``unsupported`` with detail
``not_implemented``. Adding a row is a request to the lane that keeps this file.

A lane registers a handler from its registrant module's ``register(hooks)``::

    hooks.methods.register("session.turn", handler, service="sessions")

    handler(ctx, params) -> answer          a dict with a "code"

``ctx`` (``Context``) carries:

``ctx.principal``
    Who is calling, derived by the server from the way the request came in. It is never read from params.
``ctx.tx``
    For a method keyed by ``command_id``: the open transaction of the event store, owned by the
    DISPATCHER. The handler makes its state changes and appends its events through it and opens no
    transaction of its own. For every other method it is ``None``, and a handler that needs to read
    consistently opens its own.
``ctx.after_commit(fn)``
    Schedules ``fn()`` for after the dispatcher's commit is confirmed. See "After the commit" below.
``ctx.call(method, params)``
    Calls another control method as the same principal, inside this dispatch: it passes the authorizer
    again and takes no new client admission. It reaches the built-in methods and the methods that are
    not keyed by ``command_id``. A method keyed by ``command_id`` cannot be called this way (it answers
    ``request_invalid``, detail ``nested_command``): one command has one receipt and one transaction.
    Inside a handler, another lane's change is made through that lane's own interface with ``ctx.tx``.
``ctx.closing``
    A ``threading.Event`` that is set when the daemon begins to close. A handler that waits (a long
    poll) waits on this and returns when it is set.

WHO OWNS THE TRANSACTION. For a method keyed by ``command_id`` the dispatcher does this, in order:

1. With no event store composed, it answers ``unsupported``, detail ``store_absent``, and calls nothing.
2. It takes the command's slot: commands with the same ``(tenant, method, command_id)`` run one at a
   time in this process, so two identical commands sent at once run the handler once.
3. It opens the store's transaction and looks the receipt up IN it. A receipt with the same payload
   digest returns the stored answer: the handler is not called and no callback is staged, whatever
   has changed since. A receipt with another digest answers ``conflict``, detail ``command_id_reused``.
4. On a miss it calls the handler with ``ctx.tx``. The handler checks its own preconditions (a stale
   revision or turn is ``conflict``; a live turn is ``busy``; a missing driver flag is ``unsupported``,
   before any side effect).
5. It stores the handler's answer in the receipt, in the same transaction, and commits.

A handler that RETURNS an answer has accepted the command: the answer is stored and replayed for that id
for good. A handler that must not leave a receipt (the refusal is about this moment, not about the
command: ``busy``, ``unavailable``) raises ``NotAccepted(answer)``: the transaction is rolled back,
nothing is stored, no callback runs, and the caller gets ``answer``; the same id may be sent again. A
handler that raises anything else is rolled back the same way and the caller gets ``unavailable``.

The payload digest is the SHA-256 of the UTF-8 bytes of the params without ``command_id``, written as RFC
8785 writes JSON (keys in UTF-16 order, no spaces, numbers as ECMAScript writes them). Params that have
no such text (a whole number outside the exact range, a number that is not finite, half a surrogate
pair) are ``request_invalid``. The tenant of the identity is the ``tenant`` the params name, or the
empty string for a method whose params name none.

AFTER THE COMMIT. ``ctx.after_commit(fn)`` is a callback of THIS PROCESS and nothing more. After a
confirmed commit the dispatcher calls each ``fn`` once, in the order they were scheduled, outside every
lock. After a rollback it calls none. A callback that raises is logged; it does not change the
committed receipt and does not stop the callbacks after it. A replay from a receipt never calls them.
A callback can be lost when the process exits between the commit and the call, so no lane may make one
the only record that work must happen.

So a handler whose command needs an effect outside the store (on the run registry, the daemon, an
engine, the file system) records its lane's TYPED WORK INTENT through ``ctx.tx``, with a stable
operation id and its recovery state, in the SAME commit as the receipt and its events. Its answer
describes what was committed (accepted, queued), not an outside effect that has not happened yet; the
lane supplies the result or event through which completion, or an unknown outcome, is seen. The lane's
service finds pending work at startup and periodically. A callback may wake that service; recovery does
not need it. Both paths claim the same operation id in a transaction, so they cannot both do the work.
The attempt is committed BEFORE an effect whose outcome could be ambiguous, and its result after. After a
crash an unresolved attempt is ``outcome_unknown`` and is not retried by itself, unless the operation
has a documented rule that makes a repeat safe. The delivery of a queued message (``delivering``, then
the engine's acceptance) is one instance of this contract.

The tables of that work, and the workers that do it and recover it, are each lane's, built with the
event store's helper. This module runs no effect and adds nothing to the run registry.

WHAT THE DISPATCHER NEEDS OF THE EVENT STORE. ``store`` is the event store's transaction helper, or
``None`` when no event store is composed::

    with store.transaction() as tx:      commits on a normal exit and raises if the commit failed;
                                         rolls back when the block raises
        tx.receipt(tenant, method, command_id) -> None, or (digest, answer)
        tx.put_receipt(tenant, method, command_id, digest, answer)

READINESS. A handler registered with ``service="<name>"`` needs that service (seam S13). Until the
service says it is ready, the method answers ``unavailable``, detail ``service_starting``; the check
comes before the command's transaction is opened.

Registration happens once, at composition, before anything is served; ``freeze()`` then closes the
table, so nothing changes it while calls are served. A second handler for one name fails composition.
"""
from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Callable
from typing import NamedTuple

log = logging.getLogger(__name__)

BUILT_IN = ('run.start', 'run.status', 'run.list', 'run.stop', 'route.list', 'plugin.list')
MAX_INT = 2**53 - 1   # the largest whole number every reader of JSON numbers keeps exact
MAX_DEPTH = 64        # of params, for the payload digest


class Row(NamedTuple):
    scope: str          # read | run | approve | manage
    mutating: bool
    idempotency: str    # none | client_key | command_id


def _rows() -> dict:
    read = lambda: Row('read', False, 'none')                 # noqa: E731
    keyed = lambda scope='run': Row(scope, True, 'command_id')  # noqa: E731
    return {
        'run.start': Row('run', True, 'client_key'), 'run.status': read(), 'run.list': read(),
        'run.stop': Row('run', True, 'none'), 'route.list': read(), 'plugin.list': read(),
        'session.start': keyed(), 'session.turn': keyed(), 'session.steer': keyed(), 'session.queue': keyed(),
        'session.interrupt': keyed(), 'session.resume': keyed(), 'session.fork': keyed(),
        'session.export': read(), 'session.list': read(), 'session.status': read(), 'session.stop': keyed(),
        'queue.update': keyed(), 'queue.cancel': keyed(),
        'approval.list': read(), 'approval.open': keyed('approve'), 'approval.answer': keyed('approve'),
        'events.read': read(), 'events.follow': read(),
        'route.explain': read(), 'operator.read': read(), 'budget.show': read(),
        'workspace.diff': read(), 'workspace.checkpoint': keyed(), 'workspace.restore': keyed(),
        'workspace.publish': keyed('manage'),
        'plugin.status': read(), 'plugin.call': keyed(), 'key.list': read(),
        'surface.prefs.get': read(), 'surface.prefs.set': keyed(), 'doctor': read(), 'methods.list': read(),
    }


METHODS: dict = _rows()   # every method of the published table, in its order


class MethodTableError(Exception):
    """A registration the table refuses. Raised at composition; the daemon does not start."""


class NotAccepted(Exception):
    """Raised by a handler to answer WITHOUT accepting the command: rolled back, no receipt, no callback."""

    def __init__(self, answer) -> None:
        super().__init__(str(answer.get('code')) if isinstance(answer, dict) else 'not accepted')
        self.answer = dict(answer)


class NoCanonicalText(ValueError):
    """The value has no RFC 8785 text."""


class Context:
    """What a handler is given. Made by the dispatcher for one call and not kept after it."""

    __slots__ = ('principal', 'tx', 'closing', '_callbacks', '_call')

    def __init__(self, principal, tx, closing, call) -> None:
        self.principal, self.tx, self.closing = principal, tx, closing
        self._callbacks: list = []
        self._call = call

    def after_commit(self, fn: Callable[[], object]) -> None:
        if not callable(fn):
            raise TypeError('after_commit takes a function of no arguments')
        self._callbacks.append(fn)

    def call(self, method: str, params=None) -> dict:
        return self._call(method, params)


# ---------------------------------------------------------------------------------- the payload digest
_SHORT = {0x08: '\\b', 0x09: '\\t', 0x0A: '\\n', 0x0C: '\\f', 0x0D: '\\r', 0x22: '\\"', 0x5C: '\\\\'}


def _number(number) -> str:
    """A number as ECMAScript writes it (RFC 8785, 3.2.2.3): 1.0 is 1, 1e-7 stays 1e-7, 1e21 is 1e+21."""
    if type(number) is int:
        if not -MAX_INT <= number <= MAX_INT:
            raise NoCanonicalText('integer')
        return str(number)
    if number != number or number in (float('inf'), float('-inf')):
        raise NoCanonicalText('number')
    if number == 0:
        return '0'
    sign = '-' if number < 0 else ''
    mantissa, _, exponent = repr(abs(number)).partition('e')
    whole, _, fraction = mantissa.partition('.')
    digits, point = whole + fraction, len(whole) + int(exponent or 0)
    stripped = digits.lstrip('0')
    point -= len(digits) - len(stripped)
    digits = stripped.rstrip('0') or '0'
    count = len(digits)
    if count <= point <= 21:
        return sign + digits + '0' * (point - count)
    if 0 < point <= 21:
        return sign + digits[:point] + '.' + digits[point:]
    if -6 < point <= 0:
        return sign + '0.' + '0' * (-point) + digits
    power = point - 1
    tail = ('.' + digits[1:]) if count > 1 else ''
    return f"{sign}{digits[0]}{tail}e{'+' if power >= 0 else '-'}{abs(power)}"


def _string(text: str) -> str:
    out = ['"']
    for ch in text:
        point = ord(ch)
        if 0xD800 <= point <= 0xDFFF:
            raise NoCanonicalText('surrogate')
        out.append(_SHORT.get(point) or ('\\u%04x' % point if point < 0x20 else ch))
    out.append('"')
    return ''.join(out)


def canonical_text(value, depth: int = 0) -> str:
    """``value`` as RFC 8785 writes it. ``NoCanonicalText`` (a ``ValueError``) for a value that has none."""
    if depth > MAX_DEPTH:
        raise NoCanonicalText('depth')
    if value is None:
        return 'null'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if type(value) in (int, float):
        return _number(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, (list, tuple)):
        return '[' + ','.join(canonical_text(item, depth + 1) for item in value) + ']'
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise NoCanonicalText('key')
        keys = sorted(value, key=lambda key: key.encode('utf-16-be', 'surrogatepass'))
        return '{' + ','.join(_string(key) + ':' + canonical_text(value[key], depth + 1) for key in keys) + '}'
    raise NoCanonicalText('type')


def payload_digest(params: dict) -> str:
    """The digest a receipt keeps: of the params without the command id. ``ValueError`` when they have no text."""
    text = canonical_text({key: value for key, value in params.items() if key != 'command_id'})
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


# ------------------------------------------------------------------------------------------ the table
def _always_ready(name) -> bool:
    return True


class MethodTable:
    """The handlers of one composition, and the dispatcher that runs them."""

    def __init__(self) -> None:
        self._handlers: dict[str, Callable] = {}
        self._needs: dict[str, str] = {}             # method -> the service it needs
        self._frozen = False
        self.store = None                            # the event store's transaction helper, or None
        self.service_ready: Callable[[str], bool] = _always_ready
        self.closing = threading.Event()
        self._slots_lock = threading.Lock()
        self._slots: dict[tuple, list] = {}          # (tenant, method, command_id) -> [lock, waiters]

    # -- registration ----------------------------------------------------------------------------
    def register(self, name: str, handler: Callable, *, service: str | None = None) -> None:
        if self._frozen:
            raise MethodTableError(f'method {name!r}: the table is closed; register at composition')
        if name in BUILT_IN or (isinstance(name, str) and name.startswith('run.')):
            raise MethodTableError(f'method {name!r} is answered by the daemon itself and cannot be replaced')
        if name not in METHODS:
            raise MethodTableError(f'method {name!r} is not in the method table')
        if name in self._handlers:
            raise MethodTableError(f'method {name!r} already has a handler')
        if not callable(handler):
            raise MethodTableError(f'method {name!r}: the handler is not callable')
        if service is not None and not (isinstance(service, str) and service):
            raise MethodTableError(f'method {name!r}: service is a name')
        self._handlers[name] = handler
        if service is not None:
            self._needs[name] = service

    def freeze(self) -> None:
        self._frozen = True

    def implemented(self) -> tuple:
        """The methods this daemon answers now, in the table's order."""
        return tuple(name for name in METHODS if name in BUILT_IN or name in self._handlers)

    def registered(self) -> tuple:
        """The methods a lane registered a handler for, in the table's order."""
        return tuple(name for name in METHODS if name in self._handlers)

    def services_needed(self) -> dict:
        return dict(self._needs)

    # -- dispatch --------------------------------------------------------------------------------
    def dispatch(self, method, params: dict, principal, call: Callable) -> dict:
        """Answer a method that is not built in. ``call(method, params)`` is how ``ctx.call`` reaches the
        other methods as the same principal; the caller already holds this dispatch's admission."""
        row = METHODS.get(method) if isinstance(method, str) else None
        if row is None or method in BUILT_IN:
            return {'code': 'method_unknown', 'detail': str(method)}
        handler = self._handlers.get(method)
        if handler is None:
            return {'code': 'unsupported', 'detail': 'not_implemented'}
        needs = self._needs.get(method)
        if needs is not None and not self.service_ready(needs):
            return {'code': 'unavailable', 'detail': 'service_starting'}

        def nested(inner, inner_params=None):
            inner_row = METHODS.get(inner) if isinstance(inner, str) else None
            if inner_row is not None and inner_row.idempotency == 'command_id':
                return {'code': 'request_invalid', 'detail': 'nested_command'}
            return call(inner, inner_params)

        if row.idempotency != 'command_id':
            ctx = Context(principal, None, self.closing, nested)
            try:
                answer = handler(ctx, params)
                if not isinstance(answer, dict) or not isinstance(answer.get('code'), str):
                    raise TypeError(f'handler of {method} returned no answer')
            except NotAccepted as refusal:
                return refusal.answer
            except Exception as exc:  # noqa: BLE001 - a handler's bug is an answer, never a dropped call
                log.exception('handler of %s failed', method)
                return {'code': 'unavailable', 'detail': f'handler:{type(exc).__name__}'}
            # No transaction of the dispatcher's here, so nothing to wait for: the handler returned.
            self._run_callbacks(method, ctx)
            return answer
        return self._command(method, params, principal, handler, nested)

    def _command(self, method, params, principal, handler, nested) -> dict:
        store = self.store
        if store is None:
            return {'code': 'unsupported', 'detail': 'store_absent'}
        command_id = params.get('command_id')
        if not isinstance(command_id, str) or not command_id:
            return {'code': 'request_invalid', 'detail': 'command_id'}
        try:
            digest = payload_digest(params)
        except ValueError:
            return {'code': 'request_invalid', 'detail': 'params'}
        tenant = params.get('tenant') if isinstance(params.get('tenant'), str) else ''
        key = (tenant, method, command_id)
        ctx, stage = None, 'store'
        with self._slot(key):
            try:
                with store.transaction() as tx:
                    receipt = tx.receipt(tenant, method, command_id)
                    if receipt is not None:
                        kept_digest, kept_answer = receipt
                        if kept_digest == digest:
                            return dict(kept_answer)     # a replay: no handler, no callback
                        return {'code': 'conflict', 'detail': 'command_id_reused'}
                    ctx, stage = Context(principal, tx, self.closing, nested), 'handler'
                    answer = handler(ctx, params)
                    if not isinstance(answer, dict) or not isinstance(answer.get('code'), str):
                        raise TypeError(f'handler of {method} returned no answer')
                    stage = 'store'
                    tx.put_receipt(tenant, method, command_id, digest, answer)
            except NotAccepted as refusal:
                return refusal.answer
            except Exception as exc:  # noqa: BLE001 - rolled back by the store's own context manager
                # Whatever failed (the handler, the receipt's write, the commit itself), nothing of this
                # command is committed and no callback runs.
                log.exception('command %s was rolled back (%s)', method, stage)
                return {'code': 'unavailable', 'detail': f'{stage}:{type(exc).__name__}'}
        # Committed, and the slot is released: callbacks run outside every lock of the dispatcher.
        self._run_callbacks(method, ctx)
        return answer

    def _run_callbacks(self, method, ctx) -> None:
        for fn in ctx._callbacks:
            try:
                fn()
            except Exception:  # noqa: BLE001 - the receipt is committed; a later callback still runs
                log.exception('an after-commit callback of %s failed', method)

    def _slot(self, key):
        return _Slot(self, key)


class _Slot:
    """One command at a time for one ``(tenant, method, command_id)``; the entry goes when nobody waits."""

    def __init__(self, table: MethodTable, key: tuple) -> None:
        self._table, self._key = table, key

    def __enter__(self):
        table = self._table
        with table._slots_lock:
            slot = table._slots.get(self._key)
            if slot is None:
                slot = table._slots[self._key] = [threading.Lock(), 0]
            slot[1] += 1
        self._slot = slot
        slot[0].acquire()
        return self

    def __exit__(self, *exc) -> bool:
        table, slot = self._table, self._slot
        slot[0].release()
        with table._slots_lock:
            slot[1] -= 1
            if slot[1] == 0 and table._slots.get(self._key) is slot:
                del table._slots[self._key]
        return False
