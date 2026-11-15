"""Lane APPROVALS' registrant (``recursion_core.approvals.hooks``, in SPINE's fixed list).

``register(hooks)`` runs once, at composition, before the daemon serves: it registers the queue as a
service (it declares its tables, recovers and runs the deadline worker when started), the three methods
(``approval.list`` with no tenant, a read; ``approval.open`` and ``approval.answer`` for the one install
tenant) and the ``approval`` command family. Nothing is changed after ``freeze``.
"""

from __future__ import annotations

import weakref

from recursion_core.approvals.queue import ApprovalQueue
from recursion_core.control.commands import approval as approval_commands
from recursion_core.control.methods import INSTALL_TENANT
from recursion_core.control.methods.approval import handlers

__all__ = ["install", "queue_of", "register"]

# The queue each composition registered, so that a driver composed with the same hooks reaches THAT queue
# (``queue_of(hooks)``) and never builds a second one. Weak: a composition that is gone frees its queue.
_QUEUES: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def install(hooks, queue: ApprovalQueue) -> ApprovalQueue:
    hooks.service(queue)
    for name, handler in handlers(queue).items():
        hooks.methods.register(name, handler, tenant=None if name == "approval.list" else INSTALL_TENANT)
    hooks.commands.register(approval_commands.NAMES, approval_commands.parser, approval_commands.dispatch)
    _QUEUES[hooks] = queue
    return queue


def queue_of(hooks) -> ApprovalQueue:
    """The approval queue registered into this composition: drivers call ``ask(effect, binding)``, ``wait``,
    ``consume`` and ``subscribe_answered`` on it. ``KeyError`` when the composition has none."""
    return _QUEUES[hooks]


def register(hooks) -> None:
    install(hooks, ApprovalQueue())
