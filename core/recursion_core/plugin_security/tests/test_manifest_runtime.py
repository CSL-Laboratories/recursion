"""The manifest's ``runtime`` field, and the names a manifest may not use yet.

``python-sandboxed/1`` is accepted; ``python-inprocess/1`` is accepted as written with the finding
``runtime_inprocess_retiring``; a reserved name (``wasm-component/1``, ``data/1``) is refused
``runtime_reserved`` with its own text; any other value is refused ``manifest_runtime`` with a text
that names what this release runs. The permission names ``schedule``, ``memory`` and ``computer`` are
reserved and refused ``permission_reserved``. The record kinds a later release will use are held in
the schema's reserved list. Every list stays append-only: no text says it is closed. Keys here are
made up; nothing is signed.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from recursion_core.plugin_security.canonical import canonical
from recursion_core.plugin_security.errors import Refused
from recursion_core.plugin_security.loader import CoreHealth, PluginLoader
from recursion_core.plugin_security.manifest import (
    MANIFEST_SCHEMA_V1, PERMISSIONS, PERMISSIONS_V1, ReservedEffects, TrustStore, parse_manifest,
)
from recursion_core.plugin_security.tests.support import PLUGIN_SOURCE, FakeGilProbe, manifest_bytes
from recursion_core.plugin_security.tests.test_manifest_v1 import FAKE_KEY, v1

ROOT = Path(__file__).resolve().parents[4]
PLUGIN_SCHEMAS = ROOT / "schemas" / "plugin" / "v1"
FIXTURES = PLUGIN_SCHEMAS / "fixtures"

SANDBOXED = "python-sandboxed/1"
INPROCESS = "python-inprocess/1"
WASM_TEXT = "wasm-component/1 is reserved for a later release; this release runs python-sandboxed/1 only."
UNKNOWN_TEXT = "unknown runtime; this release runs python-sandboxed/1 only"
RESERVED_PERMISSIONS = ("schedule", "memory", "computer")
RESERVED_RECORD_KINDS = ("trigger.v1", "agent.v1", "memory.lesson.v1", "work.packet.v1", "node.v1")
NOT_FROZEN = "reserved is not frozen: a reserved name may be corrected until the first build that answers it"
# Words that would say a list is closed. A later runtime, permission or record kind is an append.
CLOSED_WORDS = ("the only runtime", "only python-inprocess/1 is defined", "only python-sandboxed/1 is defined",
                "has one value", "one runtime")


def _schema() -> dict:
    return json.loads((PLUGIN_SCHEMAS / "manifest.v1.json").read_text(encoding="utf-8"))


def _reserved(group: str) -> dict:
    return _schema()["$defs"]["reserved"]["$defs"][group]["$defs"]


def _refusal(raw: bytes) -> Refused:
    try:
        parse_manifest(raw)
    except Refused as exc:
        return exc
    raise AssertionError("the manifest was accepted")


def _doctor(raw: bytes) -> list:
    loader = PluginLoader(TrustStore({}), {}, ReservedEffects(), CoreHealth(), FakeGilProbe())
    return loader.doctor(raw, None, PLUGIN_SOURCE)


class RuntimeField(unittest.TestCase):
    def test_python_sandboxed_1_is_accepted_and_kept_as_written(self) -> None:
        manifest = parse_manifest(v1(runtime=SANDBOXED))
        self.assertEqual((manifest.schema, manifest.runtime), (MANIFEST_SCHEMA_V1, SANDBOXED))
        codes = [f["code"] for f in _doctor(v1(runtime=SANDBOXED))]
        self.assertIn("manifest_ok", codes)
        self.assertNotIn("runtime_inprocess_retiring", codes)

    def test_python_inprocess_1_is_accepted_as_written_with_a_retiring_finding(self) -> None:
        self.assertEqual(parse_manifest(v1(runtime=INPROCESS)).runtime, INPROCESS)
        found = [f for f in _doctor(v1(runtime=INPROCESS)) if f["code"] == "runtime_inprocess_retiring"]
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["stage"], found[0]["severity"]), ("verify", "warn"))

    def test_wasm_component_1_is_refused_reserved_with_the_contract_text(self) -> None:
        exc = _refusal(v1(runtime="wasm-component/1"))
        self.assertEqual((exc.code, exc.detail), ("runtime_reserved", WASM_TEXT))
        self.assertNotIn("unknown runtime", exc.detail)

    def test_data_1_is_refused_reserved_and_not_as_an_unknown_runtime(self) -> None:
        exc = _refusal(v1(runtime="data/1"))
        self.assertEqual(exc.code, "runtime_reserved")
        self.assertIn("reserved; not run in this release", exc.detail)
        self.assertNotIn("unknown runtime", exc.detail)
        self.assertNotEqual(exc.detail, WASM_TEXT)

    def test_a_reserved_runtime_is_refused_at_the_doctor_before_any_code(self) -> None:
        for name in ("wasm-component/1", "data/1"):
            with self.subTest(runtime=name):
                findings = _doctor(v1(runtime=name))
                self.assertEqual([(f["code"], f["severity"]) for f in findings], [("runtime_reserved", "refuse")])

    def test_any_other_runtime_is_refused_unknown_naming_what_this_release_runs(self) -> None:
        for value in ("python-subprocess/1", "wasm/1", "python-sandboxed/2", "", ["python-sandboxed/1"],
                      None, 1, {}):
            with self.subTest(runtime=value):
                exc = _refusal(v1(runtime=value))
                self.assertEqual((exc.code, exc.detail), ("manifest_runtime", UNKNOWN_TEXT))

    def test_the_schema_lists_the_runtimes_and_holds_the_reserved_ones_out(self) -> None:
        schema = _schema()
        self.assertEqual(schema["x-version"], "1.0-draft.2")
        self.assertEqual(schema["properties"]["runtime"]["enum"], [SANDBOXED, INPROCESS])
        reserved = _reserved("runtime")
        self.assertEqual(sorted(reserved), ["data/1", "wasm-component/1"])
        self.assertFalse(set(reserved) & set(schema["properties"]["runtime"]["enum"]))
        self.assertIn(WASM_TEXT, reserved["wasm-component/1"]["description"])
        self.assertIn("reserved; not run in this release", reserved["data/1"]["description"])
        self.assertIn(NOT_FROZEN, schema["$defs"]["reserved"]["description"])


class ReservedPermissions(unittest.TestCase):
    def test_a_reserved_permission_is_refused_reserved_and_not_as_unknown(self) -> None:
        for name in RESERVED_PERMISSIONS:
            for form, raw in (("v1", v1(permissions=("read", name))),
                              ("vNext", manifest_bytes(FAKE_KEY, permissions=["read", name]))):
                with self.subTest(permission=name, form=form):
                    exc = _refusal(raw)
                    self.assertEqual(exc.code, "permission_reserved")
                    self.assertIn("not available", exc.detail)
                    self.assertIn("reserved; not available in this release", exc.detail)
                    self.assertNotIn("unknown", exc.detail)

    def test_the_schema_holds_the_reserved_permissions_out_of_its_list(self) -> None:
        schema = _schema()
        reserved = _reserved("permission")
        self.assertEqual(sorted(reserved), sorted(RESERVED_PERMISSIONS))
        self.assertFalse(set(reserved) & set(schema["$defs"]["permission"]["enum"]))
        self.assertFalse(set(reserved) & (set(PERMISSIONS) | set(PERMISSIONS_V1)))
        for name in RESERVED_PERMISSIONS:
            self.assertTrue(reserved[name]["description"].strip())


class ReservedRecordKinds(unittest.TestCase):
    def test_the_schema_holds_the_five_record_kinds_with_a_sentence_each(self) -> None:
        # The manifest has no field that declares record kinds, so the names are held in the reserved
        # list and no field is invented for them.
        reserved = _reserved("record_kind")
        self.assertEqual(sorted(reserved), sorted(RESERVED_RECORD_KINDS))
        for name in RESERVED_RECORD_KINDS:
            self.assertTrue(reserved[name]["description"].strip())
        self.assertNotIn("record_kinds", _schema()["properties"])


class FixturesAndTexts(unittest.TestCase):
    def _fixture(self, name: str) -> bytes:
        return canonical(json.loads((FIXTURES / name).read_text(encoding="utf-8")))

    def test_the_new_fixtures_get_the_parser_answers_and_are_indexed(self) -> None:
        self.assertEqual(parse_manifest(self._fixture("good/manifest.v1/h04-a-sandboxed-runtime.json")).runtime,
                         SANDBOXED)
        for name in ("bad/manifest.v1/b12-a-reserved-runtime.json", "bad/manifest.v1/b13-the-reserved-data-runtime.json"):
            with self.subTest(fixture=name):
                self.assertEqual(_refusal(self._fixture(name)).code, "runtime_reserved")
        index = {e["file"]: e for e in json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))["fixtures"]}
        for name in ("bad/manifest.v1/b12-a-reserved-runtime.json", "bad/manifest.v1/b13-the-reserved-data-runtime.json"):
            self.assertEqual(index[name]["expect"], {"keyword": "enum", "at": "/runtime"})
        self.assertIn("good/manifest.v1/h04-a-sandboxed-runtime.json", index)

    def test_no_text_says_the_runtime_list_is_closed(self) -> None:
        for path in (PLUGIN_SCHEMAS / "manifest.v1.json", PLUGIN_SCHEMAS / "README.md", FIXTURES / "index.json"):
            text = path.read_text(encoding="utf-8").lower()
            for words in CLOSED_WORDS:
                with self.subTest(file=path.name, words=words):
                    self.assertNotIn(words, text)


if __name__ == "__main__":
    unittest.main()
