#!/usr/bin/env python3
"""Corpus tests for the CSL-238 public wire vocabulary draft fixtures.

Provider-free: loads tools/contract_fixtures/manifest.json, validates every
positive fixture against its declared schema $def and every adverse fixture to
rejection. Checks beyond JSON Schema are declared in the manifest and enforced
here: strict I-JSON decoding (duplicate keys, floats, lexical -0, non-I-JSON
constants, unpaired surrogates), x-sorted-set, x-calendar-date, x-preferred-in-supported and
x-sunset-subset-of-supported. Manifest "sequences" entries pin the command
journal's replay/conflict semantics as ordered send/expect scenarios.
"""
from __future__ import annotations

import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tools" / "contract_fixtures"
MANIFEST_PATH = FIXTURES / "manifest.json"
SCHEMA_PATH = FIXTURES / "schema" / "wire-vocab-draft.schema.json"
SEQUENCE_SCHEMA_PATH = FIXTURES / "schema" / "journal-sequence.schema.json"

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
    try:
        return int(token)
    except ValueError as exc:
        # Past the host's integer-string limit. This is still a decode refusal;
        # letting ValueError escape crashes the fixture runner.
        raise StrictDecodeError("integer exceeds the decoder digit limit") from exc


def _reject_non_ijson(token: str):
    raise StrictDecodeError(f"non-I-JSON literal: {token}")


