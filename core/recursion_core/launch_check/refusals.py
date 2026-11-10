"""Typed refusals for the R2a launch check (CSL-732, candidate).

Every failure in this package is a ``LaunchRefused`` carrying one code from a
closed, versioned set. No function returns a grant or a launch plan after an
error: the only way out of a failed check is this exception.
"""

from __future__ import annotations

# 1.6.0: 20 codes; adds pin_entry_unset. A pinned route's Windows .cmd/.ps1 launcher is a check
# only and is never executed (Lucas, 2026-11-10: "accept a pinned .cmd/.ps1 launcher as a check,
# but execute the pinned entry directly"); with no entry in the pin, the launch is refused.
# 1.5.0: the same 19 codes as 1.4.0; a Windows .cmd/.ps1 launcher on a pinned route moves from
# wrapper_refused to the pin check (pin_mismatch unless pinned).
REFUSAL_CODES_VERSION = "r2a-refusals/1.6.0-candidate-2026-11-10"

# Closed set. Adding, removing or renaming a code is a new REFUSAL_CODES_VERSION.
REFUSAL_CODES = frozenset(
    {
        "engine_unknown",  # engine id not in the floor table (detected allow-list)
        "path_not_absolute",  # grant path is relative
        "binary_missing",  # path, target or probe binary absent or unspawnable
        # script, shim, interpreter or non-native file (not a pinned route's Windows .cmd/.ps1 launcher)
        "wrapper_refused",
        "name_mismatch",  # file stem is not one of the engine's binary names
        "version_probe_failed",  # --version exited non-zero, timed out or flooded
        "binary_changed",  # digest or resolution differs from the grant
        "engine_mismatch",  # version line names another engine
        "identity_mismatch",  # version line does not name an engine whose real line names it
        "identity_unverified",  # engine has no identity rule or pinned digest (refused before probing)
        # pinned route: launcher (on Windows also <name>.cmd/.ps1), package files or binary match no
        # recorded pin (until re-pinned); an archive-sourced pin or a bad pin entry is never matched
        "pin_mismatch",
        # pinned Windows .cmd/.ps1 launcher accepted as a check, but its pin records no entry to execute
        "pin_entry_unset",
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
