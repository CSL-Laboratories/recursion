"""The Windows plugin-read decision on faked handle facts; the decision opens no file and makes no OS call.

One case per refusal row, plus the typed-root case (a root spelled as typed, not as its handle
reports it, refuses) and the pinned-root case (a root reached through a junction is pinned by its
own final path). Every value is fake. The purity tests only read this package's source text.
"""

from __future__ import annotations

import ast
import dataclasses
import unittest
from pathlib import Path

from recursion_core.plugin_security import windows_read_checks as w

VOLUME_A = "\\\\?\\Volume{0f0f0f0f-0000-4000-8000-00000000000a}"  # a fake volume name, never opened
VOLUME_B = "\\\\?\\Volume{0f0f0f0f-0000-4000-8000-00000000000b}"
ROOT = VOLUME_A + "\\Users\\Op\\Plugins"
NAME = "alpha\\plugin.py"


def file_facts(**changes):
    base = w.WindowsHandleFacts(
        attributes=w.FILE_ATTRIBUTE_ARCHIVE, reparse_tag=0, link_count=1, file_type=w.FILE_TYPE_DISK,
        final_path=ROOT + "\\" + NAME, root_final_path=ROOT, expected_name=NAME, owner_ok=True)
    return dataclasses.replace(base, **changes)


def dir_facts(**changes):
    base = w.WindowsHandleFacts(
        attributes=w.FILE_ATTRIBUTE_DIRECTORY, reparse_tag=0, link_count=1, file_type=w.FILE_TYPE_DISK,
        final_path=ROOT + "\\alpha", root_final_path=ROOT, expected_name="alpha", owner_ok=True)
    return dataclasses.replace(base, **changes)


class AcceptedHandleTests(unittest.TestCase):
    def test_clean_plugin_file_is_accepted(self):
        self.assertIsNone(w.judge_handle(file_facts(), w.KIND_FILE))

    def test_clean_directory_step_is_accepted(self):
        self.assertIsNone(w.judge_handle(dir_facts(), w.KIND_DIRECTORY))

    def test_facts_are_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            file_facts().owner_ok = False


class ReparseRowTests(unittest.TestCase):
    def test_any_reparse_tag_is_refused(self):
        for tag in (w.IO_REPARSE_TAG_SYMLINK, w.IO_REPARSE_TAG_MOUNT_POINT, 0x8000001B, 0x9000001A, 1):
            with self.subTest(tag=hex(tag)):
                self.assertEqual(w.judge_handle(file_facts(reparse_tag=tag), w.KIND_FILE), "plugin_file_unreadable")
                self.assertEqual(w.judge_handle(dir_facts(reparse_tag=tag), w.KIND_DIRECTORY),
                                 "plugin_directory_untrusted")

    def test_reparse_attribute_with_tag_zero_is_refused(self):
        attrs = w.FILE_ATTRIBUTE_ARCHIVE | w.FILE_ATTRIBUTE_REPARSE_POINT
        self.assertEqual(w.judge_handle(file_facts(attributes=attrs), w.KIND_FILE), "plugin_file_unreadable")
        attrs = w.FILE_ATTRIBUTE_DIRECTORY | w.FILE_ATTRIBUTE_REPARSE_POINT
        self.assertEqual(w.judge_handle(dir_facts(attributes=attrs), w.KIND_DIRECTORY), "plugin_directory_untrusted")


