"""The canonical writers on master compared, over the 50 vectors of research prompt 157 and a generated corpus.

Every input goes through every writer and reader that takes it (``canonical_record.py`` says which and
how). The record of what each one gives is kept in ``schemas/canonical/results/master-<head>.json``;
this module fails when a fresh run on this interpreter differs from that record. The record is made on
CPython 3.12 and on the free-threaded 3.14 and is the same file for both, so a run on either that
differs from it means the two interpreters' results differ.

It asserts only four named checks:

(a) W1 and W2 (``csl-cj/v0``) give the same bytes or both refuse, on every input;
(b) W3 and W4 (``rfc8785/v1``) give the same bytes or both refuse, on every input;
(c) on every input W1 gives bytes for, they equal W3's, except the inputs holding U+007F, which the
    record lists under "expected difference: U+007F, refused by #577";
(d) W1 under eight threads that change the same value either refuses or gives the bytes of one
    consistent value, never others (the capture and recheck of ``canonical``; it bites on the
    free-threaded build, and runs on every build).

Any other difference between writers is recorded, never asserted away: the record's ``differences``.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import threading
import unittest

from recursion_core.plugin_security.errors import Refused
from recursion_core.plugin_security.tests import canonical_record as rec

# By module path: the package names a function ``canonical`` that hides the module of that name.
cj = importlib.import_module("recursion_core.plugin_security.canonical")

HEAD = "ba82fb923ff0a290e8e9d10e03a3a14dad552250"
RECORD = rec.RESULTS / f"master-{HEAD}.json"
VECTORS_BLOCK_SHA256 = "e03f2d2e88aebfa46def5508a0ca5e59e8343151c36bf07c950c731e086e0999"


class CanonicalWritersComparedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inputs = rec.all_inputs()
        cls.full = rec.run_all(cls.inputs)
        cls.found = rec.checks(cls.full, cls.inputs)

    def test_the_vectors_are_the_fifty_of_research_prompt_157(self):
        lines = rec.vector_lines()
        self.assertEqual(hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest(), VECTORS_BLOCK_SHA256)
        data = json.loads(rec.VECTORS.read_text(encoding="utf-8"))
        self.assertEqual([item["id"] for item in data["vectors"]], [f"V{n:02d}" for n in range(1, 51)])
        self.assertEqual(data["source"]["block_sha256"], VECTORS_BLOCK_SHA256)
        for item in data["vectors"]:
            self.assertIn("rfc8785_expected", item)
            self.assertIn("sup_expected", item)
            self.assertEqual(sum(key in item for key in ("input_hex", "input_formula")), 1, item["id"])

    def test_the_corpus_is_what_its_generator_makes(self):
        self.assertEqual(rec.CORPUS.read_text(encoding="utf-8"), rec.corpus_text(rec.build_corpus()))

    def test_a_w1_and_w2_give_the_same_bytes_or_both_refuse(self):
        self.assertEqual(self.found["a"], [], self._show(self.found["a"], ("W1", "W2")))

    def test_b_w3_and_w4_give_the_same_bytes_or_both_refuse(self):
        self.assertEqual(self.found["b"], [], self._show(self.found["b"], ("W3", "W4")))

    def test_c_w1_bytes_equal_w3_bytes_where_w1_gives_bytes_u007f_listed(self):
        self.assertEqual(self.found["c"], [], self._show(self.found["c"], ("W1", "W3")))
        recorded = json.loads(RECORD.read_text(encoding="utf-8"))
        self.assertEqual(recorded[rec.DEL_LISTING], self.found["del_listed"])
        self.assertTrue(self.found["del_listed"], "no input holding U+007F reached W1")

    def test_d_w1_under_eight_threads_changing_one_value(self):
        shared = {"a": ["x"], "b": 1, "k": "ok"}
        stop = threading.Event()
        bad: list = []
        seen = {"bytes": 0, "refused": 0}
        lock = threading.Lock()

        def change(step: int) -> None:
            moves = (
                lambda: shared["a"].append("x"),
                lambda: shared["a"].pop() if shared["a"] else None,
                lambda: shared.__setitem__("k", "\u00e9"),
                lambda: shared.__setitem__("k", "ok"),
                lambda: shared.__setitem__("b", 2**60),
                lambda: shared.__setitem__("b", 1),
                lambda: shared.__setitem__(1, 1),
                lambda: shared.pop(1, None),
                lambda: shared.__setitem__("f", 1.5),
                lambda: shared.pop("f", None),
            )
            moves[step % len(moves)]()

        def worker(index: int) -> None:
            step = index
            while not stop.is_set():
                try:
                    change(step)
                except (IndexError, KeyError, RuntimeError):
                    pass
                step += 3
                try:
                    out = cj.canonical(shared)
                except Refused:
                    with lock:
                        seen["refused"] += 1
                    continue
                try:
                    again = cj.parse(out)        # canonical bytes of an in-form value, or not
                except Refused as exc:
                    with lock:
                        bad.append((out, exc.code))
                    continue
                if set(again) - {"a", "b", "k"} or again.get("k", "ok") != "ok" or again.get("b", 1) != 1:
                    with lock:
                        bad.append((out, "a value never in the form"))
                with lock:
                    seen["bytes"] += 1
                if seen["bytes"] + seen["refused"] >= 16000:
                    stop.set()

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        stop.set()
        self.assertFalse(any(thread.is_alive() for thread in threads), "a thread did not end")
        self.assertEqual(bad[:3], [])
        self.assertGreater(seen["bytes"], 0)

    def test_a_fresh_run_on_this_interpreter_equals_the_record(self):
        fresh = rec.record_text(rec.record(self.inputs, HEAD, rec.vectors_commit()))
        recorded = RECORD.read_text(encoding="utf-8")
        if fresh != recorded:
            old = json.loads(recorded)["outcomes"]
            new = json.loads(fresh)["outcomes"]
            moved = [ident for ident in new if old.get(ident) != new[ident]][:5]
            self.fail(f"a fresh run differs from {RECORD.name} (made on 3.12 and 3.14t alike): first inputs {moved}")

    def _show(self, idents: list, names: tuple) -> str:
        return "; ".join(f"{ident}: " + ", ".join(f"{name}={self.full[ident][name][:80]}" for name in names)
                         for ident in idents[:5])


if __name__ == "__main__":
    unittest.main()
