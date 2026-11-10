"""Versioned floor table and strict version parsing for R2a (CSL-732, candidate).

The table is the core's detected allow-list: an engine id that is not here is
refused (``engine_unknown``). Each engine has two separate slots:

* ``functional``: the Q43 functional floor (PRODUCT.md O50, 11.6), the lowest
  version that passes the conformance cells. ``None`` means no functional floor
  is recorded. O50 ("Beta until measured") does not refuse the engine for that:
  the grant is tagged ``beta`` for route.list, and only known security floors
  are checked.
* ``security``: a relayed security floor (hygiene, not containment; 11.6).

A grant needs ``found >= security`` (when set). When a functional floor is set,
it also needs ``found >= functional``. When it is unset, the engine still runs
under the beta tag.
Identity is checked positively only where the real version line is recorded and
names the engine (``ident_required``): claude_code (``2.1.318 (Claude Code)``),
codex (``codex-cli 0.160.0``) and opencode (``opencode v2.2.5``). For these, a line
that does not match ``ident`` is refused (``identity_mismatch``). An engine with no
identity rule and no pin is refused (``identity_unverified``) before its binary is
run. For grok and gemini the real line is bare or unread, so no format is invented:
a native binary with the right name printing a bare version that passes the floors
is granted, and the only identity check is that the line names no other engine.
Whether those two need a pinned digest or are accepted as is, is open for Lucas.

Pinned routes (``pin``; Lucas's owner call of 2026-11-09): pi, hermes and cursor
(``launcher+tree``) and antigravity (``binary+launcher``) are granted only when the
install matches a pin in ``pins.py``; the pin is their identity and their version,
nothing is run, and the grant is beta. Any mismatch is ``pin_mismatch`` until
re-pinned. Gemini CLI is kept as an API-key route only (``api_key_only``): Google
ended sign-in for it on 2026-06-18 and the Google subscription route is Antigravity
CLI (``agy``). A Gemini launch plan needs ``auth="api_key"``; anything else is
``sign_in_refused``. It gets no pin, so a real Gemini CLI (a JS bundle) stays refused.
A floor change is a new ``FLOOR_TABLE_VERSION``, never an in-place edit; grants
issued under another table version are refused (``grant_stale``).
"""

from __future__ import annotations

import datetime

import re
from dataclasses import dataclass
from types import MappingProxyType

from recursion_core.launch_check import pins
from recursion_core.launch_check.refusals import LaunchRefused

FLOOR_TABLE_VERSION = "r2a-floors/1.4.0-candidate-2026-11-09"


@dataclass(frozen=True)
class EngineSpec:
    engine: str
    names: tuple[str, ...]  # bare binary names; argv[0] must be one of these
    scheme: str  # "semver" or "date"
    functional: str | None
    security: str | None
    functional_basis: str
    security_basis: str
    ident: str | None = None  # regex naming this engine in a version line
    ident_required: bool = False  # the real line names the engine: require an ident match
    probe_name: str | None = None  # sibling binary read for the version, if not the engine itself
    probe_env: tuple[tuple[str, str], ...] = ()  # extra fixed probe variables (no secrets)
    pin: str | None = None  # pinned route kind (pins.KINDS): granted only on a pin match, as beta
    api_key_only: bool = False  # no subscription sign-in route: a launch plan needs auth="api_key"
    note: str = ""  # route note appended to this engine's wrapper and pin refusals