class FinalPathRowTests(unittest.TestCase):
    def test_ancestor_junction_changes_the_final_path_and_is_refused(self):
        # The leaf itself carries no tag; only its final path shows the redirect.
        moved = file_facts(final_path=VOLUME_A + "\\Elsewhere\\alpha\\plugin.py")
        self.assertEqual(w.judge_handle(moved, w.KIND_FILE), "plugin_file_unreadable")

    def test_short_name_is_refused(self):
        short = file_facts(expected_name="ALPHA~1\\plugin.py")
        self.assertEqual(w.judge_handle(short, w.KIND_FILE), "plugin_file_unreadable")

    def test_case_only_difference_is_refused(self):
        renamed = file_facts(final_path=ROOT + "\\alpha\\Plugin.PY")
        self.assertEqual(w.judge_handle(renamed, w.KIND_FILE), "plugin_file_unreadable")

    def test_name_that_is_not_a_plain_child_is_refused(self):
        for name in ("alpha/plugin.py", "\\alpha\\plugin.py", "alpha\\..\\alpha\\plugin.py"):
            with self.subTest(name=name):
                self.assertEqual(w.judge_handle(file_facts(expected_name=name), w.KIND_FILE),
                                 "plugin_file_unreadable")

    def test_typed_root_spelling_is_refused(self):
        # The operator typed the root in another letter case. A reader that wrongly passes that typed
        # spelling as the root final path gets a refusal, never a read; the root's own handle spelling passes.
        typed = ROOT.lower()
        self.assertNotEqual(typed, ROOT)
        self.assertEqual(w.judge_handle(file_facts(root_final_path=typed), w.KIND_FILE), "plugin_file_unreadable")
        self.assertEqual(w.judge_handle(dir_facts(root_final_path=typed), w.KIND_DIRECTORY),
                         "plugin_directory_untrusted")
        self.assertIsNone(w.judge_handle(file_facts(root_final_path=ROOT), w.KIND_FILE))

    def test_pinned_root_reached_through_a_junction(self):
        # The root is followed once and pinned by its own final path (the junction's target).
        target = VOLUME_B + "\\Profiles\\Op\\Plugins"
        root = dir_facts(final_path=target, root_final_path=target, expected_name="")
        self.assertIsNone(w.judge_handle(root, w.KIND_DIRECTORY))
        child = file_facts(final_path=target + "\\" + NAME, root_final_path=target)
        self.assertIsNone(w.judge_handle(child, w.KIND_FILE))
        # A child that still names the junction's own path is not under the pinned root.
        stale = file_facts(final_path=ROOT + "\\" + NAME, root_final_path=target)
        self.assertEqual(w.judge_handle(stale, w.KIND_FILE), "plugin_file_unreadable")

    def test_volume_root_joins_with_one_separator(self):
        top = file_facts(final_path=VOLUME_A + "\\" + NAME, root_final_path=VOLUME_A + "\\")
        self.assertIsNone(w.judge_handle(top, w.KIND_FILE))

    def test_root_trailing_separator_is_handled_alike_on_both_sides(self):
        # The pinned root itself (empty name): a trailing separator on either side does not decide.
        for root, final in ((ROOT + "\\", ROOT), (ROOT, ROOT + "\\"), (ROOT + "\\", ROOT + "\\"),
                            (VOLUME_A + "\\", VOLUME_A), (VOLUME_A, VOLUME_A + "\\")):
            with self.subTest(root=root, final=final):
                step = dir_facts(expected_name="", root_final_path=root, final_path=final)
                self.assertIsNone(w.judge_handle(step, w.KIND_DIRECTORY))
        moved = dir_facts(expected_name="", root_final_path=ROOT + "\\", final_path=VOLUME_B + "\\Op\\")
        self.assertEqual(w.judge_handle(moved, w.KIND_DIRECTORY), "plugin_directory_untrusted")

    def test_sibling_sharing_the_root_prefix_is_refused(self):
        sibling = file_facts(final_path=ROOT + "Evil\\" + NAME)
        self.assertEqual(w.judge_handle(sibling, w.KIND_FILE), "plugin_file_unreadable")


class NameRuleTests(unittest.TestCase):
    """The name itself is judged, even when the final path is the plain text join of it."""

    def test_name_that_cannot_be_judged_is_refused(self):
        cases = {
            "empty part": "alpha\\\\plugin.py", "leading separator": "\\alpha\\plugin.py",
            "trailing separator": "alpha\\", "dot part": "alpha\\.\\plugin.py", "dot-dot part": "..\\plugin.py",
            "stream": "alpha\\plugin.py:s", "drive": "c:plugin.py", "slash": "alpha/plugin.py",
            "trailing dot": "alpha\\plugin.py.", "trailing space": "alpha\\plugin.py ", "dir trailing dot": "alpha.\\plugin.py",
            "bare dot": ".", "bare dot-dot": "..",
        }
        for label, name in cases.items():
            with self.subTest(label):
                joined = file_facts(expected_name=name, final_path=ROOT + "\\" + name)
                self.assertEqual(w.judge_handle(joined, w.KIND_FILE), "plugin_file_unreadable")
                step = dir_facts(expected_name=name, final_path=ROOT + "\\" + name)
                self.assertEqual(w.judge_handle(step, w.KIND_DIRECTORY), "plugin_directory_untrusted")

    def test_empty_name_is_refused_for_a_file(self):
        root_as_file = file_facts(expected_name="", final_path=ROOT)
        self.assertEqual(w.judge_handle(root_as_file, w.KIND_FILE), "plugin_file_unreadable")
        self.assertIsNone(w.judge_handle(dir_facts(expected_name="", final_path=ROOT), w.KIND_DIRECTORY))

    def test_device_names_are_refused(self):
        for part in ("CON", "con", "Prn", "aux", "NUL", "nul.txt", "COM1", "com9.py", "LPT1", "lpt9.json",
                     "CONIN$", "conout$", "CON.tar.gz", "NUL .txt"):
            for name in (part, "alpha\\" + part, part + "\\plugin.py"):
                with self.subTest(name=name):
                    joined = file_facts(expected_name=name, final_path=ROOT + "\\" + name)
                    self.assertEqual(w.judge_handle(joined, w.KIND_FILE), "plugin_file_unreadable")
                    step = dir_facts(expected_name=name, final_path=ROOT + "\\" + name)
                    self.assertEqual(w.judge_handle(step, w.KIND_DIRECTORY), "plugin_directory_untrusted")

    def test_names_near_a_device_name_pass(self):
        for name in ("CONSOLE\\plugin.py", "alpha\\COM10.py", "alpha\\LPT0.py", "alpha\\nul_plugin.py", "COM\\x.py"):
            with self.subTest(name=name):
                self.assertIsNone(w.judge_handle(file_facts(expected_name=name, final_path=ROOT + "\\" + name),
                                                 w.KIND_FILE))

    def test_reserved_and_control_characters_are_refused(self):
        for char in '<>"|?*' + "".join(chr(code) for code in range(32)):
            for name in (f"alpha\\plu{char}gin.py", f"al{char}pha\\plugin.py"):
                with self.subTest(char=ord(char), name=name):
                    joined = file_facts(expected_name=name, final_path=ROOT + "\\" + name)
                    self.assertEqual(w.judge_handle(joined, w.KIND_FILE), "plugin_file_unreadable")
                    step = dir_facts(expected_name=name, final_path=ROOT + "\\" + name)
                    self.assertEqual(w.judge_handle(step, w.KIND_DIRECTORY), "plugin_directory_untrusted")


