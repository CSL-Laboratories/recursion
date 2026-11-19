"""gui/panels.py's frozen interface, gui.panels/1 (lane PAGE, slice 1, Nov 14 2026).

The plugin host (daemon/plugins.py) imports panel_registry; the page imports filter_venue_rows and
PAPER_BOOK. These tests pin the public names, the signatures, the accepted keys, the refusal
texts and their order, and the shape of the answer. A change to this file is a change to an
interface other lanes build on: it needs the Liaison's word and the importing lane's, never an edit
made to let another change pass.
"""
import inspect
import unittest
from collections import OrderedDict
from collections.abc import Mapping
from types import MappingProxyType

from recursion_core.gui import panels
from recursion_core.gui.client import ACTIONS, READS
from recursion_core.gui.panels import (PANEL_KEYS, PAPER_BOOK, REFUSALS, REQUIRED_PANEL_KEYS,
                                       filter_venue_rows, panel_registry)

NEEDS_KEYS, ONLY_READS, BAD_ID, DUPLICATE_OR_TITLE, BAD_READS, BAD_VENUE, BAD_VENUE_USE, BAD_WITHHOLD = (
    "panel needs id, title and reads",
    "panel only declares reads",
    "invalid panel id",
    "duplicate panel id or empty title",
    "panel reads must be O7 reads",
    "venue must be a venue id",
    "venue_use must be venue:use",
    "withhold_venue_data must be a boolean",
)
GOOD = {"id": "example.panel", "title": "Example", "reads": [READS[0]]}


class _Str(str):
    """A str subclass that claims to equal anything and hashes like the text it carries."""

    def __eq__(self, other):
        return True

    __hash__ = str.__hash__


class _FlipList(list):
    """A list subclass whose second iteration yields something else."""

    def __iter__(self):
        self.seen = getattr(self, "seen", 0) + 1
        return iter([READS[0]] if self.seen == 1 else ["operator.orders.submit"])


