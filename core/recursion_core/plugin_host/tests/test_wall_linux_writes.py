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
         "make_char_device", "make_block_device", "remove_file", "remove_folder")
KIND_RIGHT = {"open_for_write": "write_file", "truncate": "truncate", "create_file": "make_reg",
              "create_folder": "make_dir", "make_fifo": "make_fifo", "make_symlink": "make_sym", "make_link": "make_reg",
              "make_char_device": "make_char", "make_block_device": "make_block", "remove_file": "remove_file",
              "remove_folder": "remove_dir"}
REFUSED = ("refused_by_landlock", "refused_by_filter")
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
                                   timeout=CHILD_TIMEOUT_S, cwd=top, env={"HOME": home, "TMPDIR": top})
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
        self.assertEqual(seen["folder"], ["remove-me", "remove-me.d"], "nothing made, nothing removed")
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
        self.assertEqual(seen["folder"], made, "what went through is really there, and both removals took place")
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
        self.assertEqual(seen["folder"], ["remove-me", "remove-me.d"])
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
        self.assertEqual(seen["folder"], ["remove-me", "remove-me.d"])

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
            self.assertEqual(sorted(os.listdir(taken)), ["made-file", "remove-me", "remove-me.d"])


def _record(writes: dict) -> dict:
    stand_ins = {fn: _refuse for fn in ("_read_root", "_read_canary", "_start_program", "_open_sockets",
                                         "_signal_other", "_reach_other", "_change_mode", "_kernel_surface")}
    with mock.patch.multiple(wall_linux, _write_outcomes=lambda *_a, **_k: dict(writes), _landlock_abi=lambda libc: 6,
                             **stand_ins), mock.patch.dict(wall_linux._STATE, {"failed": None}):
        return wall_linux.probe_wall("/nonexistent/canary", 1)


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
        with mock.patch.multiple(wall_linux, _landlock_abi=lambda libc: 6, **stand_ins), \
                mock.patch.dict(wall_linux._STATE, {"failed": None}):
            record = wall_linux.probe_wall("/nonexistent/canary", 1)
        self.assertEqual(record["writes"], dict.fromkeys(KINDS, "not_tried"))
        self.assertIs(record["proven"], False)


class WriteDefinitionTests(unittest.TestCase):

    def test_the_kinds_outcomes_and_names_are_the_walls_own_definition(self):
        self.assertEqual(wall_linux.WRITE_KINDS, KINDS)
        self.assertEqual(wall_linux.WRITE_OUTCOMES, ("refused_by_landlock", "refused_by_filter", "refused_elsewhere",
                                                     "let_through", "error", "not_tried"))
        self.assertEqual(sorted(wall_linux.WRITES_NOT_TRIED), ["device_ioctl", "make_socket_node", "move_between_folders"])
        self.assertEqual({kind: RIGHT[name] for kind, name in KIND_RIGHT.items()}, wall_linux._LANDLOCK_RIGHT)
        self.assertEqual(set(wall_linux.WRITE_FOLDER_NAMES), set(KINDS) - {"open_for_write", "truncate"})
        self.assertEqual(len(set(wall_linux.WRITE_FOLDER_NAMES.values())), len(wall_linux.WRITE_FOLDER_NAMES))

    def test_the_schema_holds_every_kind_and_proven_needs_each(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        writes = schema["properties"]["writes"]
        self.assertEqual(tuple(writes["required"]), KINDS)
        self.assertEqual(set(writes["properties"]), set(KINDS))
        self.assertIs(writes["additionalProperties"], False)
        self.assertEqual(tuple(schema["$defs"]["write_outcome"]["enum"]), wall_linux.WRITE_OUTCOMES)
        self.assertEqual(tuple(schema["$defs"]["write_refused"]["enum"]), REFUSED)
        self.assertEqual(schema["then"]["properties"]["writes"]["properties"],
                         {kind: {"$ref": "#/$defs/write_refused"} for kind in KINDS})
        self.assertEqual(set(schema["properties"]["writes_not_tried"]["required"]), set(wall_linux.WRITES_NOT_TRIED))
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


if __name__ == "__main__":
    unittest.main()
