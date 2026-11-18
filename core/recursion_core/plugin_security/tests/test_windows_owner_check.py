"""The Windows owner rule (default A3) on plain facts: an owner SID, the process SID and the access entries.

Pure: no file is opened and no OS call is made, so this runs on every host. Every SID below is either a
well-known one or a made-up account SID.
"""

from __future__ import annotations

import ast
import dataclasses
import unittest
from pathlib import Path

from recursion_core.plugin_security import windows_owner_check as o

USER = "S-1-5-21-1000000001-1000000002-1000000003-1001"  # a made-up account SID: the process user
OTHER = "S-1-5-21-1000000001-1000000002-1000000003-1002"  # a made-up second account
EVERYONE = "S-1-1-0"
AUTHENTICATED_USERS = "S-1-5-11"
CAPABILITY = "S-1-15-3-1024-1-2-3-4-5-6-7-8"  # a made-up AppContainer capability SID
FULL = 0x001F01FF
READ_EXECUTE = 0x001200A9


def allow(sid, mask=FULL, flags=0):
    return o.AccessEntry(o.ACCESS_ALLOWED_ACE_TYPE, flags, mask, sid)


def deny(sid, mask=FULL, flags=0):
    return o.AccessEntry(o.ACCESS_DENIED_ACE_TYPE, flags, mask, sid)


# What a folder made with mkdir(mode=0o700) by CPython 3.12.4+ carries (MEASURED.md row 5).
MKDTEMP = (allow(o.SYSTEM, flags=3), allow(o.ADMINISTRATORS, flags=3), allow(o.OWNER_RIGHTS, flags=3))


def judge(owner=USER, entries=MKDTEMP, directory=False, process=USER):
    return o.owner_ok(owner, process, entries, directory=directory)


class OwnerTests(unittest.TestCase):
    def test_the_process_user_system_and_administrators_may_own(self):
        for owner in (USER, o.SYSTEM, o.ADMINISTRATORS):
            for directory in (False, True):
                with self.subTest(owner=owner, directory=directory):
                    self.assertTrue(judge(owner=owner, directory=directory))

    def test_any_other_owner_is_not_ok(self):
        for owner in (OTHER, EVERYONE, AUTHENTICATED_USERS, o.OWNER_RIGHTS, CAPABILITY):
            with self.subTest(owner=owner):
                self.assertFalse(judge(owner=owner))
                self.assertFalse(judge(owner=owner, directory=True))

    def test_the_owner_is_judged_against_this_process_not_a_fixed_account(self):
        self.assertFalse(judge(owner=USER, process=OTHER))
        self.assertTrue(judge(owner=OTHER, process=OTHER))

    def test_an_owner_that_could_not_be_read_is_not_ok(self):
        for owner in (None, "", b"S-1-5-18", 18):
            with self.subTest(owner=owner):
                self.assertFalse(judge(owner=owner))

    def test_a_process_identity_that_could_not_be_read_is_not_ok(self):
        for process in (None, "", b"S-1-5-18", "not a sid", 1001):
            with self.subTest(process=process):
                self.assertFalse(judge(process=process))
                self.assertFalse(judge(owner=process, process=process))


