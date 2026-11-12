"""Repros for defects in the CSL-801 race_stress harness.

Each test fails on the unfixed harness and passes once that defect is fixed.
No measured tree and no network: fakes stand in for the stacked heads.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import random
import shutil
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
        self._saved = dict(pins.PINS["RACE_STRESS_TREE_326"])
        self._saved_objects = dict(self._saved["objects"])

    def tearDown(self) -> None:
        pins.PINS["RACE_STRESS_TREE_326"] = {
            "pr": self._saved["pr"], "head": self._saved["head"], "objects": self._saved_objects}

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

        def quick(workers, timeout=60.0):
            return orig(workers, timeout=0.2)

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
