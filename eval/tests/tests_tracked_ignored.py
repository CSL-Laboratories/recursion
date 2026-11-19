"""A file the source repository tracks although its own .gitignore matches it.

The GitHub Codex reviewer's finding on #381 at 3511f47f (P2, eval/gitutil.py line 65): such a file is
in the source commit's tree (it was added with ``git add --force``, or tracked before the rule was
written), but a plain ``git add -A`` in a task's fresh copy leaves it out. The start tree then has another
id than the source commit's tree, ``validate()`` refuses the task for a start-tree mismatch before its
reference solution is checked, and the runner refuses every attempt the same way. The same rebuild pins a
dev task's commit tree in eval/dev.py.

Every repository here is a made-up one in a scratch folder outside the repository.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from eval import arms, dev, gitutil, harvest, heldout, runner, tasks, validate
from eval.tests import outside_folder, scratch_bases
from eval.tests.tests_harvest import Repo

TEST_COMMAND = ["{python}", "-B", "-m", "unittest", "{hidden_modules}"]
OPS = "def add(a, b):\n    return a + b\n"
OPS_MUL = OPS + "\n\ndef mul(a, b):\n    return a * b\n"
TEST_MUL = """import unittest
from pathlib import Path

from calc import ops


class T(unittest.TestCase):
    def test_mul(self):
        self.assertEqual(ops.mul(3, 4), 12)

    def test_the_fixtures_are_there(self):
        self.assertEqual(Path("fixtures/table.txt").read_text(), "one\\n")
        self.assertEqual(Path("notes.log").read_text(), "two\\n")
        self.assertEqual(Path("fixtures/added.txt").read_text(), "three\\n")
