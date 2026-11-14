"""The method table (seam S1): the control methods other lanes answer, and the one dispatcher that runs them.

Six methods are answered by the code that answers them today and are never in this table: ``run.start``,
``run.status``, ``run.list``, ``run.stop``, ``route.list`` and ``plugin.list`` (``BUILT_IN``). A seventh,
``methods.list``, is answered by this table itself: what this daemon implements now. Those seven are the
MANDATORY built-ins: every daemon answers them, none of them can answer ``unsupported``, and a handler
cannot replace one.

``METHODS`` lists every method of the published table (``schemas/control/v1/methods.table.v1.json``) with
the facts the dispatcher needs: its scope, whether it mutates, how it is keyed, and the service it waits
for. A name that is not in ``METHODS`` is answered ``method_unknown``, as today, a name some plan has
reserved included. A name that is in ``METHODS`` and has no handler in this daemon is answered
``unsupported`` with detail ``not_implemented``. Adding a row is a request to the lane that keeps this file.

A method's service declaration is part of its versioned public row and cannot be changed privately by
registration. The same published version supplies scope, mutation/keying, capability requirements,
fallbacks and readiness; composition rejects disagreement.

A lane registers a handler from its registrant module's ``register(hooks)``::

    hooks.methods.register("session.turn", handler, service=None, tenant=tenant_of)

    handler(ctx, params) -> answer          a dict with a "code"
    tenant_of(principal, params) -> tenant  for a method keyed by command_id only; see "the tenant" below

``ctx`` (``Context``) carries:

``ctx.principal``
    Who is calling, derived by the server from the way the request came in. It is never read from params.
``ctx.tx``
    For a method keyed by ``command_id``: the open transaction of the event store, owned by the
    DISPATCHER. The handler makes its state changes and appends its events through it and opens no
    transaction of its own. For every other method it is ``None``; a read that is NOT inside a command
    may open its own, through its lane's interface.
``ctx.after_commit(fn)``
    Schedules ``fn()`` for after the dispatcher's commit is confirmed. See "After the commit" below.
``ctx.call(method, params)``
    Calls another control method as the same principal, inside this dispatch: it passes the authorizer
    again and takes no new client admission.

    A nested call inherits the active dispatch's admission and transaction restrictions. While a command
    transaction is open, ``ctx.call`` refuses mutating built-ins with ``request_invalid/nested_effect``,
    as well as command-id methods with ``request_invalid/nested_command``. Registry, daemon, engine and
    filesystem effects use the committed work-intent path. Nested read handlers still receive
    ``ctx.tx=None``; they cannot open a second helper transaction. Same-transaction lane reads and
    writes use the lane's interface with the already-owned ``ctx.tx``.

    Callbacks registered by successful nested reads belong to the enclosing command's callback scope.
    They run in registration order only after that command commits and releases its locks; they are
    discarded if it rolls back. A failed nested handler contributes no callbacks. Outside a command, a
    successful read's callbacks run after its handler returns.

    Each callback is owned by the dispatch that registered it. It runs only if that dispatch, and every
    dispatch between it and the one whose commit it waits for, SUCCEEDED: returned an answer. A dispatch
    that left any other way (a refusal by ``NotAccepted``, an error, an interruption that an outer
    handler caught) contributes none, and neither do the nested calls made inside it; the callbacks of
    its siblings are not touched, whatever the order in which they were registered.

    The restriction follows the dispatch, not the caller's position in it: a command that calls a read
    that calls a built-in is still inside the command. A method keyed by ``command_id`` is refused as
    ``nested_command`` wherever the nested call is made. Once the outermost dispatch has returned,
    ``ctx.call`` answers ``request_invalid/dispatch_ended``: its admission is over.

    Calls made on another thread through ``ctx.call`` must finish, and their threads must be joined,
    before the invoking handler or callback returns. Contexts are not a detached-work admission.
    ``after_commit`` registration belongs to an active handler frame; registering from a callback or
    after that frame returns is rejected. (It raises ``RuntimeError``; so does a registration that
    arrives after the dispatch whose commit it would wait for has finished.)
``ctx.closing``
    A ``threading.Event`` that is set when the daemon begins to close. A handler that waits (a long
    poll) waits on this and returns when it is set.

WHO OWNS THE TRANSACTION. For a method keyed by ``command_id`` the dispatcher does this, in order:

1. With no event store composed, it answers ``unsupported``, detail ``store_absent``, and calls nothing.
2. It takes the command's slot: commands with the same ``(tenant, method, command_id)`` run one at a
   time in this process, so two identical commands sent at once run the handler once.
3. It opens the store's transaction and looks the receipt up IN it. A receipt with the same payload
   digest returns the stored answer: the handler is not called and no callback is staged. Global
   admission, current authorization, store presence and per-method readiness are prerequisites to
   receipt lookup. Once those pass, a receipt hit precedes handler preconditions such as revision and
   live-turn checks. A receipt with another digest answers ``conflict``, detail ``command_id_reused``.
4. On a miss it calls the handler with ``ctx.tx``. The handler checks its own preconditions (a stale
   revision or turn is ``conflict``; a live turn is ``busy``; a missing driver flag is ``unsupported``,
   before any side effect).
5. It stores the handler's answer in the receipt, in the same transaction, and commits.

A handler that RETURNS an answer has accepted the command: the answer is stored and replayed for that id
for good. A handler that must not leave a receipt (the refusal is about this moment, not about the
command: ``busy``, ``unavailable``) raises ``NotAccepted(answer)``: the transaction is rolled back,
nothing is stored, no callback runs, and the caller gets ``answer``; the same id may be sent again. A
handler that raises anything else is rolled back the same way and the caller gets ``unavailable``.

The payload digest is the SHA-256 of the UTF-8 bytes of the params AS SUPPLIED without ``command_id``,
written as RFC 8785 writes JSON (keys in UTF-16 order, no spaces, numbers as ECMAScript writes them).
Absence is preserved: no default is inserted before the digest is taken. Params that have no such text
(a whole number outside the exact range, however it is spelled; a number that is not finite; half a
surrogate pair) are ``request_invalid``. The rule for numbers is the one the approval records use
(``schemas/control/v1``, the call profile): an integral value outside -(2^53 - 1) .. 2^53 - 1 has no text
whether it arrives as an int or as a float, so every text this writes reads back to the same value.

THE TENANT of the identity ``(tenant, method, command_id)`` is derived from the AUTHORIZED TARGET, never
taken from params and never inferred. The caller is authorized for the target before the dispatcher is
reached, so before any receipt is returned; the principal is not a further part of the key. A method
keyed by ``command_id`` is therefore registered with one of two things as ``tenant``: a function
``tenant_of(principal, params) -> tenant`` that names the tenant the target belongs to (the session's
tenant, say), or the constant ``INSTALL_TENANT``, the explicitly named fixed namespace of a method whose
target is the whole install. ``tenant_of`` runs before the transaction is opened; it raises
``NotAccepted(answer)`` when there is no such target (``session_unknown``, say).

AFTER THE COMMIT. ``ctx.after_commit(fn)`` is a callback of THIS PROCESS and nothing more. After a
confirmed commit the dispatcher calls each ``fn`` once, in the order they were scheduled, outside every
lock. After a rollback it calls none. A callback that raises is logged; it does not change the
committed receipt and does not stop the callbacks after it. A replay from a receipt never calls them.
A callback can be lost when the process exits between the commit and the call, so no lane may make one
the only record that work must happen.

The committed response is an owned snapshot. A handler or callback retaining the handler's original
result object cannot change either the first returned answer or later replays.

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
event store's helper. This module runs no effect and adds nothing to the run registry. SPINE schedules
callbacks and passes transactions; the behavior-owning lane supplies work tables and recovery, and
EVENTS supplies the transaction protocol, event append and receipt persistence.

WHAT THE DISPATCHER NEEDS OF THE EVENT STORE. ``store`` is the event store's transaction helper, or
``None`` when no event store is composed. The names are the published helper's (the lanes' preamble,
"Shared state"); the dispatcher uses these two of them::

    with store.transaction() as tx:      commits on a normal exit and raises if the commit failed;
                                         rolls back when the block raises; a second transaction() in
                                         the same thread raises
        tx.receipt_get(tenant, method, command_id)  # None, or (payload_digest, answer)
        tx.receipt_put(tenant, method, command_id, payload_digest, answer)

Lane tables are declared through ``db.register_tables(lane, version, ddl)`` and accessed through the
EVENTS transaction's ``execute(sql, params)``; events use ``tx.append(event)``. There is no shared
``tx.put(table, key, value)`` API. The return shape of ``receipt_get`` is published once, with the event
store; ``(payload_digest, answer)`` is what this dispatcher reads until then.

READINESS. A method waits for the service its PUBLISHED ROW names (seam S13), and a handler is
registered with that same name or the registration is refused. Until the service says it is ready, the
method answers ``unavailable``, detail ``service_starting``; the check comes before the command's
transaction is opened.

Registration happens once, at composition, before anything is served; ``freeze()`` then closes the
table, so nothing changes it while calls are served. A second handler for one name fails composition.
"""
from __future__ import annotations

