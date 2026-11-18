#!/usr/bin/env python3
"""Tests for tools/scan_scratch_paths.py (CSL-238).

Every fixture is written at run time inside a temporary directory this file makes;
no test scans the repository tree. The scratch-path forms are built from pieces so
that this file itself holds none of them.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "scan_scratch_paths.py"
SPEC = importlib.util.spec_from_file_location("scan_scratch_paths", SCRIPT)
scan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scan)

ENGINE = "grok"
VAR_FORM = "/" + "var" + "/" + "tmp" + "/" + ENGINE + "-"
TMP_FORM = "/" + "tmp" + "/" + ENGINE + "-"
MARKER = "scratch-path: " + "allowed"


class ScanScratchPathsTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, name: str, text: str) -> str:
        path = self.dir / name
        path.write_bytes(text.encode("utf-8"))
        return str(path)

    def run_main(self, *paths: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = scan.main(list(paths))
        return code, out.getvalue(), err.getvalue()

    def test_var_tmp_form_is_a_hit_with_its_line_number(self) -> None:
        path = self.write("a.py", "x = 1\ny = '" + VAR_FORM + "cog/x'\n")
        code, out, _ = self.run_main(path)
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), [f"{path}:2"])

    def test_tmp_form_is_a_hit(self) -> None:
        path = self.write("b.py", "d = '" + TMP_FORM + "cog'\n")
        code, out, _ = self.run_main(path)
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), [f"{path}:1"])

    def test_marked_line_is_not_a_hit(self) -> None:
        path = self.write("c.py", "d = '" + VAR_FORM + "cog'  # " + MARKER + "\n")
        self.assertEqual(self.run_main(path), (0, "", ""))

    def test_engine_name_form_is_not_a_hit(self) -> None:
        text = "engine = '" + ENGINE + "-build'\nname = '" + ENGINE + "-cog'\n"
        path = self.write("d.py", text)
        self.assertEqual(self.run_main(path), (0, "", ""))

    def test_plain_tmp_paths_without_the_scratch_name_are_not_hits(self) -> None:
        def form(*parts: str) -> str:
            return "/" + "/".join(parts)

        text = (
            "a = '" + form("tmp", "other") + "'\n"
            "b = '" + form("var", "tmp", "x-" + ENGINE) + "'\n"
            "c = '" + form("tmp", ENGINE + "file") + "'\n"
            "d = '" + form("var", "tmp", ENGINE + "_x") + "'\n"
        )
        path = self.write("e.py", text)
        self.assertEqual(self.run_main(path), (0, "", ""))

    def test_empty_file_is_not_a_hit(self) -> None:
        path = self.write("f.py", "")
        self.assertEqual(self.run_main(path), (0, "", ""))

    def test_no_paths_is_no_hit(self) -> None:
        self.assertEqual(self.run_main(), (0, "", ""))

    def test_every_hit_in_every_file_is_printed_in_order(self) -> None:
        first = self.write("g.py", VAR_FORM + "a\nok\r\n" + TMP_FORM + "b\r\n")
        second = self.write("h.py", "ok\n")
        third = self.write("i.py", "\n\n" + TMP_FORM + "c")
        code, out, _ = self.run_main(first, second, third)
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), [f"{first}:1", f"{first}:3", f"{third}:3"])

    def test_missing_file_is_refused_with_exit_2(self) -> None:
        missing = str(self.dir / "absent.py")
        code, out, err = self.run_main(missing)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("absent.py", err)

    def test_command_line_exit_codes(self) -> None:
        hit = self.write("j.py", TMP_FORM + "x\n")
        clean = self.write("k.py", "clean\n")
        env = dict(os.environ, TMPDIR=str(self.dir))
        for args, want in (([hit], 1), ([clean], 0)):
            proc = subprocess.run(
                [sys.executable, "-X", "utf8", "-B", str(SCRIPT), *args],
                capture_output=True, text=True, env=env, timeout=60, check=False,
            )
            self.assertEqual(proc.returncode, want, proc.stderr)

    def test_nested_tmp_and_var_tmp_paths_are_not_hits(self) -> None:
        # A tmp folder inside another path is not a scratch root: no hit, exit 0.
        repo = "/" + "/".join(("home", "u", "repos", "recursion"))
        lines = (
            "a = '" + repo + TMP_FORM + "build'",
            "b = '" + "/" + "srv" + VAR_FORM + "y'",
            "c = '" + "~" + TMP_FORM + "z'",
            "d = '" + "." + TMP_FORM + "a'",
            "e = " + repo + VAR_FORM + "b",
        )
        path = self.write("l.py", "\n".join(lines) + "\n")
        self.assertEqual(self.run_main(path), (0, "", ""))

    def test_a_form_at_the_start_of_a_path_token_is_still_a_hit(self) -> None:
        lines = (
            VAR_FORM + "a",
            "TMPDIR=" + TMP_FORM + "b",
            'path = "' + VAR_FORM + 'c"',
            "run --dir " + TMP_FORM + "d",
            "PATH=x:" + TMP_FORM + "e",
            "url = file://" + TMP_FORM + "f",
            "(" + VAR_FORM + "g)",
        )
        path = self.write("m.py", "\n".join(lines) + "\n")
        code, out, _ = self.run_main(path)
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), [f"{path}:{n}" for n in range(1, len(lines) + 1)])


if __name__ == "__main__":
    unittest.main()
