"""Pinned wrapper routes for R2a (CSL-732, candidate).

Lucas's owner call of 2026-11-09, aligned with the route layer on recursion #321
(``subscription_routes/wrapper_pin.py`` at 38f41d3b; nothing is imported from it): Pi, Hermes and Cursor run as beta when the launcher
script and the installed package files match a pin; Antigravity CLI (``agy``) runs as beta
when its binary and its launcher match a pin. Any mismatch is refused (``pin_mismatch``)
until someone re-pins. A pin is measured from a real install and committed on its own;
no hash here is invented, and an engine with no pin recorded is refused.

Lucas's owner call of 2026-11-10 (csl-research PR #1159, call 6): "pins stay measured from
a real install only for Nov 11; no archive-derived pins". A pin whose ``source`` names a
vendor archive (``archive_sourced``) is never matched: ``pin_mismatch``. An archive may be
recorded as a cross-check of a measured pin, never as its source.

Two pin kinds (``EngineSpec.pin``):

* ``launcher+tree``: the SHA-256 of the launcher (the file the engine name resolves to)
  and ``tree_sha256`` of the installed package root.
* ``binary+launcher``: the SHA-256 of the launcher and of the native binary it starts.
  When the engine name resolves straight to the binary, the two are one file and
  ``target`` is empty.

``Pin.target`` is a relative path from the launcher's directory to the package root or
the binary: empty (the launcher itself), ``.`` or ``/``-separated names and ``..``.
Nothing is executed here: the version comes from the pin, never from running a wrapper.

``Pin.entry`` (a field of ``launcher+tree`` pins only) is the program the launcher would reach, as argv
items: relative ``/``-separated paths inside the package root. Lucas's owner call of
2026-11-10 (calls 13/14): "accept a pinned .cmd/.ps1 launcher as a check, but execute the
pinned entry directly". A Windows ``.cmd``/``.ps1`` launcher is hashed and never executed;
what is executed is the entry (``grant.LaunchPlan.exec_argv``). Empty means not measured,
and such a launcher's grant refuses at launch (``pin_entry_unset``). A ``binary+launcher``
pin leaves ``entry`` empty because its measured binary is its entry: behind a script
launcher the launcher is checked and the pinned binary (``target``, matched to
``target_sha256``) is executed directly. Only a pin with no measured binary or entry behind
its launcher is ``pin_entry_unset``. Lucas's answer of
2026-11-10 extends the check-only rule "to a bare or .exe granted path that resolves to a
script (.cmd/.ps1, by content or resolution as appropriate)"; ``grant`` decides that.

``Pin.interpreter`` records, as measured fact, that the launcher starts an interpreter from
outside the pinned tree, named as the launcher resolves it (``node (PATH)``); empty means
none, or one inside the tree. Lucas's answer of 2026-11-10: "A Windows Pi launch whose
interpreter (Node from PATH) is outside the pinned tree must refuse with a new typed code
`interpreter_unpinned`. Hashing external interpreters is post-Nov-11; don't implement it."
The pin still matches and its grant is issued; every launch on it is refused
(``interpreter_unpinned``). No outside interpreter is hashed or looked up here.

Limits: a symlink inside the tree is pinned by its text, not by the
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
# 1.4.0: the Antigravity CLI 1.5.3 linux_amd64 pin is removed (it was hashed from the release
# archive, not from an installed agy); archive-sourced pins are never matched; Pin gains `entry`.
# 1.5.0: Pin gains `interpreter`; the two Pi 1.5.1 windows_amd64 rows record "node (PATH)" (their
# launchers run the Node found on PATH, outside the pinned tree). No hash, size, target or entry changed.
# 1.6.0: data only, measured on sentry 2026-11-10. Cursor 2026.11.05-86047df gains its entry
# (node.exe, index.js) and a .cmd launcher row; Antigravity CLI 1.5.5 windows_amd64 is pinned from
# an installed agy.exe; Pi 1.5.2 (.cmd, .ps1 and bare launchers, with its entry) replaces both 1.5.1 rows.
PIN_TABLE_VERSION = "r2a-pins/1.6.0-candidate-2026-11-10"

KINDS = ("launcher+tree", "binary+launcher")
_WHAT = {"launcher+tree": "installed package files", "binary+launcher": "binary"}
TREE_FORMAT = b"r2a-tree/1\0"
MAX_TREE_ENTRIES = 100_000
MAX_TREE_BYTES = 4 * 1024**3
# A pin source whose path ends in one of these names a vendor archive, not a real install.
ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2", ".tar.zst", ".tar", ".zip", ".7z")


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
    source: str  # where the measured files came from: a real install, never a vendor archive
    # launcher+tree only: the program the launcher reaches, as argv items relative to `target`
    # (an interpreter and a script inside the tree, say). () is not measured. A binary+launcher pin
    # leaves this (): its measured binary (`target`) is the entry a check-only launch executes.
    entry: tuple[str, ...] = ()
    # An interpreter the launcher starts from outside the pinned tree, named as the launcher
    # resolves it ("node (PATH)"). "" is none, or one inside the tree. It is not hashed, so a
    # launch on a pin that names one is refused (interpreter_unpinned).
    interpreter: str = ""


@dataclass(frozen=True)
class Match:
    pin: Pin
    target: str  # absolute realpath of the package root or binary
    target_sha256: str


# Measured pins only. An engine with no pin here is refused (pin_mismatch).
# Lucas, 2026-11-10 (csl-research PR #1159 comment 6777082374): "pins stay measured from a real
# install only for Nov 11; no archive-derived pins". The Antigravity CLI 1.5.3 linux_amd64 row,
# hashed from the release archive, was removed in 1.4.0; the 1.5.5 windows_amd64 row below is
# measured from an installed agy.exe.
# Entries: both Cursor rows and the three Pi rows carry a measured entry. The Antigravity row is
# binary+launcher, so its entry is (): its measured binary is its entry.
# Lucas, 2026-11-10: "A Windows Pi launch whose interpreter (Node from PATH) is outside the pinned
# tree must refuse with a new typed code `interpreter_unpinned`." Every Pi launcher runs the Node
# found on PATH, so the three Pi rows record it and Pi still refuses at launch. Cursor's node.exe
# is inside its tree, so its rows record none.
PINS: tuple[Pin, ...] = (
    # The windows_amd64 pins below were measured on Lucas's Windows machine "sentry" on 2026-11-10
    # with pins.measure() at commit 6cce8ff96eab600f14d3109d4547cf9b3eda95ab (Lucas, 2026-11-10,
    # on recording agy, Cursor and Pi from sentry: "Yes"). The .cmd/.ps1 rows are reachable on
    # Windows through the .cmd/.ps1 rule (Lucas's owner call of 2026-11-09): for a pinned route
    # detect.path_hits tries "", .exe, .cmd, .ps1, in that order, in each PATH directory, and
    # grant() accepts a <name>.cmd or <name>.ps1 launcher only bound to a pin (its SHA-256 is a
    # recorded launcher and the pinned tree matches); any other .cmd/.ps1 launcher is pin_mismatch.
    #
    # Cursor 2026.11.05-86047df, windows_amd64: launcher
    # C:\Users\Lucas\AppData\Local\cursor-agent\cursor-agent.ps1 (2527 bytes); agent.ps1 has the
    # same SHA-256. Tree of versions/2026.11.05-86047df: 408 entries, 265684223 bytes, unchanged
    # since 2026-11-09. It includes an empty .running directory, so this pin goes stale if Cursor
    # writes into it or removes it. Entry: node.exe (v24.22.1, 93915464 bytes) and index.js
    # (7970872 bytes), both inside the tree, so no interpreter outside it.
    Pin(
        engine="cursor",
        version="2026.11.05-86047df",
        launcher_sha256="b5975caa126e855f4be9b6084791a47eb40dd0babccb297265bf131df033acfc",
        launcher_bytes=2527,
        target="versions/2026.11.05-86047df",
        target_sha256="d9088fb279e9a004d4098b927df9b1257cd05b367cdc0959ba11d082e291bf23",
        platform="windows_amd64",
        measured="2026-11-10",
        source="https://cursor.com/install?win32=true",
        entry=("node.exe", "index.js"),
    ),
    # Cursor 2026.11.05-86047df, windows_amd64: launcher cursor-agent.cmd (517 bytes); agent.cmd
    # has the same SHA-256. Same tree and entry as the .ps1 pin. detect.path_hits tries .cmd
    # before .ps1, so this is the launcher a Windows PATH search offers when both are present.
    Pin(
        engine="cursor",
        version="2026.11.05-86047df",
        launcher_sha256="9244cdfcf1f42640f3ee17f40f9140171eb1ac3285077db066f51ccb4d6199a1",
        launcher_bytes=517,
        target="versions/2026.11.05-86047df",
        target_sha256="d9088fb279e9a004d4098b927df9b1257cd05b367cdc0959ba11d082e291bf23",
        platform="windows_amd64",
        measured="2026-11-10",
        source="https://cursor.com/install?win32=true",
        entry=("node.exe", "index.js"),
    ),
    # Antigravity CLI (agy) 1.5.5, windows_amd64: the native
    # C:\Users\Lucas\AppData\Local\agy\bin\agy.exe (226117784 bytes), installed by
    # https://antigravity.google/cli/install.ps1. The launcher is the binary itself, so target is
    # "" and both digests are the one file's. The source is the install. Cross-check only: the
    # file's SHA-512 matches Google's windows_amd64 release manifest for 1.5.5. agy updates itself
    # in the background, so this pin goes stale (pin_mismatch) after an update until re-pinned.
    Pin(
        engine="antigravity",
        version="1.5.5",
        launcher_sha256="fe9eb2f3884ca6cd630c57eed117580819341bce4b87d2c40b441ddd01cb8f2a",
        launcher_bytes=226117784,
        target="",
        target_sha256="fe9eb2f3884ca6cd630c57eed117580819341bce4b87d2c40b441ddd01cb8f2a",
        platform="windows_amd64",
        measured="2026-11-10",
        source="https://antigravity.google/cli/install.ps1 (installed agy.exe)",
    ),
    # Pi 1.5.2, windows_amd64 (replaces the 1.5.1 rows): launcher pi.cmd (464 bytes). Tree of
    # ../install/releases/1.5.2: 16776 entries. Chain: launcher -> bin/pi-launcher.js (outside the
    # tree), which reads install/current-version and spawns process.execPath on the entry (cli.js)
    # with PI_MANAGED_INSTALL_ROOT set. Executing the entry skips pi-launcher.js, and the route's
    # child_env decides PI_MANAGED_INSTALL_ROOT; it is never inherited. The Node is the one found
    # on PATH, outside the tree, so every launch is still refused (interpreter_unpinned).
    Pin(
        engine="pi",
        version="1.5.2",
        launcher_sha256="c56ef3cdd441b3c00d49e6e36d4a163bbcc715bf32e4de3473ece96efd232c4b",
        launcher_bytes=464,
        target="../install/releases/1.5.2",
        target_sha256="0e2feb8977d64e87901d4bb9d4d5d01156e1b217e35faa89ec090496380eb4fa",
        platform="windows_amd64",
        measured="2026-11-10",
        source="https://pi.dev/install.ps1",
        entry=("node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js",),
        interpreter="node (PATH)",
    ),
    # Pi 1.5.2, windows_amd64: launcher pi.ps1 (500 bytes); same tree, entry and chain as the
    # pi.cmd pin.
    Pin(
        engine="pi",
        version="1.5.2",
        launcher_sha256="3f422e96483162aad373b8e3e28f282236bdffb5cc1b8bfb6c25a8d918bd31d1",
        launcher_bytes=500,
        target="../install/releases/1.5.2",
        target_sha256="0e2feb8977d64e87901d4bb9d4d5d01156e1b217e35faa89ec090496380eb4fa",
        platform="windows_amd64",
        measured="2026-11-10",
        source="https://pi.dev/install.ps1",
        entry=("node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js",),
        interpreter="node (PATH)",
    ),
    # Pi 1.5.2, windows_amd64: the bare `pi` launcher (390 bytes); same tree, entry and chain as
    # the pi.cmd pin. For a pinned route on Windows detect.path_hits tries the bare name ("")
    # first, before .exe, .cmd and .ps1, so this is the file a PATH search offers when present.
    Pin(
        engine="pi",
        version="1.5.2",
        launcher_sha256="e05c2aba9e41123c919ca34d8478a18aeb2ec65fc298522aa9b1694e5487df98",
        launcher_bytes=390,
        target="../install/releases/1.5.2",
        target_sha256="0e2feb8977d64e87901d4bb9d4d5d01156e1b217e35faa89ec090496380eb4fa",
        platform="windows_amd64",
        measured="2026-11-10",
        source="https://pi.dev/install.ps1",
        entry=("node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js",),
        interpreter="node (PATH)",
    ),
)


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


def archive_sourced(source: object) -> bool:
    """True if a pin's ``source`` names a vendor archive: its path, less query and fragment, has an archive suffix."""
    if not isinstance(source, str):  # table bug: fail closed
        return True
    path = source.split("#", 1)[0].split("?", 1)[0]
    return path.strip().rstrip("/").lower().endswith(ARCHIVE_SUFFIXES)


