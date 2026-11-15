"""The tool table projected from the published method table (SPINE's #368), joined with methods.list."""

from __future__ import annotations

import copy
import json
import unittest

from recursion_core.control.mcp import projection, tools
from recursion_core.control.mcp.projection import Published, ProjectionRefused, choose_tools, project
from recursion_core.control.mcp.tests.support import ScriptedAPI
from recursion_core.control.mcp.tools import SchemaUnsupported, check_arguments

PUBLISHED = projection.default_published()
NOT_EXPOSED = {"approval.open", "approval.answer", "events.follow", "operator.read", "workspace.publish",
               "plugin.call", "surface.prefs.get", "surface.prefs.set", "methods.list"}


def listing(published: Published, implemented=None, version=None) -> dict:
    names = {row["name"] for row in published.rows} if implemented is None else set(implemented)
    return {"code": "ok", "table_version": published.table_version if version is None else version,
            "methods": [{"name": row["name"], "implemented": row["name"] in names, "beta": row["beta"]}
                        for row in published.rows]}


def with_rows(rows_change) -> Published:
    documents = copy.deepcopy(PUBLISHED.documents)
    rows_change(documents[projection.TABLE_FILE]["methods"], documents)
    return Published(documents)


class PublishedTableTest(unittest.TestCase):
    def test_the_published_files_are_there(self) -> None:
        self.assertIsNotNone(PUBLISHED, projection.DEFAULT_DIRECTORY)
        self.assertEqual("1.0-draft.10", PUBLISHED.table_version)

    def test_every_exposed_implemented_row_is_a_tool_under_its_published_name_and_marks(self) -> None:
        projected = project(PUBLISHED, listing(PUBLISHED))
        exposed = [row for row in PUBLISHED.rows if row["mcp"]["exposed"]]
        self.assertEqual([row["mcp"]["tool"] for row in exposed], [t.name for t in projected])
        self.assertEqual([row["name"] for row in exposed], [t.method for t in projected])
        for tool, row in zip(projected, exposed, strict=True):
            with self.subTest(tool=tool.name):
                self.assertEqual(row["mcp"]["annotations"], tool.descriptor["annotations"])
                self.assertEqual(["annotations", "description", "inputSchema", "name"], sorted(tool.descriptor))
                text = json.dumps(tool.input_schema)
                self.assertNotIn("$ref", text)
                self.assertEqual("object", tool.input_schema["type"])
                tools.require_supported(tool.input_schema, tool.name)
                self.assertIn(row["name"], tool.descriptor["description"])

    def test_rows_that_are_not_exposed_are_never_tools_whatever_the_daemon_implements(self) -> None:
        self.assertEqual(NOT_EXPOSED, {row["name"] for row in PUBLISHED.rows if not row["mcp"]["exposed"]})
        projected = project(PUBLISHED, listing(PUBLISHED))
        self.assertTrue(NOT_EXPOSED.isdisjoint({t.method for t in projected}))
        self.assertNotIn("approval.answer", [t.method for t in projected])
        self.assertIn("approval.list", [t.method for t in projected])

    def test_only_what_the_daemon_implements(self) -> None:
        built_in = {"run.start", "run.status", "run.list", "run.stop", "route.list", "plugin.list", "methods.list"}
        projected = project(PUBLISHED, listing(PUBLISHED, built_in))
        self.assertEqual(["run.start", "run.status", "run.list", "run.stop", "route.list", "plugin.list"],
                         [t.method for t in projected])
        self.assertEqual((), project(PUBLISHED, listing(PUBLISHED, ())))

    def test_the_run_tools_marks_agree_with_control_mcp_tools(self) -> None:
        legacy = {t.name: t.descriptor["annotations"] for t in tools.tool_table()}
        projected = {t.name: t.descriptor["annotations"] for t in project(PUBLISHED, listing(PUBLISHED))}
        for name, marks in legacy.items():
            self.assertEqual({k: v for k, v in marks.items() if k != "title"}, projected[name], name)

    def test_stop_and_interrupt_tools_get_the_reserved_slots(self) -> None:
        stops = sorted(t.method for t in project(PUBLISHED, listing(PUBLISHED)) if t.is_stop)
        self.assertEqual(["run.stop", "session.interrupt", "session.stop"], stops)

    def test_projected_schemas_check_published_rules(self) -> None:
        by_method = {t.method: t.input_schema for t in project(PUBLISHED, listing(PUBLISHED))}
        stop = by_method["run.stop"]
        self.assertIsNone(check_arguments(stop, {"run_id": "run-1"}))
        self.assertIsNotNone(check_arguments(stop, {"run_id": "run 1"}))
        self.assertIsNotNone(check_arguments(stop, {"run_id": "run-1\n"}))
        session = by_method["session.interrupt"]
        self.assertEqual("arguments.command_id is required",
                         check_arguments(session, {"session_id": "ses-1", "expected_turn_id": "turn-1"}))


