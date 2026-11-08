"""Structural tests for the B6 evidence contract (Lucas, Nov 8).

The records here are hand-built fixtures, not host evidence. They show which records
the contract accepts or rejects; they say nothing about kernel or systemd behaviour.
No systemd, cgroup write, uid switch or root command runs here.
"""
import copy
import unittest

from recursion_core.cgroup_leaf import b6_evidence
from recursion_core.cgroup_leaf.b6_evidence import SCOPE_ARGV, evaluate

LEAF = '/system.slice/csl-b6-wrapper-rerun.service/run-probe'
OUTSIDE = '/user.slice/user-1000.slice/user@1000.service/app.slice/run-x.scope'


def absent_attempts():
    pids = {'4101': LEAF, '4102': LEAF + '/sub'}
    scope = [{'method': 'scope', 'argv': list(SCOPE_ARGV), 'exit_code': 1,
              'sleep_cgroup': None, 'run_pids': dict(pids)} for _ in range(10)]
    write = [{'method': 'direct_write', 'target': '/user.slice/cgroup.procs', 'refused': True,
              'errno': 'EACCES', 'run_pids': dict(pids)} for _ in range(10)]
    return scope + write


def control_attempts():
    return [{'method': 'scope', 'argv': list(SCOPE_ARGV), 'exit_code': 0,
             'sleep_cgroup': OUTSIDE, 'run_pids': {'5101': LEAF}} for _ in range(10)]


def host_record():
    return {
        'evidence_class': 'host', 'host': 'gs-worker-nbg', 'kernel': 'k', 'systemd': '259',
        'captured_at': '2026-11-08T00:00:00Z', 'command_log_sha256': 'a' * 64,
        'leaf': LEAF,
        'escapers': {'escaped': 0, 'total': 120},
        'procs_write_refused': {'ok': 10, 'total': 10},
        'user_spawn_refused_or_contained': {'ok': 10, 'total': 10},
        'terminated_while_populated': 0,
        'spawn_order': ['fork', 'uid_switch', 'pdeathsig', 'exec'],
        'manager_absent': {'user_manager': 'inactive', 'manager_socket': 'absent',
                           'attempts': absent_attempts()},
        'manager_present': {'user_manager': 'active', 'attempts': control_attempts()},
    }


