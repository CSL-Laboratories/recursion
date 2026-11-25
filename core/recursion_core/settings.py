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
  or below its meaningful floor keeps that setting's default (``settings_value_out_of_range``). A value
  outside a row's advice stays in force. A number row declared to accept no limit reads the string
  ``"none"`` as no limit; on a text row it is the text, and on any other row it is refused. Each note
  names the key, the value found and what is allowed.
* SHOWN. ``python -m recursion_core.settings`` prints the file path and the values a daemon started
  from this environment would take, with their defaults, sources, any advice and any notes. It takes no argument
  and writes nothing. The daemon that holds the one-daemon lock records the path and its values in its
  log at start, with the process id and a time on each line.
* ONE CAP ON A SHOWN VALUE. Every value status, the start log or a note shows passes ``_shown``, whose one
  return goes through the cap of the row ``status.value_max_chars`` (80 characters as shipped, 40 at least;
  ``"none"`` shows every value whole): a text past it is cut and the characters not shown are counted, and
  a list keeps the names that fit whole and counts the rest. A key of the file is capped the same way; a key
  that is cut carries the first characters of the SHA-256 of its whole text, said to be a digest (the row
  ``status.digest_chars``, 8 as shipped), so two keys never read the same; a key that fits is shown whole.
* NEVER SHOWN, under any cap and under ``"none"``, not in part and not by its length: the value of a
  setting whose name holds a word of the row ``status.hidden_names`` (the first net: by name, whatever the
  value looks like); the sign-in of a web address, what stands between the scheme and the host, also
  without a scheme and inside a longer text (``postgres://<sign-in hidden>@db/x``); and a text that looks
  like a key or a password. That look-alike rule is only a second net: it misses a short key, a key of one
  case or of digits only, a UUID, base64 cut by ``+`` or ``/``, and a key split over a list's items, so
  such a value under a name not on the list is shown. Every line is one line: a line break, a control
  character or a direction or format character is shown escaped (``\\n``, ``\\u202e``).
* A FILE ADDS AND NEVER TAKES AWAY. The words of ``status.hidden_names`` a file lists are added to the
  shipped words, which no file can take away; an empty or missing list keeps the shipped words alone. A
  setting named whole in ``status.shown_names`` is shown although its name holds such a word, and every
  start and every status says so in the note ``settings_shown_anyway``; the look-alike rule and the
  sign-in rule still hide what they find in its value.

Read at daemon start in this build; a later reload may call ``load`` again. ``load`` keeps no state between
calls. ``SETTINGS`` is append-only: a row is added, never renamed or given another meaning.
"""

from __future__ import annotations

import difflib
import hashlib
import math
import os
import re
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
SHOWN_ANYWAY = "settings_shown_anyway"  # a setting named in status.shown_names is shown although its name hides it
WARNED = "warning: "  # what begins the line of a note that warns, in the start log and in the command
EXIT_WARNED = 1      # the command's end when a note warns: a key, a table or the whole file skipped (2: usage)


class _NoLimit:
    __slots__ = ()

    def __repr__(self) -> str:
        return "NO_LIMIT"

    def __reduce__(self):  # a copy or a pickle round trip is this one object, never a second
        return "NO_LIMIT"


NO_LIMIT = _NoLimit()
_NO_LIMIT_TEXT = "none"
VALUE_MAX_CHARS = "status.value_max_chars"  # the row of the one cap on a shown value
HIDDEN_NAMES = "status.hidden_names"  # the row of the words of a name whose value is never shown
SHOWN_NAMES = "status.shown_names"  # the row of the settings shown although their name holds such a word
DIGEST_CHARS = "status.digest_chars"  # the row of how many hex characters of a cut key's digest are shown
# The words put in place of what is never shown; each fits the least cap, so no cap cuts them.
_HIDDEN_KEY = "<hidden: looks like a key or password>"
_HIDDEN_NAME = f"<hidden: name in {HIDDEN_NAMES}>"
_HIDDEN_SIGN_IN = "<sign-in hidden>"
# A key of the file that looks like a key or a password: its ordinal among such keys by the file's order, equal
# between two starts of the same file; no digest, no length and no character of its text.
_HIDDEN_KEY_NAME = "<hidden key-like name {} of this file>"
# A text is never shown when it holds a run of this many letters and digits of two kinds or more, a known
# key prefix or a password label followed by this many more characters, or a PEM block. Each figure is to
# become a row of the settings file.
_KEY_RUN = 20
_KEY_TAIL = 8
_KEY_PREFIXES = ("sk-", "sk_", "pk_", "rk_", "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_",
                 "glpat-", "xoxa-", "xoxb-", "xoxp-", "xoxr-", "xoxs-", "AKIA", "ASIA", "AIza", "ya29.",
                 "hf_", "npm_", "pypi-")
_KEY_RUN_TEXT = re.compile("[A-Za-z0-9]{%d,}" % _KEY_RUN)
_KEY_LABEL = re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key|bearer|token)\b\s*[:=]?\s*\S{%d,}" % _KEY_TAIL)
_KEY_PREFIX = re.compile(r"(?<![A-Za-z0-9])(?:%s)[A-Za-z0-9_.+/=-]{%d,}"
                         % ("|".join(re.escape(prefix) for prefix in _KEY_PREFIXES), _KEY_TAIL))
_SPACE = re.compile(r"(\s+)")
_Advice = tuple[int | float | None, int | float | None]  # (at least, at most)


@dataclass(frozen=True)
class Setting:
    """One setting with a TOML type and a default.

    A list row uses a tuple of strings for its default. For numeric rows, floor is the first
    meaningful value, advice gives optional inclusive bounds, and allow_no_limit opts in to "none". A list
    row with adds keeps its shipped words: the words of the file are added to them, never put in their place.
    """

    name: str
    type: type
    default: object
    says: str
    floor: int | float | None = None
    allow_no_limit: bool = False
    advice: _Advice | None = None
    adds: bool = False


# Append-only. The type is bool, int, float, str or list. A row that loosens safety has the safe value as
# its default, so a file that cannot be read as a whole, which leaves every default in force, can never
# loosen anything.
_VALUE_MAX_CHARS_ROW = Setting(
    VALUE_MAX_CHARS, int, 80,
    "The most characters of one value that status, the start log and a note show; what is past it is cut "
    'and counted, a list shows the names that fit and how many more, and "none" shows every value whole. '
    "The least is 40, so the words that say what is cut or hidden always fit.",
    floor=40, allow_no_limit=True)
_HIDDEN_NAMES_ROW = Setting(
    HIDDEN_NAMES, list, ("key", "keys", "token", "tokens", "secret", "secrets", "password", "passwords", "passwd",
                         "credential", "credentials"),
    "The words that hide a setting's value: status, the start log and a note never show the value of a setting "
    "whose name holds one of them, whatever the value looks like (a name's words stand between dots, "
    "underscores and dashes). The words a settings file lists are added to these shipped words, which no "
    "settings file can take away; an empty list keeps the shipped words alone. To show one setting's value "
    "anyway, name that setting in status.shown_names.",
    adds=True)
_SHOWN_NAMES_ROW = Setting(
    SHOWN_NAMES, list, (),
    "The settings whose value is shown although the name holds a word of status.hidden_names, each named "
    "whole as status shows it (such as db.api_token); empty as shipped. Each start and each status that uses "
    "this list says so in a note that names those settings. A text that looks like a key or password and the "
    "sign-in of an address stay hidden in such a value.")
_DIGEST_CHARS_ROW = Setting(
    DIGEST_CHARS, int, 8,
    "How many hex characters of the SHA-256 digest of a key's whole text a key cut at the cap carries in "
    "a note, so two long keys that differ only past the cap read apart; a key that fits the cap carries n"
    "one. The least is 6, since below it two keys read the same too often; past 64, the whole digest, not"
    "hing more is shown, so 64 is the advised most.",
    floor=6, advice=(None, 64))
SETTINGS: tuple[Setting, ...] = (
    Setting("plugin_host.gil_check", bool, True,
            "Whether each plugin host makes its GIL check; on by default, and a person may turn it off."),
    _VALUE_MAX_CHARS_ROW,
    _HIDDEN_NAMES_ROW,
    _SHOWN_NAMES_ROW,
    _DIGEST_CHARS_ROW,
)
DECLARED = MappingProxyType({row.name: row for row in SETTINGS})


@dataclass(frozen=True)
class Value:
    name: str
    value: object
    default: object
    source: str  # SOURCE_DEFAULT or SOURCE_FILE
    advice: _Advice | None = None


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


def _looks_like_a_key(text: str) -> bool:
    """The second net: a text taken for a key or a password by its look. It misses many keys (a short one, one
    of a single case or of digits only, a UUID, base64 cut by + or /, a key split over a list's items); the
    first net is the name of the setting (``_hidden_name``)."""

    if "-----BEGIN" in text or _KEY_LABEL.search(text) or _KEY_PREFIX.search(text):
        return True
    return any(sum((any(c.islower() for c in run), any(c.isupper() for c in run), any(c.isdigit() for c in run))) >= 2
               for run in _KEY_RUN_TEXT.findall(text))


def _without_sign_in(text: str) -> str:
    """``text`` with the sign-in of every web address in it replaced by words that say so; the rest stays."""

    if "@" not in text:
        return text
    return "".join(_run_without_sign_in(run) for run in _SPACE.split(text))


def _run_without_sign_in(run: str) -> str:
    """One run of text without a space. With a scheme, the sign-in is what stands between ``://`` and the
    host: up to the last @ before the path, or, where what stands there is no host and port (a password that
    holds a /), up to the last @ of the run. Without a scheme it is a name, a colon and what follows, up to
    the last @ of the run. What cannot be told from a host is hidden too. Each step is one pass over the run."""

    last = run.rfind("@")
    if last < 0:
        return run
    mark = run.find("://", 0, last)
    if mark >= 0:
        begin = mark + 3
        ends = [i for i in (run.find(c, begin) for c in "/?#") if i >= 0]
        authority = run[begin:min(ends, default=len(run))]
        if "@" in authority:
            end = begin + authority.rfind("@")
        else:
            host = authority.split("]", 1)[1] if authority.startswith("[") and "]" in authority else authority
            if ":" not in host or host.rsplit(":", 1)[1].isdigit() or host.endswith(":"):
                return run  # a host, or a host and its port: no sign-in, and an @ after it is in the path
            end = last
        return run[:begin] + _HIDDEN_SIGN_IN + run[end:]
    colon = run.find(":", 0, last)
    if colon < 0:
        return run
    begin = colon
    while begin > 0 and (run[begin - 1].isalnum() or run[begin - 1] in ".%+~_-"):
        begin -= 1
    return run[:begin] + _HIDDEN_SIGN_IN + run[last:]


def _hidden_name(name: str, hidden: object) -> bool:
    """Whether the setting ``name`` holds a word of ``hidden`` (the row ``status.hidden_names``). Its words stand
    between dots, underscores and dashes, and before a capital that follows a small letter; case is not
    compared. By name only, whatever the value looks like."""

    listed = {str(word).lower() for word in hidden}
    words = re.findall(r"[a-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name).lower())
    return any(word in listed for word in words)


def _hides(name: str, hidden: object, shown: object) -> bool:
    """Whether the value of the setting ``name`` is hidden by its name: the name holds a word of ``hidden``
    (the row ``status.hidden_names``) and is not named in ``shown`` (the row ``status.shown_names``)."""

    return name not in shown and _hidden_name(name, hidden)


def _one_line(text: str) -> str:
    """``text`` with every character that is not printable (a line break, a control character, a direction or
    format character) written as its escape, so a line never becomes two and never reads as another."""

    if text.isprintable():
        return text
    return "".join(c if c.isprintable() else str.__repr__(c)[1:-1] for c in text)


def _piece(value: object) -> str:
    """The whole text of one value before the cap. Only ``_shown`` calls it (and it calls itself for a list in
    a list); a value of a kind added later gets its text here and meets the cap in ``_shown``."""

    if value is NO_LIMIT:
        return "no limit"
    if isinstance(value, tuple):  # a validated list of names; a list inside one is shown the same way
        return "[" + ", ".join(_piece(name) for name in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        text = _without_sign_in(str.__str__(value))  # its own text: never a subclass's __str__ or __repr__
        if _looks_like_a_key(text):  # never shown, not in part and not by its length, whatever the cap
            return _HIDDEN_KEY
        return repr(text)
    if isinstance(value, (int, float)):
        try:
            return (float.__repr__ if isinstance(value, float) else int.__repr__)(value)
        except ValueError:  # a whole number past the interpreter's digit limit cannot be printed
            return "a whole number too long to show"
    return _one_line(f"a {type(value).__name__}")


def _digest(text: str, chars: int) -> str:
    """The first ``chars`` hex characters of the SHA-256 of the whole ``text`` as the file holds it (UTF-8), so
    that two keys cut at the same place never read the same."""

    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:chars]


def _cut(text: str, cap: object, rest: int, names: bool, digest: str = "") -> str:
    if rest:  # the names that fit were chosen with room for this count
        return f"{text}, and {rest} more" if names else f"a list of {rest}, too long to show"
    if cap is NO_LIMIT or len(text) <= cap:
        return text

    def said(more: int) -> str:
        if digest:  # a key: how much more, and the digest of its whole text
            return f"... ({more} more, digest {digest})"
        return f"... ({more} more character{'' if more == 1 else 's'} not shown)"

    keep = max(0, cap - len(said(len(text))))  # the count fits inside the cap
    return f"{text[:keep]}{said(len(text) - keep)}"


def _shown(value: object, cap: object = None, hidden: bool = False, key: bool | int = False) -> str:
    """The one way a value becomes text for a person in this module: status, the start log and every note.

    Each branch computes text; the one return passes it through the cap, ``cap`` characters (the row
    ``status.value_max_chars``, its shipped value when None; NO_LIMIT shows it whole). A text past the cap is
    cut and the characters not shown are counted, the count inside the cap; a list keeps the names that fit
    whole, never cut inside a name, and counts the rest. With ``hidden`` (the setting's name holds a word of
    ``status.hidden_names``) the value is never shown; the sign-in of an address and a text that looks like a
    key or a password are never shown either (``_piece``): the look-alike rule is only a second net. With
    ``key`` (the value is a key of the file) a cut text carries a short digest of the whole key, so that two
    keys cut at the same place never read the same; a key that fits the cap is shown whole. The digest has as
    many hex characters as ``key`` says (the row ``status.digest_chars``), its shipped value when True."""

    cap = _VALUE_MAX_CHARS_ROW.default if cap is None else cap
    text, names, rest = (_HIDDEN_NAME if hidden else _piece(value)), [], 0
    if not hidden and isinstance(value, tuple) and value and cap is not NO_LIMIT and len(text) > cap:
        room = cap - len(f", and {len(value)} more")
        width = 2  # the two brackets
        for name in value:
            piece = _piece(name)
            width += len(piece) + (2 if names else 0)
            if width > room:
                break
            names.append(piece)
        rest = len(value) - len(names)
        text = "[" + ", ".join(names) + "]"
    chars = _DIGEST_CHARS_ROW.default if key is True else key
    return _cut(text, cap, rest, bool(names), _digest(str.__str__(value), chars) if key and isinstance(value, str) else "")


def _allows_no_limit(row: Setting) -> bool:
    return row.allow_no_limit and row.type in (int, float)


def _allowed(row: Setting) -> str:
    kind = {bool: "true or false", int: "a whole number", float: "a finite number",
            str: "a text", list: "a list of strings"}[row.type]
    floor = row.floor is not None and row.type in (int, float)
    if floor:
        kind += f" of {_shown(row.floor)} or more"
    if _allows_no_limit(row):
        return f'{kind}{"," if floor else ""} or "{_NO_LIMIT_TEXT}" for no limit'
    return kind


def _taken(row: Setting, found: object) -> tuple[object | None, str | None]:
    """The value in force for ``found``, or None and the reason it is refused."""

    value = _accepted(row, found)
    if value is None:
        return None, f"allowed: {_allowed(row)}"
    if row.floor is not None and type(value) in (int, float) and value < row.floor:
        return None, f"a value below {_shown(row.floor)} has no meaning; allowed: {_allowed(row)}"
    if row.adds and isinstance(value, tuple):  # the file's words are added; the shipped ones are never taken away
        value = tuple(dict.fromkeys((*row.default, *value)))
    return value, None


def _accepted(row: Setting, value: object) -> object | None:
    """Return the typed value, or None for an invalid type or spelling."""
    if isinstance(value, str) and value == _NO_LIMIT_TEXT and _allows_no_limit(row):
        return NO_LIMIT
    if row.type is list:
        if isinstance(value, list) and all(isinstance(name, str) for name in value):
            return tuple(dict.fromkeys(value))
        return None
    if row.type in (bool, str):
        return value if isinstance(value, row.type) else None
    if isinstance(value, bool) or not isinstance(value, (int, float) if row.type is float else int):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


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


def _before_notes(flat: list, shipped: Setting) -> object:
    """The value of the row ``shipped`` this file sets (the cap on a shown value, the hidden names), read
    before any note is written; else the row's default."""

    row = DECLARED.get(shipped.name)
    held = shipped.default if row is None else row.default
    for name, found in flat:
        if row is not None and name == shipped.name:
            value, why = _taken(row, found)
            held = held if why is not None else value
    return held


def _defaults() -> dict:
    return {row.name: Value(row.name, row.default, row.default, SOURCE_DEFAULT, row.advice) for row in SETTINGS}


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
    flat = list(_flatten({k: v for k, v in (doc or {}).items() if k != "version"}))
    cap = _before_notes(flat, _VALUE_MAX_CHARS_ROW)
    names = _before_notes(flat, _HIDDEN_NAMES_ROW)
    shown = _before_notes(flat, _SHOWN_NAMES_ROW)
    chars = _before_notes(flat, _DIGEST_CHARS_ROW)
    hidden_keys = 0

    def key_text(name: str) -> str:  # a key shown is capped with its digest; a key hidden carries its ordinal only
        nonlocal hidden_keys
        if _looks_like_a_key(_without_sign_in(name)):
            hidden_keys += 1
            return _HIDDEN_KEY_NAME.format(hidden_keys)
        return _shown(name, cap, key=chars)
    for name, found in flat:
        row = DECLARED.get(name)
        if found is _QUOTED:
            notes.append(Note(UNKNOWN_KEY, f"{key_text(name)} is a quoted key that holds a dot, not a setting; "
                                           "it is skipped; write it without the quotes", warns=True))
        elif row is None:
            known = sorted({*DECLARED, *(n.rsplit(".", 1)[0] for n in DECLARED)})  # its keys and its tables
            close = difflib.get_close_matches(name, known, n=1, cutoff=0.8)  # text only; never applied
            hint = f"; did you mean {close[0]}?" if close else ""
            notes.append(Note(UNKNOWN_KEY, f"{key_text(name)} is not a setting this version knows; it is "
                                           f"skipped; remove it or check its spelling{hint}", warns=True))
        else:
            value, reason = _taken(row, found)
            if reason is None:
                values[name] = Value(name, value, row.default, SOURCE_FILE, row.advice)
                continue
            hide = _hides(name, names, shown)
            notes.append(Note(OUT_OF_RANGE, f"{name}: found {_shown(found, cap, hide)}; {reason}; "
                                            f"the default {_shown(row.default, cap, hide)} stays in force"))
    if shown:  # said at every start and in every status while the list is in use
        notes.append(Note(SHOWN_ANYWAY, f"the value of each setting named in {SHOWN_NAMES} is shown even where its "
                                        f"name holds a word of {HIDDEN_NAMES}: {_shown(shown, cap)}; a text that "
                                        "looks like a key or password and the sign-in of an address stay hidden; "
                                        f"take a name out of {SHOWN_NAMES} to hide its value again"))
    return Loaded(str(path), MappingProxyType(values), tuple(notes))


def _shown_advice(v: Value, cap: object) -> str:
    if v.advice is None:
        return ""
    lower, upper = v.advice
    bounds = []
    if lower is not None:
        bounds.append(f"at least {_shown(lower, cap)}")
    if upper is not None:
        bounds.append(f"at most {_shown(upper, cap)}")
    if not bounds:
        return ""
    text = f"; advised: {', '.join(bounds)}"
    value = v.value
    if (value is NO_LIMIT and upper is not None) or type(value) in (int, float) and (
        (lower is not None and value < lower) or (upper is not None and value > upper)
    ):
        text += "; outside the advice"
    return text


def lines(loaded: Loaded) -> list[str]:
    """What status and the daemon's start log show: each setting, its value, default, source and advice, then the notes."""

    # A row whose name holds a word of status.hidden_names shows words in place of its value and default,
    # unless status.shown_names names it.
    held = loaded.values.get(VALUE_MAX_CHARS)
    cap = _VALUE_MAX_CHARS_ROW.default if held is None else held.value
    kept = loaded.values.get(HIDDEN_NAMES)
    names = _HIDDEN_NAMES_ROW.default if kept is None else kept.value
    anyway = loaded.values.get(SHOWN_NAMES)
    shown = _SHOWN_NAMES_ROW.default if anyway is None else anyway.value
    out = [_one_line(f"{v.name} = {_shown(v.value, cap, hide)} (default {_shown(v.default, cap, hide)}; "
                     f"source: {v.source}{_shown_advice(v, cap)})")
           for v in loaded.values.values() for hide in (_hides(v.name, names, shown),)]
    return out + [_one_line(f"{WARNED if note.warns else ''}note {note}") for note in loaded.notes]


def main(argv: list[str] | None = None, environ=None, out=None, err=None) -> int:
    """Show what a daemon started from this environment would read. It takes no argument and writes nothing."""

    argv = sys.argv[1:] if argv is None else argv
    out, err = out or sys.stdout, err or sys.stderr
    if argv:
        print("usage: python -m recursion_core.settings   (no argument; it reads the one settings file and "
              "writes nothing)", file=err)
        return 2
    loaded = load(os.environ if environ is None else environ)
    print(_one_line(f"settings file: {loaded.path or 'none'}"), file=out)
    uid = os.geteuid() if hasattr(os, "geteuid") else None
    owner = "<your user id>" if uid is None else uid
    whose = (f"{uid} is the user id of the account that ran this command" if uid is not None else
             "put in place of <your user id> the number `id -u` prints for the account whose daemon you mean, "
             "on the machine that runs it")
    print("These are the values a daemon started from this environment would take; a daemon already running "
          "keeps the values it read at its own start, which it wrote to its log in the lines beginning "
          "`settings file:` and `settings:` (for the shipped user unit, `journalctl --user -u recursion-daemon` "
          "shows them only where the system journal is stored on disk; where there is no per-user journal, "
          "someone allowed to read the system journal can match the unit and its owner with "
          f"`journalctl _SYSTEMD_USER_UNIT=recursion-daemon.service _SYSTEMD_OWNER_UID={owner}` ({whose}; the "
          "unit has this same name for every account, so the unit match alone also shows the daemons of other "
          "accounts on a shared host); a daemon started another way writes them "
          "to its standard error, wherever that start sent it).", file=out)
    for line in lines(loaded):
        print(line, file=out)
    return EXIT_WARNED if any(note.warns for note in loaded.notes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
