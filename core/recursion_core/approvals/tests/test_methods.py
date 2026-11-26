"""approval.list, approval.open and approval.answer through SPINE's real method table and dispatcher (#369).

The registrant is installed into ``Hooks()`` and frozen, the event store is SPINE's shared fake, and every
call goes through ``hooks.methods.dispatch``: the dispatcher's receipts, its transaction and its
after-commit callbacks are the real ones.
"""

from __future__ import annotations

import io
import json
import types
import unittest
from pathlib import Path

from recursion_core.approvals import model
from recursion_core.approvals import store as rows
from recursion_core.approvals.hooks import install
from recursion_core.approvals.queue import ApprovalQueue

try:
    from recursion_core.control.hooks import Hooks, Principal
    from recursion_core.control.commands import approval as cli
    from recursion_core.control.tests.test_seams import FakeStore
except ImportError:  # pragma: no cover - the seams (#369) are not in this tree
    Hooks = None

V1 = Path(__file__).resolve().parents[4] / "schemas" / "control" / "v1"
needs_seams = unittest.skipUnless(Hooks is not None and (V1 / "vectors").is_dir(),
                                  "SPINE's seam files (#369) and schemas (#368) are not both in this tree")
START = 1_789_000_000.0


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> float:
        return self.now


@needs_seams
class MethodTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.queue = ApprovalQueue(clock=self.clock)
        self.hooks = Hooks()
        install(self.hooks, self.queue)
        self.hooks.freeze()
        self.store = FakeStore()
        self.addCleanup(self.store.close)
        self.queue.attach(self.store)
        self.hooks.methods.store = self.store
        self.owner = Principal("owner", "local")
        self.phone = Principal("device", "phone-1")
        effect = json.loads((V1 / "vectors" / "approval.binding" / "g01-a-tool-call.json").read_text(encoding="utf-8"))
        self.effect = effect["record"]["effect"]
        self.approval_id = self.queue.request("engine_tool", self.effect, session_id=self.effect["session_id"],
                                              run_id=self.effect["run_id"], turn_id="turn-0001",
                                              engine_request_id="toolu_1")
        self.count = 0

    def call(self, method: str, params: dict, principal=None) -> dict:
        return self.hooks.methods.dispatch(method, params, principal or self.owner, lambda m, p: None)

    def _cid(self) -> str:
        self.count += 1
        return "cmd-%08d" % self.count

    def open(self, principal=None, surface="cli", command_id=None) -> dict:
        return self.call("approval.open", {"command_id": command_id or self._cid(), "approval_id": self.approval_id,
                                           "surface": surface}, principal)

    def answer(self, record: dict, answer="approve", principal=None, surface="cli", command_id=None,
               revision=None) -> dict:
        return self.call("approval.answer", {
            "command_id": command_id or self._cid(), "approval_id": self.approval_id, "surface": surface,
            "expected_revision": revision or record["revision"], "digest": record["digest"], "answer": answer},
            principal)

    def test_list_returns_summaries_only(self) -> None:
        answer = self.call("approval.list", {})
        self.assertEqual(answer["code"], "ok")
        (summary,) = answer["approvals"]
        self.assertEqual(set(summary), {"approval_id", "session_id", "run_id", "turn_id", "source", "state",
                                        "requested_at", "expires_at", "revision"})

    def test_open_then_answer_through_the_dispatcher_and_a_replay_is_historical(self) -> None:
        told: list = []
        self.queue.subscribe_answered(lambda record, binding: told.append((record["state"], self.store.commits)))
        record = self.open()["approval"]
        commits = self.store.commits
        answer = self.answer(record, command_id="cmd-answer-1")
        self.assertEqual(answer["approval"]["state"], "approved")
        self.assertEqual(told, [("approved", commits + 1)])  # told after the answer's commit, not before
        events = len(self.store.events)
        self.assertEqual(self.answer(record, command_id="cmd-answer-1"), answer)  # replayed, not taken again
        self.assertEqual(len(self.store.events), events)
        reused = self.answer(record, "deny", command_id="cmd-answer-1")
        self.assertEqual((reused["code"], reused.get("detail")), ("conflict", "command_id_reused"))

    def test_callers_that_may_not_answer_are_refused_and_nothing_is_kept(self) -> None:
        record = self.open()["approval"]
        receipts = dict(self.store.receipts)
        for principal in (Principal("plugin", "demo.plugin"), Principal("session", "ses-1"),
                          Principal("internal", "core"), Principal("owner", "local", in_run_unit=True)):
            with self.subTest(kind=principal.kind, in_run_unit=principal.in_run_unit):
                for method in ("open", "answer"):
                    got = self.open(principal) if method == "open" else self.answer(record, principal=principal)
                    self.assertEqual(got, {"code": "scope_refused", "detail": "approve"})
        self.assertEqual(dict(self.store.receipts), receipts)
        self.assertEqual(self.queue.wait(self.approval_id, timeout=0), None)  # still pending

    def test_a_request_that_is_not_whole_is_refused_and_keeps_no_receipt(self) -> None:
        receipts = dict(self.store.receipts)
        got = self.call("approval.answer", {"command_id": self._cid(), "approval_id": self.approval_id,
                                            "surface": "cli", "expected_revision": 1, "answer": "maybe",
                                            "digest": "0" * 64})
        self.assertEqual(got, {"code": "request_invalid", "detail": "answer"})
        got = self.call("approval.open", {"command_id": self._cid(), "approval_id": self.approval_id, "surface": "web"})
        self.assertEqual(got, {"code": "request_invalid", "detail": "surface"})
        self.assertEqual(dict(self.store.receipts), receipts)

    def test_list_alone_or_an_older_revision_is_not_shown(self) -> None:
        with self.store.transaction() as tx:
            held = rows.load(tx, self.approval_id)
        self.assertEqual(self.answer(held)["code"], "prompt_not_shown")
        shown = self.open()["approval"]
        with self.store.transaction() as tx:  # the core changes the content: revision 2
            rows.save(tx, model.change(rows.load(tx, self.approval_id), expires_at="2099-01-01T00:00:00.000Z"))
        stale = self.answer(shown)
        self.assertEqual((stale["code"], stale.get("detail"), stale.get("current_revision")),
                         ("conflict", "stale_revision", 2))
        self.assertEqual(self.answer(shown, revision=2)["code"], "prompt_not_shown")  # it showed revision 1 only
        self.assertEqual(self.answer(self.open()["approval"])["approval"]["state"], "approved")

    def test_another_surface_must_have_shown_it_too(self) -> None:
        record = self.open()["approval"]  # shown on the command line, to the owner
        self.assertEqual(self.answer(record, principal=self.phone, surface="phone")["code"], "prompt_not_shown")
        self.assertEqual(self.answer(record, surface="tui")["code"], "prompt_not_shown")
        self.open(self.phone, "phone")
        self.assertEqual(self.answer(record, principal=self.phone, surface="phone")["approval"]["answered_by"],
                         {"kind": "device", "id": "phone-1"})

    def test_a_record_that_is_not_an_approval_is_refused_and_nothing_kept(self) -> None:
        record = self.open()["approval"]
        with self.store.transaction() as tx:
            tx.execute("UPDATE approvals_records SET record = ? WHERE approval_id = ?",
                       (json.dumps(dict(record, revision=0)), self.approval_id))
        receipts = dict(self.store.receipts)
        got = self.answer(record)
        self.assertEqual(got["code"], "unavailable")
        self.assertEqual(dict(self.store.receipts), receipts)

    def test_the_command_line_shows_then_answers(self) -> None:
        def run(*argv) -> tuple:
            out, err = io.StringIO(), io.StringIO()
            args = types.SimpleNamespace(command="approval", out=out, err=err)
            if argv[0] == "list":
                args.approval_action, args.session, args.state = "list", None, None
            elif argv[0] == "open":
                args.approval_action, args.approval_id = "open", argv[1]
            else:
                args.approval_action, args.approval_id, args.answer, args.revision, args.digest = argv
            client = types.SimpleNamespace(call=lambda method, params: self.call(method, params))
            return cli.dispatch(args, client), out.getvalue(), err.getvalue()

        status, out, _ = run("open", self.approval_id)
        self.assertEqual(status, 0)
        self.assertTrue(out.startswith("Tool call waiting for an answer\n"))
        self.assertIn('  | "command": "rm -rf build && make"', out)
        digest = out.split("digest: ", 1)[1].split("\n", 1)[0]
        self.assertEqual(run("answer", self.approval_id, "deny", 1, "0" * 64)[0], 1)  # not the digest shown
        status, out, _ = run("answer", self.approval_id, "deny", 1, digest)
        self.assertEqual((status, json.loads(out)["state"]), (0, "denied"))
        self.assertEqual(json.loads(run("list")[1])["approvals"][0]["state"], "denied")


if __name__ == "__main__":
    unittest.main()
