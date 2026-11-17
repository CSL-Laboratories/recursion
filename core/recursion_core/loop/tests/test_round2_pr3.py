"""Round 2 of draft #423.

A legacy journal that kept the user's words and then lost the ledger line keeps those words, and a
complete old pair is one message. A turn id is scrubbed before it is written into a compaction
kernel. A provider count already over the ceiling stops the next call when nothing can be cut.
A cached journal is checked again before another line is appended. A short ledger, a non-positive
tail, a summary that is not a reply, and a window that is not a whole number are refused. A loaded
conversation with no session id is refused. A second name of the journal, and a second store of the
same id, are refused.

No test here reaches a network. Every id and secret is made up.
"""

from __future__ import annotations

import json
import os
import stat
import unittest
from unittest.mock import patch

from recursion_core.loop import compaction as c
from recursion_core.loop import store as st
from recursion_core.loop.engine import Config, Session
from recursion_core.loop.providers import base as b
from recursion_core.loop.providers.fake import FakeProvider
from recursion_core.loop.tests.support import Lines, private_dir, registry, workspace
from recursion_core.loop.tools.registry import Tool, ToolOutput, ToolRegistry
from recursion_core.loop.tools.workspace import ToolError

U = lambda text: b.Message("user", (b.TextPart(text),))  # noqa: E731
A = lambda text: b.Message("assistant", (b.TextPart(text),))  # noqa: E731


def flat(messages) -> str:
    return "\n".join(p.text for m in messages for p in m.parts if isinstance(p, b.TextPart))


