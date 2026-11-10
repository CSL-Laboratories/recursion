"""R2a grants and launch plans (CSL-732, candidate).

``grant(engine, path)`` is the only issuer of a ``Grant``. It pins one engine's exact
native binary (path, realpath, full SHA-256, size), reads the version from that binary
with the zero-egress probe, checks the floor table and returns an immutable grant whose
fields are MACed with a per-process key. Every failure raises ``LaunchRefused``.

A pinned route (``EngineSpec.pin``: pi, hermes, cursor, antigravity) is not probed. Its
launcher and its package tree or binary must match a pin in ``pins.py``; the version is
the pin's, and anything else is ``pin_mismatch`` until re-pinned. On Windows only, a
pinned route's ``<name>.cmd`` or ``<name>.ps1`` launcher takes the same pin check; every
other wrapper extension, engine and platform is still ``wrapper_refused``. That launcher is
a check only and is never executed (Lucas's owner call of 2026-11-10: "accept a pinned
.cmd/.ps1 launcher as a check, but execute the pinned entry directly"): its grant records
the pin's entry, resolved inside the pinned tree, and a launch executes those files. With
no entry in the pin the grant is still issued and every launch is ``pin_entry_unset``.

Lucas's answer of 2026-11-10: "Extend the check-only rule to a bare or .exe granted path
that resolves to a script (.cmd/.ps1, by content or resolution as appropriate). It fails
closed, with tests." Check-only is decided once, at grant time, and recorded in the MACed
``Grant.check_only``. A pinned route's grant is check-only when the granted path is a
Windows ``<name>.cmd``/``<name>.ps1``; or it is bare or ``.exe`` and its realpath ends in
``.cmd``/``.ps1`` (by resolution, on any platform); or, on Windows only, it is bare or
``.exe`` and the resolved file is not a native executable (by content). Elsewhere a bare
shebang launcher (the Linux ``launcher+tree`` shape) is still executed by its realpath.

His answer of the same day: "A Windows Pi launch whose interpreter (Node from PATH) is
outside the pinned tree must refuse with a new typed code `interpreter_unpinned`. Hashing
external interpreters is post-Nov-11; don't implement it." A grant copies the matched pin's
``interpreter``; when it names one, the grant is still issued and every launch is refused
``interpreter_unpinned``, whatever the launcher kind, before ``pin_entry_unset``.

``launch_plan(grant, argv, env)`` authenticates the grant, checks argv and the spawn
size, re-verifies the binary, and returns a ``LaunchPlan``. Every use of a plan
(``plan.exec_argv()``) authenticates and re-verifies again: a full re-hash, never a
stat key, so a same-size rewrite with the mtime restored is still ``binary_changed``.
``exec_argv()`` returns the granted realpath followed by ``argv[1:]``; for a check-only
grant it returns the entry realpaths instead, never the launcher. No shell or PowerShell is
named anywhere.

Environment (Lucas's answer of 2026-11-10): "A launch uses the Recursion route's child_env,
never the environment the launcher would have set; nothing ambient is inherited, which is
Lucas's route-owned design on #320." ``env`` is that child_env, passed in whole.
``exec_env()`` returns exactly it: nothing is read from ``os.environ`` or from the launcher,
and no ``env`` means an empty environment.

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

from recursion_core.launch_check import binary, floors, pins
from recursion_core.launch_check.binary import (
    HEX64, PINNED_LAUNCHER_EXT, WRAPPER_EXT, file_kind, is_wrapper_name, sha256_file, stem,
)
from recursion_core.launch_check.floors import (
    EngineSpec, check_floors, check_identity, check_self_named, other_engine_named, parse_version, spec_for,
    with_note,
)
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
    realpath: str  # what is executed, unless `check_only` (then the launcher is only checked and `entry` is executed)
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
    pin_kind: str = ""  # pinned routes only: the pin kind, table version and matched target
    pin_table: str = ""
    pin_target: str = ""  # realpath of the package root or binary
    pin_sha256: str = ""
    # A check-only grant only: realpaths of the pin's entry, inside pin_target.
    # They are what is executed; () means the pin records no entry and every launch is refused.
    entry: tuple[str, ...] = ()
    # Pinned routes only, decided at grant time: the launcher is a script (.cmd/.ps1 by name, by
    # resolution or, on Windows, by content) that is checked and never executed.
    check_only: bool = False
    # The matched pin's interpreter outside the pinned tree ("" for none): every launch is refused.
    interpreter: str = ""
    mac: str = ""

    def record(self) -> dict[str, object]:
        out = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "mac"}
        out["outcome"] = "granted"
        # O50: no measured functional floor => beta on route.list.
        out["beta"] = self.floor_functional is None
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
        raise LaunchRefused("wrapper_refused", spec.engine, with_note(spec, f"{what} is not a native executable ({kind})"))
    if not os.access(real, os.X_OK):
        raise LaunchRefused("binary_missing", spec.engine, f"{what} is not executable")
    return real


def _hash(spec: EngineSpec, real: str) -> tuple[str, int]:
    try:
        return sha256_file(real)
    except OSError as e:
        raise LaunchRefused("binary_missing", spec.engine, f"hash failed: {e.__class__.__name__}") from None


def _pinned_windows_launcher(spec: EngineSpec, path: str) -> bool:
    """On Windows, a pinned route's ``<name>.cmd`` or ``<name>.ps1``: decided by the pin table, not refused as a wrapper."""
    if spec.pin is None or not binary.is_windows():
        return False
    name, ext = os.path.splitext(os.path.basename(path))
    return name in spec.names and ext.lower() in PINNED_LAUNCHER_EXT


