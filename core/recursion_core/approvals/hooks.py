"""Lane APPROVALS' registrant (``recursion_core.approvals.hooks``, in SPINE's fixed list).

``register(hooks)`` runs once, at composition, before the daemon serves: it registers the queue as a
service (it declares its tables, recovers and runs the deadline worker when started), the three methods
(``approval.list`` with no tenant, a read; ``approval.open`` and ``approval.answer`` for the one install
tenant) and the ``approval`` command family. Nothing is changed after ``freeze``.

The tenant of the two commands is ``install_tenant``: the dispatcher derives it before it looks a receipt
up, so a command id outside common.v1's grammar is refused there, with no receipt read or kept and nothing
changed. A well-formed id is in ``INSTALL_TENANT``'s namespace, as before.
"""

from __future__ import annotations

import weakref

from recursion_core.approvals import model
from recursion_core.approvals.queue import ApprovalQueue
from recursion_core.control.commands import approval as approval_commands
from recursion_core.control.methods import INSTALL_TENANT, NotAccepted
from recursion_core.control.methods.approval import handlers

__all__ = ["install", "install_tenant", "queue_of", "register"]

# The queue each composition registered, so that a driver composed with the same hooks reaches THAT queue
# (``queue_of(hooks)``) and never builds a second one. Weak: a composition that is gone frees its queue.
_QUEUES: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


# The lane's own check, until SPINE's dispatcher checks the command id of every command method before it looks a
# receipt up (asked of SPINE: it is on SPINE's follow-up list). The dispatcher itself refuses only a missing or
# empty id (``request_invalid``/``command_id``, in ``_command``), and its docstring names ``request_invalid`` for
# another case, params whose digest has no text; neither holds the id to common.v1's grammar. Once the
# dispatcher does, these two methods go back to ``INSTALL_TENANT`` itself.
def install_tenant(principal, params) -> str:
    """The receipt tenant of approval.open and approval.answer: the whole install, ``INSTALL_TENANT``. A
    command id outside its grammar is refused here (``NotAccepted``, ``request_invalid``/``command_id``),
    which the dispatcher calls before it looks a receipt up."""
    refusal = model.command_id_refusal(params)
    if refusal is not None:
        raise NotAccepted(refusal)
    return INSTALL_TENANT


def install(hooks, queue: ApprovalQueue) -> ApprovalQueue:
    hooks.service(queue)
    for name, handler in handlers(queue).items():
        hooks.methods.register(name, handler, tenant=None if name == "approval.list" else install_tenant)
    hooks.commands.register(approval_commands.NAMES, approval_commands.parser, approval_commands.dispatch)
    _QUEUES[hooks] = queue
    return queue


def queue_of(hooks) -> ApprovalQueue:
    """The approval queue registered into this composition: drivers call ``ask(effect, binding)``, ``wait``,
    ``consume`` and ``subscribe_answered`` on it. ``KeyError`` when the composition has none."""
    return _QUEUES[hooks]


def register(hooks) -> None:
    install(hooks, ApprovalQueue())