class JoinTest(unittest.TestCase):
    def test_another_table_version_is_refused(self) -> None:
        with self.assertRaises(ProjectionRefused) as caught:
            project(PUBLISHED, listing(PUBLISHED, version="1.0-draft.9"))
        self.assertEqual("table_version_mismatch", caught.exception.reason)

    def test_listings_that_are_not_the_published_shape_are_refused(self) -> None:
        good = listing(PUBLISHED)
        bad = [
            {"code": "ok", "methods": good["methods"]},
            {"code": "ok", "table_version": PUBLISHED.table_version},
            dict(good, methods=[{"name": "run.start"}]),
            dict(good, methods=[{"name": "run.start", "implemented": 1, "beta": False}]),
            dict(good, methods=[{"name": "run.start", "implemented": True, "beta": False}] * 2),
            "ok",
        ]
        for answer in bad:
            with self.subTest(answer=str(answer)[:60]), self.assertRaises(ProjectionRefused) as caught:
                project(PUBLISHED, answer)
            self.assertEqual("listing_invalid", caught.exception.reason)

    def test_a_name_the_table_does_not_have_is_not_a_tool(self) -> None:
        answer = listing(PUBLISHED, ())
        answer["methods"].append({"name": "widget.spin", "implemented": True, "beta": True})
        self.assertEqual((), project(PUBLISHED, answer))

    def test_a_row_added_to_the_table_appears_with_its_mark_and_no_change_here(self) -> None:
        def add(rows, documents):
            row = copy.deepcopy(next(r for r in rows if r["name"] == "run.status"))
            row.update(name="widget.spin", params="#/$defs/params.widget.spin", codes=["ok"])
            row["mcp"] = {"exposed": True, "tool": "recursion_widget_spin",
                          "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True,
                                          "openWorldHint": False}}
            rows.append(row)
            documents[projection.PARAMS_FILE]["$defs"]["params.widget.spin"] = {
                "type": "object", "additionalProperties": False, "required": ["run_id"],
                "properties": {"run_id": {"$ref": "common.v1.json#/$defs/run_id"}}}

        published = with_rows(add)
        spun = [t for t in project(published, listing(published)) if t.method == "widget.spin"]
        self.assertEqual(1, len(spun))
        self.assertEqual("recursion_widget_spin", spun[0].name)
        self.assertIs(True, spun[0].descriptor["annotations"]["destructiveHint"])
        self.assertIsNone(check_arguments(spun[0].input_schema, {"run_id": "run-1"}))

    def test_an_exposed_row_that_cannot_be_projected_is_refused(self) -> None:
        def no_params(rows, documents):
            del documents[projection.PARAMS_FILE]["$defs"]["params.run.status"]

        def duplicate_tool(rows, documents):
            rows[1]["mcp"]["tool"] = rows[0]["mcp"]["tool"]

        def unknown_keyword(rows, documents):
            documents[projection.PARAMS_FILE]["$defs"]["params.run.status"]["properties"]["x"] = {"format": "uuid"}

        for change in (no_params, duplicate_tool, unknown_keyword):
            with self.subTest(change=change.__name__), self.assertRaises(ProjectionRefused) as caught:
                published = with_rows(change)
                project(published, listing(published))
            self.assertEqual("published_invalid", caught.exception.reason)


