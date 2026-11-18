"""The Windows plugin-file handle reader.

Two halves. A fake native layer (every value made up) drives the reader on every host: each refusal row,
the bounded read, and that every handle is closed and nothing is read on a refusal. Real-handle tests make
their files inside one TemporaryDirectory and run only where ``sys.getwindowsversion`` exists (never gated on
``os.name``, which the simulated-Windows step sets on Linux). This module imports on every host: ctypes is
touched only inside the real-handle tests and inside the reader's functions.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from recursion_core.plugin_security import windows_handle_reader as r
from recursion_core.plugin_security import windows_read_checks as w
from recursion_core.plugin_security.errors import Refused
from recursion_core.plugin_security.windows_owner_check import ADMINISTRATORS, OWNER_RIGHTS, SYSTEM, AccessEntry

ON_WINDOWS = hasattr(sys, "getwindowsversion")
VOLUME = "\\\\?\\Volume{0f0f0f0f-0000-4000-8000-00000000000a}"  # a fake volume name, never opened
ROOT_TYPED = "typed-root\\Plugins"  # a fake typed root (no drive letter), never opened: the fake native layer answers
ROOT = VOLUME + "\\Plugins"
USER = "S-1-5-21-1000000001-1000000002-1000000003-1001"  # made up
OTHER = "S-1-5-21-1000000001-1000000002-1000000003-1002"  # made up
SAFE = (AccessEntry(0, 3, 0x1F01FF, SYSTEM), AccessEntry(0, 3, 0x1F01FF, ADMINISTRATORS),
        AccessEntry(0, 3, 0x1F01FF, OWNER_RIGHTS))
DATA = b"print('plugin')\n"
SEP = "\\"


class Obj:
    """One object the fake native layer can open: its facts, and its bytes for a file."""

    def __init__(self, directory, data=b"", **facts):
        self.directory = directory
        self.data = data
        self.facts = {"attributes": w.FILE_ATTRIBUTE_DIRECTORY if directory else w.FILE_ATTRIBUTE_ARCHIVE,
                      "reparse_tag": 0, "links": 1, "end_of_file": len(data), "delete_pending": False,
                      "volume": 7, "file_type": w.FILE_TYPE_DISK, "remote": False, "owner": USER,
                      "entries": SAFE, "final_path": None}
        self.facts.update(facts)


class FakeNative:
    """Answers by the name opened; records every open, read and close."""

    def __init__(self, objects, *, process_sid=USER, open_errors=None, fail=(), fail_on=None, low32=False,
                 short=None):
        self.objects = objects
        self.process = process_sid
        self.open_errors = open_errors or {}
        self.fail = set(fail)
        self.fail_on = fail_on or {}  # name -> the queries that fail on that object only
        self.low32 = low32  # a ReadFile that keeps only the low 32 bits of the size it is given
        self.short = short  # the file yields only this many bytes, then end of file
        self.handles = {}
        self.opened, self.closed, self.reads, self.served = [], [], [], []
        self.next = 100

    def _final(self, name):
        obj = self.objects[name]
        return obj.facts["final_path"] or (ROOT if name == ROOT_TYPED else name)

    def open(self, name, directory, follow):
        self.opened.append((name, directory, follow))
        if name in self.open_errors:
            return None, self.open_errors[name]
        if name not in self.objects:
            return None, r.ERROR_FILE_NOT_FOUND
        if self.objects[name].directory and not directory:
            return None, 5
        self.next += 1
        self.handles[self.next] = name
        return self.next, 0

    def _failed(self, handle, key):
        return key in self.fail or key in self.fail_on.get(self.handles[handle], ())

    def _f(self, handle, key):
        return None if self._failed(handle, key) else self.objects[self.handles[handle]].facts[key]

    def attribute_tag(self, handle):
        if self._failed(handle, "attribute_tag"):
            return None
        return self._f(handle, "attributes"), self._f(handle, "reparse_tag")

    def standard(self, handle):
        if self._failed(handle, "standard"):
            return None
        return self._f(handle, "links"), self._f(handle, "end_of_file"), self._f(handle, "delete_pending")

    def volume(self, handle):
        return self._f(handle, "volume")

    def remote(self, handle):
        return self._f(handle, "remote")

    def file_type(self, handle):
        return self._f(handle, "file_type")

    def final_path(self, handle):
        return None if self._failed(handle, "final_path_call") else self._final(self.handles[handle])

    def security(self, handle):
        if self._failed(handle, "security"):
            return None
        return self._f(handle, "owner"), self._f(handle, "entries")

    def process_sid(self):
        return self.process

    def read(self, handle, size):
        if "read" in self.fail:
            return None
        self.reads.append((handle, size))
        if self.low32:
            size &= 0xFFFFFFFF
        obj = self.objects[self.handles[handle]]
        data = obj.data if self.short is None else obj.data[:self.short]
        done = sum(len(chunk) for h, chunk in self.served if h == handle)
        chunk = data[done:done + size]
        self.served.append((handle, chunk))
        return chunk

    def close(self, handle):
        self.closed.append(handle)


def tree(leaf=None, step=None, root=None, extra=None):
    objects = {ROOT_TYPED: root or Obj(True), ROOT + "\\alpha": step or Obj(True),
               ROOT + "\\alpha\\plugin.py": leaf or Obj(False, DATA)}
    objects.update(extra or {})
    return objects


def run(native, name="alpha\\plugin.py", limit=1024, **kwargs):
    return r.read_plugin_file(ROOT_TYPED, name, limit, native=native, **kwargs)


class FakeReadTests(unittest.TestCase):
    def assertRefused(self, native, code, reads_allowed=False, **kwargs):
        with self.assertRaises(Refused) as caught:
            run(native, **kwargs)
        self.assertEqual(caught.exception.code, code)
        if not reads_allowed:
            self.assertEqual(native.reads, [], "a refused file is never read")
        self.assertAllClosed(native)
        return caught.exception

    def assertAllClosed(self, native):
        self.assertEqual(sorted(native.closed), sorted(native.handles), "every handle is closed exactly once")

    def test_clean_plugin_is_read_from_its_own_handle(self):
        native = FakeNative(tree())
        self.assertEqual(run(native), DATA)
        self.assertAllClosed(native)
        self.assertEqual(native.opened, [(ROOT_TYPED, True, True), (ROOT + "\\alpha", True, False),
                                         (ROOT + "\\alpha\\plugin.py", False, False)])
        leaf_handle = max(native.handles)
        self.assertEqual({h for h, _ in native.reads}, {leaf_handle})

    def test_steps_open_below_the_root_final_path_never_by_the_typed_root(self):
        native = FakeNative(tree())
        run(native)
        for name, _, follow in native.opened[1:]:
            self.assertTrue(name.startswith(ROOT + "\\"), name)
            self.assertFalse(follow)

    def test_read_is_bounded_to_limit_plus_one(self):
        native = FakeNative(tree(leaf=Obj(False, b"x" * 10)))
        self.assertEqual(run(native, limit=10), b"x" * 10)
        self.assertEqual(native.reads[0][1], 11)
        self.assertTrue(all(n <= 11 for _, n in native.reads))

    def test_too_large_by_end_of_file_is_refused_unread(self):
        native = FakeNative(tree(leaf=Obj(False, b"x" * 11)))
        self.assertRefused(native, "plugin_file_too_large", limit=10)

    def test_a_file_that_grew_after_the_size_check_is_refused(self):
        native = FakeNative(tree(leaf=Obj(False, b"x" * 50, end_of_file=5)))
        with self.assertRaises(Refused) as caught:
            run(native, limit=10)
        self.assertEqual(caught.exception.code, "plugin_file_unreadable")
        self.assertLessEqual(sum(n for _, n in native.reads), 6)
        self.assertAllClosed(native)

    def test_missing_leaf(self):
        objects = tree()
        del objects[ROOT + "\\alpha\\plugin.py"]
        self.assertRefused(FakeNative(objects), "plugin_file_missing")
        native = FakeNative(objects)
        self.assertIsNone(run(native, missing_ok=True))
        self.assertAllClosed(native)
        for error in (r.ERROR_FILE_NOT_FOUND, r.ERROR_PATH_NOT_FOUND):
            native = FakeNative(tree(), open_errors={ROOT + "\\alpha\\plugin.py": error})
            self.assertIsNone(run(native, missing_ok=True))

    def test_other_open_errors_are_unreadable(self):
        for error in (5, 32, 1920, 87):
            with self.subTest(error=error):
                native = FakeNative(tree(), open_errors={ROOT + "\\alpha\\plugin.py": error})
                self.assertRefused(native, "plugin_file_unreadable")
                native = FakeNative(tree(), open_errors={ROOT + "\\alpha\\plugin.py": error})
                self.assertRefused(native, "plugin_file_unreadable", missing_ok=True)

    def test_a_directory_step_or_root_that_will_not_open_is_untrusted(self):
        for name, error in ((ROOT + "\\alpha", r.ERROR_FILE_NOT_FOUND), (ROOT + "\\alpha", 5), (ROOT_TYPED, 2)):
            with self.subTest(name=name, error=error):
                native = FakeNative(tree(), open_errors={name: error})
                self.assertRefused(native, "plugin_directory_untrusted")
                native = FakeNative(tree(), open_errors={name: error})
                self.assertRefused(native, "plugin_directory_untrusted", missing_ok=True)

    def test_names_are_refused_before_any_open(self):
        for name in ("", "alpha\\plugin.py:s", "alpha/plugin.py", "alpha\\plugin.py.", "alpha\\..\\x.py",
                     "CON", "alpha\\nul.py", "alpha\\plugin.py ", "\\alpha\\plugin.py", "c:plugin.py", "alpha\\",
                     "alpha\\p*.py", None, b"alpha\\plugin.py"):
            with self.subTest(name=name):
                native = FakeNative(tree())
                with self.assertRaises(Refused) as caught:
                    run(native, name=name)
                self.assertEqual(caught.exception.code, "plugin_file_unreadable")
                self.assertEqual(native.opened, [])

    def test_a_root_that_cannot_be_used_is_refused_before_any_open(self):
        for root in ("", None, b"typed-root\\Plugins"):
            with self.subTest(root=root):
                native = FakeNative(tree())
                with self.assertRaises(Refused) as caught:
                    r.read_plugin_file(root, "alpha\\plugin.py", 10, native=native)
                self.assertEqual(caught.exception.code, "plugin_directory_untrusted")
                self.assertEqual(native.opened, [])

    def test_a_bad_limit_is_a_caller_error(self):
        for limit in (-1, 1.5, "10", True, None):
            with self.subTest(limit=limit), self.assertRaises((TypeError, ValueError)):
                run(FakeNative(tree()), limit=limit)

    def test_reparse_leaf_is_unreadable_and_reparse_step_is_untrusted(self):
        for tag in (w.IO_REPARSE_TAG_SYMLINK, w.IO_REPARSE_TAG_MOUNT_POINT, 0x9000601A):
            with self.subTest(tag=hex(tag)):
                leaf = Obj(False, DATA, reparse_tag=tag,
                           attributes=w.FILE_ATTRIBUTE_ARCHIVE | w.FILE_ATTRIBUTE_REPARSE_POINT)
                self.assertRefused(FakeNative(tree(leaf=leaf)), "plugin_file_unreadable")
                step = Obj(True, reparse_tag=tag, attributes=w.FILE_ATTRIBUTE_DIRECTORY | w.FILE_ATTRIBUTE_REPARSE_POINT)
                self.assertRefused(FakeNative(tree(step=step)), "plugin_directory_untrusted")

    def test_ancestor_redirect_shows_in_the_final_path(self):
        leaf = Obj(False, DATA, final_path=VOLUME + "\\Elsewhere\\plugin.py")
        self.assertRefused(FakeNative(tree(leaf=leaf)), "plugin_file_unreadable")

    def test_case_only_difference_is_refused(self):
        leaf = Obj(False, DATA, final_path=ROOT + "\\alpha\\Plugin.py")
        self.assertRefused(FakeNative(tree(leaf=leaf)), "plugin_file_unreadable")

    def test_hard_link_is_untrusted(self):
        self.assertRefused(FakeNative(tree(leaf=Obj(False, DATA, links=2))), "plugin_file_untrusted")

    def test_delete_pending_counts_as_link_count_zero(self):
        self.assertRefused(FakeNative(tree(leaf=Obj(False, DATA, delete_pending=True))), "plugin_file_unreadable")
        self.assertRefused(FakeNative(tree(step=Obj(True, delete_pending=True))), "plugin_directory_untrusted")

    def test_offline_and_recall_bits_are_untrusted(self):
        for bit in (w.FILE_ATTRIBUTE_OFFLINE, w.FILE_ATTRIBUTE_RECALL_ON_OPEN, w.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS):
            with self.subTest(bit=hex(bit)):
                leaf = Obj(False, DATA, attributes=w.FILE_ATTRIBUTE_ARCHIVE | bit)
                self.assertRefused(FakeNative(tree(leaf=leaf)), "plugin_file_untrusted")
                step = Obj(True, attributes=w.FILE_ATTRIBUTE_DIRECTORY | bit)
                self.assertRefused(FakeNative(tree(step=step)), "plugin_directory_untrusted")

    def test_a_remote_file_or_step_is_refused(self):
        self.assertRefused(FakeNative(tree(leaf=Obj(False, DATA, remote=True))), "plugin_file_unreadable")
        self.assertRefused(FakeNative(tree(step=Obj(True, remote=True))), "plugin_directory_untrusted")
        # The remote query that could not be made at all is a failed query, not a pass.
        self.assertRefused(FakeNative(tree(leaf=Obj(False, DATA, remote=None))), "plugin_file_unreadable")

    def test_a_volume_change_below_the_root_is_refused(self):
        self.assertRefused(FakeNative(tree(leaf=Obj(False, DATA, volume=8))), "plugin_file_unreadable")
        self.assertRefused(FakeNative(tree(step=Obj(True, volume=8))), "plugin_directory_untrusted")
        self.assertRefused(FakeNative(tree(leaf=Obj(False, DATA, volume=None))), "plugin_file_unreadable")
        self.assertRefused(FakeNative(tree(root=Obj(True, volume=None))), "plugin_directory_untrusted")

    def test_not_a_disk_file_is_untrusted(self):
        self.assertRefused(FakeNative(tree(leaf=Obj(False, DATA, file_type=2))), "plugin_file_untrusted")

    def test_a_directory_where_the_file_should_be_is_refused(self):
        objects = tree()
        objects[ROOT + "\\alpha\\plugin.py"] = Obj(True)
        self.assertRefused(FakeNative(objects), "plugin_file_unreadable")

    def test_another_owner_is_untrusted(self):
        self.assertRefused(FakeNative(tree(leaf=Obj(False, DATA, owner=OTHER))), "plugin_file_untrusted")
        self.assertRefused(FakeNative(tree(step=Obj(True, owner=OTHER))), "plugin_directory_untrusted")
        self.assertRefused(FakeNative(tree(root=Obj(True, owner=OTHER))), "plugin_directory_untrusted")

    def test_a_write_right_for_another_account_is_untrusted(self):
        open_entries = SAFE + (AccessEntry(0, 0, 0x2, OTHER),)
        self.assertRefused(FakeNative(tree(leaf=Obj(False, DATA, entries=open_entries))), "plugin_file_untrusted")
        self.assertRefused(FakeNative(tree(step=Obj(True, entries=open_entries))), "plugin_directory_untrusted")
        self.assertRefused(FakeNative(tree(leaf=Obj(False, DATA, entries=None))), "plugin_file_untrusted")

    def test_an_unknown_process_identity_refuses(self):
        self.assertRefused(FakeNative(tree(), process_sid=None), "plugin_directory_untrusted")

    def test_one_failed_query_refuses(self):
        for failed in ("attribute_tag", "standard", "volume", "remote", "file_type", "final_path_call", "security"):
            with self.subTest(failed=failed):
                native = FakeNative(tree(), fail={failed})
                with self.assertRaises(Refused) as caught:
                    run(native)
                self.assertIn(caught.exception.code, ("plugin_directory_untrusted", "plugin_file_unreadable",
                                                      "platform_unsupported"))
                self.assertEqual(native.reads, [])
                self.assertAllClosed(native)

    def test_a_limit_above_the_bound_is_a_caller_error(self):
        # ReadFile's byte count is 32 bits: a larger size was cut to its low bits and read as a pass.
        for limit in (r.MAX_LIMIT + 1, 2**32 - 1, 2**32 + 4, 2**33, 2**62):
            with self.subTest(limit=limit):
                native = FakeNative(tree(leaf=Obj(False, b"y" * 16)), low32=True)
                with self.assertRaises(ValueError):
                    run(native, limit=limit)
                self.assertEqual(native.opened, [])
                self.assertEqual(native.reads, [])
        self.assertEqual(run(FakeNative(tree(leaf=Obj(False, b"y" * 16)), low32=True), limit=r.MAX_LIMIT), b"y" * 16)

    def test_a_read_that_honours_only_32_bits_never_passes_cut_bytes(self):
        for limit in (16, 2**20, 2**32 - 1, 2**32 + 4, 2**33):
            with self.subTest(limit=limit):
                native = FakeNative(tree(leaf=Obj(False, b"y" * 16)), low32=True)
                try:
                    data = run(native, limit=limit)
                except ValueError:
                    continue
                self.assertEqual(data, b"y" * 16)

    def test_a_read_asks_for_the_file_plus_one_in_chunks(self):
        native = FakeNative(tree(leaf=Obj(False, b"y" * 16)))
        self.assertEqual(run(native, limit=8 << 20), b"y" * 16)
        self.assertLessEqual(max(n for _, n in native.reads), 17, "never more than the end of file + 1")
        big = Obj(False, b"z" * (3 << 20))
        native = FakeNative(tree(leaf=big))
        self.assertEqual(run(native, limit=8 << 20), big.data)
        self.assertTrue(all(n <= 1 << 20 for _, n in native.reads), "one request is at most 1 MiB")

    def test_fewer_bytes_than_the_end_of_file_is_unreadable(self):
        native = FakeNative(tree(leaf=Obj(False, b"y" * 16)), short=5)
        self.assertRefused(native, "plugin_file_unreadable", reads_allowed=True)

    def test_each_failed_query_answers_its_handles_cause(self):
        names = {"root": ROOT_TYPED, "step": ROOT + SEP + "alpha", "leaf": ROOT + SEP + "alpha" + SEP + "plugin.py"}
        causes = {"root": "plugin_directory_untrusted", "step": "plugin_directory_untrusted",
                  "leaf": "plugin_file_unreadable"}
        for where, name in names.items():
            for failed in ("attribute_tag", "standard", "volume", "remote", "file_type", "final_path_call",
                           "security"):
                with self.subTest(where=where, failed=failed):
                    native = FakeNative(tree(), fail_on={name: {failed}})
                    refused = self.assertRefused(native, causes[where])
                    self.assertEqual(refused.detail.split(":")[0],
                                     {"root": "root", "step": "alpha", "leaf": "plugin.py"}[where])

    def test_a_volume_root_is_joined_with_one_separator(self):
        objects = {ROOT_TYPED: Obj(True, final_path=VOLUME + SEP), VOLUME + SEP + "alpha": Obj(True),
                   VOLUME + SEP + "alpha" + SEP + "plugin.py": Obj(False, DATA)}
        native = FakeNative(objects)
        self.assertEqual(run(native), DATA)
        self.assertEqual([name for name, _, _ in native.opened[1:]],
                         [VOLUME + SEP + "alpha", VOLUME + SEP + "alpha" + SEP + "plugin.py"])

    def test_a_root_holding_a_control_character_is_refused_before_any_open(self):
        for root in ("typed-root" + chr(0) + "x", "typed-root" + chr(10), chr(31) + "typed-root"):
            with self.subTest(root=repr(root)):
                native = FakeNative(tree())
                with self.assertRaises(Refused) as caught:
                    r.read_plugin_file(root, "alpha" + SEP + "plugin.py", 10, native=native)
                self.assertEqual(caught.exception.code, "plugin_directory_untrusted")
                self.assertEqual(native.opened, [])

    def test_missing_errors_have_names(self):
        native = FakeNative(tree(), open_errors={ROOT + SEP + "alpha": r.ERROR_PATH_NOT_FOUND})
        refused = self.assertRefused(native, "plugin_directory_untrusted")
        self.assertEqual(refused.detail, "alpha: ERROR_PATH_NOT_FOUND")

    def test_a_root_whose_final_path_fails_is_untrusted(self):
        native = FakeNative(tree())
        native.final_path = lambda handle: None if native.handles[handle] == ROOT_TYPED else ROOT
        with self.assertRaises(Refused) as caught:
            run(native)
        self.assertEqual(caught.exception.code, "plugin_directory_untrusted")
        self.assertAllClosed(native)

    def test_a_failed_read_is_unreadable(self):
        native = FakeNative(tree(), fail={"read"})
        with self.assertRaises(Refused) as caught:
            run(native)
        self.assertEqual(caught.exception.code, "plugin_file_unreadable")
        self.assertAllClosed(native)

    def test_an_exception_inside_the_native_layer_still_closes_every_handle(self):
        native = FakeNative(tree())
        native.read = mock.Mock(side_effect=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            run(native)
        self.assertAllClosed(native)

    def test_a_file_directly_under_the_root(self):
        objects = {ROOT_TYPED: Obj(True), ROOT + "\\plugin.py": Obj(False, DATA)}
        self.assertEqual(run(FakeNative(objects), name="plugin.py"), DATA)

    def test_detail_names_only_the_last_part(self):
        leaf = Obj(False, DATA, links=2)
        refused = self.assertRefused(FakeNative(tree(leaf=leaf)), "plugin_file_untrusted")
        self.assertEqual(refused.detail.split(":")[0], "plugin.py")
        self.assertNotIn("Plugins", str(refused))


class PlatformTests(unittest.TestCase):
    def test_no_windows_host_is_platform_unsupported(self):
        without = mock.Mock(spec=[])  # a sys with no getwindowsversion
        with mock.patch.object(r, "sys", without), self.assertRaises(Refused) as caught:
            r.read_plugin_file("typed-root\\Plugins", "alpha\\plugin.py", 10)
        self.assertEqual(caught.exception.code, "platform_unsupported")

    def test_a_native_layer_that_will_not_load_is_platform_unsupported(self):
        with mock.patch.object(r, "_load_native", side_effect=Refused("platform_unsupported", "test")), \
                self.assertRaises(Refused) as caught:
            r.read_plugin_file("typed-root\\Plugins", "alpha\\plugin.py", 10)
        self.assertEqual(caught.exception.code, "platform_unsupported")

    def test_no_ctypes_is_platform_unsupported(self):
        with mock.patch.object(r, "sys", mock.Mock(spec=["getwindowsversion"])), \
                mock.patch.dict(sys.modules, {"ctypes": None, "ctypes.wintypes": None}), \
                self.assertRaises(Refused) as caught:
            r.read_plugin_file("typed-root" + SEP + "Plugins", "alpha" + SEP + "plugin.py", 10)
        self.assertEqual(caught.exception.code, "platform_unsupported")
        self.assertIn(caught.exception.detail, ("ImportError", "ModuleNotFoundError"))

    def test_every_answer_is_an_existing_cause(self):
        source = Path(r.__file__).read_text(encoding="utf-8")
        tree_ = ast.parse(source)
        codes = {node.value for node in ast.walk(tree_) if isinstance(node, ast.Constant)
                 and isinstance(node.value, str) and node.value.startswith(("plugin_", "platform_"))}
        self.assertLessEqual(codes, {"plugin_file_missing", "plugin_file_unreadable", "plugin_file_untrusted",
                                     "plugin_file_too_large", "plugin_directory_untrusted", "platform_unsupported"})


class PurityTests(unittest.TestCase):
    MODULE = Path(r.__file__)

    def test_ctypes_is_touched_only_inside_functions(self):
        tree_ = ast.parse(self.MODULE.read_text(encoding="utf-8"))
        top = set()
        for node in tree_.body:
            if isinstance(node, ast.Import):
                top.update(alias.name.partition(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                top.add((node.module or "").partition(".")[0])
        self.assertLessEqual(top, {"__future__", "sys", "dataclasses", "errors", "windows_read_checks",
                                   "windows_owner_check", ""})
        self.assertNotIn("ctypes", top)

    def test_only_kernel32_and_advapi32_are_loaded(self):
        tree_ = ast.parse(self.MODULE.read_text(encoding="utf-8"))
        loaded = {node.args[0].value for node in ast.walk(tree_)
                  if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "WinDLL"
                  and node.args and isinstance(node.args[0], ast.Constant)}
        self.assertEqual(loaded, {"kernel32", "advapi32"})

    def test_nothing_imports_the_module(self):
        NAME = "windows_handle_reader"
        # These two name this module in a string on purpose (their own expected lists); their imports count.
        MENTION_OK = ("core/recursion_core/plugin_security/tests/test_windows_owner_check.py",
                      "core/recursion_core/plugin_security/tests/test_windows_read_checks.py")
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
        self.assertEqual(users, [])


def _set_attributes(path, attributes):
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    return bool(kernel32.SetFileAttributesW(path, attributes))


class _Proxy:
    """A ctypes library with some functions replaced; every other name goes to the real one."""

    def __init__(self, real, **replaced):
        self._real = real
        self.__dict__.update(replaced)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _set_null_dacl(path):
    """Give ``path`` a NULL access list (everyone may do everything); True when Windows took it."""
    import ctypes
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.SetNamedSecurityInfoW.argtypes = [ctypes.c_wchar_p, ctypes.c_int, ctypes.c_uint32] + [ctypes.c_void_p] * 4
    advapi32.SetNamedSecurityInfoW.restype = ctypes.c_uint32
    return advapi32.SetNamedSecurityInfoW(path, 1, 0x4, None, None, None, None) == 0


@unittest.skipUnless(ON_WINDOWS, "real handles need a Windows host")
class RealHandleTests(unittest.TestCase):
    """Built by a plain user inside one TemporaryDirectory; no elevation, nothing outside it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.alpha = os.path.join(self.root, "alpha")
        os.mkdir(self.alpha)
        self.plugin = os.path.join(self.alpha, "plugin.py")
        with open(self.plugin, "wb") as handle:
            handle.write(DATA)

    def read(self, name="alpha\\plugin.py", limit=1024, **kwargs):
        return r.read_plugin_file(self.root, name, limit, **kwargs)

    def assertRefused(self, code, name="alpha\\plugin.py", **kwargs):
        with self.assertRaises(Refused) as caught:
            self.read(name, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))

    def test_clean_plugin_reads_its_bytes(self):
        self.assertEqual(self.read(), DATA)
        gil = getattr(sys, "_is_gil_enabled", None)
        if gil is not None and sys._xoptions.get("gil") == "0":
            self.assertFalse(gil())

    def test_oversize_is_too_large(self):
        self.assertRefused("plugin_file_too_large", limit=len(DATA) - 1)
        self.assertEqual(self.read(limit=len(DATA)), DATA)

    def test_missing(self):
        self.assertRefused("plugin_file_missing", name="alpha\\absent.py")
        self.assertIsNone(self.read(name="alpha\\absent.py", missing_ok=True))
        self.assertRefused("plugin_directory_untrusted", name="beta\\plugin.py")

    def test_hard_link_is_refused_never_read(self):
        os.link(self.plugin, os.path.join(self.alpha, "hard.py"))
        for name in ("alpha\\plugin.py", "alpha\\hard.py"):
            with self.subTest(name=name), self.assertRaises(Refused) as caught:
                self.read(name)
            self.assertIn(caught.exception.code, ("plugin_file_untrusted", "plugin_file_unreadable"))

    def _junction(self, link, target):
        done = subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True)
        if done.returncode:
            self.skipTest("mklink /J did not succeed as this user")

    def test_junction_as_a_directory_step(self):
        target = os.path.join(self.root, "target")
        os.mkdir(target)
        with open(os.path.join(target, "plugin.py"), "wb") as handle:
            handle.write(DATA)
        self._junction(os.path.join(self.root, "jdir"), target)
        self.assertRefused("plugin_directory_untrusted", name="jdir\\plugin.py")
        self.assertEqual(self.read("target\\plugin.py"), DATA)

    def test_junction_where_the_file_is_asked(self):
        target = os.path.join(self.root, "target")
        os.mkdir(target)
        self._junction(os.path.join(self.alpha, "leaf.py"), target)
        self.assertRefused("plugin_file_unreadable", name="alpha\\leaf.py")

    def test_symlink_leaf_when_a_plain_user_may_make_one(self):
        link = os.path.join(self.alpha, "sym.py")
        try:
            os.symlink(self.plugin, link)
        except OSError:
            self.skipTest("this user may not create a symlink (no privilege, no Developer Mode)")
        self.assertRefused("plugin_file_unreadable", name="alpha\\sym.py")

    def test_case_difference_is_refused(self):
        self.assertRefused("plugin_file_unreadable", name="alpha\\Plugin.py")
        self.assertRefused("plugin_directory_untrusted", name="ALPHA\\plugin.py")

    def test_stream_name_is_refused_and_the_main_stream_is_read(self):
        with open(self.plugin + ":s", "wb") as handle:
            handle.write(b"stream")
        self.assertRefused("plugin_file_unreadable", name="alpha\\plugin.py:s")
        self.assertEqual(self.read(), DATA)

    def test_trailing_dot_file(self):
        made = "\\\\?\\" + os.path.join(self.alpha, "dot.py.")
        with open(made, "wb") as handle:
            handle.write(b"d")
        self.addCleanup(os.remove, made)  # only a \\?\ name can remove it; runs before the folder's cleanup
        self.assertRefused("plugin_file_unreadable", name="alpha\\dot.py.")
        self.assertRefused("plugin_file_missing", name="alpha\\dot.py")

    def test_file_held_open_for_write_is_unreadable(self):
        with open(self.plugin, "ab"):
            self.assertRefused("plugin_file_unreadable")
        self.assertEqual(self.read(), DATA)

    def test_offline_bit_is_untrusted(self):
        if not _set_attributes(self.plugin, w.FILE_ATTRIBUTE_OFFLINE | w.FILE_ATTRIBUTE_ARCHIVE):
            self.skipTest("this user may not set the offline attribute")
        self.addCleanup(_set_attributes, self.plugin, w.FILE_ATTRIBUTE_ARCHIVE)
        self.assertRefused("plugin_file_untrusted")

    def test_folder_others_may_write_is_untrusted(self):
        done = subprocess.run(["icacls", self.alpha, "/grant", "*S-1-1-0:(W)"], capture_output=True)
        if done.returncode:
            self.skipTest("icacls grant did not succeed as this user")
        self.assertRefused("plugin_directory_untrusted")

    def test_file_others_may_write_is_untrusted(self):
        done = subprocess.run(["icacls", self.plugin, "/grant", "*S-1-1-0:(W)"], capture_output=True)
        if done.returncode:
            self.skipTest("icacls grant did not succeed as this user")
        self.assertRefused("plugin_file_untrusted")

    def test_root_reached_through_a_junction_is_pinned_by_its_target(self):
        link = os.path.join(self._tmp.name, "rootlink")
        self._junction(link, self.root)
        self.assertEqual(r.read_plugin_file(link, "alpha\\plugin.py", 1024), DATA)

    def test_the_pinned_root_is_in_volume_guid_form(self):
        native = r._load_native()
        handle, error = native.open(self.root, True, True)
        self.assertIsNotNone(handle, error)
        try:
            final = native.final_path(handle)
        finally:
            native.close(handle)
        self.assertTrue(final.startswith("\\\\?\\Volume{"), "not in volume GUID form")
        self.assertNotIn(os.path.splitdrive(self.root)[0], final)

    def test_held_handles_share_read_only(self):
        # While the reader holds them, nobody may open the folder or the file for delete (rename, replace,
        # remove) or the file for write, even when sharing everything itself; reading stays allowed.
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                         ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        invalid = ctypes.c_void_p(-1).value

        def opens(path, access):
            handle = kernel32.CreateFileW(path, access, 0x7, None, 3, 0x02000000 | 0x00200000, None)
            if handle is None or handle == invalid:
                return ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            return 0

        native = r._load_native()
        real_read = native.read
        seen = {}

        def read(handle, size):
            if not seen:
                seen.update({"folder delete": opens(self.alpha, 0x10000), "file delete": opens(self.plugin, 0x10000),
                             "file write": opens(self.plugin, 0x2), "file read": opens(self.plugin, 0x1)})
            return real_read(handle, size)

        native.read = read
        self.assertEqual(r.read_plugin_file(self.root, "alpha\\plugin.py", 1024, native=native), DATA)
        self.assertEqual(seen, {"folder delete": 32, "file delete": 32, "file write": 32, "file read": 0})
        self.assertEqual(opens(self.plugin, 0x10000), 0)  # released afterwards

    def test_a_step_is_opened_as_itself_not_followed(self):
        target = os.path.join(self.root, "target")
        os.mkdir(target)
        link = os.path.join(self.root, "jdir")
        self._junction(link, target)
        native = r._load_native()
        handle, error = native.open(link, True, False)
        self.assertIsNotNone(handle, error)
        try:
            attributes, tag = native.attribute_tag(handle)
        finally:
            native.close(handle)
        self.assertEqual(tag, w.IO_REPARSE_TAG_MOUNT_POINT)
        self.assertTrue(attributes & w.FILE_ATTRIBUTE_REPARSE_POINT)

    # -- the real access-list walk (first read P2): each case built by this user inside the temp folder --

    def icacls(self, path, *args):
        done = subprocess.run(["icacls", path, *args], capture_output=True)
        if done.returncode:
            self.skipTest("icacls did not succeed as this user")

    def fresh(self, stem):
        path = os.path.join(self.alpha, stem + ".py")
        with open(path, "wb") as handle:
            handle.write(DATA)
        return path

    def test_a_null_access_list_on_the_file_is_untrusted(self):
        if not _set_null_dacl(self.plugin):
            self.skipTest("SetNamedSecurityInfoW did not take a NULL access list")
        self.assertRefused("plugin_file_untrusted")

    def test_a_null_access_list_on_the_folder_is_untrusted(self):
        if not _set_null_dacl(self.alpha):
            self.skipTest("SetNamedSecurityInfoW did not take a NULL access list")
        self.assertRefused("plugin_directory_untrusted")

    def test_a_read_only_grant_to_everyone_still_reads(self):
        self.icacls(self.plugin, "/grant", "*S-1-1-0:(RX)")
        self.icacls(self.alpha, "/grant", "*S-1-1-0:(RX)")
        self.assertEqual(self.read(), DATA)

    def test_single_write_bits_for_everyone_on_the_file_are_untrusted(self):
        for right in ("WD", "AD", "WDAC", "D", "WEA", "WA", "WO"):
            with self.subTest(right=right):
                self.fresh("bit_" + right)
                self.icacls(os.path.join(self.alpha, "bit_" + right + ".py"), "/grant", f"*S-1-1-0:({right})")
                self.assertRefused("plugin_file_untrusted", name="alpha" + SEP + "bit_" + right + ".py")

    def test_delete_child_refuses_on_the_folder_and_passes_on_the_file(self):
        self.icacls(self.plugin, "/grant", "*S-1-1-0:(DC)")
        self.assertEqual(self.read(), DATA)
        self.icacls(self.alpha, "/grant", "*S-1-1-0:(DC)")
        self.assertRefused("plugin_directory_untrusted")

    def test_an_inherit_only_grant_passes_the_folder_and_refuses_the_file_that_inherited_it(self):
        self.icacls(self.alpha, "/grant", "*S-1-1-0:(OI)(CI)(IO)(F)")
        self.assertRefused("plugin_file_untrusted")  # the folder passed: the refusal is the file's
        child = os.path.join(self.alpha, "beta")
        os.mkdir(child)
        with open(os.path.join(child, "plugin.py"), "wb") as handle:
            handle.write(DATA)
        self.assertRefused("plugin_directory_untrusted", name="alpha" + SEP + "beta" + SEP + "plugin.py")

    def test_a_deny_entry_is_ignored(self):
        self.icacls(self.plugin, "/deny", "*S-1-1-0:(WD)")
        self.icacls(self.alpha, "/deny", "*S-1-1-0:(WD)")
        self.assertEqual(self.read(), DATA)

    def test_an_access_entry_that_cannot_be_read_refuses(self):
        native = r._load_native()
        native.adv = _Proxy(native.adv, GetAce=lambda *args: 0)
        with self.assertRaises(Refused) as caught:
            r.read_plugin_file(self.root, "alpha" + SEP + "plugin.py", 1024, native=native)
        self.assertEqual(caught.exception.code, "plugin_directory_untrusted")
        self.assertEqual(caught.exception.detail, "root")

    def test_a_null_owner_pointer_is_unknown(self):
        native = r._load_native()
        real = native.adv.GetSecurityInfo

        def without_owner(*args):
            answer = real(*args)
            args[3]._obj.value = None  # the owner out-pointer, as if Windows had no owner to give
            return answer

        native.adv = _Proxy(native.adv, GetSecurityInfo=without_owner)
        with self.assertRaises(Refused) as caught:
            r.read_plugin_file(self.root, "alpha" + SEP + "plugin.py", 1024, native=native)
        self.assertEqual((caught.exception.code, caught.exception.detail), ("plugin_directory_untrusted", "root"))

    # -- rules the first read found unnoticed (P6) --

    def test_every_open_shares_read_only_and_carries_the_sqos_bit(self):
        native = r._load_native()
        real = native.k32.CreateFileW
        calls = []

        def spy(name, access, share, security, disposition, flags, template):
            calls.append((access, share, flags))
            return real(name, access, share, security, disposition, flags, template)

        native.k32 = _Proxy(native.k32, CreateFileW=spy)
        self.assertEqual(r.read_plugin_file(self.root, "alpha" + SEP + "plugin.py", 1024, native=native), DATA)
        self.assertEqual(len(calls), 3)
        for index, (access, share, flags) in enumerate(calls):
            with self.subTest(open=index):
                self.assertEqual(share, 0x1)
                self.assertEqual(access, 0x1 | 0x80 | 0x20000 | 0x100000)
                self.assertTrue(flags & 0x00100000, "SECURITY_SQOS_PRESENT (= FILE_FLAG_OPEN_NO_RECALL)")
                self.assertEqual(bool(flags & 0x00200000), index > 0, "only the root is followed")
                self.assertEqual(bool(flags & 0x02000000), index < 2, "backup semantics for folders only")

    def test_a_remote_answer_refuses(self):
        import ctypes
        for label in ("success means remote", "another error means unknown"):
            with self.subTest(label):
                native = r._load_native()
                real = native.k32.GetFileInformationByHandleEx

                def query(handle, kind, buffer, size, label=label, real=real):
                    if kind != 13:
                        return real(handle, kind, buffer, size)
                    if label == "success means remote":
                        return 1
                    ctypes.set_last_error(5)
                    return 0

                native.k32 = _Proxy(native.k32, GetFileInformationByHandleEx=query)
                with self.assertRaises(Refused) as caught:
                    r.read_plugin_file(self.root, "alpha" + SEP + "plugin.py", 1024, native=native)
                self.assertEqual((caught.exception.code, caught.exception.detail),
                                 ("plugin_directory_untrusted", "root"))

    def test_the_real_read_never_asks_one_readfile_for_more_than_a_chunk(self):
        native = r._load_native()
        with self.assertRaises(ValueError):
            native.read(None, (1 << 20) + 1)
        with self.assertRaises(ValueError):
            native.read(None, 2**32 + 5)


if __name__ == "__main__":
    unittest.main()
