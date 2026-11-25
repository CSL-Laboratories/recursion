"""The wall's proof tries every kind of write too, and a wall that leaves one open is not proven.

Walls are applied in CHILD interpreters only (stand-in walls that leave writes open are built there from the
real one); the tests in this process replace the attempts or run them unwalled on a temporary folder of their
own. Every file and folder the probe is handed is made by the test for the purpose; a stand-in of a person's
file sits beside them and is never touched.
"""
from __future__ import annotations

import errno
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from recursion_core.plugin_host import wall_linux
from recursion_core.plugin_host.tests.test_wall_linux import CORE, SCHEMA, _interpreter, _why_not_here

CHILD_TIMEOUT_S = 60  # a bound on one child process of this test; test-side only, not a product figure

# The kernel's Landlock rights, written here again so that the tests do not take them from the module they
# check (include/uapi/linux/landlock.h).
RIGHT = {"write_file": 1 << 1, "remove_dir": 1 << 4, "remove_file": 1 << 5, "make_char": 1 << 6,
         "make_dir": 1 << 7, "make_reg": 1 << 8, "make_sock": 1 << 9, "make_fifo": 1 << 10,
         "make_block": 1 << 11, "make_sym": 1 << 12, "refer": 1 << 13, "truncate": 1 << 14}
EVERY_WRITE = sum(RIGHT.values())
KINDS = ("open_for_write", "truncate", "create_file", "create_folder", "make_fifo", "make_symlink", "make_link",
         "make_char_device", "make_block_device", "make_socket_node", "remove_file", "remove_folder")
KIND_RIGHT = {"open_for_write": "write_file", "truncate": "truncate", "create_file": "make_reg",
              "create_folder": "make_dir", "make_fifo": "make_fifo", "make_symlink": "make_sym", "make_link": "make_reg",
              "make_char_device": "make_char", "make_block_device": "make_block", "make_socket_node": "make_sock",
              "remove_file": "remove_file", "remove_folder": "remove_dir"}
REFUSED = ("refused_by_landlock", "refused_by_filter")
NOT_TRIED = ("device_ioctl", "move_between_folders", "rename_within_folder", "change_owner", "change_times",
             "change_extended_attributes", "write_through_open_descriptor", "write_beneath_other_folder")
DEVICES = ("make_char_device", "make_block_device")
CODE_NAME = re.compile(r"\b[a-z]+_[a-z_]+\b")

CHILD = r'''
import json, struct, sys
spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["core"])
from recursion_core.plugin_host import wall_linux as w
if spec["abi_cap"]:
    real_abi = w._landlock_abi
    w._landlock_abi = lambda libc: min(real_abi(libc), spec["abi_cap"])
real_attr = w._ruleset_attr
def weaker(abi):  # the real ruleset with the rights of spec["drop"] left unhandled: a stand-in of a weaker wall
    data, scopes = real_attr(abi)
    return struct.pack("<Q", struct.unpack_from("<Q", data)[0] & ~spec["drop"]) + data[8:], scopes
w._ruleset_attr = weaker
if spec["wall"]:
    w.apply_wall(spec["paths"])
record = w.probe_wall(spec["canary"], spec["daemon"], write_file=spec["file"], write_folder=spec["folder"])
print(json.dumps({"record": record, "words": w.why_not_proven(record)}))
'''


def run_writes_child(*, drop: int = 0, wall: bool = True, abi_cap: int = 0, give: bool = True) -> tuple[dict, dict]:
    """(the child's JSON line, what the test saw afterwards) for one child that walls itself (or not) and probes."""
    with tempfile.TemporaryDirectory() as top:
        home = os.path.join(top, "home")
        os.mkdir(home)
        canary = os.path.join(home, "canary")
        with open(canary, "w", encoding="utf-8") as handle:
            handle.write("the host's test file, stand-in")
        folder = os.path.join(top, "write-probe")
        os.mkdir(folder)
        wall_linux.prepare_write_folder(folder)
        yours = os.path.join(top, "yours")  # a stand-in of a person's file: the probe is never handed it
        with open(yours, "w", encoding="utf-8") as handle:
            handle.write("a person's text, stand-in")
        daemon = subprocess.Popen(_interpreter() + ["-c", "import time; time.sleep(120)"], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, cwd=top, env={"HOME": home, "TMPDIR": top})
        try:
            spec = {"core": str(CORE), "canary": canary, "daemon": daemon.pid,
                    "paths": sorted({sys.base_prefix, sys.prefix}), "file": canary if give else None,
                    "folder": folder if give else None, "drop": drop, "abi_cap": abi_cap, "wall": wall}
            child = subprocess.run(_interpreter() + ["-c", CHILD, json.dumps(spec)], capture_output=True, text=True,
                                   stdin=subprocess.DEVNULL, timeout=CHILD_TIMEOUT_S, cwd=top,
                                   env={"HOME": home, "TMPDIR": top})
        finally:
            daemon.kill()
            daemon.wait()
        with open(canary, encoding="utf-8") as handle:
            canary_text = handle.read()
        with open(yours, encoding="utf-8") as handle:
            yours_text = handle.read()
        seen = {"folder": sorted(os.listdir(folder)), "canary": canary_text, "yours": yours_text,
                "top": sorted(os.listdir(top))}
    lines = child.stdout.strip().splitlines()
    if child.returncode != 0 or not lines:
        raise AssertionError(f"the child ended {child.returncode}: {child.stderr[-1500:]}")
    return json.loads(lines[-1]), seen


