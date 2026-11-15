"""run.list paging (ADV1-5): ``limit`` / ``after`` on the control API, and the CLI and TUI following ``next_after``.

The daemon's own cut to the socket frame is tested with the composed daemon in
``daemon/tests/test_adv1_socket_cli_gui.py``. Everything here is in memory.
"""

from __future__ import annotations

import io
import json
import unittest

from recursion_core.control import cli
from recursion_core.control.api import LIST_LIMIT_MAX
from recursion_core.control.tests.fakes import make_api, started
from recursion_core.tui import protocol
from recursion_core.tui.app import TuiApp
from recursion_core.tui.tests.fakes import Clock


def api_with_runs(count):
    api = make_api()[0]
    for _ in range(count):
        started(api)
    return api


class Paged:
    """Wraps an API: a no-limit run.list is asked with ``limit=page``, so the answer pages as the daemon's would."""

    def __init__(self, inner, page) -> None:
        self.inner, self.page, self.calls = inner, page, []

    def call(self, method, params=None):
        params = dict(params or {})
        self.calls.append((method, dict(params)))
        if method == "run.list":
            params.setdefault("limit", self.page)
        return self.inner.call(method, params)


class Scripted:
    """Answers run.list from a list of canned pages, in order."""

    def __init__(self, pages) -> None:
        self.pages, self.calls = list(pages), []

    def call(self, method, params=None):
        self.calls.append((method, dict(params or {})))
        return json.loads(json.dumps(self.pages.pop(0)))


def run(rid):
    return {"run_id": rid, "state": "running"}


class ApiListTest(unittest.TestCase):
    def test_no_parameters_lists_every_run_as_before(self) -> None:
        api = api_with_runs(5)
        answer = api.call("run.list")
        self.assertEqual(sorted(answer), ["code", "runs"])
        self.assertEqual(answer, api.call("run.list", {}))
        self.assertEqual(len(answer["runs"]), 5)

    def test_limit_and_after_walk_every_run_once_in_order(self) -> None:
        api = api_with_runs(7)
        everything = api.call("run.list")["runs"]
        seen, after = [], None
        for _ in range(10):
            params = {"limit": 3} if after is None else {"limit": 3, "after": after}
            page = api.call("run.list", params)
            self.assertEqual(page["code"], "ok")
            seen.extend(page["runs"])
            after = page.get("next_after")
            if after is None:
                break
            self.assertEqual(after, page["runs"][-1]["run_id"])
        self.assertEqual(seen, everything)

    def test_limit_out_of_bounds_is_request_invalid(self) -> None:
        api = api_with_runs(2)
        for limit in (0, -3, LIST_LIMIT_MAX + 1, True, False, "2", 2.0, [2]):
            with self.subTest(limit=limit):
                self.assertEqual(api.call("run.list", {"limit": limit}), {"code": "request_invalid", "detail": "limit"})
        self.assertEqual(len(api.call("run.list", {"limit": LIST_LIMIT_MAX})["runs"]), 2)
        self.assertEqual(len(api.call("run.list", {"limit": 1})["runs"]), 1)

    def test_a_stale_or_unknown_cursor_is_refused_not_an_empty_page(self) -> None:
        api = api_with_runs(2)
        self.assertEqual(api.call("run.list", {"after": "run-999"}), {"code": "request_invalid", "detail": "after_unknown"})
        for after in ("", 7, {"run_id": "run-1"}):
            with self.subTest(after=after):
                self.assertEqual(api.call("run.list", {"after": after}), {"code": "request_invalid", "detail": "after"})
        # The last run is a real cursor: the page after it is empty and is the last page.
        last = api.call("run.list")["runs"][-1]["run_id"]
        self.assertEqual(api.call("run.list", {"after": last}), {"code": "ok", "runs": []})