class HandleRowTests(unittest.TestCase):
    def test_link_count_above_one_is_refused(self):
        self.assertEqual(w.judge_handle(file_facts(link_count=2), w.KIND_FILE), "plugin_file_untrusted")
        self.assertEqual(w.judge_handle(dir_facts(link_count=2), w.KIND_DIRECTORY), "plugin_directory_untrusted")

    def test_not_a_disk_file_is_refused(self):
        for file_type in (0, 2, 3, 0x8000):
            with self.subTest(file_type=file_type):
                self.assertEqual(w.judge_handle(file_facts(file_type=file_type), w.KIND_FILE),
                                 "plugin_file_untrusted")

    def test_directory_where_a_file_is_expected_is_refused(self):
        self.assertEqual(w.judge_handle(file_facts(attributes=w.FILE_ATTRIBUTE_DIRECTORY), w.KIND_FILE),
                         "plugin_file_untrusted")

    def test_file_where_a_directory_is_expected_is_refused(self):
        self.assertEqual(w.judge_handle(dir_facts(attributes=w.FILE_ATTRIBUTE_ARCHIVE), w.KIND_DIRECTORY),
                         "plugin_directory_untrusted")

    def test_owner_not_ok_is_refused(self):
        self.assertEqual(w.judge_handle(file_facts(owner_ok=False), w.KIND_FILE), "plugin_file_untrusted")
        self.assertEqual(w.judge_handle(dir_facts(owner_ok=False), w.KIND_DIRECTORY), "plugin_directory_untrusted")

    def test_offline_and_recall_bits_are_refused(self):
        for bit in (w.FILE_ATTRIBUTE_OFFLINE, w.FILE_ATTRIBUTE_RECALL_ON_OPEN, w.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS):
            with self.subTest(bit=hex(bit)):
                placeholder = file_facts(attributes=w.FILE_ATTRIBUTE_ARCHIVE | bit)
                self.assertEqual(w.judge_handle(placeholder, w.KIND_FILE), "plugin_file_untrusted")
                step = dir_facts(attributes=w.FILE_ATTRIBUTE_DIRECTORY | bit)
                self.assertEqual(w.judge_handle(step, w.KIND_DIRECTORY), "plugin_directory_untrusted")


class FailedQueryTests(unittest.TestCase):
    def test_one_failed_query_is_unreadable_for_that_object(self):
        for name in ("attributes", "reparse_tag", "link_count", "file_type", "final_path", "owner_ok"):
            with self.subTest(name):
                self.assertEqual(w.judge_handle(file_facts(**{name: None}), w.KIND_FILE), "plugin_file_unreadable")
                self.assertEqual(w.judge_handle(dir_facts(**{name: None}), w.KIND_DIRECTORY),
                                 "plugin_directory_untrusted")

    def test_final_path_none_alone_is_unreadable(self):
        # The pinned root step: here only the None rule stands between a missing final path and the compare.
        root_step = dir_facts(expected_name="", final_path=None)
        self.assertEqual(w.judge_handle(root_step, w.KIND_DIRECTORY), "plugin_directory_untrusted")

    def test_link_count_zero_is_unreadable(self):
        self.assertEqual(w.judge_handle(file_facts(link_count=0), w.KIND_FILE), "plugin_file_unreadable")
        self.assertEqual(w.judge_handle(dir_facts(link_count=0), w.KIND_DIRECTORY), "plugin_directory_untrusted")

    def test_no_query_at_all_is_platform_unsupported(self):
        nothing = file_facts(attributes=None, reparse_tag=None, link_count=None, file_type=None, final_path=None,
                             owner_ok=None)
        self.assertEqual(w.judge_handle(nothing, w.KIND_FILE), "platform_unsupported")


