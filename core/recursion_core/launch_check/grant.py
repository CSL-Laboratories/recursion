"""R2a grants and launch plans (CSL-732, candidate).

``grant(engine, path)`` is the only issuer of a ``Grant``. It pins one engine's exact
native binary (path, realpath, full SHA-256, size), reads the version from that binary
with the zero-egress probe, checks the floor table and returns an immutable grant whose
fields are MACed with a per-process key. Every failure raises ``LaunchRefused``.

``launch_plan(grant, argv, env)`` authenticates the grant, checks argv and the spawn
size, re-verifies the binary, and returns a ``LaunchPlan``. Every use of a plan
(``plan.exec_argv()``) authenticates and re-verifies again: a full re-hash, never a
stat key, so a same-size rewrite with the mtime restored is still ``binary_changed``.

Limit: same-process code can read the MAC key; the real boundary is the core's
process and IPC edge. The MAC stops forged or edited grant objects reaching a plan.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, fields

from recursion_core.launch_check import floors
from recursion_core.launch_check.binary import HEX64, WRAPPER_EXT, file_kind, is_wrapper_name, sha256_file, stem
from recursion_core.launch_check.floors import EngineSpec, check_floors, other_engine_named, parse_version, spec_for
from recursion_core.launch_check.probe import PROBE_TIMEOUT_S, Probe, run_version_probe
from recursion_core.launch_check.refusals import REFUSAL_CODES_VERSION, LaunchRefused

# Spawn caps. Linux refuses one string over 128 KiB (MAX_ARG_STRLEN); the total cap keeps
# argv plus env well under ARG_MAX on every supported platform.
MAX_ARG_BYTES = 128 * 1024 - 1
MAX_SPAWN_BYTES = 256 * 1024

_GRANT_KEY = secrets.token_bytes(32)


@dataclass(frozen=True)
class Grant:
    engine: str
    path: str  # the path granted (a PATH hit or a configured path)
    realpath: str  # what is executed
    sha256: str  # full 64-hex digest of realpath
    size: int
    version_raw: str  # first non-empty line of --version, as read
    version: str  # normalised parsed version
    exit_code: int
    floor_functional: str | None
    floor_security: str | None
    floor_table: str
    refusal_codes: str
    probe_path: str  # the binary --version ran on (differs for hermes: hermes-acp)
    probe_realpath: str
    probe_sha256: str
    mac: str = ""

    def record(self) -> dict[str, object]:
        out = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "mac"}
        out["outcome"] = "granted"
        return out


def _mac(values: Mapping[str, object]) -> str:
    names = [f.name for f in fields(Grant) if f.name != "mac"]
    msg = json.dumps([[n, values.get(n)] for n in names], separators=(",", ":"), ensure_ascii=True)
    return hmac.new(_GRANT_KEY, msg.encode("ascii"), hashlib.sha256).hexdigest()


def _realpath_native(spec: EngineSpec, path: str, what: str) -> str:
    if os.path.splitext(path)[1].lower() in WRAPPER_EXT:
        raise LaunchRefused("wrapper_refused", spec.engine, f"{what} has a wrapper extension")
    if not os.path.lexists(path) or not os.path.isfile(path):
        raise LaunchRefused("binary_missing", spec.engine, f"{what} absent: {path}")
    real = os.path.realpath(path)
    if is_wrapper_name(real):
        raise LaunchRefused("wrapper_refused", spec.engine, f"{what} resolves to a wrapper name: {os.path.basename(real)}")
    try:
        kind = file_kind(real)
    except OSError as e:
        raise LaunchRefused("binary_missing", spec.engine, f"{what} unreadable: {e.__class__.__name__}") from None
    if kind != "native":
        raise LaunchRefused("wrapper_refused", spec.engine, f"{what} is not a native executable ({kind})")
    if not os.access(real, os.X_OK):
        raise LaunchRefused("binary_missing", spec.engine, f"{what} is not executable")
    return real


def _hash(spec: EngineSpec, real: str) -> tuple[str, int]:
    try:
        return sha256_file(real)
    except OSError as e:
        raise LaunchRefused("binary_missing", spec.engine, f"hash failed: {e.__class__.__name__}") from None


def grant(engine: str, path: str, *, timeout: float = PROBE_TIMEOUT_S, probe: Probe | None = None) -> Grant:
    """Pin one engine. Refuses with a typed code on every failure; never returns after one."""
    spec = spec_for(engine)
    if not isinstance(path, str) or not os.path.isabs(path):
        raise LaunchRefused("path_not_absolute", spec.engine, repr(path))
    if stem(path) not in spec.names:
        if is_wrapper_name(path):
            raise LaunchRefused("wrapper_refused", spec.engine, f"{os.path.basename(path)} is a wrapper shape")
        raise LaunchRefused("name_mismatch", spec.engine, f"{stem(path)!r} not in {list(spec.names)}")
    real = _realpath_native(spec, path, "grant path")
    if spec.probe_name is None:
        probe_path, probe_real = path, real
    else:
        probe_path = os.path.join(os.path.dirname(path), spec.probe_name)
        probe_real = _realpath_native(spec, probe_path, spec.probe_name)
    digest, size = _hash(spec, real)
    probe_digest = digest if probe_real == real else _hash(spec, probe_real)[0]

    result = (probe or run_version_probe)(spec, probe_real, timeout)

    if _hash(spec, real) != (digest, size) or (probe_real != real and _hash(spec, probe_real)[0] != probe_digest):
        raise LaunchRefused("binary_changed", spec.engine, "digest changed during the version probe")
    if result.exit_code != 0:
        raise LaunchRefused("version_probe_failed", spec.engine, f"exit {result.exit_code}, raw {result.raw[:120]!r}")
    other = other_engine_named(spec, result.raw)
    if other is not None:
        raise LaunchRefused("engine_mismatch", spec.engine, f"version line names {other}: {result.raw[:120]!r}")
    parsed = parse_version(spec.scheme, result.raw)
    if parsed is None:
        raise LaunchRefused("version_unparsed", spec.engine, f"raw {result.raw[:120]!r}")
    check_floors(spec, parsed)

    values: dict[str, object] = dict(
        engine=spec.engine, path=path, realpath=real, sha256=digest, size=size,
        version_raw=result.raw, version=parsed.text, exit_code=result.exit_code,
        floor_functional=spec.functional, floor_security=spec.security,
        floor_table=floors.FLOOR_TABLE_VERSION, refusal_codes=REFUSAL_CODES_VERSION,
        probe_path=probe_path, probe_realpath=probe_real, probe_sha256=probe_digest,
    )
    return Grant(mac=_mac(values), **values)


def authenticate(g: object) -> EngineSpec:
    """Refuse anything ``grant()`` did not issue in this process, or issued under another table."""
    if type(g) is not Grant:
        raise LaunchRefused("grant_forged", "?", f"not a Grant: {type(g).__name__}")
    values = asdict(g)
    mac = values.pop("mac")
    engine = g.engine if isinstance(g.engine, str) else "?"
    if not isinstance(mac, str) or not hmac.compare_digest(mac, _mac(values)):
        raise LaunchRefused("grant_forged", engine, "MAC missing or invalid: not issued by grant()")
    if g.floor_table != floors.FLOOR_TABLE_VERSION or g.refusal_codes != REFUSAL_CODES_VERSION:
        raise LaunchRefused("grant_stale", engine, f"issued under {g.floor_table}; table is now {floors.FLOOR_TABLE_VERSION}")
    spec = spec_for(engine)
    if not HEX64.fullmatch(g.sha256) or not HEX64.fullmatch(g.probe_sha256):
        raise LaunchRefused("grant_forged", engine, "digest is not a full 64-hex SHA-256")
    parsed = parse_version(spec.scheme, g.version_raw)
    if parsed is None or parsed.text != g.version:
        raise LaunchRefused("grant_forged", engine, "version fields do not re-parse")
    check_floors(spec, parsed)  # re-checked at the trusted boundary
    return spec


def verify(g: Grant) -> None:
    """Re-verify the pinned binary (and probe binary): resolution, then a full re-hash."""
    spec = spec_for(g.engine)
    pairs = [(g.path, g.realpath, g.sha256)]
    if g.probe_path != g.path:
        pairs.append((g.probe_path, g.probe_realpath, g.probe_sha256))
    for path, real, digest in pairs:
        if not os.path.lexists(path) or not os.path.isfile(path):
            raise LaunchRefused("binary_missing", spec.engine, path)
        if os.path.realpath(path) != real:
            raise LaunchRefused("binary_changed", spec.engine, f"{path} now resolves elsewhere")
        if _hash(spec, real)[0] != digest:
            raise LaunchRefused("binary_changed", spec.engine, f"{real} digest differs from the grant")


def _spawn_bytes(spec: EngineSpec, argv: Sequence[str], env: Mapping[str, str]) -> int:
    total = 0
    for a in argv:
        if not isinstance(a, str) or "\x00" in a:
            raise LaunchRefused("spawn_too_large", spec.engine, "argv item is not NUL-free text")
        n = len(a.encode("utf-8", "surrogateescape")) + 1
        if n > MAX_ARG_BYTES:
            raise LaunchRefused("spawn_too_large", spec.engine, f"one argv item is {n} bytes > {MAX_ARG_BYTES}")
        total += n
    for k, v in env.items():
        if not isinstance(k, str) or not isinstance(v, str) or "\x00" in k + v or "=" in k or not k:
            raise LaunchRefused("spawn_too_large", spec.engine, "env item is not NUL-free text")
        n = len(f"{k}={v}".encode("utf-8", "surrogateescape")) + 1
        if n > MAX_ARG_BYTES:
            raise LaunchRefused("spawn_too_large", spec.engine, f"env item {k} is {n} bytes > {MAX_ARG_BYTES}")
        total += n
    if total > MAX_SPAWN_BYTES:
        raise LaunchRefused("spawn_too_large", spec.engine, f"argv+env is {total} bytes > {MAX_SPAWN_BYTES}")
    return total


@dataclass(frozen=True)
class LaunchPlan:
    grant: Grant
    argv: tuple[str, ...]  # as requested; argv[0] is the bare engine name
    env: tuple[tuple[str, str], ...]
    spawn_bytes: int

    def exec_argv(self) -> list[str]:
        """Authenticate and re-verify on every use; return ``[realpath, *argv[1:]]``."""
        authenticate(self.grant)
        verify(self.grant)
        return [self.grant.realpath, *self.argv[1:]]

    def exec_env(self) -> dict[str, str]:
        return dict(self.env)


def launch_plan(g: object, argv: Sequence[str], env: Mapping[str, str] | None = None) -> LaunchPlan:
    """Check a launch against a grant. ``env`` is the already-built child env (child_env), not os.environ."""
    spec = authenticate(g)  # before any field of g is trusted
    argv = tuple(argv) if argv is not None else ()
    if not argv:
        raise LaunchRefused("argv_empty", spec.engine, "")
    a0 = argv[0]
    if not isinstance(a0, str):
        raise LaunchRefused("argv0_mismatch", spec.engine, "argv[0] is not text")
    if is_wrapper_name(a0):
        raise LaunchRefused("wrapper_refused", spec.engine, f"argv[0] {a0!r} is a wrapper shape")
    if a0 not in spec.names:
        raise LaunchRefused("argv0_mismatch", spec.engine, f"argv[0] {a0!r} must be the bare engine name")
    env = dict(env or {})
    size = _spawn_bytes(spec, argv, env)
    verify(g)
    return LaunchPlan(g, argv, tuple(sorted(env.items())), size)
