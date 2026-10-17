#!/usr/bin/env python3
"""Corpus tests for the CSL-238 public wire vocabulary draft fixtures.

Provider-free: loads tools/contract_fixtures/manifest.json, validates every
positive fixture against its declared schema $def and every adverse fixture to
rejection. Semantic rules beyond JSON Schema (x-sorted-set,
x-preferred-in-supported) are declared in the manifest and enforced here.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tools" / "contract_fixtures"
MANIFEST_PATH = FIXTURES / "manifest.json"
SCHEMA_PATH = FIXTURES / "schema" / "wire-vocab-draft.schema.json"

RECORD_DEFS = {
    "wire_identity_ref",
    "wire_event_envelope",
    "wire_command_outcome",
    "wire_version_advertisement",
}


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class ContractFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = load_json(SCHEMA_PATH)
        cls.defs = cls.schema["$defs"]
        cls.manifest = load_json(MANIFEST_PATH)

    def target_schema(self, target: str) -> dict:
        return {"$defs": self.defs, "$ref": f"#/$defs/{target}"}

    def schema_errors(self, fixture: dict, doc) -> list[str]:
        validator = jsonschema.Draft202012Validator(self.target_schema(fixture["target"]))
        return [error.message for error in validator.iter_errors(doc)]

    def semantic_errors(self, fixture: dict, doc) -> list[str]:
        errors: list[str] = []
        target = self.defs[fixture["target"]]
        for name, prop in target.get("properties", {}).items():
            if prop.get("x-sorted-set") and name in doc:
                value = doc[name]
                if not isinstance(value, list) or value != sorted(set(value)):
                    errors.append(f"{name} violates x-sorted-set")
        if target.get("x-preferred-in-supported") and "preferred" in doc:
            if doc["preferred"] not in doc.get("supported", []):
                errors.append("preferred must be a member of supported")
        return errors

    def test_manifest_integrity(self) -> None:
        ids = [f["id"] for f in self.manifest["fixtures"]]
        self.assertEqual(len(ids), len(set(ids)), "duplicate fixture ids")
        for fixture in self.manifest["fixtures"]:
            self.assertTrue((FIXTURES / fixture["path"]).is_file(), fixture["path"])
            self.assertIn(fixture["target"], self.defs, fixture["id"])
            self.assertIn(fixture["kind"], ("positive", "adverse"), fixture["id"])
            expected = "accept" if fixture["kind"] == "positive" else "reject"
            self.assertEqual(fixture["expect"], expected, fixture["id"])

    def test_every_record_type_has_positive_and_adverse_coverage(self) -> None:
        positive = {f["target"] for f in self.manifest["fixtures"] if f["kind"] == "positive"}
        adverse = {f["target"] for f in self.manifest["fixtures"] if f["kind"] == "adverse"}
        self.assertTrue(RECORD_DEFS <= positive)
        self.assertTrue(RECORD_DEFS <= adverse)

    def test_positive_fixtures_validate(self) -> None:
        for fixture in self.manifest["fixtures"]:
            if fixture["kind"] != "positive":
                continue
            doc = load_json(FIXTURES / fixture["path"])
            errors = self.schema_errors(fixture, doc) + self.semantic_errors(fixture, doc)
            self.assertEqual(errors, [], fixture["id"])

    def test_adverse_fixtures_reject(self) -> None:
        for fixture in self.manifest["fixtures"]:
            if fixture["kind"] != "adverse":
                continue
            doc = load_json(FIXTURES / fixture["path"])
            errors = self.schema_errors(fixture, doc) + self.semantic_errors(fixture, doc)
            self.assertTrue(errors, f"{fixture['id']} unexpectedly accepted")

    def test_refusal_outcome_requires_block(self) -> None:
        refused = self.defs["wire_command_outcome"]["allOf"][0]
        self.assertIn("required", refused["then"])
        self.assertIn("refusal", refused["then"]["required"])

    def test_reason_registry_is_closed(self) -> None:
        registry = self.defs["reason_code"]["enum"]
        self.assertEqual(len(registry), len(set(registry)))
        for code in registry:
            self.assertRegex(code, r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")


if __name__ == "__main__":
    unittest.main()
