# Model: Claude Opus 5.5 (claude-opus-5-5), lane SESSIONS.
"""The queue, and the rule by which a message reaches an engine (queue.item.v1, #368, draft 7).

This is the durable form of ``conformance/rules.py``'s ``DeliveryModel``: the same happenings, the same
refusals and the same end states, kept in the lane's tables through the caller's transaction, with three
differences that the model does not need: ``begin`` also refuses (``binding``) a delivery id that was used
before, a quarantine appends its ``effect.outcome_unknown`` event, and the evidence kept for a late acceptance
is its five acceptance members (delivery, log, run, turn, how), not every key the driver sent. The tests walk
every vector of ``schemas/control/v1/vectors/queue.delivery`` through it and compare every event in full.

THE RULE, in short (queue.item.v1 says it in full):

- An item can be edited or withdrawn ONLY while it is ``queued``; an edit raises its revision.
- ``begin`` COMMITS, in one write and before anything is written toward the engine: the item as
  ``delivering``, a delivery id that is never used again, and the whole binding (the delivery, the item, the
  log, the run, the worker's incarnation, the turn the input is aimed at, and how). Only a ``queued`` item can
  begin, so seeing ``delivering``, retrying, or replaying a command never sends again.
- ``written``: the write returned. It changes nothing in the item. At most one write per binding.
- ``accepted``: the engine's own typed acceptance, checked against the binding member by member (delivery,
  log, run, turn, how). All equal: the item is ``delivered`` and carries the turn that took it, and
  ``input.accepted`` is appended in the same commit. Nothing else makes an item delivered.
- At a restart every item still ``delivering`` becomes ``outcome_unknown``; so does one whose target turn
  ended with no acceptance. It is never resent. An acceptance that arrives after that changes nothing by
  itself and is KEPT as evidence.
- A refusal changes nothing: not the item, not its binding, not the record of the write. The one exception
  is the kept evidence: a ``not_delivering`` refusal of an acceptance for an ``outcome_unknown`` item has
  written that acceptance into the binding's evidence before it raises, so a caller that wants to keep it
  catches ``Refused`` INSIDE its transaction and lets the transaction commit.

A turn's own input (the prompt of ``session.start``, the text of ``session.turn``) is delivered under the same
rule with no item: ``begin_input`` commits its binding, and ``written``, ``accept`` and the quarantines treat it
as they treat an item's.
"""
from __future__ import annotations

import copy

from recursion_core.sessions import events, records, store


class Refused(Exception):
    """The step does not apply. ``code`` is the published refusal word (conformance/rules.py)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _filed_under(session: dict | None) -> tuple:
    """The run and turn a queue event of this session is filed under: its unit and its turn in progress, if any."""
    if session is None:
        return None, None
    worker = session.get('worker')
    return (worker['run_id'] if worker else None), session.get('active_turn_id')


# ------------------------------------------------------------------------------------------------ queue
def queue_item(tx, session: dict, *, item_id: str, text: str, delivery: str, created_at: str) -> dict:
    """A new message, ``queued``, at the end of the session's order. Appends ``steer.queued``."""
    session_id = session['session_id']
    item = {'item_id': item_id, 'session_id': session_id, 'order': store.next_order(tx, session_id), 'text': text,
            'revision': 1, 'delivery': delivery, 'state': 'queued', 'delivery_id': None, 'turn_id': None,
            'created_at': created_at}
    store.insert_item(tx, item)
    tx.append(events.steer_queued(item, *_filed_under(session)))
    return item


def edit(tx, item_id: str, facts: dict, session: dict | None = None) -> dict:
    """Change the text, the delivery, or both, of a queued item. The edited item is made whole and checked before
    it replaces the item; an edit that names neither, an empty text or a delivery that is not one of the two is
    refused (``edit``) and nothing changes, the revision included."""
    item = store.load_item(tx, item_id)
    if item is None:
        raise Refused('item_unknown')
    if item['state'] != 'queued':
        raise Refused('not_queued')
    changes = {key: facts[key] for key in ('text', 'delivery') if key in facts}
    if (not changes or not isinstance(changes.get('text', 'text'), str) or not changes.get('text', 'text')
            or changes.get('delivery', 'steer') not in records.DELIVERIES):
        raise Refused('edit')
    after = {**item, **changes, 'revision': item['revision'] + 1}
    store.save_item(tx, item, after)
    tx.append(events.steer_queued(after, *_filed_under(session)))
    return after


