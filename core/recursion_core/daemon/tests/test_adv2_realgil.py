"""ADV2 red-team: a plugin that really turns the GIL on, on a free-threaded build, with the real probe.

PRODUCT.md d84cdd26 (CSLR-D-012) 3.6 / 4.1 and ``plugin_security/loader.py``: after every load the
loader reads ``sys._is_gil_enabled()`` and a GIL that is on poisons the core. The other ADV2 tests
use a SIMULATED probe; this one compiles a tiny single-phase C extension that declares no
``Py_mod_gil`` slot (so importing it re-enables the GIL on 3.14t), and runs the daemon's
``PluginHost`` in a fresh interpreter with no ``-X gil=0`` and no ``PYTHON_GIL``. Skipped where there
is no free-threaded build, no C compiler, no Python headers or no ``cryptography``.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import unittest

from recursion_core.daemon.tests import plugin_fixture as fx

CORE = Path(__file__).resolve().parents[3]
CC = shutil.which('cc') or shutil.which('gcc')
INCLUDE = sysconfig.get_paths().get('include') or ''
FREE_THREADED = bool(sysconfig.get_config_var('Py_GIL_DISABLED'))
C_SOURCE = r'''
#include <Python.h>
static struct PyModuleDef def = {PyModuleDef_HEAD_INIT, "adv2gil", NULL, -1, NULL};
PyMODINIT_FUNC PyInit_adv2gil(void) { return PyModule_Create(&def); }
'''
HOST = r'''
import json, sys
sys.path.insert(0, sys.argv[3])
from recursion_core.daemon import plugins
from recursion_core.gui.client import o7_command
h = plugins.PluginHost(sys.argv[1], sys.argv[2])
out = {"gil_after_host": sys._is_gil_enabled(), "records": [(r["state"], r["code"]) for r in h.records],
       "poisoned": h.health.poisoned}
out["read"] = list(h.operator(o7_command("operator.positions.inspect", "gui", False)))
out["read_again"] = list(h.operator(o7_command("operator.positions.inspect", "gui", False)))
out["poisoned_after"] = h.health.poisoned
print(json.dumps(out))
'''
ANSWER = b'''
def operator(command):
    return {"status_code": "ok", "result": {}}
'''


def _crypto():
    try:
        import cryptography  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(sys.platform.startswith('linux') and FREE_THREADED and CC and os.path.exists(
    os.path.join(INCLUDE, 'Python.h')) and _crypto(),
    'needs a free-threaded Linux build with a C compiler, its headers and cryptography')
class Adv2RealGilTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-adv2-gil-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)
        self.ext = self.tmp / 'ext'
        self.ext.mkdir()
        (self.ext / 'adv2gil.c').write_text(C_SOURCE)
        suffix = sysconfig.get_config_var('EXT_SUFFIX')
        done = subprocess.run([CC, '-shared', '-fPIC', '-I', INCLUDE, '-o', str(self.ext / ('adv2gil' + suffix)),
                               str(self.ext / 'adv2gil.c')], capture_output=True, text=True, timeout=120, check=False)
        if done.returncode != 0:
            self.skipTest(f'could not build the test extension: {done.stderr[-300:]}')
        self.config = self.tmp / 'config' / 'recursion'

    def run_host(self, source):
        fx.write_install(self.config, grants=[fx.grant_for(source)])
        fx.write_plugin(self.config / 'plugins' / 'gil', source)
        env = {'PYTHONPATH': str(CORE), 'PATH': '/usr/bin:/bin'}  # no PYTHON_GIL, and no -X gil=0 below
        done = subprocess.run([sys.executable, '-B', '-c', HOST, str(self.config), str(self.tmp / 'state'), str(self.ext)],
                              capture_output=True, text=True, timeout=120, env=env, check=False)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        import json
        return json.loads(done.stdout.strip().splitlines()[-1])

    def test_gil_turned_on_at_import_poisons_with_the_real_probe(self):
        out = self.run_host(b'import adv2gil\n' + ANSWER)
        self.assertTrue(out['gil_after_host'])
        self.assertEqual(out['records'], [['refused', 'core_poisoned_gil_enabled']])
        self.assertEqual(out['poisoned'], 'gil_enabled_after_load:fixture.operator')
        self.assertEqual(out['read'][0], 503)

    def test_gil_turned_on_inside_operator_poisons_with_the_real_probe(self):
        out = self.run_host(b'def operator(command):\n    import adv2gil\n    return {"status_code": "ok"}\n')
        self.assertEqual(out['records'], [['loaded', None]])
        self.assertIsNone(out['poisoned'])
        # Observed: the call that turned the GIL on is still answered 200; every later read refuses.
        self.assertEqual(out['read'][0], 200)
        self.assertEqual(out['poisoned_after'], 'gil_enabled_after_plugin_call')
        self.assertEqual(out['read_again'], [503, {'error': 'unavailable: core_poisoned_restart_required'}])


if __name__ == '__main__':
    unittest.main()