class FollowTest(unittest.TestCase):
    def test_one_page_is_returned_as_it_came(self) -> None:
        api = api_with_runs(3)
        self.assertEqual(protocol.list_all_runs(api.call), api.call("run.list"))

    def test_pages_are_joined_and_every_run_is_read_once(self) -> None:
        api = api_with_runs(11)
        paged = Paged(api, 4)
        joined = protocol.list_all_runs(paged.call)
        self.assertEqual(joined, api.call("run.list"))
        # run_id order is string order: run-1, run-10, run-11, run-2, ...
        self.assertEqual([p for m, p in paged.calls], [{}, {"after": "run-2"}, {"after": "run-6"}])

    def test_a_cursor_that_does_not_advance_is_a_typed_error(self) -> None:
        cases = {
            "same cursor twice": [{"code": "ok", "runs": [run("b")], "next_after": "b"},
                                  {"code": "ok", "runs": [run("b")], "next_after": "b"}],
            "cursor moves back": [{"code": "ok", "runs": [run("b")], "next_after": "b"},
                                  {"code": "ok", "runs": [run("c")], "next_after": "a"}],
            "cursor is not the last run": [{"code": "ok", "runs": [run("a"), run("b")], "next_after": "a"}],
            "empty page with a cursor": [{"code": "ok", "runs": [], "next_after": "a"}],
        }
        for name, pages in cases.items():
            with self.subTest(name):
                answer = protocol.list_all_runs(Scripted(pages).call)
                self.assertEqual(answer["code"], protocol.LIST_INCOMPLETE)
                self.assertNotIn("runs", answer)

    def test_a_run_listed_twice_or_a_failed_later_page_is_not_shown_as_the_list(self) -> None:
        twice = Scripted([{"code": "ok", "runs": [run("a"), run("b")], "next_after": "b"},
                          {"code": "ok", "runs": [run("b"), run("c")]}])
        self.assertEqual(protocol.list_all_runs(twice.call)["detail"], "run_listed_twice:b")
        failed = Scripted([{"code": "ok", "runs": [run("a")], "next_after": "a"},
                           {"code": "control_api_unavailable", "delivered": "unknown"}])
        self.assertEqual(protocol.list_all_runs(failed.call), {"code": "control_api_unavailable", "delivered": "unknown"})

    def test_the_walk_is_bounded(self) -> None:
        class Endless:
            def __init__(self) -> None:
                self.n = 0

            def call(self, method, params=None):
                self.n += 1
                rid = "run-%09d" % self.n
                return {"code": "ok", "runs": [run(rid)], "next_after": rid}

        endless = Endless()
        old = protocol.LIST_FOLLOW_MAX_PAGES
        protocol.LIST_FOLLOW_MAX_PAGES = 50
        try:
            answer = protocol.list_all_runs(endless.call)
        finally:
            protocol.LIST_FOLLOW_MAX_PAGES = old
        self.assertEqual((answer["code"], answer["detail"], endless.n), (protocol.LIST_INCOMPLETE, "too_many_pages", 50))

    def test_a_stale_page_marks_the_joined_list_stale(self) -> None:
        pages = Scripted([{"code": "ok", "runs": [run("a")], "next_after": "a"},
                          {"code": "ok", "runs": [run("b")], "stale": True}])
        self.assertEqual(protocol.list_all_runs(pages.call), {"code": "ok", "runs": [run("a"), run("b")], "stale": True})


class SurfacesFollowTest(unittest.TestCase):
    def test_cli_list_and_conditions_read_every_page(self) -> None:
        api = api_with_runs(9)
        out = io.StringIO()
        self.assertEqual(cli.main(["list"], Paged(api, 2), out, io.StringIO()), cli.EXIT_OK)
        self.assertEqual(json.loads(out.getvalue()), json.loads(json.dumps(api.call("run.list"))))
        pages = Scripted([{"code": "ok", "runs": [run("a")], "next_after": "a"},
                          {"code": "ok", "runs": [{"run_id": "b", "state": "stop_pending", "stop": {"reason": "x"}}]}])
        out = io.StringIO()
        cli.main(["conditions"], pages, out, io.StringIO())
        self.assertEqual([c["label"] for c in json.loads(out.getvalue())["conditions"]], [protocol.UNRECONCILED])

    def test_cli_list_reports_a_stuck_cursor_as_an_error(self) -> None:
        stuck = Scripted([{"code": "ok", "runs": [run("b")], "next_after": "b"},
                          {"code": "ok", "runs": [run("b")], "next_after": "b"}])
        out = io.StringIO()
        self.assertEqual(cli.main(["list"], stuck, out, io.StringIO()), cli.EXIT_ERROR)
        self.assertEqual(json.loads(out.getvalue())["code"], protocol.LIST_INCOMPLETE)

    def test_tui_list_reads_every_page(self) -> None:
        api = api_with_runs(9)
        paged = Paged(api, 2)
        app = TuiApp(paged, clock=Clock(), environ={"NO_COLOR": "1"})
        app.open()
        self.assertEqual([r["run_id"] for r in app.runs], [r["run_id"] for r in api.call("run.list")["runs"]])
        self.assertEqual(len(paged.calls), 5)
        self.assertIsNone(app.refresh_failed)

    def test_tui_shows_a_stuck_cursor_as_a_failed_read(self) -> None:
        stuck = Scripted([{"code": "ok", "runs": [run("b")], "next_after": "b"},
                          {"code": "ok", "runs": [run("b")], "next_after": "b"}])
        app = TuiApp(stuck, clock=Clock(), environ={"NO_COLOR": "1"})
        app.open()
        self.assertEqual(app.runs, [])
        self.assertIn(protocol.LIST_INCOMPLETE, app.refresh_failed)

    def test_cli_and_tui_keep_one_command_set(self) -> None:
        self.assertEqual(protocol.COMMANDS, ("list", "status", "stop", "start", "conditions"))


if __name__ == "__main__":
    unittest.main()
