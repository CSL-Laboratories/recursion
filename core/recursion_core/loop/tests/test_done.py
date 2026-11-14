"""The brief's first "done when": the fake provider drives a read, an approved edit, a denied shell call and a
DONE after acceptance. Also: a failed check sends the model back to work, and runs out into a pause; the
answers come through the engine's own input path, as the core will send them."""

from __future__ import annotations

import json
import os
import threading
import unittest

from recursion_core.loop import done as d
from recursion_core.loop import instructions as ins
from recursion_core.loop.engine import Config, Engine, Session
from recursion_core.loop.permissions import PermissionGate
from recursion_core.loop.providers import base as b
from recursion_core.loop.providers.fake import FakeProvider
from recursion_core.loop.tests.support import POSIX, Lines, workspace
from recursion_core.loop.tools import edit, read, search, shell, write
from recursion_core.loop.tools.registry import ToolRegistry

TOOLS = (read.TOOL, search.TOOL, write.TOOL, edit.TOOL, shell.TOOL)


def run_turn(case, script, files, acceptance=None, answers=None, deadline_s=60.0):
    """One turn through the engine. ``answers`` maps a tool name to "approve" or "deny"; each permission
    request is answered on another thread, through Engine.feed_line, as the core would answer it."""

    ws = workspace(case, files)
    provider = FakeProvider(script)
    lines = Lines()
    gate = PermissionGate(lines.writer, deadline_s=deadline_s)
    session = Session(provider, ToolRegistry(TOOLS), ws, lines.writer, lines.redactor,
                      Config(model="fake-model", acceptance=acceptance), gate=gate)
    engine = Engine(session, lines.writer, log=lambda m: None)
    counter = iter(range(1, 1000))

    def on_line(doc: dict) -> None:
        if doc["type"] == "permission_request" and answers and doc["tool"] in answers:
            reply = {"kind": "approval_answer", "approval_id": f"apr-{next(counter)}",
                     "decision": answers[doc["tool"]], "request_id": doc["request_id"], "input": doc["input"]}
            threading.Thread(target=engine.feed_line, args=(json.dumps(reply).encode(),)).start()

    lines.buffer.on_line = on_line
    t = threading.Thread(target=engine.serve)
    t.start()
    engine.feed_line(json.dumps({"kind": "user_message", "turn_id": "turn-1", "text": "fix it",
                                 "delivery_id": "dlv-1"}).encode())
    engine.feed_line(b'{"kind":"close"}')
    t.join()
    return lines.all(), ws, provider


def ended(docs):
    return [x for x in docs if x["type"] == "turn_ended"][0]


