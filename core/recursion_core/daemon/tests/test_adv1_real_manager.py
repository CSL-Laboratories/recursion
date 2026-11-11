"""ADV1 red team against a real ``systemd --user`` manager (skips everywhere else).

Run inside the box evidence namespace (``/dev/shm/nov11/out/adv1/adv1_ns.sh``), where a private
user manager owns a delegated cgroup subtree. Same uid as the operator, exactly as the candidate
launches engines (``EngineUnitManager``: a same-uid transient unit with ``Delegate=yes``).
"""
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest

from recursion_core.daemon import Daemon, Registry
from recursion_core.daemon import control_adapter as adapter
from recursion_core.daemon import launcher
from recursion_core.control.registry import EMPTY

BOOT = 'boot-adv1'

# Runs as the unit's main process. Forks one child that moves itself into a new cgroup next to the
# unit (a plain cgroup.procs write; the operator owns the user manager's subtree), then sleeps.
ESCAPE = r'''
import os, sys, time
out = sys.argv[1]
own = open("/proc/self/cgroup").read().split("0::", 1)[1].strip()
unit_dir = "/sys/fs/cgroup" + own
target = os.path.join(os.path.dirname(unit_dir), "adv1-escape-%d" % os.getpid())
pid = os.fork()
if pid == 0:
    os.setsid()
    try:
        os.mkdir(target)
        with open(os.path.join(target, "cgroup.procs"), "w") as f:
            f.write("0\n")
        status = "moved"
    except OSError as exc:
        status = "refused:%s" % exc.errno
    with open(out, "w") as f:
        f.write("%d %s %s\n" % (os.getpid(), status, target))
    time.sleep(300)
    os._exit(0)
time.sleep(300)
'''

# Runs as the unit's main process: one control call from inside the unit, then the same call after
# moving itself out of the unit's cgroup. Raw frames; nothing imported from recursion_core.
PEER = r'''
import json, os, socket, struct, sys, time
path, out = sys.argv[1], sys.argv[2]
def call():
    s = socket.socket(socket.AF_UNIX); s.settimeout(10); s.connect(path)
    body = json.dumps({"op": "control", "method": "run.list", "params": {}}).encode()
    try:
        s.sendall(struct.pack(">I", len(body)) + body)
    except OSError:
        pass  # refused at accept: the daemon sent its refusal frame and closed before this write
    try:
        n = struct.unpack(">I", s.recv(4))[0]; data = b""
        while len(data) < n: data += s.recv(n - len(data))
        return json.loads(data)
    except (OSError, struct.error) as exc:
        return {"error": {"code": "closed:" + type(exc).__name__}}
    finally:
        s.close()
inside = call()
own = open("/proc/self/cgroup").read().split("0::", 1)[1].strip()
target = os.path.join(os.path.dirname("/sys/fs/cgroup" + own), "adv1-peer-%d" % os.getpid())
os.mkdir(target)
open(os.path.join(target, "cgroup.procs"), "w").write("0\n")
outside = call()
open(out, "w").write(json.dumps({"inside": inside, "outside": outside, "target": target, "pid": os.getpid()}))
time.sleep(300)
'''

ARGV = r'''
import json, sys
open(sys.argv[1], "w").write(json.dumps(sys.argv[2:]))
'''


