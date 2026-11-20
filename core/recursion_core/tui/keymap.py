"""The keymap (lane TUI, slice 2): one table of action ids, default bindings by context, a key-sequence
matcher with a leader, and the user's binding file. Toolkit-neutral: key names in, action ids out.

Key names are ``keys.KeyDecoder``'s (``j``, ``enter``, ``ctrl+x``, ``alt+x``, ``shift+tab`` ...). A binding
is a sequence of them, written with spaces (``"ctrl+d ctrl+d"``, ``"g g"``, ``"<leader> k"``); ``<leader>``
stands for the leader key, ``ctrl+x`` unless the binding file says otherwise. A name is taken as the key a
terminal sends for it: the decoder reads BS as ``backspace``, HT as ``tab`` and LF and CR as ``enter``, so
``ctrl+h``, ``ctrl+i``, ``ctrl+j`` and ``ctrl+m`` (and their ``ctrl+alt+`` forms) are those keys; a name the
decoder never makes (``ctrl+enter``, ``alt+esc``) is refused.

Contexts. ``runs`` is the run list, ``output`` the output pager, ``command`` the ":" and "/" line, ``help``
the help view. ``global`` applies in ``runs`` and ``output`` wherever they do not bind the same keys: a
context's own binding shadows every global binding it overlaps (``output``'s ``ctrl+d`` shadows the global
``ctrl+d ctrl+d``). ``command`` and ``help`` use their own bindings only, so a typed ``q`` stays text and a
global ``<leader> p`` plugin binding does not reach the command line. The Stop confirmation is not a context
of the keymap: ``y`` alone confirms and any other key cancels.

No timers. Within one context no binding may be a prefix of another, so a sequence is complete, still
pending, or not bound, decided by the keys alone. Binding is not serialised: two threads that bind into one
keymap at once are the caller's to order (each client keeps its own pending sequence, ``for_client``; the
binding tables are shared). A pending sequence waits for the next key; Esc cancels it,
and then does what Esc does where it was pressed (leaves the line, the help or the pager).
A key that breaks a leader sequence is consumed (the user meant a leader command); a key that breaks any
other prefix (``ctrl+d`` then ``j``) is read again on its own.

The binding file (TOML, ``version = 1``) overrides the defaults per sequence; the value ``"none"`` removes
that context's binding, a default or a file's own (in ``runs`` and ``output`` a global binding of the same keys
then applies). A ``[safety]`` table states rules that a binding file can only make stricter: it may add keys to
``never_bind``, and may restate the fixed rules as ``true``, never ``false``. The fixed rules:

- ``ctrl+y`` is bound nowhere and ``ctrl+p`` nowhere globally (no YOLO key, no global palette over
  readline's Ctrl+P);
- ``ctrl+c`` is kept: it ends the TUI from any mode (the terminal is in raw mode, so it arrives as a key),
  and no binding, nor the leader, can take it;
- no action approves anything: approvals are answered by number and Enter, never by a key binding;
- ``esc`` is bound to nothing that sends a request; Esc cancels local UI and never stops or interrupts a run,
  and no binding file takes that cancel away (``[safety] never_bind`` cannot hold Esc);
- plugin actions (``plugin.<plugin>.<command>``) are bound only under ``<leader> p``, and only where a leader
  sequence is typed (``global``, ``runs``, ``output``); only they are bound under ``<leader> p``;
- the Stop action opens the confirmation; no binding sends ``run.stop`` by itself.

A binding file that cannot be read, or whose ``[safety]`` loosens a rule, is not used at all; one bad binding
in an otherwise good file is skipped. Either way the reason is returned for the app to show.
"""

from __future__ import annotations

import copy
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace

from recursion_core.tui.sanitize import visible

