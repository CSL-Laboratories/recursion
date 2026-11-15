"""Lane TUI slice 2, first part: the keymap layer and the binding file (TUI brief v3.3, build step 2).

The keymap is one table of action ids with default bindings by context, a leader (Ctrl+X) with a hint row,
sequences decided by keys alone (no timers), and a TOML binding file whose [safety] table can only make the
rules stricter. In the app: ? shows the keys, / opens the command line like ":", Ctrl+D twice closes, Esc
cancels local UI and never sends, the output pane is a pager (j/k, Ctrl+D/U, g g/G, / n N). Every key that
sends has a parity test against its CLI twin's request, and every key has its --plain command (or the reason
the linear renderer needs none).
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest

from recursion_core.control import cli
from recursion_core.tui import keymap as km_mod
from recursion_core.tui import term
from recursion_core.tui.app import TuiApp
from recursion_core.tui.keymap import ACTIONS, BY_ID, Keymap, Pending, Unbound, load, parse_sequence
from recursion_core.tui.keys import Paste
from recursion_core.tui.tests.fake_daemon import exchange
from recursion_core.tui.tests.fakes import Clock, FakeDriver, RecordingAPI, ScriptedAPI
from recursion_core.tui.tests.test_inline import FakeTerminal, TtyOut

RUNS = [{"run_id": "run-1", "tenant": "t1", "engine": "codex", "state": "running"},
        {"run_id": "run-2", "tenant": "t1", "engine": "codex", "state": "running"}]
ANSWERS = {"run.list": {"code": "ok", "runs": RUNS}, "run.status": {"code": "ok"}, "run.stop": {"code": "stopped"},
           "run.start": {"code": "started"}}


def new_app(keymap: Keymap | None = None) -> tuple[TuiApp, ScriptedAPI]:
    api = ScriptedAPI(dict(ANSWERS))
    app = TuiApp(api, clock=Clock(), environ={"NO_COLOR": "1"}, keymap=keymap)
    app.open()
    api.calls.clear()
    return app, api


def press_all(app: TuiApp, keys) -> None:
    for key in keys:
        app.press(key)


def cli_requests(command: str) -> list:
    api = ScriptedAPI({})
    cli.main(command.split(), api, io.StringIO(), io.StringIO())
    return api.calls


def twin_requests(action: str) -> list:
    """What the action's CLI twin sends. The session verbs' twins belong to the verb lanes and do not exist
    yet; until they do, their twin is SPINE's recorded request (schemas/control/v1/fixtures/good)."""

    if action == "sessions.show":
        frame = exchange("h27-session-list-ok")
        return [(frame["method"], frame["params"])]
    return cli_requests(plain_command(action))


def plain_command(action: str) -> str:
    return (BY_ID[action].plain.replace("RUN", "run-1").replace(" T ", " t1 ").replace(" E ", " codex ")
            .replace(" P", " hi"))


