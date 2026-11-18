"""The systemd user unit for the per-user daemon. The text is data: nothing here starts systemd.

``Restart=on-failure`` and ``WantedBy=default.target`` are the directives in systemd.service(5) and
systemd.unit(5): the unit comes back after a crash, and ``enable`` attaches it to the user manager's
default target. ``KillMode=control-group`` ends only this unit's own processes: runs live in their
own transient ``recursion-run-*.service`` units, outside this unit's cgroup.

The stop timeout is the external backstop for a stalled drain (the requirement Codex wrote in its
read of plan version 3.3, applied as written): a stop that has not finished after
``TimeoutStopSec`` is ended with SIGKILL (``SendSIGKILL=yes``). That expiry is a daemon crash, not a
clean stop: the run units stay alive, and durable intents and ambiguous attempts are recovered at
the next start under seam S1. The timeout never lets the daemon's own close return, raise or close
a resource still in use; the kill ends the process instead (seam S13's shutdown rule).
"""

from __future__ import annotations

import os
import re

from .errors import InstallRefused

UNIT_NAME = "recursion-daemon.service"
DAEMON_MODULE = "recursion_core.daemon"
# The unit's first line. ``uninstall`` removes only a unit that starts with it.
MARKER = "# Recursion per-user daemon, written by `recursiond install` (csl-recursion)."
STOP_TIMEOUT_S = 90  # systemd's own default, stated: finite, and long enough for a normal drain
# The daemon keeps its state in $XDG_STATE_HOME/recursion, or <home>/.local/state/recursion when the
# variable is unset (daemon/__main__.py). The unit sets the variable to that default under the user
# manager's %h, the folder `install` creates the stores in, so a value in the user manager's own
# environment cannot point the daemon at another state folder than the one install prepared.
STATE_HOME = ".local/state"  # relative to the home
MAX_ARGS = 16
MAX_ARG_LEN = 256
_ARG = re.compile(r"[A-Za-z0-9_./:%@=+-]+")
# systemd 255's line ends, a NUL among them (Grok R1 on #382); str.splitlines also ends lines at \v, \f, \x1c-\x1e,
# \x85, \u2028 and \u2029, which systemd reads within a line (Grok R3 of round 10)
_LINE_END = re.compile(r"\r\n|\r|\n|\x00")
_BLANKS = " \t\r\n"  # what systemd strips around a line, a key and a value
_ESCAPES = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\", "\"": "\"",
            "'": "'", "s": " "}  # systemd's C escapes (cunescape_one); \x, \u, \U and three octal digits below
_HEX_ESCAPES = {"x": 2, "u": 4, "U": 8}
_HEX_DIGITS = "0123456789abcdefABCDEF"
_OCTAL_DIGITS = "01234567"


def daemon_exec_args(interpreter: str) -> tuple[str, ...]:
    """The ``ExecStart`` vector for the daemon under ``interpreter``, an installed Python's path.

    ``-I`` keeps the daemon from reading ``PYTHONPATH``, the user site folder or the working
    folder, so it runs the package installed beside that interpreter and nothing else. ``-X gil=0``
    turns the GIL off on the free-threaded build the daemon requires. The path is kept as given,
    not resolved: a virtual environment's ``bin/python`` is a link whose target is outside it. Its
    ``%`` signs are written ``%%``: systemd reads ``%`` in ``ExecStart`` as a specifier, and
    ``%%`` is its literal ``%``, so the unit runs this exact path.
    """

    if type(interpreter) is not str or not os.path.isabs(interpreter):
        raise InstallRefused("exec_arg", "the interpreter path is not absolute")
    return (interpreter.replace("%", "%%"), "-I", "-X", "gil=0", "-m", DAEMON_MODULE)


