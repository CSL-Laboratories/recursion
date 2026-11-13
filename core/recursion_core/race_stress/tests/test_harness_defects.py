"""Repros for defects in the CSL-801 race_stress harness.

Each test fails on the unfixed harness and passes once that defect is fixed.
No measured tree and no network: fakes stand in for the stacked heads.
"""

from __future__ import annotations

import argparse
import contextlib
import gc
import importlib.machinery
import importlib.util
import io
import json
import os
import py_compile
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

DRIVERS = Path(__file__).resolve().parents[1] / "drivers"
sys.path.insert(0, str(DRIVERS))
import _harness as h  # noqa: E402
import breaker_326  # noqa: E402
import breaker_328  # noqa: E402
import daemon_324  # noqa: E402

from recursion_core.race_stress import flake_runner, pins  # noqa: E402

CORE = Path(__file__).resolve().parents[3]


def _git_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "pin-test"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "pin-test@example.com"
    return env


def _repo(path: Path, files: dict[str, str]) -> str:
    path.mkdir(parents=True)
    env = _git_env()
    for rel, text in files.items():
        dest = path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
    subprocess.check_call(["git", "init", "-q"], cwd=path, env=env)
    subprocess.check_call(["git", "add", "--", *files], cwd=path, env=env)
    subprocess.check_call(["git", "commit", "-q", "-m", "init"], cwd=path, env=env)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, env=env, text=True).strip()


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)


class SeedTests(unittest.TestCase):
    def test_interleave_module_keeps_seed_zero(self) -> None:
        env = os.environ.copy()
        env["RACE_STRESS_SEED"] = "0"
        env["PYTHONPATH"] = str(CORE)
        out = subprocess.check_output(
            [sys.executable, "-B", "-c",
             "from recursion_core.race_stress.tests.interleave_support import SEED; print(SEED)"],
            env=env, text=True, cwd=CORE)
        self.assertEqual(out.strip(), "0")


class PinTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {}
        for env in ("RACE_STRESS_TREE_324", "RACE_STRESS_TREE_326"):
            cur = pins.PINS[env]
            self._saved[env] = {"pr": cur["pr"], "head": cur["head"], "objects": dict(cur["objects"])}

    def tearDown(self) -> None:
        for env, saved in self._saved.items():
            pins.PINS[env] = {"pr": saved["pr"], "head": saved["head"], "objects": saved["objects"]}

    def _pin(self, env: str, repo: Path, head: str, rels: list[str]) -> None:
        objects = {}
        for rel in rels:
            objects[rel] = subprocess.check_output(
                ["git", "rev-parse", f"HEAD:{rel}"], cwd=repo, env=_git_env(), text=True).strip()
        pins.PINS[env] = {"pr": "test", "head": head, "objects": objects}

    def test_dirty_worktree_at_the_pinned_head_is_refused(self) -> None:
        rel = "core/recursion_core/routes/breaker.py"
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            head = _repo(repo, {rel: "v1\n"})
            oid = subprocess.check_output(
                ["git", "rev-parse", f"HEAD:{rel}"], cwd=repo, env=_git_env(), text=True).strip()
            pins.PINS["RACE_STRESS_TREE_326"] = {
                "pr": "test", "head": head, "objects": {rel: oid}}
            ok, message = pins.pin_status("RACE_STRESS_TREE_326", repo)
            self.assertTrue(ok, message)
            (repo / rel).write_text("v1\ndirty\n")
            ok, message = pins.pin_status("RACE_STRESS_TREE_326", repo)
            self.assertFalse(ok, message)
            self.assertIn(rel, message)

    def test_git_dir_cannot_make_another_checkout_look_pinned(self) -> None:
        rel = "core/recursion_core/routes/breaker.py"
        with tempfile.TemporaryDirectory() as tmp:
            repo_a = Path(tmp) / "a"
            repo_b = Path(tmp) / "b"
            head_a = _repo(repo_a, {rel: "alpha\n"})
            head_b = _repo(repo_b, {rel: "beta\n"})
            oid_a = subprocess.check_output(
                ["git", "rev-parse", f"HEAD:{rel}"], cwd=repo_a, env=_git_env(), text=True).strip()
            pins.PINS["RACE_STRESS_TREE_326"] = {
                "pr": "test", "head": head_a, "objects": {rel: oid_a}}
            old = os.environ.get("GIT_DIR")
            os.environ["GIT_DIR"] = str(repo_a / ".git")
            try:
                ok, message = pins.pin_status("RACE_STRESS_TREE_326", repo_b)
                info = h.tree_info(str(repo_b), rel)
            finally:
                if old is None:
                    os.environ.pop("GIT_DIR", None)
                else:
                    os.environ["GIT_DIR"] = old
            self.assertFalse(ok, message)
            self.assertEqual(info["tree_head"], head_b)
            self.assertNotEqual(head_a, head_b)

    def test_untracked_files_under_a_directory_pin_are_not_refused(self) -> None:
        """A file PathFinder cannot load, sitting in the pinned directory, stays allowed.

        ``scratch.txt`` and ``notes.txt`` have no source, bytecode, or extension suffix
        and do not create a directory, so they are not modules. ``scratch.py`` is not
        on that allowlist: it is a source module (``recursion_core.daemon.scratch``)
        even when no tracked file imports it. A new directory is not either: it is a
        namespace package. Those two are refused by
        ``test_untracked_importable_sources_under_a_directory_pin_are_refused``.
        """
        rel = "core/recursion_core/daemon"
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            files = {
                f"{rel}/__init__.py": "VALUE = 1\n",
                f"{rel}/registry.py": "VALUE = 1\n",
            }
            head = _repo(repo, files)
            self._pin("RACE_STRESS_TREE_324", repo, head, [rel])
            ok, message = pins.pin_status("RACE_STRESS_TREE_324", repo)
            self.assertTrue(ok, message)
            (repo / rel / "scratch.txt").write_text("not a module\n")
            (repo / rel / "notes.txt").write_text("not imported\n")
            status = subprocess.check_output(
                ["git", "status", "--porcelain", "--untracked-files=all", "--", rel],
                cwd=repo, env=_git_env(), text=True)
            self.assertIn("scratch.txt", status)
            self.assertIn("notes.txt", status)
            ok, message = pins.pin_status("RACE_STRESS_TREE_324", repo)
            self.assertTrue(ok, message)
            (repo / rel / "registry.py").write_text("VALUE = 2\n")
            ok, message = pins.pin_status("RACE_STRESS_TREE_324", repo)
            self.assertFalse(ok, message)
            self.assertIn(rel, message)

    def test_untracked_importable_sources_under_a_directory_pin_are_refused(self) -> None:
        rel = "core/recursion_core/daemon"
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            files = {
                f"{rel}/__init__.py": "VALUE = 1\n",
                f"{rel}/registry.py": "VALUE = 1\n",
            }
            head = _repo(repo, files)
            self._pin("RACE_STRESS_TREE_324", repo, head, [rel])
            (repo / rel / "scratch.py").write_text("VALUE = 'scratch'\n")
            note = repo / rel / "tests" / "local_note.py"
            note.parent.mkdir(parents=True)
            note.write_text("VALUE = 'note'\n")
            # notes.txt is not a module suffix, but tests/ is a new namespace package.
            (repo / rel / "tests" / "notes.txt").write_text("not a module\n")
            ok, message = pins.pin_status("RACE_STRESS_TREE_324", repo)
            self.assertFalse(ok, message)
            self.assertIn("scratch.py", message)
            self.assertIn("local_note.py", message)
            self.assertIn("notes.txt", message)

    def test_pycache_under_a_directory_pin_is_refused(self) -> None:
        # An unchecked-hash .pyc is what Python executes under -B; __file__ stays the .py.
        rel = "core/recursion_core/daemon"
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            files = {
                f"{rel}/__init__.py": "VALUE = 1\n",
                f"{rel}/registry.py": "VALUE = 1\n",
            }
            head = _repo(repo, files)
            self._pin("RACE_STRESS_TREE_324", repo, head, [rel])
            src = repo / rel / "registry.py"
            src.write_text("VALUE = 'cache'\n")
            py_compile.compile(
                str(src), doraise=True,
                invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
            src.write_text("VALUE = 1\n")
            cache = Path(importlib.util.cache_from_source(str(src)))
            self.assertTrue(cache.is_file(), cache)
            env = _git_env()
            env["PYTHONPATH"] = str(repo / "core")
            out = subprocess.check_output(
                [sys.executable, "-B", "-P", "-c",
                 "from recursion_core.daemon.registry import VALUE; print(VALUE)"],
                env=env, text=True, cwd=repo)
            self.assertEqual(out.strip(), "cache")
            ok, message = pins.pin_status("RACE_STRESS_TREE_324", repo)
            self.assertFalse(ok, message)
            self.assertIn("__pycache__", message)

    def test_ignored_importables_under_a_directory_pin_are_refused(self) -> None:
        rel = "core/recursion_core/daemon"
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            files = {
                f"{rel}/__init__.py": "VALUE = 1\n",
                f"{rel}/registry.py": "VALUE = 1\n",
                ".gitignore": "__pycache__/\n*.ignored.py\n",
            }
            head = _repo(repo, files)
            self._pin("RACE_STRESS_TREE_324", repo, head, [rel])
            ignored = repo / rel / "evil.ignored.py"
            ignored.write_text("VALUE = 'ignored'\n")
            src = repo / rel / "registry.py"
            py_compile.compile(str(src), doraise=True)
            status = subprocess.check_output(
                ["git", "status", "--porcelain", "--untracked-files=all", "--", rel],
                cwd=repo, env=_git_env(), text=True)
            self.assertEqual(status.strip(), "", status)
            self.assertEqual(
                subprocess.call(["git", "check-ignore", "-q", "--", str(ignored.relative_to(repo))],
                                 cwd=repo, env=_git_env()), 0)
            ok, message = pins.pin_status("RACE_STRESS_TREE_324", repo)
            self.assertFalse(ok, message)
            self.assertIn("evil.ignored.py", message)
            self.assertIn("__pycache__", message)

    def test_extension_module_under_a_directory_pin_is_refused(self) -> None:
        rel = "core/recursion_core/daemon"
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            files = {
                f"{rel}/__init__.py": "VALUE = 1\n",
                f"{rel}/registry.py": "VALUE = 1\n",
            }
            head = _repo(repo, files)
            self._pin("RACE_STRESS_TREE_324", repo, head, [rel])
            suffix = importlib.machinery.EXTENSION_SUFFIXES[0]
            ext = repo / rel / f"registry{suffix}"
            ext.write_bytes(b"not a real extension")
            ok, message = pins.pin_status("RACE_STRESS_TREE_324", repo)
            self.assertFalse(ok, message)
            self.assertIn(ext.name, message)

    def test_package_dir_shadowing_a_tracked_module_is_refused(self) -> None:
        # FileFinder selects foo/__init__.py before foo.py.
        rel = "core/recursion_core/daemon"
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            files = {
                f"{rel}/__init__.py": "VALUE = 1\n",
                f"{rel}/registry.py": "VALUE = 1\n",
            }
            head = _repo(repo, files)
            self._pin("RACE_STRESS_TREE_324", repo, head, [rel])
            pkg = repo / rel / "registry" / "__init__.py"
            pkg.parent.mkdir()
            pkg.write_text("VALUE = 'pkg'\n")
            env = _git_env()
            env["PYTHONPATH"] = str(repo / "core")
            out = subprocess.check_output(
                [sys.executable, "-B", "-P", "-c",
                 "from recursion_core.daemon.registry import VALUE; print(VALUE)"],
                env=env, text=True, cwd=repo)
            self.assertEqual(out.strip(), "pkg")
            ok, message = pins.pin_status("RACE_STRESS_TREE_324", repo)
            self.assertFalse(ok, message)
            self.assertIn("registry/__init__.py", message)

    def _pinned_file(self, tmp: str, *, gitignore: str | None = None) -> tuple[Path, str]:
        rel = "core/recursion_core/routes/breaker.py"
        repo = Path(tmp) / "repo"
        files = {rel: "v1\n", "core/recursion_core/routes/contract.py": "v1\n"}
        if gitignore is not None:
            files[".gitignore"] = gitignore
        head = _repo(repo, files)
        self._pin("RACE_STRESS_TREE_326", repo, head, [rel])
        ok, message = pins.pin_status("RACE_STRESS_TREE_326", repo)
        self.assertTrue(ok, message)
        return repo, rel

    def test_file_pin_refuses_sibling_pycache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, rel = self._pinned_file(tmp, gitignore="__pycache__/\n")
            sibling = repo / "core/recursion_core/routes/scratch.txt"
            sibling.write_text("not a module\n")
            ok, message = pins.pin_status("RACE_STRESS_TREE_326", repo)
            self.assertTrue(ok, message)
            src = repo / rel
            py_compile.compile(str(src), doraise=True)
            status = subprocess.check_output(
                ["git", "status", "--porcelain", "--untracked-files=all", "--",
                 "core/recursion_core/routes"],
                cwd=repo, env=_git_env(), text=True)
            self.assertNotIn("__pycache__", status)
            ok, message = pins.pin_status("RACE_STRESS_TREE_326", repo)
            self.assertFalse(ok, message)
            self.assertIn("__pycache__", message)

    def test_file_pin_refuses_extension_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, rel = self._pinned_file(tmp)
            suffix = importlib.machinery.EXTENSION_SUFFIXES[0]
            ext = (repo / rel).with_name("breaker" + suffix)
            ext.write_bytes(b"not a real extension")
            ok, message = pins.pin_status("RACE_STRESS_TREE_326", repo)
            self.assertFalse(ok, message)
            self.assertIn(ext.name, message)

    def test_file_pin_refuses_package_dir_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, rel = self._pinned_file(tmp)
            init = repo / "core/recursion_core/routes/breaker" / "__init__.py"
            init.parent.mkdir()
            init.write_text("VALUE = 'pkg'\n")
            env = _git_env()
            env["PYTHONPATH"] = str(repo / "core")
            out = subprocess.check_output(
                [sys.executable, "-B", "-P", "-c",
                 "from recursion_core.routes.breaker import VALUE; print(VALUE)"],
                env=env, text=True, cwd=repo)
            self.assertEqual(out.strip(), "pkg")
            ok, message = pins.pin_status("RACE_STRESS_TREE_326", repo)
            self.assertFalse(ok, message)
            self.assertIn("breaker/__init__.py", message)

    def test_untracked_toplevel_name_on_inserted_core_path_is_refused(self) -> None:
        rel = "core/recursion_core/daemon"
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            files = {
                f"{rel}/__init__.py": "VALUE = 1\n",
                f"{rel}/registry.py": "VALUE = 1\n",
            }
            head = _repo(repo, files)
            self._pin("RACE_STRESS_TREE_324", repo, head, [rel])
            shadow = repo / "core" / "dataclasses.py"
            shadow.write_text("SHADOW = True\n")
            pkg = repo / "core" / "json" / "__init__.py"
            pkg.parent.mkdir()
            pkg.write_text("SHADOW = True\n")
            env = _git_env()
            env["PYTHONPATH"] = str(repo / "core")
            out = subprocess.check_output(
                [sys.executable, "-B", "-P", "-c",
                 "import dataclasses, json; print(dataclasses.SHADOW, json.SHADOW)"],
                env=env, text=True, cwd=repo)
            self.assertEqual(out.strip(), "True True")
            ok, message = pins.pin_status("RACE_STRESS_TREE_324", repo)
            self.assertFalse(ok, message)
            self.assertIn("core/dataclasses.py", message)
            self.assertIn("core/json/__init__.py", message)

    def test_ignored_toplevel_name_on_inserted_core_path_is_refused(self) -> None:
        rel = "core/recursion_core/daemon"
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            files = {
                f"{rel}/__init__.py": "VALUE = 1\n",
                f"{rel}/registry.py": "VALUE = 1\n",
                ".gitignore": "dataclasses.py\n",
            }
            head = _repo(repo, files)
            self._pin("RACE_STRESS_TREE_324", repo, head, [rel])
            shadow = repo / "core" / "dataclasses.py"
            shadow.write_text("SHADOW = True\n")
            status = subprocess.check_output(
                ["git", "status", "--porcelain", "--untracked-files=all", "--", "core"],
                cwd=repo, env=_git_env(), text=True)
            self.assertNotIn("dataclasses.py", status)
            ok, message = pins.pin_status("RACE_STRESS_TREE_324", repo)
            self.assertFalse(ok, message)
            self.assertIn("core/dataclasses.py", message)

    def _outside_package(self, path: Path, body: str = "SHADOW = True\n") -> Path:
        path.mkdir(parents=True)
        (path / "__init__.py").write_text(body)
        return path

    def test_untracked_symlink_package_on_core_path_is_refused(self) -> None:
        # git ls-files --others prints the symlink and not the __init__.py it points at.
        rel = "core/recursion_core/daemon"
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            files = {
                f"{rel}/__init__.py": "from dataclasses import dataclass\nVALUE = 1\n",
                f"{rel}/registry.py": "VALUE = 1\n",
            }
            head = _repo(repo, files)
            self._pin("RACE_STRESS_TREE_324", repo, head, [rel])
            outside = self._outside_package(Path(tmp) / "dataclasses", "SHADOW = True\n")
            (repo / "core" / "dataclasses").symlink_to(outside)
            listed = subprocess.check_output(
                ["git", "ls-files", "-z", "--others", "--", "core"],
                cwd=repo, env=_git_env())
            names = [n for n in listed.split(b"\0") if n]
            self.assertTrue(any(n == b"core/dataclasses" for n in names), names)
            self.assertFalse(any(b"__init__.py" in n for n in names), names)
            env = _git_env()
            env["PYTHONPATH"] = str(repo / "core")
            out = subprocess.check_output(
                [sys.executable, "-B", "-P", "-c",
                 "import dataclasses; print(dataclasses.SHADOW)"],
                env=env, text=True, cwd=repo)
            self.assertEqual(out.strip(), "True")
            ok, message = pins.pin_status("RACE_STRESS_TREE_324", repo)
            self.assertFalse(ok, message)
            self.assertIn("core/dataclasses", message)
            self.assertIn("symlink", message)

    def test_untracked_symlinks_for_other_stdlib_names_are_refused(self) -> None:
        # Names the pinned daemon imports, refused before any of them is imported.
        rel = "core/recursion_core/daemon"
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            files = {f"{rel}/__init__.py": "VALUE = 1\n", f"{rel}/registry.py": "VALUE = 1\n"}
            head = _repo(repo, files)
            self._pin("RACE_STRESS_TREE_324", repo, head, [rel])
            for name in ("fcntl", "re", "contextlib", "typing"):
                outside = self._outside_package(Path(tmp) / name)
                (repo / "core" / name).symlink_to(outside)
            ok, message = pins.pin_status("RACE_STRESS_TREE_324", repo)
            self.assertFalse(ok, message)
            for name in ("fcntl", "re", "contextlib", "typing"):
                self.assertIn(f"core/{name}", message)

    def test_nested_git_directory_on_core_path_is_refused(self) -> None:
        rel = "core/recursion_core/daemon"
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            files = {f"{rel}/__init__.py": "VALUE = 1\n", f"{rel}/registry.py": "VALUE = 1\n"}
            head = _repo(repo, files)
            self._pin("RACE_STRESS_TREE_324", repo, head, [rel])
            shadow = repo / "core" / "dataclasses"
            shadow.mkdir()
            (shadow / "__init__.py").write_text("SHADOW = True\n")
            subprocess.check_call(["git", "init", "-q"], cwd=shadow, env=_git_env())
            listed = subprocess.check_output(
                ["git", "ls-files", "--others", "--", "core"],
                cwd=repo, env=_git_env(), text=True)
            self.assertIn("dataclasses", listed)
            self.assertNotIn("__init__.py", listed)
            ok, message = pins.pin_status("RACE_STRESS_TREE_324", repo)
            self.assertFalse(ok, message)
            self.assertIn("core/dataclasses", message)
            self.assertIn("nested git", message)

    def test_nested_git_file_under_a_pin_is_refused(self) -> None:
        rel = "core/recursion_core/daemon"
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            files = {f"{rel}/__init__.py": "VALUE = 1\n", f"{rel}/registry.py": "VALUE = 1\n"}
            head = _repo(repo, files)
            self._pin("RACE_STRESS_TREE_324", repo, head, [rel])
            (repo / rel / ".git").write_text("gitdir: /tmp/nowhere\n")
            status = subprocess.check_output(
                ["git", "status", "--porcelain", "--untracked-files=all", "--", rel],
                cwd=repo, env=_git_env(), text=True)
            self.assertNotIn(".git", status)
            ok, message = pins.pin_status("RACE_STRESS_TREE_324", repo)
            self.assertFalse(ok, message)
            self.assertIn("nested git", message)
            self.assertIn(".git", message)

    def test_untracked_symlink_package_shadowing_a_file_pin_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, rel = self._pinned_file(tmp)
            outside = self._outside_package(Path(tmp) / "breaker", "VALUE = 'pkg'\n")
            (repo / "core/recursion_core/routes/breaker").symlink_to(outside)
            env = _git_env()
            env["PYTHONPATH"] = str(repo / "core")
            out = subprocess.check_output(
                [sys.executable, "-B", "-P", "-c",
                 "from recursion_core.routes.breaker import VALUE; print(VALUE)"],
                env=env, text=True, cwd=repo)
            self.assertEqual(out.strip(), "pkg")
            ok, message = pins.pin_status("RACE_STRESS_TREE_326", repo)
            self.assertFalse(ok, message)
            self.assertIn("core/recursion_core/routes/breaker", message)
            self.assertIn("symlink", message)

    def test_untracked_symlink_package_shadowing_a_directory_pin_is_refused(self) -> None:
        rel = "core/recursion_core/daemon"
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            files = {f"{rel}/__init__.py": "VALUE = 1\n", f"{rel}/registry.py": "VALUE = 1\n"}
            head = _repo(repo, files)
            self._pin("RACE_STRESS_TREE_324", repo, head, [rel])
            outside = self._outside_package(Path(tmp) / "registry", "VALUE = 'pkg'\n")
            (repo / rel / "registry").symlink_to(outside)
            env = _git_env()
            env["PYTHONPATH"] = str(repo / "core")
            out = subprocess.check_output(
                [sys.executable, "-B", "-P", "-c",
                 "from recursion_core.daemon.registry import VALUE; print(VALUE)"],
                env=env, text=True, cwd=repo)
            self.assertEqual(out.strip(), "pkg")
            ok, message = pins.pin_status("RACE_STRESS_TREE_324", repo)
            self.assertFalse(ok, message)
            self.assertIn("registry", message)
            self.assertIn("symlink", message)

    def test_tracked_directory_replaced_by_a_matching_symlink_is_refused(self) -> None:
        rel = "core/recursion_core/routes/breaker.py"
        contract = "core/recursion_core/routes/contract.py"
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            head = _repo(repo, {rel: "v1\n", contract: "c1\n"})
            self._pin("RACE_STRESS_TREE_326", repo, head, [rel, contract])
            copy = Path(tmp) / "copy"
            shutil.copytree(repo / "core/recursion_core/routes", copy)
            pkg = copy / "breaker" / "__init__.py"
            pkg.parent.mkdir()
            pkg.write_text("VALUE = 'pkg'\n")
            routes = repo / "core/recursion_core/routes"
            shutil.rmtree(routes)
            routes.symlink_to(copy)
            work = subprocess.check_output(
                ["git", "hash-object", "--no-filters", "--", rel],
                cwd=repo, env=_git_env(), text=True).strip()
            oid = subprocess.check_output(
                ["git", "rev-parse", f"HEAD:{rel}"], cwd=repo, env=_git_env(), text=True).strip()
            self.assertEqual(work, oid)
            env = _git_env()
            env["PYTHONPATH"] = str(repo / "core")
            out = subprocess.check_output(
                [sys.executable, "-B", "-P", "-c",
                 "from recursion_core.routes.breaker import VALUE; print(VALUE)"],
                env=env, text=True, cwd=repo)
            self.assertEqual(out.strip(), "pkg")
            ok, message = pins.pin_status("RACE_STRESS_TREE_326", repo)
            self.assertFalse(ok, message)
            self.assertIn("symlink", message)
            self.assertIn("not a real directory", message)

    def test_tracked_symlink_matching_head_is_allowed_until_retargeted(self) -> None:
        rel = "core/recursion_core/daemon"
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            head = _repo(repo, {
                f"{rel}/__init__.py": "VALUE = 1\n",
                f"{rel}/registry.py": "VALUE = 1\n",
                "core/readme.txt": "hello\n",
            })
            link = repo / "core" / "note-link"
            link.symlink_to("readme.txt")
            subprocess.check_call(["git", "add", "--", "core/note-link"], cwd=repo, env=_git_env())
            subprocess.check_call(["git", "commit", "-q", "-m", "link"], cwd=repo, env=_git_env())
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, env=_git_env(), text=True).strip()
            self._pin("RACE_STRESS_TREE_324", repo, head, [rel])
            ok, message = pins.pin_status("RACE_STRESS_TREE_324", repo)
            self.assertTrue(ok, message)
            link.unlink()
            link.symlink_to("missing-target")
            ok, message = pins.pin_status("RACE_STRESS_TREE_324", repo)
            self.assertFalse(ok, message)
            self.assertIn("note-link", message)

    def test_path_git_cannot_make_a_dirty_tree_look_pinned(self) -> None:
        rel = "core/recursion_core/routes/breaker.py"
        lie = "0" * 40
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            head = _repo(repo, {rel: "v1\n"})
            self._pin("RACE_STRESS_TREE_326", repo, head, [rel])
            clean = subprocess.check_output(
                ["/usr/bin/git", "-C", str(repo), "hash-object", "--no-filters", "--", rel],
                text=True).strip()
            real_head = subprocess.check_output(
                ["/usr/bin/git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            (repo / rel).write_text("v1\ndirty\n")
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            fake = bindir / "git"
            fake.write_text(
                "#!/bin/sh\n"
                "prev=\n"
                "for arg in \"$@\"; do\n"
                f"  if [ \"$arg\" = \"hash-object\" ]; then printf '%s\\n' '{clean}'; exit 0; fi\n"
                f"  if [ \"$prev\" = \"rev-parse\" ] && [ \"$arg\" = \"HEAD\" ]; then printf '%s\\n' '{lie}'; exit 0; fi\n"
                "  prev=$arg\n"
                "done\n"
                "exec /usr/bin/git \"$@\"\n")
            fake.chmod(0o755)
            old = os.environ.get("PATH", "")
            os.environ["PATH"] = str(bindir) + os.pathsep + old
            try:
                lied = subprocess.check_output(
                    ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
                self.assertEqual(lied, lie)
                info = h.tree_info(str(repo), rel)
                ok, message = pins.pin_status("RACE_STRESS_TREE_326", repo)
            finally:
                os.environ["PATH"] = old
            self.assertEqual(info["tree_head"], real_head)
            self.assertFalse(ok, message)
            self.assertEqual(pins.GIT, "/usr/bin/git")

    def test_skip_worktree_and_assume_unchanged_cannot_hide_edits(self) -> None:
        rel = "core/recursion_core/routes/breaker.py"
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            head = _repo(repo, {rel: "v1\n"})
            self._pin("RACE_STRESS_TREE_326", repo, head, [rel])
            for flag in ("--skip-worktree", "--assume-unchanged"):
                subprocess.check_call(
                    ["git", "update-index", "--no-skip-worktree", "--no-assume-unchanged", "--", rel],
                    cwd=repo, env=_git_env())
                (repo / rel).write_text("v1\n")
                subprocess.check_call(["git", "update-index", flag, "--", rel], cwd=repo, env=_git_env())
                ok, message = pins.pin_status("RACE_STRESS_TREE_326", repo)
                self.assertTrue(ok, message)
                (repo / rel).write_text("v1\nhidden\n")
                status = subprocess.check_output(
                    ["git", "status", "--porcelain", "--", rel], cwd=repo, env=_git_env(), text=True)
                self.assertEqual(status.strip(), "", flag)
                ok, message = pins.pin_status("RACE_STRESS_TREE_326", repo)
                self.assertFalse(ok, message)
                self.assertIn(rel, message)

    def test_staged_blob_is_ignored_when_worktree_bytes_match_head(self) -> None:
        rel = "core/recursion_core/routes/breaker.py"
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            head = _repo(repo, {rel: "v1\n"})
            self._pin("RACE_STRESS_TREE_326", repo, head, [rel])
            original = (repo / rel).read_text()
            (repo / rel).write_text(original + "staged\n")
            subprocess.check_call(["git", "add", "--", rel], cwd=repo, env=_git_env())
            (repo / rel).write_text(original)
            work = subprocess.check_output(
                ["git", "hash-object", "--no-filters", "--", rel], cwd=repo, env=_git_env(), text=True)
            blob = subprocess.check_output(
                ["git", "rev-parse", f"HEAD:{rel}"], cwd=repo, env=_git_env(), text=True)
            self.assertEqual(work.strip(), blob.strip())
            cached = subprocess.check_output(
                ["git", "diff", "--cached", "--name-only", "--", rel], cwd=repo, env=_git_env(), text=True)
            self.assertIn(rel, cached)
            ok, message = pins.pin_status("RACE_STRESS_TREE_326", repo)
            self.assertTrue(ok, message)


class DaemonResourceTests(unittest.TestCase):
    def _mod(self, *, block: threading.Event | None = None, held: threading.Lock | None = None):
        def hold() -> None:
            assert held is not None and block is not None
            with held:
                block.wait()

        class Registry:
            def __init__(self, path):
                Path(path).mkdir(parents=True, exist_ok=True)
            def get(self, rid):
                if block is not None:
                    hold()
                raise KeyError(rid)
            def state(self, *args):
                if block is not None:
                    hold()
            def all(self):
                return []
            def close(self):
                pass

        class Daemon:
            def __init__(self, registry, manager, boot_id=None):
                self.lock = threading.Lock()
                self.registry = registry
            def start(self, rid, argv):
                if block is not None:
                    hold()
                raise RuntimeError("boom")
            def stop(self, rid):
                if block is not None:
                    hold()
                raise RuntimeError("boom")
            def sweep(self):
                if block is not None:
                    hold()
                raise RuntimeError("boom")
            def close(self):
                if held is not None:
                    with held:
                        pass
                self.registry.close()

        class AlreadyRunning(Exception):
            pass

        class UserLock:
            taken = False
            def __init__(self, runtime):
                pass
            def __enter__(self):
                if UserLock.taken:
                    raise AlreadyRunning()
                UserLock.taken = True
                return self
            def __exit__(self, *exc):
                UserLock.taken = False

        mod = argparse.Namespace(
            Registry=Registry, Daemon=Daemon, UserLock=UserLock,
            AlreadyRunning=AlreadyRunning, TERMINAL=frozenset())
        return mod

    def test_iterations_delete_their_temp_directories(self) -> None:
        parent = Path(tempfile.mkdtemp(prefix="rs-parent-"))
        old = tempfile.tempdir
        tempfile.tempdir = str(parent)
        try:
            args = argparse.Namespace(mutant=False, threads=2)
            daemon_324.one_iteration(
                self._mod(), random.Random(1), args, h.Violations(),
                {"runs": 0, "closed_mid_run": 0, "boot_flips": 0, "terminal": 0, "stop_requested": 0})
            daemon_324.lock_iteration(self._mod(), args, h.Violations())
            left = sorted(p.name for p in parent.iterdir())
        finally:
            tempfile.tempdir = old
            shutil.rmtree(parent, ignore_errors=True)
        self.assertEqual(left, [])

    def test_close_does_not_wait_on_a_worker_that_hammer_already_gave_up_on(self) -> None:
        release = threading.Event()
        held = threading.Lock()
        orig = daemon_324.h.hammer

        def quick(workers, timeout=60.0, **kwargs):
            return orig(workers, timeout=0.2, **kwargs)

        daemon_324.h.hammer = quick
        box: dict = {}

        def run() -> None:
            args = argparse.Namespace(mutant=False, threads=1)
            daemon_324.one_iteration(
                self._mod(block=release, held=held),
                argparse.Namespace(random=lambda: 0.99, randrange=lambda n: 1),
                args, h.Violations(),
                {"runs": 0, "closed_mid_run": 0, "boot_flips": 0, "terminal": 0, "stop_requested": 0})
            box["done"] = True

        thread = threading.Thread(target=run)
        thread.start()
        try:
            thread.join(5)
            alive = thread.is_alive()
        finally:
            release.set()
            daemon_324.h.hammer = orig
            thread.join(5)
        self.assertFalse(alive)
        self.assertTrue(box.get("done"))

    def tearDown(self) -> None:
        finish = getattr(daemon_324, "finish_pending", None)
        if finish is not None:
            finish(2)
        if hasattr(daemon_324, "CLEANUP_WAIT"):
            daemon_324.CLEANUP_WAIT = 5.0
        if hasattr(daemon_324, "OBSERVER_JOIN"):
            daemon_324.OBSERVER_JOIN = 30.0
        reset = getattr(daemon_324, "reset_harness_state", None)
        if reset is not None:
            reset()

    def test_hang_aborts_the_run_and_drops_db_fds_after_release(self) -> None:
        # Seed 12: the first worker operation is start, and neither the closer nor the
        # boot flipper is scheduled. manager.start blocks, so hammer reports a hang.
        release = threading.Event()
        registries: list[str] = []
        closed_after_release: list[bool] = []

        class Registry:
            def __init__(self, path):
                registries.append(str(path))
                reg = Path(path)
                reg.mkdir(parents=True, exist_ok=True)
                self.db = sqlite3.connect(reg / "runs.sqlite3", check_same_thread=False)
                self.db.execute("PRAGMA journal_mode=WAL")
                self.db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT)")
                self.db.commit()

            def get(self, rid):
                raise KeyError(rid)

            def state(self, *args):
                return None

            def all(self):
                return []

            def close(self):
                self.db.close()

        class Daemon:
            def __init__(self, registry, manager, boot_id=None):
                self.lock = threading.Lock()
                self.registry = registry
                self.manager = manager

            def start(self, rid, argv):
                with self.lock:
                    self.manager.start(f"recursion-run-{rid}.service", argv)

            def stop(self, rid):
                return None

            def sweep(self):
                return None

            def close(self):
                closed_after_release.append(release.is_set())
                with self.lock:
                    self.registry.close()

        class AlreadyRunning(Exception):
            pass

        class UserLock:
            inits = 0

            def __init__(self, runtime):
                UserLock.inits += 1

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        mod = argparse.Namespace(
            Registry=Registry, Daemon=Daemon, UserLock=UserLock,
            AlreadyRunning=AlreadyRunning, TERMINAL=frozenset())
        orig_start = daemon_324.FakeManager.start
        orig_hammer = daemon_324.h.hammer

        def blocking(self, unit, argv):
            release.wait()

        def quick(workers, timeout=60.0, **kwargs):
            return orig_hammer(workers, timeout=0.2, **kwargs)

        daemon_324.FakeManager.start = blocking
        daemon_324.h.hammer = quick
        if hasattr(daemon_324, "CLEANUP_WAIT"):
            daemon_324.CLEANUP_WAIT = 0.25
        v = h.Violations()
        try:
            leaked_threads, leaked_fds = daemon_324.drive(
                mod, random.Random(12),
                argparse.Namespace(mutant=False, threads=1, iters=2, seed=12),
                v, {"runs": 0, "closed_mid_run": 0, "boot_flips": 0, "terminal": 0, "stop_requested": 0})
            self.assertTrue(any("still running" in m for m in v.first), v.first)
            self.assertEqual(len(registries), 1, registries)
            self.assertEqual(UserLock.inits, 0)
            self.assertTrue(daemon_324.aborted())
            self.assertTrue(any("no further iteration" in m for m in v.first), v.first)
            self.assertGreater(leaked_threads, 0, v.first)
            self.assertGreater(leaked_fds, 0, v.first)
            self.assertEqual(closed_after_release, [])
            self.assertNotEqual(_race324_db_fds(), [])
        finally:
            release.set()
            daemon_324.FakeManager.start = orig_start
            daemon_324.h.hammer = orig_hammer
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and _race324_db_fds():
                gc.collect()
                time.sleep(0.05)
        self.assertEqual(_race324_db_fds(), [])
        self.assertEqual(closed_after_release, [True])

    def test_blocked_userlock_is_not_torn_down(self) -> None:
        release = threading.Event()
        saw: dict[str, bool] = {}

        class AlreadyRunning(Exception):
            pass

        class UserLock:
            def __init__(self, runtime):
                self.runtime = Path(runtime)

            def __enter__(self):
                release.wait(30)
                saw["exists"] = self.runtime.is_dir()
                return self

            def __exit__(self, *exc):
                return False

        mod = argparse.Namespace(UserLock=UserLock, AlreadyRunning=AlreadyRunning)
        orig = daemon_324.h.hammer

        def quick(workers, timeout=60.0, **kwargs):
            return orig(workers, timeout=0.3, **kwargs)

        parent = Path(tempfile.mkdtemp(prefix="rs-lock-"))
        old = tempfile.tempdir
        tempfile.tempdir = str(parent)
        daemon_324.h.hammer = quick
        daemon_324.reset_harness_state()
        try:
            daemon_324.lock_iteration(mod, argparse.Namespace(threads=1), h.Violations())
            left = sorted(p.name for p in parent.iterdir())
            self.assertTrue(left, "runtime directory removed while UserLock.__enter__ is blocked")
            self.assertTrue(daemon_324.aborted())
            alive, _fds = daemon_324.finish_pending(0.05)
            self.assertGreater(alive, 0)
        finally:
            release.set()
            daemon_324.h.hammer = orig
            daemon_324.finish_pending(2)
            tempfile.tempdir = old
            shutil.rmtree(parent, ignore_errors=True)
        self.assertTrue(saw.get("exists"), saw)

    def test_stuck_observer_is_not_torn_down(self) -> None:
        release = threading.Event()
        saw: dict[str, bool] = {}

        class Registry:
            def __init__(self, path):
                self.path = Path(path)
                self.path.mkdir(parents=True, exist_ok=True)

            def get(self, rid):
                raise KeyError(rid)

            def state(self, *args):
                return None

            def all(self):
                release.wait(30)
                saw["exists"] = self.path.is_dir()
                return []

            def close(self):
                pass

        class Daemon:
            def __init__(self, registry, manager, boot_id=None):
                self.lock = threading.Lock()
                self.registry = registry

            def start(self, rid, argv):
                return None

            def stop(self, rid):
                return None

            def sweep(self):
                return None

            def close(self):
                self.registry.close()

        class AlreadyRunning(Exception):
            pass

        class UserLock:
            def __init__(self, runtime):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        mod = argparse.Namespace(
            Registry=Registry, Daemon=Daemon, UserLock=UserLock,
            AlreadyRunning=AlreadyRunning, TERMINAL=frozenset())
        orig_join = threading.Thread.join

        def quick_join(self, timeout=None):
            if timeout == 30 or timeout == 30.0:
                timeout = 0.25
            return orig_join(self, timeout)

        parent = Path(tempfile.mkdtemp(prefix="rs-obs-"))
        old_temp = tempfile.tempdir
        tempfile.tempdir = str(parent)
        threading.Thread.join = quick_join
        if hasattr(daemon_324, "OBSERVER_JOIN"):
            daemon_324.OBSERVER_JOIN = 0.25
        daemon_324.reset_harness_state()
        v = h.Violations()
        try:
            daemon_324.one_iteration(
                mod, random.Random(1), argparse.Namespace(mutant=False, threads=1), v,
                {"runs": 0, "closed_mid_run": 0, "boot_flips": 0, "terminal": 0, "stop_requested": 0})
            left = sorted(p.name for p in parent.iterdir())
            self.assertTrue(left, "temp directory removed while the observer is still inside it")
            self.assertTrue(daemon_324.aborted())
            self.assertTrue(any("observer did not finish" in m for m in v.first), v.first)
            alive, _fds = daemon_324.finish_pending(0.05)
            self.assertGreater(alive, 0)
        finally:
            release.set()
            threading.Thread.join = orig_join
            if hasattr(daemon_324, "OBSERVER_JOIN"):
                daemon_324.OBSERVER_JOIN = 30.0
            daemon_324.finish_pending(2)
            tempfile.tempdir = old_temp
            shutil.rmtree(parent, ignore_errors=True)
        self.assertTrue(saw.get("exists"), saw)

    def test_drive_stops_after_a_hung_lock(self) -> None:
        release = threading.Event()
        registries: list[str] = []

        class Registry:
            def __init__(self, path):
                registries.append(str(path))
                Path(path).mkdir(parents=True, exist_ok=True)

            def get(self, rid):
                raise KeyError(rid)

            def state(self, *args):
                return None

            def all(self):
                return []

            def close(self):
                pass

        class Daemon:
            def __init__(self, registry, manager, boot_id=None):
                self.lock = threading.Lock()
                self.registry = registry

            def start(self, rid, argv):
                raise RuntimeError("boom")

            def stop(self, rid):
                raise RuntimeError("boom")

            def sweep(self):
                raise RuntimeError("boom")

            def close(self):
                self.registry.close()

        class AlreadyRunning(Exception):
            pass

        class UserLock:
            def __init__(self, runtime):
                self.runtime = Path(runtime)

            def __enter__(self):
                release.wait(30)
                return self

            def __exit__(self, *exc):
                return False

        mod = argparse.Namespace(
            Registry=Registry, Daemon=Daemon, UserLock=UserLock,
            AlreadyRunning=AlreadyRunning, TERMINAL=frozenset())
        orig = daemon_324.h.hammer

        def quick(workers, timeout=60.0, **kwargs):
            return orig(workers, timeout=0.4, **kwargs)

        daemon_324.h.hammer = quick
        daemon_324.CLEANUP_WAIT = 0.2
        daemon_324.reset_harness_state()
        try:
            leaked_threads, _fds = daemon_324.drive(
                mod, random.Random(1),
                argparse.Namespace(mutant=False, threads=1, iters=2, seed=1),
                h.Violations(),
                {"runs": 0, "closed_mid_run": 0, "boot_flips": 0, "terminal": 0, "stop_requested": 0})
            # One finished iteration opens the registry and reopens it; a second must not start.
            self.assertEqual(len(registries), 2, registries)
            self.assertTrue(daemon_324.aborted())
            self.assertGreater(leaked_threads, 0)
        finally:
            release.set()
            daemon_324.h.hammer = orig
            daemon_324.CLEANUP_WAIT = 5.0
            daemon_324.finish_pending(2)


def _race324_db_fds() -> list[str]:
    found = []
    fd_dir = "/proc/self/fd"
    if not os.path.isdir(fd_dir):
        return found
    for name in os.listdir(fd_dir):
        try:
            target = os.readlink(os.path.join(fd_dir, name))
        except OSError:
            continue
        if "race324-" in target and "runs.sqlite3" in target:
            found.append(target)
    return found


class FlakeRunnerTests(unittest.TestCase):
    def test_failed_subtest_is_attributed_to_its_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "statuses.json"
            rc = flake_runner.child("recursion_core.race_stress.subtest_sample", str(out))
            statuses = json.loads(out.read_text())
        self.assertEqual(rc, 1)
        failed = [k for k in statuses if k.endswith(".test_one")]
        self.assertEqual(failed, ["recursion_core.race_stress.subtest_sample.Sample.test_one"])
        self.assertEqual(statuses[failed[0]], "fail")
        ok = "recursion_core.race_stress.subtest_sample.Sample.test_ok"
        self.assertEqual(statuses[ok], "ok")

    def test_summary_does_not_blame_a_phantom_process_for_a_subtest(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = flake_runner.main(["--allow-gil", "--runs", "1", "--seed", "1",
                                    "--pattern", "recursion_core.race_stress.subtest_sample"])
        self.assertEqual(rc, 1)
        text = buf.getvalue()
        summary = json.loads(text[text.rfind("\n{"):])
        names = set(summary["steady_failures"]) | set(summary["flaky"])
        self.assertNotIn("unittest#process", names)
        self.assertTrue(any(name.endswith(".test_one") for name in summary["steady_failures"]), summary)

    def test_timeout_tail_decodes_child_output(self) -> None:
        code = "import sys,time; print('out-line', flush=True); print('err-line', file=sys.stderr, flush=True); time.sleep(5)"
        rc, text, _secs = flake_runner.run_once([sys.executable, "-c", code], os.environ.copy(), 0.4)
        self.assertEqual(rc, -999)
        self.assertIn("out-line", text)
        self.assertIn("err-line", text)
        self.assertNotIn("b'", text)


class DriverExitTests(unittest.TestCase):
    def test_routes_driver_exits_nonzero_when_it_records_a_violation(self) -> None:
        self._assert_nonzero("breaker_326.py", {
            "core/recursion_core/__init__.py": "",
            "core/recursion_core/routes/__init__.py": "",
            "core/recursion_core/routes/contract.py": "PLAN_LIMIT='PLAN_LIMIT'\nSHORT_FAULT='SHORT_FAULT'\n",
            "core/recursion_core/routes/breaker.py": ROUTES_FAKE,
        })

    def test_plugin_driver_exits_nonzero_when_it_records_a_violation(self) -> None:
        self._assert_nonzero("breaker_328.py", {
            "core/recursion_core/__init__.py": "",
            "core/recursion_core/plugin_security/__init__.py": "",
            "core/recursion_core/plugin_security/errors.py": PLUGIN_ERRORS,
            "core/recursion_core/plugin_security/breaker.py": PLUGIN_FAKE,
        })

    def test_daemon_driver_exits_nonzero_when_it_records_a_violation(self) -> None:
        self._assert_nonzero("daemon_324.py", {
            "core/recursion_core/__init__.py": "",
            "core/recursion_core/daemon/__init__.py": DAEMON_FAKE,
        })

    def _assert_nonzero(self, script: str, files: dict[str, str]) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, files)
            proc = subprocess.run(
                [sys.executable, "-B", str(DRIVERS / script), "--tree", str(root),
                 "--iters", "1", "--threads", "1", "--seed", "1"],
                capture_output=True, text=True, timeout=60)
            payload = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertGreater(payload["violation_count"], 0, payload)
            self.assertNotEqual(proc.returncode, 0, payload)
            from recursion_core.race_stress.tests.test_race_stress import run_driver
            result = run_driver(script, str(root), iters=1)
            self.assertGreater(result["violation_count"], 0)

    def test_driver_rejects_a_loaded_toplevel_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, {
                "core/recursion_core/__init__.py": "",
                "core/recursion_core/daemon/__init__.py": "import dataclasses\n" + DAEMON_FAKE,
                "core/dataclasses.py": "SHADOW = True\n",
            })
            proc = subprocess.run(
                [sys.executable, "-B", str(DRIVERS / "daemon_324.py"), "--tree", str(root),
                 "--iters", "1", "--threads", "1", "--seed", "1"],
                capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("shadows the import", proc.stderr)
        self.assertNotIn("violation_count", proc.stdout)

    def test_driver_rejects_a_symlinked_stdlib_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tree"
            outside = Path(tmp) / "dataclasses"
            outside.mkdir()
            (outside / "__init__.py").write_text(
                "def dataclass(cls=None, **kwargs):\n"
                "    return (lambda c: c) if cls is None else cls\n"
                "SHADOW = True\n")
            _write(root, {
                "core/recursion_core/__init__.py": "",
                "core/recursion_core/daemon/__init__.py":
                    "from dataclasses import dataclass\n" + DAEMON_FAKE,
            })
            (root / "core" / "dataclasses").symlink_to(outside)
            proc = subprocess.run(
                [sys.executable, "-B", str(DRIVERS / "daemon_324.py"), "--tree", str(root),
                 "--iters", "1", "--threads", "1", "--seed", "1"],
                capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("shadows the import", proc.stderr)
        self.assertIn("dataclasses", proc.stderr)
        self.assertNotIn("violation_count", proc.stdout)

    def test_run_driver_ignores_pythonpath_sitecustomize(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tree"
            _write(root, {
                "core/recursion_core/__init__.py": "",
                "core/recursion_core/routes/__init__.py": "",
                "core/recursion_core/routes/contract.py":
                    "PLAN_LIMIT='PLAN_LIMIT'\nSHORT_FAULT='SHORT_FAULT'\n",
                "core/recursion_core/routes/breaker.py": ROUTES_FAKE,
            })
            real = root / "core/recursion_core/routes/breaker.py"
            site = Path(tmp) / "site"
            site.mkdir()
            (site / "sitecustomize.py").write_text(
                "import sys\n"
                "from types import SimpleNamespace\n"
                "tree = sys.argv[sys.argv.index('--tree') + 1]\n"
                "real = tree + '/core/recursion_core/routes/breaker.py'\n"
                "mod = type(sys)('recursion_core.routes.breaker')\n"
                "mod.__file__ = real\n"
                "mod.__spec__ = SimpleNamespace(origin=real, name=mod.__name__,\n"
                "    submodule_search_locations=None)\n"
                "class ConnectionBreaker:\n"
                "    def __init__(self, *args, **kwargs):\n"
                "        raise RuntimeError('sitecustomize-shadow')\n"
                "mod.ConnectionBreaker = ConnectionBreaker\n"
                "sys.modules['recursion_core.routes.breaker'] = mod\n")
            self.assertTrue(real.is_file())
            from recursion_core.race_stress.tests.test_race_stress import run_driver
            old = os.environ.get("PYTHONPATH")
            os.environ["PYTHONPATH"] = str(site) + (os.pathsep + old if old else "")
            try:
                try:
                    result = run_driver("breaker_326.py", str(root), iters=1)
                except AssertionError as exc:
                    self.fail(f"pinned driver did not run; startup path leaked: {exc}")
            finally:
                if old is None:
                    os.environ.pop("PYTHONPATH", None)
                else:
                    os.environ["PYTHONPATH"] = old
        self.assertGreater(result["violation_count"], 0)
        self.assertNotIn("sitecustomize-shadow", str(result))

    def test_driver_rejects_a_loaded_package_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, {
                "core/recursion_core/__init__.py": "",
                "core/recursion_core/routes/__init__.py": "",
                "core/recursion_core/routes/breaker.py": "VALUE = 'file'\n",
                "core/recursion_core/routes/breaker/__init__.py": "VALUE = 'pkg'\n",
                "core/recursion_core/routes/contract.py": "PLAN_LIMIT='PLAN_LIMIT'\nSHORT_FAULT='SHORT_FAULT'\n",
            })
            proc = subprocess.run(
                [sys.executable, "-B", str(DRIVERS / "breaker_326.py"), "--tree", str(root),
                 "--iters", "1", "--threads", "1", "--seed", "1"],
                capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("not pinned source", proc.stderr)
        self.assertNotIn("violation_count", proc.stdout)

    def test_loaded_origin_rejects_package_shadow_and_bytecode_cache(self) -> None:
        from recursion_core.race_stress.tests.interleave_support import origin_error
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            py = root / "breaker.py"
            py.write_text("VALUE = 'file'\n")
            init = root / "breaker" / "__init__.py"
            init.parent.mkdir()
            init.write_text("VALUE = 'pkg'\n")
            pkg = argparse.Namespace(__name__="breaker", __file__=str(init), __cached__=None)
            self.assertIn("not pinned source", origin_error(pkg, root, ["breaker"]))
            source = argparse.Namespace(__name__="breaker", __file__=str(py), __cached__=None)
            # The sibling package is still on disk; the loaded file is the .py only when
            # the package dir is absent. Remove it and require the source path.
            shutil.rmtree(init.parent)
            self.assertIsNone(origin_error(source, root, ["breaker"]))
            cache = Path(importlib.util.cache_from_source(str(py)))
            cache.parent.mkdir()
            cache.write_bytes(b"pyc")
            cached = argparse.Namespace(__name__="breaker", __file__=str(py), __cached__=str(cache))
            self.assertIn("bytecode cache", origin_error(cached, root, ["breaker"]))


class LeaseAndDeadlineTests(unittest.TestCase):
    def test_replay_catches_a_lease_that_moved_to_another_connection(self) -> None:
        class DB:
            def execute(self, *args):
                return self
            def fetchall(self):
                return []

        class Breaker:
            n = 0
            def __init__(self, path, clock=None):
                Breaker.n += 1
                self.instance = Breaker.n
                self._lock = threading.Lock()
                self._db = DB()
                self._leases: dict = {}
            def begin_probe(self, conn):
                with self._lock:
                    lease = f"L{self.instance}"
                    key = conn if self.instance == 1 else "other-conn"
                    self._leases = {key: (lease, 5.0)}
                    return lease
            def bind_run(self, *args):
                with self._lock:
                    return None
            def report(self, *args):
                with self._lock:
                    return False
            def fault_run(self, *args):
                with self._lock:
                    return None
            def reset_after_probe(self, *args):
                with self._lock:
                    return False
            def check(self, *args):
                with self._lock:
                    return {}
            def tripped(self, conn):
                with self._lock:
                    return False
            def close(self):
                pass

        class Refused(Exception):
            def __init__(self, code):
                super().__init__(code)
                self.code = code

        class Fixed(random.Random):
            def random(self):
                return 0.40  # begin_probe band
            def choice(self, seq):
                return seq[0]
            def randrange(self, n):
                return 0

        mod = argparse.Namespace(
            create_breaker_store=lambda path: None, ConnectionBreaker=Breaker,
            _NOT_ENGINE_FAULTS=frozenset(), TYPED_LIMITS=frozenset({"plan_limit"}))
        orig = breaker_328.random.Random
        breaker_328.random.Random = Fixed
        try:
            v = h.Violations()
            with tempfile.TemporaryDirectory() as tmp:
                breaker_328.linearizability(
                    mod, Refused, random.Random(1),
                    argparse.Namespace(mutant=False, threads=1), v,
                    {"ops": 0, "leases": 0, "leased_resets": 0}, tmp)
        finally:
            breaker_328.random.Random = orig
        self.assertTrue(any("final lease state differs" in m for m in v.first), v.first)

    def test_a_deadline_lease_still_blocks_a_second_probe_after_probe_lease_s(self) -> None:
        class C:
            PLAN_LIMIT = "PLAN_LIMIT"
            SHORT_FAULT = "SHORT_FAULT"

        class Breaker:
            def __init__(self, clock, **params):
                self._clock = clock
                self._lock = threading.Lock()
                self._keys: dict = {}
                self._short: dict = {}
                self._lease_s = params["probe_lease_s"]
                self._grace = params.get("probe_grace_s", 30.0)
                self._n = 0
                self._live: list = []
            def _mint(self, connection, now, length):
                self._n += 1
                token = f"probe-{self._n}"
                self._live.append((connection, now + length, now))
                self._keys[connection] = {"probe": token, "state": "half_open"}
                return token
            def admit(self, connection, model, tenant, deadline_s=None):
                with self._lock:
                    now = self._clock()
                    length = self._lease_s if deadline_s is None else float(deadline_s) + self._grace
                    live = [p for p in self._live if p[0] == connection and now < p[1]]
                    # Second probe only once probe_lease_s has elapsed, while deadline+grace
                    # is still running. A checker that stores now+probe_lease_s misses it.
                    if deadline_s is not None and live and now >= live[0][2] + self._lease_s:
                        return {"admitted": True, "code": None, "probe": self._mint(connection, now, length),
                                "retry_at": None, "key": connection}
                    if live:
                        return {"admitted": False, "code": "probe_in_flight", "probe": None,
                                "retry_at": live[0][1], "key": connection}
                    if deadline_s is None:
                        return {"admitted": True, "code": None, "probe": None, "retry_at": None, "key": None}
                    return {"admitted": True, "code": None, "probe": self._mint(connection, now, length),
                            "retry_at": None, "key": connection}
            def record(self, *args, **kwargs):
                with self._lock:
                    self._clock()
                    return "none"
            def reset(self, *args, **kwargs):
                with self._lock:
                    self._keys.clear()
                    self._live.clear()
            def state(self, *args, **kwargs):
                with self._lock:
                    self._clock()
                    return "closed"
            @staticmethod
            def model_key(connection, model):
                return f"{connection}#model:{model}"

        class Fixed(random.Random):
            def __init__(self, seed):
                super().__init__(seed)
                self.n = 0
            def random(self):
                self.n += 1
                return (0.0, 0.9, 0.99)[(self.n - 1) % 3]
            def choice(self, seq):
                return seq[-1]
            def uniform(self, a, b):
                return b

        orig = breaker_326.random.Random
        breaker_326.random.Random = Fixed
        try:
            v = h.Violations()
            breaker_326.one_iteration(
                argparse.Namespace(ConnectionBreaker=Breaker), C(), random.Random(1),
                argparse.Namespace(mutant=False, threads=1), v,
                {"ops": 0, "probes": 0, "opened": 0, "refused": 0})
        finally:
            breaker_326.random.Random = orig
        self.assertTrue(any("second live probe" in m for m in v.first), v.first)


class RunThreadsTests(unittest.TestCase):
    def test_timeout_is_one_budget_for_the_whole_batch(self) -> None:
        from recursion_core.race_stress.tests.interleave_support import run_threads
        release = threading.Event()
        started = time.monotonic()
        try:
            errors = run_threads([lambda: release.wait(30) for _ in range(4)], timeout=0.25)
        finally:
            release.set()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.7, f"waited {elapsed:.2f}s for a 0.25s budget")
        self.assertTrue(any(isinstance(err, TimeoutError) for err in errors), errors)


ROUTES_FAKE = '''
import threading
class ConnectionBreaker:
    def __init__(self, clock, **kwargs):
        self._clock = clock
        self._lock = threading.Lock()
        self._keys = {}
        self._short = {}
    def admit(self, *args, **kwargs):
        raise RuntimeError("boom")
    def record(self, *args, **kwargs):
        with self._lock:
            self._clock()
            return "none"
    def reset(self, *args, **kwargs):
        with self._lock:
            return None
    def state(self, *args, **kwargs):
        with self._lock:
            self._clock()
            return "closed"
    @staticmethod
    def model_key(connection, model):
        return f"{connection}#model:{model}"
'''

PLUGIN_ERRORS = '''
class Refused(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code
'''

PLUGIN_FAKE = '''
import threading
_NOT_ENGINE_FAULTS = frozenset()
TYPED_LIMITS = frozenset({"plan_limit", "rate_limit", "usage_window_exhausted"})
def create_breaker_store(path):
    return None
class ConnectionBreaker:
    def __init__(self, path, clock=None):
        self._lock = threading.Lock()
        self._leases = {}
        self._db = self
    def execute(self, *args):
        return self
    def fetchall(self):
        return []
    def bind_run(self, *args):
        return None
    def report(self, *args):
        return False
    def fault_run(self, *args):
        return None
    def begin_probe(self, *args):
        return "lease"
    def reset_after_probe(self, *args):
        return False
    def check(self, *args):
        return {}
    def tripped(self, *args):
        return False
    def close(self):
        return None
'''

DAEMON_FAKE = '''
import threading
TERMINAL = frozenset()
class AlreadyRunning(Exception):
    pass
class Registry:
    def __init__(self, path):
        from pathlib import Path
        Path(path).mkdir(parents=True, exist_ok=True)
    def get(self, rid):
        raise RuntimeError("boom")
    def state(self, *args):
        raise RuntimeError("boom")
    def all(self):
        return []
    def close(self):
        pass
class Daemon:
    def __init__(self, registry, manager, boot_id=None):
        self.lock = threading.Lock()
        self.registry = registry
    def start(self, rid, argv):
        raise RuntimeError("boom")
    def stop(self, rid):
        raise RuntimeError("boom")
    def sweep(self):
        raise RuntimeError("boom")
    def close(self):
        self.registry.close()
class UserLock:
    taken = False
    def __init__(self, runtime):
        pass
    def __enter__(self):
        if UserLock.taken:
            raise AlreadyRunning()
        UserLock.taken = True
        return self
    def __exit__(self, *exc):
        UserLock.taken = False
'''


if __name__ == "__main__":
    unittest.main()
