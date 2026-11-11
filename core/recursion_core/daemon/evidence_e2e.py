"""End-to-end evidence for the composed daemon on Linux (CSL-738). Run by ``evidence_ns.sh``.

Drives the REAL daemon process (``python3.14t -X gil=0 -m recursion_core.daemon``) and the REAL
CLI (``python -m recursion_core.control``) over the owner-only Unix socket, against a real
``systemd --user`` manager, and writes what it observed to ``evidence.json`` and ``evidence.md``:
start, stream, inspect, Stop, cleanup and restart (PRODUCT.md d84cdd26, CSLR-D-012: section 3.1
lines 459-461 runs survive the daemon; section 3.4 S6 line 888 Stop is truthful on every surface).

The engine is ``evidence_standin_engine.py``, a STAND-IN ENGINE installed under the name ``claude``:
no subscription, API key, model or network is used. Operator Linger is a host precondition this
harness stubs when the wrapper says so (a box with no logind); the stub is on the harness PATH
only and is named in the evidence. Nothing here is product code, and a step that did not hold is
written down as failed: the harness exits 1 if any check fails.
"""
from __future__ import annotations

import argparse
import datetime
import http.client
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time

CORE = Path(__file__).resolve().parents[2]
STANDIN = Path(__file__).with_name('evidence_standin_engine.py')
TENANT = 'evidence'


