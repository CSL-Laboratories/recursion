"""write, edit and shell through the registry: read before change, nothing runs without an approval, and
commands are stopped at their deadline or on an interrupt."""

from __future__ import annotations

import os
import stat
import threading
import unittest

from recursion_core.loop import permissions as perm
from recursion_core.loop.providers import base as b
from recursion_core.loop.tests.support import FAKE_KEY, POSIX, workspace
from recursion_core.loop.tools import edit, read, search, shell, write
from recursion_core.loop.tools import registry as r

TOOLS = (read.TOOL, search.TOOL, write.TOOL, edit.TOOL, shell.TOOL)


class Approver:
    """Answers every request with one decision, and counts the requests."""

    def __init__(self, outcome: str = perm.APPROVED) -> None:
        self.outcome = outcome
        self.asked: list[b.ToolCall] = []

    def __call__(self, call: b.ToolCall) -> perm.Decision:
        self.asked.append(call)
        return perm.Decision(self.outcome, dict(call.input) if self.outcome == perm.APPROVED else None)


def run(ws, name: str, args: dict, approver=None, cancel=None) -> r.Outcome:
    ctx = r.ToolContext(ws, cancel or b.CancelToken(), lambda s: s.replace(FAKE_KEY, "[redacted]"),
                        approve=approver if approver is not None else Approver())
    return r.ToolRegistry(TOOLS).dispatch(b.ToolCall("toolu_c", name, args, ""), ctx)


class Write(unittest.TestCase):
    def test_create_needs_an_approval_and_makes_folders(self) -> None:
        ws = workspace(self)
        approver = Approver()
        out = run(ws, "write", {"path": "pkg/new.py", "content": "x = 1\n"}, approver)
        self.assertEqual(out.status, r.OK, out.model_text)
        self.assertEqual((ws.root / "pkg" / "new.py").read_text(), "x = 1\n")
        self.assertEqual(len(approver.asked), 1)

    def test_a_denied_write_writes_nothing(self) -> None:
        ws = workspace(self)
        out = run(ws, "write", {"path": "new.py", "content": "x"}, Approver(perm.DENIED))
        self.assertEqual(out.status, r.DENIED)
        self.assertFalse((ws.root / "new.py").exists())

    def test_an_expired_request_writes_nothing(self) -> None:
        ws = workspace(self)
        out = run(ws, "write", {"path": "new.py", "content": "x"}, Approver(perm.EXPIRED))
        self.assertEqual(out.status, r.DENIED)
        self.assertIn("before its deadline", out.model_text)
        self.assertFalse((ws.root / "new.py").exists())

    def test_replacing_a_file_needs_a_fresh_read_and_is_refused_before_asking(self) -> None:
        ws = workspace(self, {"a.py": "old\n"})
        approver = Approver()
        out = run(ws, "write", {"path": "a.py", "content": "new\n"}, approver)
        self.assertEqual(out.status, r.ERROR)
        self.assertIn("not_read", out.model_text)
        self.assertEqual(approver.asked, [], "a call that would fail is not put to the user")
        run(ws, "read", {"path": "a.py"})
        (ws.root / "a.py").write_text("changed by someone else\n")
        stale = run(ws, "write", {"path": "a.py", "content": "new\n"}, approver)
        self.assertIn("stale_snapshot", stale.model_text)
        run(ws, "read", {"path": "a.py"})
        ok = run(ws, "write", {"path": "a.py", "content": "new\n"}, approver)
        self.assertEqual(ok.status, r.OK, ok.model_text)
        self.assertEqual((ws.root / "a.py").read_text(), "new\n")

    def test_a_file_changed_while_the_request_waits_is_not_replaced(self) -> None:
        ws = workspace(self, {"a.py": "old\n"})
        run(ws, "read", {"path": "a.py"})

        def approve_then_change(call: b.ToolCall) -> perm.Decision:
            (ws.root / "a.py").write_text("theirs\n")
            return perm.Decision(perm.APPROVED, dict(call.input))

        out = run(ws, "write", {"path": "a.py", "content": "mine\n"}, approve_then_change)
        self.assertIn("stale_snapshot", out.model_text)
        self.assertEqual((ws.root / "a.py").read_text(), "theirs\n")

    def test_git_folders_spill_files_and_outside_paths_are_not_written(self) -> None:
        ws = workspace(self)
        (ws.root / ".git").mkdir()
        for path in (".git/hooks/pre-commit", "sub/.git/config", "spill:" + "a" * 64, "../outside.txt"):
            with self.subTest(path=path):
                out = run(ws, "write", {"path": path, "content": "x"})
                self.assertEqual(out.status, r.ERROR)
        self.assertFalse((ws.root / ".git" / "hooks").exists())

    @unittest.skipUnless(POSIX, "POSIX permission bits")
    def test_a_replaced_file_keeps_its_mode(self) -> None:
        ws = workspace(self, {"run.sh": "echo 1\n"})
        os.chmod(ws.root / "run.sh", 0o750)
        run(ws, "read", {"path": "run.sh"})
        run(ws, "write", {"path": "run.sh", "content": "echo 2\n"})
        self.assertEqual(stat.S_IMODE(os.stat(ws.root / "run.sh").st_mode), 0o750)
        self.assertEqual([p.name for p in ws.root.iterdir()], ["run.sh"], "no temporary file is left behind")


