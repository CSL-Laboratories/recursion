"""B6 (CSL-728, F23) acceptance contract over recorded host evidence (Lucas, Nov 8).

This module runs no probe. It only judges an evidence record that a founder's host
run produced (or a structural fixture in tests) and never touches systemd, cgroups
or another uid. A record whose ``evidence_class`` is not ``host`` can at best be
``structural``: it checks the contract's shape, not kernel or systemd behaviour.

Bars (all kept from Nov 7, plus the Nov 8 migration probe):

- escapers 0/120;
- engine writes to ``cgroup.procs`` refused 10/10;
- a ``systemd-run --user`` spawn refused or inside the leaf 10/10;
- ``terminated`` never booked while ``populated`` is 1;
- migration probe, n=10 per method, with the engine user manager absent: a process
  already in the operator's leaf, as the engine uid, tries
  ``systemd-run --user --scope -- sleep 60`` and a direct write of its own pid to a
  ``cgroup.procs`` outside the leaf. Every attempt fails, and afterwards
  ``/proc/<pid>/cgroup`` names the leaf for every pid in the run;
- the same probe with the engine user manager present is a control and must
  reproduce the Nov 7 escape through ``systemd-run --user --scope``. A control that
  does not escape means the probe is not reaching the user manager: the record is
  ``invalid``, never a pass;
- B33: in the cross-uid spawn path, ``PR_SET_PDEATHSIG`` is armed after the uid
  switch (the kernel clears it on a credential change) and before exec. Cleanup
  still never relies on it (cgroup + pidfd, CSL-706); this same-uid launcher does
  not arm it at all.
"""
from __future__ import annotations

from pathlib import PurePosixPath

N_ESCAPERS, N_PROBE = 120, 10
SCOPE_ARGV = ('systemd-run', '--user', '--scope', '--', 'sleep', '60')
HOST_FIELDS = ('host', 'kernel', 'systemd', 'captured_at', 'command_log_sha256')
METHODS = ('scope', 'direct_write')


def _inside(path, leaf):
    try:
        p, l = PurePosixPath(path), PurePosixPath(leaf)
    except TypeError:
        return False
    return p.is_absolute() and '..' not in p.parts and (p == l or l in p.parents)


def _count(value, n):
    """(ok, total) pairs must be exactly n trials with integer counts."""
    if not isinstance(value, dict):
        return None
    ok, total = value.get('ok'), value.get('total')
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (ok, total)):
        return None
    if total != n or not 0 <= ok <= total:
        return None
    return ok


def _attempt_failed(attempt, leaf):
    """One manager-absent migration attempt: refused, and every run pid still in the leaf."""
    method = attempt.get('method')
    if method == 'scope':
        if tuple(attempt.get('argv', ())) != SCOPE_ARGV:
            return False, 'scope argv differs from the contract'
        if not isinstance(attempt.get('exit_code'), int) or attempt['exit_code'] == 0:
            return False, 'systemd-run --user --scope succeeded'
        sleep_cgroup = attempt.get('sleep_cgroup')
        if sleep_cgroup is not None and not _inside(sleep_cgroup, leaf):
            return False, f'sleep landed outside the leaf: {sleep_cgroup}'
    elif method == 'direct_write':
        target = attempt.get('target')
        if not isinstance(target, str) or _inside(target, leaf) or not target.endswith('/cgroup.procs'):
            return False, 'direct write target is not a cgroup.procs outside the leaf'
        if attempt.get('refused') is not True:
            return False, f'write of own pid to {target} was not refused'
    else:
        return False, f'unknown method {method!r}'
    pids = attempt.get('run_pids')
    if not isinstance(pids, dict) or not pids:
        return False, 'no /proc/<pid>/cgroup read-back for the run'
    stray = sorted(str(pid) for pid, group in pids.items() if not _inside(group, leaf))
    if stray:
        return False, f'pids outside the leaf after the attempt: {", ".join(stray)}'
    return True, ''


def _control_escaped(attempt, leaf):
    return (attempt.get('method') == 'scope'
            and tuple(attempt.get('argv', ())) == SCOPE_ARGV
            and attempt.get('exit_code') == 0
            and isinstance(attempt.get('sleep_cgroup'), str)
            and not _inside(attempt['sleep_cgroup'], leaf))


def _per_method(attempts, method):
    return [a for a in attempts if isinstance(a, dict) and a.get('method') == method]


def evaluate(record):
    """Return {'verdict': pass|fail|invalid|structural, 'reasons': [...]}.

    ``pass`` needs host-class evidence and every bar. ``invalid`` means the manager-
    present control did not escape, so the manager-absent result proves nothing.
    """
    reasons = []
    if not isinstance(record, dict):
        return {'verdict': 'fail', 'reasons': ['record is not a mapping']}
    leaf = record.get('leaf')
    if not isinstance(leaf, str) or not PurePosixPath(leaf).is_absolute():
        return {'verdict': 'fail', 'reasons': ['leaf path missing']}

    escapers = record.get('escapers')
    if not isinstance(escapers, dict) or escapers.get('total') != N_ESCAPERS \
            or escapers.get('escaped') != 0:
        reasons.append('escapers must be 0/120')
    if _count(record.get('procs_write_refused'), N_PROBE) != N_PROBE:
        reasons.append('engine cgroup.procs writes must be refused 10/10')
    if _count(record.get('user_spawn_refused_or_contained'), N_PROBE) != N_PROBE:
        reasons.append('systemd-run --user must be refused or leaf-contained 10/10')
    if record.get('terminated_while_populated') != 0:
        reasons.append('terminated booked while populated=1')

    absent = record.get('manager_absent') or {}
    if absent.get('user_manager') != 'inactive' or absent.get('manager_socket') != 'absent':
        reasons.append('manager-absent arm did not record user@ inactive and socket absent')
    attempts = absent.get('attempts') or []
    for method in METHODS:
        mine = _per_method(attempts, method)
        if len(mine) != N_PROBE:
            reasons.append(f'migration {method}: need n={N_PROBE}, got {len(mine)}')
        for i, attempt in enumerate(mine):
            ok, why = _attempt_failed(attempt, leaf)
            if not ok:
                reasons.append(f'migration {method} #{i + 1}: {why}')
    if len(attempts) != len(_per_method(attempts, 'scope')) + len(_per_method(attempts, 'direct_write')):
        reasons.append('manager-absent attempts include an unknown method')

    order = record.get('spawn_order')
    if not isinstance(order, list) or not {'uid_switch', 'pdeathsig', 'exec'} <= set(order) \
            or not order.index('uid_switch') < order.index('pdeathsig') < order.index('exec'):
        reasons.append('PR_SET_PDEATHSIG must be armed after the uid switch and before exec (B33)')

    present = record.get('manager_present') or {}
    controls = _per_method(present.get('attempts') or [], 'scope')
    control_valid = (present.get('user_manager') == 'active' and len(controls) == N_PROBE
                     and all(_control_escaped(a, leaf) for a in controls))

    if record.get('evidence_class') != 'host':
        return {'verdict': 'structural', 'reasons': reasons
                + ['not host evidence: structural check only, no B6 pass']}
    missing = [f for f in HOST_FIELDS if not record.get(f)]
    if missing:
        return {'verdict': 'fail', 'reasons': reasons + [f'host fields missing: {missing}']}
    if not control_valid:
        return {'verdict': 'invalid', 'reasons': reasons + [
            'manager-present control did not escape 10/10 via systemd-run --user --scope: '
            'the probe is not reaching the user manager; fix the probe before reading a pass']}
    return {'verdict': 'fail' if reasons else 'pass', 'reasons': reasons}