"""
# Tracked and ignored at the start: one by a folder rule, one by a file rule.
START = {".gitignore": "*.log\nfixtures/\n", "calc/__init__.py": "", "calc/ops.py": OPS, "tests/__init__.py": "",
         "fixtures/table.txt": "one\n", "notes.log": "one\n"}
IGNORED = ["fixtures/table.txt", "notes.log"]


def source_history(path: Path) -> dict:
    repo = Repo(path)
    repo.write(START)
    repo.git("add", "-A")
    repo.git("add", "--force", "--", *IGNORED)
    repo.git("commit", "-q", "-m", "initial")
    shas = {"c0": repo.git("rev-parse", "HEAD")}
    # The commit changes one tracked, ignored file and adds another.
    repo.write({"calc/ops.py": OPS_MUL, "tests/test_mul.py": TEST_MUL, "notes.log": "two\n",
                "fixtures/added.txt": "three\n"})
    repo.git("add", "-A")
    repo.git("add", "--force", "--", "fixtures/added.txt")
    repo.git("commit", "-q", "-m", "Add mul", date="2026-03-01T00:00:00+0000")
    shas["mul"] = repo.git("rev-parse", "HEAD")
    for name in ("c0", "mul"):
        shas[name + "_tree"] = repo.git("rev-parse", f"{shas[name]}^{{tree}}")
    at_start = repo.git("ls-tree", "-r", "--name-only", shas["c0"]).split("\n")
    at_commit = repo.git("ls-tree", "-r", "--name-only", shas["mul"]).split("\n")
    # The fixture is really tracked at the source (the precondition of the whole file).
    assert all(p in at_start for p in IGNORED) and "fixtures/added.txt" in at_commit, (at_start, at_commit)
    return shas


def write_files(work: Path, files: dict) -> None:
    for name, (data, mode) in files.items():
        path = work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)


def every_file_tree(work: Path) -> str:
    """The tree of every file under ``work``, built by ``git update-index``, which reads no .gitignore:
    an oracle that shares no code with eval/gitutil.py."""
    gitutil.git(["init", "-q", "--template=", "-b", "main"], work)
    names = sorted(p.relative_to(work).as_posix() for p in work.rglob("*") if p.is_file() and ".git" not in p.parts)
    gitutil.git(["update-index", "--add", "-z", "--stdin"], work, input="\0".join(names).encode() + b"\0")
    return gitutil.git(["write-tree"], work).decode().strip()


def tracked(work: Path, treeish: str) -> list:
    return gitutil.git(["ls-tree", "-r", "--name-only", treeish], work).decode().split()


class _Source(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._outside = outside_folder()
        cls.folder = cls._outside.__enter__()
        cls.upstream = cls.folder / "upstream"
        cls.shas = source_history(cls.upstream)

    @classmethod
    def tearDownClass(cls):
        cls._outside.__exit__(None, None, None)

    def fresh_copy(self, name: str) -> Path:
        work = self.folder / name
        work.mkdir()
        write_files(work, harvest.tree_files(self.upstream, self.shas["c0"]))
        return work


class IndexRebuildTests(_Source):
    def test_snapshot_keeps_a_tracked_ignored_file(self):
        work = self.fresh_copy("snapshot")
        _, tree = gitutil.snapshot(work)
        self.assertEqual([p for p in IGNORED if p not in tracked(work, "HEAD")], [])
        self.assertEqual(tree, self.shas["c0_tree"])

    def test_tree_of_files_keeps_a_tracked_ignored_file(self):
        work = self.fresh_copy("tree-of-files")
        tree = gitutil.tree_of_files(work)
        self.assertEqual([p for p in IGNORED if p not in tracked(work, tree)], [])
        self.assertEqual(tree, self.shas["c0_tree"])

    def test_the_diff_keeps_tracked_paths_and_adds_nothing_never_tracked(self):
        work = self.fresh_copy("diff")
        base, _ = gitutil.snapshot(work)
        (work / "notes.log").write_text("edited\n", encoding="utf-8")       # tracked, ignored: in the diff
        (work / "fixtures" / "table.txt").unlink()                          # tracked, ignored: deleted
        (work / "engine.log").write_text("by-product\n", encoding="utf-8")  # never tracked, ignored: not
        (work / "fixtures" / "cache.bin").write_bytes(b"x")                 # never tracked, ignored folder: not
        (work / "calc" / "new.py").write_text("X = 1\n", encoding="utf-8")  # never tracked, not ignored: in
        patch, paths = gitutil.diff_from(work, base)
        self.assertEqual(paths, ["calc/new.py", "fixtures/table.txt", "notes.log"])
        self.assertNotIn(b"engine.log", patch)
        self.assertNotIn(b"cache.bin", patch)


class HarvestedTaskTests(_Source):
    """The reviewer's reproduction: a harvested task whose start tree holds tracked, ignored fixtures."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._bases = scratch_bases()
        cls.tmp, cls.xdg = cls._bases.__enter__()
        cls.repos = cls.folder / "repos.json"
        cls.repos.write_text(json.dumps({"schema": harvest.REPOS_SCHEMA, "repos": [
            {"name": "calc", "path": str(cls.upstream), "branch": "main", "test_globs": ["tests/**/test_*.py"],
             "test_command": TEST_COMMAND, "timeout_s": 600, "grading_timeout_s": 120}]}), encoding="utf-8")
        cls.out = cls.folder / "held-out"
        cls.report = harvest.harvest(cls.repos, "2026-02-01", cls.out, suite_id="ignored-h", tmp_base=cls.tmp,
                                     xdg_base=cls.xdg, log=lambda _m: None)

    @classmethod
    def tearDownClass(cls):
        cls._bases.__exit__(None, None, None)
        super().tearDownClass()

    def setUp(self):
        heldout._declared = None
        self.addCleanup(setattr, heldout, "_declared", None)

    def test_the_task_is_kept(self):
        self.assertEqual(self.report["rejected"], [])
        manifest = json.loads((self.out / "suite.json").read_text(encoding="utf-8"))
        self.assertEqual([(t["commit"], t["parent_tree"], t["commit_tree"]) for t in manifest["tasks"]],
                         [(self.shas["mul"], self.shas["c0_tree"], self.shas["mul_tree"])])

    def test_validate_reaches_the_reference_solution(self):
        # Built apart from the harvest above, so that this test shows validate()'s own answer.
        repo_cfg = json.loads(self.repos.read_text(encoding="utf-8"))["repos"][0]
        candidate, reason = harvest.build_candidate(repo_cfg, self.shas["mul"], {})
        self.assertIsNone(reason)
        folder = self.folder / "validate-alone"
        harvest.write_task(folder, candidate)
        task = tasks.load_dir(folder)
        task.parent_tree = candidate["parent_tree"]
        checked = validate.validate(task, reruns=1, tmp_base=self.tmp, xdg_base=self.xdg)
        self.assertEqual((checked.ok, checked.reason), (True, None))
        self.assertEqual(checked.runs, [("parent", 1, False), ("commit", 0, False)])

    def test_the_runner_grades_it(self):
        suite = heldout.open_suite(self.out, purpose="score")
        outcome = runner.run(suite, [arms.DirectArm("standin")], k=1, label="shakedown",
                             out_dir=self.folder / "records", tmp_base=self.tmp, xdg_base=self.xdg,
                             log=lambda _m: None)
        record = json.loads(Path(outcome.records["direct"]).read_text(encoding="utf-8"))
        self.assertEqual([(t["grade"]["verdict"], t["error"]["class"]) for t in record["trials"]],
                         [("resolved", None)])


