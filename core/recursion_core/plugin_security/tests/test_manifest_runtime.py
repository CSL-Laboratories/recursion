"""The manifest's ``runtime`` field, and the names a manifest may not use yet.

``python-sandboxed/1`` is accepted; ``python-inprocess/1`` is accepted as written with the finding
``runtime_inprocess_retiring``; a reserved name (``wasm-component/1``, ``data/1``) is refused
``runtime_reserved`` with its own text; any other value is refused ``manifest_runtime`` with a text
that names what this release runs. The permission names ``schedule``, ``memory`` and ``computer`` are
reserved and refused ``permission_reserved``. The record kinds a later release will use are held in
the schema's reserved list. Every list stays append-only: no text says it is closed. No text says a
plugin is contained in this build: the schema and the README each say that nothing is contained yet,
and a ``python-sandboxed/1`` load gives the warning ``runtime_not_yet_contained``, which changes no
verdict. Keys here are made up; nothing is signed.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from recursion_core.plugin_security.canonical import canonical
from recursion_core.plugin_security.errors import Refused
from recursion_core.plugin_security.loader import CoreHealth, PluginLoader
from recursion_core.plugin_security.manifest import (
    MANIFEST_SCHEMA_V1, PERMISSIONS, PERMISSIONS_V1, Grant, ReservedEffects, TrustStore, parse_manifest,
)
from recursion_core.plugin_security.signing import key_id
from recursion_core.plugin_security.tests.support import PLUGIN_SOURCE, FakeGilProbe, manifest_bytes
from recursion_core.plugin_security.tests.test_manifest_v1 import FAKE_KEY, sha, v1, v1_fields

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
# The one sentence the runtime's description and the README each carry: what this build does.
HONEST = ("In this build a plugin of either runtime runs in the daemon's own process and nothing is contained "
          "yet; the name python-sandboxed/1 is accepted so that manifests can be written for the host that "
          "follows.")
# Words that would say or imply that this build runs a plugin in a sandbox.
SANDBOX_CLAIMS = ("in a sandboxed host", "sandboxed host process", "never in the daemon",
                  "is read as python-sandboxed/1", "behind the wall")
NOT_CONTAINED = "runtime_not_yet_contained"
RUNTIME_FINDINGS = (NOT_CONTAINED, "runtime_inprocess_retiring")


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


def _doctor(raw: bytes, artifact: bytes = PLUGIN_SOURCE) -> list:
    loader = PluginLoader(TrustStore({}), {}, ReservedEffects(), CoreHealth(), FakeGilProbe())
    return loader.doctor(raw, None, artifact)


def _load(raw: bytes, grant: Grant | None = None):
    grants = {} if grant is None else {grant.plugin_id: grant}
    return PluginLoader(TrustStore({}), grants, ReservedEffects(), CoreHealth(), FakeGilProbe()).load(
        raw, None, PLUGIN_SOURCE)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _without_runtime_findings(findings) -> list:
    return [(f["stage"], f["code"], f["severity"], f["detail"]) for f in findings
            if f["code"] not in RUNTIME_FINDINGS]


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

    def test_a_reserved_permission_has_a_bad_fixture_refused_at_its_place(self) -> None:
        name = "bad/manifest.v1/b14-a-reserved-permission.json"
        self.assertEqual(_refusal(self._fixture(name)).code, "permission_reserved")
        index = {e["file"]: e for e in json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))["fixtures"]}
        self.assertEqual(index[name]["expect"], {"keyword": "enum", "at": "/permissions/1"})

    def test_no_text_says_the_runtime_list_is_closed(self) -> None:
        for path in (PLUGIN_SCHEMAS / "manifest.v1.json", PLUGIN_SCHEMAS / "README.md", FIXTURES / "index.json"):
            text = path.read_text(encoding="utf-8").lower()
            for words in CLOSED_WORDS:
                with self.subTest(file=path.name, words=words):
                    self.assertNotIn(words, text)


class TheTextSaysNoMoreThanTheBuild(unittest.TestCase):
    def test_the_schema_and_the_readme_each_say_that_nothing_is_contained_yet(self) -> None:
        texts = {"manifest.v1.json runtime": _schema()["properties"]["runtime"]["description"],
                 "README.md": (PLUGIN_SCHEMAS / "README.md").read_text(encoding="utf-8")}
        for name, text in texts.items():
            with self.subTest(text=name):
                self.assertEqual(_flat(text).count(HONEST), 1)

    def test_no_text_says_this_build_runs_a_plugin_in_a_sandbox(self) -> None:
        details = [f["detail"] for runtime in (SANDBOXED, INPROCESS) for f in _doctor(v1(runtime=runtime))]
        texts = {"manifest.v1.json": (PLUGIN_SCHEMAS / "manifest.v1.json").read_text(encoding="utf-8"),
                 "README.md": (PLUGIN_SCHEMAS / "README.md").read_text(encoding="utf-8"),
                 "the loader's findings": "\n".join(details)}
        for name, text in texts.items():
            for words in SANDBOX_CLAIMS:
                with self.subTest(text=name, words=words):
                    self.assertNotIn(words, _flat(text).lower())


class NotYetContained(unittest.TestCase):
    def _one(self, findings) -> dict:
        found = [f for f in findings if f["code"] == NOT_CONTAINED]
        self.assertEqual(len(found), 1)
        return found[0]

    def test_a_sandboxed_plugin_gets_one_warning_at_the_doctor_and_at_load(self) -> None:
        raw = v1(runtime=SANDBOXED)
        for where, findings in (("doctor", _doctor(raw)), ("load", _load(raw).findings)):
            with self.subTest(where=where):
                finding = self._one(findings)
                self.assertEqual((finding["stage"], finding["severity"]), ("verify", "warn"))
                self.assertIn("not contained in this build", finding["detail"])
                self.assertIn("the daemon's own process", finding["detail"])

    def test_the_retiring_runtime_keeps_its_own_warning_and_does_not_get_this_one(self) -> None:
        codes = [f["code"] for f in _doctor(v1(runtime=INPROCESS))]
        self.assertNotIn(NOT_CONTAINED, codes)
        self.assertEqual(codes.count("runtime_inprocess_retiring"), 1)

    def test_no_manifest_field_silences_it(self) -> None:
        cases = {"read only": v1(runtime=SANDBOXED, permissions=("read",)),
                 "network": v1(runtime=SANDBOXED, permissions=("read", "network"), network=("api.example.org",)),
                 "process and order": v1(runtime=SANDBOXED, permissions=("read", "process", "order")),
                 "a dependency lock": v1(runtime=SANDBOXED, lock="0" * 64),
                 "install scripts": v1(runtime=SANDBOXED, install_scripts=("python setup.py",))}
        for name, raw in cases.items():
            with self.subTest(manifest=name):
                self._one(_doctor(raw))
        for key in ("quiet", "findings", "contained"):
            with self.subTest(extra_field=key):
                body = v1_fields(runtime=SANDBOXED)
                body[key] = True
                self.assertEqual(_refusal(canonical(body)).code, "manifest_fields")

    def test_it_changes_no_verdict(self) -> None:
        sandboxed, inprocess = _load(v1(runtime=SANDBOXED)), _load(v1(runtime=INPROCESS))
        self.assertEqual(_without_runtime_findings(sandboxed.findings), _without_runtime_findings(inprocess.findings))
        self.assertEqual((sandboxed.permissions, sandboxed.trust), (inprocess.permissions, inprocess.trust))
        self.assertIsNotNone(sandboxed.module)
        refused = {runtime: _doctor(v1(runtime=runtime), artifact=b"other bytes") for runtime in (SANDBOXED, INPROCESS)}
        self.assertEqual(refused[SANDBOXED][-1]["code"], "artifact_digest_mismatch")
        self.assertEqual(_without_runtime_findings(refused[SANDBOXED]), _without_runtime_findings(refused[INPROCESS]))
        self._one(refused[SANDBOXED])
        self.assertEqual(_refusal(v1(runtime=SANDBOXED, permissions=("read", "memory"))).code, "permission_reserved")

    def test_it_changes_no_verdict_for_a_plugin_that_holds_a_grant(self) -> None:
        grant = Grant("demo.plugin", sha(PLUGIN_SOURCE), None, ("read",), developer_key_id=key_id(FAKE_KEY))
        loads = {runtime: _load(v1(runtime=runtime, permissions=("read",)), grant) for runtime in (SANDBOXED, INPROCESS)}
        for runtime, loaded in loads.items():
            with self.subTest(runtime=runtime):
                self.assertEqual((loaded.trust, loaded.permissions), ("unsigned", ("read",)))


if __name__ == "__main__":
    unittest.main()
