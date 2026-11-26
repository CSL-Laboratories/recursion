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
  from this environment would take, with their defaults, sources, any advice and any notes, and, when a
  line hides a value, closing lines (``_closing_lines``) that count the lines hiding one and name where the
  person reads it: the settings file, by its path, for a value the file holds (and that it is not used,
  where it is not), and this module, for a shipped default. It takes no argument and writes nothing. The
  daemon that holds the one-daemon lock records the path and its values in its log at start, with the
  process id and a time on each line (never the closing lines).
* ONE CAP ON A SHOWN VALUE. Every value status, the start log or a note shows passes ``_shown``, whose one
  return goes through the cap of the row ``status.value_max_chars`` (80 characters as shipped, 40 at least;
  ``"none"`` shows every value whole): a text past it is cut and the characters not shown are counted, and
  a list keeps the names that fit whole and counts the rest. A key of the file is capped the same way; a key
  that is cut carries the first characters of the SHA-256 of its text after the sign-in rule's hiding
  (below), said to be a digest (the row ``status.digest_chars``, 8 as shipped, as many as fit the cap beside the
  key's first 8 characters, 6 at least), so two keys never read the same; a key that fits is shown whole.
* NEVER SHOWN, under any cap and under ``"none"``, not in part and not by its length: the value and the
  default of a row declared to hold a secret, a sign-in or an address (``Setting.holds``), which show only
  ``<set; hidden by its declaration>`` or ``<not set>``, by declaration and with no pattern, and never in a
  note, a digest or a hint, whatever ``status.shown_names`` says (two notes still count lines: the note for a
  file that is not UTF-8 and the note for a file that is not TOML name only the line of the first place the
  file cannot be read (or that the TOML reader met the end of the document), never its column, and nothing
  else of that line; the line counts the lines before that place, so for a declared value written over
  several lines it tells how many of its lines came before it, and the note's words tell which kind of fault
  it met); the value of a
  setting whose name holds a word of the row ``status.hidden_names`` (the first net: by name, whatever the
  value looks like); a text that looks like a key or a password; and a sign-in, by one rule on each run of
  text (the sign-in rule). A run is what stands between two white-space characters as Python counts them
  (``str.isspace``: a space, a tab, a line break, a no-break space, U+2028, U+001C to U+001F and the
  others); a zero-width character such as U+200B does not end a run. In a run that holds an @, everything
  before its LAST @ is hidden, except a scheme and its mark at the very start of the run (ASCII letters,
  digits, ``+``, ``.`` and ``-``, then ``://``), which stay: ``postgres://<sign-in hidden>@db/x``,
  ``<sign-in hidden>@db/x``. Nothing else before that @ is shown: not a user name or a part of it, not a
  label, not the host or the path of an earlier address (two joined addresses show as
  ``a://<sign-in hidden>@h2/``, ``https://h/u@mail`` as ``https://<sign-in hidden>@mail`` and
  ``url=a://u:p@h`` as ``<sign-in hidden>@h``); what follows the last @ is shown. Everything before the last
  at-sign of a run is hidden; of an email address only the part after the at-sign is shown. The sign-in
  rule misses a sign-in that holds white space (the run ends there, and what stands before it is shown), an
  address written wholly percent-escaped, and a secret written as the scheme at the very start of a run.
  The look-alike rule is only a second net: it misses a short key, a key of
  one case or of digits only, a UUID, base64 cut by ``+`` or ``/``, and a key split over a list's items, so
  such a value under a name not on the list is shown. A value or a key whose own text, as the person wrote
  it, carries a password label or a known key prefix is never shown, not in part, wherever an @ stands in
  it: ``password=ab@cdefgh`` is hidden whole. With ``status.label_by_rule`` true (false as shipped: then a label is only
  what that rule reads) a label is found by a rule, not a list of spellings (``_labelled``): a name-like token (words joined by ``_``, ``-`` or ``.``) in which a secret word stands as
  a part (between those joins, a step from a small letter to a capital, or between letters and digits) or as
  the end of its last part (``db_password``, ``MyPassword``, ``apitoken``), in any case, followed by anything
  at all after any marks (``=``, ``:``, ``=>``, ``->``, ``:=``, quotes, white space, ``is``); the secret words
  are those of ``status.label_words`` and ``status.hidden_names`` in force. It is read on the
  text as written and again on the text folded (percent-escapes decoded, Unicode compatibility forms and
  case folded, combining marks and format characters such as U+200B dropped), and a word written in letters
  of more than one script is a label by that fact alone; a known key prefix is read in any case. No length
  is asked after a label: a short password is still one. It still misses a label written wholly in another
  script, a look-alike letter Unicode does not fold (a dotless i), a prefix split by a plain space, and it
  hides ordinary text that reads like a label (``bypass=on``, ``token@example.org``), which stays in the
  settings file as the person wrote it. Every line is one line: a line break, a control
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
import unicodedata
import urllib.parse
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
LABEL_BY_RULE = "status.label_by_rule"  # the row of whether a password label is also read by rule
LABEL_WORDS = "status.label_words"  # the row of the words that rule reads as a password label
# The words put in place of what is never shown; each fits the least cap, so no cap cuts them.
_HIDDEN_KEY = "<hidden: looks like a key or password>"
_HIDDEN_NAME = f"<hidden: name in {HIDDEN_NAMES}>"
_HIDDEN_SIGN_IN = "<sign-in hidden>"
# A key of the file that looks like a key or a password: its ordinal among such keys by the file's order, equal
# between two starts of the same file; no digest, no length and no character of its text.
_HIDDEN_KEY_NAME = "<hidden key-like name {} of this file>"
# A cut key keeps this many characters of its shown text (the opening quote and 8 of the key) before its digest;
# the digest takes what is left of the cap, never less than the least of status.digest_chars. To become a row.
_KEY_KEPT = 9
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
_KEY_PREFIX = re.compile(r"(?<![A-Za-z0-9])(?:%s)\S{%d,}"
                         % ("|".join(re.escape(prefix) for prefix in _KEY_PREFIXES), _KEY_TAIL))
# The shipped words of the row status.label_words: the label rule (``_labelled``) reads them, and the words of
# status.hidden_names in force, while status.label_by_rule is true; a settings file adds words through that row.
_LABEL_WORDS = ("password", "passwd", "secret", "apikey", "bearer", "token", "pwd", "pass", "passphrase", "pw",
                "auth", "pin")
_LABEL_MARKS = ("=>", "->", ":=", "=", ":")  # what may stand between a label and its value, beside white space
_NAME_JOINS = "_-."  # what joins the words of one name-like token
_QUOTES = "\"'`"
_KEY_PREFIX_FOLDED = re.compile(r"(?<![a-z0-9])(?:%s)\S{%d,}"  # a known key prefix in any case, on folded text
                                % ("|".join(re.escape(prefix.casefold()) for prefix in _KEY_PREFIXES), _KEY_TAIL))
# Scripts one ordinary word may join (Japanese writes kanji and kana in one word); any other mix of scripts in one
# word is taken as a disguise. Unicode's names, not a figure.
_ONE_SCRIPT = MappingProxyType({"HIRAGANA": "CJK", "KATAKANA": "CJK", "HANGUL": "CJK"})
# What a row may declare it holds (``Setting.holds``): then status, the start log and every note show only
# whether it is set.
HOLDS_SECRET = "secret"  # a password, a key or a token
HOLDS_SIGN_IN = "sign-in"  # a user and its password
HOLDS_ADDRESS = "address"  # an address that may carry a sign-in
HOLDS = (HOLDS_SECRET, HOLDS_SIGN_IN, HOLDS_ADDRESS)
_DECLARED_SET = "<set; hidden by its declaration>"
_DECLARED_NOT_SET = "<not set>"
STAYS_HIDDEN = "settings_declared_stays_hidden"  # a row named in status.shown_names that declares a secret
_SPACE = re.compile(r"(\s+)")
_SCHEME_AT_START = re.compile(r"[A-Za-z0-9+.-]+://")  # the scheme and its mark a run may begin with
_Advice = tuple[int | float | None, int | float | None]  # (at least, at most)


@dataclass(frozen=True)
class Setting:
    """One setting with a TOML type and a default.

    A list row uses a tuple of strings for its default. For numeric rows, floor is the first
    meaningful value, advice gives optional inclusive bounds, and allow_no_limit opts in to "none". A list
    row with adds keeps its shipped words: the words of the file are added to them, never put in their place.
    A row with holds (one of ``HOLDS``) declares that its value is a secret, a sign-in or an address that may
    carry one: its value and its default are then shown only as set or not set (``_declared_text``).
    """

    name: str
    type: type
    default: object
    says: str
    floor: int | float | None = None
    allow_no_limit: bool = False
    advice: _Advice | None = None
    adds: bool = False
    holds: str | None = None

    def __post_init__(self) -> None:
        if self.holds is not None and self.holds not in HOLDS:  # a misspelt kind would show the value
            raise ValueError(f"holds must be one of {HOLDS} or None")


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
    "How many hex characters of the SHA-256 digest of a key's text, with what the sign-in rule hides taken "
    "out, a key cut at the cap carries in a note, so two long keys that differ only past the cap read "
    "apart; a key that fits the cap carries none. The least is 6, since below it two keys read the same too "
    "often; past 64, the whole digest, nothing more is shown, so 64 is the advised most. A cut key carries "
    "only as many as fit the cap beside its first 8 characters, and never fewer than 6.",
    floor=6, advice=(None, 64))
_LABEL_BY_RULE_ROW = Setting(
    LABEL_BY_RULE, bool, False,
    "Whether a text with no declared kind (a list item, a refused value, a key of the file) is also hidden whole "
    "when a word of status.label_words or status.hidden_names stands in it as a password label by rule: as a "
    "part of a name or the end of its last part, in any case, read again with percent-escapes, look-alike "
    "letters and format characters folded, with anything after it, and a known key prefix in any case. False as "
    "shipped keeps the earlier rule, which hides less ordinary text (such as 'a token budget of 5') and lets more "
    "ways of writing a password through; true hides those, and what it hides stays in your settings file as "
    "you wrote it.")
_LABEL_WORDS_ROW = Setting(
    LABEL_WORDS, list, _LABEL_WORDS,
    "The words read as a password label when status.label_by_rule is true, beside the words of "
    "status.hidden_names; not read while it is false. The words a settings file lists are added to these "
    "shipped words, which no settings file can take away; an empty list keeps the shipped words alone.",
    adds=True)
SETTINGS: tuple[Setting, ...] = (
    Setting("plugin_host.gil_check", bool, True,
            "Whether each plugin host makes its GIL check; on by default, and a person may turn it off."),
    _VALUE_MAX_CHARS_ROW,
    _HIDDEN_NAMES_ROW,
    _SHOWN_NAMES_ROW,
    _DIGEST_CHARS_ROW,
    _LABEL_BY_RULE_ROW,
    _LABEL_WORDS_ROW,
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


def _key_shape_as_written(text: str, words: object = None) -> bool:
    """Whether ``text``, as the person wrote it and before the sign-in rule, carries a password label or a known
    key prefix (``_KEY_LABEL``, ``_KEY_PREFIX``, and, where ``words`` is given, the label rule
    ``_labelled_by_rule`` with those words, ``_label_words_in_force``). Where it does, the whole text is never shown, even where the
    label or prefix stands in a part the sign-in rule would hide: then one fact about that part is shown, that
    it looks like a key or a password, and never a character of it."""

    if _KEY_LABEL.search(text) or _KEY_PREFIX.search(text):
        return True
    return words is not None and _labelled_by_rule(text, words)


def _label_words_in_force(by_rule: object, label_words: object, hidden_names: object) -> tuple | None:
    """The words the label rule reads, those of status.label_words and then of status.hidden_names, when
    status.label_by_rule (``by_rule``) is true; None when it is false: then only ``_KEY_LABEL`` and ``_KEY_PREFIX``
    read a label, the rule as shipped."""

    if by_rule is not True:
        return None
    return tuple(dict.fromkeys((*label_words, *hidden_names)))


def _secret_words(words: object = None) -> frozenset:
    """The words the label rule reads (``words``; the shipped words of status.label_words and
    status.hidden_names when None), case folded; an empty word is no word."""

    listed = (*_LABEL_WORDS_ROW.default, *_HIDDEN_NAMES_ROW.default) if words is None else words
    return frozenset(str(word).casefold() for word in listed if str(word))


def _folded(text: str) -> str:
    """``text`` as the label and prefix nets read it a second time: percent-escapes decoded, Unicode
    compatibility forms taken apart (a fullwidth or styled letter becomes its plain letter), combining marks
    and format characters (U+200B, U+00AD and the other Cf) dropped, case folded and composed again. The fold
    is Unicode's, not a list of ours."""

    text = urllib.parse.unquote(text, errors="replace")
    kept = "".join(c for c in unicodedata.normalize("NFKD", text) if unicodedata.category(c) not in ("Mn", "Me", "Cf"))
    return unicodedata.normalize("NFKC", kept.casefold())


def _parts(word: str) -> list[str]:
    """A word of letters and digits cut where a small letter meets a capital and where letters meet digits."""

    parts, begin = [], 0
    for i in range(1, len(word)):
        before, here = word[i - 1], word[i]
        if before.isdigit() != here.isdigit() or (before.islower() and here.isupper()):
            parts.append(word[begin:i])
            begin = i
    parts.append(word[begin:])
    return parts


def _mixed_script(word: str) -> bool:
    """Whether the letters of ``word`` belong to more than one script, by the first word of their Unicode names."""

    scripts = set()
    for c in word:
        if c.isalpha():
            script = unicodedata.name(c, "").split(" ")[0]
            scripts.add(_ONE_SCRIPT.get(script, script))
    return len(scripts) > 1


def _value_follows(text: str, at: int) -> bool:
    """Whether anything but marks, quotes and white space follows ``at`` (where a label ends)."""

    end = len(text)
    while at < end and text[at].isspace():
        at += 1
    for mark in _LABEL_MARKS:
        if text.startswith(mark, at):
            at += len(mark)
            break
    else:
        if text.startswith("is", at) and (at + 2 == end or not text[at + 2].isalnum()):
            at += 2
    while at < end and (text[at].isspace() or text[at] in _QUOTES):
        at += 1
    return at < end


def _labelled(text: str, secret: frozenset, scripts: bool) -> bool:
    """The label rule on one text, in one pass: a name-like token (words of letters and digits joined by one of
    ``_NAME_JOINS``) is a label when a word of ``secret`` is one of its parts (``_parts``) or ends its last part,
    or (with ``scripts``) when one of its words mixes scripts; it hides the text when anything follows it after
    any marks (``_value_follows``)."""

    end, at = len(text), 0
    while at < end:
        if not text[at].isalnum():
            at += 1
            continue
        label, last = False, ""
        while True:
            stop = at
            while stop < end and text[stop].isalnum():
                stop += 1
            word = text[at:stop]
            parts = _parts(word)
            label = label or any(part.casefold() in secret for part in parts) or (scripts and _mixed_script(word))
            last = parts[-1].casefold()
            at = stop
            if stop + 1 < end and text[stop] in _NAME_JOINS and text[stop + 1].isalnum():
                at = stop + 1
                continue
            break
        if (label or any(last.endswith(word) for word in secret)) and _value_follows(text, at):
            return True
    return False


def _labelled_by_rule(text: str, words: object = None) -> bool:
    """Whether ``text`` carries a password label by the label rule (``_labelled``), read on the text as written
    and on it folded (``_folded``), or a known key prefix in any case, as written or folded. ``words`` are the
    label words in force (``_label_words_in_force``; the shipped ones when None). One pass over each text: linear."""

    secret = _secret_words(words)
    folded = _folded(text)
    return (_labelled(text, secret, False) or _labelled(folded, secret, True)
            or bool(_KEY_PREFIX_FOLDED.search(text.casefold()) or _KEY_PREFIX_FOLDED.search(folded)))


def _never_shown(text: str, words: object = None) -> bool:
    """Whether a text of the person's file, a value or a key, is never shown, not in part: the one predicate
    the value path (``_piece``) and the key path (``load``) both ask. A password label or a known key prefix
    is read on the text as the person wrote it, so the sign-in rule cannot take a label away and leave the end
    of its password in view: ``password=ab@cdefgh`` is hidden whole, wherever the @ stands. The whole
    look-alike rule is read on the text after the sign-in rule as well. The long-run test is not read on the
    text as written: a long run inside a sign-in the rule hides would hide the whole address."""

    return _key_shape_as_written(text, words) or _looks_like_a_key(_without_sign_in(text))


def _without_sign_in(text: str) -> str:
    """``text`` with what the sign-in rule hides in each run (``_run_without_sign_in``) replaced by words that
    say so; the rest stays. A run ends at every white-space character as Python counts them (``str.isspace``);
    a zero-width character does not end a run."""

    if "@" not in text:
        return text
    return "".join(_run_without_sign_in(run) for run in _SPACE.split(text))


def _run_without_sign_in(run: str) -> str:
    """One run of text with no white space in it, by one rule that errs on the side of hiding: everything
    before the run's LAST @ is hidden, except a scheme and its mark at the very start of the run
    (``_SCHEME_AT_START``: ASCII letters, digits, ``+``, ``.`` and ``-``, then ``://``), which stay. Nothing
    else before that @ is shown: not a user name or a part of it, not a label, not a host or a path of an
    earlier address. What follows the last @ is shown. So of a mail address only the part after the @ is
    shown; a run with no @, or with nothing before its last @ but such a scheme, stays whole."""

    last = run.rfind("@")
    if last < 0:
        return run
    scheme = _SCHEME_AT_START.match(run)  # it holds no @, so it ends at or before the last @
    begin = scheme.end() if scheme else 0
    if begin == last:
        return run
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


def _piece(value: object, words: object = None) -> str:
    """The whole text of one value before the cap. Only ``_shown`` calls it (and it calls itself for a list in
    a list); a value of a kind added later gets its text here and meets the cap in ``_shown``."""

    if value is NO_LIMIT:
        return "no limit"
    if isinstance(value, tuple):  # a validated list of names; a list inside one is shown the same way
        return "[" + ", ".join(_piece(name, words) for name in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        text = str.__str__(value)  # its own text: never a subclass's __str__ or __repr__
        if _never_shown(text, words):  # never shown, not in part and not by its length, whatever the cap
            return _HIDDEN_KEY
        return repr(_without_sign_in(text))
    if isinstance(value, (int, float)):
        try:
            return (float.__repr__ if isinstance(value, float) else int.__repr__)(value)
        except ValueError:  # a whole number past the interpreter's digit limit cannot be printed
            return "a whole number too long to show"
    return _one_line(f"a {type(value).__name__}")


def _digest(text: str, chars: int) -> str:
    """The first ``chars`` hex characters of the SHA-256 of ``text`` (UTF-8), so that two keys cut at the same
    place never read the same. ``_shown`` gives it the key's text after the sign-in rule's hiding
    (``_without_sign_in``), so nothing that rule hides enters the hash."""

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

    if digest:  # as many digest characters as fit beside the key's first characters, never fewer than the least
        room = cap - len(f"... ({len(text)} more, digest )") - _KEY_KEPT
        digest = digest[:max(_DIGEST_CHARS_ROW.floor, room)]  # at the least cap every key of MAX_BYTES keeps 8
    keep = max(0, cap - len(said(len(text))))  # the count fits inside the cap
    return f"{text[:keep]}{said(len(text) - keep)}"


def _declared_text(value: object) -> str:
    """What status, the start log and a note show of a value of a row that declares it holds a secret, a sign-in
    or an address (``Setting.holds``): only whether it is set, never a character, a length, a digest or a kind
    of it, by declaration and with no pattern. One rule for the three kinds; were a part of an address ever to be
    shown, its host would be shown here, in this one function, and nowhere else."""

    unset = value is None or (isinstance(value, (str, tuple)) and not value)
    return _DECLARED_NOT_SET if unset else _DECLARED_SET


def _holds(name: str) -> bool:
    """Whether the declared row ``name`` declares that it holds a secret, a sign-in or an address."""

    row = DECLARED.get(name)
    return row is not None and row.holds is not None


def _shown(value: object, cap: object = None, hidden: bool = False, key: bool | int = False,
           declared: bool = False, words: object = None) -> str:
    """The one way a value becomes text for a person in this module: status, the start log and every note.

    Each branch computes text; the one return passes it through the cap, ``cap`` characters (the row
    ``status.value_max_chars``, its shipped value when None; NO_LIMIT shows it whole). A text past the cap is
    cut and the characters not shown are counted, the count inside the cap; a list keeps the names that fit
    whole, never cut inside a name, and counts the rest. With ``hidden`` (the setting's name holds a word of
    ``status.hidden_names``) the value is never shown; the sign-in of an address and a text that looks like a
    key or a password are never shown either (``_piece``): the look-alike rule is only a second net. With
    ``key`` (the value is a key of the file) a cut text carries a short digest of the key's text after the
    sign-in rule's hiding, so that two keys cut at the same place never read the same and nothing that rule
    hides enters the hash; a key that fits the cap is shown whole. The digest has as many hex characters
    as ``key`` says (the row ``status.digest_chars``), its shipped value when True, or as fit the cap beside the
    key's first characters, never fewer than the row's least. With ``declared`` (the row declares it holds a
    secret) only whether the value is set is shown (``_declared_text``), whatever ``hidden`` says: no digest,
    no list and no character of it. ``words`` are the label words in force while status.label_by_rule is true
    (``_label_words_in_force``), else None."""

    cap = _VALUE_MAX_CHARS_ROW.default if cap is None else cap
    if declared:
        text, names, rest = _declared_text(value), [], 0
    else:
        text, names, rest = (_HIDDEN_NAME if hidden else _piece(value, words)), [], 0
    if not hidden and not declared and isinstance(value, tuple) and value and cap is not NO_LIMIT and len(text) > cap:
        room = cap - len(f", and {len(value)} more")
        width = 2  # the two brackets
        for name in value:
            piece = _piece(name, words)
            width += len(piece) + (2 if names else 0)
            if width > room:
                break
            names.append(piece)
        rest = len(value) - len(names)
        text = "[" + ", ".join(names) + "]"
    chars = _DIGEST_CHARS_ROW.default if key is True else key
    digest = _digest(_without_sign_in(str.__str__(value)), chars) if key and not declared and isinstance(value, str) else ""
    return _cut(text, cap, rest, bool(names), digest)


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
        if row.holds is not None:  # by declaration: the rule only, never which way the value failed it
            return None, f"allowed: {_allowed(row)}"
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


# The place the TOML reader puts at the end of its words; the note keeps its line (or that it is the end of the
# document) and neither its column, which counts the characters before it on its line, nor a word before it.
_TOML_PLACE = re.compile(r"\((?:at line (\d+), column \d+|at end of document)\)\Z")


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
    except UnicodeDecodeError as exc:  # its line only: a byte's place would tell the length of what stands before it
        line = data.count(b"\n", 0, exc.start) + 1
        return None, f"is not UTF-8 (at line {line}); save it as UTF-8"
    except tomllib.TOMLDecodeError as exc:  # its line only: the reader's words can name a character of a value
        place = _TOML_PLACE.search(str(exc))
        if place is None:
            return None, "is not TOML; correct the file"
        where = "(at end of document)" if place.group(1) is None else f"(at line {place.group(1)})"
        return None, f"is not TOML {where}; correct the file there"
    except (ValueError, RecursionError):  # plain ones, with no line; their words can count a number's digits
        return None, ("holds a value or a nesting the TOML reader cannot take (a whole number with more digits than "
                      "this interpreter reads, or lists, tables or keys nested deeper than it follows); shorten it")
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
    labels = _label_words_in_force(_before_notes(flat, _LABEL_BY_RULE_ROW), _before_notes(flat, _LABEL_WORDS_ROW),
                                   names)
    hidden_keys = 0

    def key_text(name: str) -> str:  # a key shown is capped with its digest; a key hidden carries its ordinal only
        nonlocal hidden_keys
        if _never_shown(name, labels):
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
            # text only, never applied, and only for a key that is shown: a hidden key never tells what it resembles
            close = [] if _never_shown(name, labels) else difflib.get_close_matches(name, known, n=1, cutoff=0.8)
            hint = f"; did you mean {close[0]}?" if close else ""
            notes.append(Note(UNKNOWN_KEY, f"{key_text(name)} is not a setting this version knows; it is "
                                           f"skipped; remove it or check its spelling{hint}", warns=True))
        else:
            value, reason = _taken(row, found)
            if reason is None:
                values[name] = Value(name, value, row.default, SOURCE_FILE, row.advice)
                continue
            if row.holds is not None:  # by declaration: never what was found, only the key and what is allowed
                notes.append(Note(OUT_OF_RANGE, f"{name}: the value found is not used and not shown, since this "
                                                f"setting holds a {row.holds}; {reason}; the default "
                                                f"{_shown(row.default, cap, declared=True)} stays in force"))
                continue
            hide = _hides(name, names, shown)
            notes.append(Note(OUT_OF_RANGE, f"{name}: found {_shown(found, cap, hide, words=labels)}; {reason}; "
                                            f"the default {_shown(row.default, cap, hide, words=labels)} stays in force"))
    if shown:  # said at every start and in every status while the list is in use
        notes.append(Note(SHOWN_ANYWAY, f"the value of each setting named in {SHOWN_NAMES} is shown even where its "
                                        f"name holds a word of {HIDDEN_NAMES}: {_shown(shown, cap, words=labels)}; a text that "
                                        "looks like a key or password and the sign-in of an address stay hidden; "
                                        f"take a name out of {SHOWN_NAMES} to hide its value again"))
    for name in shown:
        if _holds(name):  # status.shown_names never lifts a declaration
            notes.append(Note(STAYS_HIDDEN, f"{name} is named in {SHOWN_NAMES}, but it holds a "
                                            f"{DECLARED[name].holds}, so status, the start log and every note show "
                                            "only whether it is set; naming it there changes nothing, so take it "
                                            "out"))
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
    if _holds(v.name):  # by declaration: the advice is the row's, and nothing that depends on the value is said
        return text
    if (value is NO_LIMIT and upper is not None) or type(value) in (int, float) and (
        (lower is not None and value < lower) or (upper is not None and value > upper)
    ):
        text += "; outside the advice"
    return text


def _value_texts(loaded: Loaded) -> tuple[object, list[tuple[Value, str, str]]]:
    """The cap in force, and for each setting of ``loaded`` the text status shows of its value and of its default."""

    # A row whose name holds a word of status.hidden_names shows words in place of its value and default,
    # unless status.shown_names names it.
    held = loaded.values.get(VALUE_MAX_CHARS)
    cap = _VALUE_MAX_CHARS_ROW.default if held is None else held.value
    kept = loaded.values.get(HIDDEN_NAMES)
    names = _HIDDEN_NAMES_ROW.default if kept is None else kept.value
    anyway = loaded.values.get(SHOWN_NAMES)
    shown = _SHOWN_NAMES_ROW.default if anyway is None else anyway.value
    by_rule, words = loaded.values.get(LABEL_BY_RULE), loaded.values.get(LABEL_WORDS)
    labels = _label_words_in_force(_LABEL_BY_RULE_ROW.default if by_rule is None else by_rule.value,
                                   _LABEL_WORDS_ROW.default if words is None else words.value, names)
    texts = []
    for v in loaded.values.values():
        hide, declared = _hides(v.name, names, shown), _holds(v.name)
        texts.append((v, _shown(v.value, cap, hide, declared=declared, words=labels),
                      _shown(v.default, cap, hide, declared=declared, words=labels)))
    return cap, texts


def lines(loaded: Loaded) -> list[str]:
    """What status and the daemon's start log show: each setting, its value, default, source and advice, then the notes."""

    cap, texts = _value_texts(loaded)
    out = [_one_line(f"{v.name} = {value} (default {default}; source: {v.source}{_shown_advice(v, cap)})")
           for v, value, default in texts]
    return out + [_one_line(f"{WARNED if note.warns else ''}note {note}") for note in loaded.notes]


_HIDDEN_MARKS = (_HIDDEN_KEY, _HIDDEN_NAME, _DECLARED_SET, _HIDDEN_KEY_NAME.split("{")[0], _HIDDEN_SIGN_IN)


def _hides_a_value(text: str) -> bool:
    return any(mark in text for mark in _HIDDEN_MARKS)


def _closing_lines(loaded: Loaded) -> list[str]:
    """The lines status ends with when a line it shows hides a value; ``lines()``, and so the start log, never
    carry them. A line counts once. One line counts the lines that hide a value the settings file holds (a value
    in force from the file, a value a note says was found there, a key of the file) and names the file, by its
    path, where the person reads it as they wrote it; the other counts the lines that hide only a shipped default
    and names this module, where that default is written. Where the file is not read or not used, both say so."""

    _, texts = _value_texts(loaded)
    defaults = {v.name: default for v, _, default in texts}
    of_file = only_default = 0
    for v, value, default in texts:
        if v.source == SOURCE_FILE and _hides_a_value(value):
            of_file += 1
        elif _hides_a_value(value) or _hides_a_value(default):
            only_default += 1
    for note in loaded.notes:
        text, name = str(note), note.detail.split(":", 1)[0]
        if note.code == OUT_OF_RANGE and name in defaults:
            if _holds(name):  # the value found there is not shown at all, by declaration
                of_file += 1
                continue
            text = "".join(text.split(f"the default {defaults[name]} stays in force"))  # counted on its row's line
        of_file += _hides_a_value(text)
    if any(note.code == NOT_READ_HERE for note in loaded.notes):
        unused = " (this system reads no settings file)"
    elif loaded.path is None:
        unused = " (no settings file is read: there is no absolute XDG_CONFIG_HOME or HOME)"
    elif any(note.code == UNREADABLE for note in loaded.notes):
        unused = f" (the settings file {loaded.path} is not used, as the note above says)"
    else:
        unused = ""

    def counted(n: int) -> str:
        return f"{n} line{'' if n == 1 else 's'} above hide{'s' if n == 1 else ''}"

    out = []
    if of_file:
        out.append(f"{counted(of_file)} a value the settings file holds{unused}; open {loaded.path} yourself to "
                   "read it as you wrote it")
    if only_default:
        out.append(f"{counted(only_default)} only a shipped default{unused}; it is written in the product's "
                   f"settings module, {__file__}")
    return out


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
    for line in _closing_lines(loaded):  # in the command's output only, never in lines() or the start log
        print(_one_line(line), file=out)
    return EXIT_WARNED if any(note.warns for note in loaded.notes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