LEADER = "ctrl+x"
CONTEXTS = ("global", "runs", "output", "command", "help")
VIEWS = {"runs": ("runs", "global"), "output": ("output", "global"), "command": ("command",), "help": ("help",)}
FILE_VERSION = 1
NONE = "none"
PLUGIN_PREFIX = "plugin."
# A plugin action is plugin.<plugin_id>.<command> in SPINE's names (schemas/control/v1/api.v1.json at #368's
# pin 16dea486: $defs plugin_id and name). A plugin_id may hold dots; a command name holds none, so the last
# dot separates them (review 1 of #404, R8).
PLUGIN_ID = re.compile(r"[a-z][a-z0-9_.-]{1,63}")
PLUGIN_COMMAND = re.compile(r"[a-z][a-z0-9_-]{0,63}")
# Esc only cancels local UI: alone, and only to the cancel of its context (review 1 of #404, R1).
ESC_CANCELS = {"command": "line.cancel", "help": "help.close", "output": "focus.runs"}
SAFETY_RULES = ("confirm_stop", "esc_never_sends", "plugins_under_leader_p", "no_key_approves")
NEVER_BIND = frozenset({"ctrl+y"})
NEVER_BIND_GLOBAL = frozenset({"ctrl+p"})
RESERVED = frozenset({"ctrl+c"})  # the app takes it before the keymap: it ends the TUI from any mode

# Key names as keys.KeyDecoder makes them (review 2 of #404, Astra 4). The four one-byte keys take no modifier
# but Alt (ESC before them), and Esc not even that (ESC ESC is two Escs); shift+tab is a sequence of its own.
# The keys of _NAMED take xterm's modifier parameter.
_SINGLE = ("enter", "tab", "backspace", "esc")
_NAMED = ("up", "down", "left", "right", "home", "end", "pageup", "pagedown", "insert", "delete",
          *(f"f{n}" for n in range(1, 13)))
_MODIFIER_SETS = ("ctrl+", "alt+", "shift+", "alt+shift+", "ctrl+shift+", "ctrl+alt+", "ctrl+alt+shift+")
# Control bytes the decoder reads as another key (keys._plain): BS is backspace, HT tab, LF and CR enter.
_SAME_BYTE = {"h": "backspace", "i": "tab", "j": "enter", "m": "enter"}
_CTRL = {"ctrl+space", "ctrl+\\", "ctrl+]", "ctrl+^", "ctrl+_", *(f"ctrl+{chr(c)}" for c in range(ord("a"), ord("z") + 1))}
ALIASES = {**{f"ctrl+{c}": key for c, key in _SAME_BYTE.items()},
           **{f"ctrl+alt+{c}": f"alt+{key}" for c, key in _SAME_BYTE.items()},
           "alt+space": "alt+ "}  # ESC and a space; a binding cannot write the space (review 3 of #404, DeepSeek F5)


@dataclass(frozen=True, slots=True)
class Action:
    """One thing a key can do. ``plain`` is the ``--plain`` command that does the same, or, for an action
    that only moves the view (the linear renderer prints everything once and names the run in each
    command), a note that starts with ``(``. ``label`` is the word a hint row shows; ``sends`` is true when
    the action itself sends a request."""

    id: str
    contexts: tuple[str, ...]
    label: str
    help: str
    plain: str
    sends: bool = False


