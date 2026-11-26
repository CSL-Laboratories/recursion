"""Tests for the example script embedded in docs/scheduled-runs.md."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

_SH = shutil.which("sh")

_STANDIN_SCRIPT = """#!/bin/sh
set -eu
n=0
if [ -f "$FAKE_COUNT" ]; then
  n=$(cat "$FAKE_COUNT")
fi
n=$(( n + 1 ))
printf '%s\\n' "$n" > "$FAKE_COUNT"
printf '%s\\n' "$*" >> "$FAKE_ARGS"
line=$(sed -n "${n}p" "$FAKE_RESPONSES")
if [ -z "$line" ]; then
  line=$(tail -n 1 "$FAKE_RESPONSES")
fi
rc=${line%%|*}
body=${line#*|}
if [ -n "$body" ]; then
  printf '%s\\n' "$body"
fi
exit "$rc"
"""

_FAKE_DATE_SCRIPT = """#!/bin/sh
set -eu
if [ "$#" -eq 1 ] && [ "$1" = "+%s" ]; then
  cat "$FAKE_CLOCK"
  exit 0
fi
exec /bin/date "$@"
"""

_FAKE_SLEEP_SCRIPT = """#!/bin/sh
set -eu
cur=$(cat "$FAKE_CLOCK")
inc="$1"
printf '%s\\n' "$(( cur + inc ))" > "$FAKE_CLOCK"
"""


def _find_doc() -> Path:
    for parent in Path(os.path.abspath(__file__)).parents:
        candidate = parent / "docs" / "scheduled-runs.md"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"docs/scheduled-runs.md not found above {__file__}")


def _extract_example_script() -> str:
    text = _find_doc().read_text(encoding="utf-8")
    blocks = re.findall(r"```sh\n(.*?)\n```", text, re.DOTALL)
    if len(blocks) != 1:
        raise AssertionError(f"expected 1 sh block in docs/scheduled-runs.md, found {len(blocks)}")
    return blocks[0] + "\n"


@unittest.skipIf(_SH is None, "POSIX sh is required to check the example script")
class ScheduledRunsScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.script = self.tmp / "recursion-run-routine"
        self.script.write_text(_extract_example_script(), encoding="utf-8")
        self.script.chmod(self.script.stat().st_mode | stat.S_IXUSR)

        self.standin = self.tmp / "fake-recursion-control"
        self.standin.write_text(_STANDIN_SCRIPT, encoding="utf-8")
        self.standin.chmod(self.standin.stat().st_mode | stat.S_IXUSR)

        self.routines = self.tmp / "routines"
        self.routines.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_routine(
        self,
        responses: list[str],
        *,
        wait: str | None,
        pause: str | None = "0",
        fake_clock: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], int, list[str]]:
        lines = [
            "ROUTINE_TENANT=demo",
            "ROUTINE_ENGINE=claude_code",
            "ROUTINE_PROMPT='Review yesterday'",
            "ROUTINE_SLOT=%Y%m%d",
            "export RECURSION_CONTROL='fake-recursion-control'",
        ]
        if wait is not None:
            lines.append(f"ROUTINE_WAIT={wait}")
        if pause is not None:
            lines.append(f"ROUTINE_PAUSE={pause}")
        (self.routines / "nightly.env").write_text("\n".join(lines) + "\n", encoding="utf-8")

        count_file = self.tmp / "count"
        args_file = self.tmp / "args"
        count_file.unlink(missing_ok=True)
        args_file.unlink(missing_ok=True)
        responses_file = self.tmp / "responses"
        responses_file.write_text("\n".join(responses) + "\n", encoding="utf-8")

        env = {
            "PATH": f"{self.tmp}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(self.tmp),
            "RECURSION_ROUTINES_DIR": str(self.routines),
            "FAKE_COUNT": str(count_file),
            "FAKE_ARGS": str(args_file),
            "FAKE_RESPONSES": str(responses_file),
        }
        fake_date = self.tmp / "date"
        fake_sleep = self.tmp / "sleep"
        clock_file = self.tmp / "clock"
        if fake_clock:
            clock_file.write_text("100\n", encoding="utf-8")
            fake_date.write_text(_FAKE_DATE_SCRIPT, encoding="utf-8")
            fake_date.chmod(fake_date.stat().st_mode | stat.S_IXUSR)
            fake_sleep.write_text(_FAKE_SLEEP_SCRIPT, encoding="utf-8")
            fake_sleep.chmod(fake_sleep.stat().st_mode | stat.S_IXUSR)
            env["FAKE_CLOCK"] = str(clock_file)
        else:
            fake_date.unlink(missing_ok=True)
            fake_sleep.unlink(missing_ok=True)
            clock_file.unlink(missing_ok=True)
        assert _SH is not None
        proc = subprocess.run(
            [_SH, str(self.script), "nightly"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        calls = int(count_file.read_text(encoding="utf-8").strip()) if count_file.exists() else 0
        arg_lines = args_file.read_text(encoding="utf-8").splitlines() if args_file.exists() else []
        return proc, calls, arg_lines

    def test_script_syntax_and_no_default_wait(self) -> None:
        assert _SH is not None
        proc = subprocess.run([_SH, "-n", str(self.script)], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        body = self.script.read_text(encoding="utf-8")
        # The wait has no non-zero default. The pause has a first value, in one place only.
        self.assertNotRegex(body, r"ROUTINE_WAIT:-[1-9]")
        self.assertEqual(body.count("ROUTINE_PAUSE:-"), 1)

    def test_retries_control_api_unavailable_twice_then_starts(self) -> None:
        responses = [
            '1|{"code": "control_api_unavailable", "delivered": false}',
            '1|{"code": "control_api_unavailable", "delivered": false}',
            '0|{"code": "started", "run": {"run_id": "run-1"}, "duplicate": false}',
        ]
        proc, calls, arg_lines = self._run_routine(responses, wait="10", pause="0")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(calls, 3)
        self.assertEqual(json.loads(proc.stdout)["code"], "started")
        self.assertEqual(len(set(arg_lines)), 1)

    def test_routine_wait_zero_exits_one_at_once(self) -> None:
        responses = [
            '1|{"code": "control_api_unavailable", "delivered": false}',
            '0|{"code": "started", "run": {"run_id": "run-1"}, "duplicate": false}',
        ]
        proc, calls, _ = self._run_routine(responses, wait="0", pause="0")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(calls, 1)
        self.assertEqual(json.loads(proc.stdout)["code"], "control_api_unavailable")

    def test_unset_routine_wait_defaults_to_no_wait(self) -> None:
        responses = [
            '1|{"code": "control_api_unavailable", "delivered": false}',
            '0|{"code": "started", "run": {"run_id": "run-1"}, "duplicate": false}',
        ]
        proc, calls, _ = self._run_routine(responses, wait=None, pause=None)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(calls, 1)

    def test_different_answer_does_not_retry(self) -> None:
        for answer in (
            '1|{"code": "start_failed", "run_id": "run-1", "detail": "RuntimeError", "recorded": true}',
            '1|{"code": "request_invalid", "detail": "control_api_unavailable"}',
        ):
            with self.subTest(answer=answer):
                responses = [
                    answer,
                    '0|{"code": "started", "run": {"run_id": "run-1"}, "duplicate": false}',
                ]
                proc, calls, _ = self._run_routine(responses, wait="10", pause="0")
                self.assertEqual(proc.returncode, 1)
                self.assertEqual(calls, 1)
                self.assertEqual(proc.stdout.strip(), answer.split("|", 1)[1])

    def test_usage_error_exit_two_does_not_retry(self) -> None:
        responses = [
            "2|",
            '0|{"code": "started", "run": {"run_id": "run-1"}, "duplicate": false}',
        ]
        proc, calls, _ = self._run_routine(responses, wait="10", pause="0")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(calls, 1)

    def test_a_usage_error_that_carries_the_unavailable_code_is_returned_at_once(self) -> None:
        responses = [
            '2|{"code": "control_api_unavailable"}',
            '0|{"code": "started", "duplicate": false}',
        ]
        proc, calls, _ = self._run_routine(responses, wait="10", pause="0")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(calls, 1)

    def test_compact_json_control_api_unavailable_retries(self) -> None:
        responses = [
            '1|{"code":"control_api_unavailable","delivered":false}',
            '0|{"code":"started","duplicate":false}',
        ]
        proc, calls, _ = self._run_routine(responses, wait="10", pause="0")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(calls, 2)

    def test_wait_expires_returns_unavailable(self) -> None:
        responses = ['1|{"code": "control_api_unavailable", "delivered": false}']
        proc, calls, _ = self._run_routine(responses, wait="2", pause="1")
        self.assertEqual(proc.returncode, 1)
        self.assertGreaterEqual(calls, 2)
        self.assertLessEqual(calls, 4)
        self.assertEqual(json.loads(proc.stdout)["code"], "control_api_unavailable")

    def test_wait_without_a_pause_does_not_spin(self) -> None:
        responses = ['1|{"code": "control_api_unavailable", "delivered": false}']
        proc, calls, _ = self._run_routine(responses, wait="1", pause=None)
        self.assertEqual(proc.returncode, 1)
        self.assertGreaterEqual(calls, 1)
        # With the first pause value a one second wait allows two or three tries; a spin makes hundreds.
        self.assertLessEqual(calls, 5)
        self.assertEqual(json.loads(proc.stdout)["code"], "control_api_unavailable")

    def test_a_pause_of_zero_that_is_written_is_honoured(self) -> None:
        responses = ['1|{"code": "control_api_unavailable", "delivered": false}']
        proc, calls, _ = self._run_routine(responses, wait="2", pause="0")
        self.assertEqual(proc.returncode, 1)
        self.assertGreaterEqual(calls, 2)
        self.assertEqual(json.loads(proc.stdout)["code"], "control_api_unavailable")

    def test_a_pause_of_zero_with_no_wait_makes_one_try(self) -> None:
        responses = ['1|{"code": "control_api_unavailable", "delivered": false}']
        proc, calls, _ = self._run_routine(responses, wait="0", pause="0")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(calls, 1)

    def test_a_number_the_shell_cannot_count_is_refused(self) -> None:
        responses = ['1|{"code": "control_api_unavailable", "delivered": false}']
        too_long = "9" * 19
        cases = {
            "wait": dict(wait=too_long, pause=None),
            "pause": dict(wait="5", pause=too_long),
        }
        for name, kwargs in cases.items():
            with self.subTest(name=name):
                proc, calls, _ = self._run_routine(responses, **kwargs)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertEqual(calls, 0)
                self.assertEqual(proc.stdout, "")
                self.assertIn("cannot count", proc.stderr)

    def test_eighteen_digits_are_counted(self) -> None:
        responses = [
            '1|{"code": "control_api_unavailable", "delivered": false}',
            '1|{"code": "control_api_unavailable", "delivered": false}',
            '0|{"code": "started", "duplicate": false}',
        ]
        proc, calls, _ = self._run_routine(responses, wait="9" * 18, pause="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(calls, 3)

    def test_no_try_begins_at_or_after_the_deadline(self) -> None:
        responses = ['1|{"code": "control_api_unavailable", "delivered": false}']
        proc, calls, _ = self._run_routine(responses, wait="3", pause="1", fake_clock=True)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(calls, 3)
        self.assertEqual(json.loads(proc.stdout)["code"], "control_api_unavailable")


if __name__ == "__main__":
    unittest.main()
