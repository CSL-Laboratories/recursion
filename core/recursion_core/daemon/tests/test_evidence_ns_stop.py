"""evidence_ns.sh cleanup must not signal a pid it has already seen exit.

The extracted stop_all is run with kill and systemctl stubbed. No user manager,
session bus, or real kill command runs.
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'evidence_ns.sh'


@unittest.skipUnless(sys.platform.startswith('linux'), 'the evidence wrapper is a Linux shell script')
class EvidenceNsStopTests(unittest.TestCase):
    def test_shell_cleanup_does_not_kill_a_pid_it_saw_exit(self):
        source = SCRIPT.read_text()
        stop_all = source.split('stop_all() {', 1)[1].split('\n}\n', 1)[0]
        scratch = tempfile.TemporaryDirectory(prefix='rc-evidence-ns-')
        self.addCleanup(scratch.cleanup)
        base = Path(scratch.name)
        log = base / 'signals.txt'
        script = base / 'cleanup.sh'
        script.write_text(
            'set -u\n'
            'SYSTEMD_PID=12345\n'
            "DBUS_PID=''\n"
            'ROOT_CG="$1"\n'
            'signal_log="$2"\n'
            'systemctl() { return 0; }\n'
            "kill() { printf '%s\\n' \"$*\" >> \"$signal_log\"; [ \"$1\" != '-0' ]; }\n"
            'stop_all() {' + stop_all + '\n}\n'
            'stop_all\n'
        )
        done = subprocess.run(
            ['/bin/bash', '--noprofile', '--norc', str(script), str(base), str(log)],
            capture_output=True, text=True, timeout=2)
        self.assertEqual(done.returncode, 0, done.stderr)
        sent = log.read_text().splitlines()
        self.assertNotIn('-KILL 12345', sent)
