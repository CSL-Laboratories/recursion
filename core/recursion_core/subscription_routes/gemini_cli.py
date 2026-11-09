"""Gemini CLI (E7) sign-in policy (Nov 8 briefing, candidate).

Lucas's Nov 8 briefing: "consumer accounts ended June 18, 2026. Only paid API keys and existing Code Assist
Standard or Enterprise licences work."

``check_gemini_cli_sign_in`` decides one Gemini CLI route before any spawn and fails closed:

* ``api_key`` passes only when the route records the user's statement that the key is paid
  (``paid: True``). Whether a key is on a paid tier cannot be seen offline, and this module makes no
  provider call to find out.
* ``code_assist`` passes only for a ``standard`` or ``enterprise`` licence the user already holds
  (``existing: True``). Recursion does not buy, start or upgrade a licence.
* A Google consumer sign-in, a Google One / AI plan, a free tier, or any method not named here is refused.

No Gemini CLI route adapter exists in this repository yet, so nothing calls this; the environment names
Gemini CLI reads are not in repository evidence and are not handled here. No credential is read.
"""

from __future__ import annotations

from collections.abc import Mapping

CONSUMER_END = "2026-06-18"
CODE_ASSIST_LICENCES = ("standard", "enterprise")
_CONSUMER = ("login_with_google", "google_account", "personal", "consumer", "google_one", "ai_pro",
             "ai_ultra", "free", "free_tier", "oauth", "subscription")


class GeminiCliRefused(Exception):
    code = "gemini_cli_route_refused"

    def __init__(self, cause: str, detail: str = "") -> None:
        super().__init__(f"gemini: {cause}" + (f" ({detail})" if detail else ""))
        self.cause, self.detail = cause, detail


def _norm(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return "_".join(value.strip().lower().replace("-", " ").replace("_", " ").split())


def check_gemini_cli_sign_in(route: Mapping[str, object]) -> dict:
    """Return the allowed decision or raise ``GeminiCliRefused``. ``route`` keys: method, paid, licence, existing."""

    if not isinstance(route, Mapping):
        raise GeminiCliRefused("route_malformed")
    unknown = sorted(set(route) - {"method", "paid", "licence", "existing"})
    if unknown:
        raise GeminiCliRefused("route_unknown_field", ",".join(unknown))
    method = _norm(route.get("method"))
    if not method:
        raise GeminiCliRefused("method_missing")
    if method in _CONSUMER:
        raise GeminiCliRefused("consumer_account_ended", f"Gemini CLI consumer accounts ended {CONSUMER_END}")
    if method == "api_key":
        if route.get("paid") is not True:
            raise GeminiCliRefused("api_key_not_paid", "only a paid API key works; free tier refused")
        if "licence" in route or "existing" in route:
            raise GeminiCliRefused("route_mixed_methods")
        return {"engine": "gemini", "method": "api_key", "paid": True}
    if method == "code_assist":
        licence = _norm(route.get("licence"))
        if licence not in CODE_ASSIST_LICENCES:
            raise GeminiCliRefused("code_assist_licence_not_allowed", licence or "missing")
        if route.get("existing") is not True:
            raise GeminiCliRefused("code_assist_licence_not_existing", "only an existing licence works")
        if "paid" in route:
            raise GeminiCliRefused("route_mixed_methods")
        return {"engine": "gemini", "method": "code_assist", "licence": licence, "existing": True}
    raise GeminiCliRefused("method_unconfirmed", method)
