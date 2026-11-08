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
A floor change is a new ``FLOOR_TABLE_VERSION``, never an in-place edit; grants
issued under another table version are refused (``grant_stale``).
"""

from __future__ import annotations

import datetime

import re
from dataclasses import dataclass
from types import MappingProxyType

from recursion_core.launch_check.refusals import LaunchRefused

FLOOR_TABLE_VERSION = "r2a-floors/1.1.0-candidate-2026-11-08"


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
    probe_name: str | None = None  # sibling binary read for the version, if not the engine itself
    probe_env: tuple[tuple[str, str], ...] = ()  # extra fixed probe variables (no secrets)


_SPECS = (
    EngineSpec(
        "claude_code", ("claude",), "semver", "2.1.314", "2.1.260",
        "decided by Lucas (O50, grill Q43); not yet measured as the lowest passing version (CX-956)",
        "PRODUCT.md 11.6",
        ident=r"\bclaude\b",
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
        ident=r"\bcodex",
    ),
    EngineSpec(
        "opencode", ("opencode", "opencode2"), "semver", None, "1.18.33",
        "no conformance measurement", "D2-72 (CVE map 1.18.22, raised to 1.18.33)",
        ident=r"\bopencode",
    ),
    EngineSpec(
        "grok", ("grok",), "semver", None, None,
        "O50: Grok's once measured; not measured", "none derived (D2-72)",
        ident=r"\bgrok\b",
    ),
    EngineSpec(
        "antigravity", ("agy",), "semver", None, None,
        "never read or measured; version format assumed semver", "none recorded",
        ident=r"\bantigravity\b|\bagy\b",
    ),
    EngineSpec(
        "cursor", ("agent", "cursor-agent"), "date", None, "2025.09.17-25b418f",
        "no conformance measurement", "D2-72 (codex-verify348 record)",
        ident=r"\bcursor\b",
    ),
    EngineSpec(
        "gemini", ("gemini",), "semver", None, "0.39.1",
        "no conformance measurement", "D2-72 (CVE map)",
        ident=r"\bgemini\b",
    ),
    EngineSpec(
        "pi", ("pi",), "semver", None, None,
        "no record", "none recorded",
        ident=None,  # "pi" is too short to search for safely
    ),
    # Hermes: read with `hermes-acp --version`, which skips the network update check
    # (PRODUCT.md 11.6, CSL-712 engine CLIs record).
    EngineSpec(
        "hermes", ("hermes",), "semver", None, None,
        "no record", "none recorded",
        ident=r"\bhermes\b",
        probe_name="hermes-acp",
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
        "engines": {
            s.engine: {
                "names": list(s.names),
                "scheme": s.scheme,
                "functional": s.functional,
                "security": s.security,
                "functional_basis": s.functional_basis,
                "security_basis": s.security_basis,
                "probe_name": s.probe_name,
            }
            for s in _SPECS
        },
    }