_SPECS = (
    EngineSpec(
        "claude_code", ("claude",), "semver", "2.1.314", "2.1.260",
        "decided by Lucas (O50, grill Q43); not yet measured as the lowest passing version (CX-956)",
        "PRODUCT.md 11.6",
        ident=r"\bclaude\b", ident_required=True,
        probe_env=(("DISABLE_AUTOUPDATER", "1"), ("DISABLE_UPDATES", "1")),
    ),
    # Codex: 0.131.0 is a security floor only (A-35, 11.6). There is no measured
    # functional floor, so Codex runs as beta until measured (O50). rust-v0.145.0
    # (first tag with all 15 --disable keys, PRODUCT.md 11.6) is an unmeasured
    # candidate and is NOT applied here.
    EngineSpec(
        "codex", ("codex",), "semver", None, "0.131.0",
        "O50: no conformance measurement; tagged beta until measured. rust-v0.145.0 "
        "is an unmeasured candidate (11.6) and is not applied",
        "A-35, PRODUCT.md 11.6",
        ident=r"\bcodex", ident_required=True,
    ),
    EngineSpec(
        "opencode", ("opencode", "opencode2"), "semver", None, "1.18.33",
        "no conformance measurement", "D2-72 (CVE map 1.18.22, raised to 1.18.33)",
        ident=r"\bopencode", ident_required=True,
    ),
    EngineSpec(
        "grok", ("grok",), "semver", None, None,
        "O50: Grok's once measured; not measured", "none derived (D2-72)",
        ident=r"\bgrok\b",
    ),
    # Antigravity CLI: the Google subscription route since Gemini CLI stopped serving
    # individual accounts (2026-06-18). One Go binary; pinned by version, binary and launcher.
    EngineSpec(
        "antigravity", ("agy",), "semver", None, None,
        "no conformance measurement; pinned beta route", "none recorded",
        ident=r"\bantigravity\b|\bagy\b",
        pin="binary+launcher",
        note="Antigravity CLI (agy) runs as beta only when its version, binary and launcher match a pin",
    ),
    EngineSpec(
        "cursor", ("agent", "cursor-agent"), "date", None, "2025.09.17-25b418f",
        "no conformance measurement; pinned beta route", "D2-72 (codex-verify348 record)",
        ident=r"\bcursor\b",
        pin="launcher+tree",
    ),
    # Gemini CLI: API-key route only. No pin, so the real JS bundle stays wrapper_refused.
    EngineSpec(
        "gemini", ("gemini",), "semver", None, "0.39.1",
        "no conformance measurement", "D2-72 (CVE map)",
        ident=r"\bgemini\b",
        api_key_only=True,
        note="Gemini CLI is kept as an API-key route only; the Google subscription route is Antigravity CLI (agy)",
    ),
    # Pi, Hermes: a JS bundle and a Python entry point (11.6). "pi" is too short to search
    # for safely, so Pi has no ident; for both, the pin is the identity and the version.
    EngineSpec(
        "pi", ("pi",), "semver", None, None,
        "no record; pinned beta route", "none recorded",
        ident=None,
        pin="launcher+tree",
    ),
    EngineSpec(
        "hermes", ("hermes",), "semver", None, None,
        "no record; pinned beta route", "none recorded",
        ident=r"\bhermes\b",
        pin="launcher+tree",
    ),
)

FLOORS: MappingProxyType[str, EngineSpec] = MappingProxyType({s.engine: s for s in _SPECS})

MAX_VERSION_LINE = 256
_NUM = r"(0|[1-9]\d{0,8})"
_SEMVER = re.compile(
    rf"v?{_NUM}\.{_NUM}\.{_NUM}(?:-([0-9A-Za-z][0-9A-Za-z.-]*))?(\+[0-9A-Za-z.-]+)?"
)
# Cursor: YYYY.MM.DD, optional -HH-MM-SS build time, optional -<hex commit>.
_DATE = re.compile(
    r"v?(\d{4})\.(\d{1,2})\.(\d{1,2})(?:-(\d{2})-(\d{2})-(\d{2}))?(?:-([0-9a-f]{6,40}))?"
)
_VERSIONISH = re.compile(r"\d+\.\d+")


@dataclass(frozen=True)
class ParsedVersion:
    key: tuple[int, ...]  # comparison key
    text: str  # normalised display form, e.g. "2.1.318" or "2026.10.1"
    prerelease: str | None = None


def spec_for(engine: str) -> EngineSpec:
    spec = FLOORS.get(engine) if isinstance(engine, str) else None
    if spec is None:
        raise LaunchRefused("engine_unknown", str(engine), "not in the floor table (detected allow-list)")
    return spec


def _token(raw: str) -> str | None:
    for word in raw.split():
        word = word.strip("()[]{},;:")
        if _VERSIONISH.search(word):
            return word
    return None