class Harness:
    def __init__(self, out, python, linger_stubbed, exec_dir=None):
        self.out = Path(out)
        self.work = self.out / ('work-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
        # Where the stand-in engine and the loginctl stub are installed. The output directory may sit on
        # a noexec mount (/dev/shm here), so the wrapper names a private directory that allows exec.
        self.exec_dir = Path(exec_dir) if exec_dir else self.work
        self.python, self.linger_stubbed = python, linger_stubbed
        self.steps, self.checks = [], []
        self.daemon = None
        self.port = None
        self.env = None

    # -- recording -----------------------------------------------------------------------------
    def step(self, name, **observed):
        entry = {'step': name, 'at': datetime.datetime.now(datetime.timezone.utc).isoformat(), **observed}
        self.steps.append(entry)
        print(f'[{name}] ' + json.dumps(observed, sort_keys=True, default=str)[:600], flush=True)
        return entry

    def check(self, name, ok, detail=''):
        self.checks.append({'check': name, 'ok': bool(ok), 'detail': detail})
        print(f'  {"PASS" if ok else "FAIL"} {name} {detail}', flush=True)
        return bool(ok)

    # -- processes -----------------------------------------------------------------------------
    def run(self, argv, **kwargs):
        return subprocess.run(argv, text=True, capture_output=True, timeout=120, env=self.env, **kwargs)

    def cli(self, *argv):
        """One real CLI process over the socket. Returns (exit code, parsed stdout, seconds)."""
        began = time.monotonic()
        done = self.run([self.python, '-X', 'gil=0', '-B', '-m', 'recursion_core.control', *argv])
        took = round(time.monotonic() - began, 3)
        try:
            answer = json.loads(done.stdout)
        except ValueError:
            answer = {'unparsed_stdout': done.stdout, 'stderr': done.stderr[-500:]}
        return done.returncode, answer, took

    def systemctl(self, *argv):
        done = self.run(['systemctl', '--user', *argv])
        return done.stdout.strip()

    def unit_props(self, unit):
        text = self.systemctl('show', '--property=LoadState,ActiveState,SubState,ControlGroup,MainPID,'
                              'KillSignal,TimeoutStopUSec,KillMode', '--', unit)
        return dict(line.split('=', 1) for line in text.splitlines() if '=' in line)

    def cgroup_pids(self, group):
        """Every pid in the cgroup subtree, or None when the cgroup directory does not exist."""
        root = Path('/sys/fs/cgroup') / group.lstrip('/')
        if not root.is_dir():
            return None
        pids = []
        for directory, _, files in os.walk(root):
            if 'cgroup.procs' in files:
                pids += [int(p) for p in (Path(directory) / 'cgroup.procs').read_text().split()]
        return sorted(pids)

    @staticmethod
    def pid_exists(pid):
        try:
            state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
        except OSError:
            return False
        return state not in ('Z', 'X')

    def start_daemon(self):
        log = open(self.work / 'daemon.log', 'ab')
        self.daemon = subprocess.Popen(
            [self.python, '-X', 'gil=0', '-B', '-m', 'recursion_core.daemon', '--gui-port', str(self.port),
             '--sweep-seconds', '1'], env=self.env, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            start_new_session=True)
        sock = Path(self.env['XDG_RUNTIME_DIR']) / 'recursion' / 'control.sock'
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.daemon.poll() is not None:
                break
            if sock.exists() and self.cli('list')[0] == 0:
                return True
            time.sleep(0.2)
        return False

    def gui(self, method, path, *, token=None, body=None, origin=False):
        headers = {}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        if origin:
            headers['Origin'] = f'http://127.0.0.1:{self.port}'
        if body is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, response.read().decode('utf-8', 'replace')
        finally:
            conn.close()

    def wait_for(self, predicate, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.25)
        return predicate()

    # -- the run -------------------------------------------------------------------------------
    def prepare(self):
        for name in ('state', 'config/recursion'):
            (self.work / name).mkdir(parents=True)
        os.chmod(self.work, 0o700)
        for name in ('bin', 'stub'):
            (self.exec_dir / name).mkdir(parents=True, mode=0o700)
        engine = self.exec_dir / 'bin' / 'claude'
        shutil.copyfile(STANDIN, engine)
        os.chmod(engine, 0o700)
        path = '/usr/bin:/bin'
        if self.linger_stubbed:
            stub = self.exec_dir / 'stub' / 'loginctl'
            stub.write_text('#!/bin/sh\n# HARNESS STUB (not product code): this box has no logind.\n'
                            '# Answers operator Linger only; every other call fails.\n'
                            'case "$*" in *show-user*Linger*) echo yes;; *) exit 1;; esac\n')
            os.chmod(stub, 0o700)
            path = f'{self.exec_dir / "stub"}:{path}'
        routes = {'routes': {'claude_code': {
            'model': 'stand-in-model', 'connection': {'kind': 'subscription', 'id': 'stand-in-login'},
            'api_key_helper': 'absent', 'deadline_s': 900, 'binary': str(engine)}}}
        config = self.work / 'config' / 'recursion' / 'routes.json'
        config.write_text(json.dumps(routes, indent=2))
        os.chmod(config, 0o600)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            self.port = probe.getsockname()[1]
        keep = ('XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS', 'HOME', 'USER', 'LOGNAME', 'LANG')
        self.env = {k: os.environ[k] for k in keep if k in os.environ}
        self.env.update(PATH=path, PYTHONPATH=str(CORE), XDG_STATE_HOME=str(self.work / 'state'),
                        XDG_CONFIG_HOME=str(self.work / 'config'))
        self.step('preconditions',
                  systemd_user=self.systemctl('is-system-running'),
                  systemd_version=self.run(['systemctl', '--version']).stdout.splitlines()[0],
                  harness_cgroup=Path('/proc/self/cgroup').read_text().strip(),
                  daemon_python=self.run([self.python, '-X', 'gil=0', '-c',
                                          'import sys; print(sys.version, sys._is_gil_enabled())']).stdout.strip(),
                  engine=f'STAND-IN ENGINE (evidence_standin_engine.py installed as {self.exec_dir}/bin/claude): '
                         'no subscription, API key, model or network',
                  operator_linger=('host precondition stubbed: operator Linger (no logind on box); '
                                   f'stub is {self.exec_dir}/stub/loginctl on the harness PATH only')
                  if self.linger_stubbed else 'read from the real loginctl')

    def start_run(self, prompt, key):
        code, answer, took = self.cli('start', '--tenant', TENANT, '--engine', 'claude_code',
                                      '--prompt', prompt, '--client-key', key)
        run = answer.get('run') or {}
        return code, answer, took, run.get('run_id'), run.get('cgroup')

    def status(self, run_id):
        return self.cli('status', run_id)[1].get('run') or {}

    def output_lines(self, run_id):
        path = self.work / 'state' / 'recursion' / 'runs' / run_id / 'stdout.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def cleanup_view(self, run_id, group, pids):
        unit = f'recursion-run-{run_id}.service'
        return {'cgroup': group, 'cgroup_pids': self.cgroup_pids(group),
                'pids_alive': {str(p): self.pid_exists(p) for p in pids},
                'unit': {k: v for k, v in self.unit_props(unit).items() if k in ('LoadState', 'ActiveState', 'SubState')},
                'listed_run_units': self.systemctl('list-units', '--all', '--plain', '--no-legend',
                                                   'recursion-run-*.service')}

    def is_clean(self, view):
        return (not view['cgroup_pids'] and not any(view['pids_alive'].values())
                and view['unit'].get('ActiveState') in ('inactive', 'failed'))

    def main(self):
        self.prepare()
        # 0. CLI with the daemon down ----------------------------------------------------------
        code, answer, _ = self.cli('list')
        self.step('cli_daemon_down', exit=code, answer=answer)
        self.check('daemon down: CLI answers control_api_unavailable, exit 1',
                   code == 1 and answer.get('code') == 'control_api_unavailable', json.dumps(answer))

        # 1. start -----------------------------------------------------------------------------
        up = self.start_daemon()
        self.step('daemon_start', up=up, pid=self.daemon.pid, gui_port=self.port,
                  socket=str(Path(self.env['XDG_RUNTIME_DIR']) / 'recursion' / 'control.sock'),
                  socket_mode=oct(os.stat(Path(self.env['XDG_RUNTIME_DIR']) / 'recursion' / 'control.sock').st_mode & 0o777)
                  if up else None,
                  socket_dir_mode=oct(os.stat(Path(self.env['XDG_RUNTIME_DIR']) / 'recursion').st_mode & 0o777) if up else None)
        if not self.check('daemon up: real daemon process answers the CLI over the socket', up):
            return
        code, answer, took, run1, group1 = self.start_run('stream until stopped', 'ev-run-1')
        unit1 = f'recursion-run-{run1}.service'
        props = self.unit_props(unit1) if run1 else {}
        self.step('start', exit=code, seconds=took, answer=answer, unit=unit1, unit_properties=props,
                  cgroup_pids=self.cgroup_pids(group1) if group1 else None)
        self.check('start: CLI exit 0, code started, state running', code == 0 and answer.get('code') == 'started'
                   and (answer.get('run') or {}).get('state') == 'running')
        self.check('start: engine is a transient user unit in its own cgroup (not the daemon\'s)',
                   props.get('ActiveState') == 'active' and props.get('ControlGroup') == group1
                   and bool(group1) and group1.endswith(unit1), props.get('ControlGroup', ''))
        self.check('start: unit carries the #916 stop properties (SIGINT, 2 s, control-group)',
                   props.get('KillSignal') == '2' and props.get('TimeoutStopUSec') == '2s'
                   and props.get('KillMode') == 'control-group',
                   f"KillSignal={props.get('KillSignal')} TimeoutStopUSec={props.get('TimeoutStopUSec')}")
        code, replay, _, run1b, _ = self.start_run('stream until stopped', 'ev-run-1')
        self.check('start: same client key answers the same run, launches nothing', run1b == run1
                   and replay.get('duplicate') is True, f'duplicate={replay.get("duplicate")}')

        # 2. stream ----------------------------------------------------------------------------
        samples = []
        for _ in range(4):
            out = self.status(run1).get('output') or {}
            samples.append({'bytes': out.get('bytes'), 'lines_in_tail': len(out.get('lines') or []),
                            'last_line': (out.get('lines') or [None])[-1], 'complete': out.get('complete')})
            time.sleep(1.0)
        lines = self.output_lines(run1)
        init = next((l for l in lines if l.get('subtype') == 'init'), {})
        helper = next((l.get('helper_pid') for l in lines if l.get('subtype') == 'stand_in_helper'), None)
        self.step('stream', status_output_samples=samples, engine_init_line=init, helper_pid=helper,
                  note='lines are the STAND-IN ENGINE\'s stdout, read through `status` over the socket')
        sizes = [s['bytes'] or 0 for s in samples]
        self.check('stream: engine output arrives and grows across status reads',
                   sizes == sorted(sizes) and sizes[-1] > sizes[0] > 0, str(sizes))
        self.check('stream: lines are labelled stand-in engine', init.get('stand_in_engine') is True)
        names = set(init.get('env_names') or [])
        self.check('launch env: built by the child_env policy (route home, no daemon-only names)',
                   init.get('home') == str(self.work / 'state' / 'recursion' / 'homes' / 'claude_code')
                   and not names & {'PYTHONPATH', 'XDG_STATE_HOME', 'XDG_CONFIG_HOME', 'DBUS_SESSION_BUS_ADDRESS',
                                    'INVOCATION_ID', 'SYSTEMD_EXEC_PID', 'MANAGERPID'}, ','.join(sorted(names)))

        # 3. inspect ---------------------------------------------------------------------------
        code_l, listing, _ = self.cli('list')
        code_s, status, _ = self.cli('status', run1)
        code_c, conditions, _ = self.cli('conditions')
        code_u, unknown, _ = self.cli('status', 'run-does-not-exist')
        token = (Path(self.env['XDG_RUNTIME_DIR']) / 'recursion' / 'gui-device-token').read_text().strip()
        gui_anon = self.gui('GET', '/')[0]
        gui_wrong = self.gui('GET', '/', token='not-the-token')[0]
        gui_ok, page = self.gui('GET', '/', token=token)
        run_view = dict(status.get('run') or {})
        run_view.pop('output', None)
        self.step('inspect', list_exit=code_l, list=listing, status_exit=code_s, status_run=run_view,
                  conditions=conditions, unknown_run={'exit': code_u, 'answer': unknown},
                  gui={'no_token': gui_anon, 'wrong_token': gui_wrong, 'device_token': gui_ok,
                       'bind': f'127.0.0.1:{self.port}', 'token_printed': False})
        self.check('inspect: list and status agree the run is running, observed running',
                   code_l == 0 and code_s == 0 and run_view.get('state') == 'running'
                   and (run_view.get('observed') or {}).get('state') == 'running'
                   and [r.get('run_id') for r in listing.get('runs', [])] == [run1])
        self.check('inspect: unknown run id answers run_unknown, exit 4',
                   code_u == 4 and unknown.get('code') == 'run_unknown')
        self.check('GUI: loopback page refuses without the device token (401) and serves with it (200)',
                   (gui_anon, gui_wrong, gui_ok) == (401, 401, 200) and 'Recursion operator' in page,
                   f'{gui_anon}/{gui_wrong}/{gui_ok}')

        # 4. Stop ------------------------------------------------------------------------------
        before = sorted(set((self.cgroup_pids(group1) or []) + ([helper] if helper else [])))
        code, receipt, took = self.cli('stop', run1)
        after_status = self.status(run1)
        tail = self.output_lines(run1)[-1]
        self.step('stop', exit=code, seconds=took, receipt=receipt, pids_before=before,
                  last_engine_line=tail, status_after={k: after_status.get(k) for k in
                                                       ('state', 'stop', 'stop_attestation', 'observed')})
        self.check('Stop: receipt is stopped (exit 0) and carries no unrecorded flag',
                   code == 0 and receipt.get('code') == 'stopped' and receipt.get('recorded') is not False,
                   json.dumps(receipt))
        self.check('Stop: engine was told to stop (SIGINT seen by the engine before it ended)',
                   tail.get('subtype') == 'stand_in_sigint', json.dumps(tail))
        self.check('Stop: record is stopped with an empty-cgroup attestation for this cgroup',
                   after_status.get('state') == 'stopped'
                   and (after_status.get('stop_attestation') or {}).get('proof') == 'empty'
                   and (after_status.get('stop_attestation') or {}).get('cgroup') == group1
                   and (after_status.get('observed') or {}).get('state') == 'terminated')
        self.check('Stop: took at least the SIGINT grace for the SIGINT-ignoring helper, under 10 s',
                   1.5 <= took <= 10, f'{took}s')

        # 5. cleanup ---------------------------------------------------------------------------
        view = self.wait_for(lambda: (lambda v: v if self.is_clean(v) else None)(
            self.cleanup_view(run1, group1, before)), 10) or self.cleanup_view(run1, group1, before)
        self.step('cleanup', **view)
        self.check('cleanup: cgroup empty or removed', not view['cgroup_pids'], str(view['cgroup_pids']))
        self.check('cleanup: every pid of the tree is gone, including the SIGINT-ignoring helper',
                   len(before) >= 2 and not any(view['pids_alive'].values()), json.dumps(view['pids_alive']))
        self.check('cleanup: unit is no longer active (inactive, failed or not loaded) and holds no process', self.is_clean(view), json.dumps(view['unit']))
        code, again, _ = self.cli('stop', run1)
        self.check('Stop repeat: still stopped, marked repeat', code == 0 and again.get('repeat') is True,
                   json.dumps(again))

        # 5b. the same Stop from the GUI ---------------------------------------------------------
        _, _, _, run2, group2 = self.start_run('stream until stopped', 'ev-run-2')
        pids2 = self.cgroup_pids(group2) or []
        status_code, page = self.gui('POST', '/stop', token=token, origin=True,
                                     body=f'run_id={run2}&confirm=explicit')
        gui_status = self.status(run2)
        view2 = self.wait_for(lambda: (lambda v: v if self.is_clean(v) else None)(
            self.cleanup_view(run2, group2, pids2)), 10) or self.cleanup_view(run2, group2, pids2)
        self.step('gui_stop', run_id=run2, http_status=status_code,
                  page_says_stopped='reported by the control API: <strong>stopped</strong>' in page,
                  status_after=gui_status.get('state'), cleanup=view2)
        self.check('GUI Stop: same control path, page shows stopped, tree gone',
                   status_code == 200 and 'reported by the control API: <strong>stopped</strong>' in page
                   and gui_status.get('state') == 'stopped' and self.is_clean(view2))

        # 6. restart ---------------------------------------------------------------------------
        _, _, _, run3, group3 = self.start_run('stream until stopped', 'ev-run-3')   # keeps running
        _, _, _, run4, group4 = self.start_run('ticks=8', 'ev-run-4')                # ends on its own while down
        _, _, _, run5, group5 = self.start_run('stream until stopped', 'ev-run-5')   # stopped behind the daemon's back
        time.sleep(1.0)
        pids3 = self.cgroup_pids(group3) or []
        bytes_before = (self.status(run3).get('output') or {}).get('bytes')
        self.daemon.send_signal(signal.SIGKILL)
        self.daemon.wait()
        code_down, down, _ = self.cli('status', run3)
        self.systemctl('stop', f'recursion-run-{run5}.service')
        time.sleep(6.0)  # run 4 finishes (8 ticks) with no daemon alive
        size_down = (self.work / 'state' / 'recursion' / 'runs' / run3 / 'stdout.jsonl').stat().st_size
        alive_down = {str(p): self.pid_exists(p) for p in pids3}
        self.step('daemon_killed', signal='SIGKILL', daemon_exit=self.daemon.returncode,
                  cli_while_down={'exit': code_down, 'answer': down},
                  run3_pids_alive=alive_down, run3_output_bytes={'before_kill': bytes_before, 'while_down': size_down},
                  run3_unit=self.unit_props(f'recursion-run-{run3}.service').get('ActiveState'),
                  run4_last_line=self.output_lines(run4)[-1],
                  run5='stopped with `systemctl --user stop` while the daemon was down')
        self.check('restart: run survives the daemon being killed and keeps writing output',
                   all(alive_down.values()) and len(pids3) >= 2 and size_down > (bytes_before or 0),
                   f'{bytes_before} -> {size_down}')
        self.check('restart: CLI is truthful while the daemon is down (control_api_unavailable)',
                   code_down == 1 and down.get('code') == 'control_api_unavailable')
        up = self.start_daemon()
        self.check('restart: daemon comes back on the same registry', up)
        time.sleep(2.5)  # one sweep and one control reconcile
        code_l, listing, _ = self.cli('list')
        s3, s4, s5 = self.status(run3), self.status(run4), self.status(run5)
        brief = lambda s: {k: s.get(k) for k in ('state', 'recorded_state', 'observed', 'result', 'stop_attestation')}  # noqa: E731
        self.step('restart', daemon_pid=self.daemon.pid, list_exit=code_l,
                  list=[{k: r.get(k) for k in ('run_id', 'state', 'recorded_state', 'observed')}
                        for r in listing.get('runs', [])],
                  run3_still_running=brief(s3), run4_ended_while_down=brief(s4), run5_stopped_outside=brief(s5),
                  run3_output_bytes_after=(s3.get('output') or {}).get('bytes'))
        self.check('restart: all five runs are still listed', code_l == 0 and
                   {r.get('run_id') for r in listing.get('runs', [])} == {run1, run2, run3, run4, run5})
        self.check('restart: the surviving run reads running (observed running), output still growing',
                   s3.get('state') == 'running' and (s3.get('observed') or {}).get('state') == 'running'
                   and ((s3.get('output') or {}).get('bytes') or 0) > size_down)
        self.check('restart: the run that ended while the daemon was down reads ended, never running or stopped',
                   s4.get('state') == 'ended' and s4.get('recorded_state') == 'running'
                   and (s4.get('observed') or {}).get('state') == 'terminated', json.dumps(brief(s4)))
        self.check('restart: the run stopped behind the daemon\'s back is not reported running or stopped',
                   s5.get('state') not in ('running', 'stopped'), json.dumps(brief(s5)))
        self.check('restart: earlier Stop receipts are unchanged (stopped, attested)',
                   self.status(run1).get('state') == 'stopped' and self.status(run2).get('state') == 'stopped')
        code, receipt3, took3 = self.cli('stop', run3)
        view3 = self.wait_for(lambda: (lambda v: v if self.is_clean(v) else None)(
            self.cleanup_view(run3, group3, pids3)), 10) or self.cleanup_view(run3, group3, pids3)
        code5, receipt5, _ = self.cli('stop', run5)
        self.step('stop_after_restart', run3={'exit': code, 'seconds': took3, 'receipt': receipt3, 'cleanup': view3},
                  run5={'exit': code5, 'receipt': receipt5, 'status': brief(self.status(run5))})
        self.check('restart: Stop of the recovered run is stopped and its tree is gone',
                   code == 0 and receipt3.get('code') == 'stopped' and self.is_clean(view3), json.dumps(receipt3))

        # 7. shutdown ---------------------------------------------------------------------------
        self.daemon.send_signal(signal.SIGTERM)
        try:
            self.daemon.wait(timeout=20)
        except subprocess.TimeoutExpired:
            pass
        left = self.systemctl('list-units', '--plain', '--no-legend', '--state=active', 'recursion-run-*.service')
        sock = Path(self.env['XDG_RUNTIME_DIR']) / 'recursion' / 'control.sock'
        self.step('shutdown', daemon_exit=self.daemon.returncode, active_run_units=left, socket_left=sock.exists(),
                  device_token_left=(sock.parent / 'gui-device-token').exists())
        self.check('shutdown: daemon exits 0 on SIGTERM, removes its socket and device token',
                   self.daemon.returncode == 0 and not sock.exists() and not (sock.parent / 'gui-device-token').exists())
        self.check('shutdown: no run unit is left active', not left, left)

    def finish(self):
        if self.daemon is not None and self.daemon.poll() is None:
            self.daemon.kill()
            self.daemon.wait()
        # Leave nothing behind in the user manager, whatever failed above.
        for line in self.systemctl('list-units', '--all', '--plain', '--no-legend', 'recursion-run-*.service').splitlines():
            if line.split():
                self.run(['systemctl', '--user', 'stop', '--', line.split()[0]])
        failed = [c for c in self.checks if not c['ok']]
        head = self.run(['git', '-C', str(CORE), 'rev-parse', 'HEAD']).stdout.strip()
        dirty = bool(self.run(['git', '-C', str(CORE), 'status', '--porcelain']).stdout.strip())
        report = {'harness': 'recursion_core/daemon/evidence_e2e.py', 'commit': head, 'worktree_dirty': dirty,
                  'spec': 'PRODUCT.md d84cdd26 (CSLR-D-012)', 'passed': len(self.checks) - len(failed),
                  'failed': len(failed), 'checks': self.checks, 'steps': self.steps,
                  'work_dir': str(self.work)}
        (self.out / 'evidence.json').write_text(json.dumps(report, indent=2, sort_keys=True, default=str))
        lines = ['# S12 evidence: composed daemon, end to end', '',
                 f'- commit: `{head}`' + (' (worktree dirty)' if dirty else ''),
                 '- spec: PRODUCT.md d84cdd26 (CSLR-D-012)',
                 '- engine: **stand-in engine** (no subscription, API key, model or network)',
                 '- operator Linger: ' + ('**host precondition stubbed: operator Linger (no logind on box)**'
                                          if self.linger_stubbed else 'real loginctl'),
                 f'- checks: {len(self.checks) - len(failed)} passed, {len(failed)} failed', '', '## Checks', '']
        lines += [f'- {"PASS" if c["ok"] else "**FAIL**"}: {c["check"]}' + (f' (`{c["detail"][:160]}`)' if c['detail'] else '')
                  for c in self.checks]
        lines += ['', '## Steps (full detail in evidence.json)', '']
        for entry in self.steps:
            body = {k: v for k, v in entry.items() if k not in ('step', 'at')}
            lines += [f'### {entry["step"]} ({entry["at"]})', '', '```json',
                      json.dumps(body, indent=2, sort_keys=True, default=str)[:6000], '```', '']
        (self.out / 'evidence.md').write_text('\n'.join(lines) + '\n')
        print(f'evidence: {self.out}/evidence.md  checks passed={len(self.checks) - len(failed)} failed={len(failed)}')
        return 1 if failed or not self.checks else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    parser.add_argument('--python', default=shutil.which('python3.14t') or 'python3.14t')
    parser.add_argument('--exec-dir', help='private directory on an exec-capable mount for the stand-in '
                        'engine and the stub (default: the work directory)')
    parser.add_argument('--linger-stubbed', action='store_true',
                        help='this host has no logind: put a labelled loginctl stub on the harness PATH')
    args = parser.parse_args()
    Path(args.out).mkdir(parents=True, exist_ok=True)
    harness = Harness(args.out, args.python, args.linger_stubbed, args.exec_dir)
    harness.work.mkdir(parents=True)
    try:
        harness.main()
    except Exception as exc:  # noqa: BLE001 - a harness crash is a failed check, with what was seen so far
        import traceback
        traceback.print_exc()
        harness.check('harness completed without an exception', False, f'{type(exc).__name__}: {exc}')
    return harness.finish()


if __name__ == '__main__':
    sys.exit(main())