def cancel(tx, item_id: str, session: dict | None = None) -> dict:
    item = store.load_item(tx, item_id)
    if item is None:
        raise Refused('item_unknown')
    if item['state'] != 'queued':
        raise Refused('not_queued')
    after = {**item, 'state': 'cancelled'}
    store.save_item(tx, item, after)
    tx.append(events.steer_queued(after, *_filed_under(session)))
    return after


# ---------------------------------------------------------------------------------------------- delivery
def _binding_is_whole(binding) -> bool:
    try:
        records.check_binding(binding)
    except records.RecordError:
        return False
    return True


def begin(tx, item_id: str, binding: dict) -> dict:
    """Commit ``delivering``, the delivery id and the whole binding, before anything is written. Only this
    transition owns the write."""
    item = store.load_item(tx, item_id)
    if item is None:
        raise Refused('item_unknown')
    if item['state'] != 'queued':
        raise Refused('not_queued')
    if (not isinstance(binding, dict) or set(binding) != set(records.BINDING_KEYS)
            or binding['item_id'] != item['item_id'] or binding['session_id'] != item['session_id']
            or binding['how'] not in records.HOW_OF.get(item['delivery'], ()) or not _binding_is_whole(binding)):
        raise Refused('binding')
    if store.load_delivery(tx, binding['delivery_id']) is not None:
        raise Refused('binding')     # a delivery id is never used again
    after = {**item, 'state': 'delivering', 'delivery_id': binding['delivery_id']}
    store.save_item(tx, item, after)
    store.insert_delivery(tx, binding, item_id)
    return after


def begin_input(tx, binding: dict) -> None:
    """Commit the binding of a turn's own input (no item): always ``new_turn``, aimed at the turn just opened."""
    whole = {**binding, 'item_id': 'qi-none'}
    if (set(binding) != set(records.BINDING_KEYS) - {'item_id'} or binding['how'] != 'new_turn'
            or not _binding_is_whole(whole) or store.load_delivery(tx, binding['delivery_id']) is not None):
        raise Refused('binding')
    store.insert_delivery(tx, binding, None)


def _binding_of(delivery: dict) -> dict:
    return {key: delivery[key] for key in records.BINDING_KEYS}


def written(tx, delivery: dict, facts: dict) -> dict:
    """Record the write of ``delivery`` (a row as ``store.load_delivery`` reads it). ``facts`` are what the write
    observation carries: always the delivery id, and for a write into a turn in progress the item, log, run and
    turn of its steer.sent. Appends ``steer.sent`` for a mid-turn write."""
    if delivery['state'] != 'delivering':
        raise Refused('not_delivering')
    if (facts.get('delivery_id') != delivery['delivery_id']
            or facts.get('item_id', delivery['item_id']) != delivery['item_id']
            or facts.get('session_id', delivery['session_id']) != delivery['session_id']
            or facts.get('run_id', delivery['run_id']) != delivery['run_id']
            or facts.get('turn_id', delivery['target_turn_id']) != delivery['target_turn_id']):
        raise Refused('delivery_mismatch')
    if delivery['written']:
        raise Refused('already_written')
    store.update_delivery(tx, delivery['delivery_id'], written=True)
    if delivery['how'] == 'mid_turn' and delivery['item_id'] is not None:
        tx.append(events.steer_sent(_binding_of(delivery)))
    return {**delivery, 'written': True}


ACCEPT_FACTS = ('delivery_id', 'session_id', 'run_id', 'turn_id', 'how')


