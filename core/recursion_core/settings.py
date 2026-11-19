"""A person's own settings: one owner-only file, ``$XDG_CONFIG_HOME/recursion/settings.toml``.

A trade-off the core makes is a setting with a default, declared once in ``SETTINGS``; it is never a
second mechanism. The file is optional: with no file every default is in force. Its form::

    version = 1

    [plugin_host]
    gil_check = true      # a comment may say why

The rules this module keeps:

* ONE place. The file is read only at ``$XDG_CONFIG_HOME/recursion/settings.toml`` of the daemon's own
  environment (``$HOME/.config`` when the variable is unset, as for ``routes.json``; a relative value is
  ignored, so the current folder is never the place). ``load`` takes that environment and nothing else: never the current folder, a
  project, a run's workspace or environment, a plugin's folder, message or manifest, or a command-line value.
* OWNER-ONLY. The file is used only if it is a regular file, not a link, owned by the daemon's user and not
  writable by group or others (``owner_only_file``, the check ``load_routes`` makes on ``routes.json``).
* NEVER WRITTEN. Nothing here writes the file; the standard library reads TOML and cannot write it. A model or
  a plugin never writes it: a person keeps what a model proposes.
* A WRONG FILE STARTS NOTHING WRONG. A file that fails the owner check, is not TOML, has another ``version``
  or cannot be read leaves every default in force (note ``settings_unreadable``). A key ``SETTINGS`` does not
  declare is skipped and reported (``settings_unknown_key``): a newer file read by an older core keeps the
  keys this core knows. A value of the wrong type or outside its floor and ceiling keeps that setting's
  default, never a clamped value (``settings_value_out_of_range``). Each note names the key, the value found
  and what is allowed.
* SHOWN. ``python -m recursion_core.settings`` prints every setting, its value in force, its default and its
  source, and every note. It takes no argument and writes nothing. The daemon logs the same lines.

Read at daemon start in this build; a later reload may call ``load`` again. ``load`` keeps no state between
calls. ``SETTINGS`` is append-only: a row is added, never renamed or given another meaning.
"""

from __future__ import annotations

import math
import os
import stat
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

FILE_VERSION = 1
SETTINGS_FILE = ("recursion", "settings.toml")  # under $XDG_CONFIG_HOME, or ~/.config
MAX_BYTES = 65536                                # a settings file is small; a larger one is not read
SOURCE_DEFAULT = "default"
SOURCE_FILE = "settings file"
# The typed notes.
UNREADABLE = "settings_unreadable"
UNKNOWN_KEY = "settings_unknown_key"
OUT_OF_RANGE = "settings_value_out_of_range"
NOT_READ_HERE = "settings_not_read_on_this_system"


@dataclass(frozen=True)
class Setting:
    """One declared setting: its dotted name, type, default, floor and ceiling (numbers only), and what it does."""

    name: str
    type: type
    default: object
    says: str
    floor: int | float | None = None
    ceiling: int | float | None = None


# Append-only. The type is bool, int, float or str.
SETTINGS: tuple[Setting, ...] = (
    Setting("plugin_host.gil_check", bool, True,
            "Whether each plugin host makes its GIL check; on by default, and a person may turn it off."),
)
DECLARED = MappingProxyType({row.name: row for row in SETTINGS})


@dataclass(frozen=True)
class Value:
    name: str
    value: object
    default: object
    source: str  # SOURCE_DEFAULT or SOURCE_FILE


@dataclass(frozen=True)
class Note:
    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass(frozen=True)
class Loaded:
    """The settings in force: one ``Value`` per declared setting, the file looked at, and the notes."""

    path: str | None
    values: MappingProxyType
    notes: tuple[Note, ...]

    def __getitem__(self, name: str) -> object:
        return self.values[name].value


def settings_path(environ) -> Path | None:
    """``$XDG_CONFIG_HOME/recursion/settings.toml`` of ``environ``, or under ``$HOME/.config``. A relative
    ``XDG_CONFIG_HOME`` is ignored (the base directory specification); None when no absolute folder is known."""

    base = environ.get("XDG_CONFIG_HOME") or ""
    if not os.path.isabs(base):
        home = environ.get("HOME") or ""
        base = os.path.join(home, ".config") if os.path.isabs(home) else ""
    return Path(base, *SETTINGS_FILE) if base else None


def owner_only_file(info: os.stat_result) -> bool:
    """A regular file (an ``lstat`` of a link is not), owned by this user, writable by no group or other."""

    return stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o022


