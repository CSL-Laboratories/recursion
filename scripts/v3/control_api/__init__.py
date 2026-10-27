"""Recursion v3 Control API, spine slice (CSL-495, CSL-498, CSL-503; WP16).

Authenticated JSON-RPC 2.0 plus a tenant-filtered observer journal over the
Route Contract v0 reference adapters (CSL-496). Fake driver only: every live
adapter stays ``unsupported`` until CSL-460, and nothing here spawns a provider.
"""

from scripts.v3.control_api.auth import (
    SCOPE_CONTROL,
    SCOPE_OBSERVE,
    Principal,
    TokenRegistry,
)
from scripts.v3.control_api.errors import ControlError
from scripts.v3.control_api.events import EventJournal
from scripts.v3.control_api.service import API_VERSION, ControlService, make_validator

__all__ = (
    "API_VERSION",
    "SCOPE_CONTROL",
    "SCOPE_OBSERVE",
    "ControlError",
    "ControlService",
    "EventJournal",
    "Principal",
    "TokenRegistry",
    "make_validator",
)