class AccessEntryTests(unittest.TestCase):
    def test_owner_system_and_administrators_may_write(self):
        entries = (allow(USER), allow(o.SYSTEM), allow(o.ADMINISTRATORS))
        self.assertTrue(judge(entries=entries))
        self.assertTrue(judge(entries=entries, directory=True))

    def test_owner_rights_counts_as_the_owner(self):
        # MEASURED.md row 5: every folder tempfile makes here carries OWNER RIGHTS full control and no entry
        # for the user's own SID. OWNER RIGHTS is whoever owns the object, and that owner is judged above.
        self.assertTrue(judge(entries=MKDTEMP))
        self.assertTrue(judge(entries=MKDTEMP, directory=True))
        self.assertFalse(judge(owner=OTHER, entries=MKDTEMP))

    def test_a_write_class_right_for_anyone_else_is_not_ok(self):
        for sid in (OTHER, EVERYONE, AUTHENTICATED_USERS, CAPABILITY, o.CREATOR_OWNER):
            with self.subTest(sid=sid):
                self.assertFalse(judge(entries=MKDTEMP + (allow(sid),)))
                self.assertFalse(judge(entries=MKDTEMP + (allow(sid),), directory=True))

    def test_each_write_class_bit_alone_is_not_ok(self):
        for bit in (0x2, 0x4, 0x10, 0x100, 0x10000, 0x40000, 0x80000, 0x40000000, 0x10000000):
            with self.subTest(bit=hex(bit)):
                self.assertFalse(judge(entries=(allow(EVERYONE, bit),)))
                self.assertFalse(judge(entries=(allow(EVERYONE, bit),), directory=True))

    def test_delete_child_is_a_write_right_on_a_directory_only(self):
        self.assertFalse(judge(entries=(allow(EVERYONE, 0x40),), directory=True))
        self.assertTrue(judge(entries=(allow(EVERYONE, 0x40),), directory=False))

    def test_read_and_execute_for_others_is_ok(self):
        entries = MKDTEMP + (allow(EVERYONE, READ_EXECUTE), allow(OTHER, READ_EXECUTE), allow(CAPABILITY, 0x120089))
        self.assertTrue(judge(entries=entries))
        self.assertTrue(judge(entries=entries, directory=True))

    def test_an_inherit_only_entry_does_not_apply_to_this_object(self):
        inherit_only = allow(EVERYONE, FULL, flags=o.INHERIT_ONLY_ACE | 3)
        self.assertTrue(judge(entries=MKDTEMP + (inherit_only,)))
        # The same entry without the inherit-only flag applies.
        self.assertFalse(judge(entries=MKDTEMP + (allow(EVERYONE, FULL, flags=3),)))

    def test_deny_entries_are_ignored(self):
        self.assertTrue(judge(entries=(deny(EVERYONE),) + MKDTEMP))
        self.assertFalse(judge(entries=(deny(EVERYONE), allow(EVERYONE))))

    def test_an_unknown_entry_type_is_not_ok(self):
        for ace_type in (2, 5, 6, 9, 10, 0x11):
            for sid in (None, USER):
                with self.subTest(ace_type=ace_type, sid=sid):
                    entry = o.AccessEntry(ace_type, 0, 0x1, sid)
                    self.assertFalse(judge(entries=MKDTEMP + (entry,)))

    def test_an_absent_access_list_is_not_ok(self):
        # A NULL DACL grants everyone everything; FAT and exFAT keep no access list at all.
        self.assertFalse(judge(entries=None))
        self.assertFalse(judge(entries=None, directory=True))

    def test_an_empty_access_list_is_ok(self):
        self.assertTrue(judge(entries=()))

    def test_entries_that_cannot_be_judged_are_not_ok(self):
        for bad in ((("0", 0, 1, USER),), [object()], (o.AccessEntry(0, 0, -1, USER),),
                    (o.AccessEntry(True, 0, 1, USER),), (o.AccessEntry(0, None, 1, USER),),
                    (o.AccessEntry(0, 0, FULL, None),), (o.AccessEntry(0, 0, FULL, b"S-1-1-0"),), "S-1-1-0", 7):
            with self.subTest(bad=bad):
                self.assertFalse(judge(entries=bad))

    def test_only_exact_entries_in_an_exact_tuple_or_list_are_judged(self):
        # A subclass could answer "allow" at the type check and "deny" afterwards (first read P3).
        reads = []

        class Shifty(o.AccessEntry):
            def __getattribute__(self, name):
                value = object.__getattribute__(self, name)
                if name == "ace_type":
                    reads.append(name)
                    return o.ACCESS_ALLOWED_ACE_TYPE if len(reads) == 1 else o.ACCESS_DENIED_ACE_TYPE
                return value

        self.assertFalse(judge(entries=MKDTEMP + (Shifty(0, 0, FULL, EVERYONE),)))
        self.assertFalse(judge(entries=MKDTEMP + (allow(EVERYONE),)))

        class Plain(o.AccessEntry):
            pass

        # Exact type only: even a subclass whose values would pass is not judged.
        self.assertFalse(judge(entries=MKDTEMP + (Plain(0, 0, READ_EXECUTE, EVERYONE),)))
        self.assertTrue(judge(entries=MKDTEMP + (allow(EVERYONE, READ_EXECUTE),)))

        class Entries(tuple):
            pass

        self.assertFalse(judge(entries=Entries(MKDTEMP)))
        self.assertTrue(judge(entries=tuple(MKDTEMP)))
        self.assertTrue(judge(entries=list(MKDTEMP)))

    def test_entries_are_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            allow(USER).mask = 0


class PurityTests(unittest.TestCase):
    MODULE = Path(o.__file__)

    def test_module_imports_no_os_access(self):
        tree = ast.parse(self.MODULE.read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.partition(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").partition(".")[0])
        self.assertLessEqual(names, {"__future__", "dataclasses"})

    def test_only_the_handle_reader_imports_the_module(self):
        NAME = "windows_owner_check"
        MENTION_OK = ()
        def imports(node, name):
            if isinstance(node, ast.Import):
                dotted = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                dotted = [node.module or ""] + [alias.name for alias in node.names]
            elif (isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant)
                  and isinstance(node.args[0].value, str)
                  and getattr(node.func, "attr", getattr(node.func, "id", None)) in ("import_module", "__import__")):
                dotted = [node.args[0].value]
            else:
                return False
            return any(name in part.split(".") for part in dotted)

        def docstrings(tree):
            found = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
                    first = node.body[0]
                    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                        found.add(id(first.value))
            return found

        def names_it(tree, name):
            # A real import statement, or any string constant outside a docstring that holds the name: this
            # also catches import_module(name=...), "." + name, __import__(fromlist=[...]), runpy, pkgutil,
            # spec_from_file_location with the file's path, and exec("...").
            if any(imports(node, name) for node in ast.walk(tree)):
                return "import"
            skip = docstrings(tree)
            if any(isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
                   and name in node.value for node in ast.walk(tree)):
                return "string"
            return None

        # Every .py and .pyw file of the repository. Hidden folders count; only .git and cache folders are
        # skipped. A file ast cannot read counts when it names the module at all.
        repo = Path(__file__).resolve().parents[4]
        users = []
        for folder, subfolders, files in repo.walk():
            subfolders[:] = sorted(d for d in subfolders if d not in (".git", "__pycache__"))
            for file in sorted(files):
                path = folder / file
                if path.suffix not in (".py", ".pyw") or path == Path(__file__).resolve():
                    continue
                relative = path.relative_to(repo).as_posix()
                text = path.read_text(encoding="utf-8-sig", errors="replace")
                try:
                    kind = names_it(ast.parse(text), NAME)
                except SyntaxError:
                    kind = "string" if NAME in text else None
                if kind == "import" or (kind == "string" and relative not in MENTION_OK):
                    users.append(relative)
        users.sort()
        self.assertEqual(users, ["core/recursion_core/plugin_security/tests/test_windows_handle_reader.py",
                                 "core/recursion_core/plugin_security/windows_handle_reader.py"])


if __name__ == "__main__":
    unittest.main()