class DefaultsTest(unittest.TestCase):
    def test_every_action_has_a_default_binding_and_every_binding_a_known_action(self) -> None:
        km = Keymap.default()
        bound = {a for table in km.bindings.values() for a in table.values()}
        self.assertEqual(bound, set(BY_ID))
        self.assertEqual(len(BY_ID), len(ACTIONS), "action ids are unique")

    def test_no_binding_is_a_prefix_of_another_in_its_context(self) -> None:
        for context, table in Keymap.default().bindings.items():
            seqs = list(table)
            for a in seqs:
                for b in seqs:
                    with self.subTest(context=context, a=a, b=b):
                        self.assertFalse(a != b and b[:len(a)] == a)

    def test_the_fixed_safety_rules_hold_for_the_defaults(self) -> None:
        km = Keymap.default()
        for context, table in km.bindings.items():
            for seq, action in table.items():
                with self.subTest(context=context, seq=seq):
                    self.assertNotIn("ctrl+y", seq)
                    if context == "global":
                        self.assertNotIn("ctrl+p", seq)
                    self.assertFalse(action.startswith("approval."))
                    if seq == ("esc",):
                        self.assertFalse(BY_ID[action].sends, "Esc never sends")
        self.assertEqual(km.view("runs")[("esc",)] if ("esc",) in km.view("runs") else None, None,
                         "Esc on the run list does nothing")

    def test_the_default_file_reads_back_to_the_defaults(self) -> None:
        again = load(km_mod.default_toml())
        self.assertEqual(again.bindings, Keymap.default().bindings)
        self.assertEqual(again.notes, [])

    def test_key_names_and_sequences(self) -> None:
        for good in ("j", "G", ":", "?", " ", "enter", "esc", "tab", "shift+tab", "ctrl+x", "ctrl+d", "ctrl+space",
                     "alt+x", "alt+enter", "ctrl+alt+d", "ctrl+up", "ctrl+shift+down", "f12", "pagedown", "delete"):
            with self.subTest(good=good):
                self.assertTrue(km_mod.is_key_name(good))
        for bad in ("", "enterr", "ctrl+shift+a", "ctrl+1", "hyper+x", "shift+a", "f13", "ctrl+"):
            with self.subTest(bad=bad):
                self.assertFalse(km_mod.is_key_name(bad))
        self.assertEqual(parse_sequence("<leader> k"), ("ctrl+x", "k"))
        self.assertEqual(parse_sequence("g space", leader="ctrl+a"), ("g", " "))
        with self.assertRaises(ValueError):
            parse_sequence("  ")
        self.assertEqual(km_mod.show_sequence(("ctrl+x", "k")), "Ctrl+X k")
        self.assertEqual(km_mod.show_sequence(("pagedown", "alt+X", " ", "+")), "PageDown Alt+X Space +")


class MatcherTest(unittest.TestCase):
    def test_a_leader_sequence_waits_for_its_next_key(self) -> None:
        km = Keymap.default()
        self.assertEqual(km.feed("ctrl+x", "runs"), Pending(("ctrl+x",)))
        self.assertEqual(dict(km.continuations("runs"))["k"], "run.stop")
        self.assertEqual(km.feed("k", "runs"), "run.stop")
        self.assertEqual(km.pending, ())

    def test_a_broken_leader_sequence_is_consumed_and_esc_cancels(self) -> None:
        km = Keymap.default()
        km.feed("ctrl+x", "runs")
        self.assertEqual(km.feed("j", "runs"), Unbound(("ctrl+x", "j")), "j is not read as selection")
        km.feed("ctrl+x", "runs")
        self.assertEqual(km.feed("esc", "runs"), Unbound(("ctrl+x", "esc")))
        self.assertEqual(km.pending, ())
        km.feed("g", "output")
        self.assertEqual(km.feed("esc", "output"), Unbound(("g", "esc")), "Esc cancels g; it does not leave the pager")

    def test_a_broken_ordinary_prefix_reads_the_key_again(self) -> None:
        km = Keymap.default()
        self.assertEqual(km.feed("ctrl+d", "runs"), Pending(("ctrl+d",)))
        self.assertEqual(km.feed("j", "runs"), "runs.next")
        self.assertEqual(km.feed("ctrl+d", "runs"), Pending(("ctrl+d",)))
        self.assertEqual(km.feed("ctrl+d", "runs"), "app.quit")
        self.assertEqual(km.feed("g", "output"), Pending(("g",)))
        self.assertEqual(km.feed("g", "output"), "pager.top")

    def test_the_focused_context_shadows_the_global_bindings_it_overlaps(self) -> None:
        km = Keymap.default()
        self.assertEqual(km.feed("ctrl+d", "output"), "pager.half_down")
        self.assertEqual(km.feed("q", "output"), "focus.runs")
        self.assertEqual(km.feed("/", "output"), "pager.search")
        self.assertEqual(km.feed("q", "runs"), "app.quit")
        self.assertEqual(km.feed("/", "runs"), "line.command")
        self.assertIsNone(km.feed("x", "runs"))
        self.assertIsNone(km.feed("j", "command"), "typed text is not a binding")


