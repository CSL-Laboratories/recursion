"""The plugin-effect sink: one plugin effect, recorded on the approval queue, charged, then consumed once.

The queue already stores a plugin effect and an owner or a device can answer it. Nothing on that path
charges the effect before the approval is consumed, so a plugin effect cannot be carried out through the
queue alone. This sink is that path. It does not decide who may answer (that stays ``approval.answer``,
owner or device) and it does not compose an authenticator or a plugin principal: the caller supplies the
charge.

* ``request(effect)`` records the effect on the queue, bound to the effect's own run. The caller cannot
  name another run. A request that cannot be bound whole is refused and nothing is written.
* ``carry(approval_id, effect)`` charges, then consumes, once. The charge runs only while the approval is
  still approved and the effect is the one that was bound, run included. The approval becomes consumed
  only after the charge returns, or inside the charge's own commit when the charge consumes it (the
  plugin host's charge does, so that no failure leaves it charged and still approved). A charge that
  refuses, raises or is interrupted before that leaves the approval unconsumed. A second carry is
  refused and does not charge again.

One sink, or two sinks on the same queue, share one gate, so two callers spend once. The charge callable
must not call back into the sink (that is refused). It must also be idempotent on the approval id: the
spent mark is in this process, and the lane's tables cannot gain a column (a changed declaration is
refused, and there is no migration). A later process calls the charge again, and that call must return
the same receipt without spending a second time.

The charge is given a copy of the effect that was approved. It runs with no store transaction open.
"""

from __future__ import annotations

import copy
import threading
import weakref

from recursion_core.approvals import effects
from recursion_core.approvals import store as rows
from recursion_core.approvals.effects import EffectRefused
from recursion_core.approvals.model import ApprovalRefused

__all__ = ["PluginEffectSink"]

_GATES: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_GATES_LOCK = threading.Lock()


class _Gate:
    """What one queue's sinks share: one carry at a time, and which approvals this process has charged."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.spent: dict = {}
        self.busy = threading.local()


def _gate_for(queue) -> _Gate:
    with _GATES_LOCK:
        gate = _GATES.get(queue)
        if gate is None:
            gate = _Gate()
            _GATES[queue] = gate
        return gate


class PluginEffectSink:
    """Records a plugin effect on an approval queue and carries it out charge-first."""

    def __init__(self, queue, charge) -> None:
        if not callable(charge):
            raise TypeError("the plugin-effect sink charges through a callable")
        self._queue = queue
        self._charge = charge
        self._gate = _gate_for(queue)

    def request(self, effect: dict, *, session_id=None, turn_id=None) -> str:
        """Record ``effect`` on the queue, bound to ``effect["run_id"]``. Returns the approval id.

        The run is the effect's own: a caller cannot record it under another run. Refused, and nothing
        written, when the effect is not a plugin effect or cannot be bound whole.
        """
        if type(effect) is not dict:
            raise ApprovalRefused("effect")
        run_id = effect.get("run_id")
        if type(run_id) is not str or not run_id:
            raise ApprovalRefused("effect")
        return self._queue.request("plugin_effect", effect, session_id=session_id, run_id=run_id,
                                   turn_id=turn_id)

    def carry(self, approval_id: str, effect: dict) -> dict:
        """Charge ``effect``, then consume its approval, once.

        Returns ``{"approval": record, "charge": receipt}``. The receipt is whatever the charge callable
        returned. A second carry raises ``ApprovalRefused("already_consumed")`` and does not charge.
        """
        if getattr(self._gate.busy, "on", False):
            raise RuntimeError("the plugin-effect sink cannot be carried from inside its own charge")
        with self._gate.lock:
            self._gate.busy.on = True
            try:
                return self._carry(approval_id, effect)
            finally:
                self._gate.busy.on = False

    def _held(self, approval_id: str):
        store = self._queue.store
        if store is None:
            raise ApprovalRefused("store_absent")
        with store.transaction() as tx:
            return rows.load(tx, approval_id)

    def _carry(self, approval_id: str, effect: dict) -> dict:
        record = self._held(approval_id)
        if record is None:
            raise ApprovalRefused("approval_unknown")
        if record["state"] == "consumed":
            raise ApprovalRefused("already_consumed")
        if record["state"] != "approved":
            raise ApprovalRefused("not_approved")
        if record.get("source") != "plugin_effect":
            raise ApprovalRefused("source")
        try:
            bound = effects.bind("plugin_effect", effect)
        except EffectRefused as refused:
            raise ApprovalRefused(refused.code) from None
        if bound.digest != record["digest"] or bound.effect != record["effect"]:
            raise ApprovalRefused("approval_mismatch")
        if record["run_id"] != record["effect"]["run_id"] or record["run_id"] != bound.effect["run_id"]:
            raise ApprovalRefused("envelope")
        if approval_id not in self._gate.spent:
            # Still approved: the consumption below is reached only if this call returns.
            receipt = self._charge(copy.deepcopy(record["effect"]), approval_id)
            self._gate.spent[approval_id] = receipt
        try:
            consumed = self._queue.consume(approval_id, record["revision"], record["digest"], None)
        except ApprovalRefused as refused:
            if refused.code != "already_consumed":
                raise
            consumed = self._held(approval_id)  # the charge consumed it inside its own commit
        return {"approval": consumed, "charge": self._gate.spent[approval_id]}