class RevocationAndUnusableFactsTests(unittest.TestCase):
    def test_revocation_store_is_always_platform_unsupported(self):
        store = file_facts(final_path=ROOT + "\\revoked.sqlite3", expected_name="revoked.sqlite3")
        self.assertEqual(w.judge_handle(store, w.KIND_REVOCATION_STORE), "platform_unsupported")
        self.assertEqual(w.judge_handle(file_facts(), w.KIND_REVOCATION_STORE), "platform_unsupported")
        # Even a store whose handle would refuse on its own answers the platform cause, first.
        for changes in ({"reparse_tag": w.IO_REPARSE_TAG_SYMLINK}, {"link_count": 2}, {"owner_ok": None}):
            with self.subTest(changes=changes):
                self.assertEqual(w.judge_handle(file_facts(**changes), w.KIND_REVOCATION_STORE),
                                 "platform_unsupported")

    def test_unknown_kind_is_platform_unsupported(self):
        self.assertEqual(w.judge_handle(file_facts(), "plugin"), "platform_unsupported")

    def test_facts_that_cannot_be_judged_are_platform_unsupported(self):
        for changes in ({"reparse_tag": "0"}, {"link_count": True}, {"file_type": 1.0}, {"expected_name": b"x"},
                        {"owner_ok": 1}, {"attributes": -1}, {"final_path": b"x"}, {"root_final_path": ""},
                        {"root_final_path": "\\\\"}, {"root_final_path": None}, {"root_final_path": b"root"},
                        {"expected_name": None}):
            with self.subTest(changes=changes):
                self.assertEqual(w.judge_handle(file_facts(**changes), w.KIND_FILE), "platform_unsupported")
        self.assertEqual(w.judge_handle(object(), w.KIND_FILE), "platform_unsupported")

    def test_every_answer_is_a_cause_master_already_gives(self):
        answers = set()
        for kind in (w.KIND_FILE, w.KIND_DIRECTORY, w.KIND_REVOCATION_STORE, "other"):
            for changes in ({}, {"reparse_tag": 1}, {"link_count": 2}, {"link_count": 0}, {"owner_ok": False},
                            {"file_type": 2}, {"final_path": None}, {"expected_name": ".."},
                            {"attributes": w.FILE_ATTRIBUTE_OFFLINE}, {"root_final_path": ""}):
                answers.add(w.judge_handle(file_facts(**changes), kind))
                answers.add(w.judge_handle(dir_facts(**changes), kind))
        answers.discard(None)
        self.assertEqual(answers, {"platform_unsupported", "plugin_file_unreadable", "plugin_file_untrusted",
                                   "plugin_directory_untrusted"})
        self.assertEqual(answers, set(w.CAUSES))
        # Each is a cause master's own code already refuses with: a string handed to a refusal
        # constructor (PluginRefused, EnvRefused, Refused, ...) outside this module and outside tests.
        core = Path(w.__file__).parents[1]
        causes = set()
        for path in core.rglob("*.py"):
            if "tests" in path.relative_to(core).parts or path.name == "windows_read_checks.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8", errors="replace"))):
                if not isinstance(node, ast.Call):
                    continue
                callee = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                if callee.endswith("Refused"):
                    causes.update(arg.value for arg in node.args
                                  if isinstance(arg, ast.Constant) and isinstance(arg.value, str))
        for cause in answers:
            with self.subTest(cause):
                self.assertIn(cause, causes)


class PurityTests(unittest.TestCase):
    MODULE = Path(w.__file__)

    def test_module_imports_no_os_access(self):
        tree = ast.parse(self.MODULE.read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.partition(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").partition(".")[0])
        self.assertLessEqual(names, {"__future__", "dataclasses"})
        called = {node.func.id for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertFalse(called & {"open", "__import__", "exec", "eval", "compile"})

    def test_nothing_imports_the_module(self):
        core = self.MODULE.parents[1]
        users = [p.relative_to(core).as_posix() for p in core.rglob("*.py")
                 if p != self.MODULE and p.name != "test_windows_read_checks.py"
                 and "windows_read_checks" in p.read_text(encoding="utf-8", errors="replace")]
        self.assertEqual(users, [])


if __name__ == "__main__":
    unittest.main()