ACTIONS: tuple[Action, ...] = (
    Action("app.quit", ("global", "runs"), "close", "close the TUI (runs keep going)", "q"),
    Action("help.show", ("global", "runs", "output"), "keys", "show the keys", "help"),
    Action("line.command", ("global", "runs", "output"), "command", "open the command line", "(--plain reads one command per line)"),
    Action("line.start", ("global", "runs"), "start", "open the command line with start", "start --tenant T --engine E --prompt P"),
    Action("runs.next", ("runs",), "next", "select the next run", "(--plain names the run in each command)"),
    Action("runs.prev", ("runs",), "previous", "select the previous run", "(--plain names the run in each command)"),
    Action("run.attach", ("runs",), "attach", "attach to the selected run (reads run.status)", "status RUN", sends=True),
    Action("runs.refresh", ("runs", "global"), "refresh", "refresh the run list (reads run.list)", "list", sends=True),
    Action("run.stop", ("runs", "global"), "stop", "stop the selected run (asks y first)", "stop RUN"),
    Action("conditions.show", ("runs", "global"), "conditions", "say the current conditions again", "conditions"),
    Action("sessions.show", ("runs", "global"), "sessions", "list the sessions (reads session.list)", "sessions",
           sends=True),
    Action("focus.output", ("runs",), "output", "move to the output pager", "(--plain prints the output once, in order)"),
    Action("focus.runs", ("output",), "runs", "back to the run list", "(--plain has no panes)"),
    Action("pager.down", ("output",), "down", "scroll down one line", "(--plain prints the output once, in order)"),
    Action("pager.up", ("output",), "up", "scroll up one line", "(--plain prints the output once, in order)"),
    Action("pager.half_down", ("output",), "half down", "scroll down half a page", "(--plain prints the output once, in order)"),
    Action("pager.half_up", ("output",), "half up", "scroll up half a page", "(--plain prints the output once, in order)"),
    Action("pager.top", ("output",), "top", "go to the oldest line kept", "(--plain prints the output once, in order)"),
    Action("pager.bottom", ("output",), "bottom", "go to the newest line and follow", "(--plain prints the output once, in order)"),
    Action("pager.search", ("output",), "search", "search the output", "(--plain: the screen reader's own search)"),
    Action("pager.next", ("output",), "next match", "next match", "(--plain: the screen reader's own search)"),
    Action("pager.prev", ("output",), "previous match", "previous match", "(--plain: the screen reader's own search)"),
    Action("line.submit", ("command",), "send", "send the line", "(--plain: Enter ends each command)"),
    Action("line.cancel", ("command",), "leave", "leave the line, send nothing", "(--plain: an empty line sends nothing)"),
    Action("line.left", ("command",), "left", "cursor left", "(--plain: the terminal edits the line)"),
    Action("line.right", ("command",), "right", "cursor right", "(--plain: the terminal edits the line)"),
    Action("line.home", ("command",), "start of line", "cursor to the start", "(--plain: the terminal edits the line)"),
    Action("line.end", ("command",), "end of line", "cursor to the end", "(--plain: the terminal edits the line)"),
    Action("line.backspace", ("command",), "delete back", "delete before the cursor", "(--plain: the terminal edits the line)"),
    Action("line.delete", ("command",), "delete", "delete at the cursor", "(--plain: the terminal edits the line)"),
    Action("help.close", ("help",), "close", "close the keys", "(--plain: help prints once)"),
    Action("help.down", ("help",), "down", "scroll the keys down a line", "(--plain: help prints once)"),
    Action("help.up", ("help",), "up", "scroll the keys up a line", "(--plain: help prints once)"),
    Action("help.page_down", ("help",), "page down", "scroll the keys down a page", "(--plain: help prints once)"),
    Action("help.page_up", ("help",), "page up", "scroll the keys up a page", "(--plain: help prints once)"),
    Action("help.top", ("help",), "top", "the first of the keys", "(--plain: help prints once)"),
    Action("help.bottom", ("help",), "bottom", "the last of the keys", "(--plain: help prints once)"),
)
BY_ID: dict[str, Action] = {a.id: a for a in ACTIONS}

