"""What the breaker store records as a run's model version, and how that sits beside two other rules.

The store records a model version as given when it is 1 to 128 printable ASCII characters with no
space (0x21 to 0x7E). It is a field of the run's binding, not a key, so it does not have to fit the
id grammar the other four fields keep.

``subscription_routes.own_loop`` applies the same rule to the model of an own-loop route. Neither
package imports the other, so nothing but ``ModelRuleDriftTests`` holds the two together: it fails
when either one moves. An authority profile names its model more narrowly, in the id grammar;
``ProfileModelTests`` says how the two stand.

Nothing here signs or verifies, so these run where ``cryptography`` is missing too.
"""

from __future__ import annotations

import os
import unittest

from recursion_core.plugin_security import ConnectionBreaker, Refused, create_breaker_store
from recursion_core.plugin_security.tests.support import temp_dir
from recursion_core.subscription_routes import own_loop

PRINTABLE = "".join(chr(code) for code in range(0x21, 0x7F))  # all 94 of them: no space

RECORDED = (
    "m", "opus", "opus[1m]", "sonnet[1m]", "a+b", "claude-sonnet-4.5(beta)", "us.anthropic.claude-x-v1:0",
    "anthropic/claude-x@2026-11", "!", "~", '"quoted"', "back\\slash", PRINTABLE, "x" * 128,
)
NOT_RECORDED = (
    "", "x" * 129, PRINTABLE + PRINTABLE, " ", "claude sonnet 4.5", " leading", "trailing ", "opus[1m]\n",
    "\nopus", "a\tb", "a\x00b", "a\x1fb", "a\x7fb", "a\x80b", "mod\u00e8le", "\u3000", "opus\u200b", "\U0001f600",
)
NOT_TEXT = (None, 7, 1.5, b"opus", ["opus"], ("opus",))
CHARACTERS = (*range(0x00, 0x100), 0x2028, 0xFEFF, 0x1F600)


class Name(str):
    """A ``str`` subclass: the store takes exact ``str`` only, as it does for its ids."""


class ModelRuleTests(unittest.TestCase):
    def open(self) -> ConnectionBreaker:
        path = os.path.join(temp_dir(self), "breaker.sqlite3")
        create_breaker_store(path)
        breaker = ConnectionBreaker(path)
        self.addCleanup(breaker.close)
        return breaker

    def test_a_printable_ascii_name_of_1_to_128_characters_with_no_space_is_recorded_as_given(self) -> None:
        breaker = self.open()
        for number, model in enumerate(RECORDED):
            with self.subTest(model=model[:24]):
                self.assertTrue(ConnectionBreaker.model_recordable(model))
                self.assertIsNone(breaker.bind_run(f"run-{number}", "conn-a", "plug-a", "prov", model))
                bound = breaker.check(f"run-{number}", "conn-a", "plug-a")
                self.assertEqual(bound, {"provider": "prov", "model_version": model})
                self.assertIs(type(bound["model_version"]), str)

    def test_any_other_name_is_refused_and_binds_nothing(self) -> None:
        breaker = self.open()
        for number, model in enumerate(NOT_RECORDED + NOT_TEXT + (Name("opus"),)):
            with self.subTest(model=ascii(model)[:24]):
                self.assertFalse(ConnectionBreaker.model_recordable(model))
                with self.assertRaises(Refused) as caught:
                    breaker.bind_run(f"run-{number}", "conn-a", "plug-a", "prov", model)
                self.assertEqual(caught.exception.code, "invalid_model_version")
                with self.assertRaises(Refused) as caught:
                    breaker.check(f"run-{number}", "conn-a", "plug-a")
                self.assertEqual(caught.exception.code, "run_unknown")

    def test_every_single_character_is_on_the_side_the_rule_puts_it(self) -> None:
        for code in CHARACTERS:
            with self.subTest(code=hex(code)):
                self.assertEqual(ConnectionBreaker.model_recordable("a" + chr(code) + "b"), 0x21 <= code <= 0x7E)

    def test_the_other_fields_of_a_binding_keep_the_id_grammar(self) -> None:
        breaker = self.open()
        good = ["run-1", "conn-a", "plug-a", "prov", "opus[1m]"]
        codes = ("invalid_run_id", "invalid_connection", "invalid_plugin_id", "invalid_provider")
        for place, code in enumerate(codes):
            for value in ("opus[1m]", "a b", "a+b"):
                with self.subTest(field=code, value=value):
                    args = list(good)
                    args[place] = value
                    with self.assertRaises(Refused) as caught:
                        breaker.bind_run(*args)
                    self.assertEqual(caught.exception.code, code)
        self.assertIsNone(breaker.bind_run(*good))

    def test_a_run_cannot_move_to_another_spelling_of_its_model(self) -> None:
        breaker = self.open()
        breaker.bind_run("run-1", "conn-a", "plug-a", "prov", "opus[1m]")
        breaker.bind_run("run-1", "conn-a", "plug-a", "prov", "opus[1m]")  # the same binding again is fine
        for other in ("opus", "opus[1M]", "Opus[1m]"):
            with self.subTest(other=other):
                with self.assertRaises(Refused) as caught:
                    breaker.bind_run("run-1", "conn-a", "plug-a", "prov", other)
                self.assertEqual(caught.exception.code, "run_binding_mismatch")


