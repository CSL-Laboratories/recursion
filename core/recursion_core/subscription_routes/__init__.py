"""Claude-subscription route refusal for Pi, Hermes and OpenCode profiles (CSL-786, candidate). See README.md."""

from recursion_core.subscription_routes.gemini_cli import GeminiCliRefused, check_gemini_cli_sign_in
from recursion_core.subscription_routes.policy import (
    CLAUDE_SUBSCRIPTION_ENV,
    ENGINE_SELECTOR_ENV,
    ENGINE_STORE_ENTRIES,
    ENGINES,
    SAFE_ENV,
    SUBSCRIPTION_SIGN_INS,
    RouteDecision,
    SubscriptionRouteRefused,
    check_route,
    guard_then_spawn,
)

__all__ = [
    "GeminiCliRefused",
    "check_gemini_cli_sign_in",
    "CLAUDE_SUBSCRIPTION_ENV",
    "ENGINES",
    "ENGINE_SELECTOR_ENV",
    "ENGINE_STORE_ENTRIES",
    "RouteDecision",
    "SAFE_ENV",
    "SUBSCRIPTION_SIGN_INS",
    "SubscriptionRouteRefused",
    "check_route",
    "guard_then_spawn",
]
