"""The served tool table is control/mcp_tools.py's four tools, unchanged, and their arguments are checked
against the schemas the server advertises."""

from __future__ import annotations

import copy
import unittest
from unittest import mock

from recursion_core.control import mcp_tools
from recursion_core.control.mcp import tools
from recursion_core.control.mcp.tools import SchemaUnsupported, check_arguments, tool_table


class TableTest(unittest.TestCase):
    def test_the_table_is_control_mcp_tools_unchanged_and_in_order(self) -> None:
        table = tool_table()
        self.assertEqual([t["name"] for t in mcp_tools.TOOLS], [t.name for t in table])
        self.assertEqual({t["name"]: t["method"] for t in mcp_tools.TOOLS}, {t.name: t.method for t in table})
        self.assertEqual(mcp_tools.descriptors(), [t.descriptor for t in table])
        self.assertEqual({"run.list", "run.status", "run.start", "run.stop"}, {t.method for t in table})

    def test_only_the_stop_tool_is_a_stop(self) -> None:
        self.assertEqual(["recursion_run_stop"], [t.name for t in tool_table() if t.is_stop])
        self.assertEqual(mcp_tools.STOP_TOOL, next(t.name for t in tool_table() if t.is_stop))

    def test_marks_are_the_decided_ones(self) -> None:
        marks = {t.name: t.descriptor["annotations"] for t in tool_table()}
        self.assertIs(marks["recursion_run_stop"]["destructiveHint"], True)
        self.assertIs(marks["recursion_run_stop"]["openWorldHint"], False)
        self.assertIs(marks["recursion_run_start"]["openWorldHint"], True)
        for name in ("recursion_run_list", "recursion_run_status"):
            self.assertIs(marks[name]["readOnlyHint"], True)

    def test_the_table_is_a_copy(self) -> None:
        table = tool_table()
        table[0].descriptor["inputSchema"]["properties"]["limit"]["maximum"] = 1
        self.assertEqual(mcp_tools.LIST_LIMIT_MAX, tool_table()[0].input_schema["properties"]["limit"]["maximum"])

    def with_schema(self, schema: dict):
        patched = copy.deepcopy(mcp_tools.TOOLS)
        patched[1]["inputSchema"] = schema
        return mock.patch.object(mcp_tools, "TOOLS", patched)

    def test_a_schema_keyword_the_checker_does_not_know_is_refused(self) -> None:
        refused = [
            {"type": "object", "properties": {"run_id": {"type": "string", "format": "uuid"}}},
            {"type": "object", "properties": {}, "additionalProperties": True},
            {"type": "object", "properties": {"x": {"const": "a"}}},
            {"type": "object", "properties": {"x": {"type": "date"}}},
            {"type": "object", "properties": {"x": {"type": "string", "pattern": "^\\d+$"}}},
            {"type": "object", "properties": {"x": {"type": "string", "pattern": "^(?=a)a$"}}},
            {"type": "object", "properties": {"x": {"type": "string", "enum": []}}},
            {"type": "object", "properties": {"x": {"anyOf": []}}},
            {"type": "string"},
            {"type": "object", "oneOf": []},
        ]
        for schema in refused:
            with self.subTest(schema=schema), self.with_schema(schema):
                with self.assertRaises(SchemaUnsupported):
                    tool_table()

    def test_a_known_keyword_with_a_value_the_checker_cannot_read_is_refused(self) -> None:
        refused = [
            {"type": "object", "properties": {"run_id": {"type": "string", "minLength": "x"}}},
            {"type": "object", "properties": {"run_id": {"type": "string", "maxLength": -1}}},
            {"type": "object", "properties": {"run_id": {"type": "string", "minLength": True}}},
            {"type": "object", "properties": {"n": {"type": "integer", "maximum": [1]}}},
            {"type": "object", "properties": {"n": {"type": "integer", "minimum": False}}},
            {"type": "object", "properties": [{"run_id": {"type": "string"}}]},
            {"type": "object", "properties": {}, "required": "run_id"},
            {"type": "object", "properties": {}, "required": [1]},
        ]
        for schema in refused:
            with self.subTest(schema=schema), self.with_schema(schema):
                with self.assertRaises(SchemaUnsupported):
                    tool_table()


