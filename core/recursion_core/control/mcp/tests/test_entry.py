"""``python -m recursion_core.control.mcp`` in process: usage, the refusal when the socket check fails (nothing
read, nothing on stdout), serving, and an interrupt."""

from __future__ import annotations

import io
import json
import os
import unittest

from recursion_core.control.mcp import __main__ as entry
from recursion_core.control.mcp import projection
from recursion_core.control.mcp.framing import encode
from recursion_core.control.mcp.socket_check import SocketCheckFailed
from recursion_core.control.mcp.tests.support import ScriptedAPI


class Untouchable(io.RawIOBase):
    """A stream that fails the test if anything reads from or writes to it."""

    def readable(self):
        return True

    def writable(self):
        return True

    def readinto(self, buffer):
        raise AssertionError("stdin was read")

    def readline(self, size=-1):
        raise AssertionError("stdin was read")

    def write(self, data):
        raise AssertionError("stdout was written")


PUBLISHED = projection.default_published()


def stderr_lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


class EntryTest(unittest.TestCase):
    def test_an_argument_is_a_usage_error_and_nothing_is_checked(self) -> None:
        def never(environ):
            raise AssertionError("the socket check ran")

        err = io.StringIO()
        self.assertEqual(2, entry.main(["--stdio"], stdin=Untouchable(), stdout=Untouchable(), stderr=err, check=never))
        self.assertEqual("usage", stderr_lines(err)[0]["code"])

    def test_a_failed_socket_check_refuses_before_reading_and_writes_one_stderr_line(self) -> None:
        def failing(environ):
            raise SocketCheckFailed("socket_dir_mode", "/run/x/recursion has mode 0755")

        err = io.StringIO()
        self.assertEqual(1, entry.main([], environ={}, stdin=Untouchable(), stdout=Untouchable(), stderr=err,
                                       check=failing))
        self.assertEqual([{"code": "socket_check_failed", "reason": "socket_dir_mode",
                           "detail": "/run/x/recursion has mode 0755"}], stderr_lines(err))

    def test_the_check_gets_the_environment_it_is_given(self) -> None:
        seen = []

        def recording(environ):
            seen.append(environ)
            raise SocketCheckFailed("socket_missing")

        entry.main([], environ={"XDG_RUNTIME_DIR": "/x"}, stdin=Untouchable(), stdout=Untouchable(),
                   stderr=io.StringIO(), check=recording)
        self.assertEqual([{"XDG_RUNTIME_DIR": "/x"}], seen)

    def test_a_passed_check_serves_until_end_of_input(self) -> None:
        api = ScriptedAPI([{"method": "methods.list", "params": {},
                            "answer": {"code": "method_unknown", "detail": "methods.list"}},
                           {"method": "run.status", "params": {"run_id": "run-1"},
                            "answer": {"code": "run_unknown", "run_id": "run-1"}}])
        stdin = io.BytesIO(b"".join(encode(m) for m in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "recursion_run_status", "arguments": {"run_id": "run-1"}}})))
        out, err = io.BytesIO(), io.StringIO()
        self.assertEqual(0, entry.main([], stdin=stdin, stdout=out, stderr=err, check=lambda environ: api))
        answers = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([1, 2], [a["id"] for a in answers])
        self.assertEqual("run_unknown", answers[1]["result"]["structuredContent"]["code"])
        events = stderr_lines(err)
        self.assertEqual({"event": "serving", "protocol": "2025-06-18"}, events[0])
        self.assertEqual({"event": "tools", "table": "legacy", "reason": "daemon_without_method_table"}, events[1])
        self.assertEqual("initialize", events[2]["event"])
        self.assertEqual({"event": "end_of_input", "stdout_broken": False}, events[-1])
        api.assert_done(self)

    def test_a_refused_tool_choice_refuses_to_start(self) -> None:
        cases = [
            ({"code": "unavailable", "detail": "daemon_closing"}, lambda: None, "listing_unavailable"),
            ({"code": "ok", "table_version": "1.0-draft.1", "methods": []}, lambda: PUBLISHED, "table_version_mismatch"),
            ({"code": "ok", "methods": []}, lambda: PUBLISHED, "listing_invalid"),
        ]
        for answer, published, reason in cases:
            with self.subTest(reason=reason):
                api = ScriptedAPI([{"method": "methods.list", "params": {}, "answer": answer}])
                err = io.StringIO()
                self.assertEqual(1, entry.main([], stdin=Untouchable(), stdout=Untouchable(), stderr=err,
                                               check=lambda environ, api=api: api, published=published))
                self.assertEqual(("tools_unavailable", reason), (stderr_lines(err)[0]["code"], stderr_lines(err)[0]["reason"]))
                api.assert_done(self)

    def test_published_files_that_cannot_be_read_refuse_to_start(self) -> None:
        def broken():
            raise ValueError("not JSON")

        err = io.StringIO()
        self.assertEqual(1, entry.main([], stdin=Untouchable(), stdout=Untouchable(), stderr=err,
                                       check=lambda environ: ScriptedAPI(), published=broken))
        self.assertEqual({"code": "tools_unavailable", "reason": "published_unreadable", "detail": "ValueError"},
                         stderr_lines(err)[0])

    def test_an_interrupt_exits_130(self) -> None:
        class Interrupting(io.RawIOBase):
            def readable(self):
                return True

            def readline(self, size=-1):
                raise KeyboardInterrupt

        legacy = ScriptedAPI([{"method": "methods.list", "params": {}, "answer": {"code": "method_unknown"}}])
        self.assertEqual(130, entry.main([], stdin=Interrupting(), stdout=io.BytesIO(), stderr=io.StringIO(),
                                         check=lambda environ: legacy))

    def test_an_unexpected_error_while_serving_exits_3_with_one_json_line(self) -> None:
        class Failing(io.RawIOBase):
            def readable(self):
                return True

            def readline(self, size=-1):
                raise RuntimeError("the input broke")

        err = io.StringIO()
        legacy = ScriptedAPI([{"method": "methods.list", "params": {}, "answer": {"code": "method_unknown"}}])
        self.assertEqual(3, entry.main([], stdin=Failing(), stdout=io.BytesIO(), stderr=err,
                                       check=lambda environ: legacy))
        self.assertEqual({"code": "internal_error", "detail": "RuntimeError: the input broke"}, stderr_lines(err)[-1])

    @unittest.skipUnless(os.name == "nt", "Windows-only: the real socket check refuses the platform")
    def test_windows_refuses_with_the_real_check(self) -> None:
        err = io.StringIO()
        self.assertEqual(1, entry.main([], environ={}, stdin=Untouchable(), stdout=Untouchable(), stderr=err))
        self.assertEqual("platform_unsupported", stderr_lines(err)[0]["reason"])


if __name__ == "__main__":
    unittest.main()