DEFAULTS: dict[str, dict[str, str]] = {
    "global": {"q": "app.quit", "ctrl+d ctrl+d": "app.quit", "<leader> q": "app.quit", "?": "help.show",
               ":": "line.command", "/": "line.command", "<leader> n": "line.start", "<leader> r": "runs.refresh",
               "<leader> k": "run.stop", "<leader> c": "conditions.show", "<leader> s": "sessions.show"},
    "runs": {"j": "runs.next", "down": "runs.next", "k": "runs.prev", "up": "runs.prev", "a": "run.attach",
             "enter": "run.attach", "r": "runs.refresh", "s": "run.stop", "tab": "focus.output"},
    "output": {"j": "pager.down", "down": "pager.down", "k": "pager.up", "up": "pager.up",
               "ctrl+d": "pager.half_down", "ctrl+u": "pager.half_up", "pagedown": "pager.half_down",
               "pageup": "pager.half_up", "g g": "pager.top", "home": "pager.top", "G": "pager.bottom",
               "end": "pager.bottom", "/": "pager.search", "n": "pager.next", "N": "pager.prev",
               "tab": "focus.runs", "esc": "focus.runs", "q": "focus.runs"},
    "command": {"enter": "line.submit", "esc": "line.cancel", "left": "line.left", "right": "line.right",
                "home": "line.home", "ctrl+a": "line.home", "end": "line.end", "ctrl+e": "line.end",
                "backspace": "line.backspace", "delete": "line.delete"},
    "help": {"esc": "help.close", "q": "help.close", "?": "help.close", "j": "help.down", "down": "help.down",
             "k": "help.up", "up": "help.up", "pagedown": "help.page_down", "ctrl+d": "help.page_down",
             "pageup": "help.page_up", "ctrl+u": "help.page_up", "g g": "help.top", "home": "help.top",
             "G": "help.bottom", "end": "help.bottom"},
}
# Every key a default binding uses where the global bindings apply; none may be the leader, or that binding
# would shadow the leader's sequences there (review 1 of #404, M3). The command line and the help never see
# a leader sequence, so their keys (Ctrl+A on the line) may be the leader.
LEADER_CONTEXTS = ("global", "runs", "output")  # where a leader sequence can be typed
DEFAULT_KEYS = frozenset(key for context in LEADER_CONTEXTS for seq in DEFAULTS[context]
                         for key in seq.split() if key != "<leader>")


def _sendable(ch: str) -> bool:
    """One character a terminal can send and the decoder gives as a key: UTF-8 (a lone surrogate is neither sent
    nor read; DeepSeek's round 6 of #404, F4) and visible."""

    try:
        ch.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return visible(ch)


def key_name(token: str) -> str | None:
    """The name ``keys.KeyDecoder`` gives the key that ``token`` names (``space`` is " ", ``ctrl+i`` is
    ``tab``), or None when no key a terminal sends is read as it (review 2 of #404, Astra 4)."""

    if not isinstance(token, str):
        return None
    token = " " if token == "space" else ALIASES.get(token, token)
    if len(token) == 1:
        return token if _sendable(token) else None  # what the decoder gives (Devin's thread on #404: U+034F)
    if token in _CTRL or token in _SINGLE or token in _NAMED or token == "shift+tab":
        return token
    if any(token.startswith(mods) and token[len(mods):] in _NAMED for mods in _MODIFIER_SETS):
        return token
    if token.startswith("ctrl+alt+") and "ctrl+" + token[9:] in _CTRL:
        return token
    rest = token[4:] if token.startswith("alt+") else ""
    return token if len(rest) == 1 and _sendable(rest) or rest in _SINGLE[:3] else None  # DeepSeek r5, r6 of #404


def is_key_name(token: str) -> bool:
    """True for a key name ``keys.KeyDecoder`` can produce, or one it reads as another key (``ctrl+i``)."""

    return key_name(token) is not None


def parse_sequence(text: str, leader: str = LEADER) -> tuple[str, ...]:
    """``"<leader> k"`` -> ``("ctrl+x", "k")``. Raises ValueError for an empty or unknown key."""

    out = []
    for token in text.split():
        name = leader if token == "<leader>" else key_name(token)
        if name is None:
            raise ValueError(f"unknown key {token!r}")
        out.append(name)
    if not out:
        raise ValueError("empty key sequence")
    return tuple(out)


