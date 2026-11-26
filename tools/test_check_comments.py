#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(os.path.abspath(__file__)).parent.parent
SPEC = importlib.util.spec_from_file_location("check_comments", ROOT / "tools" / "check_comments.py")
cc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cc)

OLD = 'import os\n\n\ndef old():\n    """Old docstring.\n\n    More.\n    """\n    # an old comment\n    return 1\n'


class Repo:
    def __init__(self, case: unittest.TestCase):
        tmp = tempfile.TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        self.top = Path(tmp.name)
        self.root = self.top / "repo"
        self.root.mkdir()
        home = self.top / "home"
        home.mkdir()
        config = self.top / "gitconfig"
        config.write_text("", encoding="utf-8")
        env = {
            "HOME": str(home), "USERPROFILE": str(home), "TMPDIR": str(self.top), "TEMP": str(self.top),
            "TMP": str(self.top), "GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_SYSTEM": str(config),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        case.addCleanup(patcher.stop)
        os.environ.pop("CI_BASE_SHA", None)
        self.git("init", "-q", "-b", "master")
        self.cutoff = self.top / "cutoff.json"
        self.record([])

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True,
                              text=True).stdout.strip()

    def write(self, path: str, text: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))

    def commit(self, files: dict[str, str | None], message: str = "c") -> str:
        for path, text in files.items():
            if text is None:
                self.git("rm", "-q", path)
            else:
                self.write(path, text)
                self.git("add", path)
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def record(self, commits: list[str]) -> None:
        self.cutoff.write_text(json.dumps({"format": "csl-comment-cutoff/v1",
                                           "repository": "CSL-Laboratories/recursion",
                                           "recorded_at": "2026-11-18T00:00:00Z",
                                           "commits": sorted(set(commits))}), encoding="utf-8")

    def check(self, base: str, vault: Path | None = None) -> tuple[int, list[str]]:
        return cc.run(self.root, base, "HEAD", self.cutoff, vault)

    def main(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cc.main(list(argv), root=self.root)
        return code, out.getvalue()


def refused(lines: list[str]) -> list[str]:
    return [line for line in lines[1:-1] if not line.startswith("  ")]


class Base(unittest.TestCase):
    def setUp(self):
        self.repo = Repo(self)
        self.base = self.repo.commit({"pkg/mod.py": OLD}, "base")
        self.repo.git("checkout", "-q", "-b", "draft")

    def add(self, text: str, path: str = "pkg/new.py") -> tuple[int, list[str]]:
        self.repo.commit({path: text})
        return self.repo.check(self.base)

    def assertPasses(self, result):
        code, lines = result
        self.assertEqual(code, 0, lines)
        self.assertTrue(lines[0].startswith("PASS: no new comments or docstrings"), lines)

    def assertRefuses(self, result, count):
        code, lines = result
        self.assertEqual(code, 1, lines)
        self.assertEqual(len(refused(lines)), count, lines)
        return lines


class NewCommentTests(Base):
    def test_a_new_comment_line_and_a_trailing_comment_are_refused_with_path_and_line(self):
        lines = self.assertRefuses(self.add("# says what\nx = 1  # and why\n"), 2)
        self.assertIn("pkg/new.py:1: comment: '# says what'", lines)
        self.assertIn("pkg/new.py:2: comment: '# and why'", lines)
        self.assertIn(cc.COMMENT_ADVICE, lines)
        self.assertTrue(lines[0].startswith("FAIL: 2 new comment or docstring line(s)"))

    def test_a_comment_added_to_an_old_file_is_refused(self):
        lines = self.assertRefuses(self.add(OLD + "# late\n", "pkg/mod.py"), 1)
        self.assertIn("pkg/mod.py:11: comment: '# late'", lines)

    def test_tool_marks_interpreter_encoding_licence_and_pointers_pass(self):
        self.assertPasses(self.add(
            "#!/usr/bin/env python3\n"
            "# -*- coding: utf-8 -*-\n"
            "import os  # noqa: E402\n"
            "x = os.sep  # type: ignore[attr-defined]\n"
            "if x:  # pragma: no cover\n"
            "    y = 1  # type: ignore  # noqa: E501\n"
            "# CSLR-D-013\n"
            "z = 2  # CSLR-D-013\n"
            "w = 3  # pyright: ignore[reportGeneralTypeIssues]\n"
            "v = 4  # nosec B101\n"
            "u = 5  # fmt: skip\n"
            "t = 6  # pylint: disable=invalid-name\n"
            "s = 7  # CSLR-I-004\n"
        ))

    def test_an_spdx_line_passes_on_line_one_and_is_refused_later(self):
        self.assertPasses(self.add("# SPDX-License-Identifier: Apache-2.0\nx = 1\n"))
        self.assertRefuses(self.add("x = 1\ny = 2\nz = 3\n# SPDX-License-Identifier: Apache-2.0\n", "pkg/b.py"), 1)

    def test_a_shebang_not_on_line_one_is_refused(self):
        self.assertRefuses(self.add("x = 1\n#!/usr/bin/env python3\n"), 1)

    def test_a_tool_mark_with_text_is_refused_with_the_bare_mark_as_advice(self):
        lines = self.assertRefuses(self.add("if 1:  # pragma: no cover - reason\n    pass\n"), 1)
        self.assertIn("pkg/new.py:1: tool mark with text: '# pragma: no cover - reason'", lines)
        self.assertIn("  keep the mark only: '# pragma: no cover'", lines)

    def test_pointers_of_the_wrong_shape_are_refused(self):
        lines = self.assertRefuses(self.add("a = 1  # CSLR-D-13\nb = 2  # CSLR-X-001\n# CSLR-D-013 because\n"), 3)
        self.assertEqual(lines.count(cc.POINTER_ADVICE), 3, lines)
        self.assertIn("pkg/new.py:3: pointer: '# CSLR-D-013 because'", lines)

    def test_a_string_that_looks_like_a_comment_passes(self):
        self.assertPasses(self.add('x = "# not a comment"\ny = f"# {x}"\n'))

    def test_deleting_comments_and_docstrings_passes(self):
        self.assertPasses(self.add("import os\n\n\ndef old():\n    return 1\n", "pkg/mod.py"))

    def test_deleting_a_whole_file_passes(self):
        self.repo.commit({"pkg/mod.py": None})
        self.assertPasses(self.repo.check(self.base))

    def test_other_languages_are_not_checked(self):
        self.assertPasses(self.add("# a shell comment\necho hi\n", "run.sh"))

    def test_more_than_the_shown_limit_is_cut_with_a_count(self):
        code, lines = self.add("".join(f"x{n} = {n}  # c{n}\n" for n in range(cc.MAX_SHOWN + 5)))
        self.assertEqual(code, 1)
        self.assertEqual(len(refused(lines)), cc.MAX_SHOWN + 1)
        self.assertIn("... and 5 more", lines)


class DocstringTests(Base):
    def test_a_new_function_docstring_and_a_mid_body_string_are_refused(self):
        lines = self.assertRefuses(self.add('def parse():\n    """Parse."""\n    x = 1\n    "why"\n    return x\n'), 2)
        self.assertIn("pkg/new.py:2: docstring of function 'parse' (lines 2-2)", lines)
        self.assertIn("pkg/new.py:4: string statement in 'parse' (lines 4-4)", lines)
        self.assertIn(cc.DOCSTRING_ADVICE, lines)

    def test_class_and_module_docstrings_are_refused(self):
        lines = self.assertRefuses(self.add('"""Module."""\n\n\nclass A:\n    """A.\n\n    More.\n    """\n'), 2)
        self.assertIn("pkg/new.py:1: docstring of module (lines 1-1)", lines)
        self.assertIn("pkg/new.py:5: docstring of class 'A' (lines 5-8)", lines)

    def test_a_module_docstring_passes_where_the_file_reads_it(self):
        self.assertPasses(self.add('"""Tool help."""\nimport argparse\nargparse.ArgumentParser(description=__doc__)\n'))

    def test_an_object_docstring_passes_where_the_file_reads_it(self):
        self.assertPasses(self.add(
            'class A:\n    """A."""\n\n    def run(self):\n        """Run."""\n\n\n'
            "print(A.__doc__, A.run.__doc__)\n"))

    def test_a_docstring_not_read_by_the_file_is_still_refused(self):
        self.assertRefuses(self.add('class A:\n    """A."""\n\n\nclass B:\n    """B."""\n\n\nprint(A.__doc__)\n'), 1)

    def test_plugin_api_docstrings_pass_and_private_methods_are_refused(self):
        pkg = "core/recursion_core/plugin_security/"
        self.repo.commit({
            pkg + "__init__.py": '"""Plugin API."""\nfrom .thing import Thing, helper\n\n__all__ = ["Thing", "helper"]\n',
            pkg + "thing.py": (
                'class Thing:\n    """A thing."""\n\n    def go(self):\n        """Go."""\n\n'
                '    def _hidden(self):\n        """Hidden."""\n\n\n'
                'def helper():\n    """Help."""\n\n\ndef other():\n    """Other."""\n'),
            pkg + "tests/test_thing.py": 'def test_x():\n    """Test."""\n',
        })
        lines = self.assertRefuses(self.repo.check(self.base), 3)
        self.assertIn(f"{pkg}thing.py:8: docstring of function 'Thing._hidden' (lines 8-8)", lines)
        self.assertIn(f"{pkg}thing.py:16: docstring of function 'other' (lines 16-16)", lines)
        self.assertIn(f"{pkg}tests/test_thing.py:2: docstring of function 'test_x' (lines 2-2)", lines)

    def test_a_listed_docstring_passes(self):
        path, name = "core/recursion_core/tui/sessions.py", "Transcript.apply"
        self.assertIn((path, name), cc.DOCSTRINGS_READ_ELSEWHERE)
        self.assertPasses(self.add('class Transcript:\n    def apply(self):\n        """Apply."""\n', path))
        self.assertEqual(len(cc.DOCSTRINGS_READ_ELSEWHERE), 15)

    def test_one_changed_line_inside_an_old_docstring_is_refused(self):
        lines = self.assertRefuses(self.add(OLD.replace("More.", "More, edited."), "pkg/mod.py"), 1)
        self.assertIn("pkg/mod.py:5: docstring of function 'old' (lines 5-8)", lines)


class HistoryTests(Base):
    def test_a_recorded_draft_passes_after_master_is_merged_and_a_later_comment_is_refused(self):
        draft = self.repo.commit({"pkg/new.py": "x = 1  # from the draft\n"})
        self.assertRefuses(self.repo.check(self.base), 1)
        self.repo.record([draft])
        self.assertPasses(self.repo.check(self.base))
        self.repo.git("checkout", "-q", "master")
        master = self.repo.commit({"pkg/other.py": "y = 2\n"}, "master moves")
        self.repo.git("checkout", "-q", "draft")
        self.repo.git("merge", "-q", "--no-ff", "-m", "merge master", "master")
        self.assertPasses(self.repo.check(master))
        self.repo.commit({"pkg/new.py": "x = 1  # from the draft\nz = 3  # later\n"})
        lines = self.assertRefuses(self.repo.check(master), 1)
        self.assertIn("pkg/new.py:2: comment: '# later'", lines)

    def test_master_comments_merged_into_a_draft_are_not_new(self):
        self.repo.git("checkout", "-q", "master")
        master = self.repo.commit({"pkg/other.py": "y = 2  # landed on master\n"}, "master moves")
        self.repo.git("checkout", "-q", "draft")
        self.repo.commit({"pkg/new.py": "x = 1\n"})
        self.repo.git("merge", "-q", "--no-ff", "-m", "merge master", "master")
        self.assertPasses(self.repo.check(master))
        self.assertRefuses(self.repo.check(self.base), 1)
        self.repo.git("update-ref", "refs/remotes/origin/master", master)
        self.assertPasses(self.repo.check(self.base))

    def test_a_cutoff_commit_missing_locally_is_skipped_and_counted(self):
        self.repo.record(["f" * 40])
        code, lines = self.add("x = 1  # new\n")
        self.assertEqual(code, 1)
        self.assertIn("1 commits, 1 not present locally", lines[-1])


class FailureTests(Base):
    def test_a_file_that_does_not_tokenize_fails_naming_it(self):
        code, lines = self.add("def broken(:\n    pass\n", "pkg/bad.py")
        self.assertEqual(code, 1)
        self.assertTrue(lines[0].startswith("FAIL: cannot read pkg/bad.py at HEAD:"), lines)

    def test_a_base_that_names_no_commit_fails(self):
        code, out = self.repo.main("--base", "no-such-rev", "--cutoff", str(self.repo.cutoff))
        self.assertEqual(code, 1)
        self.assertIn("FAIL: no commit named 'no-such-rev'", out)

    def test_no_base_skips_and_exits_zero(self):
        code, out = self.repo.main("--cutoff", str(self.repo.cutoff))
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "SKIP check_comments (no CI_BASE_SHA; pass --base origin/master)")

    def test_the_base_comes_from_ci_base_sha(self):
        self.repo.commit({"pkg/new.py": "x = 1  # new\n"})
        with mock.patch.dict(os.environ, {"CI_BASE_SHA": self.base}):
            code, out = self.repo.main("--cutoff", str(self.repo.cutoff))
        self.assertEqual(code, 1, out)
        self.assertIn(f"base {self.base[:8]}", out)

    def test_a_cutoff_with_a_bad_entry_fails(self):
        for commits in (["abc"], ["b" * 40, "a" * 40], ["a" * 40, "a" * 40], ["A" * 40]):
            self.repo.cutoff.write_text(json.dumps({"format": "csl-comment-cutoff/v1", "repository": "r",
                                                    "recorded_at": "t", "commits": commits}), encoding="utf-8")
            code, out = self.repo.main("--base", self.base, "--cutoff", str(self.repo.cutoff))
            self.assertEqual(code, 1, commits)
            self.assertIn("FAIL: cutoff", out)

    def test_a_cutoff_with_a_wrong_format_or_keys_fails(self):
        for data in ({"format": "x", "repository": "r", "recorded_at": "t", "commits": []}, {"commits": []}, []):
            self.repo.cutoff.write_text(json.dumps(data), encoding="utf-8")
            code, out = self.repo.main("--base", self.base, "--cutoff", str(self.repo.cutoff))
            self.assertEqual(code, 1, data)
            self.assertIn("FAIL: cutoff", out)

    def test_a_pass_through_main_prints_pass(self):
        self.repo.commit({"pkg/new.py": "x = 1\n"})
        code, out = self.repo.main("--base", self.base, "--cutoff", str(self.repo.cutoff))
        self.assertEqual(code, 0, out)
        self.assertTrue(out.startswith("PASS: no new comments or docstrings (1 files, 1 added lines checked)"), out)


