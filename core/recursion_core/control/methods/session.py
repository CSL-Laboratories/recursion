# Model: Claude Opus 5.5 (claude-opus-5-5), lane SESSIONS.
"""The handlers of lane SESSIONS for the method table (seam S1): the rows the published table gives this lane.

    session.start  session.turn  session.queue  session.steer  queue.update  queue.cancel     (keyed)
    session.status  session.list                                                               (reads)

``register(table, sessions)`` registers them on a ``MethodTable``. NOTHING CALLS IT IN THIS SLICE: the lane's
registrant (``sessions/hooks.py``) registers nothing until the seams merge, and the tests call it on a
``Hooks()`` of their own. The other rows of this lane are not registered, so the dispatcher answers them
``unsupported``/``not_implemented``: ``session.interrupt`` and ``session.resume`` (the next slice),
``session.fork`` (it needs lane WORKSPACE) and ``session.export``. ``session.stop`` is lane DELEGATION's row.

WHAT THE DISPATCHER DOES AND WHAT THESE DO (control/methods/__init__.py). For a keyed method the dispatcher
derives the tenant (``tenant_of`` below, from the authorized target, before any transaction), takes the
command's slot, opens the event store's transaction, looks the receipt up, and only on a miss calls the
handler with ``ctx.tx``; it stores the answer and commits. So a handler here:

- checks its own preconditions AFTER the receipt: a stale ``expected_revision`` or ``expected_turn_id`` is
  ``conflict`` (``stale_revision`` with ``current_revision``, or ``stale_turn``), and is RETURNED: it is about
  the command, which named old state, and is replayed for that id;
- raises ``NotAccepted`` for a refusal that is about this moment or about a request that was never a command:
  ``busy`` (a turn is live), ``unavailable``, a turn that cannot continue the conversation YET, a field this
  slice does not honour, a steer on an engine where steering is not offered (questions.md Q7), and every
  ``request_invalid``. Nothing is stored and the id may be sent again;
- changes state only through ``ctx.tx`` and the lane's own tables, appends its events there, and opens no
  transaction of its own. What must happen outside the store is committed as a work intent (``service.py``);
  ``ctx.after_commit`` only wakes the lane's service, which finds the work without it.

EVERY REQUEST IS CHECKED WHOLE FIRST (``records.check_request``): every field that is present must have its
published shape (methods.v1, ``params.<method>``), a null included, before anything else is looked at. A
malformed request is ``request_invalid`` naming the field, with nothing written and no receipt. A well-formed
field this slice does not honour (a parent session, a containment level, a workspace) is ``unsupported``.

THE TWO READS open their own transaction, as the dispatcher allows a read that is not inside a command. A read
nested in a command gets ``ctx.tx`` None and may not open a second one (control/methods/__init__.py, "Nested
read handlers still receive ctx.tx=None"), so a nested ``session.status`` answers ``unavailable``: another
lane's command that needs a session reads it through this lane's interface with its own ``ctx.tx``
(``store.load(ctx.tx, session_id)``), as the dispatcher says (questions.md Q11).

Two answers have no exact word in the published table yet, and use the nearest one it allows (questions.md):
an item that is no longer queued (``queue.update``, ``queue.cancel``) and a session that has ended are both
``conflict``/``stale_revision`` with ``current_revision``. No session ends in this slice.
"""
from __future__ import annotations

import copy

from recursion_core.control.methods import NotAccepted
from recursion_core.sessions import delivery, machine, records, store

KEYED = ('session.start', 'session.turn', 'session.queue', 'session.steer', 'queue.update', 'queue.cancel')
READS = ('session.status', 'session.list')
LIST_LIMIT = 100


def _invalid(field: str):
    return NotAccepted({'code': 'request_invalid', 'detail': field})


def _shape(method: str, params) -> dict:
    """The params as supplied, refused ``request_invalid`` (naming the field, or ``params`` when they are not an
    object) unless they are the published params of ``method`` (``records.check_request``)."""
    try:
        records.check_request(method, params)
    except records.RecordError as problem:
        raise _invalid(problem.path.lstrip('/') or 'params') from None
    return params


def _stale(record: dict, **echo) -> dict:
    return {'code': 'conflict', 'detail': 'stale_revision', 'current_revision': record['revision'], **echo}