class CheckArgumentsTest(unittest.TestCase):
    SCHEMA = {"type": "object",
              "properties": {"name": {"type": "string", "minLength": 2, "maxLength": 4},
                             "n": {"type": "integer", "minimum": -1, "maximum": 3},
                             "inner": {"type": "object", "properties": {"k": {"type": "string"}},
                                       "required": ["k"], "additionalProperties": False}},
              "required": ["name"], "additionalProperties": False}

    def test_conforming(self) -> None:
        for value in ({"name": "ab"}, {"name": "abcd", "n": -1}, {"name": "abc", "n": 3, "inner": {"k": ""}}):
            self.assertIsNone(check_arguments(self.SCHEMA, value), value)

    def test_each_rule(self) -> None:
        cases = [
            ([], "arguments must be an object"),
            ({}, "arguments.name is required"),
            ({"name": "ab", "z": 1}, "arguments.z is not a known argument"),
            ({"name": "a"}, "arguments.name must be at least 2 character(s) long"),
            ({"name": "abcde"}, "arguments.name must be at most 4 character(s) long"),
            ({"name": 12}, "arguments.name must be a string"),
            ({"name": "ab", "n": -2}, "arguments.n must be at least -1"),
            ({"name": "ab", "n": 4}, "arguments.n must be at most 3"),
            ({"name": "ab", "n": 2.0}, "arguments.n must be an integer"),
            ({"name": "ab", "n": False}, "arguments.n must be an integer"),
            ({"name": "ab", "inner": {}}, "arguments.inner.k is required"),
            ({"name": "ab", "inner": {"k": "v", "j": 1}}, "arguments.inner.j is not a known argument"),
            ({"name": "ab", "inner": "k"}, "arguments.inner must be an object"),
        ]
        for value, problem in cases:
            with self.subTest(value=value):
                self.assertEqual(problem, check_arguments(self.SCHEMA, value))

    def test_length_counts_characters_not_bytes(self) -> None:
        self.assertIsNone(check_arguments(self.SCHEMA, {"name": "\u00e9\u00e9\u00e9\u00e9"}))

    def test_the_four_descriptors_accept_what_the_control_api_takes(self) -> None:
        by_name = {t.name: t.input_schema for t in tools.tool_table()}
        accepted = {
            "recursion_run_list": [{}, {"limit": 1}, {"limit": mcp_tools.LIST_LIMIT_MAX, "after": "run-1"}],
            "recursion_run_status": [{"run_id": "run-1"}],
            "recursion_run_stop": [{"run_id": "run-1"}],
            "recursion_run_start": [{"tenant": "t", "engine": "codex", "prompt": "p"},
                                    {"tenant": "t", "engine": "codex", "prompt": "", "client_key": "k"}],
        }
        for name, values in accepted.items():
            for value in values:
                self.assertIsNone(check_arguments(by_name[name], value), (name, value))