def check_entry(kind: str, entry: object) -> None:
    """``ValueError`` unless ``entry`` is a tuple of plain relative paths that stay inside the package root."""
    if type(entry) is not tuple:
        raise ValueError("pin entry is not a tuple")
    if entry and kind != "launcher+tree":
        raise ValueError(f"pin entry on a {kind} pin: only a launcher+tree pin can carry one")
    for item in entry:
        depth = 0
        for p in item.split("/") if isinstance(item, str) else [""]:
            depth += -1 if p == ".." else 1
            if p in ("", ".") or "\\" in p or ":" in p or depth < 0:
                raise ValueError(f"pin entry {item!r} is not a plain relative path inside the package root")


def check_interpreter(interpreter: object) -> None:
    """``ValueError`` unless ``interpreter`` is text ("" for none outside the tree)."""
    if type(interpreter) is not str:
        raise ValueError("pin interpreter is not text")


def inside(root_real: str, real: str) -> bool:
    return real.startswith(root_real.rstrip(os.sep) + os.sep)


def resolve_entry(root_real: str, entry: tuple[str, ...]) -> tuple[str, ...]:
    """Absolute realpaths of a pin's entry items. ``ValueError`` unless each is a regular file inside the package root."""
    out = []
    for item in entry:
        real = os.path.realpath(os.path.join(root_real, *item.split("/")))
        if not inside(root_real, real) or not os.path.isfile(real):
            raise ValueError(f"pin entry {item!r} is not a regular file inside the pinned tree")
        out.append(real)
    return tuple(out)


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


