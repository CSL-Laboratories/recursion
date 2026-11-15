"""Lane APPROVALS' registrant (``recursion_core.approvals.hooks``, in SPINE's fixed list).

``register(hooks)`` runs once, at composition, before the daemon serves: it registers the queue as a
service (it declares its tables, recovers and runs the deadline worker when started), the three methods
(``approval.list`` with no tenant, a read; ``approval.open`` and ``approval.answer`` for the one install
tenant) and the ``approval`` command family. Nothing is changed after ``freeze``.
"""

from __future__ import annotations

from recursion_core.approvals.queue import ApprovalQueue
from recursion_core.control.commands import approval as approval_commands
from recursion_core.control.methods import INSTALL_TENANT
from recursion_core.control.methods.approval import handlers

__all__ = ["install", "register"]


def install(hooks, queue: ApprovalQueue) -> ApprovalQueue:
    hooks.service(queue)
    for name, handler in handlers(queue).items():
        hooks.methods.register(name, handler, tenant=None if name == "approval.list" else INSTALL_TENANT)
    hooks.commands.register(approval_commands.NAMES, approval_commands.parser, approval_commands.dispatch)
    return queue


def register(hooks) -> None:
    install(hooks, ApprovalQueue())
