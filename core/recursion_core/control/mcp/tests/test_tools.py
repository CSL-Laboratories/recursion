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
            {"type": "object", "properties": {"run_id": {"type": "string", "pattern": "^run-"}}},
            {"type": "object", "properties": {}, "additionalProperties": True},
            {"type": "object", "properties": {"n": {"type": "number"}}},
            {"type": "object", "properties": {"x": {"type": "string", "enum": ["a"]}}},
            {"type": "string"},
            {"type": "object", "oneOf": []},
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


if __name__ == "__main__":
    unittest.main()
