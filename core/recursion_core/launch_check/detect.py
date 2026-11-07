"""Static engine detection for R2a (CSL-732, candidate).

Given an explicit PATH string (never ``os.environ``), list each engine name's hits,
resolve symlinks and keep native targets only. Nothing found here is executed: a shim
is read, never run. The one shim followed is Codex's npm ``codex`` -> ``bin/codex.js``,
and only when the package layout is recognised and the vendored binary is native;
any other script, shim or non-native target is reported and left without a candidate,
so ``grant()`` is never offered a wrapper.
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass

from recursion_core.launch_check.binary import file_kind
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


def path_hits(name: str, path_string: str) -> list[str]:
    exts = ("", ".exe") if os.name == "nt" else ("",)
    hits: list[str] = []
    for d in path_string.split(os.pathsep):
        if not d or not os.path.isabs(d):  # empty or relative PATH entries are never searched
            continue
        for ext in exts:
            p = os.path.join(d, name + ext)
            if os.path.isfile(p) and p not in hits:
                hits.append(p)
    return hits


def detect(engine: str, path_string: str) -> Detection:
    """Return the engine's PATH hits and a native candidate, or None (fail closed).

    Only the first PATH hit of each binary name counts (it is what that name runs);
    a later native copy shadowed by a wrapper is not offered.
    """
    spec = spec_for(engine)
    hits: list[Hit] = []
    firsts: list[Hit] = []
    for name in spec.names:
        for i, p in enumerate(path_hits(name, path_string)):
            real = os.path.realpath(p)
            hits.append(Hit(p, real, _kind(real)))
            if i == 0:
                firsts.append(hits[-1])
    notes = []
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
    return Detection(spec.engine, tuple(hits), None, note)


def detect_all(path_string: str) -> dict[str, Detection]:
    return {engine: detect(engine, path_string) for engine in FLOORS}