class VaultTests(Base):
    def make_vault(self, ids: list[str]) -> Path:
        vault = self.repo.top / "vault"
        vault.mkdir()
        run = lambda *a: subprocess.run(["git", "-C", str(vault), *a], check=True, capture_output=True)
        run("init", "-q", "-b", "main")
        for ident in ids:
            record = vault / "submissions" / ident / "record.json"
            record.parent.mkdir(parents=True)
            record.write_text("{}", encoding="utf-8")
        run("add", "-A")
        run("commit", "-q", "--allow-empty", "-m", "v")
        run("update-ref", "refs/remotes/origin/main", "HEAD")
        return vault

    def test_vault_checks_that_each_new_pointer_has_a_record(self):
        vault = self.make_vault(["CSLR-D-013"])
        self.repo.commit({"pkg/new.py": "a = 1  # CSLR-D-013\nb = 2  # CSLR-D-014\n"})
        code, lines = self.repo.check(self.base, vault)
        self.assertEqual(code, 1, lines)
        self.assertIn("pkg/new.py:2: pointer: '# CSLR-D-014'", lines)
        self.assertIn(f"  no record submissions/CSLR-D-014/record.json at {vault} origin/main", lines)
        self.assertEqual(len(refused(lines)), 1)
        self.assertPasses(self.repo.check(self.base))


class WriteCutoffTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.top = Path(tmp.name)

    def write(self, *ids: str) -> tuple[int, str, Path]:
        heads = self.top / "heads.txt"
        heads.write_text("\n".join(ids) + "\n", encoding="utf-8")
        cutoff = self.top / "out.json"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cc.main(["--write-cutoff", str(heads), "--cutoff", str(cutoff)], root=self.top)
        return code, out.getvalue(), cutoff

    def test_write_cutoff_sorts_and_dedups(self):
        code, out, cutoff = self.write("b" * 40, "a" * 40, "b" * 40)
        self.assertEqual(code, 0, out)
        data = json.loads(cutoff.read_text(encoding="utf-8"))
        self.assertEqual(data["commits"], ["a" * 40, "b" * 40])
        self.assertEqual(data["format"], "csl-comment-cutoff/v1")
        self.assertEqual(data["repository"], "CSL-Laboratories/recursion")
        self.assertEqual(cc.read_cutoff(cutoff), ["a" * 40, "b" * 40])

    def test_write_cutoff_refuses_a_bad_id(self):
        code, out, cutoff = self.write("a" * 40, "nothex")
        self.assertEqual(code, 1)
        self.assertIn("FAIL: not a 40-hex commit id: 'nothex'", out)
        self.assertFalse(cutoff.exists())