def parse_version(scheme: str, raw: str) -> ParsedVersion | None:
    """Strict parse. ``None`` means unparsed (extra parts, glued letters, ``+build``, bad date).

    The version token is the first whitespace-separated word holding ``digit.digit``;
    it must match the engine's whole format. A prerelease is returned, not accepted:
    callers refuse it (floors name releases).
    """
    if not isinstance(raw, str) or len(raw) > MAX_VERSION_LINE:
        return None
    token = _token(raw)
    if token is None:
        return None
    if scheme == "date":
        m = _DATE.fullmatch(token)
        if not m:
            return None
        y, mo, d, hh, mi, ss, _commit = m.groups()
        year, month, day = int(y), int(mo), int(d)
        h, mn, s = int(hh or 0), int(mi or 0), int(ss or 0)
        if not (h < 24 and mn < 60 and s < 60):
            return None
        try:  # the whole calendar date must exist (no 2026.02.31, no 2025.02.29)
            datetime.date(year, month, day)
        except ValueError:
            return None
        # The commit hash does not order; equal dates with no build time compare equal.
        return ParsedVersion((year, month, day, h, mn, s), f"{year}.{month}.{day}")
    if scheme != "semver":
        return None
    m = _SEMVER.fullmatch(token)
    if not m or m.group(5) is not None:
        return None
    key = tuple(int(x) for x in m.groups()[:3])
    return ParsedVersion(key, ".".join(map(str, key)), m.group(4))


def _floor_key(spec: EngineSpec, floor: str) -> tuple[int, ...]:
    parsed = parse_version(spec.scheme, floor)
    if parsed is None or parsed.prerelease is not None:  # table bug: fail closed
        raise LaunchRefused("floor_unset", spec.engine, f"floor {floor!r} does not parse")
    return parsed.key


def with_note(spec: EngineSpec, detail: str) -> str:
    return f"{detail}. {spec.note}" if spec.note else detail


def check_identity(spec: EngineSpec) -> None:
    """Refuse an engine with no identity rule and no pin: its name and a version line alone prove nothing."""
    if spec.ident is None and spec.pin is None:
        raise LaunchRefused(
            "identity_unverified", spec.engine,
            "no identity rule or pinned digest for this engine; a name plus a version line is not enough",
        )


def check_self_named(spec: EngineSpec, raw: str) -> None:
    """For an engine whose real version line names it, refuse a line that does not."""
    if spec.ident_required and not (spec.ident and re.search(spec.ident, raw, re.IGNORECASE)):
        raise LaunchRefused("identity_mismatch", spec.engine, f"version line does not name the engine: {raw[:120]!r}")


def check_floors(spec: EngineSpec, found: ParsedVersion) -> None:
    """Security floor (when set), then the functional floor when one is measured.

    An unset functional floor is not a refusal: the caller tags the grant beta
    for route.list (O50). ``floor_unset`` is reserved for a table floor that
    does not parse.
    """
    if found.prerelease is not None:
        raise LaunchRefused("version_refused", spec.engine, f"prerelease {found.prerelease!r}; floors name releases only")
    if spec.security is not None and found.key < _floor_key(spec, spec.security):
        raise LaunchRefused("version_refused", spec.engine, f"found {found.text} < security floor {spec.security}")
    if spec.functional is None:
        return
    if found.key < _floor_key(spec, spec.functional):
        raise LaunchRefused("version_refused", spec.engine, f"found {found.text} < functional floor {spec.functional}")


def other_engine_named(spec: EngineSpec, raw: str) -> str | None:
    """Return another engine's id if the line names it and does not name this engine."""
    if spec.ident and re.search(spec.ident, raw, re.IGNORECASE):
        return None
    for other in FLOORS.values():
        if other.engine != spec.engine and other.ident and re.search(other.ident, raw, re.IGNORECASE):
            return other.engine
    return None


def table_record() -> dict[str, object]:
    """The floor table as plain data, for the route record and the README."""
    return {
        "floor_table": FLOOR_TABLE_VERSION,
        **pins.table_record(),
        "engines": {
            s.engine: {
                "names": list(s.names),
                "scheme": s.scheme,
                "functional": s.functional,
                "security": s.security,
                "functional_basis": s.functional_basis,
                "security_basis": s.security_basis,
                "probe_name": s.probe_name,
                "identity": "pin" if s.pin else "required" if s.ident_required else ("rule" if s.ident else "unverified"),
                "pin": s.pin,
                "api_key_only": s.api_key_only,
            }
            for s in _SPECS
        },
    }