class InlineTest(unittest.TestCase):
    def published(self, defs: dict, common: dict | None = None) -> Published:
        return Published({projection.TABLE_FILE: {"table_version": "1.0", "methods": []},
                          projection.PARAMS_FILE: {"$defs": defs},
                          "common.v1.json": {"$defs": common or {}}})

    def test_references_across_files_and_inside_one(self) -> None:
        published = self.published({"p": {"type": "object", "properties": {"a": {"$ref": "common.v1.json#/$defs/a"},
                                                                         "b": {"$ref": "#/$defs/b"}}},
                                    "b": {"type": "string"}},
                                   {"a": {"anyOf": [{"type": "null"}, {"$ref": "#/$defs/c"}]}, "c": {"type": "integer"}})
        self.assertEqual({"type": "object", "properties": {"a": {"anyOf": [{"type": "null"}, {"type": "integer"}]},
                                                           "b": {"type": "string"}}},
                         published.inline({"$ref": "methods.v1.json#/$defs/p"}))

    def test_cycles_siblings_and_missing_targets_are_refused(self) -> None:
        cases = [
            self.published({"p": {"$ref": "#/$defs/q"}, "q": {"$ref": "#/$defs/p"}}),
            self.published({"p": {"type": "object", "properties": {"a": {"$ref": "#/$defs/p"}}}}),
            self.published({"p": {"$ref": "#/$defs/q", "description": "x"}, "q": {"type": "string"}}),
            self.published({"p": {"$ref": "#/$defs/nothing"}}),
            self.published({"p": {"$ref": "other.v1.json#/$defs/x"}}),
            self.published({"p": {"$ref": "common.v1.json"}}),
        ]
        for published in cases:
            with self.subTest(defs=published.documents[projection.PARAMS_FILE]), self.assertRaises(SchemaUnsupported):
                published.inline({"$ref": "methods.v1.json#/$defs/p"})

    def test_data_keywords_are_not_read_as_schemas(self) -> None:
        published = self.published({"p": {"enum": [{"$ref": "#/$defs/nothing"}], "required": ["$ref"]}})
        self.assertEqual({"enum": [{"$ref": "#/$defs/nothing"}], "required": ["$ref"]},
                         published.inline({"$ref": "methods.v1.json#/$defs/p"}))

    def test_the_published_documents_are_copied(self) -> None:
        documents = {projection.TABLE_FILE: {"table_version": "1.0", "methods": []}, projection.PARAMS_FILE: {"$defs": {}}}
        published = Published(documents)
        documents[projection.TABLE_FILE]["table_version"] = "changed"
        self.assertEqual("1.0", published.table_version)
        for incomplete in ({projection.TABLE_FILE: {"table_version": "1.0", "methods": []}},
                           {projection.TABLE_FILE: {"methods": []}, projection.PARAMS_FILE: {}}):
            with self.assertRaises(ProjectionRefused):
                Published(incomplete)


class ChooseTest(unittest.TestCase):
    def choose(self, answer, published=PUBLISHED):
        api = ScriptedAPI([{"method": "methods.list", "params": {}, "answer": answer}])
        chosen = choose_tools(api, published)
        api.assert_done(self)
        return chosen

    def test_a_daemon_from_before_the_method_table_gets_the_legacy_four(self) -> None:
        tools_, entry = self.choose({"code": "method_unknown", "detail": "methods.list"})
        self.assertEqual([t.name for t in tools.tool_table()], [t.name for t in tools_])
        self.assertEqual({"event": "tools", "table": "legacy", "reason": "daemon_without_method_table"}, entry)

    def test_a_daemon_with_the_table_gets_the_projection(self) -> None:
        tools_, entry = self.choose(listing(PUBLISHED, {"run.status"}))
        self.assertEqual(["recursion_run_status"], [t.name for t in tools_])
        self.assertEqual(("projected", ["recursion_run_status"]), (entry["table"], entry["tools"]))

    def test_without_the_published_files_the_legacy_four(self) -> None:
        tools_, entry = self.choose(listing(PUBLISHED), published=None)
        self.assertEqual(4, len(tools_))
        self.assertEqual("published_table_missing", entry["reason"])

    def test_any_other_answer_refuses(self) -> None:
        for answer in ({"code": "unavailable", "detail": "daemon_closing"}, {"code": "scope_refused"},
                       {"code": "control_api_unavailable", "delivered": False}, {}, "ok"):
            with self.subTest(answer=answer), self.assertRaises(ProjectionRefused) as caught:
                self.choose(answer)
            self.assertEqual("listing_unavailable", caught.exception.reason)


if __name__ == "__main__":
    unittest.main()
