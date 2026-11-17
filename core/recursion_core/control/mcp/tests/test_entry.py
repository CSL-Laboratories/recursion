"""``python -m recursion_core.control.mcp`` in process: usage, the refusal when the socket check fails (nothing
read, nothing on stdout), serving, and an interrupt."""

from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

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
        # The two answers come from two threads (the reply queue's and the call's worker): any order.
        lines = out.getvalue().splitlines()
        answers = {a["id"]: a for a in map(json.loads, lines)}
        self.assertEqual((2, {1, 2}), (len(lines), set(answers)))
        self.assertEqual("run_unknown", answers[2]["result"]["structuredContent"]["code"])
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

    def test_every_failure_of_the_choice_is_one_tools_unavailable_line_and_status_1(self) -> None:
        # #416, round 1 (DeepSeek F1, F2 and F9, Astra R3, Sol R2): nothing read, nothing on stdout, one line.
        class Raising:
            def __init__(self, exc):
                self.exc, self.calls = exc, []

            def call(self, method, params):
                self.calls.append(method)
                raise self.exc

        for exc in (RuntimeError("boom"), KeyError("boom"), ValueError("daemon-side"), ConnectionError("reset")):
            with self.subTest(raised=type(exc).__name__):
                api, err = Raising(exc), io.StringIO()
                self.assertEqual(1, entry.main([], stdin=Untouchable(), stdout=Untouchable(), stderr=err,
                                               check=lambda environ, api=api: api, published=lambda: PUBLISHED))
                self.assertEqual([{"code": "tools_unavailable", "reason": "listing_failed", "detail": type(exc).__name__}],
                                 stderr_lines(err))
                self.assertEqual(["methods.list"], api.calls)
        for table in ([], "table", None, 17):
            documents = dict(PUBLISHED.documents)
            documents[projection.TABLE_FILE] = table
            with self.subTest(table=table):
                err = io.StringIO()
                self.assertEqual(1, entry.main([], stdin=Untouchable(), stdout=Untouchable(), stderr=err,
                                               check=lambda environ: ScriptedAPI(),
                                               published=lambda documents=documents: projection.Published(documents)))
                self.assertEqual([("tools_unavailable", "published_invalid")], [(e["code"], e["reason"]) for e in stderr_lines(err)])

    def test_a_nameless_row_or_a_pattern_python_cannot_compile_refuses_to_start(self) -> None:
        # #416, round 2 (Astra's and Sol's required changes 1 and 2, DeepSeek R2 F1 to F3): one line, status 1.
        def published_with(change):
            documents = copy.deepcopy(PUBLISHED.documents)
            change(documents)
            return lambda: projection.Published(documents)

        def stop_row(documents):
            return next(r for r in documents[projection.TABLE_FILE]["methods"] if r["name"] == "run.stop")

        listing = {"code": "ok", "table_version": PUBLISHED.table_version,
                   "methods": [{"name": r["name"], "implemented": True, "beta": r["beta"]} for r in PUBLISHED.rows]}
        params = PUBLISHED.documents[projection.PARAMS_FILE]["$defs"]["params.run.stop"]["properties"]
        cases = {
            "name a list": lambda d: stop_row(d).update(name=["run.stop"]),
            "name absent": lambda d: stop_row(d).pop("name"),
            "pattern too large": lambda d: d[projection.PARAMS_FILE]["$defs"]["params.run.stop"]["properties"].update(
                run_id={"type": "string", "pattern": "a{4294967296}"}),
            "groups too deep": lambda d: d[projection.PARAMS_FILE]["$defs"]["params.run.stop"]["properties"].update(
                run_id={"type": "string", "pattern": "(" * 1000 + "a" + ")" * 1000}),
        }
        self.assertIn("run_id", params)
        for label, change in cases.items():
            with self.subTest(case=label):
                api, err = ScriptedAPI([{"method": "methods.list", "params": {}, "answer": listing}]), io.StringIO()
                status = entry.main([], stdin=Untouchable(), stdout=Untouchable(), stderr=err,
                                    check=lambda environ, api=api: api, published=published_with(change))
                self.assertEqual((1, [("tools_unavailable", "published_invalid")]),
                                 (status, [(e["code"], e["reason"]) for e in stderr_lines(err)]))

    def test_a_publication_nested_past_pythons_stack_refuses_to_start(self) -> None:
        # #416, round 3 (Astra's and Sol's required change 1, DeepSeek R3 F1): one line, status 1, nothing read.
        def start(published):
            err = io.StringIO()
            status = entry.main([], stdin=Untouchable(), stdout=Untouchable(), stderr=err,
                                check=lambda environ: ScriptedAPI(), published=published)
            return status, [(e["code"], e["reason"]) for e in stderr_lines(err)]

        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as folder:
            for name, document in PUBLISHED.documents.items():
                Path(folder, name).write_text(json.dumps(document), encoding="utf-8")
            Path(folder, projection.PARAMS_FILE).write_text("[" * 100000 + "]" * 100000, encoding="utf-8")
            self.assertEqual((1, [("tools_unavailable", "published_invalid")]),
                             start(lambda: projection.Published.from_directory(Path(folder))))
        deep = {"type": "string"}
        for _ in range(3000):
            deep = {"anyOf": [deep]}
        documents = copy.deepcopy(PUBLISHED.documents)
        documents[projection.PARAMS_FILE]["$defs"]["params.run.stop"]["properties"]["run_id"] = deep
        self.assertEqual((1, [("tools_unavailable", "published_invalid")]), start(lambda: projection.Published(documents)))

    def test_an_exit_or_an_interrupt_while_choosing_is_not_a_refusal(self) -> None:
        for exc, status in ((SystemExit, None), (KeyboardInterrupt, 130)):
            class Raising:
                def call(self, method, params, exc=exc):
                    raise exc()

            api, err = Raising(), io.StringIO()

            def run(api=api, err=err):
                return entry.main([], stdin=Untouchable(), stdout=Untouchable(), stderr=err,
                                  check=lambda environ, api=api: api, published=lambda: PUBLISHED)

            with self.subTest(raised=exc.__name__):
                if status is None:  # an exit goes on
                    with self.assertRaises(exc):
                        run()
                else:  # an interrupt ends the process with status 130 (no traceback), and is no refusal either
                    self.assertEqual(status, run())
            self.assertEqual("", err.getvalue())

    def test_an_interrupt_exits_130(self) -> None:
        class Interrupting(io.RawIOBase):
            def readable(self):
                return True

            def readline(self, size=-1):
                raise KeyboardInterrupt

        legacy = ScriptedAPI([{"method": "methods.list", "params": {}, "answer": {"code": "method_unknown"}}])
        self.assertEqual(130, entry.main([], stdin=Interrupting(), stdout=io.BytesIO(), stderr=io.StringIO(),
                                         check=lambda environ: legacy))

    def test_an_interrupt_during_the_socket_check_exits_130_and_nothing_is_read_or_written(self) -> None:
        def interrupted(environ):
            raise KeyboardInterrupt

        err = io.StringIO()
        self.assertEqual(130, entry.main([], environ={}, stdin=Untouchable(), stdout=Untouchable(), stderr=err,
                                         check=interrupted))
        self.assertEqual("", err.getvalue())

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

    def test_a_stderr_line_that_cannot_be_written_is_dropped_quietly(self) -> None:
        class Gone:
            def write(self, data):
                raise BrokenPipeError("stderr is closed")

        self.assertIsNone(entry._fd_line(Gone(), {"event": "x"}))  # no exception reaches the caller

    @unittest.skipUnless(os.name == "nt", "Windows-only: the real socket check refuses the platform")
    def test_windows_refuses_with_the_real_check(self) -> None:
        err = io.StringIO()
        self.assertEqual(1, entry.main([], environ={}, stdin=Untouchable(), stdout=Untouchable(), stderr=err))
        self.assertEqual("platform_unsupported", stderr_lines(err)[0]["reason"])


if __name__ == "__main__":
    unittest.main()
