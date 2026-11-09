"""B6 (CSL-728, F23) acceptance contract over recorded host evidence (Lucas, 2026-11-08).

This module runs no probe. It only judges an evidence record that a founder's host
run produced (or a structural fixture in tests) and never touches systemd, cgroups
or another uid. A record whose ``evidence_class`` is not ``host`` can at best be
``structural``: it checks the record's schema and the contract's logic, not kernel
or systemd behaviour. Nothing here is a measurement.

Bars (all kept from 2026-11-07, plus the 2026-11-08 migration probe):

- escapers 0/120;
- engine writes to ``cgroup.procs`` refused 10/10;
- a ``systemd-run --user`` spawn refused or inside the leaf 10/10;
- ``terminated`` never booked while ``populated`` is 1;
- migration probe, n=10 per method, with the engine user manager absent: a process
  already in the operator's leaf, as the engine uid, tries
  ``systemd-run --user --scope -- sleep 60``, a direct write of its own pid to a
  ``cgroup.procs`` outside the leaf, and an unprivileged ``AttachProcessesToUnit``
  call on the system bus for its own pid (O65: PID 1 checks only process
  ownership). Each attempt runs from its own caller process and records the
  caller's uid and pid (a pid repeated within an arm fails: one trial copied N
  times is not N trials),
  the caller's cgroup before the attempt (it must already be in the leaf), and the
  run's expected pids. A manager-absent scope attempt fails when its exit code is
  0, or when ``sleep_cgroup`` is set and lies outside the leaf. A missing
  ``sleep_cgroup`` with a non-zero exit does not by itself fail that check.
  A direct write and an attach call fail unless ``refused`` is true. Afterwards
  ``/proc/<pid>/cgroup`` names the leaf for every expected pid in the run;
- the same probe, all three methods, n=10 each, with the engine user manager present
  is a control. Its scope attempts must reproduce the 2026-11-07 escape (exit 0, sleep in
  a canonical cgroup outside the leaf); its direct writes and system-bus attach calls
  are recorded with an explicit outcome either way. A control whose scope attempts do not all escape means the
  probe is not reaching the user manager: the record is ``invalid``, never a pass;
- B33: in the cross-uid spawn path, ``PR_SET_PDEATHSIG`` is armed after the uid
  switch (the kernel clears it on a credential change) and before exec, from one
  long-lived spawning thread: the signal fires when the forking *thread* exits, so
  the record names that thread's tid, every spawn's tid must equal it, and it must
  be marked long-lived. Cleanup
  still never relies on it (cgroup + pidfd, CSL-706); this same-uid launcher does
  not arm it at all.

Malformed records fail closed; ``evaluate`` never raises on them.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

N_ESCAPERS, N_PROBE = 120, 10
SCOPE_ARGV = ('systemd-run', '--user', '--scope', '--', 'sleep', '60')
HOST_FIELDS = ('host', 'kernel', 'systemd', 'captured_at', 'command_log_sha256')
METHODS = ('scope', 'direct_write', 'attach_unit')
ATTACH_CALL = 'org.freedesktop.systemd1.Manager.AttachProcessesToUnit'
SHA256_HEX = re.compile(r'[0-9a-f]{64}')


def _int(value, minimum=0):
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _canonical(path):
    """An absolute, canonical cgroup path: no '.', '..', '//', trailing '/' or NUL."""
    if not isinstance(path, str) or not path.startswith('/') or '\0' in path:
        return False
    if path != '/' and (path.endswith('/') or '//' in path):
        return False
    parts = PurePosixPath(path).parts
    return '..' not in parts and '.' not in path.split('/') and str(PurePosixPath(path)) == path


def _inside(path, leaf):
    if not (_canonical(path) and _canonical(leaf)):
        return False
    p, l = PurePosixPath(path), PurePosixPath(leaf)
    return p == l or l in p.parents


def _count(value, n):
    if not isinstance(value, dict):
        return None
    ok, total = value.get('ok'), value.get('total')
    if not (_int(ok) and _int(total)) or total != n or ok > total:
        return None
    return ok


def _pid_key(key):
    """A positive pid. Non-ASCII digits pass ``str.isdigit`` and must not raise."""
    if isinstance(key, bool):
        return None
    if isinstance(key, int):
        return key if key > 0 else None
    if isinstance(key, str) and key.isascii() and key.isdigit() and not key.startswith('0'):
        return int(key)
    return None


def _shape(attempt, leaf, engine_uid):
    """Common migration-attempt fields. Returns (ok, reason, run_pids as {int: path})."""
    if not isinstance(attempt, dict):
        return False, 'attempt is not a mapping', {}
    if attempt.get('uid') != engine_uid or not _int(attempt.get('uid'), 1):
        return False, 'attempt did not run as the engine uid', {}
    pid = attempt.get('pid')
    if not _int(pid, 1):
        return False, 'caller pid missing or invalid', {}
    if not _inside(attempt.get('initial_cgroup'), leaf):
        return False, 'caller was not inside the leaf before the attempt', {}
    expected = attempt.get('expected_pids')
    if not isinstance(expected, list) or not expected or not all(_int(p, 1) for p in expected) \
            or len(set(expected)) != len(expected) or pid not in expected:
        return False, 'expected run pids missing, invalid, or without the caller', {}
    pids = attempt.get('run_pids')
    if not isinstance(pids, dict):
        return False, 'no /proc/<pid>/cgroup read-back for the run', {}
    readback = {}
    for key, group in pids.items():
        k = _pid_key(key)
        if k is None or k in readback:
            return False, f'invalid pid in read-back: {key!r}', {}
        readback[k] = group
    if set(readback) != set(expected):
        return False, 'read-back pids differ from the expected run pids', {}
    if attempt.get('method') == 'scope':
        if not isinstance(attempt.get('argv'), list) or tuple(attempt['argv']) != SCOPE_ARGV:
            return False, 'scope argv differs from the contract', {}
        code = attempt.get('exit_code')
        if isinstance(code, bool) or not isinstance(code, int):
            return False, 'scope exit code missing or not an int', {}
    elif attempt.get('method') == 'direct_write':
        target = attempt.get('target')
        if not _canonical(target) or _inside(target, leaf) or not target.endswith('/cgroup.procs'):
            return False, 'direct write target is not a canonical cgroup.procs outside the leaf', {}
        if attempt.get('written_pid') != pid:
            return False, 'direct write did not write the caller\'s own pid', {}
        if not isinstance(attempt.get('refused'), bool):
            return False, 'direct write outcome not recorded', {}
    elif attempt.get('method') == 'attach_unit':
        if attempt.get('bus') != 'system' or attempt.get('call') != ATTACH_CALL:
            return False, 'attach attempt is not AttachProcessesToUnit on the system bus', {}
        unit = attempt.get('unit')
        if not isinstance(unit, str) or not unit or '/' in unit or '\0' in unit:
            return False, 'attach target unit missing or invalid', {}
        if not isinstance(attempt.get('subcgroup'), str):
            return False, 'attach subcgroup argument not recorded', {}
        if attempt.get('pids') != [pid]:
            return False, 'attach call did not name exactly the caller\'s own pid', {}
        if not isinstance(attempt.get('refused'), bool):
            return False, 'attach outcome not recorded', {}
    else:
        return False, f'unknown method {attempt.get("method")!r}', {}
    return True, '', readback


def _absent_attempt(attempt, leaf, engine_uid):
    ok, why, readback = _shape(attempt, leaf, engine_uid)
    if not ok:
        return False, why
    if attempt['method'] == 'scope':
        if attempt['exit_code'] == 0:
            return False, 'systemd-run --user --scope succeeded'
        sleep = attempt.get('sleep_cgroup')
        if sleep is not None and not _inside(sleep, leaf):
            return False, f'sleep landed outside the leaf: {sleep!r}'
    elif attempt['method'] == 'direct_write' and attempt['refused'] is not True:
        return False, f'write of own pid to {attempt["target"]} was not refused'
    elif attempt['method'] == 'attach_unit' and attempt['refused'] is not True:
        return False, f'AttachProcessesToUnit into {attempt["unit"]} was not refused'
    stray = sorted(str(p) for p, group in readback.items() if not _inside(group, leaf))
    if stray:
        return False, f'pids outside the leaf after the attempt: {", ".join(stray)}'
    return True, ''


def _control_attempt(attempt, leaf, engine_uid):
    ok, why, _ = _shape(attempt, leaf, engine_uid)
    if not ok:
        return False, why
    if attempt['method'] == 'scope':
        sleep = attempt.get('sleep_cgroup')
        if attempt['exit_code'] != 0 or not _canonical(sleep) or _inside(sleep, leaf):
            return False, 'scope control did not escape to a canonical cgroup outside the leaf'
    return True, ''  # direct-write and attach controls: shape and explicit outcome only


def _attempts(arm):
    if not isinstance(arm, dict):
        return None
    attempts = arm.get('attempts')
    return attempts if isinstance(attempts, list) else None


def _by_method(attempts, method):
    return [a for a in attempts if isinstance(a, dict) and a.get('method') == method]


def _distinct_callers(attempts):
    """Each attempt is its own process: a caller pid repeated within an arm means one
    trial was copied, not N_PROBE trials run (O65 10/10)."""
    pids = [a.get('pid') for a in attempts if isinstance(a, dict) and _int(a.get('pid'), 1)]
    return len(pids) == len(set(pids))


def _spawner(order, spawner):
    """B33: armed after the uid switch, before exec, from one long-lived spawning thread."""
    if not isinstance(order, list) or not all(isinstance(s, str) for s in order) \
            or not all(order.count(s) == 1 for s in ('uid_switch', 'pdeathsig', 'exec')) \
            or not order.index('uid_switch') < order.index('pdeathsig') < order.index('exec'):
        return 'PR_SET_PDEATHSIG must be armed after the uid switch and before exec (B33)'
    if not isinstance(spawner, dict) or not _int(spawner.get('tid'), 1):
        return 'spawning thread tid not recorded (B33)'
    if spawner.get('long_lived') is not True:
        return 'spawning thread not recorded as long-lived (B33)'
    tids = spawner.get('spawn_tids')
    if not isinstance(tids, list) or not tids or not all(_int(t, 1) for t in tids) \
            or any(t != spawner['tid'] for t in tids):
        return 'spawns did not all come from the one long-lived spawning thread (B33)'
    return None


def evaluate(record):
    """Return {'verdict': pass|fail|invalid|structural, 'reasons': [...]}.

    ``pass`` needs host-class evidence and every bar. ``invalid`` means the manager-
    present control did not escape, so the manager-absent result proves nothing.
    """
    if not isinstance(record, dict):
        return {'verdict': 'fail', 'reasons': ['record is not a mapping']}
    leaf = record.get('leaf')
    if not _canonical(leaf) or leaf == '/':
        return {'verdict': 'fail', 'reasons': ['leaf path missing or not canonical']}
    engine_uid, operator_uid = record.get('engine_uid'), record.get('operator_uid')
    if not (_int(engine_uid, 1) and _int(operator_uid) and engine_uid != operator_uid):
        return {'verdict': 'fail', 'reasons': ['engine_uid must be a non-root uid different from operator_uid']}
    reasons = []

    escapers = record.get('escapers')
    if not isinstance(escapers, dict) or escapers.get('total') != N_ESCAPERS \
            or not _int(escapers.get('total')) or escapers.get('escaped') != 0 \
            or not _int(escapers.get('escaped')):
        reasons.append('escapers must be 0/120')
    if _count(record.get('procs_write_refused'), N_PROBE) != N_PROBE:
        reasons.append('engine cgroup.procs writes must be refused 10/10')
    if _count(record.get('user_spawn_refused_or_contained'), N_PROBE) != N_PROBE:
        reasons.append('systemd-run --user must be refused or leaf-contained 10/10')
    populated = record.get('terminated_while_populated')
    if populated != 0 or not _int(populated):
        reasons.append('terminated booked while populated=1 (or not recorded)')

    absent = record.get('manager_absent')
    attempts = _attempts(absent)
    if attempts is None:
        reasons.append('manager-absent arm missing or malformed')
        attempts = []
    elif absent.get('user_manager') != 'inactive' or absent.get('manager_socket') != 'absent':
        reasons.append('manager-absent arm did not record user@ inactive and socket absent')
    for method in METHODS:
        mine = _by_method(attempts, method)
        if len(mine) != N_PROBE:
            reasons.append(f'migration {method}: need n={N_PROBE}, got {len(mine)}')
        for i, attempt in enumerate(mine):
            ok, why = _absent_attempt(attempt, leaf, engine_uid)
            if not ok:
                reasons.append(f'migration {method} #{i + 1}: {why}')
    if len(attempts) != sum(len(_by_method(attempts, m)) for m in METHODS):
        reasons.append('manager-absent attempts include an unknown or malformed entry')
    if not _distinct_callers(attempts):
        reasons.append('manager-absent attempts repeat a caller pid: trials are not distinct')

    b33 = _spawner(record.get('spawn_order'), record.get('pdeathsig_spawner'))
    if b33:
        reasons.append(b33)

    present = record.get('manager_present')
    controls = _attempts(present)
    control_reasons = []
    if controls is None or present.get('user_manager') != 'active':
        control_reasons.append('manager-present arm missing, malformed or not active')
        controls = []
    for method in METHODS:
        mine = _by_method(controls, method)
        if len(mine) != N_PROBE:
            control_reasons.append(f'control {method}: need n={N_PROBE}, got {len(mine)}')
        for i, attempt in enumerate(mine):
            ok, why = _control_attempt(attempt, leaf, engine_uid)
            if not ok:
                control_reasons.append(f'control {method} #{i + 1}: {why}')
    if len(controls) != sum(len(_by_method(controls, m)) for m in METHODS):
        control_reasons.append('control attempts include an unknown or malformed entry')
    if not _distinct_callers(controls):
        control_reasons.append('control attempts repeat a caller pid: trials are not distinct')

    if record.get('evidence_class') != 'host':
        return {'verdict': 'structural', 'reasons': reasons + control_reasons
                + ['not host evidence: structural check only, no B6 pass']}
    missing = [f for f in HOST_FIELDS if not (isinstance(record.get(f), str) and record.get(f))]
    if missing:
        return {'verdict': 'fail', 'reasons': reasons + control_reasons + [f'host fields missing: {missing}']}
    if not SHA256_HEX.fullmatch(record['command_log_sha256']):
        return {'verdict': 'fail', 'reasons': reasons + control_reasons + [
            'command_log_sha256 is not a lowercase hex SHA-256 digest']}
    if control_reasons:
        return {'verdict': 'invalid', 'reasons': reasons + control_reasons + [
            'manager-present control did not reproduce the escape via systemd-run --user --scope '
            '(or is incomplete): the probe is not reaching the user manager; fix the probe before '
            'reading a pass']}
    return {'verdict': 'fail' if reasons else 'pass', 'reasons': reasons}
