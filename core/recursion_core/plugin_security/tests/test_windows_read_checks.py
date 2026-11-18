"""The Windows plugin-read decision on faked handle facts; no file is opened and no OS call is made.

One case per refusal row, plus the typed-root case (the comparison ignores what the operator typed)
and the pinned-root case (a root reached through a junction is pinned by its own final path).
Every value is fake.
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
                                 "plugin_file_unreadable")

    def test_reparse_attribute_with_tag_zero_is_refused(self):
        attrs = w.FILE_ATTRIBUTE_ARCHIVE | w.FILE_ATTRIBUTE_REPARSE_POINT
        self.assertEqual(w.judge_handle(file_facts(attributes=attrs), w.KIND_FILE), "plugin_file_unreadable")
        attrs = w.FILE_ATTRIBUTE_DIRECTORY | w.FILE_ATTRIBUTE_REPARSE_POINT
        self.assertEqual(w.judge_handle(dir_facts(attributes=attrs), w.KIND_DIRECTORY), "plugin_file_unreadable")


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

    def test_typed_root_case_does_not_matter(self):
        # The operator typed the root in another letter case; both sides of the compare come from handles on disk,
        # so the typed spelling never enters it and the clean file is accepted.
        typed = ROOT.lower()
        self.assertNotEqual(typed, ROOT)
        self.assertIsNone(w.judge_handle(file_facts(), w.KIND_FILE))

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


class HandleRowTests(unittest.TestCase):
    def test_link_count_above_one_is_refused(self):
        self.assertEqual(w.judge_handle(file_facts(link_count=2), w.KIND_FILE), "plugin_file_untrusted")
        self.assertEqual(w.judge_handle(dir_facts(link_count=2), w.KIND_DIRECTORY), "plugin_file_untrusted")

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
                         "plugin_file_untrusted")

    def test_owner_not_ok_is_refused(self):
        self.assertEqual(w.judge_handle(file_facts(owner_ok=False), w.KIND_FILE), "plugin_file_untrusted")
        self.assertEqual(w.judge_handle(dir_facts(owner_ok=False), w.KIND_DIRECTORY), "plugin_file_untrusted")


class RevocationAndUnusableFactsTests(unittest.TestCase):
    def test_revocation_store_is_always_platform_unsupported(self):
        store = file_facts(final_path=ROOT + "\\revocations.sqlite3", expected_name="revocations.sqlite3")
        self.assertEqual(w.judge_handle(store, w.KIND_REVOCATION_STORE), "platform_unsupported")
        self.assertEqual(w.judge_handle(file_facts(), w.KIND_REVOCATION_STORE), "platform_unsupported")

    def test_unknown_kind_is_platform_unsupported(self):
        self.assertEqual(w.judge_handle(file_facts(), "plugin"), "platform_unsupported")

    def test_facts_that_cannot_be_judged_are_platform_unsupported(self):
        for changes in ({"attributes": None}, {"reparse_tag": "0"}, {"link_count": True}, {"file_type": 1.0},
                        {"final_path": None}, {"expected_name": b"x"}, {"owner_ok": 1}, {"attributes": -1},
                        {"link_count": 0}, {"root_final_path": ""}, {"root_final_path": "\\\\"}):
            with self.subTest(changes=changes):
                self.assertEqual(w.judge_handle(file_facts(**changes), w.KIND_FILE), "platform_unsupported")
        self.assertEqual(w.judge_handle(object(), w.KIND_FILE), "platform_unsupported")

    def test_every_answer_is_an_existing_cause(self):
        self.assertEqual(set(w.CAUSES), {"platform_unsupported", "plugin_file_unreadable", "plugin_file_untrusted"})


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

    def test_nothing_imports_the_module(self):
        core = self.MODULE.parents[1]
        users = [p.relative_to(core).as_posix() for p in core.rglob("*.py")
                 if p != self.MODULE and p.name != "test_windows_read_checks.py"
                 and "windows_read_checks" in p.read_text(encoding="utf-8", errors="replace")]
        self.assertEqual(users, [])


if __name__ == "__main__":
    unittest.main()