def _reject_unpaired_surrogates(value) -> None:
    """I-JSON forbids unpaired surrogates. json.loads keeps them as str."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise StrictDecodeError("unpaired surrogate") from exc
    elif isinstance(value, list):
        for item in value:
            _reject_unpaired_surrogates(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            _reject_unpaired_surrogates(key)
            _reject_unpaired_surrogates(item)


def strict_load(path: Path):
    """I-JSON profile decode: no duplicate keys, floats, -0, NaN/Infinity, or unpaired surrogates."""
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),  # invalid UTF-8 raises UnicodeDecodeError
            object_pairs_hook=_no_duplicate_keys,
            parse_int=_strict_int,
            parse_float=_reject_non_ijson,
            parse_constant=_reject_non_ijson,
        )
    except RecursionError as exc:
        raise StrictDecodeError("nesting is too deep to decode") from exc
    _reject_unpaired_surrogates(value)
    return value


def _strings(value) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


class ContractFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = strict_load(SCHEMA_PATH)
        cls.defs = cls.schema["$defs"]
        cls.manifest = strict_load(MANIFEST_PATH)
        cls.seq_schema = strict_load(SEQUENCE_SCHEMA_PATH)

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
        # Schema rejection returns before this on the fixture path. Direct calls
        # and a schema hole must still refuse, not raise TypeError.
        if not isinstance(doc, dict):
            return ["document is not an object"]
        errors: list[str] = []
        target = self.defs[fixture["target"]]
        for name, raw_prop in target.get("properties", {}).items():
            prop = self.resolve(raw_prop)
            if prop.get("x-sorted-set") and name in doc:
                value = doc[name]
                if not _strings(value) or value != sorted(set(value)):
                    errors.append(f"{name} violates x-sorted-set")
            if prop.get("x-calendar-date") and name in doc:
                try:
                    datetime.datetime.strptime(doc[name], "%Y-%m-%dT%H:%M:%SZ")
                except (TypeError, ValueError):
                    errors.append(f"{name} violates x-calendar-date")
        if target.get("x-preferred-in-supported") and "preferred" in doc:
            supported = doc.get("supported", [])
            preferred = doc["preferred"]
            if not _strings(supported) or not isinstance(preferred, str) or preferred not in supported:
                errors.append("preferred must be a member of supported")
        if target.get("x-sunset-subset-of-supported") and "sunset" in doc:
            sunset = doc["sunset"]
            supported = doc.get("supported", [])
            if not _strings(sunset) or not _strings(supported) or not set(sunset) <= set(supported):
                errors.append("sunset must be a subset of supported")
        return errors

    def check_fixture(self, fixture: dict) -> list[str]:
        try:
            doc = strict_load(FIXTURES / fixture["path"])
        except (StrictDecodeError, json.JSONDecodeError, UnicodeDecodeError, ValueError, RecursionError) as exc:
            return [f"strict decode failed: {exc}"]
        schema_errs = self.schema_errors(fixture, doc)
        if schema_errs:
            # semantic rules only apply to schema-valid documents
            return schema_errs
        return self.semantic_errors(fixture, doc)

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

    def sequence_schema_errors(self, doc) -> list[str]:
        validator = jsonschema.Draft202012Validator(self.seq_schema)
        return [error.message for error in validator.iter_errors(doc)]

    def run_journal_sequence(self, seq: dict) -> list[str]:
        """Simulate the draft command journal over a sequence's steps.

        First receipt for an unjournaled command_id must be accepted or refused;
        an identical retry (same digest) answers duplicate for admitted commands
        or replays the journaled receipt verbatim for refused/conflict; a
        different digest under a journaled id answers conflict and never rebinds.
        """
        errors: list[str] = []
        journal: dict = {}
        outcome_target = {"target": "wire_command_outcome"}
        for index, step in enumerate(seq["steps"]):
            tag = f"step {index}"
            send, expect = step["send"], step["expect"]
            for message in self.schema_errors(outcome_target, expect) + self.semantic_errors(
                outcome_target, expect
            ):
                errors.append(f"{tag} expected receipt invalid: {message}")
            command_id, command_digest = send["command_id"], send["command_digest"]
            if (
                expect.get("command_id") != command_id
                or expect.get("command_digest") != command_digest
            ):
                errors.append(f"{tag}: expected receipt does not echo the sent identity")
            entry = journal.get(command_id)
            if entry is None:
                if expect.get("outcome") not in ("accepted", "refused"):
                    errors.append(
                        f"{tag}: first receipt for unjournaled id claims "
                        f"{expect.get('outcome')!r}"
                    )
                journal[command_id] = {"digest": command_digest, "attempts": {command_digest: expect}}
                produced = expect
            elif command_digest == entry["digest"]:
                bound = entry["attempts"][entry["digest"]]
                if bound["outcome"] == "accepted":
                    produced = {
                        "type": "WireCommandOutcome@1",
                        "command_id": command_id,
                        "command_digest": command_digest,
                        "outcome": "duplicate",
                    }
                else:
                    produced = bound
            else:
                produced = entry["attempts"].get(command_digest)
                if produced is None:
                    produced = {
                        "type": "WireCommandOutcome@1",
                        "command_id": command_id,
                        "command_digest": command_digest,
                        "outcome": "conflict",
                    }
                    entry["attempts"][command_digest] = produced
            if produced != expect:
                errors.append(f"{tag}: produced {produced} != expected {expect}")
        return errors

    def test_journal_sequences(self) -> None:
        for entry in self.manifest.get("sequences", []):
            with self.subTest(sequence=entry["id"]):
                try:
                    doc = strict_load(FIXTURES / entry["path"])
                except (StrictDecodeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    self.fail(f"{entry['id']} strict decode failed: {exc}")
                schema_errs = self.sequence_schema_errors(doc)
                if schema_errs:
                    self.fail(f"{entry['id']} malformed sequence: {schema_errs}")
                self.assertEqual(self.run_journal_sequence(doc), [], entry["id"])

    def _load_text(self, text: str):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json") as handle:
            handle.write(text)
            handle.flush()
            return strict_load(Path(handle.name))

    def test_overlong_integer_is_a_decode_error_not_a_crash(self) -> None:
        # Same input csl-canonical/1 refuses as integer_range. Past CPython's
        # int() digit limit the wire decoder raises ValueError out of strict_load.
        old = sys.get_int_max_str_digits()
        # CPython refuses a digit limit below 640. 641 digits is enough to pass
        # that floor and still trip the converter.
        sys.set_int_max_str_digits(640)
        try:
            with self.assertRaises(StrictDecodeError):
                self._load_text("9" * 641)
            with self.assertRaises(StrictDecodeError):
                self._load_text("-" + "9" * 641)
        finally:
            sys.set_int_max_str_digits(old)

    def test_deep_nesting_does_not_crash(self) -> None:
        # csl-canonical/1 refuses this as depth. The wire decoder must not raise
        # RecursionError; a clean decode error is enough (no new depth cap).
        text = "[" * 20000 + "]" * 20000
        try:
            self._load_text(text)
        except RecursionError as exc:
            self.fail(f"decoder crashed: {exc}")
        except StrictDecodeError:
            return

    def test_unpaired_surrogate_is_rejected(self) -> None:
        # The wire profile says I-JSON: no unpaired surrogates. csl-canonical/1
        # refuses these as lone_surrogate. strict_load currently returns them.
        for text in ('"\\ud800"', '{"a":"\\ud800"}', '{"\\ud800":1}'):
            with self.subTest(text=text):
                with self.assertRaises(StrictDecodeError):
                    self._load_text(text)

    def test_semantic_checks_do_not_crash_on_unhashable_or_scalar_documents(self) -> None:
        fixture = {"target": "wire_version_advertisement"}
        for doc in (
            1,
            "nope",
            {"supported": [{"a": 1}], "sunset": ["A@1"]},
            {"supported": ["b", 1], "sunset": ["A@1"]},
            {"supported": 1, "preferred": "A@1", "sunset": ["A@1"]},
        ):
            with self.subTest(doc=doc):
                try:
                    errors = self.semantic_errors(fixture, doc)
                except (TypeError, AttributeError) as exc:
                    self.fail(f"semantic_errors crashed: {exc}")
                self.assertTrue(errors, doc)

    def test_decoder_currently_accepts_integers_past_the_safe_range(self) -> None:
        # Owner question: csl-canonical/1 refuses 2^53 as integer_range. This
        # decoder still returns the Python int; record schemas then apply their
        # own maximum. Not changed.
        self.assertEqual(9007199254740992, self._load_text("9007199254740992"))
        self.assertEqual(-(2**53), self._load_text(str(-(2**53))))


if __name__ == "__main__":
    unittest.main()