# ------------------------------------------------------------------------------------------- the tenant
def _session_tenant(sessions):
    def tenant_of(principal, params) -> str:
        session_id = params.get('session_id') if isinstance(params, dict) else None
        if not records._matches(records.SESSION_ID, session_id):
            raise _invalid('session_id')
        with sessions.store.transaction() as tx:
            tenant = store.tenant_of(tx, session_id)
        if tenant is None:
            raise NotAccepted({'code': 'session_unknown', 'session_id': session_id})
        return tenant
    return tenant_of


def _item_tenant(sessions):
    def tenant_of(principal, params) -> str:
        item_id = params.get('item_id') if isinstance(params, dict) else None
        if not records._matches(records.ITEM_ID, item_id):
            raise _invalid('item_id')
        with sessions.store.transaction() as tx:
            item = store.load_item(tx, item_id)
            tenant = None if item is None else store.tenant_of(tx, item['session_id'])
        if tenant is None:
            raise NotAccepted({'code': 'item_unknown', 'item_id': item_id})
        return tenant
    return tenant_of


def _start_tenant(principal, params) -> str:
    """A new session's target is the tenant it is started in: the tenant the request names, which the authorizer
    passed for this principal before the dispatcher was reached (as run.start keys client_key by tenant)."""
    tenant = params.get('tenant') if isinstance(params, dict) else None
    if not isinstance(tenant, str) or not tenant:
        raise _invalid('tenant')
    return tenant


# ------------------------------------------------------------------------------------------- handlers
def _load(ctx, session_id: str) -> dict:
    session = store.load(ctx.tx, session_id)
    if session is None:
        raise NotAccepted({'code': 'session_unknown', 'session_id': session_id})
    return session


def _open_turn(sessions, ctx, session: dict, text: str) -> tuple:
    """``((session after, turn id), None)``, or ``(None, the answer to return)``; raises ``NotAccepted``."""
    try:
        return sessions.open_turn(ctx.tx, session, text), None
    except machine.Refused as refusal:
        if refusal.code == 'conflict':                      # the session ended
            return None, _stale(session, session_id=session['session_id'])
        if refusal.code == 'unsupported':
            raise NotAccepted({'code': 'unsupported', 'detail': 'not_implemented',
                               'session_id': session['session_id']}) from None
        raise NotAccepted({'code': refusal.code, 'detail': refusal.detail,
                           'session_id': session['session_id']}) from None