def _kernel_abi() -> int:
    return wall_linux._landlock_abi(wall_linux._libc())  # a query only: nothing is applied here


def _schema_errors(record) -> list:
    try:
        import jsonschema
    except ImportError:
        return []
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    return [error.message for error in jsonschema.Draft202012Validator(schema).iter_errors(record)]


@unittest.skipIf(_why_not_here(), _why_not_here() or "")
class WritesInAChildTests(unittest.TestCase):

    def assert_harmless(self, seen):
        self.assertEqual(seen["canary"], "the host's test file, stand-in", "the probe's file keeps its text")
        self.assertEqual(seen["yours"], "a person's text, stand-in")
        self.assertEqual(seen["top"], ["home", "write-probe", "yours"], "nothing is made beside the folder given")

    def test_the_wall_refuses_every_write_kind_and_says_by_which_part(self):
        result, seen = run_writes_child()
        record = result["record"]
        truncate_by = "refused_by_landlock" if record["landlock_abi"] >= 3 else "refused_by_filter"
        self.assertEqual(record["writes"], dict(dict.fromkeys(KINDS, "refused_by_landlock"), truncate=truncate_by))
        self.assertIs(record["proven"], True)
        self.assertEqual(result["words"], [])
        self.assertEqual(seen["folder"], ["remove-me", "remove-me.d", "write-me"], "nothing made, nothing removed")
        self.assert_harmless(seen)
        self.assertEqual(_schema_errors(record), [])

    def test_a_wall_that_leaves_every_write_open_is_not_proven(self):
        result, seen = run_writes_child(drop=EVERY_WRITE)
        record = result["record"]
        self.assertTrue(all(record[name] for name in wall_linux.RECORD_CHECKS), "every other check still holds")
        self.assertEqual([kind for kind in KINDS if record["writes"][kind] in REFUSED], [], "no write kind is refused")
        self.assertEqual({kind: record["writes"][kind] for kind in KINDS if kind not in DEVICES},
                         dict.fromkeys([kind for kind in KINDS if kind not in DEVICES], "let_through"))
        for kind in DEVICES:  # an unprivileged user's block device is refused by the kernel itself, not the wall
            self.assertIn(record["writes"][kind], ("let_through", "refused_elsewhere"), kind)
        self.assertIs(record["proven"], False)
        made = sorted(name for kind, name in wall_linux.WRITE_FOLDER_NAMES.items()
                      if not kind.startswith("remove_") and record["writes"][kind] == "let_through")
        self.assertEqual(seen["folder"], sorted(made + ["write-me"]), "what went through is really there, and both removals took place")
        self.assert_harmless(seen)
        words = result["words"]
        self.assertEqual((words[0], words[-1]), (wall_linux.WORDS["not_loaded"], wall_linux.WORDS["what_to_do"]))
        self.assertIn(wall_linux.WORDS["let_through"].format(what=wall_linux.WORDS["open_for_write"]), words)
        self.assertEqual(_schema_errors(record), [])

    def test_a_wall_that_leaves_only_truncation_open_is_not_proven(self):
        if _kernel_abi() < 3:
            self.skipTest("below Landlock ABI 3 the ruleset has no truncation right to leave out")
        result, seen = run_writes_child(drop=RIGHT["truncate"])
        record = result["record"]
        self.assertEqual([kind for kind in KINDS if record["writes"][kind] not in REFUSED], ["truncate"])
        self.assertEqual(record["writes"]["truncate"], "let_through")
        self.assertIs(record["proven"], False)
        self.assertEqual(seen["folder"], ["remove-me", "remove-me.d", "write-me"])
        self.assert_harmless(seen)
        self.assertEqual(result["words"], [wall_linux.WORDS["not_loaded"],
                                           wall_linux.WORDS["let_through"].format(what=wall_linux.WORDS["truncate"]),
                                           wall_linux.WORDS["what_to_do"]])

    def test_below_abi_3_the_filter_refuses_truncation_and_the_record_says_so(self):
        result, seen = run_writes_child(abi_cap=2)
        record = result["record"]
        self.assertEqual(record["landlock_abi"], 2)
        self.assertEqual(record["writes"], dict(dict.fromkeys(KINDS, "refused_by_landlock"), truncate="refused_by_filter"))
        self.assertIs(record["proven"], True)
        self.assert_harmless(seen)

    def test_without_a_file_and_a_folder_nothing_is_tried_and_nothing_is_proven(self):
        result, seen = run_writes_child(give=False)
        record = result["record"]
        self.assertEqual(record["writes"], dict.fromkeys(KINDS, "not_tried"))
        self.assertIs(record["proven"], False)
        self.assertIn(wall_linux.WORDS["not_tried"].format(what=wall_linux.WORDS["remove_folder"]), result["words"])
        self.assertEqual(record["writes_not_tried"], wall_linux.WRITES_NOT_TRIED)
        self.assertEqual(seen["folder"], ["remove-me", "remove-me.d", "write-me"])

    def test_unwalled_every_write_kind_goes_through_so_the_probe_is_not_vacuous(self):
        result, seen = run_writes_child(wall=False)
        record = result["record"]
        self.assertEqual([kind for kind in KINDS if record["writes"][kind] in REFUSED], [])
        self.assertEqual(record["writes"]["open_for_write"], "let_through")
        self.assertIs(record["proven"], False)
        self.assert_harmless(seen)


