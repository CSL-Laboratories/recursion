"""Free-threaded runtime: refusals on this interpreter, and real 3.14t checks where installed.

Evidence classes, kept apart:

* ``ReportRefusalTests`` feeds hand-written runtime reports to the start check. These
  are synthetic inputs; they show the decision table, not any interpreter's behaviour.
* ``ThisInterpreterTests`` uses the interpreter running the suite. On this box that is
  CPython 3.13.5 with the GIL, which must be refused, and the real ``GilProbe`` must
  poison the loader after a load.
* ``FreeThreaded314Tests`` runs a real CPython 3.14 free-threaded binary in a subprocess
  (found as ``python3.14t`` on PATH or ``~/.local/bin``). It compiles a tiny C extension
  that does not declare ``Py_mod_gil`` and loads it through the loader. Skipped when no
  3.14t or no C compiler is available; never simulated.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import sysconfig
import textwrap
import unittest
from pathlib import Path

from recursion_core.plugin_security import CoreHealth, GilProbe, PluginLoader, Refused, ReservedEffects, \
    TrustStore, require_production_runtime, runtime_report
from recursion_core.plugin_security.tests.support import temp_dir

CORE_ROOT = Path(__file__).resolve().parents[3]


def _find_314t() -> str | None:
    for candidate in (shutil.which("python3.14t"), os.path.expanduser("~/.local/bin/python3.14t")):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


PY314T = _find_314t()
CC = shutil.which("cc") or shutil.which("gcc")

GOOD = {"implementation": "cpython", "version": [3, 14, 10], "free_threaded_build": True,
        "gil_xoption": "0", "gil_enabled": False}


class ReportRefusalTests(unittest.TestCase):
    def test_decision_table(self) -> None:
        require_production_runtime(GOOD)
        cases = {
            "runtime_requires_cpython_314t": [{"version": [3, 13, 5]}, {"implementation": "pypy"},
                                              {"version": [3, 15, 0]}],
            "runtime_gil_build_refused": [{"free_threaded_build": False}],
            "runtime_requires_X_gil_0": [{"gil_xoption": None}, {"gil_xoption": "1"}],
            "runtime_gil_enabled": [{"gil_enabled": True}],
        }
        for code, changes in cases.items():
            for change in changes:
                with self.subTest(code=code, change=change), self.assertRaises(Refused) as ctx:
                    require_production_runtime({**GOOD, **change})
                self.assertEqual(ctx.exception.code, code)


class ThisInterpreterTests(unittest.TestCase):
    def test_this_interpreter_is_judged_honestly(self) -> None:
        info = runtime_report()
        production = (info["implementation"] == "cpython" and info["version"][:2] == [3, 14]
                      and info["free_threaded_build"] and info["gil_xoption"] == "0"
                      and not info["gil_enabled"])
        if production:
            require_production_runtime()
        else:
            with self.assertRaises(Refused):
                require_production_runtime()

    @unittest.skipIf(bool(sysconfig.get_config_var("Py_GIL_DISABLED")), "needs a GIL build")
    def test_real_probe_on_gil_build_poisons_after_first_load(self) -> None:
        import hashlib
        from recursion_core.plugin_security.tests.support import manifest_bytes
        source = b"X = 1\n"
        health = CoreHealth()
        loader = PluginLoader(TrustStore({}), {}, ReservedEffects(), health, GilProbe())
        raw = manifest_bytes("11" * 32, source=source, permissions=("read",))
        self.assertEqual(hashlib.sha256(source).hexdigest(), json.loads(raw)["artifact_sha256"])
        with self.assertRaises(Refused) as ctx:
            loader.load(raw, "", source)
        self.assertEqual(ctx.exception.code, "core_poisoned_gil_enabled")
        self.assertTrue(health.poisoned)


_CHILD = textwrap.dedent('''
    import hashlib, json, sys
    sys.path.insert(0, sys.argv[1])
    sys.path.insert(0, sys.argv[2])
    from recursion_core.plugin_security import (CoreHealth, PluginLoader, Refused, ReservedEffects,
        TrustStore, runtime_report)
    from recursion_core.plugin_security.tests.support import manifest_bytes
    plugin = sys.argv[3].encode()
    health = CoreHealth()
    loader = PluginLoader(TrustStore({}), {}, ReservedEffects(), health)
    out = {"before": runtime_report()}
    try:
        loader.load(manifest_bytes("11" * 32, source=plugin, permissions=("read",)), "", plugin)
        out["load"] = "ok"
    except Refused as exc:
        out["load"] = exc.code
    out["after_gil"] = sys._is_gil_enabled()
    out["poisoned"] = health.poisoned
    out["findings"] = [f["code"] for f in loader.last_findings]
    try:
        loader.load(manifest_bytes("11" * 32, plugin_id="second.plugin", source=b"Y = 2\\n",
                    permissions=("read",)), "", b"Y = 2\\n")
        out["second"] = "ok"
    except Refused as exc:
        out["second"] = exc.code
    print(json.dumps(out))
''')


@unittest.skipUnless(PY314T, "no CPython 3.14t on this machine")
class FreeThreaded314Tests(unittest.TestCase):
    """Real 3.14t subprocesses. Not mocked."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.version = subprocess.run([PY314T, "-VV"], capture_output=True, text=True, timeout=30).stdout

    def _run(self, flags: list[str], code: str, *argv: str) -> subprocess.CompletedProcess:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run([PY314T, *flags, "-B", "-I", "-c", code, *argv], capture_output=True,
                              text=True, timeout=120, env=env)

    def test_start_check_on_real_314t(self) -> None:
        self.assertIn("free-threading", self.version)
        code = ("import sys; sys.path.insert(0, sys.argv[1]);"
                "from recursion_core.plugin_security import require_production_runtime, Refused\n"
                "try:\n require_production_runtime(); print('ok')\n"
                "except Refused as e:\n print(e.code)")
        expected = {("-X", "gil=0"): "ok", (): "runtime_requires_X_gil_0",
                    ("-X", "gil=1"): "runtime_requires_X_gil_0"}
        for flags, want in expected.items():
            with self.subTest(flags=flags):
                proc = self._run(list(flags), code, str(CORE_ROOT))
                self.assertEqual(proc.stdout.strip(), want, proc.stderr)

    def _build_extension(self) -> str:
        if not CC:
            self.skipTest("no C compiler")
        info = self._run([], "import sysconfig, json; print(json.dumps([sysconfig.get_paths()['include'],"
                             " sysconfig.get_config_var('EXT_SUFFIX')]))")
        include, suffix = json.loads(info.stdout)
        work = temp_dir(self)
        src = Path(work, "nogilmod.c")
        src.write_text('#include <Python.h>\nstatic struct PyModuleDef m = {PyModuleDef_HEAD_INIT, '
                       '"nogilmod", NULL, -1, NULL};\nPyMODINIT_FUNC PyInit_nogilmod(void) '
                       '{ return PyModule_Create(&m); }\n')
        built = subprocess.run([CC, "-shared", "-fPIC", "-I", include, str(src), "-o",
                                str(Path(work, "nogilmod" + suffix))], capture_output=True, text=True,
                               timeout=120)
        if built.returncode != 0:
            self.skipTest("C build failed: " + built.stderr[-200:])
        return work

    def _load_extension_plugin(self, flags: list[str], raise_after: bool) -> dict:
        work = self._build_extension()
        plugin = "import nogilmod\n" + ("raise RuntimeError('after import')\n" if raise_after else "")
        proc = self._run(flags, _CHILD, str(CORE_ROOT), work, plugin)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_non_ft_extension_turns_gil_on_and_poisons(self) -> None:
        """No ``-X gil=0``: the import switches the GIL on; the loader poisons the core."""
        out = self._load_extension_plugin([], raise_after=False)
        self.assertFalse(out["before"]["gil_enabled"])
        self.assertTrue(out["after_gil"])
        self.assertEqual(out["load"], "core_poisoned_gil_enabled")
        self.assertTrue(out["poisoned"])
        self.assertEqual(out["second"], "core_poisoned_restart_required")

    def test_gil_check_runs_even_when_plugin_raises(self) -> None:
        out = self._load_extension_plugin([], raise_after=True)
        self.assertTrue(out["after_gil"])
        self.assertEqual(out["load"], "core_poisoned_gil_enabled")
        self.assertEqual(out["second"], "core_poisoned_restart_required")

    def test_under_X_gil_0_gil_stays_off_and_doctor_reports_extension(self) -> None:
        """Measured limit: with ``-X gil=0`` the GIL stays off, so only the finding remains."""
        out = self._load_extension_plugin(["-X", "gil=0"], raise_after=False)
        self.assertFalse(out["after_gil"])
        self.assertEqual(out["load"], "ok")
        self.assertIsNone(out["poisoned"])
        self.assertIn("extension_module_loaded", out["findings"])
        self.assertEqual(out["second"], "ok")


if __name__ == "__main__":
    unittest.main()