def manager_running():
    try:
        state = subprocess.run(['systemctl', '--user', 'is-system-running'], capture_output=True, text=True,
                               timeout=15, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return state in ('running', 'degraded')


def alive(pid):
    try:
        text = Path(f'/proc/{pid}/stat').read_text()
    except OSError:
        return False
    return text[text.rindex(')') + 2:].split()[0] not in ('Z', 'X')


@unittest.skipUnless(sys.platform.startswith('linux') and manager_running(),
                     'needs a running systemd --user manager (run /dev/shm/nov11/out/adv1/adv1_ns.sh)')
class RealManagerAttackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-adv1-real-', dir=os.environ.get('XDG_RUNTIME_DIR')))
        os.chmod(self.tmp, 0o700)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.manager = launcher.EngineUnitManager()
        self.daemon = Daemon(Registry(self.tmp / 'state'), self.manager, boot_id=lambda: BOOT)
        self.addCleanup(self.daemon.close)
        self.leftovers = []
        self.addCleanup(self.clean)

    def clean(self):
        for pid, cgroup in self.leftovers:
            try:
                if pid > 0:
                    os.kill(pid, 9)
            except OSError:
                pass
            for _ in range(100):
                try:
                    os.rmdir(cgroup)
                    break
                except FileNotFoundError:
                    break
                except OSError:
                    time.sleep(0.05)

    def launch(self, run_id, argv):
        unit = f'recursion-run-{run_id}.service'
        self.addCleanup(lambda: subprocess.run(['systemctl', '--user', 'stop', unit], capture_output=True, check=False))
        self.manager.prepare(unit, properties=(), working_directory=None)
        self.daemon.start(run_id, argv)
        group, main = self.manager.unit_identity(unit)
        self.assertTrue(group)
        return unit, group, main

    def wait_file(self, path, seconds=15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if path.exists() and path.read_text().strip():
                return path.read_text()
            time.sleep(0.05)
        self.fail(f'{path} never written')

    def test_a_recorded_cgroup_is_killed_through_the_cgroup_without_the_manager(self):
        """ADV1-1 (fixed): the kill a forgotten unit gets. No systemctl stop is sent here."""
        run_id = 'run-adv1kill%d' % os.getpid()
        unit, group, main = self.launch(run_id, [sys.executable, '-I', '-c', 'import time; time.sleep(300)'])
        self.assertTrue(alive(main))
        self.assertIsNone(self.manager.recorded_cgroup_clear(unit, group + '-other'))
        self.assertTrue(alive(main))
        self.assertIs(self.manager.recorded_cgroup_clear(unit, group), True)
        self.assertFalse(alive(main))

    @unittest.expectedFailure  # ADV1-2
    def test_a_descendant_that_moves_out_of_the_unit_cgroup_outlives_an_attested_stop(self):
        """ADV1-2: same-uid engine, operator-owned user-manager subtree: one cgroup.procs write escapes Stop."""
        run_id = 'run-adv1esc%d' % os.getpid()
        out = self.tmp / 'escape.txt'
        _, group, main = self.launch(run_id, [sys.executable, '-I', '-c', ESCAPE, str(out)])
        pid, status, target = self.wait_file(out).split()
        pid = int(pid)
        self.leftovers.append((pid, target))
        self.assertEqual(status, 'moved', 'the escape itself must work for this test to mean anything')
        kill = launcher.UnitKillTree(self.daemon)
        kill.request_stop(group)
        self.assertEqual(kill.confirm(group, 10.0), EMPTY)  # the receipt would say stopped, attested
        self.assertFalse(alive(main))
        self.assertFalse(alive(pid), f'pid {pid} (a descendant of the run) is alive in {target} after "stopped"')

    @unittest.expectedFailure  # ADV1-2b
    def test_an_engine_that_left_its_unit_cgroup_regains_control_of_the_socket(self):
        """ADV1-2b: peer_is_engine reads the peer's current cgroup, which the engine can change."""
        environ = {'XDG_RUNTIME_DIR': str(self.tmp / 'rt'), 'XDG_CONFIG_HOME': str(self.tmp / 'cfg'),
                   'PATH': '/usr/bin:/bin', 'HOME': str(self.tmp)}
        (self.tmp / 'rt').mkdir(mode=0o700)
        composed = adapter.compose_control(self.daemon, self.tmp / 'state', environ=environ, routes={})
        self.addCleanup(composed.close)
        run_id = 'run-adv1peer%d' % os.getpid()
        out = self.tmp / 'peer.json'
        self.launch(run_id, [sys.executable, '-I', '-c', PEER, composed.server.path, str(out)])
        result = json.loads(self.wait_file(out))
        self.leftovers.append((result['pid'], result['target']))
        self.assertEqual(result['inside'].get('error', {}).get('code'), 'scope_refused', result)
        self.assertEqual(result['outside'].get('error', {}).get('code'), 'scope_refused',
                         'the same engine process got: %s' % json.dumps(result['outside'])[:200])

    def test_unit_argv_reaches_the_engine_verbatim(self):
        """ADV1-11 (fixed): systemd-run would expand $VAR, ${VAR} and $$ in the unit argv from the user
        manager's environment; EngineUnitManager.start passes --expand-environment=no."""
        run_id = 'run-adv1argv%d' % os.getpid()
        out = self.tmp / 'argv.json'
        tricky = ['$HOME', '${USER}', '%n', '%h', '%%', 'a b', ';id', '-x', '\\n', 'line\nbreak', '$$']
        self.launch(run_id, [sys.executable, '-I', '-c', ARGV, str(out), *tricky])
        self.assertEqual(json.loads(self.wait_file(out)), tricky)


if __name__ == '__main__':
    unittest.main()
