"""Pinned wrapper routes for R2a (CSL-732, candidate).

Lucas's owner call of 2026-11-09, aligned with the route layer on recursion #321
(``subscription_routes/wrapper_pin.py`` at 38f41d3b; nothing is imported from it): Pi, Hermes and Cursor run as beta when the launcher
script and the installed package files match a pin; Antigravity CLI (``agy``) runs as beta
when its binary and its launcher match a pin. Any mismatch is refused (``pin_mismatch``)
until someone re-pins. A pin is measured from a real install and committed on its own;
no hash here is invented, and an engine with no pin recorded is refused.

Two pin kinds (``EngineSpec.pin``):

* ``launcher+tree``: the SHA-256 of the launcher (the file the engine name resolves to)
  and ``tree_sha256`` of the installed package root.
* ``binary+launcher``: the SHA-256 of the launcher and of the native binary it starts.
  When the engine name resolves straight to the binary, the two are one file and
  ``target`` is empty.

``Pin.target`` is a relative path from the launcher's directory to the package root or
the binary: empty (the launcher itself), ``.`` or ``/``-separated names and ``..``.
Nothing is executed here: the version comes from the pin, never from running a wrapper.

Limits: the interpreter a launcher names (``node``, ``python``) is not pinned unless it
sits inside the pinned tree; a symlink inside the tree is pinned by its text, not by the
file it points to; a tree that differs per machine (dependency resolution, absolute
shebang paths, ``__pycache__``) needs its own pin.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass

from recursion_core.launch_check.binary import file_kind, sha256_file
from recursion_core.launch_check.refusals import LaunchRefused

# A pin added, removed or changed is a new PIN_TABLE_VERSION; grants under another are grant_stale.
PIN_TABLE_VERSION = "r2a-pins/1.0.0-candidate-2026-11-09"

KINDS = ("launcher+tree", "binary+launcher")
_WHAT = {"launcher+tree": "installed package files", "binary+launcher": "binary"}
TREE_FORMAT = b"r2a-tree/1\0"
MAX_TREE_ENTRIES = 100_000
MAX_TREE_BYTES = 4 * 1024**3


@dataclass(frozen=True)
class Pin:
    engine: str
    version: str  # the version as measured; parsed with the engine's scheme
    launcher_sha256: str
    launcher_bytes: int  # size of the launcher file, as measured (recorded; the digest decides)
    target: str  # relative to the launcher's directory; "" is the launcher itself
    target_sha256: str  # tree_sha256 (launcher+tree) or the binary's SHA-256 (binary+launcher)
    platform: str  # where it was measured, e.g. linux_amd64
    measured: str  # YYYY-MM-DD
    source: str  # where the measured files came from


@dataclass(frozen=True)
class Match:
    pin: Pin
    target: str  # absolute realpath of the package root or binary
    target_sha256: str


# Measured pins only. Empty means every pinned route is refused (pin_mismatch).
PINS: tuple[Pin, ...] = ()


def tree_sha256(root: str) -> str:
    """Digest of every entry under ``root``; links are never followed.

    Entries are walked in sorted order, a directory's own entries before its
    subdirectories. Each adds a NUL-terminated record: ``F <path>``, ``x`` or ``-`` (owner
    exec bit) and the file's SHA-256; ``L <path>`` and the link text; ``D <path>``. Any
    other file type, or a tree over the caps, raises ``ValueError``.
    """
    h = hashlib.sha256(TREE_FORMAT)
    entries = total = 0
    pending = [""]
    while pending:
        rel = pending.pop()
        with os.scandir(os.path.join(root, rel) if rel else root) as it:
            found = sorted(it, key=lambda e: e.name)
        subdirs = []
        for e in found:
            name = f"{rel}/{e.name}" if rel else e.name
            entries += 1
            if entries > MAX_TREE_ENTRIES:
                raise ValueError(f"over {MAX_TREE_ENTRIES} entries")
            if e.is_symlink():
                fields = ["L " + name, os.readlink(e.path)]
            elif e.is_dir(follow_symlinks=False):
                fields = ["D " + name]
                subdirs.append(name)
            elif e.is_file(follow_symlinks=False):
                digest, size = sha256_file(e.path)
                total += size
                if total > MAX_TREE_BYTES:
                    raise ValueError(f"over {MAX_TREE_BYTES} bytes")
                fields = ["F " + name, "x" if e.stat(follow_symlinks=False).st_mode & 0o100 else "-", digest]
            else:
                raise ValueError(f"{name} is not a file, directory or link")
            for field in fields:
                h.update(field.encode("utf-8", "surrogateescape") + b"\0")
        pending.extend(reversed(subdirs))
    return h.hexdigest()


def resolve_target(launcher_real: str, target: str) -> str:
    """Absolute realpath of a pin's target. ``ValueError`` if ``target`` is not a plain relative path."""
    if target == "":
        return launcher_real
    parts = [] if target == "." else target.split("/")
    if any(p in ("", ".") or "\\" in p for p in parts):
        raise ValueError(f"pin target {target!r} is not a plain relative path")
    return os.path.realpath(os.path.join(os.path.dirname(launcher_real), *parts))


