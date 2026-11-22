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
  or cannot be read leaves every default in force (note ``settings_unreadable``, a warning that names the fault
  and the way out; the command then ends with ``EXIT_WARNED``). A key ``SETTINGS`` does not
  declare is skipped and reported (``settings_unknown_key``): a newer file read by an older core keeps the
  keys this core knows. That note is a warning, names a close known key or table when there is one, and makes
  the command end with ``EXIT_WARNED``; nothing is ever corrected. So is a quoted key that holds a dot
  (``"plugin_host.gil_check" = ...``): it is never a second spelling of a setting. A value of the wrong type
  or outside its floor and ceiling keeps that setting's default, never a clamped value
  (``settings_value_out_of_range``), and so does a text a row does not allow (``Setting.allowed``). Each note
  names the key, the value found and what is allowed.
* SHOWN. ``python -m recursion_core.settings`` prints the file path and the values a daemon started
  from this environment would take, with their defaults, sources and any notes. It takes no argument
  and writes nothing. The daemon that holds the one-daemon lock records the path and its values in its
  log at start, with the process id and a time on each line.

Read at daemon start in this build; a later reload may call ``load`` again. ``load`` keeps no state between
calls. ``SETTINGS`` is append-only: a row is added, never renamed or given another meaning.
"""

from __future__ import annotations

import difflib
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
WARNED = "warning: "  # what begins the line of a note that warns, in the start log and in the command
EXIT_WARNED = 1      # the command's end when a note warns: a key, a table or the whole file skipped (2: usage)


@dataclass(frozen=True)
class Setting:
    """One declared setting: its dotted name, type, default, floor and ceiling (numbers only), and what it does."""

    name: str
    type: type
    default: object
    says: str
    floor: int | float | None = None
    ceiling: int | float | None = None
    allowed: tuple[str, ...] = ()  # a text row's only texts (empty: any text); any other keeps the default


# Append-only. The type is bool, int, float or str. A row that loosens safety has the safe value as its default,
# so a file that cannot be read as a whole, which leaves every default in force, can never loosen anything.
SETTINGS: tuple[Setting, ...] = (
    Setting("plugin_host.gil_check", bool, True,
            "Whether each plugin host makes its GIL check; on by default, and a person may turn it off."),
    Setting("stores.sqlite_wal_bug", str, "refuse",
            "Whether the stores refuse to open on an SQLite with the WAL reset bug (refuse, the default) or open anyway at the person's own risk (accept_risk).",
            allowed=("refuse", "accept_risk")),
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
    warns: bool = False  # a key, a table or the whole file of the person's was skipped

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
        try:
            text = repr(value)
        except ValueError:  # a whole number past the interpreter's digit limit cannot be printed
            text = "a whole number too long to show"
    else:
        text = f"a {type(value).__name__}"
    return text if len(text) <= 80 else text[:77] + "..."


def _allowed(row: Setting) -> str:
    if row.type is bool:
        return "true or false"
    if row.allowed:
        return "one of " + " or ".join(_shown(text) for text in row.allowed)
    kind = {int: "a whole number", float: "a number", str: "a text"}[row.type]
    if row.floor is not None and row.ceiling is not None:
        return f"{kind} from {_shown(row.floor)} to {_shown(row.ceiling)}"
    return kind


def _fits(row: Setting, value: object) -> bool:
    if row.type is bool:
        return isinstance(value, bool)
    if row.type is str:
        return isinstance(value, str) and (not row.allowed or value in row.allowed)
    if isinstance(value, bool) or not isinstance(value, (int, float) if row.type is float else int):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return not ((row.floor is not None and value < row.floor) or (row.ceiling is not None and value > row.ceiling))


_QUOTED = object()  # a key that itself holds a dot (a quoted dotted key): never joined into a setting name


def _flatten(table: dict, prefix: str = ""):
    for key, value in table.items():
        name = prefix + key
        if "." in key:  # TOML sees another key than the dotted one, so one setting would have two spellings
            yield name, _QUOTED
        elif isinstance(value, dict) and name not in DECLARED and any(n.startswith(name + ".") for n in DECLARED):
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
        if not stat.S_ISREG(info.st_mode):
            fault = "it is a link or another kind of file; put a regular file there"
        elif info.st_uid != os.geteuid():
            fault = "it is owned by another user; put a file of your own there"
        else:
            fault = f"its mode is {stat.S_IMODE(info.st_mode):04o}; run chmod 600 on it"
        return None, f"must be a regular file (not a link) owned by this user and not group or other writable: {fault}"
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
    except UnicodeDecodeError as exc:
        return None, f"is not UTF-8 (at byte {exc.start}); save it as UTF-8"
    except tomllib.TOMLDecodeError as exc:  # its text ends with the line and column, as the reader reports them
        return None, f"is not TOML: {exc}; correct the file there"
    except (ValueError, RecursionError) as exc:  # the parser raises plain ones too, with no line or column
        return None, f"holds a value or a nesting the TOML reader cannot take ({exc}); shorten it"
    if "version" not in doc:
        return None, f"has no version line; add `version = {FILE_VERSION}` at its top"
    if type(doc["version"]) is not int or doc["version"] != FILE_VERSION:  # true and 1.0 are not 1
        return None, f"version must be {FILE_VERSION}, found {_shown(doc['version'])}; write `version = {FILE_VERSION}`"
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
        note = Note(UNREADABLE, f"{path} {why}; every default is in force", warns=True)
        return Loaded(str(path), MappingProxyType(values), (note,))
    notes: list[Note] = []
    for name, found in _flatten({k: v for k, v in (doc or {}).items() if k != "version"}):
        row = DECLARED.get(name)
        if found is _QUOTED:
            notes.append(Note(UNKNOWN_KEY, f"{_shown(name)} is a quoted key that holds a dot, not a setting; it is "
                                           "skipped; write it without the quotes", warns=True))
        elif row is None:
            known = sorted({*DECLARED, *(n.rsplit(".", 1)[0] for n in DECLARED)})  # its keys and its tables
            close = difflib.get_close_matches(name, known, n=1, cutoff=0.8)  # text only; never applied
            hint = f"; did you mean {close[0]}?" if close else ""
            notes.append(Note(UNKNOWN_KEY, f"{_shown(name)} is not a setting this version knows; it is skipped{hint}",
                              warns=True))
        elif not _fits(row, found):
            notes.append(Note(OUT_OF_RANGE, f"{name}: found {_shown(found)}; allowed: {_allowed(row)}; "
                                            f"the default {_shown(row.default)} stays in force"))
        else:
            values[name] = Value(name, found, row.default, SOURCE_FILE)
    return Loaded(str(path), MappingProxyType(values), tuple(notes))


def lines(loaded: Loaded) -> list[str]:
    """What status and the daemon's start log show: each setting, its value, default and source, then the notes."""

    # A future secret row must show set or not set here and keep raw values out of notes.
    out = [f"{v.name} = {_shown(v.value)} (default {_shown(v.default)}; source: {v.source})"
           for v in loaded.values.values()]
    return out + [f"{WARNED if note.warns else ''}note {note}" for note in loaded.notes]


def main(argv: list[str] | None = None, environ=None, out=None, err=None) -> int:
    """Show what a daemon started from this environment would read. It takes no argument and writes nothing."""

    argv = sys.argv[1:] if argv is None else argv
    out, err = out or sys.stdout, err or sys.stderr
    if argv:
        print("usage: python -m recursion_core.settings   (no argument; it reads the one settings file and "
              "writes nothing)", file=err)
        return 2
    loaded = load(os.environ if environ is None else environ)
    print(f"settings file: {loaded.path or 'none'}", file=out)
    print("These are the values a daemon started from this environment would take; a daemon already running "
          "keeps the values it read at its own start, which it wrote to its log in the lines beginning "
          "`settings file:` and `settings:` (for the shipped user unit, `journalctl --user -u recursion-daemon` "
          "shows them; a daemon started another way writes them to its standard error, wherever that start "
          "sent it).", file=out)
    for line in lines(loaded):
        print(line, file=out)
    return EXIT_WARNED if any(note.warns for note in loaded.notes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