def _check_only_path(spec: EngineSpec, path: str, real: str) -> bool:
    """Decide at grant time whether a pinned route's launcher is a script that is checked and never executed.

    True for a Windows ``<name>.cmd``/``<name>.ps1``; for a bare or ``.exe`` path whose realpath ends in
    ``.cmd``/``.ps1`` (any platform); and, on Windows only, for a bare or ``.exe`` path whose resolved
    file is not native. A file that cannot be read for that content check is ``binary_missing``.
    """
    if _pinned_windows_launcher(spec, path):
        return True
    if os.path.splitext(path)[1].lower() not in ("", ".exe"):
        return False
    if os.path.splitext(real)[1].lower() in PINNED_LAUNCHER_EXT:
        return True
    if not binary.is_windows():
        return False
    try:
        return file_kind(real) != "native"
    except OSError as e:
        raise LaunchRefused("binary_missing", spec.engine, f"grant path unreadable: {e.__class__.__name__}") from None


def _check_only_launcher(g: Grant) -> bool:
    """A grant whose launcher was only checked (``Grant.check_only``, MACed): the entry is executed, never the launcher."""
    return g.check_only is True


def grant(engine: str, path: str, *, timeout: float = PROBE_TIMEOUT_S, probe: Probe | None = None) -> Grant:
    """Pin one engine. Refuses with a typed code on every failure; never returns after one."""
    spec = spec_for(engine)
    if not isinstance(path, str) or not os.path.isabs(path):
        raise LaunchRefused("path_not_absolute", spec.engine, repr(path))
    if stem(path) not in spec.names:
        if _pinned_windows_launcher(spec, path):  # granted only bound to a pin; else pin_mismatch
            return _grant_pinned(spec, path)
        if is_wrapper_name(path):
            raise LaunchRefused("wrapper_refused", spec.engine, f"{os.path.basename(path)} is a wrapper shape")
        raise LaunchRefused("name_mismatch", spec.engine, f"{stem(path)!r} not in {list(spec.names)}")
    if spec.pin is not None:
        return _grant_pinned(spec, path)
    real = _realpath_native(spec, path, "grant path")
    if spec.probe_name is None:
        probe_path, probe_real = path, real
    else:
        probe_path = os.path.join(os.path.dirname(path), spec.probe_name)
        probe_real = _realpath_native(spec, probe_path, spec.probe_name)
    check_identity(spec)  # before the binary is run
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
    check_self_named(spec, result.raw)
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
        pin_kind="", pin_table="", pin_target="", pin_sha256="", entry=(), check_only=False, interpreter="",
    )
    return Grant(mac=_mac(values), **values)


