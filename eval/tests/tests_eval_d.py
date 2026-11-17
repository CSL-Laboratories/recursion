"""EVAL-D witnesses: record checks return errors, and bad inputs are typed refusals.

Scratch bases are folders under the test's own temporary directory. A base under the real runtime
directory is refused, so the evidence path that joins XDG_RUNTIME_DIR would error first.
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eval import arms, gitutil, harvest, heldout, record, runner, suite, tasks
from eval.tests.tests_record import good


def rehash(value):
    plan = {key: item for key, item in value["plan"].items() if key != "plan_sha256"}
    plan.update(suite_manifest_sha256=value["suite"]["manifest_sha256"],
                arm_settings_sha256=value["arm"]["settings_sha256"])
    value["plan"]["plan_sha256"] = record.sha256_json(plan)


def minimal_record():
    """A record shaped like the evidence helper. It is not asserted valid; the witnesses mutate it."""
    return {
        "schema": record.SCHEMA_ID, "record_id": "tiny", "label": "dev",
        "suite": {"id": "dev", "version": "1", "split": "dev", "frozen": False, "manifest_sha256": "0" * 64},
        "arm": {"name": "direct", "engine": "standin", "engine_version": "eval-standin/1",
                "model": None, "route": None, "effort": None, "settings_sha256": "0" * 64},
        "runner": {"version": "0.1.0", "sha256": "0" * 64, "pinned": False, "evaluated_commit": None},
        "protocol": runner.protocol_block(),
        "plan": {"k": 1, "attempts_per_task": 1, "replacements_per_task": 0, "seed": 1,
                 "tasks": [{"id": "tiny", "cluster": None, "task_sha256": "0" * 64, "created_at": None}],
                 "plan_sha256": "0" * 64},
        "environment": {"python": "offline", "platform": "offline"},
        "started_at": "2026-11-15T00:00:00Z", "finished_at": None,
        "trials": [runner.not_run_trial("tiny.1", "tiny", 1)],
    }


class RecordValidationTests(unittest.TestCase):
    def test_F08_validator_does_not_hash_an_invalid_id(self):
        doc = minimal_record()
        doc["trials"] = [{"trial_id": []}]
        self.assertTrue(record.validate(doc))

    def test_F09_record_validation_does_not_expand_the_plan(self):
        doc = minimal_record()
        doc["plan"]["attempts_per_task"] = 10**400
        # Overflows while building the index list, before a diagnostic is returned.
        self.assertTrue(record.validate(doc))

    def test_F10a_record_number_overflow_stays_in_validation(self):
        doc = minimal_record()
        doc["trials"][0]["timing"]["wall_seconds"] = 10**400
        self.assertIsInstance(record.validate(doc), list)

    def test_F09_invalid_trial_id_returns_diagnostics(self):
        value = good()
        value["trials"][0]["trial_id"] = []
        errors = record.validate(value)
        self.assertIsInstance(errors, list)
        self.assertTrue(any("trial_id" in item for item in errors))

    def test_f05_huge_attempt_count_is_reported_without_expansion(self):
        value = good()
        value["plan"]["attempts_per_task"] = 10**100
        rehash(value)
        self.assertTrue(record.validate(value))

    def test_f09_malformed_record_ids_are_validation_errors(self):
        for field in ("plan_id", "trial_id", "error_class"):
            with self.subTest(field=field):
                value = good()
                if field == "plan_id":
                    value["plan"]["tasks"][0]["id"] = []
                elif field == "trial_id":
                    value["trials"][0]["trial_id"] = []
                else:
                    value["trials"][0]["error"]["class"] = []
                self.assertTrue(record.validate(value))


class StreamCostTests(unittest.TestCase):
    def test_f06_stream_json_huge_cost_cannot_abort_result_parsing(self):
        raw = b'{"type":"result","subtype":"success","total_cost_usd":' + b"1" + b"0" * 400 + b"}\n"
        parsed = arms.parse_stream_json(raw, cost_basis="reported_not_billed")
        self.assertIsNone(parsed["usage"]["cost"])


class HarvestInputTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory(prefix="eval-d-")
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.tmp_base = self.root / "attempts"
        self.xdg_base = self.root / "xdg"
        self.tmp_base.mkdir()
        self.xdg_base.mkdir()

    def test_F07_bad_repository_entry_is_harvest_error(self):
        path = self.root / "repos.json"
        path.write_text('{"schema":"recursion-eval-repos/1","repos":[null]}', encoding="utf-8")
        with self.assertRaises(harvest.HarvestError):
            harvest.load_repos(path)

    def test_f03_harvest_input_errors_refuse_before_output_creation(self):
        upstream = self.root / "upstream"
        upstream.mkdir()
        gitutil.snapshot(upstream)
        cfg = {"name": "tiny", "path": str(upstream), "branch": "main",
               "test_globs": ["test_*.py"], "test_command": ["x"]}
        cases = (
            ("date", "2026-02-30", [cfg]),
            ("entry", "2026-01-01", [None]),
            ("name", "2026-01-01", [dict(cfg, name=3)]),
            ("null-name", "2026-01-01", [dict(cfg, name=None)]),
        )
        for name, date, entries in cases:
            with self.subTest(name=name):
                repos = self.root / (name + ".json")
                repos.write_text(json.dumps({"schema": harvest.REPOS_SCHEMA, "repos": entries}), encoding="utf-8")
                out = self.root / ("out-" + name)
                with contextlib.redirect_stderr(io.StringIO()):
                    code = harvest.main(["run", "--repos", str(repos), "--not-before", date, "--suite", "h",
                                         "--out", str(out), "--tmp-base", str(self.tmp_base),
                                         "--xdg-base", str(self.xdg_base)])
                self.assertEqual(code, 2)
                self.assertFalse(out.exists())
                if name == "date":
                    with self.assertRaises(harvest.HarvestError):
                        harvest.candidate_commits(upstream, "main", date)


class MalformedTaskCliTests(unittest.TestCase):
    def test_f04_malformed_tasks_are_typed_cli_refusals(self):
        root = tempfile.TemporaryDirectory(prefix="eval-d-cli-")
        self.addCleanup(root.cleanup)
        base = Path(root.name)
        for name, meta in (("truncated", "{"), ("command", '{"id":"tiny","test_command":[]}')):
            with self.subTest(name=name):
                folder = base / name
                folder.mkdir()
                (folder / "tiny.txtar").write_text(
                    "-- task.json --\n" + meta + "\n-- prompt.md --\np\n-- hidden/test_t.py --\n# empty\n",
                    encoding="utf-8")
                manifest = folder / "suite.json"
                manifest.write_text(json.dumps({
                    "schema": suite.SCHEMA_ID, "id": "dev", "version": "1", "split": "dev", "frozen": False,
                    "tasks": [{"id": "tiny", "file": "tiny.txtar"}],
                }), encoding="utf-8")
                with patch.object(runner, "DEV_SUITE", manifest), patch.object(heldout, "_declared", None), \
                        contextlib.redirect_stderr(io.StringIO()) as err:
                    code = runner.main(["--suite", "dev", "--arms", "direct", "--out", str(folder / "out")])
                self.assertEqual(code, 2)
                self.assertIn("refused:", err.getvalue())
                self.assertFalse((folder / "out").exists())


class HeldOutPathTests(unittest.TestCase):
    def test_F13_bad_path_is_heldout_denial(self):
        root = tempfile.TemporaryDirectory(prefix="eval-d-path-")
        self.addCleanup(root.cleanup)
        for value in (None, str(Path(root.name)) + "/\0"):
            with self.subTest(value=value), self.assertRaises(heldout.HeldOutDenied):
                heldout.check_path(value)


if __name__ == "__main__":
    unittest.main()
