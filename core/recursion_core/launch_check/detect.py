"""Static engine detection for R2a (CSL-732, candidate).

Given an explicit PATH string (never ``os.environ``), list each engine name's hits,
resolve symlinks and keep native targets only. Nothing found here is executed: a shim
is read, never run. The one shim followed is Codex's npm ``codex`` -> ``bin/codex.js``,
and only when the package layout is recognised and the vendored binary is native;
any other script, shim or non-native target is reported and left without a candidate,
so ``grant()`` is never offered a wrapper. The exception is a pinned route (pi, hermes,
cursor, antigravity): its first PATH hit is offered whatever its kind, because ``grant()``
decides it against the pin table and runs nothing. On Windows a pinned route's name is also
searched as ``.cmd`` and ``.ps1`` (after the bare name and ``.exe``); other engines are not.
A ``hermes`` hit in the unofficial npm ``hermes-agent`` bridge is named and never offered,
pinned route or not (B27, CSL-772).
"""

from __future__ import annotations

import json
import os
import platform
import stat
from dataclasses import dataclass

from recursion_core.launch_check import binary, pins
from recursion_core.launch_check.binary import PINNED_LAUNCHER_EXT, file_kind, sha256_file
from recursion_core.launch_check.floors import FLOORS, spec_for


@dataclass(frozen=True)
class Hit:
    path: str
    realpath: str
    kind: str  # native, script, other, unreadable


@dataclass(frozen=True)
class Detection:
    engine: str
    hits: tuple[Hit, ...]
    candidate: str | None  # path to pass to grant(); None means not grantable
    note: str


def _kind(path: str) -> str:
    try:
        return file_kind(path)
    except OSError:
        return "unreadable"


def _triples() -> tuple[str, ...]:
    machine = platform.machine().lower()
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(machine, machine)
    system = platform.system()
    if system == "Linux":
        return (f"{arch}-unknown-linux-musl", f"{arch}-unknown-linux-gnu")
    if system == "Darwin":
        return (f"{arch}-apple-darwin",)
    return ()


def codex_vendored_binary(js_realpath: str) -> str | None:
    """Follow an npm ``@openai/codex/bin/codex.js`` to its vendored native ``codex``.

    Recognised layouts (package root = parent of ``bin/``):
    ``<root>/vendor/<triple>/codex/codex`` and the optional-dependency packages
    ``<root>/node_modules/@openai/codex-<plat>/vendor/<triple>/codex/codex`` or
    ``<root>/../codex-<plat>/vendor/<triple>/codex/codex``. Exactly one native match, or None.
    """
    if os.path.basename(js_realpath) != "codex.js" or os.path.basename(os.path.dirname(js_realpath)) != "bin":
        return None
    root = os.path.dirname(os.path.dirname(js_realpath))
    if os.path.basename(root) != "codex" or os.path.basename(os.path.dirname(root)) != "@openai":
        return None
    bases = [root]
    for parent in (os.path.join(root, "node_modules", "@openai"), os.path.dirname(root)):
        try:
            bases += [os.path.join(parent, d) for d in sorted(os.listdir(parent)) if d.startswith("codex-")]
        except OSError:
            pass
    found = []
    for base in bases:
        for triple in _triples():
            cand = os.path.join(base, "vendor", triple, "codex", "codex")
            if os.path.isfile(cand) and not os.path.islink(cand) and _kind(cand) == "native":
                found.append(cand)
    return found[0] if len(found) == 1 else None


NPM_HERMES_PACKAGE = "hermes-agent"
_PACKAGE_JSON_MAX = 1 << 16


_MAX_HOPS = 40


def _resolution_paths(path: str) -> list[str]:
    """``path`` as written, then the whole path again after each symlink hop, ending at the realpath.

    Resolution goes component by component (like ``os.path.realpath``) and records every hop,
    so a symlink such as npm's ``lib/node_modules/hermes-agent`` -> ``src/hermes-agent`` (``npm link``,
    ``npm install -g <folder>``) is still seen after the final target leaves ``node_modules``.
    Nothing is opened or executed; a readlink error or a loop just ends the walk.
    """
    first = os.path.abspath(path)
    out = [first]
    drive, rest = os.path.splitdrive(first)
    resolved = drive + os.sep
    pending = [c for c in rest.split(os.sep) if c]
    hops = 0
    while pending:
        c = pending.pop(0)
        if c == ".":
            continue
        if c == "..":
            resolved = os.path.dirname(resolved)
            continue
        nxt = os.path.join(resolved, c)
        if not os.path.islink(nxt):
            resolved = nxt
            continue
        hops += 1
        try:
            target = os.readlink(nxt)
        except OSError:
            break
        if hops > _MAX_HOPS:
            break
        if os.path.isabs(target):
            drive, target = os.path.splitdrive(target)
            resolved = (drive or os.path.splitdrive(resolved)[0]) + os.sep
        pending = [t for t in target.split(os.sep) if t] + pending
        out.append(os.path.join(resolved, *pending) if pending else resolved)
    out.append(os.path.realpath(path))
    return list(dict.fromkeys(out))