def show_sequence(seq: Iterable[str], leader: str = LEADER) -> str:
    """How a sequence is written in help and hints: ``Ctrl+X k``."""

    def one(key: str) -> str:
        if key == " ":
            return "Space"
        if len(key) == 1:
            return key
        mods, _, base = key.rpartition("+") if not key.endswith("++") else (key[:-2], "+", "+")
        if base == " ":
            base = "Space"  # alt+ is written alt+space, and shown Alt+Space
        if len(base) > 1:
            base = {"pageup": "PageUp", "pagedown": "PageDown"}.get(base, base.capitalize())
        elif mods.startswith("ctrl") and base.isalpha():
            base = base.upper()  # Ctrl+X: a control key has no case
        return "+".join(m.capitalize() for m in mods.split("+") if m) + ("+" if mods else "") + base

    return " ".join(one(k) for k in seq)


@dataclass(frozen=True, slots=True)
class Pending:
    """A sequence that is a prefix of a binding: the next key decides."""

    keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Unbound:
    """A leader sequence that names nothing; the key that broke it is consumed."""

    keys: tuple[str, ...]


@dataclass
class Keymap:
    """Bindings by context, after the defaults, the binding file and the safety rules, and one client's
    matching state (``pending``, ``last``). That state belongs to one client's keys: a client takes its own
    with ``for_client()``, so several clients can share one set of bindings. ``leader`` and ``never_bind`` hold
    keys as a terminal sends them: a name given for one (``ctrl+i``) is kept as that key (``tab``)."""

    leader: str = LEADER
    bindings: dict[str, dict[tuple[str, ...], str]] = field(default_factory=dict)
    never_bind: frozenset[str] = NEVER_BIND
    notes: list[str] = field(default_factory=list)  # why a binding or the file was not used
    pending: tuple[str, ...] = ()
    last: tuple[str, ...] = ()  # the sequence that chose the last action ``feed`` returned

    def __post_init__(self) -> None:
        # However a consumer spelled them (review 3 of #404, DeepSeek F4; review 4, N2 for the leader).
        self.leader = key_name(self.leader) or self.leader
        self.never_bind = frozenset(key_name(k) or k for k in self.never_bind)

    @classmethod
    def default(cls) -> Keymap:
        km = cls()
        for context, table in DEFAULTS.items():
            for seq_text, action in table.items():
                problem = km.bind(context, seq_text, action)
                if problem:  # the defaults are checked by a test; this cannot happen unless they change
                    raise ValueError(f"default binding {context} {seq_text!r}: {problem}")
        return km

    def for_client(self) -> Keymap:
        """The same bindings (shared, not copied) with matching state of their own (review 1 of #404, M1)."""

        return replace(self, pending=(), last=())

    # -- building --------------------------------------------------------------------------------
    def bind(self, context: str, seq_text: str, action: str) -> str | None:
        """Add one binding; return why it was refused, or None. ``action == "none"`` removes one."""

        if context not in CONTEXTS:
            return f"unknown context {context!r}"
        if not isinstance(seq_text, str) or not isinstance(action, str):  # review 2 of #404, DeepSeek L3
            return (f"a key sequence and an action are text, not {type(seq_text).__name__} and "
                    f"{type(action).__name__}")
        try:
            seq = parse_sequence(seq_text, self.leader)
        except ValueError as exc:
            return str(exc)
        return self._bind(context, seq, action)

    def _bind(self, context: str, seq: tuple[str, ...], action: str) -> str | None:
        table = self.bindings.setdefault(context, {})
        if action == NONE:
            if seq == ("esc",) and context in ESC_CANCELS:
                return "Esc always cancels here; it cannot be removed"
            table.pop(seq, None)
            return None
        problem = self._refusal(context, seq, action)
        if problem:
            return problem
        clash = next((other for other in table.copy() if other != seq and (other[:len(seq)] == seq or seq[:len(other)] == other)), None)
        if clash is not None:
            return f"overlaps {show_sequence(clash, self.leader)} in {context} (no binding may be a prefix of another)"
        table[seq] = action
        return None

    def _refusal(self, context: str, seq: tuple[str, ...], action: str) -> str | None:
        if seq == (self.leader,) and context in LEADER_CONTEXTS:  # alone it would shadow the leader (#404, M3)
            return "the leader is not bound alone; it starts the leader's sequences"
        if context == "command" and len(seq[0]) == 1:  # a typed key would be lost from the line (Devin, #404)
            return f"{show_sequence(seq[:1], self.leader)} types text on the command line; no binding there begins with it"
        if "esc" in seq and (seq != ("esc",) or ESC_CANCELS.get(context) != action):  # review 1 of #404, R1
            return ("Esc never sends a request or arms anything: it is bound only alone, to the cancel of its "
                    "context (line.cancel, help.close, focus.runs)")
        under_p = len(seq) >= 2 and seq[:2] == (self.leader, "p")
        if action.startswith(PLUGIN_PREFIX):
            if context not in LEADER_CONTEXTS:  # the line and the keys view never see a leader sequence (Devin, #404)
                return f"plugin actions are bound only where a leader sequence is typed ({', '.join(LEADER_CONTEXTS)})"
            if not under_p:
                return "plugin actions are bound only under <leader> p"
            plugin_id, _, command = action[len(PLUGIN_PREFIX):].rpartition(".")
            if not (PLUGIN_ID.fullmatch(plugin_id) and plugin_id != "host" and PLUGIN_COMMAND.fullmatch(command)):
                return (f"plugin action {action!r} must be plugin.<plugin>.<command> in SPINE's names "
                        f"(api.v1 plugin_id and name)")
            return self._key_refusal(context, seq)
        if under_p:
            return "<leader> p is kept for plugin actions"
        if action.startswith("approval."):
            return "no key approves anything: approvals are answered by number and Enter"
        known = BY_ID.get(action)
        if known is None:
            return f"unknown action {action!r}"
        if context not in known.contexts:
            return f"{action} is not available in {context}"
        return self._key_refusal(context, seq)

    def _key_refusal(self, context: str, seq: tuple[str, ...]) -> str | None:
        for key in seq:
            if key in RESERVED:
                return f"{key} is kept: it ends the TUI from any mode"
            if key in self.never_bind:  # "alt+ " is said as Alt+Space (review 4 of #404, DeepSeek N3)
                return f"{show_sequence((key,)) if ' ' in key else key} is bound nowhere ([safety] never_bind)"
            if context == "global" and key in NEVER_BIND_GLOBAL:
                return f"{key} is not bound globally"
        return None

    # -- matching --------------------------------------------------------------------------------
    def view(self, focus: str) -> dict[tuple[str, ...], str]:
        """The bindings that apply with ``focus``: its own, then, in ``runs`` and ``output``, every global one
        it does not overlap (``command`` and ``help`` have their own only). It reads a copy of each table, so
        a binding another client adds meanwhile cannot break it (review 2 of #404, DeepSeek L5)."""

        merged: dict[tuple[str, ...], str] = {}
        for context in VIEWS.get(focus, (focus,)):
            for seq, action in self.bindings.get(context, {}).copy().items():
                if any(seq[:len(s)] == s or s[:len(seq)] == seq for s in merged):
                    continue  # shadowed by the focused context
                merged[seq] = action
        return merged

    def feed(self, key: str, focus: str) -> str | Pending | Unbound | None:
        """One key in ``focus``: an action id, ``Pending``, ``Unbound`` (a broken leader sequence), or None
        when nothing is bound to it (the caller may still use the key, for example as typed text)."""

        table = self.view(focus)
        seq = self.pending + (key,)
        if key == "esc" and self.pending:
            self.pending = ()
            return Unbound(seq)
        if seq in table:
            self.pending, self.last = (), seq
            return table[seq]
        if any(len(s) > len(seq) and s[:len(seq)] == seq for s in table):
            self.pending = seq
            return Pending(seq)
        if self.pending:
            broken, self.pending = self.pending, ()
            if broken[0] == self.leader:
                return Unbound(seq)
            return self.feed(key, focus)
        return None

    def continuations(self, focus: str) -> list[tuple[str, str]]:
        """While a sequence is pending: each next key with the action it leads to (or "..." for a longer one)."""

        out: dict[str, str] = {}
        n = len(self.pending)
        for seq, action in self.view(focus).items():
            if len(seq) > n and seq[:n] == self.pending:
                out.setdefault(seq[n], action if len(seq) == n + 1 else "...")
        return sorted(out.items())

    def keys_for(self, action: str, focus: str) -> list[str]:
        return sorted(show_sequence(s, self.leader) for s, a in self.view(focus).items() if a == action)


