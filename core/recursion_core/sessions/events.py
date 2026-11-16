# Model: Claude Opus 5.5 (claude-opus-5-5), lane SESSIONS.
"""The event.v1 envelopes this lane appends, through ``tx.append`` in the transaction that makes the change.

An envelope carries no ``seq`` and no ``time``: the event store assigns both (lane EVENTS' helper refuses an
event that brings either). The log is the session's: ``session_id`` is the session's id. The payload of
each type is the one event.v1 (#368) gives it. Who may write each type, and with which trust, follows the
same schema: everything here is written by the core (``source`` 'core'), except ``input.accepted``, which is
the engine's own typed acceptance and so is 'engine' and 'untrusted'. A core event that carries text a
person or an engine wrote (``turn.started`` carries the input) is 'untrusted' too: it is not the product's
own words.
"""
from __future__ import annotations

ENVELOPE_KEYS = ('session_id', 'run_id', 'turn_id', 'type', 'source', 'trust', 'payload')


def envelope(kind: str, session_id: str, run_id, turn_id, payload: dict, *, source: str = 'core',
             trust: str = 'trusted') -> dict:
    return {'session_id': session_id, 'run_id': run_id, 'turn_id': turn_id, 'type': kind, 'source': source,
            'trust': trust, 'payload': payload}


def steer_queued(item: dict, run_id=None, turn_id=None) -> dict:
    """A message was queued, edited or withdrawn: its id, place, delivery, revision and state after the change."""
    return envelope('steer.queued', item['session_id'], run_id, turn_id,
                    {'item_id': item['item_id'], 'order': item['order'], 'delivery': item['delivery'],
                     'revision': item['revision'], 'state': item['state']})


def steer_sent(binding: dict) -> dict:
    """The write of a message into the turn in progress, filed under the turn its binding aims at. NOT acceptance."""
    return envelope('steer.sent', binding['session_id'], binding['run_id'], binding['target_turn_id'],
                    {'item_id': binding['item_id'], 'delivery_id': binding['delivery_id']})


def input_accepted(binding: dict) -> dict:
    """The engine's typed acceptance, as the core files it once it agreed with the binding in every member."""
    return envelope('input.accepted', binding['session_id'], binding['run_id'], binding['target_turn_id'],
                    {'delivery_id': binding['delivery_id'], 'turn_id': binding['target_turn_id'],
                     'how': binding['how']},
                    source='engine', trust='untrusted')


def turn_started(session_id: str, run_id: str, turn_id: str, delivery_id: str, index: int, text: str) -> dict:
    return envelope('turn.started', session_id, run_id, turn_id,
                    {'delivery_id': delivery_id, 'index': index, 'text': text}, trust='untrusted')


def turn_ended(session_id: str, run_id, turn_id: str, outcome: str, result) -> dict:
    # A result's detail may carry engine text, so the event is untrusted whenever it has a result.
    return envelope('turn.ended', session_id, run_id, turn_id, {'outcome': outcome, 'result': result},
                    trust='trusted' if result is None else 'untrusted')


def interrupt(session_id: str, run_id, turn_id, requested_by: dict, outcome: str) -> dict:
    """An interrupt was asked, and what came of it: 'delivered' (the turn WAS interrupted; written only when the
    driver reported it), 'fell_back_to_stop' (the driver cannot interrupt, so a Stop was asked instead) or
    'no_live_turn' (with no turn id). ``requested_by`` is a common.v1 principal, checked by the caller
    (``Sessions.interrupt``) before anything is written."""
    return envelope('interrupt', session_id, run_id, turn_id,
                    {'turn_id': turn_id, 'requested_by': dict(requested_by), 'outcome': outcome})


def outcome_unknown(session_id: str, run_id, turn_id, kind: str, effect_id: str, why: str) -> dict:
    """Something with an outside effect was in flight and nobody can say whether it happened. Never retried."""
    return envelope('effect.outcome_unknown', session_id, run_id, turn_id,
                    {'effect': {'kind': kind, 'id': effect_id}, 'why': why})