DEV_TASK = """A dev task whose start tree tracks a file its .gitignore matches, and whose solution adds one.
-- task.json --
{"id": "ignored-dev", "cluster": "tiny", "created_at": null,
 "test_command": ["{python}", "-B", "-m", "unittest", "{hidden_modules}"],
 "timeout_s": 600, "grading_timeout_s": 120}
-- prompt.md --
Make `answer()` in `tiny/core.py` return the number in `answer.log`.
-- start/.gitignore --
*.log
-- start/answer.log --
41
-- start/tiny/__init__.py --
-- start/tiny/core.py --
def answer():
    return 0
-- hidden/test_ignored_dev.py --
import unittest
from pathlib import Path

from tiny.core import answer


class AnswerTests(unittest.TestCase):
    def test_answer(self):
        self.assertEqual(answer(), int(Path("answer.log").read_text()))
        self.assertTrue(Path("added.log").is_file())
-- solution/answer.log --
42
-- solution/added.log --
new in the solution
-- solution/tiny/core.py --
def answer():
    return 42
"""


class DevTaskTests(unittest.TestCase):
    """eval/dev.py pins a dev task's commit tree with the same kind of index rebuild: a file the reference
    solution adds that the start tree's .gitignore matches must be in it, or validate() refuses the commit
    side for a commit-tree mismatch."""

    def test_pinned_trees_hold_the_tracked_ignored_files_and_validate(self):
        with outside_folder() as folder, scratch_bases() as (tmp, xdg):
            path = folder / "ignored-dev.txtar"
            path.write_text(DEV_TASK, encoding="utf-8")
            task = tasks.load_txtar(path)
            _, tree, _, commit_tree = dev.commits_for(task, tmp, xdg, with_tree=True)
            start = folder / "start"
            start.mkdir()
            task.materialize_start(start)
            self.assertEqual(tree, every_file_tree(start))
            self.assertIn("answer.log", tracked(start, tree))
            commit = folder / "commit"
            commit.mkdir()
            task.materialize_start(commit)
            task.apply_solution(commit)
            task.write_hidden(commit)
            self.assertEqual(commit_tree, every_file_tree(commit))
            self.assertEqual([p for p in ("added.log", "answer.log") if p not in tracked(commit, commit_tree)], [])
            task.parent_tree, task.commit_tree = tree, commit_tree
            checked = validate.validate(task, reruns=1, tmp_base=tmp, xdg_base=xdg)
            self.assertEqual((checked.ok, checked.reason), (True, None))


if __name__ == "__main__":
    unittest.main()
