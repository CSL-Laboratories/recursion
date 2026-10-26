"""AT1 route-adapter contracts (CSL-537).

In-process fake driver, per-session ownership, and credential references.
This package does not call a live provider.
"""

from scripts.v3.route_adapter.credentials import (
    CredentialRef,
    CredentialResolver,
    InvocationHandle,
    LocalCredentialResolver,
)
from scripts.v3.route_adapter.descriptor import (
    CODEX_APP_SERVER_PROOF,
    CONTROL_MCP_CALLER_ONLY_ADMITTED,
    F5_COUNSEL_GATE,
    SUBSCRIPTION_FANOUT_IN_AT1,
    RouteSurfaceDescriptor,
    admit_experimental_driver,
    grok_rows,
    require_vendor_experimental_api,
)
from scripts.v3.route_adapter.errors import Busy, CustodyDenied, RouteAdapterError, StaleGeneration
from scripts.v3.route_adapter.fake import FakeRouteDriver
from scripts.v3.route_adapter.session import (
    CONTENTION,
    DISTRIBUTED_FENCING,
    Operation,
    SessionKey,
    SessionOwner,
)

__all__ = (
    "BUSY_CONTENTION",
    "CODEX_APP_SERVER_PROOF",
    "CONTENTION",
    "CONTROL_MCP_CALLER_ONLY_ADMITTED",
    "DISTRIBUTED_FENCING",
    "F5_COUNSEL_GATE",
    "SUBSCRIPTION_FANOUT_IN_AT1",
    "Busy",
    "CredentialRef",
    "CredentialResolver",
    "CustodyDenied",
    "FakeRouteDriver",
    "InvocationHandle",
    "LocalCredentialResolver",
    "Operation",
    "RouteAdapterError",
    "RouteSurfaceDescriptor",
    "SessionKey",
    "SessionOwner",
    "StaleGeneration",
    "admit_experimental_driver",
    "grok_rows",
    "require_vendor_experimental_api",
)

BUSY_CONTENTION = CONTENTION