def _grant_pinned(spec: EngineSpec, path: str) -> Grant:
    """A pinned route: the launcher and its tree or binary must match a pin. Nothing is run."""
    if not os.path.lexists(path) or not os.path.isfile(path):
        raise LaunchRefused("binary_missing", spec.engine, f"grant path absent: {path}")
    real = os.path.realpath(path)
    if not os.access(real, os.X_OK):
        raise LaunchRefused("binary_missing", spec.engine, "grant path is not executable")
    digest, size = _hash(spec, real)
    found = pins.match(spec.engine, spec.pin, real, digest, spec.note)
    parsed = parse_version(spec.scheme, found.pin.version)
    if parsed is None:  # table bug: fail closed
        raise LaunchRefused("pin_mismatch", spec.engine, f"pin version {found.pin.version!r} does not parse")
    check_floors(spec, parsed)
    entry: tuple[str, ...] = ()
    check_only = _check_only_path(spec, path, real)
    if check_only:  # the launcher is a check; the entry is what a launch executes
        try:
            entry = pins.resolve_entry(found.target, found.pin.entry)
        except (OSError, ValueError) as e:
            raise LaunchRefused("pin_mismatch", spec.engine, f"{e}; refused until re-pinned") from None
    values: dict[str, object] = dict(
        engine=spec.engine, path=path, realpath=real, sha256=digest, size=size,
        version_raw=found.pin.version, version=parsed.text, exit_code=0,
        floor_functional=spec.functional, floor_security=spec.security,
        floor_table=floors.FLOOR_TABLE_VERSION, refusal_codes=REFUSAL_CODES_VERSION,
        probe_path=path, probe_realpath=real, probe_sha256=digest,
        pin_kind=spec.pin, pin_table=pins.PIN_TABLE_VERSION, pin_target=found.target, pin_sha256=found.target_sha256,
        entry=entry, check_only=check_only, interpreter=found.pin.interpreter,
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
    check_identity(spec)
    if not HEX64.fullmatch(g.sha256) or not HEX64.fullmatch(g.probe_sha256):
        raise LaunchRefused("grant_forged", engine, "digest is not a full 64-hex SHA-256")
    parsed = parse_version(spec.scheme, g.version_raw)
    if parsed is None or parsed.text != g.version:
        raise LaunchRefused("grant_forged", engine, "version fields do not re-parse")
    check_self_named(spec, g.version_raw)  # re-checked at the trusted boundary
    check_floors(spec, parsed)  # re-checked at the trusted boundary
    if (spec.pin or "") != g.pin_kind:
        raise LaunchRefused("grant_forged", engine, "pin kind does not match the floor table")
    if type(g.entry) is not tuple or not all(isinstance(e, str) for e in g.entry):
        raise LaunchRefused("grant_forged", engine, "entry is not a tuple of paths")
    if type(g.check_only) is not bool or type(g.interpreter) is not str:
        raise LaunchRefused("grant_forged", engine, "check_only is not a bool or interpreter is not text")
    if not g.pin_kind and (g.check_only or g.entry or g.interpreter):
        raise LaunchRefused("grant_forged", engine, "check_only, entry or interpreter on a grant with no pin")
    if g.entry and not g.check_only:
        raise LaunchRefused("grant_forged", engine, "entry on a grant that is not check-only")
    if spec.pin is not None:
        if g.pin_table != pins.PIN_TABLE_VERSION:
            raise LaunchRefused("grant_stale", engine, f"issued under {g.pin_table}; pin table is now {pins.PIN_TABLE_VERSION}")
        if not pins.listed(engine, spec.pin, g.version_raw, g.sha256, g.pin_sha256):
            raise LaunchRefused("pin_mismatch", engine, with_note(
                spec,
                "the grant's pin is no longer recorded, or is archive-derived (pins are measured from a real "
                "install only) or has a bad entry; refused until re-pinned",
            ))
    return spec


def verify(g: Grant) -> None:
    """Re-verify the pinned binary (and probe binary, package tree, entry): resolution, then a full re-hash."""
    spec = spec_for(g.engine)
    pairs = [(g.path, g.realpath, g.sha256)]
    if g.probe_path != g.path:
        pairs.append((g.probe_path, g.probe_realpath, g.probe_sha256))
    for path, real, digest in pairs:
        if not os.path.lexists(path) or not os.path.isfile(path):
            raise LaunchRefused("binary_missing", spec.engine, path)
        if os.path.realpath(path) != real:
            raise LaunchRefused("binary_changed", spec.engine, f"{path} now resolves elsewhere")
        if not os.access(real, os.X_OK):  # exec bit removed after the grant: refuse here, not at spawn
            raise LaunchRefused("binary_missing", spec.engine, f"{real} is not executable")
        if _hash(spec, real)[0] != digest:
            raise LaunchRefused("binary_changed", spec.engine, f"{real} digest differs from the grant")
    if g.pin_kind and g.pin_target != g.realpath:  # the pinned tree or binary behind the launcher
        if os.path.realpath(g.pin_target) != g.pin_target:
            raise LaunchRefused("binary_changed", spec.engine, f"{g.pin_target} now resolves elsewhere")
        try:
            digest = pins.target_sha256(g.pin_kind, g.pin_target)
        except (OSError, ValueError) as e:
            raise LaunchRefused("binary_changed", spec.engine, f"{g.pin_target} unreadable: {e.__class__.__name__}") from None
        if digest != g.pin_sha256:
            raise LaunchRefused("binary_changed", spec.engine, f"{g.pin_target} digest differs from the grant")
    for e in g.entry:  # the tree digest above covers their bytes; each must still be the same file inside it
        if os.path.realpath(e) != e or not pins.inside(g.pin_target, e) or not os.path.isfile(e):
            raise LaunchRefused("binary_changed", spec.engine, f"entry {e} now resolves elsewhere")


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
        """Re-run every ``launch_plan`` check on every use; return ``[realpath, *argv[1:]]``.

        A ``LaunchPlan`` built by hand (or with ``dataclasses.replace``) gets no pass: its argv and env
        meet the same argv[0], wrapper and size rules as ``launch_plan`` before anything is returned.

        For a check-only grant (a pinned ``.cmd``/``.ps1`` launcher, or a bare or ``.exe`` path that
        resolves to a script) the result is ``[*entry, *argv[1:]]``: the launcher was a check and is
        never in it (``pin_entry_unset`` when the grant has no entry). A grant whose pin names an
        interpreter outside the pinned tree is ``interpreter_unpinned``.
        """
        self._recheck()
        if _check_only_launcher(self.grant):
            return [*self.grant.entry, *self.argv[1:]]
        return [self.grant.realpath, *self.argv[1:]]

    def exec_env(self) -> dict[str, str]:
        """Exactly the child_env given to ``launch_plan``: nothing from ``os.environ`` or the launcher."""
        self._recheck()
        return dict(self.env)

    def _recheck(self) -> None:
        env = self.env
        if not isinstance(env, tuple) or not all(isinstance(kv, tuple) and len(kv) == 2 for kv in env):
            spec = authenticate(self.grant)
            raise LaunchRefused("spawn_too_large", spec.engine, "env is not a tuple of name/value pairs")
        if len(dict(env)) != len(env):
            spec = authenticate(self.grant)
            raise LaunchRefused("spawn_too_large", spec.engine, "env repeats a name")
        size = _checked_launch(self.grant, self.argv, dict(env))
        if size != self.spawn_bytes:
            raise LaunchRefused("spawn_too_large", self.grant.engine, "spawn_bytes does not match argv and env")


def launch_plan(g: object, argv: Sequence[str], env: Mapping[str, str] | None = None) -> LaunchPlan:
    """Check a launch against a grant. ``env`` is the already-built child env (child_env), not os.environ.

    Only ``env`` reaches the plan; with none, the plan's environment is empty. Nothing ambient is inherited.
    """
    argv = tuple(argv) if argv is not None else ()
    env = dict(env or {})
    size = _checked_launch(g, argv, env)
    return LaunchPlan(g, argv, tuple(sorted(env.items())), size)


def _checked_launch(g: object, argv: Sequence[str], env: dict[str, str]) -> int:
    """Every launch check; returns the spawn size. Used by ``launch_plan`` and again on every plan use."""
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
    size = _spawn_bytes(spec, argv, env)
    if g.interpreter:  # the more specific cause, whatever the launcher kind
        raise LaunchRefused(
            "interpreter_unpinned", spec.engine,
            f"the pinned launcher starts {g.interpreter!r}, an interpreter outside the pinned tree that is not "
            "hashed; refused until external interpreters can be pinned (post-Nov 11)",
        )
    if _check_only_launcher(g) and not g.entry:
        raise LaunchRefused(
            "pin_entry_unset", spec.engine,
            "pinned script launcher (.cmd/.ps1, or a bare or .exe name that resolves to one) accepted as a check, "
            "but the pin records no entry to execute; refused until the entry is measured",
        )
    verify(g)
    return size