class SelfTests(unittest.TestCase):
    def test_the_tool_and_its_test_follow_the_rule(self):
        api = {}
        for name in ("check_comments.py", "test_check_comments.py"):
            path = f"tools/{name}"
            found = cc.candidates(path, (ROOT / path).read_bytes(), api)
            self.assertEqual(found, [], name)

    def test_the_recorded_cutoff_is_valid(self):
        self.assertTrue(cc.read_cutoff(ROOT / "tools" / "comment_cutoff.json"))


class ThreadAnswerTests(Base):
    def test_a_clean_file_with_a_space_in_its_name_passes(self):
        self.assertPasses(self.add("x = 1\n", "tools/local helper.py"))

    def test_a_comment_in_a_file_with_a_space_in_its_name_is_refused(self):
        lines = self.assertRefuses(self.add("x = 1  # new\n", "tools/local helper.py"), 1)
        self.assertIn("tools/local helper.py:1: comment: '# new'", lines)

    def test_a_comment_is_refused_when_git_drops_the_diff_prefixes(self):
        self.repo.git("config", "diff.noprefix", "true")
        lines = self.assertRefuses(self.add("# a new comment\nx = 1\n"), 1)
        self.assertIn("pkg/new.py:1: comment: '# a new comment'", lines)

    def test_a_python_file_git_calls_binary_is_refused_naming_it(self):
        target = self.repo.root / "pkg" / "u16.py"
        target.write_bytes("# a comment\nx = 1\n".encode("utf-16"))
        self.repo.git("add", "pkg/u16.py")
        self.repo.git("commit", "-q", "-m", "u16")
        code, lines = self.repo.check(self.base)
        self.assertEqual(code, 1, lines)
        self.assertTrue(lines[0].startswith("FAIL: cannot read pkg/u16.py at HEAD:"), lines)

    def test_a_class_body_read_of_doc_allows_that_class_docstring(self):
        self.assertPasses(self.add('class Command:\n    """Help."""\n    help_text = __doc__\n'))

    def test_a_class_body_read_of_doc_does_not_allow_the_module_docstring(self):
        lines = self.assertRefuses(self.add('"""Module."""\n\n\nclass Command:\n    """Help."""\n    help_text = __doc__\n'), 1)
        self.assertIn("pkg/new.py:1: docstring of module (lines 1-1)", lines)

    def test_a_read_of_doc_inside_a_method_allows_the_module_docstring(self):
        self.assertPasses(self.add('"""Module."""\n\n\nclass C:\n    def run(self):\n        return __doc__\n'))

    def test_assigning_doc_allows_no_module_docstring(self):
        lines = self.assertRefuses(self.add('"""Module doc."""\n__doc__ = "replacement"\n'), 1)
        self.assertIn("pkg/new.py:1: docstring of module (lines 1-1)", lines)

    def test_assigning_an_object_doc_allows_no_docstring(self):
        lines = self.assertRefuses(self.add('class A:\n    """A."""\n\n\nA.__doc__ = "replacement"\n'), 1)
        self.assertIn("pkg/new.py:2: docstring of class 'A' (lines 2-2)", lines)

    def test_an_aliased_plugin_api_export_allows_the_source_definition(self):
        pkg = "core/recursion_core/plugin_security/"
        self.repo.commit({
            pkg + "__init__.py": '"""Plugin API."""\nfrom .thing import Thing as PublicThing\n\n__all__ = ["PublicThing"]\n',
            pkg + "thing.py": (
                'class Thing:\n    """Doc."""\n\n    def run(self):\n        """Run."""\n\n'
                '    def _hidden(self):\n        """Hidden."""\n'),
        })
        lines = self.assertRefuses(self.repo.check(self.base), 1)
        self.assertIn(f"{pkg}thing.py:8: docstring of function 'Thing._hidden' (lines 8-8)", lines)

    def test_a_trailing_interpreter_mark_on_line_one_is_refused(self):
        lines = self.assertRefuses(self.add("value = 1  #! temporary note\n"), 1)
        self.assertIn("pkg/new.py:1: comment: '#! temporary note'", lines)

    def test_an_encoding_line_after_code_on_line_one_is_refused(self):
        lines = self.assertRefuses(self.add("value = 1\n# coding: utf-8\n"), 1)
        self.assertIn("pkg/new.py:2: comment: '# coding: utf-8'", lines)

    def test_a_trailing_encoding_comment_on_line_one_is_refused(self):
        lines = self.assertRefuses(self.add("value = 1  # coding: utf-8\n"), 1)
        self.assertIn("pkg/new.py:1: comment: '# coding: utf-8'", lines)

    def test_an_encoding_line_after_a_blank_first_line_passes(self):
        self.assertPasses(self.add("\n# coding: utf-8\nx = 1\n"))


if __name__ == "__main__":
    unittest.main()
