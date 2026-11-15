"""approval.list, approval.open and approval.answer (lane APPROVALS; rows in SPINE's method table, beta).

* approval.list (scope read, no command id) returns summaries and counts as showing nothing.
* approval.open (scope approve, command id) is called by a surface when it actually displays the prompt: it
  records (principal, surface, revision, time) and returns the record with its rendered lines.
* approval.answer (scope approve, command id) is taken only from a principal that opened the current
  revision on that surface, for that digest, while the request is pending and before its deadline; the
  first valid answer wins and later ones are ``conflict``/``already_resolved``.

The dispatcher owns the transaction of the last two (``ctx.tx``): it looks up the receipt, calls the
handler, stores the answer and commits; waiters and drivers are told after the commit (``ctx.after_commit``).
A caller that is not an owner or a device, or that runs inside a run's unit, is ``scope_refused``; a
request that is not whole is ``request_invalid``. Both are raised as ``NotAccepted``, so no receipt is
kept and nothing changes. A durable record that is not the shape of an approval is the core's own refusal
(``record_invalid``): the handler raises, and the dispatcher answers ``unavailable`` and keeps nothing.
"""

from __future__ import annotations

from recursion_core.approvals import model
from recursion_core.control.methods import NotAccepted

__all__ = ["handlers"]


def _approver(ctx):
    """The caller as an approval names it, ``{"kind", "id"}``, or ``None`` when it may not answer."""
    principal = ctx.principal
    if principal is None or getattr(principal, "in_run_unit", False):
        return None
    public = principal.public() if hasattr(principal, "public") else principal
    return None if model.principal_refusal(public) is not None else dict(public)


def handlers(queue) -> dict:
    """The three handlers, over one ``ApprovalQueue``."""

    def approval_list(ctx, params: dict) -> dict:
        if queue.store is None:
            return {"code": "unsupported", "detail": "store_absent"}
        return queue.list_page(params)

    def gate(ctx, method: str, params: dict) -> dict:
        approver = _approver(ctx)
        if approver is None:
            raise NotAccepted({"code": "scope_refused", "detail": "approve"})
        refusal = model.params_refusal(method, params)
        if refusal is not None:
            raise NotAccepted(refusal)
        return approver

    def approval_open(ctx, params: dict) -> dict:
        approver = gate(ctx, "approval.open", params)
        return queue.open_in(ctx.tx, approver, params)

    def approval_answer(ctx, params: dict) -> dict:
        approver = gate(ctx, "approval.answer", params)
        return queue.answer_in(ctx.tx, approver, params, ctx.after_commit)

    return {"approval.list": approval_list, "approval.open": approval_open, "approval.answer": approval_answer}