def load(text: str, base: Keymap | None = None) -> Keymap:
    """The defaults (or a copy of ``base``, which is left unchanged) with a binding file applied. A file that
    cannot be used leaves the defaults, with the reason in ``notes``; a binding that is refused is skipped,
    with the reason in ``notes``."""

    km = copy.deepcopy(base) if base is not None else Keymap.default()
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        km.notes.append(f"binding file not used: not TOML ({exc})")
        return km
    problem = _file_refusal(doc, km.leader)
    if problem:
        km.notes.append(f"binding file not used: {problem}")
        return km
    leader = key_name(doc.get("leader", km.leader))  # as a terminal sends it: ctrl+i is tab (Astra 4)
    held = {key for context in LEADER_CONTEXTS for seq in km.bindings.get(context, {})
            for key in (seq[1:] if seq[0] == km.leader else seq)}
    if leader != km.leader and leader in held:  # a binding in force would shadow the leader's sequences
        km.notes.append(f"binding file not used: leader {doc.get('leader', km.leader)!r} is a key the bindings in force use "
                        f"where a leader is typed; choose one they do not (review 3 of #404, DeepSeek F3)")
        return km
    if leader != km.leader:
        # Every binding moves with the leader, base's as well as the defaults': a sequence that starts with the
        # old leader where a leader can be typed starts with the new one; the rest stay (review 2 of #404,
        # DeepSeek L1).
        old, fresh = km.leader, Keymap(leader=leader, never_bind=km.never_bind, notes=km.notes)
        for context, table in km.bindings.items():
            for seq, action in table.items():
                moved = (leader, *seq[1:]) if context in LEADER_CONTEXTS and seq[0] == old else seq
                refused = fresh._bind(context, moved, action)
                if refused:
                    fresh.notes.append(f"{context} {show_sequence(seq)} dropped with leader "
                                       f"{show_sequence((leader,))}: {refused}")
        km = fresh
    safety = doc.get("safety", {})
    km.never_bind = km.never_bind | frozenset(key_name(k) for k in safety.get("never_bind", ()))
    for context, table in km.bindings.items():  # a key the file forbids leaves the defaults too
        for seq in [s for s in table if any(k in km.never_bind for k in s)]:
            km.notes.append(f"default {context} {show_sequence(seq, km.leader)} removed: [safety] never_bind")
            del table[seq]
    for context, table in doc.get("bindings", {}).items():
        if not isinstance(table, Mapping):
            km.notes.append(f"bindings.{context} skipped: not a table")
            continue
        given: dict[tuple[str, ...], str] = {}  # this table's keys as a terminal sends them, and how each was written
        for seq_text, action in table.items():
            if not isinstance(action, str):
                km.notes.append(f"bindings.{context} {seq_text!r} skipped: the action is not a string")
                continue
            try:
                seq = parse_sequence(seq_text, km.leader)
            except ValueError:
                seq = None  # bind() says why
            if seq in given:  # two spellings of one key (ctrl+i and tab): the second is said, not taken
                km.notes.append(f"bindings.{context} {seq_text!r} = {action!r} skipped: the same keys as "
                                f"{given[seq]!r} above (review 3 of #404, DeepSeek F3)")
                continue
            refused = km.bind(context, seq_text, action)
            if refused:
                km.notes.append(f"bindings.{context} {seq_text!r} = {action!r} skipped: {refused}")
            elif seq is not None:
                given[seq] = seq_text
    return km