class ContractTests(unittest.TestCase):
    def verdict(self, record):
        return evaluate(record)['verdict']

    def test_complete_host_record_passes(self):
        self.assertEqual(evaluate(host_record()), {'verdict': 'pass', 'reasons': []})

    def test_fixture_class_is_structural_never_pass(self):
        for cls in ('structural_fixture', None, 'HOST', ''):
            record = host_record()
            record['evidence_class'] = cls
            with self.subTest(cls=cls):
                self.assertEqual(self.verdict(record), 'structural')

    def test_host_fields_required(self):
        for field in b6_evidence.HOST_FIELDS:
            record = host_record()
            del record[field]
            with self.subTest(field=field):
                self.assertEqual(self.verdict(record), 'fail')

    def test_existing_bars_kept(self):
        cases = [('escapers', {'escaped': 1, 'total': 120}),
                 ('escapers', {'escaped': 0, 'total': 119}),
                 ('procs_write_refused', {'ok': 9, 'total': 10}),
                 ('procs_write_refused', {'ok': 10, 'total': 9}),
                 ('procs_write_refused', {'ok': True, 'total': 10}),
                 ('user_spawn_refused_or_contained', {'ok': 9, 'total': 10}),
                 ('terminated_while_populated', 1),
                 ('terminated_while_populated', None)]
        for key, value in cases:
            record = host_record()
            record[key] = value
            with self.subTest(key=key, value=value):
                self.assertEqual(self.verdict(record), 'fail')

    def test_scope_escape_in_absent_arm_fails(self):
        record = host_record()
        record['manager_absent']['attempts'][3].update(exit_code=0, sleep_cgroup=OUTSIDE)
        result = evaluate(record)
        self.assertEqual(result['verdict'], 'fail')
        self.assertTrue(any('scope #4' in r for r in result['reasons']))

    def test_scope_exit_zero_is_failure_even_if_contained(self):
        # The migration bar is "every attempt fails", stricter than "refused or contained".
        record = host_record()
        record['manager_absent']['attempts'][0].update(exit_code=0, sleep_cgroup=LEAF)
        self.assertEqual(self.verdict(record), 'fail')

    def test_direct_write_accepted_fails(self):
        record = host_record()
        record['manager_absent']['attempts'][12]['refused'] = False
        self.assertEqual(self.verdict(record), 'fail')

    def test_direct_write_target_must_be_outside_leaf(self):
        record = host_record()
        record['manager_absent']['attempts'][10]['target'] = LEAF + '/cgroup.procs'
        self.assertEqual(self.verdict(record), 'fail')

    def test_every_run_pid_must_stay_in_leaf(self):
        for index in (0, 15):
            record = host_record()
            record['manager_absent']['attempts'][index]['run_pids']['4102'] = OUTSIDE
            with self.subTest(index=index):
                self.assertEqual(self.verdict(record), 'fail')
        record = host_record()
        record['manager_absent']['attempts'][1]['run_pids']['4103'] = LEAF + '/../escape'
        self.assertEqual(self.verdict(record), 'fail')
        record = host_record()
        record['manager_absent']['attempts'][2]['run_pids'] = {}
        self.assertEqual(self.verdict(record), 'fail')

    def test_sibling_prefix_is_not_inside(self):
        record = host_record()
        record['manager_absent']['attempts'][0]['run_pids']['4101'] = LEAF + '-evil'
        self.assertEqual(self.verdict(record), 'fail')

    def test_n10_per_method(self):
        for drop in (0, 19):
            record = host_record()
            del record['manager_absent']['attempts'][drop]
            with self.subTest(drop=drop):
                self.assertEqual(self.verdict(record), 'fail')
        record = host_record()
        record['manager_absent']['attempts'].append({'method': 'ptrace'})
        self.assertEqual(self.verdict(record), 'fail')

    def test_wrong_scope_argv_fails(self):
        record = host_record()
        record['manager_absent']['attempts'][0]['argv'] = ['systemd-run', '--user', 'sleep', '60']
        self.assertEqual(self.verdict(record), 'fail')

    def test_absent_arm_must_record_manager_absent(self):
        for key, value in (('user_manager', 'active'), ('manager_socket', 'present')):
            record = host_record()
            record['manager_absent'][key] = value
            with self.subTest(key=key):
                self.assertEqual(self.verdict(record), 'fail')

    def test_control_that_does_not_escape_is_invalid(self):
        mutations = [
            lambda c: c['attempts'][0].update(exit_code=1),
            lambda c: c['attempts'][4].update(sleep_cgroup=LEAF),
            lambda c: c['attempts'].pop(),
            lambda c: c.update(user_manager='inactive'),
            lambda c: c.update(attempts=[]),
        ]
        for i, mutate in enumerate(mutations):
            record = host_record()
            mutate(record['manager_present'])
            with self.subTest(case=i):
                result = evaluate(record)
                self.assertEqual(result['verdict'], 'invalid')
                self.assertTrue(any('not reaching the user manager' in r
                                    for r in result['reasons']))

    def test_invalid_control_never_pass_even_if_absent_arm_clean(self):
        record = host_record()
        record['manager_present'] = {}
        self.assertNotEqual(self.verdict(record), 'pass')

    def test_pdeathsig_after_uid_switch(self):
        for order in (['pdeathsig', 'uid_switch', 'exec'], ['uid_switch', 'exec', 'pdeathsig'],
                      ['uid_switch', 'exec'], None):
            record = host_record()
            record['spawn_order'] = order
            with self.subTest(order=order):
                self.assertEqual(self.verdict(record), 'fail')

    def test_malformed_records_fail(self):
        self.assertEqual(self.verdict(None), 'fail')
        self.assertEqual(self.verdict({'leaf': 'relative/leaf'}), 'fail')
        self.assertEqual(self.verdict({**host_record(), 'manager_absent': None}), 'fail')

    def test_evaluate_does_not_mutate(self):
        record = host_record()
        before = copy.deepcopy(record)
        evaluate(record)
        self.assertEqual(record, before)

    def test_module_runs_nothing(self):
        source = open(b6_evidence.__file__).read()
        for forbidden in ('import subprocess', 'import os', 'Popen', 'os.system',
                          'open(', 'setresuid', 'prctl('):
            self.assertNotIn(forbidden, source)


if __name__ == '__main__':
    unittest.main()