class Edit(unittest.TestCase):
    def test_an_edit_with_no_read_is_refused(self) -> None:
        ws = workspace(self, {"a.py": "print(1)\n"})
        approver = Approver()
        out = run(ws, "edit", {"path": "a.py", "old_string": "1", "new_string": "2"}, approver)
        self.assertEqual(out.status, r.ERROR)
        self.assertIn("not_read", out.model_text)
        self.assertEqual(approver.asked, [])
        self.assertEqual((ws.root / "a.py").read_text(), "print(1)\n")

    def test_exact_unique_match_and_the_typed_refusals(self) -> None:
        ws = workspace(self, {"a.py": "x = 1\ny = 1\n"})
        run(ws, "read", {"path": "a.py"})
        many = run(ws, "edit", {"path": "a.py", "old_string": "= 1", "new_string": "= 2"})
        self.assertIn("anchor_not_unique: old_string occurs 2 times", many.model_text)
        none = run(ws, "edit", {"path": "a.py", "old_string": "z = 1", "new_string": "z = 2"})
        self.assertIn("replace_not_found", none.model_text)
        same = run(ws, "edit", {"path": "a.py", "old_string": "x", "new_string": "x"})
        self.assertIn("nothing would change", same.model_text)
        empty = run(ws, "edit", {"path": "a.py", "old_string": "", "new_string": "q"})
        self.assertIn("too short", empty.model_text)
        ok = run(ws, "edit", {"path": "a.py", "old_string": "y = 1", "new_string": "y = 2"})
        self.assertEqual(ok.status, r.OK, ok.model_text)
        self.assertTrue(ok.model_text.startswith("edited a.py: 1 replacement\n"))
        self.assertIn("     2\ty = 2\n", ok.model_text)
        again = run(ws, "edit", {"path": "a.py", "old_string": "= 1", "new_string": "= 3", "replace_all": False})
        self.assertEqual(again.status, r.OK, "the loop's own edit keeps the read fresh")
        self.assertEqual((ws.root / "a.py").read_text(), "x = 3\ny = 2\n")

    def test_replace_all(self) -> None:
        ws = workspace(self, {"a.py": "a a a\n"})
        run(ws, "read", {"path": "a.py"})
        out = run(ws, "edit", {"path": "a.py", "old_string": "a", "new_string": "b", "replace_all": True})
        self.assertTrue(out.model_text.startswith("edited a.py: 3 replacements"))
        self.assertEqual((ws.root / "a.py").read_text(), "b b b\n")

    def test_a_file_changed_after_the_approval_is_not_overwritten(self) -> None:
        ws = workspace(self, {"a.py": "v = 1\n"})
        run(ws, "read", {"path": "a.py"})

        def approve_then_change(call: b.ToolCall) -> perm.Decision:
            (ws.root / "a.py").write_text("v = 99\n")  # someone writes while the request waits
            return perm.Decision(perm.APPROVED, dict(call.input))

        out = run(ws, "edit", {"path": "a.py", "old_string": "v = 1", "new_string": "v = 2"}, approve_then_change)
        self.assertIn("stale_snapshot", out.model_text)
        self.assertEqual((ws.root / "a.py").read_text(), "v = 99\n")


