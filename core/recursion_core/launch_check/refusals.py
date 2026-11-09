"""Typed refusals for the R2a launch check (CSL-732, candidate).

Every failure in this package is a ``LaunchRefused`` carrying one code from a
closed, versioned set. No function returns a grant or a launch plan after an
error: the only way out of a failed check is this exception.
"""

from __future__ import annotations

REFUSAL_CODES_VERSION = "r2a-refusals/1.2.0-candidate-2026-11-09"

# Closed set. Adding, removing or renaming a code is a new REFUSAL_CODES_VERSION.
REFUSAL_CODES = frozenset(
    {
        "engine_unknown",  # engine id not in the floor table (detected allow-list)
        "path_not_absolute",  # grant path is relative
        "binary_missing",  # path, target or probe binary absent or unspawnable
        "wrapper_refused",  # script, shim, interpreter or non-native file
        "name_mismatch",  # file stem is not one of the engine's binary names
        "version_probe_failed",  # --version exited non-zero, timed out or flooded
        "binary_changed",  # digest or resolution differs from the grant
        "engine_mismatch",  # version line names another engine
        "identity_mismatch",  # version line does not name an engine whose real line names it
        "identity_unverified",  # engine has no identity rule or pinned digest (refused before probing)
        "version_unparsed",  # version token does not match the engine's format
        "floor_unset",  # a floor in the table does not parse
        "version_refused",  # below a floor, or a prerelease
        "argv_empty",  # launch plan with no argv
        "argv0_mismatch",  # argv[0] is not the bare engine name
        "grant_forged",  # not issued by grant(), or fields altered
        "grant_stale",  # issued under another floor table or refusal-code version
        "spawn_too_large",  # argv/env over the byte caps, or not text
    }
)


class LaunchRefused(Exception):
    """A typed launch-check refusal. ``code`` is always in ``REFUSAL_CODES``."""

    def __init__(self, code: str, engine: str, detail: str = "") -> None:
        if code not in REFUSAL_CODES:
            # A programming error must still fail closed, never as a grant.
            raise ValueError(f"unknown refusal code {code!r}")
        super().__init__(f"{code}: {engine}: {detail}")
        self.code = code
        self.engine = engine
        self.detail = detail

    def record(self) -> dict[str, str]:
        from recursion_core.launch_check.floors import FLOOR_TABLE_VERSION

        return {
            "outcome": "refused",
            "refusal": self.code,
            "engine": self.engine,
            "detail": self.detail,
            "floor_table": FLOOR_TABLE_VERSION,
            "refusal_codes": REFUSAL_CODES_VERSION,
        }