def _unusable(p: Pin, kind: str) -> str:
    """Why a pin is never matched, or "": an archive-derived source, or a bad entry or interpreter (table bug)."""
    if archive_sourced(p.source):
        return (
            f"its source {p.source!r} is a vendor archive: archive-derived pins are never matched "
            "(pins are measured from a real install only)"
        )
    try:
        check_entry(kind, p.entry)
        check_interpreter(p.interpreter)
    except ValueError as e:
        return str(e)
    return ""


def listed(engine: str, kind: str, version: str, launcher_sha256: str, digest: str) -> bool:
    """True if the table holds exactly this pin and it can be matched (re-checked on every grant use)."""
    return any(
        (p.version, p.launcher_sha256, p.target_sha256) == (version, launcher_sha256, digest) and not _unusable(p, kind)
        for p in _for(engine)
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
        why = _unusable(p, kind)
        if why:
            continue
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
    engine: str, kind: str, path: str, target: str, *, version: str, platform: str, measured: str, source: str,
    entry: tuple[str, ...] = (), interpreter: str = "",
) -> Pin:
    """Measure an install on disk into a ``Pin`` (for re-pinning and tests). Nothing is executed.

    ``version`` is supplied by whoever measures (release manifest, package metadata or a
    version read by hand); ``source`` says where the files and the version came from: the
    real install, never a vendor archive. ``entry`` is what the launcher was read to start;
    ``interpreter`` names an interpreter it starts from outside the tree ("" for none).
    """
    if kind not in KINDS:
        raise ValueError(f"unknown pin kind {kind!r}")
    if archive_sourced(source):
        raise ValueError(f"pin source {source!r} is a vendor archive; measure a real install")
    check_entry(kind, entry)
    check_interpreter(interpreter)
    real = os.path.realpath(path)
    digest, size = sha256_file(real)
    root = resolve_target(real, target)
    resolve_entry(root, entry)
    return Pin(
        engine=engine, version=version, launcher_sha256=digest, launcher_bytes=size, target=target,
        target_sha256=target_sha256(kind, root),
        platform=platform, measured=measured, source=source, entry=entry, interpreter=interpreter,
    )


def table_record() -> dict[str, object]:
    """The pin table as plain data, for the route record and the README."""
    return {"pin_table": PIN_TABLE_VERSION, "pins": [asdict(p) for p in PINS]}
