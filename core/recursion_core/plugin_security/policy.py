"""Recorded product direction for the same-uid fallback (B17, O69: the signed profile under in-process plugins).

Frozen text shared by the docs and the code. ``tests/test_o69_policy.py`` checks that
``README.md`` quotes it exactly. This records Lucas's Nov 8 direction, which PRODUCT.md 055cc7a5 does not
yet carry as accepted text; it changes no runtime check. Paper-only comes from PRODUCT.md 5.2 / CP0-v2.
"""

from __future__ import annotations

__all__ = ["SAME_UID_FALLBACK_POLICY", "SAME_UID_FALLBACK_POLICY_SOURCE"]

SAME_UID_FALLBACK_POLICY = "it should have permission to run paper testing nov 11 regardless"
SAME_UID_FALLBACK_POLICY_SOURCE = "Lucas, Nov 8 (t1418u), O69 correction; not yet in PRODUCT.md 055cc7a5"
