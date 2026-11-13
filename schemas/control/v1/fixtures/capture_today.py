#!/usr/bin/env python3
"""Capture what the daemon answers today, as fixtures for run.record.v1, run.view.v1 and stop.result.v1.

Run from the repository root, on Linux, with the core on the path and the daemon's own interpreter:

    PYTHONPATH=core python -X gil=0 -B schemas/control/v1/fixtures/capture_today.py

It drives the composed daemon the way ``core/recursion_core/daemon/tests/test_composition.py`` does: the
real control API, registry, launcher, socket and route adapter, with the stand-in engine and a child
process in place of a systemd unit. No engine, model, network or credential is used. Every answer is read
over the control socket; every stored record is read back from the registry file.

One change is made to what was captured: the temporary state directory's path is replaced by
``/var/lib/recursion-capture``, so no local path is published. Run ids, pids, cgroup markers and times
differ on every capture. The committed files are one capture; the conformance client checks that they
validate, and ``index.json`` beside this file says what each one shows.

This file is a capture tool. It is not part of the conformance client and it is the only file under
``schemas/`` that imports the core.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import unittest
from pathlib import Path

from recursion_core.daemon.tests import test_composition as tc

HERE = Path(__file__).resolve().parent
PUBLISHED_ROOT = '/var/lib/recursion-capture'


class Capture(tc.ComposedDaemonTest):
    def runTest(self):  # the base class is used for its set-up and clean-up only
        pass


def capture(out_dir: Path, with_exchanges: bool = False) -> dict[str, object]:
    case = Capture('runTest')
    case.setUp()
    files: dict[str, object] = {}
    try:
        daemon, composed = case.boot(confirm_timeout_s=0.3)
        api = case.api(composed)

        def exchange(name, method, params, socket=None):
            """One request and its whole answer, kept as a fixture of the method table."""
            answer = (socket or api).call(method, params)
            files[f'good/methods.v1/{name}.json'] = {'method': method, 'params': params, 'answer': answer}
            return answer

        def status(run_id):
            return api.call('run.status', {'run_id': run_id})['run']

        def stored(run_id):
            return json.loads(dict(daemon.registry.control_all())[run_id])

        def unit(run_id):
            return f'recursion-run-{run_id}.service'

        # A keyed start, read while it runs.
        started = api.call('run.start', {'tenant': 't1', 'engine': 'claude_code', 'prompt': 'helper=0 fast=1',
                                         'client_key': 'capture-1'})
        assert started['code'] == 'started', started
        run_a = started['run']['run_id']
        files['good/run.view.v1/c01-start-answer-running.json'] = started['run']
        assert tc.until(lambda: (status(run_a).get('output') or {}).get('lines'))
        files['good/run.view.v1/c02-status-running-with-output.json'] = status(run_a)
        files['good/run.record.v1/c01-running-keyed.json'] = stored(run_a)
        files['good/run.view.v1/c03-list-entry-running.json'] = api.call('run.list')['runs'][0]

        # A Stop that cannot be confirmed yet: the unit stays, so the answer is pending.
        case.units.stubborn.add(unit(run_a))
        pending = api.call('run.stop', {'run_id': run_a})
        assert pending['code'] == 'pending', pending
        files['good/stop.result.v1/c01-pending-cgroup-populated.json'] = pending
        files['good/run.view.v1/c04-status-stop-pending.json'] = status(run_a)
        files['good/run.record.v1/c02-stop-pending.json'] = stored(run_a)
        case.units.stubborn.clear()
        finished = api.call('run.stop', {'run_id': run_a})
        assert finished['code'] == 'stopped', finished
        files['good/stop.result.v1/c02-stopped-after-pending.json'] = finished
        files['good/run.view.v1/c05-status-stopped.json'] = status(run_a)
        files['good/run.record.v1/c03-stopped.json'] = stored(run_a)
        files['good/stop.result.v1/c03-stopped-again.json'] = api.call('run.stop', {'run_id': run_a})

        # A first Stop that is confirmed at once.
        run_b = case.start(api)
        first = api.call('run.stop', {'run_id': run_b})
        assert first['code'] == 'stopped', first
        files['good/stop.result.v1/c04-stopped-first.json'] = first

        # A run that finishes on its own: nobody stopped it, so it reads "ended", never "stopped".
        run_c = case.start(api, 'helper=0 fast=1 ticks=3')
        assert tc.until(lambda: status(run_c).get('result'))
        assert tc.until(lambda: daemon.reconcile(run_c).state == 'terminated')
        files['good/run.view.v1/c06-status-ended-with-result.json'] = status(run_c)
        files['good/run.record.v1/c04-running-after-the-run-ended.json'] = stored(run_c)
        listed = {run['run_id']: run for run in api.call('run.list')['runs']}
        files['good/run.view.v1/c07-list-entry-ended.json'] = listed[run_c]

        # A start the launch path refused: no unit was ever recorded.
        refused = api.call('run.start', {'tenant': 't1', 'engine': 'pi', 'prompt': 'x'})
        assert refused['code'] == 'start_failed', refused
        files['good/run.view.v1/c08-status-start-failed.json'] = status(refused['run_id'])
        files['good/run.record.v1/c05-start-failed.json'] = stored(refused['run_id'])

        # Whole exchanges of the six methods the daemon answers today, refusals included. They are fixtures of
        # the method table, which is not in this folder yet: capture them only when asked (--with-exchanges).
        keyed = {'tenant': 't1', 'engine': 'claude_code', 'prompt': 'helper=0 fast=1', 'client_key': 'capture-2'}
        if not with_exchanges:
            exchange = lambda name, method, params: api.call(method, params)  # noqa: E731 - same calls, nothing kept
        run_x = exchange('c01-run-start-started', 'run.start', keyed)['run']['run_id']
        exchange('c02-run-start-same-key-again', 'run.start', keyed)
        exchange('c03-run-start-key-reused-for-another-prompt', 'run.start', dict(keyed, prompt='another prompt'))
        exchange('c04-run-start-no-route-for-the-engine', 'run.start', {'tenant': 't1', 'engine': 'pi', 'prompt': 'x'})
        exchange('c05-run-start-without-a-prompt', 'run.start', {'tenant': 't1', 'engine': 'claude_code'})
        assert tc.until(lambda: (status(run_x).get('output') or {}).get('lines'))
        exchange('c06-run-status', 'run.status', {'run_id': run_x})
        exchange('c07-run-status-of-no-run', 'run.status', {'run_id': 'run-none'})
        exchange('c08-run-status-without-a-run-id', 'run.status', {})
        exchange('c09-run-list-one-page', 'run.list', {'limit': 2})
        exchange('c10-run-list-after-an-unknown-run', 'run.list', {'after': 'run-none'})
        exchange('c11-run-list-limit-out-of-range', 'run.list', {'limit': 0})
        case.units.stubborn.add(unit(run_x))
        exchange('c12-run-stop-pending', 'run.stop', {'run_id': run_x})
        case.units.stubborn.clear()
        exchange('c13-run-stop-stopped', 'run.stop', {'run_id': run_x})
        exchange('c14-run-stop-of-no-run', 'run.stop', {'run_id': 'run-none'})
        exchange('c15-route-list', 'route.list', {})
        exchange('c16-plugin-list', 'plugin.list', {})
        exchange('c17-a-method-the-daemon-does-not-know', 'run.kill', {})

        # A unit the manager can no longer observe is never reported running.
        run_d = case.start(api)
        proc = case.units.procs.pop(unit(run_d))
        try:
            daemon.sweep()
            files['good/run.view.v1/c09-status-unknown.json'] = status(run_d)
        finally:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()

        # A run of an earlier boot: its processes cannot exist now, and nothing saw them end.
        run_e = case.start(api)
        case.shutdown()
        tc.BOOT = 'boot-2'
        daemon, composed = case.boot(confirm_timeout_s=0.3)
        api = case.api(composed)
        files['good/run.view.v1/c10-status-lost-reboot.json'] = status(run_e)
    finally:
        tc.BOOT = 'boot-1'
        case.doCleanups()

    local_root = str(case.tmp)
    written = {}
    for name, value in sorted(files.items()):
        text = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False).replace(local_root, PUBLISHED_ROOT)
        path = out_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + '\n', encoding='utf-8')
        written[name] = json.loads(text)
    return written


def main(argv: list[str]) -> int:
    with_exchanges = '--with-exchanges' in argv
    paths = [arg for arg in argv[1:] if not arg.startswith('--')]
    out_dir = Path(paths[0]) if paths else HERE
    try:
        written = capture(out_dir, with_exchanges)
    except unittest.SkipTest as skipped:
        print(f'not captured: {skipped}')
        return 2
    for name in written:
        print(f'captured {name}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
