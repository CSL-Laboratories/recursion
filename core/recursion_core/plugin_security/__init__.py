"""Plugin loader (G2), core approval gate and connection breaker (O11), authority profiles (K3).

Candidate code for build-order item 9. In-process plugins are *not* contained: see
``loader.py`` and ``authority.py`` for what same-uid plugin code can still do.
"""

from .approval import EFFECT_SCHEMA, ApprovalGate, check_effect, effect_digest
from .authority import PROFILE_SCHEMA, Authority, create_ledger
from .breaker import TYPED_LIMITS, ConnectionBreaker, create_breaker_store
from .canonical import canonical, parse
from .errors import Refused
from .loader import CoreHealth, LoadedPlugin, PluginLoader
from .manifest import (MANIFEST_SCHEMA, PERMISSIONS, Grant, ReservedEffects, TrustStore,
                       create_revocation_store, parse_manifest)
from .policy import SAME_UID_FALLBACK_POLICY, SAME_UID_FALLBACK_POLICY_SOURCE
from .protocol import HOST, PROTOCOL, PROTOCOL_VERSION, CoreService
from .runtime import GilProbe, require_production_runtime, runtime_report

__all__ = [
    "EFFECT_SCHEMA", "HOST", "MANIFEST_SCHEMA", "PERMISSIONS", "PROFILE_SCHEMA", "PROTOCOL",
    "PROTOCOL_VERSION", "SAME_UID_FALLBACK_POLICY", "SAME_UID_FALLBACK_POLICY_SOURCE",
    "TYPED_LIMITS", "ApprovalGate", "Authority", "ConnectionBreaker",
    "CoreHealth", "CoreService", "GilProbe", "Grant", "LoadedPlugin", "PluginLoader", "Refused",
    "ReservedEffects", "TrustStore", "canonical", "check_effect", "create_breaker_store", "create_ledger",
    "create_revocation_store",
    "effect_digest",
    "parse", "parse_manifest", "require_production_runtime", "runtime_report",
]