class BindingFileTest(unittest.TestCase):
    def test_bindings_override_add_and_remove(self) -> None:
        km = load('version = 1\n[bindings.runs]\n"x" = "runs.refresh"\n"r" = "none"\n'
                  '[bindings.global]\n"<leader> p d" = "plugin.example.deploy"\n')
        self.assertEqual(km.notes, [])
        self.assertEqual(km.feed("x", "runs"), "runs.refresh")
        self.assertIsNone(km.feed("r", "runs"))
        km.feed("ctrl+x", "runs")
        km.feed("p", "runs")
        self.assertEqual(km.feed("d", "runs"), "plugin.example.deploy")

    def test_refused_bindings_are_skipped_with_the_reason(self) -> None:
        cases = {
            '"x" = "no.such.action"': "unknown action",
            '"ctrl+y" = "runs.refresh"': "ctrl+y is bound nowhere",
            '"x" = "plugin.example.deploy"': "only under <leader> p",
            '"<leader> p x" = "runs.refresh"': "kept for plugin actions",
            '"<leader> p x" = "plugin.bad"': "must be plugin.<plugin>.<command>",
            '"1" = "approval.approve"': "no key approves anything",
            '"esc" = "run.attach"': "Esc never sends a request",
            '"g" = "runs.next"\n"g g" = "runs.prev"': "no binding may be a prefix of another",
            '"x" = "pager.top"': "not available in runs",
            '"hyper+x" = "runs.next"': "unknown key",
        }
        for body, reason in cases.items():
            with self.subTest(body=body):
                km = load(f"version = 1\n[bindings.runs]\n{body}\n")
                self.assertTrue(any(reason in n for n in km.notes), km.notes)
                self.assertEqual(km.view("runs")[("j",)], "runs.next", "the rest still works")
        km = load('version = 1\n[bindings.global]\n"ctrl+p" = "help.show"\n[bindings.runs]\n"ctrl+p" = "runs.prev"\n')
        self.assertEqual(len(km.notes), 1)
        self.assertIn("ctrl+p is not bound globally", km.notes[0])
        self.assertEqual(km.feed("ctrl+p", "runs"), "runs.prev", "Ctrl+P may be bound in a context")

    def test_a_file_that_loosens_safety_or_cannot_be_read_is_not_used(self) -> None:
        cases = {
            'version = 1\n[safety]\nconfirm_stop = false\n': "can only be true",
            'version = 1\n[safety]\nallow_yolo = true\n': "is not a rule",
            'version = 1\n[safety]\nnever_bind = ["ctrl+x"]\n': "never_bind holds the leader",
            'version = 2\n': "version must be 1",
            'version = 1\nleader = "q"\n': "not a usable key",
            'version = 1\nleader = "ctrl+y"\n': "bound nowhere",
            'version = 1\ntheme = "dark"\n': "unknown table or key",
            'version = 1\n[bindings\n': "not TOML",
        }
        for text, reason in cases.items():
            with self.subTest(text=text):
                km = load(text + '[bindings.runs]\n"x" = "runs.refresh"\n' if "[bindings" not in text else text)
                self.assertTrue(any("binding file not used" in n and reason in n for n in km.notes), km.notes)
                self.assertEqual(km.bindings, Keymap.default().bindings, "the defaults stand")

    def test_safety_can_add_keys_and_restate_rules(self) -> None:
        km = load('version = 1\n[safety]\nnever_bind = ["s"]\nconfirm_stop = true\n[bindings.runs]\n"s" = "run.stop"\n')
        self.assertIsNone(km.feed("s", "runs"), "the default s is removed")
        self.assertTrue(any("removed: [safety] never_bind" in n for n in km.notes))
        self.assertTrue(any("s is bound nowhere" in n for n in km.notes))
        km.feed("ctrl+x", "runs")
        self.assertEqual(km.feed("k", "runs"), "run.stop", "Stop is still reachable, still with its confirmation")

    def test_a_new_leader_moves_every_leader_binding(self) -> None:
        km = load('version = 1\nleader = "ctrl+a"\n')
        self.assertEqual(km.notes, [])
        self.assertEqual(km.feed("ctrl+x", "runs"), None)
        km.feed("ctrl+a", "runs")
        self.assertEqual(km.feed("k", "runs"), "run.stop")
        self.assertEqual(km.view("command")[("ctrl+a",)], "line.home", "the command line keeps its own Ctrl+A")


