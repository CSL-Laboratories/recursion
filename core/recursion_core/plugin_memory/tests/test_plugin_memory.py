"""A plugin's saved data through the host channel: two daemon ends over a stand-in core and the real store.

Evidence classes: every test runs the channel's two ends and the store in-process, over SQLite files in a
temporary folder the test made; no plugin host process and no real daemon start exists in these tests (the
daemon-side reader that builds a store for a real host is later work). No test starts a thread. ONE helper,
``held_by_a_child``, starts a child process of the test's own that holds a store file's write lock after the
store opened: it is released on every path (its input is closed, it is waited for, killed after a timeout) and
it ends by itself after HOLDER_LIMIT_S whatever the test does; it gets HOME and TMPDIR inside the test's folder.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from recursion_core import settings
from recursion_core.plugin_security import canonical, parse
from recursion_core.plugin_security import host_channel as hc
from recursion_core.plugin_security.manifest import PERMISSIONS

POSIX_ONLY = "the store's folders are owner-only POSIX folders"
if hasattr(os, "geteuid"):
    from recursion_core.plugin_memory import store as ms
    from recursion_core.plugin_memory import words

REPO = Path(__file__).resolve().parents[4]
KEY_A = "ed25519:" + "a" * 32
KEY_B = "ed25519:" + "b" * 32
DAMAGED_NEXT = ("The plugin goes on with no saved data. The damaged file stays in Recursion's state folder for you; no "
                "command of Recursion lists, shows or restores saved data yet.")
DIR_A, DIR_B = "ed25519-" + "a" * 32, "ed25519-" + "b" * 32


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# The child that holds a store file's write lock until its input closes; it ends by itself after HOLDER_LIMIT_S.
HOLDER_LIMIT_S = 60
HOLDER = f"""
import signal, sqlite3, sys
signal.alarm({HOLDER_LIMIT_S})
db = sqlite3.connect(sys.argv[1], isolation_level=None)
db.execute("BEGIN IMMEDIATE")
db.execute("UPDATE totals SET items = items WHERE one = 1")
print("held", flush=True)
sys.stdin.readline()
db.execute("ROLLBACK")
db.close()
"""


class Refusing:
    """A stand-in connection: a statement that begins with ``match`` raises SQLite's error ``code``; every other
    statement and attribute is the real connection's."""

    def __init__(self, db, code, match):
        self._db, self._code, self._match = db, code, match

    def execute(self, sql, *args):
        if sql.lstrip().upper().startswith(self._match):
            exc = sqlite3.OperationalError("stood in")
            exc.sqlite_errorcode = self._code
            raise exc
        return self._db.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._db, name)


def parts(answer):
    return {key: answer.get(key) for key in ("code", "what", "why", "next", "changed")}


class Rows:
    """The settings in force: every declared default, with the given rows changed."""

    def __init__(self, **changes):
        self.values = {row.name: row.default for row in settings.SETTINGS}
        for name, value in changes.items():
            self.values["plugin_memory." + name] = value

    def __getitem__(self, name):
        return self.values[name]


class Garbled:
    """A stand-in core whose answer cannot be read."""

    def client_for(self, caller):
        return lambda request: b"not an answer"


class Core:
    """A stand-in core: it answers plugin.permissions with what is granted now and records every request."""

    def __init__(self, permissions=("read", "memory")):
        self.permissions, self.requests = list(permissions), []

    def client_for(self, caller):
        def call(request):
            self.requests.append(caller)
            message = parse(request)
            if message.get("op") == "plugin.permissions":
                granted = self.permissions if message["args"].get("plugin_id") == caller else []
                return canonical({"protocol": "csl-core/v0", "ok": True, "result": {"permissions": granted}})
            return canonical({"protocol": "csl-core/v0", "ok": False, "refusal": {"code": "unknown_op"}})
        return call