class InterfaceTests(unittest.TestCase):
    def refused(self, declaration, text, *more):
        with self.assertRaises(ValueError) as caught:
            panel_registry([*more, declaration])
        self.assertIs(type(caught.exception), ValueError)
        self.assertEqual(caught.exception.args, (text,))

    def test_public_names_and_signatures(self):
        self.assertEqual(panels.__all__, ["PANEL_KEYS", "PAPER_BOOK", "REFUSALS", "REQUIRED_PANEL_KEYS",
                                          "filter_venue_rows", "panel_registry"])
        self.assertEqual(str(inspect.signature(panel_registry)), "(panels)")
        self.assertEqual(str(inspect.signature(filter_venue_rows)), "(value, venue)")

    def test_accepted_keys_and_the_paper_book(self):
        self.assertEqual(REQUIRED_PANEL_KEYS, frozenset({"id", "title", "reads"}))
        self.assertEqual(PANEL_KEYS, frozenset({"id", "title", "reads", "venue", "venue_use",
                                                "withhold_venue_data"}))
        self.assertEqual(PAPER_BOOK, frozenset({"paper", "stock"}))
        for name in ("REQUIRED_PANEL_KEYS", "PANEL_KEYS", "PAPER_BOOK"):
            self.assertIs(type(getattr(panels, name)), frozenset)

    def test_refusal_texts_in_the_order_they_are_checked(self):
        self.assertEqual(REFUSALS, (NEEDS_KEYS, ONLY_READS, BAD_ID, DUPLICATE_OR_TITLE, BAD_READS,
                                    BAD_VENUE, BAD_VENUE_USE, BAD_WITHHOLD))
        self.assertIs(type(REFUSALS), tuple)

    def test_each_refusal_text(self):
        cases = [
            (["id", "title", "reads"], NEEDS_KEYS), ("panel", NEEDS_KEYS), (None, NEEDS_KEYS),
            ({"id": "p", "title": "T"}, NEEDS_KEYS), ({"title": "T", "reads": [READS[0]]}, NEEDS_KEYS),
            ({**GOOD, "stop": True}, ONLY_READS), ({**GOOD, "theme": {}}, ONLY_READS),
            ({**GOOD, "id": ""}, BAD_ID), ({**GOOD, "id": "x" * 97}, BAD_ID), ({**GOOD, "id": "a b"}, BAD_ID),
            ({**GOOD, "id": "a/b"}, BAD_ID), ({**GOOD, "id": 7}, BAD_ID), ({**GOOD, "id": _Str("p")}, BAD_ID),
            ({**GOOD, "id": "p\n"}, BAD_ID),
            ({**GOOD, "title": " "}, DUPLICATE_OR_TITLE), ({**GOOD, "title": ""}, DUPLICATE_OR_TITLE),
            ({**GOOD, "title": None}, DUPLICATE_OR_TITLE), ({**GOOD, "title": _Str("T")}, DUPLICATE_OR_TITLE),
            ({**GOOD, "reads": []}, BAD_READS), ({**GOOD, "reads": ()}, BAD_READS),
            ({**GOOD, "reads": READS[0]}, BAD_READS), ({**GOOD, "reads": {READS[0]}}, BAD_READS),
            ({**GOOD, "reads": (r for r in [READS[0]])}, BAD_READS), ({**GOOD, "reads": [ACTIONS[0]]}, BAD_READS),
            ({**GOOD, "reads": ["stop"]}, BAD_READS), ({**GOOD, "reads": [READS[0], "operator.orders.submit"]}, BAD_READS),
            ({**GOOD, "reads": [["bad"]]}, BAD_READS), ({**GOOD, "reads": [_Str("operator.orders.submit")]}, BAD_READS),
            ({**GOOD, "reads": _FlipList([READS[0]])}, BAD_READS),
            ({**GOOD, "venue": "Kalshi"}, BAD_VENUE), ({**GOOD, "venue": ""}, BAD_VENUE),
            ({**GOOD, "venue": "k" * 49}, BAD_VENUE), ({**GOOD, "venue": "kalshi:read"}, BAD_VENUE),
            ({**GOOD, "venue": 1}, BAD_VENUE), ({**GOOD, "venue": _Str("paper")}, BAD_VENUE),
            ({**GOOD, "venue_use": "kalshi"}, BAD_VENUE_USE), ({**GOOD, "venue_use": ["kalshi:read"]}, BAD_VENUE_USE),
            ({**GOOD, "venue_use": "Kalshi:<x>"}, BAD_VENUE_USE), ({**GOOD, "venue_use": _Str("kalshi:read")}, BAD_VENUE_USE),
            ({**GOOD, "withhold_venue_data": "no"}, BAD_WITHHOLD), ({**GOOD, "withhold_venue_data": 0}, BAD_WITHHOLD),
            ({**GOOD, "withhold_venue_data": None}, BAD_WITHHOLD),
        ]
        for declaration, text in cases:
            with self.subTest(declaration=declaration):
                self.refused(declaration, text)
        with self.subTest("duplicate id"):
            self.refused(dict(GOOD), DUPLICATE_OR_TITLE, dict(GOOD))

    def test_several_defects_are_refused_with_the_first_in_order(self):
        cases = [
            ({"id": "p", "stop": True}, NEEDS_KEYS),
            ({**GOOD, "stop": True, "id": ""}, ONLY_READS),
            ({**GOOD, "id": "", "title": ""}, BAD_ID),
            ({**GOOD, "title": "", "reads": []}, DUPLICATE_OR_TITLE),
            ({**GOOD, "reads": [], "venue": "Bad"}, BAD_READS),
            ({**GOOD, "venue": "Bad", "venue_use": "bad"}, BAD_VENUE),
            ({**GOOD, "venue_use": "bad", "withhold_venue_data": "no"}, BAD_VENUE_USE),
        ]
        for declaration, text in cases:
            with self.subTest(declaration=declaration):
                self.refused(declaration, text)

    def test_the_answer_is_read_only_plain_data_in_declaration_order(self):
        registry = panel_registry([
            {"id": "b.second", "title": "Second", "reads": (READS[1], READS[0], READS[1]),
             "venue": "paper", "venue_use": "paper:own_trading", "withhold_venue_data": False},
            MappingProxyType({"id": "a.first", "title": "First", "reads": [READS[2]], "venue": None}),
            OrderedDict(id="x" * 96, title="Third", reads=[READS[3]], venue_use=None),
        ])
        self.assertIs(type(registry), MappingProxyType)
        self.assertEqual(list(registry), ["b.second", "a.first", "x" * 96])
        for entry in registry.values():
            self.assertIs(type(entry), MappingProxyType)
            self.assertEqual(set(entry), {"title", "reads", "venue", "venue_use"})
            self.assertIs(type(entry["reads"]), tuple)
        self.assertEqual(dict(registry["b.second"]), {"title": "Second", "reads": (READS[1], READS[0]),
                                                      "venue": "paper", "venue_use": "paper:own_trading"})
        self.assertEqual(dict(registry["a.first"]), {"title": "First", "reads": (READS[2],),
                                                     "venue": None, "venue_use": None})
        self.assertEqual(registry["x" * 96]["venue"], None)
        with self.assertRaises(TypeError):
            registry["c"] = {}
        with self.assertRaises(TypeError):
            registry["a.first"]["reads"] = (READS[0],)
        self.assertEqual(panel_registry([]), {})
        self.assertIs(type(panel_registry(())), MappingProxyType)

    def test_what_is_checked_is_what_is_kept(self):
        reads = [READS[0]]
        declaration = {"id": "p", "title": "Panel", "reads": reads}
        registry = panel_registry([declaration])
        reads.append(ACTIONS[0])
        declaration["title"] = "Changed"
        self.assertEqual(registry["p"]["reads"], (READS[0],))
        self.assertEqual(registry["p"]["title"], "Panel")
        self.assertEqual(declaration, {"id": "p", "title": "Changed", "reads": [READS[0], ACTIONS[0]]})
        for entry in panel_registry([{"id": "q", "title": "Q", "reads": list(READS)}]).values():
            self.assertTrue(all(type(r) is str for r in entry["reads"]))
            self.assertIs(type(entry["title"]), str)

    def test_reads_are_copied_before_anything_else_is_read_from_the_declaration(self):
        # First review of #384 (both readers): a declaration that changes its own reads list while
        # it is read further must not get an unchecked read stored. The mapping is permitted; only
        # its get("venue") has a side effect, after the reads were checked.
        reads = [READS[0]]

        class Changing(Mapping):
            def __init__(self, data):
                self._data = data

            def __getitem__(self, key):
                return self._data[key]

            def __iter__(self):
                return iter(self._data)

            def __len__(self):
                return len(self._data)

            def get(self, key, default=None):
                if key == "venue":
                    reads[:] = ["operator.orders.submit"]
                return self._data.get(key, default)

        registry = panel_registry([Changing({"id": "p", "title": "Panel", "reads": reads})])
        self.assertEqual(reads, ["operator.orders.submit"])  # the declaration did change after the check
        self.assertEqual(registry["p"]["reads"], (READS[0],))

    def test_reads_snapshot_is_taken_before_validation(self):
        # Second review of #384 (GPT-6 Astra, R1; its suggested test, made stricter): the copy comes
        # before the check, not only before the next read of the declaration. The check itself changes
        # the source list, once by adding a read and once by replacing the read it has just checked;
        # what is kept is what was checked.
        from unittest.mock import patch

        for change in ("append", "replace"):
            source = [READS[0]]

            class Membership(tuple):
                def __contains__(self, item):
                    if source == [READS[0]]:
                        if change == "append":
                            source.append(READS[1])
                        else:
                            source[0] = READS[1]
                    return super().__contains__(item)

            with self.subTest(change=change), patch.object(panels, "READS", Membership(READS)):
                answer = panel_registry([{"id": "p", "title": "Panel", "reads": source}])
                self.assertNotEqual(source, [READS[0]])  # the check did change the declaration
                self.assertEqual(answer["p"]["reads"], (READS[0],))

    def test_panels_that_cannot_be_iterated_get_the_first_refusal(self):
        # DeepSeek break-it read of #384, F6: the ValueError promise holds for the container too.
        for panels_value in (None, 7, 1.5, object()):
            with self.subTest(panels=panels_value):
                with self.assertRaises(ValueError) as caught:
                    panel_registry(panels_value)
                self.assertIs(type(caught.exception), ValueError)
                self.assertEqual(caught.exception.args, (NEEDS_KEYS,))

    def test_panels_that_raise_while_iterated_get_the_first_refusal(self):
        # DeepSeek round 4, F2: the promise holds for a collection that raises while it is read.
        class RaisingIter:
            def __iter__(self):
                raise RuntimeError("BOOM")

        class RaisingNext:
            def __iter__(self):
                return self

            def __next__(self):
                raise KeyError("BOOM")

        def raising_generator():
            yield dict(GOOD)
            raise RuntimeError("BOOM-AFTER-ONE")

        for panels_value in (RaisingIter(), RaisingNext(), raising_generator()):
            with self.subTest(panels=type(panels_value).__name__):
                with self.assertRaises(ValueError) as caught:
                    panel_registry(panels_value)
                self.assertIs(type(caught.exception), ValueError)
                self.assertEqual(caught.exception.args, (NEEDS_KEYS,))

    def test_a_collection_is_refused_at_its_first_bad_declaration_without_reading_on(self):
        # DeepSeek round 5, F3: each declaration is checked as it is read, so a long or endless
        # collection whose first declaration is bad is refused after one read.
        pulled = []

        def declarations():
            yield {**GOOD, "reads": ["operator.orders.submit"]}
            for n in range(1000):
                pulled.append(n)
                yield {**GOOD, "id": "p%d" % n}

        self.refused_from(declarations(), BAD_READS)
        self.assertEqual(pulled, [])

    def test_what_an_unreadable_collection_raised_is_kept_as_the_cause(self):
        # DeepSeek round 5, F5: the text stays the first refusal; the cause says why reading stopped.
        failure = KeyError("WHY")

        class RaisingNext:
            def __iter__(self):
                return self

            def __next__(self):
                raise failure

        with self.assertRaises(ValueError) as caught:
            panel_registry(RaisingNext())
        self.assertEqual(caught.exception.args, (NEEDS_KEYS,))
        self.assertIs(caught.exception.__cause__, failure)
        with self.assertRaises(ValueError) as caught:
            panel_registry(None)
        self.assertIs(type(caught.exception.__cause__), TypeError)

    def refused_from(self, panels_value, text):
        with self.assertRaises(ValueError) as caught:
            panel_registry(panels_value)
        self.assertIs(type(caught.exception), ValueError)
        self.assertEqual(caught.exception.args, (text,))

    def test_the_depth_bound_is_sixty_four_containers_below_the_response_on_every_branch(self):
        # DeepSeek round 4, F5: errors and provenance count from the response as the rest does.
        def chain(levels, leaf):
            value = leaf
            for _ in range(levels):
                value = {"next": value}
            return value

        for name, make in (("errors", lambda n: {"errors": chain(n, "LEAF")}),
                           ("provenance", lambda n: {"provenance": chain(n, "LEAF")}),
                           ("labelled", lambda n: {"venue": "paper", "next": chain(n, "LEAF")})):
            with self.subTest(branch=name):
                self.assertEqual(filter_venue_rows(make(64), "paper"), (make(64), False))
                kept, withheld = filter_venue_rows(make(65), "paper")
                self.assertTrue(withheld)
                self.assertNotIn("LEAF", repr(kept))

    def test_deep_responses_and_keys_that_are_not_text_are_withheld_not_a_crash(self):
        # DeepSeek break-it read of #384, F1 and F7, at the filter itself.
        leaf = "DEEP-LEAF"
        deep = leaf
        for _ in range(5000):
            deep = {"next": deep}
        for response in ({"result": {"venue": "paper", "rows": deep}}, {"errors": [deep]}, {"provenance": deep},
                         {"venue": "paper", "rows": deep}):
            with self.subTest(top=sorted(response)):
                kept, withheld = filter_venue_rows(response, "paper")
                self.assertTrue(withheld)
                self.assertNotIn(leaf, repr(kept))
        shallow = {"result": {"venue": "paper", "rows": {"a": {"b": {"c": "SHALLOW-LEAF"}}}}}
        self.assertEqual(filter_venue_rows(shallow, "paper"), (shallow, False))
        kept, withheld = filter_venue_rows({"result": {"venue": "paper", 1: "INT-KEY", "symbol": "OK"},
                                            "errors": [{2: "INT-KEY", "code": "E"}]}, "paper")
        self.assertEqual((kept, withheld), ({"result": {"venue": "paper", "symbol": "OK"}, "errors": [{"code": "E"}]}, True))

    def test_a_tuple_subclass_of_reads_is_refused(self):
        class Reads(tuple):
            pass

        self.refused({**GOOD, "reads": Reads([READS[0]])}, BAD_READS)

    def test_length_bounds_of_venue_and_of_each_venue_use_segment(self):
        longest = "a" * 48 + ":" + "b" * 48
        registry = panel_registry([{**GOOD, "venue": "v" * 48, "venue_use": longest}])
        self.assertEqual((registry[GOOD["id"]]["venue"], registry[GOOD["id"]]["venue_use"]), ("v" * 48, longest))
        for venue_use in ("a" * 49 + ":b", "a:" + "b" * 49, ":b", "a:", "a:b:c"):
            with self.subTest(venue_use=venue_use):
                self.refused({**GOOD, "venue_use": venue_use}, BAD_VENUE_USE)
        self.refused({**GOOD, "venue": "v" * 49}, BAD_VENUE)

    def test_filter_venue_rows_contract(self):
        response = {"errors": [{"code": "E"}], "result": {"rows": [{"venue": "kalshi", "symbol": "K"},
                                                                   {"venue": "paper", "symbol": "P"}]}}
        before = repr(response)
        kept, withheld = filter_venue_rows(response, "kalshi")
        self.assertEqual((kept, withheld), ({"result": {"rows": [{"venue": "kalshi", "symbol": "K"}]},
                                              "errors": [{"code": "E"}]}, True))
        self.assertIs(type(kept), dict)
        self.assertIs(type(withheld), bool)
        self.assertEqual(repr(response), before)
        self.assertEqual(filter_venue_rows({"result": {"rows": [{"venue": "stock", "symbol": "S"}]}}, "paper"),
                         ({"result": {"rows": [{"venue": "stock", "symbol": "S"}]}}, False))
        for not_a_response in ([{"venue": "kalshi"}], [], "text", None, ({"venue": "kalshi"},)):
            with self.subTest(value=not_a_response):
                self.assertEqual(filter_venue_rows(not_a_response, "kalshi"), ({}, True))


if __name__ == "__main__":
    unittest.main()