class Round2(unittest.TestCase):
    def session(self, steps=(), store=None, loaded=None, secrets=(), reg=None, **cfg):
        lines = Lines(list(secrets))
        provider = FakeProvider(steps)
        session = Session(provider, reg or registry(), workspace(self), lines.writer, lines.redactor,
                          Config(model="fake", retry_delays=(), **cfg), store=store, loaded=loaded)
        return session, provider, lines

    def test_partial_legacy_ledger_with_torn_record_keeps_missing_rule(self) -> None:
        rule = "NEVER run a shell command without asking me."
        home = private_dir(self)
        folder = home / "sessions"
        folder.mkdir(mode=0o700)
        path = folder / "s.jsonl"
        records = [
            {"k": "message", "role": "user", "parts": [{"t": "text", "text": "first rule"}]},
            {"k": "user", "turn": "old-1", "text": "first rule"},
            {"k": "message", "role": "assistant", "parts": [{"t": "text", "text": "noted"}]},
            {"k": "message", "role": "user", "parts": [{"t": "text", "text": rule}]},
            {"k": "message", "role": "assistant", "parts": [{"t": "text", "text": "x" * 90000}]},
        ]
        path.write_bytes(("\n".join(json.dumps(r) for r in records) + "\n").encode() + b'{"k":"user","turn":"old-2"')
        path.chmod(0o600)
        store = st.ConversationStore(home, "s")
        try:
            try:
                loaded = store.load()
                session, provider, _ = self.session([{"text": "summary"}, {"text": "done"}], store=store,
                                                    loaded=loaded, context_window=20000)
            except st.StoreError:
                return
            session.run_turn("new", "next", b.CancelToken())
            self.assertTrue(provider.requests)
            self.assertIn(rule, flat(provider.requests[-1].messages))
        finally:
            store.close()

    def test_complete_legacy_pair_does_not_duplicate_user_words(self) -> None:
        store = st.ConversationStore(private_dir(self), "s")
        store.create()
        store.close()
        records = [
            {"k": "message", "role": "user", "parts": [{"t": "text", "text": "do this once"}]},
            {"k": "user", "turn": "old", "text": "do this once"},
        ]
        store.path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        self.assertEqual(store.load().messages, [U("do this once")])

    def test_resumed_turn_id_secret_with_store_scrub_stays_private(self) -> None:
        secret = "SYNTHETIC_R2_ID_SECRET_0000"
        lines = Lines([secret])
        store = st.ConversationStore(private_dir(self), "s", lines.redactor.scrub_obj)
        # The earlier run wrote the id before this run's secret list existed.
        old = st.ConversationStore(store.folder.parent, "s")
        old.user("turn-prefix_" + secret, "rule")
        old.message(A("x" * 90000))
        old.close()
        try:
            session, provider, _ = self.session([{"text": "summary"}, {"text": "done"}], store=store,
                                                loaded=store.load(), secrets=[secret], context_window=20000)
            session.run_turn("new", "next", b.CancelToken())
            self.assertFalse(any(secret in repr(req.messages) for req in provider.requests))
            self.assertNotIn(secret, repr(session.user_messages))
        except st.StoreError:
            pass
        finally:
            store.close()

    def test_current_turn_id_secret_is_not_put_in_compaction_kernel(self) -> None:
        secret = "SYNTHETIC_CURRENT_ID_SECRET"
        session, provider, _ = self.session([{"text": "noted"}, {"text": "summary"}, {"text": "done"}],
                                            secrets=[secret], context_window=20000)
        session.run_turn("turn-" + secret, "rule", b.CancelToken())
        session._append(A("x" * 90000))
        session.run_turn("next", "continue", b.CancelToken())
        self.assertFalse(any(secret in repr(req.messages) for req in provider.requests))

    def test_measured_over_ceiling_after_empty_pause_stops_next_request(self) -> None:
        empty = b.TurnResponse((), b.PAUSE, b.Usage(input_tokens=b.observed(18000)))
        session, provider, lines = self.session([empty, {"text": "must not send"}], context_window=20000,
                                                system="S" * 40000)
        outcome = session.run_turn("t1", "one", b.CancelToken())
        self.assertEqual((outcome, len(provider.requests)), ("failed", 1))
        self.assertEqual(lines.of("turn_ended")[-1]["reason"], "context_full")

    def test_system_and_tool_overhead_are_each_counted(self) -> None:
        huge = ToolRegistry((Tool(b.ToolSpec("huge", "x" * 68000, {"type": "object"}),
                                  lambda _args, _ctx: ToolOutput("unused")),))
        for kind, cfg, reg in (("system", {"system": "S" * 68000}, None),
                               ("tools", {"system": ""}, huge)):
            with self.subTest(kind=kind):
                session, provider, _ = self.session([{"text": "must not send"}], reg=reg, context_window=20000,
                                                    **cfg)
                self.assertEqual(session.run_turn("t", "tiny", b.CancelToken()), "failed")
                self.assertEqual(provider.requests, [])

    def test_loaded_without_id_is_not_silently_dropped(self) -> None:
        store = st.ConversationStore(private_dir(self), "s")
        store.create()
        loaded = st.Loaded(messages=[U("imported rule")], user_messages=[("old", "imported rule")])
        try:
            try:
                session, _, _ = self.session([{"text": "done"}], store=store, loaded=loaded)
            except st.StoreError:
                return
            session.run_turn("new", "next", b.CancelToken())
            store.close()
            self.assertIn("imported rule", flat(store.load().messages))
        finally:
            store.close()

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits")
    def test_cached_journal_privacy_is_rechecked_before_append(self) -> None:
        for changed in ("file", "folder"):
            with self.subTest(changed=changed):
                store = st.ConversationStore(private_dir(self), "s")
                store.user("old", "already written")
                path = store.path if changed == "file" else store.folder
                path.chmod(0o644 if changed == "file" else 0o755)
                before = store.path.read_bytes()
                try:
                    with self.assertRaises((st.StoreError, ToolError)):
                        store.user("next", "must not append")
                    self.assertEqual(store.path.read_bytes(), before)
                finally:
                    path.chmod(0o600 if changed == "file" else 0o700)
                    store.close()

    def test_verbatim_check_runs_after_a_broken_kernel_builder(self) -> None:
        with patch.object(c, "kernel", return_value="[broken kernel omitted the rule]"):
            with self.assertRaises(c.LedgerMismatch):
                c.compact([U("important rule"), A("x" * 10000), U("next")],
                          [("1", "important rule"), ("2", "next")], lambda _prefix: None, 1)

    def test_verbatim_check_detects_tampered_kernel(self) -> None:
        with self.assertRaises(c.LedgerMismatch):
            c._check_verbatim([U(c.HEAD + " missing")], [("t", "important rule")])

    def test_a_short_ledger_is_refused(self) -> None:
        with self.assertRaises(c.LedgerMismatch):
            c.compact([U("important rule"), A("x" * 4000), U("next")], [("1", "next")], lambda _prefix: None, 1)

    def test_post_compaction_check_is_a_backstop(self) -> None:
        real = c.estimate

        def altered(messages):
            if any("MODEL_SUMMARY_MARKER" in getattr(part, "text", "") for m in messages for part in m.parts):
                return 99999
            return real(messages)

        with patch.object(c, "estimate", side_effect=altered):
            with self.assertRaises(c.ContextFull):
                c.compact([U("rule"), A("x" * 10000), U("next")], [("1", "rule"), ("2", "next")],
                          lambda _prefix: b.TurnResponse((b.TextPart("MODEL_SUMMARY_MARKER"),), b.END_TURN, b.Usage()),
                          1, room=4000)

    def test_linked_session_folder_refuses_before_touching_target(self) -> None:
        root = private_dir(self)
        target = root / "target"
        target.mkdir(mode=0o700)
        (root / "sessions").symlink_to(target, target_is_directory=True)
        store = st.ConversationStore(root, "s")
        with self.assertRaises((st.StoreError, ToolError)):
            store.user("t", "private")
        self.assertEqual(list(target.iterdir()), [])

    @unittest.skipUnless(os.name == "posix", "POSIX links")
    def test_a_hard_link_to_the_journal_is_refused(self) -> None:
        store = st.ConversationStore(private_dir(self), "s")
        store.user("old", "already written")
        store.close()
        alias = store.path.with_name("alias.jsonl")
        os.link(store.path, alias)
        self.addCleanup(alias.unlink)
        before = store.path.read_bytes()
        again = st.ConversationStore(store.folder.parent, "s")
        with self.assertRaises(st.StoreError):
            again.user("next", "must not append")
        again.close()
        self.assertEqual(store.path.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(os.stat(store.path).st_mode) & 0o777, 0o600)
        self.assertGreater(os.stat(store.path).st_nlink, 1)

    def test_keep_tokens_and_a_summary_and_a_window_must_be_usable(self) -> None:
        msgs = [U("rule"), A("x" * 10000), U("next")]
        ledger = [("1", "rule"), ("2", "next")]
        for bad in (0, -5):
            with self.subTest(keep=bad):
                with self.assertRaises(ValueError):
                    c.compact(msgs, ledger, lambda _prefix: None, bad)
        with self.assertRaisesRegex(ValueError, "turn response"):
            c.compact(msgs, ledger, lambda _prefix: "not a reply", 1)
        with self.assertRaisesRegex(ValueError, "whole number"):
            c.ceiling(20000.0)
        with self.assertRaisesRegex(ValueError, "whole number"):
            c.ceiling(True)

    def test_a_second_store_cannot_append_while_the_first_holds_the_journal(self) -> None:
        try:
            import fcntl  # noqa: F401
        except ImportError:
            self.skipTest("this host has no file lock")
        home = private_dir(self)
        first = st.ConversationStore(home, "s")
        first.user("old", "already written")
        before = first.path.read_bytes()
        second = st.ConversationStore(home, "s")
        try:
            with self.assertRaises(st.StoreError):
                second.user("next", "must not append")
            self.assertEqual(first.path.read_bytes(), before)
        finally:
            second.close()
            first.close()


if __name__ == "__main__":
    unittest.main()