@unittest.skipUnless(sys.platform.startswith("linux"), "the write kinds are Linux calls")
class WriteOutcomeTests(unittest.TestCase):
    """In this process, unwalled: the attempts run on a temporary folder of the test's own, or are stand-ins."""

    def state(self, **change):
        return mock.patch.dict(wall_linux._STATE, dict({"applied": ["landlock_restrict_self", "seccomp_filter"],
                                                        "failed": None, "landlock_handled": EVERY_WRITE,
                                                        "filter_truncate": False}, **change))

    def test_a_refusal_is_credited_only_to_the_part_that_holds_the_kind(self):
        by = wall_linux._refused_by
        with self.state():
            self.assertEqual(by("open_for_write", errno.EACCES), "refused_by_landlock")
            self.assertEqual(by("open_for_write", errno.EPERM), "refused_elsewhere", "the filter holds no open")
            self.assertEqual(by("truncate", errno.EPERM), "refused_elsewhere", "no truncation rows loaded")
        with self.state(filter_truncate=True):
            self.assertEqual(by("truncate", errno.EPERM), "refused_by_filter")
            self.assertEqual(by("remove_file", errno.EPERM), "refused_elsewhere")
        for kind in KINDS:
            with self.subTest(kind=kind), self.state(landlock_handled=EVERY_WRITE & ~RIGHT[KIND_RIGHT[kind]]):
                self.assertEqual(by(kind, errno.EACCES), "refused_elsewhere", "a right the ruleset leaves unhandled")
        with self.state(applied=[]):
            self.assertEqual(by("create_file", errno.EACCES), "refused_elsewhere", "no ruleset in force")

    def test_a_change_that_raised_after_it_took_place_is_let_through(self):
        with tempfile.TemporaryDirectory() as top, self.state():
            name = os.path.join(top, "made")

            def make_then_refuse():
                os.mkdir(name)
                raise PermissionError(errno.EACCES, "a stand-in refusal after the change")
            self.assertEqual(wall_linux._attempt("create_folder", make_then_refuse, lambda: os.path.isdir(name)),
                             "let_through")
            self.assertEqual(wall_linux._attempt("create_folder", lambda: (_ for _ in ()).throw(
                PermissionError(errno.EACCES, "stand-in")), lambda: False), "refused_by_landlock")
            self.assertEqual(wall_linux._attempt("create_folder", lambda: (_ for _ in ()).throw(
                OSError(errno.EIO, "stand-in"))), "error")

    def test_unwalled_every_attempt_goes_through_and_touches_only_what_it_was_given(self):
        with tempfile.TemporaryDirectory() as top:
            folder, file = os.path.join(top, "w"), os.path.join(top, "f")
            os.mkdir(folder)
            wall_linux.prepare_write_folder(folder)
            with open(file, "w", encoding="utf-8") as handle:
                handle.write("kept")
            outcomes = wall_linux._write_outcomes(file, folder)
            with open(file, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "kept")
            self.assertEqual(sorted(os.listdir(top)), ["f", "w"])
            listing = sorted(os.listdir(folder))
        self.assertEqual(tuple(outcomes), KINDS)
        self.assertEqual({k: v for k, v in outcomes.items() if k not in DEVICES},
                         dict.fromkeys([k for k in KINDS if k not in DEVICES], "let_through"))
        self.assertIn("made-char", listing)
        self.assertNotIn("remove-me", listing)

    def test_a_file_or_folder_the_probe_may_not_use_is_not_tried(self):
        with tempfile.TemporaryDirectory() as top:
            empty = os.path.join(top, "empty")
            os.mkdir(empty)
            taken = os.path.join(top, "taken")
            os.mkdir(taken)
            wall_linux.prepare_write_folder(taken)
            os.mkdir(os.path.join(taken, "made-file"))
            with self.assertRaises(ValueError):
                wall_linux.prepare_write_folder(taken)
            link = os.path.join(top, "link")
            os.symlink(empty, link)
            target = os.path.join(top, "target")
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("x")
            shortcut = os.path.join(top, "shortcut")
            os.symlink(target, shortcut)
            readonly = os.path.join(top, "readonly")
            with open(readonly, "w", encoding="utf-8") as handle:
                handle.write("x")
            os.chmod(readonly, 0o400)
            folder_kinds = [k for k in KINDS if k not in ("open_for_write", "truncate")]
            for case, (file, folder) in {"no folder made for it": (None, empty), "a name already there": (None, taken),
                                         "a link to a folder": (None, link), "a folder that is not there":
                                         (None, os.path.join(top, "absent"))}.items():
                with self.subTest(case=case):
                    outcomes = wall_linux._write_outcomes(file, folder)
                    self.assertEqual([outcomes[k] for k in folder_kinds], ["not_tried"] * len(folder_kinds))
            for case, file in {"a link to a file": shortcut, "a file that is not there": os.path.join(top, "absent"),
                               "a file its mode keeps from writing": readonly}.items():
                if case.startswith("a file its mode") and os.geteuid() == 0:
                    continue  # the superuser writes whatever the mode says
                with self.subTest(case=case):
                    outcomes = wall_linux._write_outcomes(file, None)
                    self.assertEqual((outcomes["open_for_write"], outcomes["truncate"]), ("not_tried", "not_tried"))
            self.assertEqual(sorted(os.listdir(empty)), [])
            self.assertEqual(sorted(os.listdir(taken)), ["made-file", "remove-me", "remove-me.d", "write-me"])


