"""The distribution: ``pyproject.toml`` as written, and (with a uv binary) the built wheel installed for real.

The build test runs when ``RECURSION_PACKAGING_UV`` names a uv binary; hosted CI has none and
skips it. It builds outside the checkout with ``uv build --offline``, so nothing is fetched and the
tree is not touched, then checks both archives against what must and must not ship, installs the
wheel into a fresh virtual environment with no index, and runs ``recursiond`` and the unit's own
command line from ``/`` with no ``PYTHONPATH``; ``recursion-control`` answers its usage there too.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import tomllib
import unittest
import zipfile

import recursion_core
from recursion_core import _entrypoints

PACKAGE_DIR = os.path.dirname(os.path.abspath(recursion_core.__file__))
REPO = os.path.dirname(os.path.dirname(PACKAGE_DIR))
UV = os.environ.get("RECURSION_PACKAGING_UV", "")
FREE_THREADED_314 = (sys.implementation.name == "cpython" and sys.version_info[:2] == (3, 14)
                     and bool(sysconfig.get_config_var("Py_GIL_DISABLED")))

MUST_SHIP = {
    "recursion_core/__init__.py",
    "recursion_core/_entrypoints.py",
    "recursion_core/control/commands/setup.py",
    "recursion_core/daemon/__main__.py",
    "recursion_core/daemon/engine_exec.py",
    "recursion_core/daemon_install/unit.py",
    "recursion_core/plugin_security/protocol.json",
    "recursion_core/plugin_security/stores.py",
}


def _pyproject():
    with open(os.path.join(REPO, "pyproject.toml"), "rb") as handle:
        return tomllib.load(handle)


def _must_not_ship(name: str) -> bool:
    parts = name.split("/")
    base = parts[-1]
    return ("tests" in parts or "__pycache__" in parts or base.endswith((".md", ".pyc", "_test.py", "_tests.py", ".test.py"))
            or base.startswith("test_") or base.startswith("evidence_") or base in ("b6_evidence.py", "requirements.txt"))


class PyprojectTests(unittest.TestCase):
    def test_distribution_command_and_dependencies(self):
        project = _pyproject()["project"]
        self.assertEqual(project["name"], "csl-recursion")
        self.assertEqual(project["scripts"], {"recursiond": "recursion_core._entrypoints:main",
                                              "recursion-control": "recursion_core.control.__main__:main"})
        self.assertEqual(project["dependencies"], [])
        self.assertEqual(project["optional-dependencies"], {"plugins": ["cryptography>=43"]})
        self.assertEqual(project["requires-python"], ">=3.12")
        # Left for Lucas: the licence holder is not named yet.
        self.assertNotIn("license", project)
        self.assertNotIn("license-files", project)

    def test_the_plugins_extra_is_the_plugin_requirements_file(self):
        with open(os.path.join(PACKAGE_DIR, "plugin_security", "requirements.txt"), encoding="utf-8") as handle:
            pinned = [line.strip() for line in handle if line.strip() and not line.startswith("#")]
        self.assertEqual(_pyproject()["project"]["optional-dependencies"]["plugins"], pinned)

    def test_the_console_script_target_is_the_bootstrap(self):
        module, _, attribute = _pyproject()["project"]["scripts"]["recursiond"].partition(":")
        self.assertEqual(module, _entrypoints.__name__)
        self.assertTrue(callable(getattr(_entrypoints, attribute)))

    def test_the_control_command_is_the_control_package_entry(self):
        scripts = _pyproject()["project"]["scripts"]
        self.assertEqual(scripts.get("recursion-control"), "recursion_core.control.__main__:main")
        self.assertEqual(scripts["recursiond"], "recursion_core._entrypoints:main")

    def test_every_console_script_target_imports_to_a_callable(self):
        scripts = _pyproject()["project"]["scripts"]
        self.assertEqual(sorted(scripts), ["recursion-control", "recursiond"])
        for name, target in scripts.items():
            with self.subTest(command=name):
                module, colon, attribute = target.partition(":")
                self.assertEqual(colon, ":")
                self.assertTrue(callable(getattr(importlib.import_module(module), attribute, None)))

    def test_the_backend_builds_recursion_core_from_core(self):
        data = _pyproject()
        self.assertEqual(data["build-system"]["build-backend"], "uv_build")
        backend = data["tool"]["uv"]["build-backend"]
        self.assertEqual((backend["module-root"], backend["module-name"]), ("core", "recursion_core"))
        self.assertTrue(os.path.isfile(os.path.join(REPO, backend["module-root"], backend["module-name"], "__init__.py")))


@unittest.skipUnless(UV and os.path.isfile(UV), "set RECURSION_PACKAGING_UV to a uv binary to build the wheel")
@unittest.skipUnless(sys.platform.startswith("linux"), "the daemon and its unit are Linux-only")
class BuiltWheelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="wheel-")
        cls.tmp = cls._tmp.name
        dist = os.path.join(cls.tmp, "dist")
        env = dict(os.environ, UV_CACHE_DIR=os.path.join(cls.tmp, "uv-cache"), UV_NO_CONFIG="1")
        built = subprocess.run([UV, "build", "--offline", "--no-config", "--out-dir", dist], cwd=REPO, env=env,
                               capture_output=True, text=True)
        if built.returncode != 0:
            raise AssertionError(f"uv build failed: {built.stderr[-2000:]}")
        names = os.listdir(dist)
        cls.wheel = os.path.join(dist, next(name for name in names if name.endswith(".whl")))
        cls.sdist = os.path.join(dist, next(name for name in names if name.endswith(".tar.gz")))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_the_wheel_ships_the_runtime_and_nothing_else(self):
        with zipfile.ZipFile(self.wheel) as wheel:
            names = [name for name in wheel.namelist() if not name.endswith("/")]
            info = next(name for name in names if name.endswith(".dist-info/METADATA"))
            metadata = wheel.read(info).decode()
            entry_points = wheel.read(info.replace("METADATA", "entry_points.txt")).decode()
        files = {name for name in names if name.startswith("recursion_core/")}
        self.assertEqual(MUST_SHIP - files, set())
        self.assertEqual(sorted(name for name in names if _must_not_ship(name)), [])
        self.assertEqual({name.split("/")[0] for name in names},
                         {"recursion_core", os.path.basename(self.wheel).split("-py3")[0] + ".dist-info"})
        # Every runtime module of the checkout ships.
        expected = set()
        for dirpath, dirnames, filenames in os.walk(PACKAGE_DIR):
            dirnames[:] = [d for d in dirnames if d not in ("tests", "__pycache__")]
            for name in filenames:
                rel = os.path.relpath(os.path.join(dirpath, name), os.path.dirname(PACKAGE_DIR)).replace(os.sep, "/")
                if not _must_not_ship(rel) and not name.endswith((".pyc", ".sh")):
                    expected.add(rel)
        self.assertEqual(expected - files, set())
        self.assertIn("recursiond = recursion_core._entrypoints:main", entry_points)
        self.assertIn("recursion-control = recursion_core.control.__main__:main", entry_points)
        lines = metadata.splitlines()
        self.assertIn("Name: csl-recursion", lines)
        self.assertIn("Requires-Python: >=3.12", lines)
        self.assertEqual([line for line in lines if line.startswith("Requires-Dist:")],
                         ["Requires-Dist: cryptography>=43 ; extra == 'plugins'"])
        self.assertFalse([line for line in lines if line.startswith(("License", "Classifier: License"))])

    def test_the_sdist_holds_the_source_without_tests_or_evidence(self):
        with tarfile.open(self.sdist) as sdist:
            names = [member.name.split("/", 1)[1] for member in sdist.getmembers() if member.isfile()]
        self.assertIn("pyproject.toml", names)
        self.assertIn("README.md", names)
        self.assertEqual(sorted(name for name in names if name.startswith("core/") and _must_not_ship(name)), [])
        self.assertEqual(MUST_SHIP - {name[len("core/"):] for name in names if name.startswith("core/")}, set())

    @unittest.skipUnless(FREE_THREADED_314, "the fresh environment is made from free-threaded CPython 3.14")
    def test_the_wheel_installs_into_a_fresh_environment_and_runs_without_pythonpath(self):
        venv = os.path.join(self.tmp, "venv")
        subprocess.run([sys.executable, "-m", "venv", venv], check=True, capture_output=True)
        python = os.path.join(venv, "bin", "python")
        # No PYTHON* variable, and no XDG_CONFIG_HOME or XDG_STATE_HOME of the run's own: `recursiond install` refuses
        # one naming another folder than the default under the home (config_home_elsewhere, state_home_elsewhere).
        clean = {key: value for key, value in os.environ.items()
                 if not key.startswith("PYTHON") and key not in ("XDG_CONFIG_HOME", "XDG_STATE_HOME")}
        subprocess.run([python, "-m", "pip", "install", "--no-index", "--no-deps", "--no-cache-dir", "-q", self.wheel],
                       check=True, capture_output=True, env=clean)

        helped = subprocess.run([os.path.join(venv, "bin", "recursiond"), "--help"], cwd="/", env=clean,
                                capture_output=True, text=True)
        self.assertEqual(helped.returncode, 0, helped.stderr)
        self.assertIn("install", helped.stdout)

        # The control command, installed beside recursiond: a socket client, so its usage answer needs no daemon.
        controlled = subprocess.run([os.path.join(venv, "bin", "recursion-control"), "--help"], cwd="/", env=clean,
                                    capture_output=True, text=True)
        self.assertEqual(controlled.returncode, 0, controlled.stderr)
        self.assertIn("usage: recursion-control", controlled.stdout)

        served = subprocess.run([os.path.join(venv, "bin", "recursiond"), "serve", "--plugin", "relative/dir"],
                                cwd="/", env=dict(clean, PYTHON_GIL="1"), capture_output=True, text=True)
        self.assertEqual(served.returncode, 2, served.stderr)  # past the daemon's own GIL check
        self.assertIn("--plugin must be an absolute directory path", served.stderr)

        home = os.path.join(self.tmp, "home")
        os.mkdir(home, 0o700)
        script = ("import sys, recursion_core; from recursion_core.control.commands import setup; "
                  "sys.exit(setup.run('install', out=sys.stdout, roots=setup.Roots(home=sys.argv[1]), "
                  "interpreter=sys.executable, package=recursion_core.__file__))")
        installed = subprocess.run([python, "-I", "-c", script, home], cwd="/", env=clean, capture_output=True, text=True)
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        answer = json.loads(installed.stdout)
        self.assertEqual((answer["code"], answer["interpreter"]), ("ok", python))
        from recursion_core.daemon_install import service_fields
        with open(answer["unit"], encoding="utf-8") as handle:
            exec_start = service_fields(handle.read())["Service.ExecStart"].split()
        self.assertEqual(exec_start, [python, "-I", "-X", "gil=0", "-m", "recursion_core.daemon"])
        # The unit's own command line, as the user manager would run it.
        unit_run = subprocess.run([*exec_start, "--plugin", "relative/dir"], cwd="/", env=dict(clean, PYTHON_GIL="1"),
                                  capture_output=True, text=True)
        self.assertEqual(unit_run.returncode, 2, unit_run.stderr)
        self.assertIn("--plugin must be an absolute directory path", unit_run.stderr)

        absent = subprocess.run([python, "-I", "-c", "import recursion_core.daemon_install.tests"], cwd="/", env=clean,
                                capture_output=True, text=True)
        self.assertNotEqual(absent.returncode, 0)
        self.assertIn("ModuleNotFoundError", absent.stderr)


if __name__ == "__main__":
    unittest.main()