def handlers(sessions) -> dict:
    """``{method: (handler, tenant)}`` for every row this slice answers; ``tenant`` is None for a read."""

    def start(ctx, params):
        _shape('session.start', params)
        prompt = params.get('prompt')
        # A child session (lane DELEGATION's), a containment level (lane STOP's) and a workspace (lane WORKSPACE's)
        # are honoured by those lanes, not by this slice: well-formed, they are refused before anything is
        # written, and never stored. Only the workspace admits null (no workspace asked for).
        if 'parent_session_id' in params or 'containment_level' in params or params.get('workspace') is not None:
            raise NotAccepted({'code': 'unsupported', 'detail': 'not_implemented'})
        budget = params.get('budget', {'cap_usd': 0})
        route = sessions.ports.routes.resolve(ctx.principal, copy.deepcopy(params))
        if isinstance(route, tuple):
            code, detail = route
            raise NotAccepted({'code': code, 'detail': detail})
        try:
            records.check_route(route)
        except records.RecordError:
            raise NotAccepted({'code': 'unavailable', 'detail': 'route_invalid'}) from None
        if sessions.declaration(route['engine']) is None:
            raise NotAccepted({'code': 'unavailable', 'detail': 'driver_absent'})
        session = machine.new_session(session_id=sessions.mint('ses-'), created_at=sessions.now(),
                                      tenant=params['tenant'], principal=ctx.principal.public(), route=route,
                                      workspace=None, budget=budget)
        store.insert_session(ctx.tx, session)
        turn_id = None
        if prompt is not None:
            opened, refused = _open_turn(sessions, ctx, session, prompt)
            if opened is None:
                raise NotAccepted(refused)
            session, turn_id = opened
        ctx.after_commit(sessions.wake.set)
        return {'code': 'ok', 'session': session, 'turn_id': turn_id}

    def turn(ctx, params):
        _shape('session.turn', params)
        text, expected = params['text'], records.integer(params['expected_revision'])
        session = _load(ctx, params['session_id'])
        if expected != session['revision']:
            return _stale(session, session_id=session['session_id'])
        opened, refused = _open_turn(sessions, ctx, session, text)
        if opened is None:
            return refused
        after, turn_id = opened
        ctx.after_commit(sessions.wake.set)
        return {'code': 'ok', 'session': after, 'turn_id': turn_id}

    def queue(ctx, params):
        _shape('session.queue', params)
        text = params['text']
        session = _load(ctx, params['session_id'])
        if session['state'] == 'ended':
            return _stale(session, session_id=session['session_id'])
        item = delivery.queue_item(ctx.tx, session, item_id=sessions.mint('qi-'), text=text, delivery='next_turn',
                                   created_at=sessions.now())
        ctx.after_commit(sessions.wake.set)
        return {'code': 'ok', 'item': item}

    def steer(ctx, params):
        _shape('session.steer', params)
        text, expected = params['text'], params['expected_turn_id']
        session = _load(ctx, params['session_id'])
        if session['active_turn_id'] != expected:
            return {'code': 'conflict', 'detail': 'stale_turn', 'session_id': session['session_id']}
        _steering_offered(session, session_id=session['session_id'])
        item = delivery.queue_item(ctx.tx, session, item_id=sessions.mint('qi-'), text=text,
                                   delivery=sessions.steering_delivery(session), created_at=sessions.now())
        ctx.after_commit(sessions.wake.set)
        return {'code': 'ok', 'item': item}

    def _steering_offered(session, **echo):
        refusal = sessions.steering_refusal(session)
        if refusal is not None:
            raise NotAccepted({'code': refusal[0], 'detail': refusal[1], **echo})

    def _item_change(method, ctx, params, change, *, to_steer=False):
        _shape(method, params)
        expected = records.integer(params['expected_revision'])
        item = store.load_item(ctx.tx, params['item_id'])
        if item is None:
            raise NotAccepted({'code': 'item_unknown', 'item_id': params['item_id']})
        if expected != item['revision'] or item['state'] != 'queued':
            return _stale(item, item_id=item['item_id'])
        session = store.load(ctx.tx, item['session_id'])
        if to_steer:
            _steering_offered(session, item_id=item['item_id'])     # an edit cannot open steering either
        after = change(item, session)
        ctx.after_commit(sessions.wake.set)
        return {'code': 'ok', 'item': after}

    def update(ctx, params):
        _shape('queue.update', params)
        facts = {key: params[key] for key in ('text', 'delivery') if key in params}
        to_steer = facts.get('delivery') == 'steer'

        def change(item, session):
            if to_steer:
                facts['delivery'] = sessions.steering_delivery(session)     # the same fallback as session.steer
            return delivery.edit(ctx.tx, item['item_id'], facts, session)
        return _item_change('queue.update', ctx, params, change, to_steer=to_steer)

    def cancel(ctx, params):
        return _item_change('queue.cancel', ctx, params,
                            lambda item, session: delivery.cancel(ctx.tx, item['item_id'], session))

    def status(ctx, params):
        _shape('session.status', params)
        session_id = params['session_id']
        with sessions.store.transaction() as tx:
            session = store.load(tx, session_id)
            found = [] if session is None else store.items(tx, session_id)
        if session is None:
            return {'code': 'session_unknown', 'session_id': session_id}
        return {'code': 'ok', 'session': session, 'queue': found}

    def listing(ctx, params):
        _shape('session.list', params)
        tenant, state = params.get('tenant'), params.get('state')
        parent, after = params.get('parent_session_id'), params.get('after')
        limit = records.integer(params.get('limit', LIST_LIMIT))
        with sessions.store.transaction() as tx:
            found, next_after = store.page(tx, tenant=tenant, state=state, parent_session_id=parent, after=after,
                                           limit=limit)
        answer = {'code': 'ok', 'sessions': found}
        if next_after is not None:
            answer['next_after'] = next_after
        return answer

    return {
        'session.start': (start, _start_tenant),
        'session.turn': (turn, _session_tenant(sessions)),
        'session.queue': (queue, _session_tenant(sessions)),
        'session.steer': (steer, _session_tenant(sessions)),
        'queue.update': (update, _item_tenant(sessions)),
        'queue.cancel': (cancel, _item_tenant(sessions)),
        'session.status': (status, None),
        'session.list': (listing, None),
    }


def register(table, sessions) -> None:
    """Register every handler of this slice on ``table`` (a ``MethodTable``), each with the service its published
    row names (none, at table draft 10). Called by nobody in this slice but the tests."""
    for name, (handler, tenant) in handlers(sessions).items():
        table.register(name, handler, service=None, tenant=tenant)