class AppKeysTest(unittest.TestCase):
    def test_every_default_key_sends_what_its_cli_twin_sends_or_nothing(self) -> None:
        # Parity (brief step 2): each binding that sends goes through the CLI's command; Stop asks y first.
        for focus in ("runs", "output"):
            for seq, action in Keymap.default().view(focus).items():
                with self.subTest(focus=focus, seq=seq, action=action):
                    app, api = new_app()
                    if focus == "output":
                        app.press("tab")
                    press_all(app, seq)
                    if app.mode == "confirm_stop":
                        app.press("y")
                    if BY_ID[action].sends or action == "run.stop":
                        self.assertEqual(api.calls, twin_requests(action))
                    else:
                        self.assertEqual(api.calls, [], f"{action} sends nothing")

    def test_every_key_has_its_plain_command_or_the_reason_it_needs_none(self) -> None:
        for action in ACTIONS:
            with self.subTest(action=action.id):
                if action.plain.startswith("("):
                    self.assertFalse(action.sends, "an action that sends always has a --plain command")
                    continue
                key_app, key_api = new_app()
                seq = next(s for s, a in Keymap.default().view("runs").items() if a == action.id)
                press_all(key_app, seq)
                if key_app.mode == "confirm_stop":
                    key_app.press("y")
                if key_app.mode == "command":  # line.start: the plain command is the whole line
                    press_all(key_app, "--tenant t1 --engine codex --prompt hi")
                    key_app.press("enter")
                api = ScriptedAPI(dict(ANSWERS))
                out = io.StringIO()
                term.plain(api, io.StringIO(plain_command(action.id) + "\n"), out, {}, Clock())
                self.assertEqual(api.calls[1:], key_api.calls, "the same requests, after the open's run.list")
                if action.id == "help.show":
                    self.assertIn("Ctrl+X k, s: stop the selected run (asks y first)", out.getvalue())
                if action.id == "app.quit":
                    self.assertTrue(key_app.closed)
                    self.assertIn("closed: no request sent", out.getvalue())

    def test_slash_commands_match_the_cli(self) -> None:
        for line, twin in (("/stop run-1", "stop run-1"), ("/status run-1", "status run-1"), ("/list", "list"),
                           ("/runs", "list"), ("runs", "list"), ("/conditions", "conditions"),
                           ("/start --tenant t1 --engine codex --prompt hi", "start --tenant t1 --engine codex --prompt hi")):
            with self.subTest(line=line):
                app, api = new_app()
                app.command(line)
                want = [] if twin == "conditions" else cli_requests(twin)
                self.assertEqual(api.calls, want)
        app, api = new_app()
        press_all(app, ["/", *"stop run-2", "enter"])
        self.assertEqual(api.calls, cli_requests("stop run-2"))
        for local in ("/help", "help", "keys", "/keys"):
            with self.subTest(local=local):
                app, api = new_app()
                app.command(local)
                self.assertEqual((api.calls, app.mode), ([], "help"))
        app, api = new_app()
        app.command("/quit")
        self.assertEqual((api.calls, app.closed), ([], True))

    def test_the_leader_hint_row_and_its_cancel(self) -> None:
        app, api = new_app()
        app.press("ctrl+x")
        prompt = app.prompt()
        for part in ("Ctrl+X ...", "k stop", "q close", "n start", "r refresh", "c conditions", "Esc cancels"):
            self.assertIn(part, prompt)
        app.press("esc")
        self.assertIn("Ctrl+X cancelled; nothing done", app.notice)
        app.press("ctrl+x")
        app.press("z")
        self.assertIn("Ctrl+X z is not bound; nothing done", app.notice)
        self.assertEqual(api.calls, [])
        self.assertTrue(app.prompt().startswith("recursion: j/k select"))

    def test_ctrl_d_twice_closes_and_once_says_so(self) -> None:
        app, api = new_app()
        app.press("ctrl+d")
        self.assertIn("Ctrl+D ... Ctrl+D close", app.prompt())
        self.assertFalse(app.closed)
        app.press("j")
        self.assertEqual((app.closed, app.selected), (False, "run-2"), "another key cancels and is read")
        app.press("ctrl+d")
        app.press("ctrl+d")
        self.assertTrue(app.closed)
        self.assertEqual(api.calls, [])

    def test_esc_never_sends_and_cancels_only_local_ui(self) -> None:
        app, api = new_app()
        app.attach("run-1")
        api.calls.clear()
        for keys, mode in (([], "keys"), ([":"], "command"), (["?"], "help"), (["s"], "confirm_stop"),
                           (["tab"], "keys"), (["ctrl+x"], "keys")):
            with self.subTest(keys=keys):
                press_all(app, keys)
                self.assertEqual(app.mode, mode)
                app.press("esc")
                self.assertEqual(app.mode, "keys")
                self.assertEqual(app.focus, "runs")
                self.assertEqual(app.keymap.pending, ())
        self.assertEqual(api.calls, [])

    def test_help_lists_the_keys_in_force(self) -> None:
        km = load('version = 1\n[bindings.runs]\n"x" = "runs.refresh"\n')
        app, _ = new_app(km)
        app.press("?")
        self.assertEqual(app.mode, "help")
        event = app.log.latest("help")
        self.assertIn("Ctrl+X r, r, x: refresh the run list (reads run.list)", event.text)
        self.assertIn("Esc never sends anything", event.text)
        self.assertEqual(event.speech, event.text, "the linear renderer says the same")
        frame = "\n".join(t for t, _ in app.frame())
        self.assertIn("-- keys (Esc, q or ? closes", frame)
        app.press("q")
        self.assertEqual((app.mode, app.closed), ("keys", False), "q closes the help, not the TUI")

    def test_the_prompt_follows_the_keymap(self) -> None:
        km = load('version = 1\n[bindings.runs]\n"R" = "runs.refresh"\n"r" = "none"\n')
        app, _ = new_app(km)
        self.assertIn("R refresh", app.prompt())
        self.assertNotIn(" r refresh", app.prompt())
        unbound = load('version = 1\n[bindings.runs]\n"s" = "none"\n[bindings.global]\n"<leader> k" = "none"\n')
        app, _ = new_app(unbound)
        self.assertNotIn("stop", app.prompt(), "an action with no key is not offered")

    def test_a_plugin_binding_sends_nothing_yet(self) -> None:
        app, api = new_app(load('version = 1\n[bindings.global]\n"<leader> p d" = "plugin.example.deploy"\n'))
        press_all(app, ["ctrl+x", "p", "d"])
        self.assertIn("plugin.example.deploy: no plugin host in this build; nothing sent", app.notice)
        self.assertEqual(api.calls, [])

    def test_binding_file_notes_are_shown_as_errors_at_open(self) -> None:
        app, _ = new_app(load('version = 1\n[bindings.runs]\n"x" = "nope"\n'))
        self.assertTrue(any("key bindings:" in e and "unknown action" in e for e in app.errors))

    def test_a_paste_cancels_a_pending_sequence(self) -> None:
        app, api = new_app()
        app.press("ctrl+x")
        app.press(Paste("k"))
        self.assertEqual(app.keymap.pending, ())
        app.press("k")
        self.assertEqual((app.mode, app.selected, api.calls), ("keys", "run-2", []),
                         "k after the paste moves the selection (wrapping to run-2); it is not Stop")


class PagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app, self.api = new_app()
        self.app.resize(100, 20)
        self.app.attach("run-1")
        self.app.on_event({"run_id": "run-1", "text": "".join(f"line {i:03d}\n" for i in range(100))})
        self.app.frame()  # the pager's half page comes from the pane's height

    def newest_shown(self) -> str:
        bar = chr(0x2502) + " "  # the output pane's left edge
        rows = [t[2:] for t, _ in self.app.frame() if t.startswith(bar) and t[2:].strip()]
        return rows[-1] if rows else ""

    def test_scrolling_moves_the_view_and_g_follows(self) -> None:
        app = self.app
        self.assertEqual(self.newest_shown(), "line 099")
        app.press("tab")
        self.assertEqual(app.focus, "output")
        self.assertIn("[pager: following]", "\n".join(t for t, _ in app.frame()))
        app.press("k")
        self.assertEqual(self.newest_shown(), "line 098")
        app.press("ctrl+u")
        half = app._out_rows // 2
        self.assertEqual(self.newest_shown(), f"line {98 - half:03d}")
        app.press("ctrl+d")
        self.assertEqual(self.newest_shown(), "line 098")
        app.press("g")
        app.press("g")
        self.assertEqual(app.scroll, 99)
        self.assertEqual(self.newest_shown(), "line 000")
        app.press("G")
        self.assertEqual((app.scroll, self.newest_shown()), (0, "line 099"))
        app.press("q")
        self.assertEqual(app.focus, "runs")
        self.assertEqual(self.api.calls[-1][0], "run.status", "paging sent nothing")

    def test_a_scrolled_view_stays_put_while_output_arrives(self) -> None:
        app = self.app
        app.press("tab")
        for _ in range(5):
            app.press("k")
        self.assertEqual(self.newest_shown(), "line 094")
        app.on_event({"run_id": "run-1", "text": "new 1\nnew 2\n"})
        self.assertEqual(self.newest_shown(), "line 094")
        app.press("G")
        self.assertEqual(self.newest_shown(), "new 2")

    def test_search_goes_to_older_lines_and_n_and_shift_n_step(self) -> None:
        app = self.app
        app.press("tab")
        press_all(app, ["/", *"line 04", "enter"])
        self.assertEqual(self.newest_shown(), "line 049")
        self.assertIn("search line 04: line 50 of 100", app.notice)
        app.press("n")
        self.assertEqual(self.newest_shown(), "line 048")
        app.press("N")
        app.press("N")
        self.assertEqual(self.newest_shown(), "line 040", "N wraps from line 049 to the next newer match, line 040")
        press_all(app, ["/", *"LINE", "enter"])
        self.assertIn("not found", app.notice, "a capital letter makes the search case-sensitive")
        press_all(app, ["/", *"nothing here", "enter"])
        self.assertIn("not found in the 100 lines kept", app.notice)