@unittest.skipUnless(hasattr(os, "geteuid"), POSIX_ONLY)
class SavedData(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.home = self.root / "home"
        self.state = self.home / ".local" / "state" / "recursion"
        self.config = self.home / ".config"
        for item in (self.home, self.config):
            item.mkdir(parents=True)
        self.rows = []
        self.frames = []

    def store(self, plugin="demo.notes", identity=KEY_A, **changes):
        memory = ms.PluginMemory(self.state, plugin, identity, settings=Rows(**changes), record=self.rows.append)
        self.addCleanup(memory.close)
        return memory

    def end(self, plugin="demo.notes", memory=None, core=None):
        return hc.DaemonEnd(plugin, core or Core(), memory=memory)

    def ask(self, end, name, **args):
        """One call from a host to the daemon end and its answer, each through a real frame both ways."""
        host = hc.HostEnd({})
        frame = hc.encode(host.call(name, args))
        answer = end.receive(hc.decode(frame[4:], sender="host"))
        back = hc.encode(answer)
        self.frames.extend((frame, back))
        return hc.decode(back[4:], sender="daemon")

    def put(self, end, key, data=b"v", if_revision=None):
        return self.ask(end, "memory.put", key=key, value_b64=b64(data), if_revision=if_revision)

    def value(self, answer):
        self.assertEqual(answer["type"], "result", answer)
        return answer["value"]

    # (1) red first: at the base a put is call_unknown; with the slice a put then a get round-trips
    def test_a_put_then_a_get_round_trips(self):
        end = self.end(memory=self.store())
        self.assertEqual(self.value(self.put(end, "notes/today", b"hello")), {"ok": True, "revision": 1})
        got = self.value(self.ask(end, "memory.get", key="notes/today"))
        self.assertEqual((got["ok"], base64.b64decode(got["value_b64"]), got["revision"]), (True, b"hello", 1))
        self.assertRegex(got["written_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertEqual(self.value(self.ask(end, "memory.list", prefix="", after=None, limit=None)),
                         {"ok": True, "keys": ["notes/today"], "more": False, "after": None})
        self.assertEqual(self.value(self.ask(end, "memory.delete", key="notes/today", if_revision=1)),
                         {"ok": True, "revision": 1})
        self.assertEqual(self.value(self.ask(end, "memory.get", key="notes/today"))["code"], "memory_key_unknown")

    # HM1: another plugin's end never reaches the first plugin's data; nothing names a plugin
    def test_another_plugin_gets_key_unknown_and_no_argument_names_a_plugin(self):
        first = self.end(memory=self.store())
        second = self.end("other.plugin", memory=self.store("other.plugin"))
        self.put(first, "k", b"mine")
        self.assertEqual(self.value(self.ask(second, "memory.get", key="k"))["code"], "memory_key_unknown")
        self.assertEqual(self.value(self.ask(second, "memory.list", prefix="", after=None, limit=None))["keys"], [])
        for name in hc.MEMORY_CALL_NAMES:
            self.assertEqual(set(hc.CALL_NAMES[name]) & {"plugin", "plugin_id", "install", "ns", "namespace"},
                             set())
        forged = self.ask(second, "memory.get", key="k", plugin="demo.notes")
        self.assertEqual((forged["type"], forged["code"]), ("refusal", "call_args_invalid"))
        with self.assertRaises(ValueError):
            self.end("other.plugin", memory=self.store())

    # HM3, (4): the store is keyed by plugin AND install identity; the earlier store is set aside, never handed
    def test_another_identity_finds_nothing_and_the_earlier_store_is_set_aside(self):
        self.put(self.end(memory=self.store()), "k", b"from A")
        reinstalled = self.end(memory=self.store(identity=KEY_B))
        self.assertEqual(self.value(self.ask(reinstalled, "memory.get", key="k"))["code"], "memory_key_unknown")
        self.assertEqual(ms.set_aside(self.state, "demo.notes", KEY_B),
                         [{"install": "ed25519-" + "a" * 32, "why": "another install"}])
        self.assertTrue((self.state / "plugin-memory" / "demo.notes" / ("ed25519-" + "a" * 32) / "memory.sqlite3").is_file())
        aside = [row for row in self.rows if row["call"] == "set_aside"]
        self.assertEqual([(r["identity"], r["install"], r["result"]) for r in aside],
                         [(KEY_B, "ed25519-" + "a" * 32, "set_aside")])
        unsigned = self.end(memory=self.store(identity=ms.identity_for(None, "model")))
        self.assertEqual(self.value(self.ask(unsigned, "memory.get", key="k"))["code"], "memory_key_unknown")
        self.assertEqual(ms.identity_for(None, "person"), "unsigned-person")
        self.assertEqual(ms.identity_for(KEY_A), KEY_A)
        for bad in ((None, None), (None, "plugin"), ("../x", None)):
            with self.assertRaises(ValueError):
                ms.identity_for(*bad)
        info = os.stat(self.state / "plugin-memory" / "demo.notes" / ("ed25519-" + "b" * 32) / "memory.sqlite3")
        self.assertEqual(info.st_mode & 0o777, 0o600)

    # (3), HM6, (11): offered only with both grants; the words say which one is missing; revoked counts at once
    def test_each_missing_grant_is_named_and_a_revocation_counts_at_the_next_call(self):
        core = Core()
        end = self.end(memory=self.store(), core=core)
        self.assertTrue(self.value(self.put(end, "k"))["ok"])
        core.permissions.remove("memory")
        refused = self.put(end, "k", if_revision=1)
        self.assertEqual((refused["type"], refused["code"]), ("refusal", "call_not_offered"))
        self.assertEqual(refused["detail"], "memory.put: demo.notes tried to keep saved data between runs. It was not "
                         "given the permission to keep saved data when it was installed. To allow it, install it again "
                         "and grant it saved data, then restart Recursion. Only your own act widens what a plugin can "
                         "do. No: nothing was saved.")
        no_row = self.ask(self.end(memory=None), "memory.get", key="k")
        self.assertEqual(no_row["code"], "call_not_offered")
        self.assertEqual(no_row["detail"], "memory.get: demo.notes tried to keep saved data between runs. It is not "
                         "listed in plugin_memory.plugins in your settings file. To allow it, add demo.notes to "
                         "plugin_memory.plugins and restart Recursion. Settings are read when Recursion starts. Only "
                         "your own act widens what a plugin can do. No: nothing was saved.")
        self.assertIsNone(ms.for_plugin(self.state, Rows(), "demo.notes", KEY_A, record=self.rows.append))
        granted = ms.for_plugin(self.state, Rows(plugins=("demo.notes",)), "demo.notes", KEY_A, record=self.rows.append)
        self.addCleanup(granted.close)
        self.assertIsInstance(granted, ms.PluginMemory)
        run_request = self.ask(self.end(memory=self.store()), "run.request", record_kind="routine.v1",
                               record_id="r", revision=1, occurrence="2026-11-23T09:00:00Z", tenant="t1",
                               engine="codex", prompt="p")
        self.assertEqual((run_request["code"], run_request["detail"]), ("call_not_offered", "run.request"))

    # (5): revisions
    def test_a_revision_that_moved_writes_nothing(self):
        end = self.end(memory=self.store())
        self.assertEqual(self.value(self.put(end, "k", b"one"))["revision"], 1)
        for revision in (None, 2, 7):
            with self.subTest(if_revision=revision):
                self.assertEqual(self.value(self.put(end, "k", b"lost", revision))["code"], "memory_revision_moved")
        self.assertEqual(self.value(self.put(end, "k", b"two", 1))["revision"], 2)
        self.assertEqual(self.value(self.put(end, "new", b"x", 1))["code"], "memory_revision_moved")
        self.assertEqual(self.value(self.ask(end, "memory.delete", key="k", if_revision=1))["code"],
                         "memory_revision_moved")
        got = self.value(self.ask(end, "memory.get", key="k"))
        self.assertEqual((base64.b64decode(got["value_b64"]), got["revision"]), (b"two", 2))
        self.assertEqual(self.value(self.ask(end, "memory.delete", key="gone", if_revision=None))["code"],
                         "memory_key_unknown")
        self.assertEqual(self.value(self.ask(end, "memory.get", key="new"))["code"], "memory_key_unknown")

    # (1): the key's form and every argument's form, before anything runs
    def test_a_key_out_of_form_or_an_extra_argument_is_refused(self):
        end = self.end(memory=self.store())
        for key in ("", "k" * 257, "é" * 129, "a\nb", "a\x00", "tab\there", "\x85", 7, None):
            with self.subTest(key=key):
                self.assertEqual(self.ask(end, "memory.get", key=key)["code"], "call_args_invalid")
        self.assertTrue(self.value(self.put(end, "k" * 256))["ok"])
        self.assertTrue(self.value(self.put(end, "é" * 128))["ok"])
        for args in ({"key": "k", "value_b64": "not base64!", "if_revision": None},
                     {"key": "k", "value_b64": "", "if_revision": 0},
                     {"key": "k", "value_b64": "", "if_revision": True},
                     {"key": "k", "value_b64": ""}):
            with self.subTest(args=args):
                self.assertEqual(self.ask(end, "memory.put", **args)["code"], "call_args_invalid")
        for args in ({"prefix": None, "after": None, "limit": None}, {"prefix": "", "after": "", "limit": None},
                     {"prefix": "", "after": None, "limit": 0}):
            with self.subTest(args=args):
                self.assertEqual(self.ask(end, "memory.list", **args)["code"], "call_args_invalid")
        self.assertEqual(self.rows[-1]["call"], "memory.put")

    # HM5, HM12, (8): every bound is a row; over it the call is refused naming the row; at none it passes
    def test_each_row_at_a_small_value_refuses_and_at_none_passes(self):
        cases = [("max_bytes_per_plugin", 20, ("k1", b"x" * 10), ("k2", b"x" * 10), "memory_full",
                  "plugin_memory.max_bytes_per_plugin"),
                 ("max_items_per_plugin", 1, ("k1", b"x"), ("k2", b"x"), "memory_full",
                  "plugin_memory.max_items_per_plugin"),
                 ("max_item_bytes", 4, ("k1", b"x"), ("k2", b"x" * 5), "memory_item_too_large",
                  "plugin_memory.max_item_bytes")]
        for row, small, first, second, code, named in cases:
            for value in (small, settings.NO_LIMIT):
                with self.subTest(row=row, value=value):
                    end = self.end(memory=self.store(f"demo.{row.replace('_', '-')}{int(value is settings.NO_LIMIT)}",
                                                     **{row: value}),
                                   plugin=f"demo.{row.replace('_', '-')}{int(value is settings.NO_LIMIT)}")
                    self.assertTrue(self.value(self.put(end, *first))["ok"])
                    answer = self.value(self.put(end, *second))
                    if value is settings.NO_LIMIT:
                        self.assertTrue(answer["ok"])
                        continue
                    self.assertEqual(answer["code"], code)
                    self.assertIn(named, answer["why"])
                    self.assertEqual(self.value(self.ask(end, "memory.get", key=second[0]))["code"], "memory_key_unknown")
        for value in (3, settings.NO_LIMIT):
            with self.subTest(row="calls_per_minute", value=value):
                plugin = f"demo.pace{int(value is settings.NO_LIMIT)}"
                end = self.end(plugin, memory=self.store(plugin, calls_per_minute=value))
                answers = [self.value(self.ask(end, "memory.get", key="k")) for _ in range(4)]
                last = answers[-1]
                if value is settings.NO_LIMIT:
                    self.assertEqual(last["code"], "memory_key_unknown")
                else:
                    self.assertEqual(last["code"], "memory_paced")
                    self.assertIn("plugin_memory.calls_per_minute", last["why"])
        for value, expect in ((2, (["a", "b"], True, "b")), (settings.NO_LIMIT, (["a", "b", "c"], False, None))):
            with self.subTest(row="list_page_max", value=value):
                plugin = f"demo.page{int(value is settings.NO_LIMIT)}"
                end = self.end(plugin, memory=self.store(plugin, list_page_max=value))
                for key in "cab":
                    self.put(end, key)
                page = self.value(self.ask(end, "memory.list", prefix="", after=None, limit=None))
                self.assertEqual((page["keys"], page["more"], page["after"]), expect)
        held = self.store("demo.held", max_item_bytes=10 ** 9)
        self.assertEqual(held.max_item_bytes, ms.MAX_VALUE_BYTES)
        self.assertIn("held to what fits one message", held.shown()[0])

    # (8): a full disk gives memory_full with the disk as the cause, nothing half written
    def test_a_full_disk_is_memory_full_and_nothing_is_written(self):
        memory = self.store(max_bytes_per_plugin=settings.NO_LIMIT, max_item_bytes=settings.NO_LIMIT)
        end = self.end(memory=memory)
        self.put(end, "small", b"x")
        (pages,), = memory._db.execute("PRAGMA page_count").fetchall()
        memory._db.execute(f"PRAGMA max_page_count = {pages + 1}")
        answer = self.value(self.put(end, "big", os.urandom(200_000)))
        self.assertEqual((answer["code"], answer["why"]), ("memory_full", "Your disk is full, so nothing more could be written."))
        self.assertEqual(self.value(self.ask(end, "memory.list", prefix="", after=None, limit=None))["keys"], ["small"])
        self.assertEqual(memory._totals(memory._db)[0], 1)

    # (8), cut: the transaction around a write
    def test_a_write_that_fails_halfway_leaves_nothing(self):
        memory = self.store()
        end = self.end(memory=memory)
        self.put(end, "before", b"x")

        def fail():
            raise OSError("the disk went away")
        memory._between = fail
        for name, args in (("memory.put", {"key": "half", "value_b64": b64(b"y"), "if_revision": None}),
                           ("memory.delete", {"key": "before", "if_revision": None})):
            with self.subTest(call=name):
                answer = self.ask(end, name, **args)
                self.assertEqual((answer["type"], answer["code"], answer["detail"]), ("refusal", "call_failed", "OSError"))
        del memory._between
        self.assertEqual(self.value(self.ask(end, "memory.get", key="half"))["code"], "memory_key_unknown")
        self.assertTrue(self.value(self.ask(end, "memory.get", key="before"))["ok"])
        self.assertEqual(memory._totals(memory._db), (1, len("before") + 1))

    # (6), cut: the frame bound on list. The largest item and the largest page fit one frame; encode never raises
    def test_the_largest_item_and_the_largest_page_fit_one_frame(self):
        memory = self.store(max_item_bytes=settings.NO_LIMIT, max_bytes_per_plugin=settings.NO_LIMIT,
                            list_page_max=settings.NO_LIMIT, calls_per_minute=settings.NO_LIMIT, sync="normal")
        end = self.end(memory=memory)
        longest = '"' * 256
        self.assertEqual(self.value(self.put(end, longest, b"\xff" * ms.MAX_VALUE_BYTES))["revision"], 1)
        got = self.ask(end, "memory.get", key=longest)
        self.assertEqual(len(base64.b64decode(got["value"]["value_b64"])), ms.MAX_VALUE_BYTES)
        over = self.value(self.put(end, "over", b"x" * (ms.MAX_VALUE_BYTES + 1)))
        self.assertEqual(over["code"], "memory_item_too_large")
        self.assertEqual(over["why"], f"One item can be at most {words.size_text(ms.MAX_VALUE_BYTES)}: that is what "
                                      "fits one message between Recursion and a plugin.")
        self.assertEqual(self.value(self.ask(end, "memory.get", key="over"))["code"], "memory_key_unknown")
        self.assertGreater(ms.MAX_VALUE_BYTES, hc.MAX_FRAME_BYTES * 3 // 4 - 2048)
        for i in range(4200):
            memory.put(f"{i:05d}" + "\\" * 251, "", None)
        seen, after, pages = [], None, 0
        while True:
            page = self.ask(end, "memory.list", prefix="0", after=after, limit=None)
            frame = hc.encode(page)
            self.assertLessEqual(len(frame) - 4, hc.MAX_FRAME_BYTES)
            keys, pages = page["value"]["keys"], pages + 1
            seen += keys
            if not page["value"]["more"]:
                break
            self.assertEqual(page["value"]["after"], keys[-1])
            next_key = json.dumps(f"{int(keys[-1][:5]) + 1:05d}" + "\\" * 251).encode()
            self.assertGreater(len(frame) - 4 + len(next_key) + 1 + 600, hc.MAX_FRAME_BYTES)
            after = keys[-1]
        self.assertEqual((len(seen), len(set(seen)), seen == sorted(seen)), (4200, 4200, True))
        self.assertGreaterEqual(pages, 3)
        self.assertEqual(len(self.value(self.ask(end, "memory.list", prefix="000", after=None, limit=None))["keys"]), 100)

    # (7): the namespace and kind columns are held empty by the table itself; no state column
    def test_the_table_holds_namespace_and_kind_empty(self):
        memory = self.store()
        columns = [row[1] for row in memory._db.execute("PRAGMA table_info(items)")]
        self.assertEqual(columns, ["ns", "kind", "key", "value", "revision", "written_at"])
        for column in ("ns", "kind"):
            with self.subTest(column=column), self.assertRaises(sqlite3.IntegrityError):
                memory._db.execute(f"INSERT INTO items ({column}, key, value, revision, written_at) "
                                   "VALUES ('x', 'k', x'00', 1, 't')")
        (journal,), = memory._db.execute("PRAGMA journal_mode").fetchall()
        self.assertEqual(journal, "wal")

    # HM10, (9): a damaged file is set aside for its plugin only
    def test_a_damaged_file_is_set_aside_for_its_plugin_only(self):
        first = self.store()
        self.put(self.end(memory=first), "k")
        first.close()
        path = self.state / "plugin-memory" / "demo.notes" / ("ed25519-" + "a" * 32) / "memory.sqlite3"
        for suffix in ("-wal", "-shm"):
            if os.path.exists(f"{path}{suffix}"):
                os.unlink(f"{path}{suffix}")
        path.write_bytes(b"this is not a database" * 400)
        damaged = self.end(memory=self.store())
        other = self.end("other.plugin", memory=self.store("other.plugin"))
        for name, args in (("memory.get", {"key": "k"}), ("memory.put", {"key": "k", "value_b64": "", "if_revision": None})):
            answer = self.value(self.ask(damaged, name, **args))
            self.assertEqual({key: answer[key] for key in ("code", "what", "why", "next", "changed")},
                             {"code": "memory_unreadable", "what": "demo.notes's saved data could not be read.",
                              "why": "Its file is damaged, so Recursion set it aside and gave the plugin none.",
                              "next": DAMAGED_NEXT, "changed": "The damaged file was moved aside; nothing was deleted."})
        self.assertTrue(self.value(self.put(other, "k"))["ok"])
        listed = ms.set_aside(self.state, "demo.notes", KEY_A)
        self.assertEqual([item["why"] for item in listed], ["damaged"])
        self.assertEqual((path.parent / listed[0]["file"]).read_bytes()[:22], b"this is not a database")
        self.assertIn(("set_aside", "memory_unreadable"), [(r["call"], r["result"]) for r in self.rows])

    # HM4: no path or name of the person's machine in anything a plugin reads back
    def test_no_frame_carries_a_path_of_the_persons_machine(self):
        cases = [self.test_a_put_then_a_get_round_trips, self.test_another_plugin_gets_key_unknown_and_no_argument_names_a_plugin,
                 self.test_each_missing_grant_is_named_and_a_revocation_counts_at_the_next_call,
                 self.test_a_revision_that_moved_writes_nothing, self.test_each_row_at_a_small_value_refuses_and_at_none_passes,
                 self.test_a_full_disk_is_memory_full_and_nothing_is_written, self.test_a_write_that_fails_halfway_leaves_nothing,
                 self.test_a_damaged_file_is_set_aside_for_its_plugin_only]
        for number, case in enumerate(cases):
            self.state = self.home / ".local" / "state" / f"recursion{number}"
            case()
        self.assertGreater(len(self.frames), 60)
        marks = [str(self.root), str(self.home), str(self.state), str(self.config), "plugin-memory", "memory.sqlite3",
                 os.path.basename(str(self.root)), str(self.root).replace("/", "\\/")]
        for frame in self.frames:
            for mark in marks:
                self.assertNotIn(mark.encode("utf-8"), frame)
        self.assertFalse(any(str(self.root) in json.dumps(row) or ms.STORE_FILE in json.dumps(row) for row in self.rows))

    # (10): the record
    def test_one_record_row_per_write_none_per_read(self):
        memory = self.store()
        end = self.end(memory=memory)
        self.put(end, "notes/a", b"12345")
        self.ask(end, "memory.get", key="notes/a")
        self.ask(end, "memory.list", prefix="", after=None, limit=None)
        self.put(end, "notes/a", b"x", None)
        self.ask(end, "memory.delete", key="notes/a", if_revision=1)
        self.assertEqual([(r["call"], r["result"], r["revision"], r["bytes_before"], r["bytes_after"]) for r in self.rows],
                         [("memory.put", "ok", 1, 0, 12), ("memory.put", "memory_revision_moved", None, None, None),
                          ("memory.delete", "ok", 1, 12, 0)])
        first = self.rows[0]
        self.assertEqual((first["plugin"], first["identity"], first["key_length"]), ("demo.notes", KEY_A, 7))
        self.assertEqual(first["key_sha256"], hashlib.sha256(b"notes/a").hexdigest())
        self.assertNotIn("key", first)
        self.assertFalse(first["committed_after_host_ended"])
        end.host_ended()
        self.put(end, "late")
        self.assertTrue(self.rows[-1]["committed_after_host_ended"])
        texts = self.store("demo.texts", record_key_text=True)
        self.put(self.end("demo.texts", memory=texts), "plain")
        self.assertEqual(self.rows[-1]["key"], "plain")
        quiet = self.store("demo.quiet", record_each_write=False)
        quiet_end = self.end("demo.quiet", memory=quiet)
        count = len(self.rows)
        self.put(quiet_end, "a")
        self.put(quiet_end, "b")
        self.assertEqual(len(self.rows), count)
        quiet_end.host_ended()
        self.assertEqual((self.rows[-1]["call"], self.rows[-1]["counts"]), ("summary", {"memory.put ok": 2}))
        with self.assertRaises(TypeError):
            ms.PluginMemory(self.state, "demo.none", KEY_A, settings=Rows(), record=None)

    # HM13: the write-ahead file stays near its row
    def test_the_write_ahead_file_stays_within_its_row(self):
        row = 131072
        memory = self.store(wal_max_bytes=row)
        end = self.end(memory=memory)
        wal = f"{memory._path}-wal"
        largest = one_write = 0
        previous = os.path.getsize(wal) if os.path.exists(wal) else 0
        for i in range(300):
            self.put(end, f"k{i:04d}", os.urandom(2000))
            size = os.path.getsize(wal)
            if size > previous:
                one_write = max(one_write, size - previous)
            largest, previous = max(largest, size), size
        self.assertLess(one_write, row)
        self.assertLessEqual(largest, row + one_write)
        self.assertGreater(300 * 2000, 2 * largest)  # without the row the file would hold every write

    # (2), the schema and the code say the same
    def test_the_schema_and_the_code_say_the_same_of_saved_data(self):
        host = json.loads((REPO / "schemas" / "plugin" / "v1" / "host.v1.json").read_text(encoding="utf-8"))
        rows = {row["name"]: row for row in host["x-call-names"]}
        for name in hc.MEMORY_CALL_NAMES:
            self.assertEqual((tuple(rows[name]["args"]), rows[name]["permission"], rows[name]["plugin_function"]),
                             (hc.CALL_NAMES[name], "memory", None))
        self.assertEqual(list(hc.CALL_NAMES)[-4:], list(hc.MEMORY_CALL_NAMES))
        self.assertEqual(host["x-memory"]["answer_codes"], ["memory_key_unknown", "memory_revision_moved",
                                                            "memory_item_too_large", "memory_full", "memory_unreadable",
                                                            "memory_unavailable", "memory_paced"])
        self.assertEqual(set(host["x-memory"]["answer_codes"]) & set(hc.REFUSAL_CODES), set())
        manifest = json.loads((REPO / "schemas" / "plugin" / "v1" / "manifest.v1.json").read_text(encoding="utf-8"))
        install = json.loads((REPO / "schemas" / "plugin" / "v1" / "install.v1.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["$defs"]["permission"]["enum"][-1], "memory")
        lists = []

        def walk(part):
            if isinstance(part, dict):
                if isinstance(part.get("enum"), list) and "schedule" in part["enum"]:
                    lists.append(part["enum"])
                for inner in part.values():
                    walk(inner)
            elif isinstance(part, list):
                for inner in part:
                    walk(inner)
        walk(install)
        self.assertEqual([found[-1] for found in lists], ["memory"])
        self.assertEqual(PERMISSIONS[-1], hc.MEMORY_PERMISSION)
        self.assertEqual(sorted(set(words.CODE_OF.values()) - {"call_not_offered"}),
                         sorted(host["x-memory"]["answer_codes"]))

    # Round on 08f90f7e. A1: a file that is only busy is not called damaged, not moved, and opened again later
    def test_a_busy_file_is_left_in_place_and_opened_again_at_the_next_call(self):
        first = self.store()
        self.put(self.end(memory=first), "k", b"healthy")
        path = first._path
        first.close()
        held = sqlite3.connect(str(path), isolation_level=None)  # the test's own connection, released below
        try:
            held.execute("PRAGMA locking_mode=EXCLUSIVE")
            held.execute("BEGIN EXCLUSIVE")
            held.execute("UPDATE totals SET items = items WHERE one = 1")
            with mock.patch.object(ms, "OPEN_WAIT_S", 0.05):
                memory = self.store()
                end = self.end(memory=memory)
                answers = [self.value(self.ask(end, "memory.get", key="k")) for _ in range(2)]
        finally:
            if held.in_transaction:
                held.execute("ROLLBACK")
            held.close()
        for answer in answers:
            self.assertEqual({key: answer[key] for key in ("code", "what", "why", "next", "changed")},
                             {"code": "memory_unavailable", "what": "demo.notes's saved data could not be opened just now.",
                              "why": "Recursion could not open its file because another program was using it; the file "
                                     "is not damaged as far as Recursion can tell.",
                              "next": "Recursion tries again at the plugin's next call; if this goes on, end the other "
                                      "program that holds it.",
                              "changed": "Nothing was moved or deleted: the file and its data stay where they were."})
        self.assertTrue(path.is_file())
        self.assertEqual(ms.set_aside(self.state, "demo.notes", KEY_A), [])
        self.assertNotIn("memory_unreadable", [row["result"] for row in self.rows])
        got = self.value(self.ask(end, "memory.get", key="k"))
        self.assertEqual((got["ok"], base64.b64decode(got["value_b64"])), (True, b"healthy"))
        self.assertFalse(memory.unreadable)

    # A2: the row is sent before the commit; a sink that raises leaves nothing written
    def test_a_record_sink_that_raises_rolls_the_write_back(self):
        failing = []

        def sink(row):
            if failing:
                raise OSError("the record could not be written")
            self.rows.append(row)
        memory = ms.PluginMemory(self.state, "demo.notes", KEY_A, settings=Rows(), record=sink)
        self.addCleanup(memory.close)
        end = self.end(memory=memory)
        self.assertTrue(self.value(self.put(end, "kept", b"one"))["ok"])
        failing.append(True)
        for name, args in (("memory.put", {"key": "new", "value_b64": b64(b"x"), "if_revision": None}),
                           ("memory.put", {"key": "kept", "value_b64": b64(b"two"), "if_revision": 1}),
                           ("memory.delete", {"key": "kept", "if_revision": None})):
            with self.subTest(call=name, key=args["key"]):
                answer = self.ask(end, name, **args)
                self.assertEqual((answer["type"], answer["code"], answer["detail"]), ("refusal", "call_failed", "OSError"))
        failing.clear()
        self.assertEqual(self.value(self.ask(end, "memory.get", key="new"))["code"], "memory_key_unknown")
        got = self.value(self.ask(end, "memory.get", key="kept"))
        self.assertEqual((base64.b64decode(got["value_b64"]), got["revision"]), (b"one", 1))
        self.assertEqual(memory._totals(memory._db), (1, len("kept") + 3))
        self.assertEqual([(row["call"], row["result"]) for row in self.rows], [("memory.put", "ok")])

    # A3: going back to an earlier install sets the other store aside WITH a row, as a first install does
    def test_a_return_to_an_earlier_install_records_the_other_store_set_aside(self):
        for identity in (KEY_A, KEY_B):
            self.store(identity=identity).close()
        count = len(self.rows)
        self.store(identity=KEY_A)
        self.assertEqual([(r["call"], r["identity"], r["install"], r["result"]) for r in self.rows[count:]],
                         [("set_aside", KEY_A, DIR_B, "set_aside")])
        self.assertEqual(ms.set_aside(self.state, "demo.notes", KEY_A), [{"install": DIR_B, "why": "another install"}])

    # A5: damage that only SQLite's integrity check finds is set aside too
    def test_damage_only_the_integrity_check_finds_is_set_aside(self):
        first = self.store()
        self.put(self.end(memory=first), "k", b"v")
        path = first._path
        first.close()
        raw = sqlite3.connect(str(path), isolation_level=None)
        try:
            raw.execute("CREATE TABLE extra (a INTEGER)")
            raw.execute("CREATE INDEX extra_a ON extra (a)")
            raw.executemany("INSERT INTO extra (a) VALUES (?)", [(i,) for i in range(300)])
            raw.execute("PRAGMA writable_schema = ON")
            raw.execute("UPDATE sqlite_master SET sql = 'CREATE INDEX extra_a ON extra (a DESC)' WHERE name = 'extra_a'")
        finally:
            raw.close()
        check = sqlite3.connect(str(path))
        try:
            self.assertEqual(check.execute("SELECT key FROM items").fetchall(), [("k",)])
            (verdict,), = check.execute("PRAGMA integrity_check(1)").fetchall()
            self.assertNotEqual(verdict, "ok")
        finally:
            check.close()
        damaged = self.end(memory=self.store())
        self.assertEqual(self.value(self.ask(damaged, "memory.get", key="k"))["code"], "memory_unreadable")
        self.assertEqual([item["why"] for item in ms.set_aside(self.state, "demo.notes", KEY_A)], ["damaged"])

    # A6: pacing answers a code of its own, on a read and a list too
    def test_pacing_has_a_code_of_its_own_on_a_read_or_a_list(self):
        end = self.end(memory=self.store(calls_per_minute=2))
        self.put(end, "k")
        self.ask(end, "memory.get", key="k")
        for name, args in (("memory.get", {"key": "k"}), ("memory.list", {"prefix": "", "after": None, "limit": None})):
            with self.subTest(call=name):
                answer = self.value(self.ask(end, name, **args))
                self.assertEqual((answer["code"], answer["what"]),
                                 ("memory_paced", "demo.notes asked for its saved data too often."))
                self.assertEqual(answer["changed"], "No: nothing of this call was done.")

    # A7: the full words say this write would pass the limit, and count items right
    def test_the_full_words_say_this_write_would_pass_the_limit(self):
        end = self.end(memory=self.store(max_bytes_per_plugin=100))
        self.assertTrue(self.value(self.put(end, "a", b"x" * 89))["ok"])
        self.assertEqual(self.value(self.put(end, "b", b"x" * 20))["why"],
                         "With this write its saved data would pass 100 bytes, the limit in "
                         "plugin_memory.max_bytes_per_plugin.")
        one = self.end("demo.one", memory=self.store("demo.one", max_items_per_plugin=1))
        self.put(one, "a")
        self.assertEqual(self.value(self.put(one, "b"))["why"],
                         "With this write its saved data would pass 1 item, the limit in "
                         "plugin_memory.max_items_per_plugin.")
        self.assertEqual(words.count_text(2), "2 items")

    # A8: both missing grants are named
    def test_both_missing_grants_are_named(self):
        answer = self.ask(self.end(memory=None, core=Core(permissions=("read",))), "memory.get", key="k")
        self.assertEqual(answer["code"], "call_not_offered")
        self.assertEqual(answer["detail"], "memory.get: demo.notes tried to keep saved data between runs. It was not "
                         "given the permission to keep saved data when it was installed, and it is not listed in "
                         "plugin_memory.plugins in your settings file. To allow it, install it again and grant it saved "
                         "data, add demo.notes to plugin_memory.plugins, then restart Recursion. Only your own act "
                         "widens what a plugin can do. No: nothing was saved.")

    # A9: a core that cannot answer is not told to the person as a permission not given at install
    def test_a_core_that_cannot_answer_is_not_called_a_missing_permission(self):
        answer = self.ask(self.end(memory=self.store(), core=Garbled()), "memory.get", key="k")
        self.assertEqual(answer["code"], "call_not_offered")
        self.assertEqual(answer["detail"], "memory.get: demo.notes tried to keep saved data between runs. Recursion "
                         "could not learn whether demo.notes holds the permission to keep saved data, so it allowed "
                         "nothing. The plugin can ask again; if this goes on, restart Recursion. Only your own act "
                         "widens what a plugin can do. No: nothing was saved.")

    # A11: the place of the new permission is pinned, so one inserted before it turns this red
    def test_the_place_of_the_memory_permission_is_pinned(self):
        self.assertEqual((PERMISSIONS.index("schedule"), PERMISSIONS.index("memory"), len(PERMISSIONS)), (7, 8, 9))
        manifest = json.loads((REPO / "schemas" / "plugin" / "v1" / "manifest.v1.json").read_text(encoding="utf-8"))
        enum = manifest["$defs"]["permission"]["enum"]
        self.assertEqual((enum.index("schedule"), enum.index("memory"), len(enum)), (12, 13, 14))

    # A12: the summary row's sentence says what is lost if Recursion stops before the host's end
    def test_the_summary_row_says_what_is_lost_if_recursion_stops_first(self):
        says = settings.DECLARED["plugin_memory.record_each_write"].says
        self.assertIn("if Recursion stops before that the summary of those writes is lost", says)
        self.assertIn("the saved data itself is kept", says)

    # A13: the largest revision names no row to raise; it says what the plugin can do
    def test_the_largest_revision_says_what_the_plugin_can_do(self):
        memory = self.store()
        end = self.end(memory=memory)
        self.put(end, "k")
        memory._db.execute("UPDATE items SET revision = ? WHERE key = 'k'", (hc.MAX_ID,))
        answer = self.value(self.put(end, "k", b"y", hc.MAX_ID))
        self.assertEqual({key: answer[key] for key in ("code", "what", "why", "next", "changed")},
                         {"code": "memory_full", "what": "demo.notes could not change this item of its saved data again.",
                          "why": f"The item is at revision {hc.MAX_ID}, the largest number a message between Recursion "
                                 "and a plugin carries.",
                          "next": "The plugin can delete the item and save it anew, which starts it again at revision 1.",
                          "changed": "No: nothing of this write was saved."})

    # Round on 48cfc2d3: a child of this test holds the store file's write lock, released on every path
    @contextlib.contextmanager
    def held_by_a_child(self, path):
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.home), "TMPDIR": str(self.root)}
        child = subprocess.Popen([sys.executable, "-I", "-c", HOLDER, str(path)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd=str(self.root),
                                 text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "held")
            yield
        finally:
            child.stdin.close()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)
            child.stdout.close()

    def read_only_store(self, files, folder):
        """A healthy store, closed, then opened again with its file, its folder or both read-only."""
        if os.geteuid() == 0:
            self.skipTest("the owner of every file can write it as root")
        first = self.store()
        self.put(self.end(memory=first), "k", b"healthy")
        path = first._path
        first.close()
        names = [f"{path}{suffix}" for suffix in ("", "-wal", "-shm") if os.path.lexists(f"{path}{suffix}")]
        self.addCleanup(lambda: [os.chmod(path.parent / name, 0o600) for name in os.listdir(path.parent)
                                 if name.startswith(path.name)])
        self.addCleanup(os.chmod, path.parent, 0o700)
        for name in names if files else ():
            os.chmod(name, 0o400)
        if folder:
            os.chmod(path.parent, 0o500)
        memory = self.store()
        return memory, self.end(memory=memory), path, names

    # Round on 48cfc2d3. N1: a write that meets a file another program holds AFTER the open is refused in words
    def test_a_file_held_after_the_open_is_refused_in_words_and_nothing_is_written(self):
        with mock.patch.object(ms, "OPEN_WAIT_S", 0.01), mock.patch.object(ms, "CALL_WAIT_S", 0.4, create=True):
            memory = self.store()
        end = self.end(memory=memory)
        self.assertTrue(self.value(self.put(end, "k", b"healthy"))["ok"])
        with self.held_by_a_child(memory._path):
            began = time.monotonic()
            answer = self.value(self.put(end, "k2", b"new"))
            waited = time.monotonic() - began
            got = self.value(self.ask(end, "memory.get", key="k"))
        self.assertEqual(parts(answer), {
            "code": "memory_unavailable", "what": "demo.notes's saved data could not be reached just now.",
            "why": "Recursion could not use its file because another program was using it; the file is not damaged "
                   "as far as Recursion can tell.",
            "next": "The plugin can ask again; if this goes on, end the other program that holds it.",
            "changed": "No: this call wrote nothing, and nothing was moved or deleted: the file and its data stay "
                       "where they were."})
        self.assertGreaterEqual(waited, 0.35)  # the call's own wait, CALL_WAIT_S, not the open's
        self.assertEqual((got["ok"], base64.b64decode(got["value_b64"])), (True, b"healthy"))
        self.assertEqual((self.rows[-1]["call"], self.rows[-1]["result"]), ("memory.put", "memory_unavailable"))
        self.assertTrue(memory._path.is_file())
        self.assertEqual(ms.set_aside(self.state, "demo.notes", KEY_A), [])
        self.assertFalse(memory.unreadable)
        self.assertTrue(self.value(self.put(end, "k2", b"new"))["ok"])
        self.assertEqual(self.value(self.ask(end, "memory.list", prefix="", after=None, limit=None))["keys"],
                         ["k", "k2"])

    # N1: a read that meets a held file, and a write that meets a read-only file, after the open (stood in)
    def test_a_read_or_a_write_after_the_open_that_meets_a_held_or_read_only_file_says_so(self):
        if os.geteuid() == 0:
            self.skipTest("the owner of every file can write it as root")
        memory = self.store()
        end = self.end(memory=memory)
        self.assertTrue(self.value(self.put(end, "k", b"healthy"))["ok"])
        real = memory._db
        for code in (5, 6):
            memory._db = Refusing(real, code, "SELECT VALUE")
            answer = self.value(self.ask(end, "memory.get", key="k"))
            self.assertEqual((answer["code"], answer["why"], answer["next"]), (
                "memory_unavailable",
                "Recursion could not use its file because another program was using it; the file is not damaged "
                "as far as Recursion can tell.",
                "The plugin can ask again; if this goes on, end the other program that holds it."))
        os.chmod(memory._path, 0o400)
        self.addCleanup(os.chmod, memory._path, 0o600)
        memory._db = Refusing(real, 8, "INSERT")
        answer = self.value(self.put(end, "k2", b"new"))
        memory._db = real
        self.assertEqual((answer["code"], answer["why"]), (
            "memory_unavailable", "Recursion could not use its file because the file is read-only; the file is not "
                                  "damaged as far as Recursion can tell."))
        self.assertIn("make the plugin's saved-data file in Recursion's state folder writable again", answer["next"])
        self.assertFalse(real.in_transaction)
        self.assertEqual(self.value(self.ask(end, "memory.list", prefix="", after=None, limit=None))["keys"], ["k"])
        self.assertEqual((self.rows[-1]["call"], self.rows[-1]["result"]), ("memory.put", "memory_unavailable"))

    # N2: a read-only FILE in a writable folder names the file, never the folder
    def test_a_read_only_file_names_the_file_and_the_folder_is_writable(self):
        memory, end, path, names = self.read_only_store(files=True, folder=False)
        answer = self.value(self.ask(end, "memory.get", key="k"))
        self.assertEqual(parts(answer), {
            "code": "memory_unavailable", "what": "demo.notes's saved data could not be opened just now.",
            "why": "Recursion could not open its file because the file is read-only; the file is not damaged as far "
                   "as Recursion can tell.",
            "next": "Recursion tries again at the plugin's next call; if this goes on, make the plugin's saved-data "
                    "file in Recursion's state folder writable again, with the files beside it that begin with its "
                    "name; the folder itself is writable.",
            "changed": "Nothing was moved or deleted: the file and its data stay where they were."})
        # the act the words name: the file and the files beside it that begin with its name (SQLite gives a
        # write-ahead file it makes the mode of the store file, so one may have come since)
        for name in sorted(os.listdir(path.parent)):
            if name.startswith(path.name):
                os.chmod(path.parent / name, 0o600)
        got = self.value(self.ask(end, "memory.get", key="k"))
        self.assertEqual((got["ok"], base64.b64decode(got["value_b64"])), (True, b"healthy"))
        self.assertEqual(ms.set_aside(self.state, "demo.notes", KEY_A), [])

    # N2: a read-only FOLDER names the folder, and says the file itself is writable
    def test_a_read_only_folder_names_the_folder_and_the_file_is_writable(self):
        memory, end, path, names = self.read_only_store(files=False, folder=True)
        answer = self.value(self.ask(end, "memory.get", key="k"))
        self.assertEqual((answer["code"], answer["why"], answer["next"]), (
            "memory_unavailable",
            "Recursion could not open its file because the folder that holds it is read-only; the file is not "
            "damaged as far as Recursion can tell.",
            "Recursion tries again at the plugin's next call; if this goes on, make the folder that holds the "
            "plugin's saved data in Recursion's state folder writable again; the file itself is writable."))
        os.chmod(path.parent, 0o700)
        got = self.value(self.ask(end, "memory.get", key="k"))
        self.assertEqual((got["ok"], base64.b64decode(got["value_b64"])), (True, b"healthy"))
        self.assertTrue(path.is_file())

    # N2: both read-only name both
    def test_a_read_only_file_in_a_read_only_folder_names_both(self):
        memory, end, path, names = self.read_only_store(files=True, folder=True)
        answer = self.value(self.ask(end, "memory.get", key="k"))
        self.assertEqual((answer["code"], answer["why"], answer["next"]), (
            "memory_unavailable",
            "Recursion could not open its file because the file and the folder that holds it are read-only; the "
            "file is not damaged as far as Recursion can tell.",
            "Recursion tries again at the plugin's next call; if this goes on, make the folder that holds the "
            "plugin's saved data in Recursion's state folder writable again, and the files in it."))

    # The settings rows: declared once, each with its shipped default; the grant row's sentence as written
    def test_the_rows_are_settings_with_their_defaults(self):
        expect = {"plugins": (), "max_bytes_per_plugin": 67108864, "max_item_bytes": 262144,
                  "max_items_per_plugin": 100000, "list_page_max": 1000, "calls_per_minute": 6000, "sync": "full",
                  "wal_max_bytes": 16777216, "record_each_write": True, "record_key_text": False}
        self.assertEqual({name: settings.DECLARED["plugin_memory." + name].default for name in expect}, expect)
        self.assertEqual(tuple("plugin_memory." + name for name in expect), ms.ROWS)
        self.assertEqual(settings.DECLARED["plugin_memory.plugins"].says, words.ROW_PLUGINS)
        for name in ("max_bytes_per_plugin", "max_item_bytes", "max_items_per_plugin", "list_page_max",
                     "calls_per_minute", "wal_max_bytes"):
            row = settings.DECLARED["plugin_memory." + name]
            self.assertEqual((row.allow_no_limit, row.floor), (True, 1), name)


class Words(unittest.TestCase):
    """Every refusal a person reads has its four parts, as written; one part taken out turns this red."""

    WRITTEN = {
        "not_granted_permission": ("PLUGIN tried to keep saved data between runs.",
                                   "It was not given the permission to keep saved data when it was installed.",
                                   "To allow it, install it again and grant it saved data, then restart Recursion. "
                                   "Only your own act widens what a plugin can do.", "No: nothing was saved."),
        "not_granted_row": ("PLUGIN tried to keep saved data between runs.",
                            "It is not listed in plugin_memory.plugins in your settings file.",
                            "To allow it, add PLUGIN to plugin_memory.plugins and restart Recursion. Settings are read "
                            "when Recursion starts. Only your own act widens what a plugin can do.",
                            "No: nothing was saved."),
        "memory_full": ("PLUGIN could not save more.", "With this write its saved data would pass SIZE, the limit in ROW.",
                        "Raise that row and restart Recursion, or set it to none for no limit.",
                        "No: nothing of this write was saved."),
        "memory_item_too_large": ("PLUGIN tried to save one item of SIZE.",
                                  "One item can be at most LIMIT: that is what fits one message between Recursion and "
                                  "a plugin.", "The plugin can save it in smaller parts.", "No: nothing was saved."),
        "memory_unreadable": ("PLUGIN's saved data could not be read.",
                              "Its file is damaged, so Recursion set it aside and gave the plugin none.",
                              DAMAGED_NEXT, "The damaged file was moved aside; nothing was deleted."),
        "memory_unavailable": ("PLUGIN's saved data could not be opened just now.",
                               "Recursion could not open its file because CAUSE; the file is not damaged as far as "
                               "Recursion can tell.",
                               "Recursion tries again at the plugin's next call; if this goes on, ACT.",
                               "Nothing was moved or deleted: the file and its data stay where they were."),
    }

    def test_each_refusal_has_what_why_next_and_changed(self):
        from recursion_core.plugin_memory import words as table
        for entry, parts in self.WRITTEN.items():
            self.assertEqual(table.WORDS[entry], parts, entry)
        for entry, parts in table.WORDS.items():
            with self.subTest(entry=entry):
                self.assertEqual(len(parts), 4)
                self.assertTrue(all(part.strip() and part.endswith(".") for part in parts))
                for part in parts:
                    self.assertNotRegex(part, r"\bmemory\b(?!_)")
                    self.assertNotIn("/", part)
        full = table.render("memory_full", "demo.notes", size=table.size_text(67108864),
                            row="plugin_memory.max_bytes_per_plugin")
        self.assertEqual(full["why"], "With this write its saved data would pass 64 MB, the limit in "
                                      "plugin_memory.max_bytes_per_plugin.")


if __name__ == "__main__":
    unittest.main()
