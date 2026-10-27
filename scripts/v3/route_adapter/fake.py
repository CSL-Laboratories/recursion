"""In-process fake route driver for contract tests.

The start, send, observe, and cancel sequence here is fixture coverage.
It is not a live Codex, Claude, or Grok run.
"""

from __future__ import annotations

from scripts.v3.route_adapter.credentials import (
    CredentialRef,
    CredentialResolver,
    LocalCredentialResolver,
)
from scripts.v3.route_adapter.errors import CustodyDenied
from scripts.v3.route_adapter.session import (
    Operation,
    SessionKey,
    SessionOwner,
    process_session_owner,
)


class FakeRouteDriver:
    """Contract driver. No subprocess, no network, no live credential use."""

    def __init__(
        self,
        owner: SessionOwner | None = None,
        resolver: CredentialResolver | None = None,
    ) -> None:
        self.owner = process_session_owner() if owner is None else owner
        self.resolver = resolver or LocalCredentialResolver()

    def start(
        self,
        key: SessionKey,
        ref: CredentialRef,
        *,
        caller_tenant: str,
    ) -> Operation:
        if key.tenant != caller_tenant or ref.tenant_id != caller_tenant:
            raise CustodyDenied("tenant-mismatch")
        handle = self.resolver.authorize(ref, caller_tenant=caller_tenant)
        if not handle.accepted:
            raise CustodyDenied("not-accepted")
        if handle.ref != ref:
            raise CustodyDenied("ref-mismatch")
        return self.owner.begin(key, credential=ref.public_dict())

    def send(self, operation: Operation, message: str) -> None:
        self.owner.send(operation, message)

    def observe(self, operation: Operation) -> tuple[dict[str, object], ...]:
        return self.owner.observe(operation)

    def cancel(self, operation: Operation) -> bool:
        return self.owner.cancel(operation)

    def complete(self, operation: Operation) -> bool:
        return self.owner.complete(operation)

    def snapshot(self) -> dict[str, object]:
        return self.owner.snapshot()