def accept(tx, delivery: dict, facts: dict) -> dict:
    """The engine's typed acceptance, as its input.accepted event carries it: delivery, log, run, turn, how. All
    equal to the binding: the delivery and its item are ``delivered`` and ``input.accepted`` is appended. Returns
    the delivered row."""
    if delivery['state'] != 'delivering':
        if delivery['state'] == 'outcome_unknown':
            kept = {key: facts.get(key) for key in ACCEPT_FACTS}
            store.update_delivery(tx, delivery['delivery_id'], evidence=[*delivery['evidence'], kept])
        raise Refused('not_delivering')
    if any(facts.get(theirs) != delivery[ours] for theirs, ours in (
            ('delivery_id', 'delivery_id'), ('session_id', 'session_id'), ('run_id', 'run_id'),
            ('turn_id', 'target_turn_id'), ('how', 'how'))):
        raise Refused('delivery_mismatch')
    store.update_delivery(tx, delivery['delivery_id'], state='delivered')
    if delivery['item_id'] is not None:
        item = store.load_item(tx, delivery['item_id'])
        store.save_item(tx, item, {**item, 'state': 'delivered', 'turn_id': delivery['target_turn_id']})
    tx.append(events.input_accepted(_binding_of(delivery)))
    return {**delivery, 'state': 'delivered'}


def _quarantine(tx, delivery: dict, why: str) -> dict:
    store.update_delivery(tx, delivery['delivery_id'], state='outcome_unknown')
    if delivery['item_id'] is not None:
        item = store.load_item(tx, delivery['item_id'])
        store.save_item(tx, item, {**item, 'state': 'outcome_unknown'})
    tx.append(events.outcome_unknown(delivery['session_id'], delivery['run_id'], delivery['target_turn_id'],
                                     'queue_delivery', delivery['delivery_id'], why))
    return {**delivery, 'state': 'outcome_unknown'}


def quarantine_all(tx) -> list:
    """At a restart: every delivery still ``delivering`` has no acceptance and becomes ``outcome_unknown``, with
    its event, in the caller's one commit. Returns the rows it quarantined."""
    return [_quarantine(tx, delivery, 'daemon_restart') for delivery in store.deliveries(tx, state='delivering')]


def quarantine_turn(tx, session_id: str, turn_id: str) -> list:
    """A turn ended: every delivery of this session still ``delivering`` whose target is that turn becomes
    ``outcome_unknown``, whichever ``how`` it has. Ending a different turn changes nothing."""
    if not isinstance(turn_id, str) or not turn_id:
        raise Refused('turn')
    return [_quarantine(tx, delivery, 'turn_ended')
            for delivery in store.deliveries(tx, state='delivering', session_id=session_id, target_turn_id=turn_id)]


# ------------------------------------------------------------------------------- the rule, one item at a time
def step(tx, item_id: str, happening: str, facts: dict | None = None) -> None:
    """One happening to one item, as ``conformance/rules.py`` (``DeliveryModel.step``) states the rule: ``begin``,
    ``written``, ``accepted``, ``restart``, ``turn_ended``, ``edit``, ``cancel``. The binding an item is checked
    against is ITS binding: the delivery committed for it, whatever delivery id the facts name."""
    facts = copy.deepcopy(facts) if facts else {}
    item = store.load_item(tx, item_id)
    if item is None:
        raise Refused('item_unknown')
    found = store.deliveries(tx, session_id=item['session_id'])
    delivery = next((row for row in found if row['item_id'] == item_id), None)
    if happening == 'begin':
        begin(tx, item_id, facts)
    elif happening == 'written':
        if item['state'] != 'delivering':
            raise Refused('not_delivering')
        written(tx, delivery, facts)
    elif happening == 'accepted':
        if delivery is None:
            raise Refused('not_delivering')
        accept(tx, delivery, facts)
    elif happening == 'restart':
        if delivery is not None and delivery['state'] == 'delivering':
            _quarantine(tx, delivery, 'daemon_restart')
    elif happening == 'turn_ended':
        ended = facts.get('turn_id')
        if not isinstance(ended, str) or not ended:
            raise Refused('turn')
        if delivery is not None and delivery['state'] == 'delivering' and delivery['target_turn_id'] == ended:
            _quarantine(tx, delivery, 'turn_ended')
    elif happening == 'edit':
        edit(tx, item_id, facts)
    elif happening == 'cancel':
        cancel(tx, item_id)
    else:
        raise Refused('happening')