class BindingFileEntryTest(unittest.TestCase):
    def test_print_keys_writes_the_default_file_and_needs_no_api(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(term.main(["--print-keys"], None, stdout=out, stderr=err, environ={}), 0)
        self.assertEqual(out.getvalue(), km_mod.default_toml())
        self.assertEqual(load(out.getvalue()).notes, [])

    def test_keys_file_is_used_by_the_interactive_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "keys.toml")
            with open(path, "w", encoding="utf-8") as f:
                f.write('version = 1\n[bindings.runs]\n"x" = "runs.refresh"\n')
            driver = FakeDriver()
            driver.start_run()
            api = RecordingAPI(driver.api)
            code = term.main(["--keys", path], lambda: api, stdin=TtyOut(), stdout=TtyOut(),
                             environ={"NO_COLOR": "1"}, terminal=FakeTerminal(["x", "q"]))
        self.assertEqual(code, 0)
        self.assertEqual([m for m, _ in api.calls], ["run.list", "run.list"])

    def test_the_default_file_is_read_from_the_config_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "recursion"))
            with open(os.path.join(tmp, "recursion", "tui-keys.toml"), "w", encoding="utf-8") as f:
                f.write('version = 1\n[bindings.runs]\n"x" = "runs.refresh"\n')
            out = io.StringIO()
            term.main(["--plain"], lambda: ScriptedAPI(dict(ANSWERS)), stdin=io.StringIO("help\n"), stdout=out,
                      environ={"XDG_CONFIG_HOME": tmp})
        self.assertIn("Ctrl+X r, r, x: refresh the run list", out.getvalue())

    def test_a_named_file_that_is_missing_or_bad_is_said_and_the_defaults_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.toml")
            with open(bad, "w", encoding="utf-8") as f:
                f.write("version = 1\n[safety]\nconfirm_stop = false\n")
            for path, said in ((os.path.join(tmp, "missing.toml"), "does not exist"), (bad, "can only be true")):
                with self.subTest(path=path):
                    out = io.StringIO()
                    code = term.main(["--plain", f"--keys={path}"], lambda: ScriptedAPI(dict(ANSWERS)),
                                     stdin=io.StringIO("stop run-1\n"), stdout=out, environ={})
                    self.assertEqual(code, 0)
                    self.assertIn("key bindings: binding file not used", out.getvalue())
                    self.assertIn(said, out.getvalue())
                    self.assertIn("answer run.stop status=stopped", out.getvalue(), "the TUI still works")
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(term.main(["--keys"], None, stdout=out, stderr=err, environ={}), 2)
        self.assertIn("usage error: unrecognized arguments: --keys", err.getvalue())


if __name__ == "__main__":
    unittest.main()