class PublishedKeywordTest(unittest.TestCase):
    """The keywords the published parameter schemas use beyond the legacy four's."""

    def test_patterns_are_read_as_ecma_262_reads_them(self) -> None:
        end = {"type": "string", "pattern": "^[a-z]+$"}
        self.assertIsNone(check_arguments(end, "abc"))
        self.assertEqual("arguments does not match the pattern ^[a-z]+$", check_arguments(end, "abc\n"))
        dot = {"type": "string", "pattern": "^a.b$"}
        self.assertIsNone(check_arguments(dot, "axb"))
        for terminator in ("\n", "\r", "\u2028", "\u2029"):
            self.assertIsNotNone(check_arguments(dot, f"a{terminator}b"), repr(terminator))
        unanchored = {"type": "string", "pattern": "b"}
        self.assertIsNone(check_arguments(unanchored, "abc"))
        negated = {"type": "string", "pattern": "^[^$.]+$"}  # inside a class, $ and . are themselves
        self.assertIsNone(check_arguments(negated, "abc"))
        self.assertIsNotNone(check_arguments(negated, "a$c"))
        self.assertIsNotNone(check_arguments(negated, "a.c"))

    def test_not_enum_any_of_items_and_type_lists(self) -> None:
        no_newline = {"type": "string", "not": {"pattern": "[\\r\\n]"}}
        self.assertIsNone(check_arguments(no_newline, "one line"))
        self.assertEqual("arguments is in a form it must not take", check_arguments(no_newline, "two\nlines"))
        enum = {"enum": ["raw", "transcript"]}
        self.assertIsNone(check_arguments(enum, "raw"))
        self.assertEqual('arguments must be one of ["raw","transcript"]', check_arguments(enum, "RAW"))
        self.assertIsNotNone(check_arguments({"enum": [1]}, True))
        either = {"type": "object", "anyOf": [{"required": ["text"]}, {"required": ["delivery"]}]}
        self.assertIsNone(check_arguments(either, {"text": "x"}))
        self.assertIsNone(check_arguments(either, {"delivery": "steer"}))
        self.assertEqual("arguments matches none of the allowed forms", check_arguments(either, {}))
        listed = {"type": "object", "properties": {"x": {"type": "array", "items": {"type": "string", "minLength": 1}}}}
        self.assertIsNone(check_arguments(listed, {"x": ["a", "b"]}))
        self.assertEqual("arguments.x[1] must be at least 1 character(s) long", check_arguments(listed, {"x": ["a", ""]}))
        nullable = {"type": ["string", "null"]}
        self.assertIsNone(check_arguments(nullable, None))
        self.assertEqual("arguments must be one of the types string, null", check_arguments(nullable, 1))

    def test_numbers_and_booleans(self) -> None:
        number = {"type": "number", "minimum": 0}
        for good in (0, 1.5, 2):
            self.assertIsNone(check_arguments(number, good), good)
        for bad in (True, "1", None, -0.5):
            self.assertIsNotNone(check_arguments(number, bad), bad)
        self.assertIsNone(check_arguments({"type": "boolean"}, False))
        self.assertEqual("arguments must be a boolean", check_arguments({"type": "boolean"}, 0))

    def test_escapes_python_reads_differently_are_refused(self) -> None:
        for pattern in ("\\d", "\\w", "\\s", "\\b", "\\u0041", "(?i)a", "(?<n>a)", "[a", "a\\"):
            with self.subTest(pattern=pattern), self.assertRaises(SchemaUnsupported):
                tools.ecma_pattern(pattern)
        self.assertIsNotNone(tools.ecma_pattern("^[A-Za-z0-9._:-]{1,128}$").search("a.b:c"))

    def test_forms_python_reads_otherwise_are_refused(self) -> None:
        # #416, round 1 (Sol R4): an empty class, a quantifier on nothing or on a quantifier (Python's possessive
        # forms), and a brace that is not a whole quantifier are refused, not read Python's way.
        for pattern in ("[]]", "[]", "[^]", "[^]]", "a++", "a*+", "a?+", "a{2}+", "a+?+", "a**", "a???", "{1}", "^*",
                        "(*)", "a|+", "x{,3}", "x{1,2", "a{"):
            with self.subTest(pattern=pattern), self.assertRaises(SchemaUnsupported):
                tools.ecma_pattern(pattern)
        for pattern in ("{1}", "^*", "(*)", "a|+", "$?"):  # refused here, whatever Python's own parser would say
            with self.subTest(pattern=pattern), self.assertRaisesRegex(SchemaUnsupported, "nothing to repeat, or on a quantifier"):
                tools.ecma_pattern(pattern)
        for pattern, value in (("^a+?$", "aaa"), ("^a{2,3}?$", "aa"), ("^a{2,}$", "aaaa"), ("^x]$", "x]"),
                               ("^[a-c]{1,2}$", "cb"), ("^(ab)*$", "abab"), ("^a??b$", "b"), ("^[{}]+$", "{}")):
            with self.subTest(pattern=pattern):
                self.assertIsNotNone(tools.ecma_pattern(pattern).search(value))
        self.assertIsNone(tools.ecma_pattern("^a{2,3}$").search("a"))

    def test_a_pattern_python_cannot_compile_is_refused_and_an_interrupt_goes_on(self) -> None:
        # #416, round 2 (Astra's and Sol's required change 2, DeepSeek R2 F2): SchemaUnsupported, through every door.
        deep = "(" * 1000 + "a" + ")" * 1000
        for pattern in ("a{4294967296}", "a{99999999999999999999}", "a{2,4294967296}", deep):
            with self.subTest(pattern=pattern[:20]):
                with self.assertRaises(SchemaUnsupported):
                    tools.ecma_pattern(pattern)
                with self.assertRaises(SchemaUnsupported):
                    tools.require_supported({"type": "string", "pattern": pattern}, "x")
                with self.assertRaises(SchemaUnsupported):
                    check_arguments({"type": "string", "pattern": pattern}, "a")
        for exc in (KeyboardInterrupt, SystemExit):
            with self.subTest(raised=exc.__name__), mock.patch.object(tools.re, "compile", side_effect=exc):
                with self.assertRaises(exc):
                    tools.ecma_pattern("a")

    def test_an_ordinary_compile_error_is_refused_with_a_bounded_detail(self) -> None:
        # #416, round 3 (Astra's and Sol's later coverage): re.error is turned too, and the pattern named in 80 characters.
        for pattern in ("a{3,2}", "(", "a)"):
            with self.subTest(pattern=pattern), self.assertRaises(SchemaUnsupported):
                tools.ecma_pattern(pattern)
        with self.assertRaises(SchemaUnsupported) as caught:
            tools.ecma_pattern("a{3,2}" + "b" * 10000)
        self.assertLess(len(str(caught.exception)), 300)

    def test_bounds_do_not_apply_to_a_boolean_and_a_number_is_finite(self) -> None:
        # #416, round 1 (DeepSeek F3; the survivors of Astra's M06 and M12 and Sol's M12 and M13).
        either = {"type": ["boolean", "number"], "minimum": 2}
        self.assertIsNone(check_arguments(either, True))
        self.assertEqual("arguments must be at least 2", check_arguments(either, 1))
        for bad in (float("nan"), float("inf"), float("-inf")):
            self.assertEqual("arguments must be a number", check_arguments({"type": "number"}, bad), bad)


if __name__ == "__main__":
    unittest.main()
