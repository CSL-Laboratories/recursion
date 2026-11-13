"""Recorded product direction for the same-uid fallback (B17, O69: the signed profile under in-process plugins).

Frozen text shared by the docs and the code. ``tests/test_o69_policy.py`` checks that
``README.md`` quotes it exactly. This records Lucas's 2026-11-08 direction. The quote
is not accepted text in CSL-Laboratories/csl-research contracts/PRODUCT.md
055cc7a5c793b7135e5d0565dc4198cf96230531 (SHA-256
e64783152f6d67d4ede774dc41d112fe46c2d7536d53cb2c04e71a3566044daf, receipt CSLR-D-011).
It changes no runtime check. Paper-only comes from PRODUCT.md 5.2 / CP0-v2.
"""

from __future__ import annotations

__all__ = ["SAME_UID_FALLBACK_POLICY", "SAME_UID_FALLBACK_POLICY_SOURCE"]

SAME_UID_FALLBACK_POLICY = "it should have permission to run paper testing nov 11 regardless"
SAME_UID_FALLBACK_POLICY_SOURCE = (
    "Lucas, 2026-11-08 (t1418u), O69 correction; not accepted text in "
    "CSL-Laboratories/csl-research contracts/PRODUCT.md "
    "055cc7a5c793b7135e5d0565dc4198cf96230531 "
    "(SHA-256 e64783152f6d67d4ede774dc41d112fe46c2d7536d53cb2c04e71a3566044daf); "
    "receipt CSLR-D-011; claim is the quoted sentence; limitation is that the sentence "
    "is not accepted PRODUCT.md text and paper-only is PRODUCT.md 5.2 / CP0-v2"
)
