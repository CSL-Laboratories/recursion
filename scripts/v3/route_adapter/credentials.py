"""Public credential reference and a replaceable local resolver (D-RP2-3).

``CredentialRef`` is the public boundary. The local resolver is an
in-process stub: material stays in the resolver object and is not
returned to an adapter. A later broker can implement ``authorize``
without a public ``resolve(ref) -> raw string``. This stub does not
read or write process-global environment variables.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from scripts.v3.route_adapter.errors import CustodyDenied


@dataclass(frozen=True)
class CredentialRef:
    """Opaque reference. The secret is not a field."""

    tenant_id: str
    provider: str
    name: str

    def __post_init__(self) -> None:
        for label, value in (
            ("tenant_id", self.tenant_id),
            ("provider", self.provider),
            ("name", self.name),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{label} must be a non-empty string")

    def public_dict(self) -> dict[str, str]:
        return {
            "tenant_id": self.tenant_id,
            "provider": self.provider,
            "name": self.name,
        }


@dataclass(frozen=True)
class InvocationHandle:
    """What an adapter receives after authorization. No secret material."""

    ref: CredentialRef
    accepted: bool
    delivery: str


class CredentialResolver(Protocol):
    """Replaceable authorization boundary.

    Implementations return a handle. They do not return raw credential
    material through this method.
    """

    def authorize(self, ref: CredentialRef, *, caller_tenant: str) -> InvocationHandle:
        """Accept or refuse ``ref`` for ``caller_tenant``."""


class LocalCredentialResolver:
    """Tenant-scoped stub. Delivery is runtime-visible inside this object only.

    ``runtime-visible-stub`` means the secret exists in this process, in
    this object, for a later in-process executor. Adapters see the handle.
    Broker-owned egress is a future implementation of the same
    ``authorize`` shape, not this class.
    """

    delivery = "runtime-visible-stub"

    def __init__(self) -> None:
        self._material: dict[tuple[str, str, str], str] = {}

    def __repr__(self) -> str:
        return f"LocalCredentialResolver(refs={len(self._material)})"

    def register(self, ref: CredentialRef, material: str) -> None:
        if not isinstance(material, str) or not material:
            raise CustodyDenied("empty-material")
        self._material[(ref.tenant_id, ref.provider, ref.name)] = material

    def authorize(self, ref: CredentialRef, *, caller_tenant: str) -> InvocationHandle:
        if caller_tenant != ref.tenant_id:
            raise CustodyDenied("cross-tenant-denied")
        if (ref.tenant_id, ref.provider, ref.name) not in self._material:
            raise CustodyDenied("unknown-ref")
        return InvocationHandle(ref=ref, accepted=True, delivery=self.delivery)

    def public_refs(self) -> tuple[CredentialRef, ...]:
        return tuple(
            CredentialRef(tenant_id=tenant_id, provider=provider, name=name)
            for tenant_id, provider, name in self._material
        )
