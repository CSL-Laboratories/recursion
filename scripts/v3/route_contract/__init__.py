"""Route Contract v0 (draft, CSL-496). Domain-neutral and provider-free.

Projects the frozen M1B route and process shapes, adds execution and output
fields, and ships a reference executor that the conformance suite runs against
the AT1 fake driver. Every live adapter reports ``unsupported`` until CSL-460.
"""

from scripts.v3.route_contract.errors import (
    CwdRefused,
    RouteRefusal,
    SecretRefused,
    ToolPolicyRefused,
    Unsupported,
)
from scripts.v3.route_contract.executor import ReferenceAdapter
from scripts.v3.route_contract.health import InMemoryHealth
from scripts.v3.route_contract.live import (
    LIVE_ADAPTERS_ENABLED,
    UnsupportedLiveAdapter,
    live_adapter,
)
from scripts.v3.route_contract.model import ExecutionRequest, ExecutionResult
from scripts.v3.route_contract.transport import ProcResult, SubprocessTransport

__all__ = (
    "LIVE_ADAPTERS_ENABLED",
    "CwdRefused",
    "ExecutionRequest",
    "ExecutionResult",
    "InMemoryHealth",
    "ProcResult",
    "ReferenceAdapter",
    "RouteRefusal",
    "SecretRefused",
    "SubprocessTransport",
    "ToolPolicyRefused",
    "Unsupported",
    "UnsupportedLiveAdapter",
    "live_adapter",
)