def render_unit(exec_args: tuple[str, ...]) -> str:
    """Return the unit file. ``exec_args`` is the ``ExecStart`` argument vector.

    Each argument is one token (no spaces, no quotes, no ``..`` segment), so the line needs no
    shell and no quoting. The first token is an absolute path or a systemd specifier such as ``%h``.
    A ``%`` is written as given, so systemd expands it: an argument meant literally has its ``%``
    written ``%%`` before it gets here (``daemon_exec_args`` does that for the interpreter's path).
    A path with a space in it is refused (``exec_arg``) rather than quoted.
    """

    if type(exec_args) is not tuple or not exec_args or len(exec_args) > MAX_ARGS:
        raise InstallRefused("exec_args")
    for arg in exec_args:
        if type(arg) is not str or not _ARG.fullmatch(arg) or len(arg) > MAX_ARG_LEN:
            raise InstallRefused("exec_arg", "each argument is one token of letters, digits and ./:%@=+-_")
        if ".." in arg.split("/"):
            raise InstallRefused("exec_arg", "no '..' segment")
    if not (exec_args[0].startswith("/") or exec_args[0].startswith("%")):
        raise InstallRefused("exec_arg", "the program is an absolute path")
    text = (
        f"{MARKER}\n"
        "# `recursiond upgrade` rewrites this file and `recursiond uninstall` removes it.\n"
        "# Runs live in their own transient recursion-run-*.service units, not in this\n"
        "# unit's cgroup, so stopping or killing this unit never stops a run.\n"
        f"# A stop still running after {STOP_TIMEOUT_S} s is ended with SIGKILL: a daemon crash,\n"
        "# with the run units left alive and their state recovered at the next start.\n"
        f"# The daemon's state, with the stores `recursiond install` creates, is in %h/{STATE_HOME}/recursion.\n"
        "[Unit]\n"
        "Description=Recursion per-user daemon\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "UnsetEnvironment=PYTHONPATH PYTHONHOME\n"
        f"Environment=XDG_STATE_HOME=%h/{STATE_HOME}\n"
        f"ExecStart={' '.join(exec_args)}\n"
        "Restart=on-failure\n"
        "RestartSec=2\n"
        "UMask=0077\n"
        "KillMode=control-group\n"
        f"TimeoutStopSec={STOP_TIMEOUT_S}\n"
        "SendSIGKILL=yes\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    return text


def state_elsewhere(unit: bytes, home: str) -> str | None:
    """Why the daemon a unit starts may keep its state in another folder than the one ``render_unit``
    names, or ``None``: what ``upgrade`` checks in a unit before it replaces it (the S6 sweep's F1 on
    #410: a hand-placed unit's own ``XDG_STATE_HOME`` was dropped, and the daemon's revocations with it).

    Its ``[Service]`` section is read as systemd reads it, near enough: an ``Environment=`` line is a
    list of assignments, each in double or single quotes or none, an empty one drops those before it,
    and the last ``XDG_STATE_HOME`` counts; a value naming ``home``'s ``.local/state`` (``%h`` is
    ``home``) is this unit's own. ``EnvironmentFile=`` can set it (its file is not read here), and
    ``PassEnvironment=XDG_STATE_HOME`` gives the daemon the user manager's own value; ``UnsetEnvironment``
    naming it with a bare name leaves the default (a ``NAME=value`` there drops only that exact assignment,
    as systemd drops it; with no assignment of that name in the unit, the manager's own value may be the one
    removed, which cannot be told), under ``$HOME``: the daemon's default is ``Path.home()``, which reads
    ``HOME``, so a unit's own ``HOME`` (or an ``EnvironmentFile``, which can set it) moves the state too,
    unless it names ``home`` or ``UnsetEnvironment`` drops it. The first column-0 byte order mark in the
    file is skipped and that line is read (one at byte 0 already counts); a later column-0 mark stays on
    the key. On a syntax error in one of these settings (a backslash then a space or a tab, an unknown
    escape), the words already accepted stay and that line stops. A ``\\u`` noncharacter skips that word
    and the line continues; a ``\\U`` noncharacter stops the line. What remains is applied, and a unit
    whose applied state is the default is rewritten. Lines end where systemd ends
    them, at ``\\r\\n``, ``\\r``, ``\\n`` or a NUL byte only, and words are split and C escapes decoded by systemd's
    table (``_words``), and an empty ``XDG_STATE_HOME`` is the daemon's default, as the
    daemon reads it (``os.environ.get('XDG_STATE_HOME') or ...``). A unit that cannot be read
    so is answered too: then where it keeps the state cannot be told. A value only in the user manager's own
    environment
    (``environment.d``, ``systemctl --user set-environment``) is not seen: this package starts no
    process to ask it.
    """

    try:
        text = unit.decode("utf-8-sig")  # a byte order mark first is skipped, as systemd skips it (DeepSeek H9-3)
    except UnicodeDecodeError:
        return "is not UTF-8 text, so where it keeps the daemon's state cannot be told"
    section = ""
    assigned: dict[str, str] = {}
    files: list[str] = []
    passed: set[str] = set()
    unset: set[str] = set()  # names UnsetEnvironment drops whatever their value
    unset_pairs: set[str] = set()  # NAME=value assignments it drops, only those exactly
    lines = _LINE_END.split(text)  # not str.splitlines, whose other line ends systemd reads within a line (Grok R3)
    # systemd skips one column-0 mark in the file and reads that line. utf-8-sig already took a mark at byte 0,
    # so that one counts; a later column-0 mark stays on the key (Grok R1 of review 13).
    bom_skipped = unit.startswith(b"\xef\xbb\xbf")
    i = 0
    while i < len(lines):
        raw, bom_skipped = _one_mark(lines[i], bom_skipped)
        i += 1
        line = raw.strip(_BLANKS)
        if not line or line[0] in "#;":  # a comment line is skipped whole, a backslash at its end too
            continue
        # A line continues on an odd run of backslashes at its very end, as read (a blank after the backslash ends it,
        # Grok R2 of round 12; an even run is escaped backslashes), and comment lines inside a continuation are
        # skipped, as systemd's parser skips them (DeepSeek F11-2 on #382).
        while _continues(raw) and i < len(lines):
            raw, bom_skipped = _one_mark(lines[i], bom_skipped)
            i += 1
            following = raw.strip(_BLANKS)
            if following[:1] in ("#", ";"):
                raw = line  # a skipped comment leaves the line continuing
                continue
            line = line[:-1] + " " + following
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, sep, value = line.partition("=")
        if not sep:  # no '=': systemd ignores the line, it is not an empty directive (DeepSeek F11-1 on #382)
            continue
        key, value = key.strip(_BLANKS), value.strip(_BLANKS)
        if section != "Service" or key not in ("Environment", "EnvironmentFile", "PassEnvironment",
                                                "UnsetEnvironment"):
            continue
        if key == "EnvironmentFile":
            files = files + [value] if value else []
            continue
        words = _words(value)
        if words is None:
            # A syntax error keeps every word already accepted and stops the line. A \u noncharacter skips
            # that word; a \U noncharacter stops the line. A line that stored nothing leaves earlier lines
            # (Grok r14 R1, R2). Any other value this check cannot read refuses the unit.
            kept = _kept_words(value)
            if kept is None:
                return f"has {key}={value}, which cannot be read, so where it keeps the daemon's state cannot be told"
            if not kept:
                continue
            words = kept
        if key == "Environment":
            if not words:
                assigned = {}
            for word in words:
                name, has, setting = word.partition("=")
                if has:
                    assigned[name] = setting
        elif key == "PassEnvironment":  # an empty one drops those before it
            passed = passed | set(words) if words else set()
        else:  # UnsetEnvironment: a name drops the variable, NAME=value only that exact assignment (systemd.exec(5);
            # DeepSeek F10-1 on #382); an empty one drops those before it
            unset = unset | {word for word in words if "=" not in word} if words else set()
            unset_pairs = unset_pairs | {word for word in words if "=" in word} if words else set()
    # UnsetEnvironment is applied after Environment, EnvironmentFile and PassEnvironment.
    if "XDG_STATE_HOME" not in unset:
        if files:
            return f"reads EnvironmentFile={files[0]}, which can set XDG_STATE_HOME"
        if "XDG_STATE_HOME" in passed:
            return "passes the user manager's own XDG_STATE_HOME to the daemon (PassEnvironment)"
        value = assigned.get("XDG_STATE_HOME")
        if value and f"XDG_STATE_HOME={value}" not in unset_pairs:  # empty: the default, as the daemon reads it (Grok R2)
            if _names(value, os.path.join(home, *STATE_HOME.split("/")), home):
                return None
            return f"sets XDG_STATE_HOME={value}"
        if value is None and any(pair.startswith("XDG_STATE_HOME=") for pair in unset_pairs):
            # No assignment in the unit. systemd removes the manager's variable only when it holds this exact
            # value, and this package does not read that environment, so the folder cannot be told (DeepSeek F3-2).
            shown = min(pair for pair in unset_pairs if pair.startswith("XDG_STATE_HOME="))
            return (f"has UnsetEnvironment={shown}, which may remove the user manager's own value, "
                    "so where it keeps the daemon's state cannot be told")
    # No XDG_STATE_HOME: the state is under Path.home(), which reads HOME, so the unit's own HOME moves it too
    # (DeepSeek H9-2 on #382). The user manager's own HOME is the home's.
    if "HOME" not in unset:
        if files:
            return f"reads EnvironmentFile={files[0]}, which can set HOME"
        value = assigned.get("HOME")
        if value is not None and f"HOME={value}" not in unset_pairs and not _names(value, home, home):
            return f"sets HOME={value}, so the daemon's default state folder is under it"
    return None


def _words(value: str) -> list[str] | None:
    """``value`` split as systemd splits ``Environment=``, ``PassEnvironment=`` and ``UnsetEnvironment=``
    (``extract_first_word`` with ``EXTRACT_UNQUOTE`` and ``EXTRACT_CUNESCAPE``): words between blanks, quotes
    taken away wherever they stand, and C escapes decoded by systemd's table, inside quotes too (Grok R3 on #382:
    ``shlex.split`` made ``\\s`` an ``s`` and ``\\x45`` an ``x45``). ``None`` when systemd would not read it, or when an
    escape gives a character this check does not follow: an unknown escape, a NUL, a byte above 0x7f, a noncharacter,
    a quote left open or a backslash at the end. ``_kept_words`` then keeps what was already accepted; a value
    that stored nothing and still cannot be read is refused."""

    words: list[str] = []
    word: list[str] = []
    quote = ""
    started = False
    i = 0
    while i < len(value):
        c = value[i]
        if c == "\\":
            decoded, i = _escape(value, i)
            if decoded is None:
                return None
            word.append(decoded)
            started = True
        elif quote:
            if c == quote:
                quote = ""
            else:
                word.append(c)
            i += 1
        elif c in "\"'":
            quote, started = c, True
            i += 1
        elif c in _BLANKS:
            if started:
                words.append("".join(word))
                word, started = [], False
            i += 1
        else:
            word.append(c)
            started = True
            i += 1
    if quote:
        return None
    if started:
        words.append("".join(word))
    return words


def _escape(value: str, i: int) -> tuple[str | None, int]:
    """The character the C escape at ``value[i]`` (a backslash) stands for, by systemd's table, and the index after
    it; ``None`` for one this check does not follow."""

    if i + 1 >= len(value):
        return None, i + 1
    e = value[i + 1]
    if e in _ESCAPES:
        return _ESCAPES[e], i + 2
    if e in _HEX_ESCAPES:
        size = _HEX_ESCAPES[e]
        digits = value[i + 2:i + 2 + size]
        if len(digits) != size or not all(d in _HEX_DIGITS for d in digits):
            return None, i + 2
        code = int(digits, 16)
        limit = 0x7F if e == "x" else 0x10FFFF
        # A noncharacter is not stored here. Deleting the rejection lets /.. name the default folder over an
        # earlier one; _kept_words then skips a \u word or stops the line on \U (Grok r14 R2).
        if code == 0 or code > limit or 0xD800 <= code <= 0xDFFF or _noncharacter(code):
            return None, i + 2
        return chr(code), i + 2 + size
    if e in _OCTAL_DIGITS:
        digits = value[i + 1:i + 4]
        if len(digits) != 3 or not all(d in _OCTAL_DIGITS for d in digits):
            return None, i + 1
        code = int(digits, 8)
        if code == 0 or code > 0x7F:
            return None, i + 1
        return chr(code), i + 4
    return None, i + 2  # an escape systemd's table does not have: systemd refuses the rest of the line


def _one_mark(raw: str, skipped: bool) -> tuple[str, bool]:
    """Drop one column-0 U+FEFF. A later one stays, so the key is not the name systemd would read."""

    if not skipped and raw.startswith("\ufeff"):
        return raw[1:], True
    return raw, skipped


def _noncharacter(code: int) -> bool:
    """Whether ``code`` is a Unicode noncharacter. systemd's ``unichar_is_valid`` rejects these.
    A ``\\u`` one skips that word; a ``\\U`` one stops the line."""

    return 0xFDD0 <= code <= 0xFDEF or (code & 0xFFFF) in (0xFFFE, 0xFFFF)


def _unicode_noncharacter(value: str, i: int) -> str:
    """``'u'`` or ``'U'`` when the escape at ``value[i]`` is a noncharacter, else ``''``.

    A ``\\u`` surrogate is also ``'u'``. ``cunescape_one`` decodes ``\\u`` without
    ``unichar_is_valid``, so that word is dropped and the line goes on. A ``\\U`` surrogate
    fails ``unichar_is_valid`` and stays ``''``, which stops the line.
    """

    if i + 1 >= len(value) or value[i] != "\\":
        return ""
    letter = value[i + 1]
    if letter not in ("u", "U"):
        return ""
    size = 4 if letter == "u" else 8
    digits = value[i + 2:i + 2 + size]
    if len(digits) != size or any(d not in _HEX_DIGITS for d in digits):
        return ""
    code = int(digits, 16)
    if letter == "u" and 0xD800 <= code <= 0xDFFF:
        return "u"
    if code == 0 or code > 0x10FFFF or 0xD800 <= code <= 0xDFFF or not _noncharacter(code):
        return ""
    return letter


def _past_word(value: str, i: int, quote: str) -> int:
    """Index of the blank after the word that holds the ``\\u`` noncharacter at ``i``, or the end."""

    size = 4 if value[i + 1] == "u" else 8
    i += 2 + size
    while i < len(value):
        c = value[i]
        if c == "\\":
            _decoded, nxt = _escape(value, i)
            i = nxt if nxt > i else i + 1
            continue
        if quote:
            if c == quote:
                quote = ""
            i += 1
            continue
        if c in "\"'":
            quote = c
            i += 1
            continue
        if c in _BLANKS:
            return i
        i += 1
    return i


def _kept_words(value: str) -> list[str] | None:
    """Words systemd keeps when ``_words`` cannot read ``value``.

    ``None``: nothing was stored and the value still cannot be read, so the unit is refused.
    An empty list: this line stores nothing, and assignments already read stay.
    Otherwise: the words accepted before a syntax error or a ``\\U`` stop, or the words left after a
    ``\\u`` noncharacter was skipped.
    """

    words: list[str] = []
    word: list[str] = []
    quote = ""
    started = False
    i = 0
    while i < len(value):
        c = value[i]
        if c == "\\":
            mark = _unicode_noncharacter(value, i)
            if mark == "u":
                word, started = [], False
                i = _past_word(value, i, quote)
                quote = ""
                continue
            if mark == "U":
                return words
            decoded, i = _escape(value, i)
            if decoded is None:
                return words if words else None
            word.append(decoded)
            started = True
            continue
        if quote:
            if c == quote:
                quote = ""
            else:
                word.append(c)
            i += 1
            continue
        if c in "\"'":
            quote, started = c, True
            i += 1
            continue
        if c in _BLANKS:
            if started:
                words.append("".join(word))
                word, started = [], False
            i += 1
            continue
        word.append(c)
        started = True
        i += 1
    if quote:
        return None
    if started:
        words.append("".join(word))
    return words


def _continues(line: str) -> bool:
    """Whether ``line``, as read, ends in a backslash that is not itself escaped: an odd run of them, with nothing
    after it."""

    return (len(line) - len(line.rstrip("\\"))) % 2 == 1


def _names(value: str, folder: str, home: str) -> bool:
    """Whether a unit's ``value`` (``%h`` is ``home``) names ``folder``, links followed."""

    named = home + value[2:] if value.startswith("%h") else value  # no NUL: it ends the line (DeepSeek H9-1, Grok R1)
    return os.path.isabs(named) and os.path.realpath(named) == os.path.realpath(folder)


def service_fields(unit: str) -> dict[str, str]:
    """Map ``Section.Key`` to the last value in ``unit``. Comments are ignored.

    Duplicate keys keep the last value. This is a reader for the small unit this package writes,
    not a systemd parser.
    """

    if type(unit) is not str:
        raise InstallRefused("unit")
    section = ""
    out: dict[str, str] = {}
    for raw in unit.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]") and len(line) > 2:
            section = line[1:-1]
            continue
        key, sep, value = line.partition("=")
        if sep != "=" or not section or not key:
            raise InstallRefused("unit")
        out[f"{section}.{key}"] = value
    return out