@unittest.skipUnless(POSIX, "the shell tool runs bash or sh")
class Shell(unittest.TestCase):
    def test_exit_status_and_output_in_order(self) -> None:
        ws = workspace(self)
        ok = run(ws, "shell", {"command": "echo out; echo err >&2; echo again"})
        self.assertEqual(ok.status, r.OK)
        self.assertRegex(ok.model_text, r"^exit 0 after \d+\.\d s\nout\nerr\nagain\n$")
        bad = run(ws, "shell", {"command": "exit 3"})
        self.assertEqual(bad.status, r.ERROR)
        self.assertTrue(bad.model_text.startswith("exit 3 after"))

    def test_the_bound_input_is_checked_before_it_runs(self) -> None:
        ws = workspace(self)

        def float_timeout(call: b.ToolCall) -> perm.Decision:
            return perm.Decision(perm.APPROVED, dict(call.input, timeout_s=5.5))

        out = run(ws, "shell", {"command": "touch ran", "timeout_s": 5}, float_timeout)
        self.assertEqual(out.status, r.ERROR)
        self.assertIn("invalid approved input", out.model_text)
        self.assertFalse((ws.root / "ran").exists())

    def test_a_denied_command_does_not_run(self) -> None:
        ws = workspace(self)
        out = run(ws, "shell", {"command": "touch ran"}, Approver(perm.DENIED))
        self.assertEqual(out.status, r.DENIED)
        self.assertFalse((ws.root / "ran").exists())

    def test_no_standard_input_and_no_key(self) -> None:
        ws = workspace(self)
        os.environ["ANTHROPIC_API_KEY"] = FAKE_KEY
        self.addCleanup(os.environ.pop, "ANTHROPIC_API_KEY", None)
        out = run(ws, "shell", {"command": "cat; echo key=${ANTHROPIC_API_KEY:-none} pager=$PAGER"})
        self.assertIn("key=none pager=cat", out.model_text)

    def test_the_deadline_stops_the_whole_group(self) -> None:
        ws = workspace(self)
        # The command never ends by itself, so the deadline must stop it; its child is stopped with it.
        out = run(ws, "shell", {"command": "sleep 300 & echo started; wait", "timeout_s": 1})
        self.assertEqual(out.status, r.ERROR)
        self.assertTrue(out.model_text.startswith("timed out after 1 s"))
        self.assertIn("started", out.model_text)

    def test_an_interrupt_stops_the_command(self) -> None:
        ws = workspace(self)
        cancel = b.CancelToken()
        box: dict = {}

        def go() -> None:
            box["out"] = run(ws, "shell", {"command": "touch began; sleep 300"}, cancel=cancel)

        t = threading.Thread(target=go)
        t.start()
        while not (ws.root / "began").exists():
            threading.Event().wait(0.005)
        cancel.cancel()
        t.join()
        self.assertEqual(box["out"].status, r.CANCELLED)

    def test_a_background_process_holding_the_output_is_stopped(self) -> None:
        ws = workspace(self)
        out = run(ws, "shell", {"command": "(sleep 300; echo late) & echo now"})
        self.assertEqual(out.status, r.OK)
        self.assertIn("now", out.model_text)
        self.assertNotIn("late", out.model_text)


if __name__ == "__main__":
    unittest.main()