def _file_refusal(doc: Mapping, current: str = LEADER) -> str | None:
    if type(doc.get("version")) is not int or doc["version"] != FILE_VERSION:  # true and 1.0 are not 1
        return f"version must be {FILE_VERSION}"
    unknown = sorted(set(doc) - {"version", "leader", "bindings", "safety"})
    if unknown:
        return f"unknown table or key {unknown[0]!r}"
    written = doc.get("leader", current)
    # Judged as the key a terminal sends: ctrl+i arrives as tab, ctrl+j and ctrl+m as enter (review 2 of #404,
    # Astra 4), so a leader that only looks free is checked as the key it is.
    leader = key_name(written)
    said = repr(written) if leader in (None, written) else f"{written!r} (a terminal sends it as {leader!r})"
    if leader is None or len(leader) == 1 or leader in ("esc", "enter"):
        return f"leader {said} is not a usable key (a named or modified key other than Esc and Enter)"
    if leader in NEVER_BIND or leader in NEVER_BIND_GLOBAL:
        return f"leader {said} is a key bound nowhere"
    if leader in RESERVED:
        return f"leader {said} is kept: it ends the TUI from any mode"
    if leader in DEFAULT_KEYS:  # a context's own binding of it would shadow the leader's sequences there
        return f"leader {said} is a key the default bindings use; choose one they do not"
    if not isinstance(doc.get("bindings", {}), Mapping):
        return "bindings must be a table of contexts"
    safety = doc.get("safety", {})
    if not isinstance(safety, Mapping):
        return "[safety] must be a table"
    for name, value in safety.items():
        if name == "never_bind":
            if not isinstance(value, list) or not all(isinstance(k, str) and is_key_name(k) for k in value):
                return "[safety] never_bind must be a list of key names"
            keys = {key_name(k) for k in value}
            if leader in keys:
                return "[safety] never_bind holds the leader"
            if "esc" in keys:  # review 2 of #404, Astra 1 and DeepSeek M1
                return ("[safety] never_bind cannot hold esc: Esc always cancels the line, the keys and the "
                        "pager, and sends nothing")
        elif name in SAFETY_RULES:
            if value is not True:
                return f"[safety] {name} can only be true (a binding file may make the rules stricter, not looser)"
        else:
            return f"[safety] {name!r} is not a rule"
    return None


def default_toml() -> str:
    """The default binding file, as a user would copy it to start from (``recursion-tui --print-keys``)."""

    def q(text: str) -> str:
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines = ["# Recursion TUI key bindings. Key names are the TUI's own (j, enter, ctrl+x, alt+x, shift+tab, f1).",
             "# A sequence is written with spaces; <leader> is the leader key. \"none\" removes a binding.",
             f"version = {FILE_VERSION}", f"leader = {q(LEADER)}", ""]
    for context in CONTEXTS:
        lines.append(f"[bindings.{context}]")
        for seq_text, action in DEFAULTS[context].items():
            lines.append(f"{q(seq_text)} = {q(action)}")
        lines.append("")
    lines += ["# Plugin commands are bound only under <leader> p, for example:",
              "# [bindings.global]", '# "<leader> p d" = "plugin.example.deploy"', "",
              "[safety]", "# These rules hold whatever this file says; it may only restate them or add keys.",
              "never_bind = []", *(f"{rule} = true" for rule in SAFETY_RULES), ""]
    return "\n".join(lines)