class ProfileModelTests(unittest.TestCase):
    """An authority profile names its model in the id grammar, which is narrower than what the store
    records. Before the store's rule was widened the two were one grammar, so that every model a run
    could be bound to could be named in a profile (``test_gate``'s
    ``test_a_bindable_model_name_can_be_named_in_a_profile``). That no longer holds for a name outside
    the id grammar. This says so where it can fail: whoever widens the profile's rule, or narrows the
    store's, meets this test."""

    def test_a_profile_can_name_a_model_only_if_a_run_can_be_bound_to_it(self) -> None:
        from recursion_core.plugin_security import authority
        for model in RECORDED + NOT_RECORDED:
            with self.subTest(model=ascii(model)[:24]):
                if authority._MODEL_VALUE.fullmatch(model):
                    self.assertTrue(ConnectionBreaker.model_recordable(model))
        for code in CHARACTERS:
            with self.subTest(code=hex(code)):
                model = "a" + chr(code) + "b"
                if authority._MODEL_VALUE.fullmatch(model):
                    self.assertTrue(ConnectionBreaker.model_recordable(model))

    def test_a_run_can_be_bound_to_a_model_that_no_profile_can_name(self) -> None:
        from recursion_core.plugin_security import authority
        for model in ("opus[1m]", "a+b", "claude-sonnet-4.5(beta)"):
            with self.subTest(model=model):
                self.assertTrue(ConnectionBreaker.model_recordable(model))
                self.assertIsNone(authority._MODEL_VALUE.fullmatch(model))
        for model in ("opus", "vendor/model", "model@rev", "us.anthropic.claude-x-v1:0"):
            with self.subTest(model=model):
                self.assertTrue(ConnectionBreaker.model_recordable(model))
                self.assertIsNotNone(authority._MODEL_VALUE.fullmatch(model))


def own_loop_takes(model: str) -> bool:
    try:
        return own_loop._model("copilot", model) == model
    except own_loop.OwnLoopRouteRefused:
        return False


class ModelRuleDriftTests(unittest.TestCase):
    """The store and an own-loop route take the same model names and refuse the same ones. The rule is
    written twice, once in each package, and each test here compares the two: a change to either
    one's characters or to either one's length fails."""

    def test_the_two_rules_take_and_refuse_the_same_sample_set(self) -> None:
        seen = set()
        for model in RECORDED + NOT_RECORDED:
            with self.subTest(model=ascii(model)[:24]):
                self.assertEqual(own_loop_takes(model), ConnectionBreaker.model_recordable(model))
                self.assertEqual(ConnectionBreaker.model_recordable(model), model in RECORDED)
                seen.add(own_loop_takes(model))
        self.assertEqual(seen, {True, False}, "the samples held names of one kind only")

    def test_the_two_rules_agree_on_every_single_character(self) -> None:
        for code in CHARACTERS:
            with self.subTest(code=hex(code)):
                model = "a" + chr(code) + "b"
                self.assertEqual(own_loop_takes(model), ConnectionBreaker.model_recordable(model))

    def test_the_two_rules_have_the_same_longest_name(self) -> None:
        for length, taken in ((1, True), (127, True), (128, True), (129, False), (4096, False)):
            with self.subTest(length=length):
                self.assertEqual(ConnectionBreaker.model_recordable("x" * length), taken)
                self.assertEqual(own_loop_takes("x" * length), taken)
        self.assertFalse(ConnectionBreaker.model_recordable(""))
        self.assertFalse(own_loop_takes(""))


if __name__ == "__main__":
    unittest.main()
