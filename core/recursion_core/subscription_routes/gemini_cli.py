"""Gemini CLI (E7) API-key route (candidate).

Google's deprecation note (developers.google.com/gemini-code-assist/docs/deprecations/code-assist-individuals)
says that on 2026-06-18 Gemini CLI stopped serving consumer accounts, including Login with Google.
The Developers Blog post (developers.googleblog.com/an-important-update-transitioning-gemini-cli-to-antigravity-cli/)
says the subscription path is Antigravity CLI, and that Gemini CLI stays available with a paid API key.
Google also says Code Assist Standard and Enterprise licences are on a separate schedule. Lucas's
2026-11-09 call keeps Gemini CLI in this module only as a paid API-key route. Subscription sign-in,
including a Code Assist licence, is refused here; Antigravity CLI is the beta wrapper in ``policy.py``.

``check_gemini_cli_sign_in`` decides one Gemini CLI route before any spawn and fails closed:

* ``api_key`` passes only when the route records the user's statement that the key is paid
  (``paid: True``). Whether a key is on a paid tier cannot be seen offline, and this module makes no
  provider call to find out.
* Any subscription sign-in, including ``code_assist``, is refused (``subscription_sign_in_dropped``
  or ``consumer_account_ended``).
* A method not named here is refused.

No Gemini CLI route adapter exists in this repository yet, so nothing calls this; the environment names
Gemini CLI reads are not in repository evidence and are not handled here. No credential is read.
T3 Code is background only and has no route here.
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
        return {"engine": "gemini", "method": "api_key", "paid": True, "subscription": False}
    if method == "code_assist" or method in CODE_ASSIST_LICENCES:
        raise GeminiCliRefused(
            "subscription_sign_in_dropped",
            "Gemini CLI subscription sign-in is dropped; paid API key only, Antigravity CLI is the subscription path",
        )
    raise GeminiCliRefused("method_unconfirmed", method)
