#!/usr/bin/env python3
"""The project status surface must never translate unreadable state into empty state."""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock


SOURCE = pathlib.Path(__file__).resolve().parents[2]


def load_project_pilot():
    if os.name == "nt":
        sys.modules.setdefault(
            "fcntl",
            types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=lambda *_args: None),
        )
    spec = importlib.util.spec_from_file_location(
        "project_pilot_status_control", SOURCE / "scripts/project-pilot.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PROJECT_PILOT = load_project_pilot()


class Fixture:
    def __init__(self) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="project-status-"))
        for relative in (
            "state/project-config",
            "state/pilots",
            "state/goals",
            "state/tasks",
            "state/runs",
            "state/evidence",
            "state/plans/example-ops",
            "state/pilot-windows",
            "state/assists",
            "loop/logs",
        ):
            (self.root / relative).mkdir(parents=True)
        (self.root / "state/portfolio.tsv").write_text(
            "#project\tpath\tremote\tbranch\tsshkey\troadmap\ttopic\tstatus\n"
            "example-ops\t/repo\torigin\tmain\t-\t-\t10\tactive\n",
            encoding="utf-8",
        )
        config = {
            "id": "example-ops",
            "pilot_id": "pilot-example-ops",
            "topic": 10,
            "charter": "fixture charter",
            "status": "active",
        }
        pilot = {
            "id": "pilot-example-ops",
            "project_id": "example-ops",
            "topic": 10,
            "charter": "fixture charter",
            "status": "active",
        }
        self.write_json("state/project-config/example-ops.json", config)
        self.write_json("state/pilots/pilot-example-ops.json", pilot)
        self.goal = self.root / "state/goals/goal-example-ops.md"
        self.goal.write_text(
            "id: goal-example-ops\nproject_id: example-ops\nstatus: active\n", encoding="utf-8"
        )
        (self.root / "state/tasks/task-example-ops.md").write_text(
            "id: task-example-ops\nproject_id: example-ops\ngoal_id: goal-example-ops\nstatus: proposed\n",
            encoding="utf-8",
        )
        self.write_json(
            "state/plans/example-ops/plan-example-ops.json",
            {"id": "plan-example-ops", "project_id": "example-ops", "status": "approved"},
        )
        self.write_json(
            "state/pilot-windows/window-example-ops.json",
            {
                "id": "window-example-ops",
                "project_id": "example-ops",
                "goal_id": "goal-example-ops",
                "status": "armed",
            },
        )

    def write_json(self, relative: str, value: object) -> None:
        (self.root / relative).write_text(
            json.dumps(value, sort_keys=True) + "\n", encoding="utf-8"
        )

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class StatusFailClosedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = Fixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_readable_status_contains_authoritative_records(self) -> None:
        value = PROJECT_PILOT.scoped_state(self.fixture.root, "example-ops")
        self.assertEqual(len(value["plans"]), 1)
        self.assertEqual(len(value["goals"]), 1)
        self.assertEqual(len(value["tasks"]), 1)
        self.assertEqual(len(value["pilot_windows"]), 1)

    def test_lazily_absent_status_directories_are_authoritatively_empty(self) -> None:
        for relative in (
            "state/goals",
            "state/tasks",
            "state/runs",
            "state/evidence",
            "state/plans",
            "state/pilot-windows",
            "state/assists",
            "loop/logs",
        ):
            shutil.rmtree(self.fixture.root / relative)
        value = PROJECT_PILOT.scoped_state(self.fixture.root, "example-ops")
        for key in (
            "plans",
            "goals",
            "tasks",
            "runs",
            "evidence",
            "pilot_windows",
            "assists",
            "logs",
        ):
            self.assertEqual(value[key], [], key)

    def test_unlistable_control_directory_is_not_reported_as_empty(self) -> None:
        denied = self.fixture.root / "state/plans/example-ops"
        original = os.scandir

        def controlled_scandir(path):
            if pathlib.Path(path) == denied:
                raise PermissionError(13, "permission denied", str(path))
            return original(path)

        with mock.patch.object(PROJECT_PILOT.os, "scandir", side_effect=controlled_scandir):
            with self.assertRaises(SystemExit) as raised:
                PROJECT_PILOT.scoped_state(self.fixture.root, "example-ops")
        self.assertEqual(raised.exception.code, 74)

    def test_unreadable_control_record_is_not_silently_omitted(self) -> None:
        original = pathlib.Path.read_text

        def controlled_read(path, *args, **kwargs):
            if path == self.fixture.goal:
                raise PermissionError(13, "permission denied", str(path))
            return original(path, *args, **kwargs)

        with mock.patch.object(pathlib.Path, "read_text", controlled_read):
            with self.assertRaises(SystemExit) as raised:
                PROJECT_PILOT.scoped_state(self.fixture.root, "example-ops")
        self.assertEqual(raised.exception.code, 74)

    def test_wrong_type_status_directory_is_not_reported_as_empty(self) -> None:
        plans = self.fixture.root / "state/plans/example-ops"
        shutil.rmtree(plans)
        plans.write_text("not a directory\n", encoding="utf-8")
        with self.assertRaises(SystemExit) as raised:
            PROJECT_PILOT.scoped_state(self.fixture.root, "example-ops")
        self.assertEqual(raised.exception.code, 74)

    def test_symlinked_status_record_is_not_silently_omitted(self) -> None:
        directory = self.fixture.root / "state/goals"

        class SymlinkEntry:
            name = "goal-linked.md"

            @staticmethod
            def is_symlink():
                return True

            @staticmethod
            def is_file(*, follow_symlinks):
                return False

            @staticmethod
            def is_dir(*, follow_symlinks):
                return False

        class Scan:
            def __enter__(self):
                return iter((SymlinkEntry(),))

            def __exit__(self, *_args):
                return False

        with mock.patch.object(PROJECT_PILOT.os, "scandir", return_value=Scan()):
            with self.assertRaises(SystemExit) as raised:
                PROJECT_PILOT.status_files(directory, ".md", 100)
        self.assertEqual(raised.exception.code, 74)

    def test_oversized_status_record_uses_unavailable_exit(self) -> None:
        self.fixture.goal.write_text("x" * 131073, encoding="utf-8")
        with self.assertRaises(SystemExit) as raised:
            PROJECT_PILOT.scoped_state(self.fixture.root, "example-ops")
        self.assertEqual(raised.exception.code, 74)

    def test_malformed_status_json_uses_unavailable_exit(self) -> None:
        plan = self.fixture.root / "state/plans/example-ops/plan-example-ops.json"
        plan.write_text("{\n", encoding="utf-8")
        with self.assertRaises(SystemExit) as raised:
            PROJECT_PILOT.scoped_state(self.fixture.root, "example-ops")
        self.assertEqual(raised.exception.code, 74)

    def test_non_status_fields_call_remains_tolerant(self) -> None:
        original = pathlib.Path.read_text

        def controlled_read(path, *args, **kwargs):
            if path == self.fixture.goal:
                raise PermissionError(13, "permission denied", str(path))
            return original(path, *args, **kwargs)

        with mock.patch.object(pathlib.Path, "read_text", controlled_read):
            self.assertEqual(PROJECT_PILOT.fields(self.fixture.goal), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