def _shown(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        text = repr(value)
    elif isinstance(value, (int, float)):
        text = repr(value)
    else:
        text = f"a {type(value).__name__}"
    return text if len(text) <= 80 else text[:77] + "..."


def _allowed(row: Setting) -> str:
    if row.type is bool:
        return "true or false"
    kind = {int: "a whole number", float: "a number", str: "a text"}[row.type]
    if row.floor is not None and row.ceiling is not None:
        return f"{kind} from {_shown(row.floor)} to {_shown(row.ceiling)}"
    return kind


def _fits(row: Setting, value: object) -> bool:
    if row.type is bool:
        return isinstance(value, bool)
    if row.type is str:
        return isinstance(value, str)
    if isinstance(value, bool) or not isinstance(value, (int, float) if row.type is float else int):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return not ((row.floor is not None and value < row.floor) or (row.ceiling is not None and value > row.ceiling))


def _flatten(table: dict, prefix: str = ""):
    for key, value in table.items():
        name = prefix + key
        if isinstance(value, dict) and name not in DECLARED and any(n.startswith(name + ".") for n in DECLARED):
            yield from _flatten(value, name + ".")
        else:
            yield name, value


def _read(path: Path) -> tuple[dict | None, str | None]:
    """The parsed file, or (None, None) when there is no file, or (None, why) when it cannot be used."""

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"cannot be looked at ({type(exc).__name__})"
    if not owner_only_file(info):
        return None, "must be a regular file (not a link) owned by this user and not group or other writable"
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        return None, f"cannot be opened ({type(exc).__name__})"
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or not owner_only_file(opened):
            return None, "changed while it was opened"
        with os.fdopen(fd, "rb", closefd=False) as fh:
            data = fh.read(MAX_BYTES + 1)
    except OSError as exc:
        return None, f"cannot be read ({type(exc).__name__})"
    finally:
        os.close(fd)
    if len(data) > MAX_BYTES:
        return None, f"is larger than {MAX_BYTES} bytes"
    try:
        doc = tomllib.loads(data.decode("utf-8"))
    except UnicodeDecodeError:
        return None, "is not UTF-8"
    except tomllib.TOMLDecodeError as exc:
        return None, f"is not TOML ({exc})"
    if type(doc.get("version")) is not int or doc["version"] != FILE_VERSION:  # true and 1.0 are not 1
        found = _shown(doc["version"]) if "version" in doc else "no version"
        return None, f"version must be {FILE_VERSION}, found {found}"
    return doc, None


def _defaults() -> dict:
    return {row.name: Value(row.name, row.default, row.default, SOURCE_DEFAULT) for row in SETTINGS}


def load(environ) -> Loaded:
    """The settings in force for a daemon whose environment is ``environ`` (in the daemon: ``os.environ``).

    Read at daemon start in this build; a later reload may call ``load`` again. Nothing is cached and
    nothing is written. Every declared setting gets a value; the notes say what was not used and why."""

    values = _defaults()
    if not hasattr(os, "geteuid"):  # Windows: the owner check has no uid, so no file is read
        note = Note(NOT_READ_HERE, "this system has no owner check for the settings file; every default is in force")
        return Loaded(None, MappingProxyType(values), (note,))
    path = settings_path(environ)
    if path is None:
        note = Note(UNREADABLE, "no absolute XDG_CONFIG_HOME or HOME; every default is in force")
        return Loaded(None, MappingProxyType(values), (note,))
    doc, why = _read(path)
    if why is not None:
        note = Note(UNREADABLE, f"{path} {why}; every default is in force")
        return Loaded(str(path), MappingProxyType(values), (note,))
    notes: list[Note] = []
    for name, found in _flatten({k: v for k, v in (doc or {}).items() if k != "version"}):
        row = DECLARED.get(name)
        if row is None:
            notes.append(Note(UNKNOWN_KEY, f"{_shown(name)} is not a setting this version knows; it is skipped"))
        elif not _fits(row, found):
            notes.append(Note(OUT_OF_RANGE, f"{name}: found {_shown(found)}; allowed: {_allowed(row)}; "
                                            f"the default {_shown(row.default)} stays in force"))
        else:
            values[name] = Value(name, found, row.default, SOURCE_FILE)
    return Loaded(str(path), MappingProxyType(values), tuple(notes))


def lines(loaded: Loaded) -> list[str]:
    """What status and the daemon's start log show: each setting, its value, default and source, then the notes."""

    out = [f"{v.name} = {_shown(v.value)} (default {_shown(v.default)}; source: {v.source})"
           for v in loaded.values.values()]
    return out + [f"note {note}" for note in loaded.notes]


def main(argv: list[str] | None = None, environ=None, out=None, err=None) -> int:
    """``python -m recursion_core.settings``: print the settings in force. It takes no argument and writes nothing."""

    argv = sys.argv[1:] if argv is None else argv
    out, err = out or sys.stdout, err or sys.stderr
    if argv:
        print("usage: python -m recursion_core.settings   (no argument; it reads the one settings file and "
              "writes nothing)", file=err)
        return 2
    loaded = load(os.environ if environ is None else environ)
    print(f"settings file: {loaded.path or 'none'}", file=out)
    for line in lines(loaded):
        print(line, file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
