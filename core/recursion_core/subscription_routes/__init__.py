"""Claude-subscription route refusal for Pi, Hermes and OpenCode profiles, plus the Nov 8 Copilot and Nous
own-loop routes (CSL-786, candidate). See README.md."""

from recursion_core.subscription_routes.gemini_cli import GeminiCliRefused, check_gemini_cli_sign_in
from recursion_core.subscription_routes.own_loop import (
    COPILOT_STRIP_ENV,
    COPILOT_TOKEN_ENV,
    NOUS_ADAPTER,
    OwnLoopDecision,
    OwnLoopRouteRefused,
    check_copilot_own_loop,
    check_nous_own_loop,
    nous_proxy_doctor,
)
from recursion_core.subscription_routes.policy import (
    CLAUDE_SUBSCRIPTION_ENV,
    ENGINE_SELECTOR_ENV,
    ENGINE_STORE_ENTRIES,
    ENGINES,
    HERMES_ADOPT_EXTERNAL_LOGINS,
    SAFE_ENV,
    SUBSCRIPTION_SIGN_INS,
    RouteDecision,
    SubscriptionRouteRefused,
    check_route,
    guard_then_spawn,
)

__all__ = [
    "COPILOT_STRIP_ENV",
    "COPILOT_TOKEN_ENV",
    "NOUS_ADAPTER",
    "OwnLoopDecision",
    "OwnLoopRouteRefused",
    "check_copilot_own_loop",
    "check_nous_own_loop",
    "nous_proxy_doctor",
    "GeminiCliRefused",
    "check_gemini_cli_sign_in",
    "CLAUDE_SUBSCRIPTION_ENV",
    "ENGINES",
    "ENGINE_SELECTOR_ENV",
    "HERMES_ADOPT_EXTERNAL_LOGINS",
    "ENGINE_STORE_ENTRIES",
    "RouteDecision",
    "SAFE_ENV",
    "SUBSCRIPTION_SIGN_INS",
    "SubscriptionRouteRefused",
    "check_route",
    "guard_then_spawn",
]
