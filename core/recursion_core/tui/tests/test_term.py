"""The terminal entry point without a terminal: line mode for screen readers and pipes."""

from __future__ import annotations

import io
import unittest

from recursion_core.tui import term
from recursion_core.tui.tests.fakes import FakeDriver


class PlainModeTest(unittest.TestCase):
    def test_not_a_tty_runs_line_mode_and_closing_stops_nothing(self) -> None:
        driver = FakeDriver()
        run_id = driver.start_run()
        out = io.StringIO()
        code = term.main([], lambda: driver.api, stdin=io.StringIO(f"status {run_id}\nq\nstop {run_id}\n"),
                         stdout=out, environ={})
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn(f"answer run.status status=ok", text)
        self.assertNotIn("run.stop", text, "lines after q are not read")
        self.assertNotIn("\x1b", text)
        self.assertEqual(driver.registry.get(run_id)["state"], "running")
        self.assertEqual(driver.model_calls, 1)

    def test_no_api_client_is_a_text_error(self) -> None:
        out = io.StringIO()
        self.assertEqual(term.main(["--plain"], None, stdin=io.StringIO(""), stdout=out, environ={}), 1)
        self.assertIn("status=control_api_unavailable", out.getvalue())


if __name__ == "__main__":
    unittest.main()
