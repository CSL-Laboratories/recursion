"""The read and search tools, through the registry as the loop runs them."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import unittest

from recursion_core.loop.providers import base as b
from recursion_core.loop.tests.support import FAKE_KEY, POSIX, context, registry, workspace
from recursion_core.loop.tools import registry as r
from recursion_core.loop.tools import search

GIT = shutil.which("git")


def run(reg, ws, name: str, args: dict, **kw) -> r.Outcome:
    return reg.dispatch(b.ToolCall("toolu_t", name, args, ""), context(ws, **kw))


class Read(unittest.TestCase):
    def test_lines_are_numbered_and_the_read_is_recorded(self) -> None:
        ws = workspace(self, {"src/a.py": "import io\r\nprint(1)\n\nlast"})
        out = run(registry(), ws, "read", {"path": "src/a.py"})
        self.assertEqual(out.status, r.OK)
        self.assertEqual(out.model_text, "     1\timport io\n     2\tprint(1)\n     3\t\n     4\tlast\n")
        mark = ws.ledger.get(ws.root / "src" / "a.py")
        self.assertIsNotNone(mark)
        self.assertEqual(mark.sha256, hashlib.sha256(b"import io\r\nprint(1)\n\nlast").hexdigest())

    def test_offset_and_limit_page_through_with_a_continuation(self) -> None:
        ws = workspace(self, {"f.txt": "".join(f"row {i}\n" for i in range(1, 11))})
        out = run(registry(), ws, "read", {"path": "f.txt", "offset": 4, "limit": 3})
        self.assertEqual(out.model_text, "     4\trow 4\n     5\trow 5\n     6\trow 6\n"
                                         "[Showing lines 4-6 of 10. Use offset=7 to continue.]\n")
        past = run(registry(), ws, "read", {"path": "f.txt", "offset": 11})
        self.assertEqual(past.status, r.ERROR)
        self.assertIn("past the end", past.model_text)

    def test_a_large_file_pages_itself_under_the_cap_and_is_never_cut_twice(self) -> None:
        ws = workspace(self, {"big.txt": "".join(f"{i:06d} " + "q" * 100 + "\n" for i in range(5000))})
        out = run(registry(), ws, "read", {"path": "big.txt"})
        self.assertFalse(out.truncated, "the registry did not have to cut it")
        self.assertNotIn("[output truncated:", out.model_text)
        self.assertLessEqual(len(out.model_text.encode()), r.DEFAULT_MAX_BYTES)
        self.assertRegex(out.model_text, r"\[Showing lines 1-\d+ of 5000\. Use offset=\d+ to continue\.\]\n$")

    def test_a_page_fits_a_smaller_line_cap_too(self) -> None:
        ws = workspace(self, {"f.txt": "".join(f"{i}\n" for i in range(500))})
        out = run(registry(max_lines=50), ws, "read", {"path": "f.txt"})
        self.assertFalse(out.truncated)
        self.assertIn("[Showing lines 1-49 of 500. Use offset=50 to continue.]", out.model_text)

    def test_long_lines_are_cut(self) -> None:
        ws = workspace(self, {"wide.txt": "a" * 5000 + "\n"})
        out = run(registry(), ws, "read", {"path": "wide.txt"})
        self.assertIn("[line cut: 5000 characters]", out.model_text)

    def test_binary_files_are_not_shown(self) -> None:
        ws = workspace(self, {"nul.bin": b"abc\x00def", "latin.txt": ("caf\xe9 " * 100).encode("latin-1")})
        for name in ("nul.bin", "latin.txt"):
            with self.subTest(name=name):
                out = run(registry(), ws, "read", {"path": name})
                self.assertEqual(out.status, r.ERROR)
                self.assertIn("binary file", out.model_text)

    def test_a_folder_is_listed(self) -> None:
        ws = workspace(self, {"pkg/a.py": "", "pkg/sub/b.py": ""})
        out = run(registry(), ws, "read", {"path": "pkg"})
        self.assertEqual(out.model_text, "pkg/ (2 entries)\na.py\nsub/")

    def test_paths_outside_the_workspace_are_refused(self) -> None:
        ws = workspace(self, {"in.txt": "ok"})
        outside = ws.root.parent / f"{ws.root.name}-outside.txt"
        outside.write_text("secret", encoding="utf-8")
        self.addCleanup(outside.unlink)
        cases = ["../" + outside.name, str(outside), "/etc/passwd"]
        if POSIX:
            os.symlink(outside, ws.root / "link.txt")
            cases.append("link.txt")
        for path in cases:
            with self.subTest(path=path):
                out = run(registry(), ws, "read", {"path": path})
                self.assertEqual(out.status, r.ERROR)
                self.assertIn("outside the workspace", out.model_text)

    @unittest.skipUnless(POSIX and hasattr(os, "mkfifo"), "needs a FIFO")
    def test_a_fifo_cannot_hang_the_loop(self) -> None:
        ws = workspace(self)
        os.mkfifo(ws.root / "pipe")
        out = run(registry(), ws, "read", {"path": "pipe"})
        self.assertEqual(out.status, r.ERROR)
        self.assertIn("not a regular file", out.model_text)

    def test_a_spilled_output_can_be_read_back(self) -> None:
        ws = workspace(self)
        ref = ws.spill.put("".join(f"saved {i}\n" for i in range(1, 6)).encode())
        out = run(registry(), ws, "read", {"path": ref, "offset": 5})
        self.assertEqual(out.model_text, "     5\tsaved 5\n")
        missing = run(registry(), ws, "read", {"path": "spill:" + "0" * 64})
        self.assertEqual(missing.status, r.ERROR)

    def test_a_key_in_a_file_is_redacted(self) -> None:
        ws = workspace(self, {".env": f"ANTHROPIC_API_KEY={FAKE_KEY}\n"})
        out = run(registry(), ws, "read", {"path": ".env"}, scrub=lambda s: s.replace(FAKE_KEY, "[redacted]"))
        self.assertNotIn(FAKE_KEY, out.model_text)
        self.assertIn("ANTHROPIC_API_KEY=[redacted]", out.model_text)


class Search(unittest.TestCase):
    def files(self) -> dict:
        return {"src/app.py": "def main():\n    return helper()\n\ndef helper():\n    return 1\n",
                "src/util.ts": "export function helper() {}\n", "README.md": "Call helper() first.\n",
                "data.bin": b"helper\x00binary"}

    def test_matches_come_sorted_as_path_line_text(self) -> None:
        ws = workspace(self, self.files())
        out = run(registry(), ws, "search", {"pattern": r"helper\(\)"})
        self.assertEqual(out.status, r.OK, out.model_text)
        self.assertEqual(out.model_text.splitlines()[:4], ["README.md:1:Call helper() first.",
                                                           "src/app.py:2:    return helper()",
                                                           "src/app.py:4:def helper():",
                                                           "src/util.ts:1:export function helper() {}"])
        self.assertIn("1 binary", out.model_text)

    def test_glob_literal_case_and_files_only(self) -> None:
        ws = workspace(self, self.files())
        reg = registry()
        self.assertEqual(run(reg, ws, "search", {"pattern": "def ", "glob": "*.py"}).model_text,
                         "src/app.py:1:def main():\nsrc/app.py:4:def helper():\n")
        self.assertEqual(run(reg, ws, "search", {"pattern": "helper()", "literal": True, "files_only": True,
                                                 "path": "src"}).model_text, "src/app.py\nsrc/util.ts\n")
        self.assertIn("README.md:1:", run(reg, ws, "search", {"pattern": "CALL", "ignore_case": True}).model_text)

    def test_no_match_and_a_bad_pattern(self) -> None:
        ws = workspace(self, self.files())
        none = run(registry(), ws, "search", {"pattern": "nowhere_to_be_found"})
        self.assertEqual(none.status, r.OK)
        self.assertTrue(none.model_text.startswith("No matches"))
        bad = run(registry(), ws, "search", {"pattern": "("})
        self.assertEqual(bad.status, r.ERROR)
        self.assertIn("not a valid regular expression", bad.model_text)

    def test_results_are_limited_and_say_so(self) -> None:
        ws = workspace(self, {"many.txt": "hit\n" * 50})
        out = run(registry(), ws, "search", {"pattern": "hit", "max_results": 10})
        self.assertEqual(out.model_text.count("many.txt:"), 10)
        self.assertIn("there are more", out.model_text)

    def test_results_fit_a_smaller_line_cap(self) -> None:
        ws = workspace(self, {"many.txt": "hit\n" * 300})
        out = run(registry(max_lines=20), ws, "search", {"pattern": "hit"})
        self.assertFalse(out.truncated, "search kept itself under the cap")
        self.assertEqual(out.model_text.count("many.txt:"), 19)
        self.assertIn("there are more", out.model_text)

    @unittest.skipUnless(POSIX, "needs symbolic links")
    def test_links_leading_outside_are_skipped(self) -> None:
        ws = workspace(self, {"inside.txt": "needle\n"})
        outside = ws.root.parent / f"{ws.root.name}-outside.txt"
        outside.write_text("needle outside\n", encoding="utf-8")
        self.addCleanup(outside.unlink)
        os.symlink(outside, ws.root / "escape.txt")
        out = run(registry(), ws, "search", {"pattern": "needle"})
        self.assertEqual(out.model_text, "inside.txt:1:needle\n[1 files searched; skipped 1 linking outside the "
                                         "workspace.]\n")

    @unittest.skipUnless(GIT, "git is not installed")
    def test_inside_git_ignored_files_are_skipped(self) -> None:
        ws = workspace(self, {".gitignore": "build/\n", "build/out.txt": "needle\n", "kept.txt": "needle\n"})
        env = {"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(ws.root)}
        subprocess.run(["git", "init", "-q", str(ws.root)], check=True, env=env)
        out = run(registry(), ws, "search", {"pattern": "needle"})
        self.assertEqual(out.model_text, "kept.txt:1:needle\n")

    def test_search_stays_inside_the_workspace(self) -> None:
        ws = workspace(self, {"a.txt": "x"})
        out = run(registry(), ws, "search", {"pattern": "x", "path": ".."})
        self.assertEqual(out.status, r.ERROR)
        self.assertIn("outside the workspace", out.model_text)

    def test_the_worker_gets_no_key_and_no_proxy(self) -> None:
        env = search._child_env()
        self.assertEqual(set(env), {"PATH", "LC_ALL"})

    def test_a_runaway_pattern_is_stopped_by_an_interrupt(self) -> None:
        ws = workspace(self, {"evil.txt": "a" * 40 + "!\n"})
        cancel = b.CancelToken()
        request = {"root": str(ws.root), "base": str(ws.root), "pattern": "(a+)+$", "literal": False,
                   "ignore_case": False, "glob": None, "files_only": False, "max_results": 5,
                   "max_line_chars": 500, "max_file_bytes": 1 << 20}
        cancel.cancel()  # interrupted before the worker can finish: it is killed, not waited for
        with self.assertRaises(b.Cancelled):
            search.run_worker(request, cancel)

    def test_a_runaway_pattern_is_stopped_at_the_deadline(self) -> None:
        ws = workspace(self, {"evil.txt": "a" * 40 + "!\n"})
        request = {"root": str(ws.root), "base": str(ws.root), "pattern": "(a+)+$", "literal": False,
                   "ignore_case": False, "glob": None, "files_only": False, "max_results": 5,
                   "max_line_chars": 500, "max_file_bytes": 1 << 20}
        # The pattern never finishes on this line (catastrophic backtracking), so the deadline always fires;
        # the test waits for that, it does not race it.
        with self.assertRaises(Exception) as cm:
            search.run_worker(request, b.CancelToken(), deadline_s=0.5)
        self.assertIn("ran longer than", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
