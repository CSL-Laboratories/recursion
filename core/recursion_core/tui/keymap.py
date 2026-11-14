"""The keymap (lane TUI, slice 2): one table of action ids, default bindings by context, a key-sequence
matcher with a leader, and the user's binding file. Toolkit-neutral: key names in, action ids out.

Key names are ``keys.KeyDecoder``'s (``j``, ``enter``, ``ctrl+x``, ``alt+x``, ``shift+tab`` ...). A binding
is a sequence of them, written with spaces (``"ctrl+d ctrl+d"``, ``"g g"``, ``"<leader> k"``); ``<leader>``
stands for the leader key, ``ctrl+x`` unless the binding file says otherwise.

Contexts. ``global`` applies wherever the focused context does not bind the same keys; ``runs`` is the run
list, ``output`` the output pager, ``command`` the ":" and "/" line, ``help`` the help view. A context's own
binding shadows every global binding it overlaps (``output``'s ``ctrl+d`` shadows the global ``ctrl+d ctrl+d``).
The Stop confirmation is not a context of the keymap: ``y`` alone confirms and any other key cancels.

No timers. Within one context no binding may be a prefix of another, so a sequence is complete, still
pending, or not bound, decided by the keys alone. A pending sequence waits for the next key; Esc cancels it.
A key that breaks a leader sequence is consumed (the user meant a leader command); a key that breaks any
other prefix (``ctrl+d`` then ``j``) is read again on its own.

The binding file (TOML, ``version = 1``) overrides the defaults per sequence; the value ``"none"`` removes a
default. A ``[safety]`` table states rules that a binding file can only make stricter: it may add keys to
``never_bind``, and may restate the fixed rules as ``true``, never ``false``. The fixed rules:

- ``ctrl+y`` is bound nowhere and ``ctrl+p`` nowhere globally (no YOLO key, no global palette over
  readline's Ctrl+P);
- no action approves anything: approvals are answered by number and Enter, never by a key binding;
- ``esc`` is bound to nothing that sends a request; Esc cancels local UI and never stops or interrupts a run;
- plugin actions (``plugin.<plugin>.<command>``) are bound only under ``<leader> p``, and only they are;
- the Stop action opens the confirmation; no binding sends ``run.stop`` by itself.

A binding file that cannot be read, or whose ``[safety]`` loosens a rule, is not used at all; one bad binding
in an otherwise good file is skipped. Either way the reason is returned for the app to show.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

LEADER = "ctrl+x"
CONTEXTS = ("global", "runs", "output", "command", "help")
VIEWS = {"runs": ("runs", "global"), "output": ("output", "global"), "command": ("command",), "help": ("help",)}
FILE_VERSION = 1
NONE = "none"
PLUGIN_PREFIX = "plugin."
SAFETY_RULES = ("confirm_stop", "esc_never_sends", "plugins_under_leader_p", "no_key_approves")
NEVER_BIND = frozenset({"ctrl+y"})
NEVER_BIND_GLOBAL = frozenset({"ctrl+p"})

_NAMED = ("enter", "tab", "backspace", "esc", "up", "down", "left", "right", "home", "end", "pageup", "pagedown",
          "insert", "delete", *(f"f{n}" for n in range(1, 13)))
_MODIFIER_SETS = ("ctrl+", "alt+", "shift+", "alt+shift+", "ctrl+shift+", "ctrl+alt+", "ctrl+alt+shift+")
_CTRL = {"ctrl+space", "ctrl+\\", "ctrl+]", "ctrl+^", "ctrl+_", *(f"ctrl+{chr(c)}" for c in range(ord("a"), ord("z") + 1))}


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
)
BY_ID: dict[str, Action] = {a.id: a for a in ACTIONS}

DEFAULTS: dict[str, dict[str, str]] = {
    "global": {"q": "app.quit", "ctrl+d ctrl+d": "app.quit", "<leader> q": "app.quit", "?": "help.show",
               ":": "line.command", "/": "line.command", "<leader> n": "line.start", "<leader> r": "runs.refresh",
               "<leader> k": "run.stop", "<leader> c": "conditions.show"},
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
    "help": {"esc": "help.close", "q": "help.close", "?": "help.close"},
}


def is_key_name(token: str) -> bool:
    """True for a key name ``keys.KeyDecoder`` can produce (``space`` is accepted for " ")."""

    if len(token) == 1:
        return token.isprintable()
    if token in _CTRL or token == "shift+tab":
        return True
    for mods in _MODIFIER_SETS:
        if token.startswith(mods) and token[len(mods):] in _NAMED:
            return True
    if token in _NAMED:
        return True
    if token.startswith("ctrl+alt+") and "ctrl+" + token[9:] in _CTRL:
        return True
    rest = token[4:] if token.startswith("alt+") else ""
    return bool(rest) and (len(rest) == 1 and rest.isprintable() or rest in _NAMED)


def parse_sequence(text: str, leader: str = LEADER) -> tuple[str, ...]:
    """``"<leader> k"`` -> ``("ctrl+x", "k")``. Raises ValueError for an empty or unknown key."""

    out = []
    for token in text.split():
        token = leader if token == "<leader>" else " " if token == "space" else token
        if not is_key_name(token):
            raise ValueError(f"unknown key {token!r}")
        out.append(token)
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
    """Bindings by context, after the defaults, the binding file and the safety rules."""

    leader: str = LEADER
    bindings: dict[str, dict[tuple[str, ...], str]] = field(default_factory=dict)
    never_bind: frozenset[str] = NEVER_BIND
    notes: list[str] = field(default_factory=list)  # why a binding or the file was not used
    pending: tuple[str, ...] = ()

    @classmethod
    def default(cls) -> Keymap:
        km = cls()
        for context, table in DEFAULTS.items():
            for seq_text, action in table.items():
                problem = km.bind(context, seq_text, action)
                if problem:  # the defaults are checked by a test; this cannot happen unless they change
                    raise ValueError(f"default binding {context} {seq_text!r}: {problem}")
        return km

    # -- building --------------------------------------------------------------------------------
    def bind(self, context: str, seq_text: str, action: str) -> str | None:
        """Add one binding; return why it was refused, or None. ``action == "none"`` removes one."""

        if context not in CONTEXTS:
            return f"unknown context {context!r}"
        try:
            seq = parse_sequence(seq_text, self.leader)
        except ValueError as exc:
            return str(exc)
        table = self.bindings.setdefault(context, {})
        if action == NONE:
            table.pop(seq, None)
            return None
        problem = self._refusal(context, seq, action)
        if problem:
            return problem
        clash = next((other for other in table if other != seq and (other[:len(seq)] == seq or seq[:len(other)] == other)), None)
        if clash is not None:
            return f"overlaps {show_sequence(clash, self.leader)} in {context} (no binding may be a prefix of another)"
        table[seq] = action
        return None

    def _refusal(self, context: str, seq: tuple[str, ...], action: str) -> str | None:
        under_p = len(seq) >= 2 and seq[:2] == (self.leader, "p")
        if action.startswith(PLUGIN_PREFIX):
            if not under_p:
                return "plugin actions are bound only under <leader> p"
            if action.count(".") < 2 or not all(action.split(".")[1:]):
                return f"plugin action {action!r} must be plugin.<plugin>.<command>"
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
        if seq == ("esc",) and known.sends:
            return "Esc never sends a request"
        return self._key_refusal(context, seq)

    def _key_refusal(self, context: str, seq: tuple[str, ...]) -> str | None:
        for key in seq:
            if key in self.never_bind:
                return f"{key} is bound nowhere ([safety] never_bind)"
            if context == "global" and key in NEVER_BIND_GLOBAL:
                return f"{key} is not bound globally"
        return None

    # -- matching --------------------------------------------------------------------------------
    def view(self, focus: str) -> dict[tuple[str, ...], str]:
        """The bindings that apply with ``focus``: its own, then every global one it does not overlap."""

        merged: dict[tuple[str, ...], str] = {}
        for context in VIEWS.get(focus, (focus,)):
            for seq, action in self.bindings.get(context, {}).items():
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
            self.pending = ()
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
    """The defaults with a binding file applied. A file that cannot be used leaves the defaults, with the
    reason in ``notes``; a binding that is refused is skipped, with the reason in ``notes``."""

    km = base or Keymap.default()
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        km.notes.append(f"binding file not used: not TOML ({exc})")
        return km
    problem = _file_refusal(doc)
    if problem:
        km.notes.append(f"binding file not used: {problem}")
        return km
    if isinstance(doc.get("leader"), str) and doc["leader"] != km.leader:
        fresh = Keymap(leader=doc["leader"], never_bind=km.never_bind, notes=km.notes)
        for context, table in DEFAULTS.items():
            for seq_text, action in table.items():
                refused = fresh.bind(context, seq_text, action)
                if refused:
                    fresh.notes.append(f"default {context} {seq_text!r} dropped with leader {doc['leader']!r}: {refused}")
        km = fresh
    safety = doc.get("safety", {})
    km.never_bind = km.never_bind | frozenset(safety.get("never_bind", ()))
    for context, table in km.bindings.items():  # a key the file forbids leaves the defaults too
        for seq in [s for s in table if any(k in km.never_bind for k in s)]:
            km.notes.append(f"default {context} {show_sequence(seq, km.leader)} removed: [safety] never_bind")
            del table[seq]
    for context, table in doc.get("bindings", {}).items():
        if not isinstance(table, Mapping):
            km.notes.append(f"bindings.{context} skipped: not a table")
            continue
        for seq_text, action in table.items():
            if not isinstance(action, str):
                km.notes.append(f"bindings.{context} {seq_text!r} skipped: the action is not a string")
                continue
            refused = km.bind(context, seq_text, action)
            if refused:
                km.notes.append(f"bindings.{context} {seq_text!r} = {action!r} skipped: {refused}")
    return km


def _file_refusal(doc: Mapping) -> str | None:
    if doc.get("version") != FILE_VERSION:
        return f"version must be {FILE_VERSION}"
    unknown = sorted(set(doc) - {"version", "leader", "bindings", "safety"})
    if unknown:
        return f"unknown table or key {unknown[0]!r}"
    leader = doc.get("leader", LEADER)
    if not isinstance(leader, str) or not is_key_name(leader) or len(leader) == 1 or leader in ("esc", "enter"):
        return f"leader {leader!r} is not a usable key (a named or modified key other than Esc and Enter)"
    if leader in NEVER_BIND or leader in NEVER_BIND_GLOBAL:
        return f"leader {leader!r} is a key bound nowhere"
    if not isinstance(doc.get("bindings", {}), Mapping):
        return "bindings must be a table of contexts"
    safety = doc.get("safety", {})
    if not isinstance(safety, Mapping):
        return "[safety] must be a table"
    for name, value in safety.items():
        if name == "never_bind":
            if not isinstance(value, list) or not all(isinstance(k, str) and is_key_name(k) for k in value):
                return "[safety] never_bind must be a list of key names"
            if leader in value:
                return "[safety] never_bind holds the leader"
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