def _record(writes: dict) -> dict:
    stand_ins = {fn: _refuse for fn in ("_read_root", "_read_canary", "_start_program", "_open_sockets",
                                         "_signal_other", "_reach_other", "_change_mode", "_kernel_surface")}
    with mock.patch.multiple(wall_linux, _write_outcomes=lambda *_a, **_k: dict(writes), _landlock_abi=lambda libc: 6,
                             **dict(stand_ins, **ALONE)), \
            mock.patch.dict(wall_linux._STATE, {"failed": None}):
        return wall_linux.probe_wall("/nonexistent/canary", 1)


# The process alone, holding nothing open for writing: stand-ins, for the probes run in this process (the runner's own
# threads and log files are not the record's to judge).
ALONE = {"_threads_now": lambda: 1, "_descriptors_open_for_write": lambda: [], "_maps_open_for_write": lambda: [],
         "_walled_here": lambda: True, "_descriptors_unaccounted": lambda: []}  # as if apply_wall ran here


def _refuse(*_args, **_kwargs):
    raise PermissionError("a stand-in refusal")


@unittest.skipUnless(sys.platform.startswith("linux"), "the probe is Linux-only")
class ProvenNeedsEveryWriteKindTests(unittest.TestCase):
    """In this process, with every check and every write kind replaced by a stand-in: nothing is walled."""

    def test_every_kind_refused_by_the_part_that_holds_it_is_proven(self):
        writes = dict(dict.fromkeys(KINDS, "refused_by_landlock"), truncate="refused_by_filter")
        self.assertIs(_record(writes)["proven"], True)

    def test_any_one_kind_not_refused_by_the_wall_is_not_proven(self):
        for kind in KINDS:
            for outcome in ("refused_elsewhere", "let_through", "error", "not_tried"):
                with self.subTest(kind=kind, outcome=outcome):
                    record = _record(dict(dict.fromkeys(KINDS, "refused_by_landlock"), **{kind: outcome}))
                    self.assertEqual([k for k in KINDS if record["writes"][k] not in REFUSED], [kind])
                    self.assertIs(record["proven"], False)
                    words = wall_linux.why_not_proven(record)
                    self.assertEqual(len(words), 3, words)
                    self.assertIn(wall_linux.WORDS[kind], words[1])

    def test_a_probe_given_no_file_or_folder_is_never_proven(self):
        stand_ins = {fn: _refuse for fn in ("_read_root", "_read_canary", "_start_program", "_open_sockets",
                                             "_signal_other", "_reach_other", "_change_mode", "_kernel_surface")}
        with mock.patch.multiple(wall_linux, _landlock_abi=lambda libc: 6, **dict(stand_ins, **ALONE)), \
                mock.patch.dict(wall_linux._STATE, {"failed": None}):
            record = wall_linux.probe_wall("/nonexistent/canary", 1)
        self.assertEqual(record["writes"], dict.fromkeys(KINDS, "not_tried"))
        self.assertIs(record["proven"], False)