def target_sha256(kind: str, target_real: str) -> str:
    """Digest of a resolved target: the tree, or the native binary. ``OSError`` or ``ValueError`` on failure."""
    if kind == "launcher+tree":
        if not os.path.isdir(target_real):
            raise ValueError("package root is not a directory")
        return tree_sha256(target_real)
    if kind != "binary+launcher":
        raise ValueError(f"unknown pin kind {kind!r}")
    if not os.path.isfile(target_real) or file_kind(target_real) != "native":
        raise ValueError("binary is not a native executable")
    return sha256_file(target_real)[0]


def _for(engine: str) -> list[Pin]:
    return [p for p in PINS if p.engine == engine]


def launcher_listed(engine: str, launcher_sha256: str) -> bool:
    return any(p.launcher_sha256 == launcher_sha256 for p in _for(engine))


def listed(engine: str, version: str, launcher_sha256: str, digest: str) -> bool:
    """True if the table holds exactly this pin (re-checked on every grant use)."""
    return any(
        (p.version, p.launcher_sha256, p.target_sha256) == (version, launcher_sha256, digest) for p in _for(engine)
    )


def match(engine: str, kind: str, launcher_real: str, launcher_sha256: str, note: str = "") -> Match:
    """The recorded pin this install matches. Anything else is ``pin_mismatch``: refused until re-pinned."""

    def refuse(detail: str) -> LaunchRefused:
        return LaunchRefused("pin_mismatch", engine, f"{detail}. {note}" if note else detail)

    mine = _for(engine)
    if not mine:
        raise refuse(f"no pin is recorded for {engine} (launcher sha256 {launcher_sha256}); refused until someone pins it")
    near = [p for p in mine if p.launcher_sha256 == launcher_sha256]
    if not near:
        versions = ", ".join(sorted({p.version for p in mine}))
        raise refuse(
            f"launcher sha256 {launcher_sha256} matches no pin (pinned: {versions}); refused until re-pinned"
        )
    seen: dict[str, str] = {}
    why = ""
    for p in near:
        try:
            real = resolve_target(launcher_real, p.target)
            if real not in seen:
                # The launcher is the binary: already hashed, only its kind is left to check.
                same = real == launcher_real and kind == "binary+launcher" and file_kind(real) == "native"
                seen[real] = launcher_sha256 if same else target_sha256(kind, real)
        except OSError as e:
            why = f"{_WHAT[kind]} unreadable ({e.__class__.__name__})"
            continue
        except ValueError as e:
            why = str(e)
            continue
        if seen[real] == p.target_sha256:
            return Match(p, real, seen[real])
        why = f"{_WHAT[kind]} sha256 {seen[real]} differs from the pin ({p.target_sha256})"
    raise refuse(f"launcher matches pin {near[-1].version} but {why}; refused until re-pinned")


def measure(
    engine: str, kind: str, path: str, target: str, *, version: str, platform: str, measured: str, source: str
) -> Pin:
    """Measure an install on disk into a ``Pin`` (for re-pinning and tests). Nothing is executed.

    ``version`` is supplied by whoever measures (release manifest, package metadata or a
    version read by hand); ``source`` says where the files and the version came from.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown pin kind {kind!r}")
    real = os.path.realpath(path)
    digest, size = sha256_file(real)
    return Pin(
        engine=engine, version=version, launcher_sha256=digest, launcher_bytes=size, target=target,
        target_sha256=target_sha256(kind, resolve_target(real, target)),
        platform=platform, measured=measured, source=source,
    )


def table_record() -> dict[str, object]:
    """The pin table as plain data, for the route record and the README."""
    return {"pin_table": PIN_TABLE_VERSION, "pins": [asdict(p) for p in PINS]}
