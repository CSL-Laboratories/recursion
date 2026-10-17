#!/usr/bin/env python3
"""Corpus tests for the CSL-238 public wire vocabulary draft fixtures.

Provider-free: loads tools/contract_fixtures/manifest.json, validates every
positive fixture against its declared schema $def and every adverse fixture to
rejection. Checks beyond JSON Schema are declared in the manifest and enforced
here: strict I-JSON decoding (duplicate keys, floats, lexical -0, non-I-JSON
constants), x-sorted-set, x-calendar-date, x-preferred-in-supported and
x-sunset-subset-of-supported.
"""
from __future__ import annotations

import datetime
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


class StrictDecodeError(ValueError):
    pass


def _no_duplicate_keys(pairs: list) -> dict:
    seen: set = set()
    out: dict = {}
    for key, value in pairs:
        if key in seen:
            raise StrictDecodeError(f"duplicate object key: {key!r}")
        seen.add(key)
        out[key] = value
    return out


def _strict_int(token: str) -> int:
    if token == "-0":
        raise StrictDecodeError("lexical -0 is not valid I-JSON")
    return int(token)


def _reject_non_ijson(token: str):
    raise StrictDecodeError(f"non-I-JSON literal: {token}")


def strict_load(path: Path):
    """I-JSON profile decode: no duplicate keys, floats, -0 or NaN/Infinity."""
    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_no_duplicate_keys,
        parse_int=_strict_int,
        parse_float=_reject_non_ijson,
        parse_constant=_reject_non_ijson,
    )


class ContractFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = strict_load(SCHEMA_PATH)
        cls.defs = cls.schema["$defs"]
        cls.manifest = strict_load(MANIFEST_PATH)

    def target_schema(self, target: str) -> dict:
        return {"$defs": self.defs, "$ref": f"#/$defs/{target}"}

    def resolve(self, prop: dict) -> dict:
        while "$ref" in prop:
            prop = self.defs[prop["$ref"].rsplit("/", 1)[-1]]
        return prop

    def schema_errors(self, fixture: dict, doc) -> list[str]:
        validator = jsonschema.Draft202012Validator(self.target_schema(fixture["target"]))
        return [error.message for error in validator.iter_errors(doc)]

    def semantic_errors(self, fixture: dict, doc) -> list[str]:
        errors: list[str] = []
        target = self.defs[fixture["target"]]
        for name, raw_prop in target.get("properties", {}).items():
            prop = self.resolve(raw_prop)
            if prop.get("x-sorted-set") and name in doc:
                value = doc[name]
                if not isinstance(value, list) or value != sorted(set(value)):
                    errors.append(f"{name} violates x-sorted-set")
            if prop.get("x-calendar-date") and name in doc:
                try:
                    datetime.datetime.strptime(doc[name], "%Y-%m-%dT%H:%M:%SZ")
                except (TypeError, ValueError):
                    errors.append(f"{name} violates x-calendar-date")
        if target.get("x-preferred-in-supported") and "preferred" in doc:
            if doc["preferred"] not in doc.get("supported", []):
                errors.append("preferred must be a member of supported")
        if target.get("x-sunset-subset-of-supported") and "sunset" in doc:
            if not set(doc["sunset"]) <= set(doc.get("supported", [])):
                errors.append("sunset must be a subset of supported")
        return errors

    def check_fixture(self, fixture: dict) -> list[str]:
        try:
            doc = strict_load(FIXTURES / fixture["path"])
        except (StrictDecodeError, json.JSONDecodeError) as exc:
            return [f"strict decode failed: {exc}"]
        return self.schema_errors(fixture, doc) + self.semantic_errors(fixture, doc)

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
            self.assertEqual(self.check_fixture(fixture), [], fixture["id"])

    def test_adverse_fixtures_reject(self) -> None:
        for fixture in self.manifest["fixtures"]:
            if fixture["kind"] != "adverse":
                continue
            self.assertTrue(self.check_fixture(fixture), f"{fixture['id']} unexpectedly accepted")

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