class WriteDefinitionTests(unittest.TestCase):

    def test_the_kinds_outcomes_and_names_are_the_walls_own_definition(self):
        self.assertEqual(wall_linux.WRITE_KINDS, KINDS)
        self.assertEqual(wall_linux.WRITE_OUTCOMES, ("refused_by_landlock", "refused_by_filter", "refused_elsewhere",
                                                     "let_through", "error", "not_tried"))
        self.assertEqual(sorted(wall_linux.WRITES_NOT_TRIED), sorted(NOT_TRIED))
        self.assertEqual({kind: RIGHT[name] for kind, name in KIND_RIGHT.items()}, wall_linux._LANDLOCK_RIGHT)
        self.assertEqual(set(wall_linux.WRITE_FOLDER_NAMES), set(KINDS) - {"open_for_write", "truncate"})
        self.assertEqual(len(set(wall_linux.WRITE_FOLDER_NAMES.values())), len(wall_linux.WRITE_FOLDER_NAMES))

    def test_the_schema_holds_every_kind_and_proven_needs_each(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        writes = schema["properties"]["writes"]
        draft4 = tuple(kind for kind in KINDS if kind != "make_socket_node")  # the kinds every record carries
        self.assertEqual(tuple(writes["required"]), draft4)
        self.assertEqual(set(writes["properties"]), set(KINDS))
        self.assertIs(writes["additionalProperties"], False)
        self.assertEqual(tuple(schema["$defs"]["write_outcome"]["enum"]), wall_linux.WRITE_OUTCOMES)
        self.assertEqual(tuple(schema["$defs"]["write_refused"]["enum"]), REFUSED)
        self.assertEqual(schema["$defs"]["landlock_refused"]["enum"], ["refused_by_landlock"])
        self.assertEqual(schema["then"]["properties"]["writes"]["properties"],
                         {kind: {"$ref": "#/$defs/write_refused" if kind == "truncate" else "#/$defs/landlock_refused"}
                          for kind in KINDS})
        self.assertEqual(schema["then"]["properties"]["writes"]["required"], ["make_socket_node"])
        not_tried = schema["properties"]["writes_not_tried"]
        self.assertEqual(set(not_tried["required"]) | set(schema["then"]["properties"]["writes_not_tried"]["required"]),
                         set(wall_linux.WRITES_NOT_TRIED), "a proven record names every kind the probe does not try")
        self.assertIn("writes", schema["required"])
        self.assertIn("writes_not_tried", schema["required"])


class WordsTests(unittest.TestCase):

    def test_one_table_holds_a_word_for_every_check_kind_and_outcome(self):
        needed = set(wall_linux.RECORD_CHECKS) | set(KINDS) | {"let_through", "refused_elsewhere", "error", "not_tried",
                                                               "not_refused", "not_loaded", "what_to_do",
                                                               "no_landlock", "other_arch", "partial"}
        self.assertEqual(needed - set(wall_linux.WORDS), set())
        for key, text in wall_linux.WORDS.items():
            with self.subTest(key=key):
                self.assertIsNone(CODE_NAME.search(text), "no code name in the words a person reads")

    def test_a_proven_record_has_no_words_and_an_older_record_names_every_write_kind(self):
        self.assertEqual(wall_linux.why_not_proven({"proven": True}), [])
        older = dict(dict.fromkeys(wall_linux.RECORD_CHECKS, True), landlock_abi=3, arch="x86_64", proven=False)
        words = wall_linux.why_not_proven(older)
        self.assertEqual(words, [wall_linux.WORDS["not_loaded"]]
                         + [wall_linux.WORDS["not_tried"].format(what=wall_linux.WORDS[kind]) for kind in KINDS]
                         + [wall_linux.WORDS["what_to_do"]])

    def test_every_sentence_comes_from_the_table(self):
        record = dict(dict.fromkeys(wall_linux.RECORD_CHECKS, True), reads_outside=False, landlock_abi=0, arch="riscv64",
                      writes=dict(dict.fromkeys(KINDS, "refused_by_landlock"), truncate="let_through",
                                  remove_file="refused_elsewhere", make_fifo="error", make_link="something else"),
                      proven=False)
        words = wall_linux.why_not_proven(record)
        W = wall_linux.WORDS
        self.assertEqual(words, [W["not_loaded"], W["not_refused"].format(what=W["reads_outside"]),
                                 W["let_through"].format(what=W["truncate"]), W["error"].format(what=W["make_fifo"]),
                                 W["error"].format(what=W["make_link"]),
                                 W["refused_elsewhere"].format(what=W["remove_file"]), W["no_landlock"],
                                 W["other_arch"].format(arch="riscv64"), W["what_to_do"]])
        held = dict(dict.fromkeys(wall_linux.RECORD_CHECKS, True), landlock_abi=3, arch="x86_64", proven=False,
                    writes=dict.fromkeys(KINDS, "refused_by_landlock"))
        self.assertEqual(wall_linux.why_not_proven(held), [W["not_loaded"], W["partial"], W["what_to_do"]])


# A second thread, a file held open for writing, a socket file and the kinds not tried: each keeps a record from
# proven, and the record says why. Walls in CHILD interpreters only, each in a temporary folder of the test's own.
CHILD_ROUND = r'''
import json, os, sys, threading
spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["core"])
from recursion_core.plugin_host import wall_linux as w
if spec["abi_cap"]:
    real_abi = w._landlock_abi
    w._landlock_abi = lambda libc: min(real_abi(libc), spec["abi_cap"])
status_fd = os.open("/proc/self/status", os.O_RDONLY | os.O_CLOEXEC)  # opened before any wall, for reading only
def status():
    text = os.pread(status_fd, 65536, 0).decode()
    rows = dict(line.split(":", 1) for line in text.splitlines() if ":" in line)
    return {key: rows[key].strip() for key in ("NoNewPrivs", "Seccomp") if key in rows}
gate, started = threading.Event(), []
def start_thread():
    worker = threading.Thread(target=gate.wait)
    worker.start()
    started.append(worker)
mode, out, paths = spec["mode"], {}, spec["paths"]
if mode == "generator_thread":      # a thread starts while the argument itself is read
    def lazy():
        start_thread()
        yield from spec["paths"]
    paths = lazy()
elif mode == "fspath_thread":       # a thread starts while the paths are opened, after the first count
    class Late:
        def __init__(self, path):
            self.path = path
        def __fspath__(self):
            if not started:
                start_thread()
            return self.path
    paths = [Late(path) for path in spec["paths"]]
elif mode == "thread_in_filter":    # a thread starts after Landlock, while the filter is built
    real_filter = w._filter
    def filter_then_thread(*args, **kwargs):
        start_thread()
        return real_filter(*args, **kwargs)
    w._filter = filter_then_thread
held = os.open(spec["held"], os.O_WRONLY | os.O_CLOEXEC) if mode == "descriptor" else None
try:
    w.apply_wall(paths)
    out["apply"] = {"code": "applied", "step": ""}
except w.WallRefused as exc:
    out["apply"] = {"code": exc.code, "step": exc.step, "detail": exc.detail}
out["status"] = status()
if mode == "thread_before_probe":   # a thread starts after the wall, before the probe
    start_thread()
record = w.probe_wall(spec["canary"], spec["daemon"], write_file=spec["canary"], write_folder=spec["folder"])
out.update(record=record, words=w.why_not_proven(record), threads_started=len(started), held=held)
if held is not None:
    try:
        os.ftruncate(held, 0)
        out["ftruncate"] = "let_through"
    except OSError as exc:
        out["ftruncate"] = exc.errno
gate.set()
for worker in started:
    worker.join()
print(json.dumps(out))
'''
HELD_TEXT = "a file the host held open, stand-in"


def run_round_child(mode: str, *, abi_cap: int = 0) -> tuple[dict, dict]:
    """(the child's JSON line, what the test saw afterwards) for one child that walls itself in ``mode``."""
    with tempfile.TemporaryDirectory() as top:
        home = os.path.join(top, "home")
        os.mkdir(home)
        canary = os.path.join(home, "canary")
        with open(canary, "w", encoding="utf-8") as handle:
            handle.write("the host's test file, stand-in")
        folder = os.path.join(top, "write-probe")
        os.mkdir(folder)
        wall_linux.prepare_write_folder(folder)
        held = os.path.join(top, "held")
        with open(held, "w", encoding="utf-8") as handle:
            handle.write(HELD_TEXT)
        daemon = subprocess.Popen(_interpreter() + ["-c", "import time; time.sleep(120)"], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, cwd=top, env={"HOME": home, "TMPDIR": top})
        try:
            spec = {"core": str(CORE), "mode": mode, "canary": canary, "daemon": daemon.pid, "held": held,
                    "paths": sorted({sys.base_prefix, sys.prefix}), "folder": folder, "abi_cap": abi_cap}
            child = subprocess.run(_interpreter() + ["-c", CHILD_ROUND, json.dumps(spec)], capture_output=True,
                                   text=True, stdin=subprocess.DEVNULL, timeout=CHILD_TIMEOUT_S, cwd=top,
                                   env={"HOME": home, "TMPDIR": top})
        finally:
            daemon.kill()
            daemon.wait()
        with open(held, encoding="utf-8") as handle:
            seen = {"held": handle.read(), "top": sorted(os.listdir(top))}
    lines = child.stdout.strip().splitlines()
    if child.returncode != 0 or not lines:
        raise AssertionError(f"the child ({mode}) ended {child.returncode}: {child.stderr[-1500:]}")
    return json.loads(lines[-1]), seen


UNWALLED = {"NoNewPrivs": "0", "Seccomp": "0"}


@unittest.skipIf(_why_not_here(), _why_not_here() or "")
class RoundInAChildTests(unittest.TestCase):

    def test_a_thread_started_while_the_argument_is_read_is_refused_before_any_change(self):
        result, _ = run_round_child("generator_thread")
        self.assertEqual(result["threads_started"], 1)
        self.assertEqual(result["apply"]["code"], "wall_threads_running")
        self.assertEqual(result["apply"]["detail"], wall_linux.WORDS["second_thread"].format(threads=2))
        self.assertEqual(result["status"], UNWALLED, "nothing was walled, in part or whole")
        self.assertIs(result["record"]["proven"], False)
        self.assertEqual(result["record"]["threads"], 2)

    def test_a_thread_started_while_the_paths_are_opened_is_refused_before_landlock(self):
        result, _ = run_round_child("fspath_thread")
        self.assertEqual(result["threads_started"], 1)
        self.assertEqual((result["apply"]["code"], result["apply"]["step"]), ("wall_partial", "threads_before_landlock"))
        self.assertEqual(result["status"]["Seccomp"], "0", "no filter was loaded")
        self.assertIs(result["record"]["proven"], False)
        self.assertIn(wall_linux.WORDS["second_thread"].format(threads=2), result["words"])

    def test_a_thread_started_after_landlock_is_refused_after_the_filter(self):
        result, _ = run_round_child("thread_in_filter")
        self.assertEqual(result["threads_started"], 1)
        self.assertEqual((result["apply"]["code"], result["apply"]["step"]), ("wall_partial", "threads_after_filter"))
        self.assertIs(result["record"]["proven"], False)

    def test_a_thread_started_after_the_wall_keeps_the_record_from_proven(self):
        result, _ = run_round_child("thread_before_probe")
        self.assertEqual(result["apply"]["code"], "applied")
        record = result["record"]
        self.assertEqual(record["threads"], 2)
        self.assertTrue(all(record[name] for name in wall_linux.RECORD_CHECKS), "every check still holds")
        self.assertIs(record["proven"], False)
        self.assertEqual(result["words"][:2], [wall_linux.WORDS["not_loaded"],
                                               wall_linux.WORDS["second_thread"].format(threads=2)])
        self.assertEqual(_schema_errors(record), [])

    def test_a_file_held_open_for_writing_keeps_the_record_from_proven_and_is_named(self):
        result, seen = run_round_child("descriptor")
        self.assertEqual(result["apply"]["code"], "applied")
        record = result["record"]
        self.assertEqual(record["descriptors_open_for_write"], [{"descriptor": result["held"], "kind": "regular_file"}])
        self.assertIs(record["proven"], False)
        self.assertIn(wall_linux.WORDS["descriptor_open"].format(kind=wall_linux.WORDS["regular_file"]), result["words"])
        self.assertEqual(_schema_errors(record), [])
        if record["landlock_abi"] >= 3:  # what the record says is true: Landlock does not hold a file opened before
            self.assertEqual(result["ftruncate"], "let_through")
            self.assertEqual(seen["held"], "", "the stand-in held open was cut through its descriptor")

    def test_below_abi_3_the_filter_refuses_a_truncation_through_a_held_descriptor(self):
        result, seen = run_round_child("descriptor", abi_cap=2)
        self.assertEqual(result["record"]["landlock_abi"], 2)
        self.assertEqual(result["ftruncate"], errno.EPERM)
        self.assertEqual(seen["held"], HELD_TEXT)
        self.assertIs(result["record"]["proven"], False)

    def test_a_wall_that_leaves_socket_files_open_is_not_proven_and_names_the_kind(self):
        result, seen = run_writes_child(drop=RIGHT["make_sock"])
        record = result["record"]
        self.assertEqual([kind for kind in KINDS if record["writes"][kind] not in REFUSED], ["make_socket_node"])
        self.assertEqual(record["writes"]["make_socket_node"], "let_through")
        self.assertIs(record["proven"], False)
        self.assertEqual(seen["folder"], ["made-socket", "remove-me", "remove-me.d", "write-me"])
        self.assertIn(wall_linux.WORDS["let_through"].format(what=wall_linux.WORDS["make_socket_node"]), result["words"])


@unittest.skipUnless(sys.platform.startswith("linux"), "the probe is Linux-only")
class RoundInThisProcessTests(unittest.TestCase):
    """Nothing is walled here: the counts and the scan run on this process, unwalled, or with stand-ins."""

    def test_the_threads_are_counted_by_a_stat_the_wall_does_not_refuse(self):
        gate = threading.Event()
        worker = threading.Thread(target=gate.wait)
        before = (wall_linux._threads_now(), len(os.listdir("/proc/self/task")))
        worker.start()
        try:
            during = (wall_linux._threads_now(), len(os.listdir("/proc/self/task")))
        finally:
            gate.set()
            worker.join()
        self.assertEqual(before[0], before[1])
        self.assertEqual(during[0], during[1])
        self.assertEqual(during[0], before[0] + 1)

    def test_a_descriptor_open_for_writing_is_found_and_one_for_reading_or_a_pipe_is_not(self):
        with tempfile.TemporaryDirectory() as top:
            path = os.path.join(top, "f")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("x")
            write_fd, read_fd = os.open(path, os.O_WRONLY), os.open(path, os.O_RDONLY)
            pipe_read, pipe_write = os.pipe()
            try:
                held = wall_linux._descriptors_open_for_write()
            finally:
                for fd in (write_fd, read_fd, pipe_read, pipe_write):
                    os.close(fd)
        self.assertIsInstance(held, list)
        numbers = [entry["descriptor"] for entry in held]
        self.assertIn({"descriptor": write_fd, "kind": "regular_file"}, held)
        for fd in (read_fd, pipe_read, pipe_write):
            self.assertNotIn(fd, numbers)

    def test_when_not_every_descriptor_is_seen_the_answer_is_none(self):
        real_stat = os.stat

        def larger(path, *args, **kwargs):
            found = real_stat(path, *args, **kwargs)
            if path == "/proc/self/fd":
                return types.SimpleNamespace(st_size=found.st_size + 1000)
            return found
        with mock.patch.object(wall_linux.os, "stat", larger), \
                mock.patch("resource.getrlimit", lambda which: (256, 256)):
            self.assertIsNone(wall_linux._descriptors_open_for_write())

    def test_a_write_file_with_a_second_name_is_not_tried(self):
        with tempfile.TemporaryDirectory() as top:
            path, other = os.path.join(top, "f"), os.path.join(top, "g")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("x")
            os.link(path, other)
            outcomes = wall_linux._write_outcomes(path, None)
        self.assertEqual((outcomes["open_for_write"], outcomes["truncate"]), ("not_tried", "not_tried"))

    def test_a_second_thread_is_refused_in_words_from_the_table_before_any_change(self):
        state = {"applied": [], "failed": None, "scopes": [], "landlock_handled": 0, "filter_truncate": False}
        with mock.patch.dict(wall_linux._STATE, state), mock.patch.object(wall_linux, "_thread_count", lambda: 3):
            with self.assertRaises(wall_linux.WallRefused) as caught:
                wall_linux.apply_wall([sys.prefix])
            self.assertEqual(wall_linux._STATE["applied"], [])
        self.assertEqual(caught.exception.code, "wall_threads_running")
        self.assertEqual(caught.exception.detail, wall_linux.WORDS["second_thread"].format(threads=3))


def _record_with(**alone) -> dict:
    writes = dict(dict.fromkeys(KINDS, "refused_by_landlock"))
    stand_ins = {fn: _refuse for fn in ("_read_root", "_read_canary", "_start_program", "_open_sockets",
                                         "_signal_other", "_reach_other", "_change_mode", "_kernel_surface")}
    with mock.patch.multiple(wall_linux, _write_outcomes=lambda *_a, **_k: dict(writes), _landlock_abi=lambda libc: 3,
                             **dict(stand_ins, **dict(ALONE, **alone))), \
            mock.patch.dict(wall_linux._STATE, {"failed": None}):
        return wall_linux.probe_wall("/nonexistent/canary", 1)


@unittest.skipUnless(sys.platform.startswith("linux"), "the probe is Linux-only")
class RoundRecordTests(unittest.TestCase):
    """In this process, with every check and every write kind replaced by a stand-in: nothing is walled."""

    def test_alone_and_holding_nothing_is_proven(self):
        record = _record_with()
        self.assertEqual((record["threads"], record["descriptors_open_for_write"]), (1, []))
        self.assertIs(record["proven"], True)

    def test_a_second_thread_or_a_held_file_or_an_unseen_descriptor_is_not_proven_and_says_why(self):
        W = wall_linux.WORDS
        held = [{"descriptor": 7, "kind": "block_device"}]
        for alone, words in (({"_threads_now": lambda: 2}, [W["second_thread"].format(threads=2)]),
                             ({"_threads_now": lambda: 0}, [W["threads_unknown"]]),
                             ({"_descriptors_open_for_write": lambda: held},
                              [W["descriptor_open"].format(kind=W["block_device"])]),
                             ({"_descriptors_open_for_write": lambda: None}, [W["descriptors_unknown"]])):
            with self.subTest(words=words):
                record = _record_with(**alone)
                self.assertIs(record["proven"], False)
                self.assertEqual(wall_linux.why_not_proven(record), [W["not_loaded"]] + words + [W["what_to_do"]])

    def test_the_kinds_not_tried_are_the_tables_and_no_tried_kind_is_among_them(self):
        record = _record_with()
        self.assertEqual(tuple(record["writes_not_tried"]), NOT_TRIED)
        self.assertEqual(record["writes_not_tried"], wall_linux.WRITES_NOT_TRIED)
        for kind, reason in record["writes_not_tried"].items():
            with self.subTest(kind=kind):
                self.assertIn(reason, set(wall_linux.WRITES_NOT_TRIED.values()))
        self.assertEqual(set(record["writes_not_tried"]) & set(record["writes"]), set())
        self.assertEqual(set(record["writes_not_tried"]) & set(wall_linux.WRITE_KINDS), set())
        self.assertEqual(tuple(record["writes"]), KINDS)

    def test_below_abi_3_ftruncate_is_refused_by_the_program_and_from_abi_3_it_is_not(self):
        from recursion_core.plugin_host.tests.test_wall_linux_reach import _run_program
        for arch, number in (("x86_64", 77), ("aarch64", 46)):
            with self.subTest(arch=arch):
                self.assertEqual(_run_program(wall_linux._filter(arch, 2), number, arch=arch), "eperm")
                self.assertEqual(_run_program(wall_linux._filter(arch, 3), number, arch=arch), "allow")


def _fixture(name: str) -> dict:
    return json.loads((SCHEMA.parent / "fixtures" / "good" / "wall.v1" / name).read_text(encoding="utf-8"))


class RoundSchemaTests(unittest.TestCase):

    def setUp(self):
        try:
            import jsonschema  # noqa: F401
        except ImportError:
            self.skipTest("jsonschema is not installed for this interpreter")

    def test_a_proven_record_at_abi_6_carries_both_scopes(self):
        h02 = _fixture("h02-a-proven-wall-on-aarch64.json")
        self.assertEqual((h02["landlock_abi"], h02["landlock_scopes"]), (6, ["abstract_unix_socket", "signal"]))
        self.assertEqual(_schema_errors(h02), [])
        self.assertNotEqual(_schema_errors(dict(h02, landlock_scopes=[])), [])
        self.assertNotEqual(_schema_errors(dict(h02, landlock_scopes=["signal"])), [])
        self.assertEqual(_schema_errors(dict(h02, landlock_abi=5, landlock_scopes=[])), [])

    def test_a_proven_record_credits_only_truncation_to_the_filter(self):
        h01 = _fixture("h01-a-proven-wall.json")
        self.assertEqual(_schema_errors(dict(h01, writes=dict(h01["writes"], truncate="refused_by_filter"))), [])
        for kind in KINDS:
            if kind != "truncate":
                with self.subTest(kind=kind):
                    self.assertNotEqual(_schema_errors(dict(h01, writes=dict(h01["writes"], **{kind: "refused_by_filter"}))), [])

    def test_a_proven_record_ran_alone_and_held_no_file_and_names_every_kind_not_tried(self):
        h01 = _fixture("h01-a-proven-wall.json")
        self.assertEqual(_schema_errors(h01), [])
        for change in ({"threads": 2}, {"descriptors_open_for_write": [{"descriptor": 3, "kind": "regular_file"}]},
                       {"descriptors_open_for_write": None},
                       {"writes_not_tried": {k: v for k, v in h01["writes_not_tried"].items() if k != "change_owner"}},
                       {"writes": {k: v for k, v in h01["writes"].items() if k != "make_socket_node"}}):
            with self.subTest(change=sorted(change)):
                self.assertNotEqual(_schema_errors(dict(h01, **change)), [])
        for name in ("threads", "descriptors_open_for_write"):
            with self.subTest(missing=name):
                self.assertNotEqual(_schema_errors({k: v for k, v in h01.items() if k != name}), [])

    def test_a_draft_4_record_that_is_not_proven_still_validates(self):
        older = _fixture("h15-a-draft-4-record-that-is-not-proven-still-reads.json")
        self.assertNotIn("threads", older)
        self.assertIn("make_socket_node", older["writes_not_tried"])
        self.assertEqual(_schema_errors(older), [])


if __name__ == "__main__":
    unittest.main()
