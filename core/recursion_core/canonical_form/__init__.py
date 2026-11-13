"""CSL canonical form v1 and the Run@1 two-field vectors (CSL-718, F9; candidate).

Builds on the Recursion-Cockpit contract proposed in csl-research PR #1100 at 114b48be,
limited to ``Run@1.outcome`` and ``Run@1.breaker_tripped``. Standard library only.
"""

from recursion_core.canonical_form.canonical import (
    CANONICAL_FORM,
    CanonicalError,
    canonical_bytes,
    check_value,
    digest,
    digest_text,
    is_full_digest,
    loads_strict,
)
from recursion_core.canonical_form.run_fields import RUN_OUTCOMES, RUN_REQUIRED, TWO_FIELDS, run_field_errors

__all__ = [
    "CANONICAL_FORM", "CanonicalError", "RUN_OUTCOMES", "RUN_REQUIRED", "TWO_FIELDS", "canonical_bytes",
    "check_value", "digest", "digest_text", "is_full_digest", "loads_strict", "run_field_errors",
]