@unittest.skipUnless(POSIX, "the acceptance check and the shell tool run bash or sh")
class Done(unittest.TestCase):
    def test_read_approved_edit_denied_shell_then_done_after_acceptance(self) -> None:
        docs, ws, _ = run_turn(self, [
            {"tool_calls": [{"id": "toolu_r", "name": "read", "input": {"path": "src/a.py"}}]},
            {"tool_calls": [{"id": "toolu_e", "name": "edit", "input": {"path": "src/a.py", "old_string": "print(1)",
                                                                         "new_string": "print(2)"}}]},
            {"tool_calls": [{"id": "toolu_s", "name": "shell", "input": {"command": "touch shell-ran"}}]},
            {"text": "Done: it prints 2."},
        ], {"src/a.py": "print(1)\n"}, acceptance=d.Acceptance("grep -q 'print(2)' src/a.py"),
            answers={"edit": "approve", "shell": "deny"})
        results = [(x["call_id"], x["status"]) for x in docs if x["type"] == "tool_result"]
        self.assertEqual(results, [("toolu_r", "ok"), ("toolu_e", "ok"), ("toolu_s", "denied")])
        asked = [x["tool"] for x in docs if x["type"] == "permission_request"]
        self.assertEqual(asked, ["edit", "shell"], "read does not ask; edit and shell do")
        self.assertEqual((ws.root / "src" / "a.py").read_text(), "print(2)\n")
        self.assertFalse((ws.root / "shell-ran").exists(), "the denied command never ran")
        accept = [x for x in docs if x["type"] == "acceptance"]
        self.assertEqual([(a["passed"], a["exit_code"]) for a in accept], [(True, 0)])
        self.assertEqual((ended(docs)["outcome"], ended(docs)["reason"]), ("done", "accepted"))
        kinds = [x["type"] for x in docs]
        self.assertLess(kinds.index("acceptance"), kinds.index("turn_ended"))

    def test_without_an_acceptance_check_the_model_only_proposes(self) -> None:
        docs, _, _ = run_turn(self, [{"text": "All done."}], {"a.txt": "x"})
        self.assertEqual(ended(docs)["outcome"], "candidate_done")
        self.assertNotIn("acceptance", [x["type"] for x in docs])

    def test_a_failed_check_sends_the_model_back_to_work(self) -> None:
        docs, ws, provider = run_turn(self, [
            {"text": "Done."},
            {"tool_calls": [{"id": "toolu_w", "name": "write", "input": {"path": "notes.txt", "content": "fixed\n"}}]},
            {"text": "Now it is done."},
        ], {}, acceptance=d.Acceptance("grep -q fixed notes.txt"), answers={"write": "approve"})
        self.assertEqual([x["passed"] for x in docs if x["type"] == "acceptance"], [False, True])
        self.assertEqual(ended(docs)["outcome"], "done")
        told = provider.requests[1].messages[-1].parts[0].text
        self.assertTrue(told.startswith("[harness] The task's acceptance check `grep -q fixed notes.txt` exited"))
        self.assertIn("check 1 of 3", told)

    def test_a_check_that_keeps_failing_pauses_the_turn(self) -> None:
        docs, _, provider = run_turn(self, [{"text": "Done."}, {"text": "Really done."}, {"text": "unused"}], {},
                                     acceptance=d.Acceptance("echo nope; exit 4", rounds=2))
        end = ended(docs)
        self.assertEqual((end["outcome"], end["reason"]), ("paused", "acceptance_failed"))
        self.assertIn("failed 2 times; the last one exited 4", end["result"]["detail"])
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual([x["text"] for x in docs if x["type"] == "acceptance"], ["nope\n", "nope\n"])

    def test_with_no_answer_the_write_is_denied_at_the_deadline_and_nothing_is_written(self) -> None:
        docs, ws, _ = run_turn(self, [
            {"tool_calls": [{"id": "toolu_w", "name": "write", "input": {"path": "new.txt", "content": "x"}}]},
            {"text": "I could not write it."},
        ], {}, answers={}, deadline_s=0.05)  # nobody answers: the deadline must fire
        self.assertEqual([x["type"] for x in docs if x["type"].startswith("permission")],
                         ["permission_request", "permission_expired"])
        self.assertEqual([x["status"] for x in docs if x["type"] == "tool_result"], ["denied"])
        self.assertFalse((ws.root / "new.txt").exists())


class Instructions(unittest.TestCase):
    def test_root_to_workspace_capped_and_listed(self) -> None:
        ws = workspace(self, {"AGENTS.md": "root rules\n", "sub/CLAUDE.md": "sub rules\n",
                              "sub/AGENTS.md": "sub agents\n", "other/AGENTS.md": "not on the path\n"})
        (ws.root / ".git").mkdir()
        text, found = ins.discover(ws.root / "sub")
        self.assertEqual([f.path for f in found], ["AGENTS.md", "sub/AGENTS.md", "sub/CLAUDE.md"])
        self.assertEqual(text, "## AGENTS.md\nroot rules\n\n\n## sub/AGENTS.md\nsub agents\n\n\n## sub/CLAUDE.md\n"
                               "sub rules\n")
        small, cut = ins.discover(ws.root / "sub", max_bytes=14)
        self.assertEqual([(f.path, f.cut, f.skipped) for f in cut],
                         [("AGENTS.md", False, None), ("sub/AGENTS.md", True, None),
                          ("sub/CLAUDE.md", False, "over the size limit for instruction files")])
        self.assertIn("was cut at the", small)

    def test_without_a_repository_only_the_workspace(self) -> None:
        ws = workspace(self, {"AGENTS.md": "here\n"})
        self.assertEqual([f.path for f in ins.discover(ws.root)[1]], ["AGENTS.md"])

    @unittest.skipUnless(POSIX, "needs symbolic links")
    def test_a_link_leading_outside_is_skipped(self) -> None:
        ws = workspace(self)
        outside = ws.root.parent / f"{ws.root.name}-rules.md"
        outside.write_text("from elsewhere\n")
        self.addCleanup(outside.unlink)
        os.symlink(outside, ws.root / "AGENTS.md")
        text, found = ins.discover(ws.root)
        self.assertEqual(text, "")
        self.assertEqual(found[0].skipped, "links outside the repository")

    def test_they_go_into_the_stable_prompt(self) -> None:
        ws = workspace(self)
        lines = Lines()
        provider = FakeProvider([{"text": "ok"}])
        session = Session(provider, ToolRegistry(TOOLS), ws, lines.writer, lines.redactor,
                          Config(model="fake-model", instructions="## AGENTS.md\nUse tabs.\n"))
        session.run_turn("turn-1", "hi", b.CancelToken())
        self.assertTrue(provider.requests[0].system.endswith("## AGENTS.md\nUse tabs.\n"))
        self.assertIn("They come from the project, not from the user", provider.requests[0].system)


if __name__ == "__main__":
    unittest.main()