import copy
import hashlib
import logging
import threading
from collections.abc import Callable
from typing import NamedTuple

log = logging.getLogger(__name__)

BUILT_IN = ('run.start', 'run.status', 'run.list', 'run.stop', 'route.list', 'plugin.list')
MANDATORY = (*BUILT_IN, 'methods.list')    # every daemon answers these; none can answer 'unsupported'
TABLE_VERSION = '1.0-draft.9'              # of the published table these rows are
INSTALL_TENANT = 'install'                 # the receipt namespace of a method whose target is the whole install
MAX_INT = 2**53 - 1   # the largest whole number every reader of JSON numbers keeps exact
MAX_DEPTH = 64        # of params, for the payload digest


class Row(NamedTuple):
    scope: str          # read | run | approve | manage
    mutating: bool
    idempotency: str    # none | client_key | command_id
    beta: bool = True   # every method is beta except the six the daemon answers today
    service: str | None = None   # the service the published row says the method waits for, or None


def _rows() -> dict:
    read = lambda: Row('read', False, 'none')                 # noqa: E731
    keyed = lambda scope='run': Row(scope, True, 'command_id')  # noqa: E731
    today = lambda: Row('read', False, 'none', False)         # noqa: E731
    return {
        'run.start': Row('run', True, 'client_key', False), 'run.status': today(), 'run.list': today(),
        'run.stop': Row('run', True, 'none', False), 'route.list': today(), 'plugin.list': today(),
        'session.start': keyed(), 'session.turn': keyed(), 'session.steer': keyed(), 'session.queue': keyed(),
        'session.interrupt': keyed(), 'session.resume': keyed(), 'session.fork': keyed(),
        'session.export': read(), 'session.list': read(), 'session.status': read(), 'session.stop': keyed(),
        'queue.update': keyed(), 'queue.cancel': keyed(),
        'approval.list': read(), 'approval.open': keyed('approve'), 'approval.answer': keyed('approve'),
        # The two reads of a log wait for the event lane's service, as their published rows say (draft 9).
        'events.read': Row('read', False, 'none', True, 'events'), 'events.follow': Row('read', False, 'none', True, 'events'),
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


class _Dispatch:
    """One dispatch that is running. ``command`` is the enclosing dispatch whose command transaction was open
    when this one began, or None; ``root`` is the outermost dispatch, whose admission every nested call shares."""

    __slots__ = ('parent', 'command', 'root', 'callbacks', 'in_transaction', 'ended', 'active', 'succeeded',
                 'sealed', 'lock')

    def __init__(self, parent) -> None:
        self.parent = parent
        self.command = _command_of(parent)
        self.root = parent.root if parent is not None else self
        self.callbacks: list = []       # (the dispatch that registered it, fn), in the order of registration
        self.in_transaction = False
        self.ended = False
        self.active = False             # its handler is running: only then may it register a callback
        self.succeeded = False          # its handler returned an answer; set once, never by a failure
        self.sealed = False             # its callback list is closed: what it holds is about to run, or never will
        self.lock = threading.Lock()    # of ``sealed`` and of appending to ``callbacks``


def _command_of(scope):
    """The dispatch whose command transaction is open around ``scope``: itself, the one it is nested in, or None."""
    if scope is None:
        return None
    if scope.in_transaction:
        return scope
    outer = scope.command
    return outer if outer is not None and outer.in_transaction else None


class Context:
    """What a handler is given. Made by the dispatcher for one call and not kept after it."""

    __slots__ = ('principal', 'tx', 'closing', '_scope', '_owner', '_call')

    def __init__(self, principal, tx, closing, call, scope, owner) -> None:
        self.principal, self.tx, self.closing = principal, tx, closing
        self._scope = scope             # the dispatch this context is the handler's view of
        self._owner = owner             # the dispatch whose commit its callbacks wait for: the enclosing
        self._call = call               # command when this is a read nested in one, else ``scope`` itself

    def after_commit(self, fn: Callable[[], object]) -> None:
        if not callable(fn):
            raise TypeError('after_commit takes a function of no arguments')
        if not self._scope.active:
            raise RuntimeError('after_commit is called by a running handler: not from a callback, and not after '
                               'the handler has returned')
        owner = self._owner
        with owner.lock:
            if owner.sealed:
                raise RuntimeError('after_commit arrived after the dispatch its callback would wait for had finished')
            owner.callbacks.append((self._scope, fn))

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
    if number.is_integer() and not -MAX_INT <= number <= MAX_INT:
        raise NoCanonicalText('integer')
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
        self._tenant: dict[str, object] = {}         # command method -> its tenant_of, or INSTALL_TENANT
        self._frozen = False
        self.store = None                            # the event store's transaction helper, or None
        self.service_ready: Callable[[str], bool] = _always_ready
        self.closing = threading.Event()
        self._slots_lock = threading.Lock()
        self._slots: dict[tuple, list] = {}          # (tenant, method, command_id) -> [lock, waiters]
        self._active = threading.local()             # .scope: the dispatch running on this thread, or None

    # -- registration ----------------------------------------------------------------------------
    def register(self, name: str, handler: Callable, *, service: str | None = None, tenant=None) -> None:
        if self._frozen:
            raise MethodTableError(f'method {name!r}: the table is closed; register at composition')
        if name in MANDATORY or (isinstance(name, str) and name.startswith('run.')):
            raise MethodTableError(f'method {name!r} is answered by the daemon itself and cannot be replaced')
        if name not in METHODS:
            raise MethodTableError(f'method {name!r} is not in the method table')
        if name in self._handlers:
            raise MethodTableError(f'method {name!r} already has a handler')
        if not callable(handler):
            raise MethodTableError(f'method {name!r}: the handler is not callable')
        if service is not None and not (isinstance(service, str) and service):
            raise MethodTableError(f'method {name!r}: service is a name')
        if service != METHODS[name].service:
            raise MethodTableError(f'method {name!r}: service differs from the published row')
        if METHODS[name].idempotency == 'command_id':
            if tenant != INSTALL_TENANT and not callable(tenant):
                raise MethodTableError(f'method {name!r} keeps a receipt: register it with tenant=, a function of '
                                       '(principal, params) or INSTALL_TENANT')
            self._tenant[name] = tenant
        elif tenant is not None:
            raise MethodTableError(f'method {name!r} keeps no receipt and takes no tenant')
        self._handlers[name] = handler
        if service is not None:
            self._needs[name] = service

    def freeze(self) -> None:
        self._frozen = True

    def implemented(self) -> tuple:
        """The methods this daemon answers now, in the table's order."""
        return tuple(name for name in METHODS if name in MANDATORY or name in self._handlers)

    def listing(self) -> dict:
        """The answer of ``methods.list``: every row of the table, and whether this daemon answers it now."""
        answered = set(self.implemented())
        return {'code': 'ok', 'table_version': TABLE_VERSION,
                'methods': [{'name': name, 'implemented': name in answered, 'beta': row.beta}
                            for name, row in METHODS.items()]}

    def registered(self) -> tuple:
        """The methods a lane registered a handler for, in the table's order."""
        return tuple(name for name in METHODS if name in self._handlers)

    def services_needed(self) -> dict:
        return dict(self._needs)

    def command_open(self) -> bool:
        """Whether the calling thread is inside a dispatch whose command transaction is open, directly or through
        nested reads. An entry point that answers a mutating built-in itself asks this first and refuses
        ``request_invalid/nested_effect``: such a call must not reach an effect before the commit."""
        return _command_of(getattr(self._active, 'scope', None)) is not None

    # -- dispatch --------------------------------------------------------------------------------
    def dispatch(self, method, params: dict, principal, call: Callable) -> dict:
        """Answer a method that is not built in. ``call(method, params)`` is how ``ctx.call`` reaches the
        other methods as the same principal; the caller already holds this dispatch's admission.

        This is an already-authorized, already-admitted primitive, not an entry point (see ``control/hooks.py``).
        A dispatch that is reached from inside another one, through that one's ``ctx.call``, is nested in it and
        inherits its restrictions."""
        row = METHODS.get(method) if isinstance(method, str) else None
        if row is None or method in BUILT_IN:
            return {'code': 'method_unknown', 'detail': str(method)}
        if method == 'methods.list':
            return self.listing()
        handler = self._handlers.get(method)
        if handler is None:
            return {'code': 'unsupported', 'detail': 'not_implemented'}
        needs = self._needs.get(method)
        if needs is not None and not self.service_ready(needs):
            return {'code': 'unavailable', 'detail': 'service_starting'}
        parent = getattr(self._active, 'scope', None)
        scope = _Dispatch(parent)

        def nested(inner, inner_params=None):
            if scope.root.ended:
                return {'code': 'request_invalid', 'detail': 'dispatch_ended'}
            inner_row = METHODS.get(inner) if isinstance(inner, str) else None
            if inner_row is not None and inner_row.idempotency == 'command_id':
                return {'code': 'request_invalid', 'detail': 'nested_command'}
            if _command_of(scope) is not None and inner_row is not None and inner_row.mutating:
                return {'code': 'request_invalid', 'detail': 'nested_effect'}
            # The scope travels with the call, whichever thread makes it, so that the dispatch it reaches is
            # nested in this one and a command -> read -> built-in chain keeps the command's restriction.
            before = getattr(self._active, 'scope', None)
            self._active.scope = scope
            try:
                return call(inner, inner_params)
            finally:
                self._active.scope = before

        self._active.scope = scope
        try:
            if row.idempotency != 'command_id':
                return self._read(method, params, principal, handler, nested, scope)
            return self._command(method, params, principal, handler, nested, scope)
        finally:
            self._active.scope = parent
            if scope.root is scope:
                scope.ended = True

    def _read(self, method, params, principal, handler, nested, scope) -> dict:
        enclosing = scope.command   # the command this read is nested in, whose transaction is open; or None
        owner = enclosing if enclosing is not None else scope
        ctx = Context(principal, None, self.closing, nested, scope, owner)
        # A dispatch that does not reach ``succeeded`` contributes no callbacks, however it left: a refusal, an
        # error, or an interruption that passes through here and is caught further out. Nothing is deleted from
        # the list it shares with its siblings.
        scope.active = True
        try:
            answer = handler(ctx, params)
            if not isinstance(answer, dict) or not isinstance(answer.get('code'), str):
                raise TypeError(f'handler of {method} returned no answer')
            scope.succeeded = True
        except NotAccepted as refusal:
            return copy.deepcopy(refusal.answer)
        except Exception as exc:  # noqa: BLE001 - a handler's bug is an answer, never a dropped call
            log.exception('handler of %s failed', method)
            return {'code': 'unavailable', 'detail': f'handler:{type(exc).__name__}'}
        finally:
            scope.active = False
            if enclosing is None:
                with scope.lock:
                    scope.sealed = True      # nothing joins this list from here on
        # A read nested in a command registered its callbacks on that command's list (``owner``), where they wait
        # for its commit; its own list is empty. A read with no command around it has no transaction of the
        # dispatcher's to wait for: the handler returned, and its own list runs now.
        if scope.callbacks:
            answer = copy.deepcopy(answer)   # what the caller gets is not what a callback still holds
        self._run_callbacks(method, scope)
        return answer

    def _command(self, method, params, principal, handler, nested, scope) -> dict:
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
        tenant = self._tenant[method]
        if tenant != INSTALL_TENANT:
            try:
                tenant = tenant(principal, params)
                if not isinstance(tenant, str) or not tenant:
                    raise TypeError(f'tenant_of of {method} named no tenant')
            except NotAccepted as refusal:
                return copy.deepcopy(refusal.answer)
            except Exception as exc:  # noqa: BLE001 - no tenant, no receipt: nothing is run
                log.exception('the tenant of %s could not be derived', method)
                return {'code': 'unavailable', 'detail': f'tenant:{type(exc).__name__}'}
        key = (tenant, method, command_id)
        stage = 'store'
        with self._slot(key):
            try:
                with store.transaction() as tx:
                    receipt = tx.receipt_get(tenant, method, command_id)
                    if receipt is not None:
                        kept_digest, kept_answer = receipt
                        if kept_digest == digest:
                            return copy.deepcopy(kept_answer)     # a replay: no handler, no callback
                        return {'code': 'conflict', 'detail': 'command_id_reused'}
                    # From here to the end of the transaction this dispatch is a command with its transaction
                    # open: nested calls are restricted, and nested reads' callbacks join this command's.
                    scope.in_transaction = True
                    ctx, stage = Context(principal, tx, self.closing, nested, scope, scope), 'handler'
                    scope.active = True
                    try:
                        answer = handler(ctx, params)
                    finally:
                        scope.active = False
                    if not isinstance(answer, dict) or not isinstance(answer.get('code'), str):
                        raise TypeError(f'handler of {method} returned no answer')
                    # An owned snapshot: neither the handler's object nor the store's copy of it.
                    committed_answer = copy.deepcopy(answer)
                    stage = 'store'
                    tx.receipt_put(tenant, method, command_id, digest, copy.deepcopy(committed_answer))
            except NotAccepted as refusal:
                return copy.deepcopy(refusal.answer)
            except Exception as exc:  # noqa: BLE001 - rolled back by the store's own context manager
                # Whatever failed (the handler, the receipt's write, the commit itself), nothing of this
                # command is committed and no callback runs, the nested reads' included.
                log.exception('command %s was rolled back (%s)', method, stage)
                return {'code': 'unavailable', 'detail': f'{stage}:{type(exc).__name__}'}
            finally:
                scope.in_transaction = False
                with scope.lock:
                    scope.sealed = True      # committed or rolled back: no callback joins this command any more
        # Committed, and the slot is released: callbacks run outside every lock of the dispatcher, the ones
        # nested reads registered among them, in the order they were registered.
        scope.succeeded = True
        self._run_callbacks(method, scope)
        return committed_answer

    def _run_callbacks(self, method, owner) -> None:
        """Run the callbacks that wait for ``owner``, in the order they were registered: each one whose own
        dispatch succeeded, and every dispatch between it and ``owner``. The list is sealed, so it is whole."""
        for registrant, fn in list(owner.callbacks):
            current = registrant
            while current is not owner and current is not None and current.succeeded:
                current = current.parent
            if current is not owner or not owner.succeeded:
                continue                     # its own dispatch, or one around it, did not succeed
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
