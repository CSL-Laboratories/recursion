"""Recorded product direction for the same-uid fallback (B17, O69).

Frozen text shared by the docs and the code. ``tests/test_o69_policy.py`` checks that
``README.md`` quotes it exactly. This records a decision; it changes no runtime check.
"""

from __future__ import annotations

__all__ = ["SAME_UID_FALLBACK_POLICY", "SAME_UID_FALLBACK_POLICY_SOURCE"]

SAME_UID_FALLBACK_POLICY = (
    "the in-process Cockpit plugin holds paper-order permission on Nov 11 under the same-uid"
    " fallback. The signed authority profile is tamper evidence there, not a boundary, and"
    " the docs say so. Paper only; live capital stays separately authorised (5.2)."
)
SAME_UID_FALLBACK_POLICY_SOURCE = "Lucas, Nov 8 (t1415u), PRODUCT.md 055cc7a5 O69"