def _package_json_names_bridge(directory: str) -> bool:
    try:
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)
        fd = os.open(os.path.join(directory, "package.json"), flags)
        with open(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                return False
            data = json.loads(f.read(_PACKAGE_JSON_MAX))
    except (OSError, ValueError, RecursionError):
        return False
    return isinstance(data, dict) and data.get("name") == NPM_HERMES_PACKAGE


def npm_hermes_agent_root(path: str) -> str | None:
    """Package root if ``path`` (or any symlink hop to it) is in the unofficial npm ``hermes-agent`` bridge.

    The bridge (npm ``hermes-agent`` 0.23.0, "Unofficial npm bridge for Hermes Agent") installs
    a ``hermes`` bin that is ``bin/hermes.js`` under node; it is not upstream Hermes (B27).
    It is recognised on ``path`` as written, on the path after every symlink hop, and on its
    realpath, by either a ``node_modules/hermes-agent/`` path segment or a ``package.json``
    named ``hermes-agent`` in any ancestor directory (so a symlinked package root, or a file
    outside ``bin/`` such as ``<root>/bin/<platform>/hermes`` or ``<root>/vendor/hermes``, is
    still the bridge). Nothing is executed; each ``package.json`` is size-capped, opened
    non-blocking and read only if it is a regular file, so a FIFO or device named
    ``package.json`` (e.g. ``/usr/package.json`` above ``/usr/bin/hermes``) cannot hang
    detection, and a read error means "not recognised".
    """
    views = [os.path.normpath(p) for p in _resolution_paths(path)]
    for view in views:
        parts = view.split(os.sep)
        for i in range(len(parts) - 2):
            if parts[i] == "node_modules" and parts[i + 1] == NPM_HERMES_PACKAGE:
                return os.sep.join(parts[: i + 2]) or os.sep
    seen: set[str] = set()
    for view in views:
        d = os.path.dirname(view)
        while d not in seen:
            seen.add(d)
            if _package_json_names_bridge(d):
                return d
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    return None


def _launcher_pinned(engine: str, realpath: str) -> bool:
    try:
        return pins.launcher_listed(engine, sha256_file(realpath)[0])
    except OSError:
        return False


def path_hits(name: str, path_string: str, pinned: bool = False) -> list[str]:
    """Executable files named ``name`` on the PATH string, in search order.

    Windows also tries ``.exe``, and for a pinned route (``pinned``) ``.cmd`` then ``.ps1``.
    """
    exts: tuple[str, ...] = ("",)
    if binary.is_windows():
        exts = ("", ".exe", *PINNED_LAUNCHER_EXT) if pinned else ("", ".exe")
    hits: list[str] = []
    for d in path_string.split(os.pathsep):
        if not d or not os.path.isabs(d):  # empty or relative PATH entries are never searched
            continue
        for ext in exts:
            p = os.path.join(d, name + ext)
            # A shell skips a file it cannot execute, so such a file never shadows a later hit.
            if os.path.isfile(p) and os.access(p, os.X_OK) and p not in hits:
                hits.append(p)
    return hits


def detect(engine: str, path_string: str) -> Detection:
    """Return the engine's PATH hits and a native candidate, or None (fail closed).

    Only the first PATH hit of each binary name counts (it is what that name runs);
    a later native copy shadowed by a wrapper is not offered. A pinned route offers
    its first hit of any kind; with several names, the first whose launcher is pinned.
    A ``hermes`` hit in the npm ``hermes-agent`` bridge is named in the note and never offered.
    """
    spec = spec_for(engine)
    hits: list[Hit] = []
    firsts: list[Hit] = []
    for name in spec.names:
        for i, p in enumerate(path_hits(name, path_string, spec.pin is not None)):
            real = os.path.realpath(p)
            hits.append(Hit(p, real, _kind(real)))
            if i == 0:
                firsts.append(hits[-1])
    notes = []
    if spec.engine == "hermes":  # B27: a hit in the npm bridge is named and never offered, pinned route or not
        kept: list[Hit] = []
        for h in firsts:
            bridge = npm_hermes_agent_root(h.path)
            if bridge is None:
                kept.append(h)
            else:
                notes.append(
                    f"{h.path}: npm {NPM_HERMES_PACKAGE} bridge at {bridge} (unofficial, not upstream Hermes); refused"
                )
        firsts = kept
    if spec.pin is not None and firsts:
        pick = firsts[0]
        if len(firsts) > 1:
            pick = next((h for h in firsts if _launcher_pinned(spec.engine, h.realpath)), pick)
        return Detection(spec.engine, tuple(hits), pick.path, f"pinned route ({spec.pin}): {pick.kind} on PATH")
    for h in firsts:
        if h.kind == "native":
            return Detection(spec.engine, tuple(hits), h.path, "native on PATH")
        if spec.engine == "codex" and h.kind == "script":
            target = codex_vendored_binary(h.realpath)
            if target is not None:
                return Detection(spec.engine, tuple(hits), target, f"npm shim {h.path} -> vendored native")
            notes.append(f"npm shim {h.path}: layout not recognised")
        else:
            notes.append(f"{h.path}: {h.kind}")
    note = "; ".join(notes) + "; fail closed" if hits else "absent from PATH"
    if hits and spec.note:
        note = f"{note}. {spec.note}"
    return Detection(spec.engine, tuple(hits), None, note)


def detect_all(path_string: str) -> dict[str, Detection]:
    return {engine: detect(engine, path_string) for engine in FLOORS}
