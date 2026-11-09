"""Structural tests for the B6 evidence contract (Lucas, Nov 8).

The records here are hand-built fixtures, not host evidence. They check the evidence
schema and the contract's logic; they say nothing about kernel or systemd behaviour.
No systemd, cgroup write, uid switch or root command runs here.
"""
import copy
import random
import unittest

from recursion_core.cgroup_leaf import b6_evidence
from recursion_core.cgroup_leaf.b6_evidence import ATTACH_CALL, SCOPE_ARGV, evaluate

LEAF = '/system.slice/csl-b6-wrapper-rerun.service/run-probe'
OUTSIDE = '/user.slice/user-1000.slice/user@1000.service/app.slice/run-x.scope'
ENGINE_UID, OPERATOR_UID = 1000, 1001


def attempt(method, *, pid=4101, child=4102, **fields):
    base = {'method': method, 'uid': ENGINE_UID, 'pid': pid, 'initial_cgroup': LEAF,
            'expected_pids': [pid, child], 'run_pids': {str(pid): LEAF, str(child): LEAF + '/sub'}}
    if method == 'scope':
        base.update(argv=list(SCOPE_ARGV), exit_code=1, sleep_cgroup=None)
    elif method == 'direct_write':
        base.update(target='/user.slice/cgroup.procs', written_pid=pid, refused=True, errno='EACCES')
    else:
        base.update(bus='system', call=ATTACH_CALL, unit='user@1000.service', subcgroup='',
                    pids=[pid], refused=True, error='org.freedesktop.DBus.Error.AccessDenied')
    base.update(fields)
    return base


def absent_attempts():
    return ([attempt('scope') for _ in range(10)] + [attempt('direct_write') for _ in range(10)]
            + [attempt('attach_unit') for _ in range(10)])


def control_attempts():
    return ([attempt('scope', exit_code=0, sleep_cgroup=OUTSIDE) for _ in range(10)]
            + [attempt('direct_write', refused=False) for _ in range(10)]
            + [attempt('attach_unit', refused=False) for _ in range(10)])


def host_record():
    return {
        'evidence_class': 'host', 'host': 'gs-worker-nbg', 'kernel': 'k', 'systemd': '259',
        'captured_at': '2026-11-08T00:00:00Z', 'command_log_sha256': 'a' * 64,
        'leaf': LEAF, 'engine_uid': ENGINE_UID, 'operator_uid': OPERATOR_UID,
        'escapers': {'escaped': 0, 'total': 120},
        'procs_write_refused': {'ok': 10, 'total': 10},
        'user_spawn_refused_or_contained': {'ok': 10, 'total': 10},
        'terminated_while_populated': 0,
        'spawn_order': ['fork', 'uid_switch', 'pdeathsig', 'exec'],
        'pdeathsig_spawner': {'tid': 4000, 'long_lived': True, 'spawn_tids': [4000, 4000, 4000]},
        'manager_absent': {'user_manager': 'inactive', 'manager_socket': 'absent',
                           'attempts': absent_attempts()},
        'manager_present': {'user_manager': 'active', 'attempts': control_attempts()},
    }


class ContractTests(unittest.TestCase):
    def verdict(self, record):
        return evaluate(record)['verdict']

    def mutated(self, fn):
        record = host_record()
        fn(record)
        return self.verdict(record)

    def test_complete_host_record_passes(self):
        self.assertEqual(evaluate(host_record()), {'verdict': 'pass', 'reasons': []})

    def test_fixture_class_is_structural_never_pass(self):
        for cls in ('structural_fixture', None, 'HOST', ''):
            with self.subTest(cls=cls):
                self.assertEqual(self.mutated(lambda r: r.update(evidence_class=cls)), 'structural')

    def test_host_fields_required(self):
        for field in b6_evidence.HOST_FIELDS:
            with self.subTest(field=field):
                self.assertEqual(self.mutated(lambda r: r.pop(field)), 'fail')
        self.assertEqual(self.mutated(lambda r: r.update(host=True)), 'fail')

    def test_existing_bars_kept(self):
        cases = [('escapers', {'escaped': 1, 'total': 120}), ('escapers', {'escaped': 0, 'total': 119}),
                 ('escapers', {'escaped': False, 'total': 120}),
                 ('procs_write_refused', {'ok': 9, 'total': 10}), ('procs_write_refused', {'ok': 10, 'total': 9}),
                 ('procs_write_refused', {'ok': True, 'total': 10}),
                 ('user_spawn_refused_or_contained', {'ok': 9, 'total': 10}),
                 ('terminated_while_populated', 1), ('terminated_while_populated', None),
                 ('terminated_while_populated', False)]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                self.assertEqual(self.mutated(lambda r: r.update({key: value})), 'fail')

    def test_uids_required_and_distinct(self):
        for engine, operator in ((None, 1001), (0, 1001), (1000, 1000), (True, 1001), ('1000', 1001)):
            with self.subTest(engine=engine, operator=operator):
                self.assertEqual(self.mutated(lambda r: r.update(engine_uid=engine, operator_uid=operator)),
                                 'fail')

    def test_attempt_must_run_as_engine_uid_from_inside_the_leaf(self):
        for field, value in (('uid', OPERATOR_UID), ('uid', True), ('uid', None),
                             ('initial_cgroup', OUTSIDE), ('initial_cgroup', None),
                             ('initial_cgroup', 'system.slice/x'), ('pid', 0), ('pid', True)):
            with self.subTest(field=field, value=value):
                self.assertEqual(self.mutated(
                    lambda r: r['manager_absent']['attempts'][2].update({field: value})), 'fail')

    def test_scope_escape_in_absent_arm_fails(self):
        record = host_record()
        record['manager_absent']['attempts'][3].update(exit_code=0, sleep_cgroup=OUTSIDE)
        result = evaluate(record)
        self.assertEqual(result['verdict'], 'fail')
        self.assertTrue(any('scope #4' in r for r in result['reasons']))

    def test_scope_exit_zero_is_failure_even_if_contained(self):
        self.assertEqual(self.mutated(
            lambda r: r['manager_absent']['attempts'][0].update(exit_code=0, sleep_cgroup=LEAF)), 'fail')

    def test_exit_code_must_be_an_int(self):
        for code in (None, False, True, '1', 1.0):
            with self.subTest(code=code):
                self.assertEqual(self.mutated(
                    lambda r: r['manager_absent']['attempts'][0].update(exit_code=code)), 'fail')

    def test_direct_write_accepted_fails(self):
        self.assertEqual(self.mutated(lambda r: r['manager_absent']['attempts'][12].update(refused=False)), 'fail')

    def test_direct_write_target_and_pid(self):
        for fields in ({'target': LEAF + '/cgroup.procs'}, {'target': 'user.slice/cgroup.procs'},
                       {'target': '/user.slice/../x/cgroup.procs'}, {'target': '/user.slice//cgroup.procs'},
                       {'target': '/user.slice/cgroup.events'}, {'written_pid': 999}, {'refused': None},
                       {'refused': 'yes'}):
            with self.subTest(fields=fields):
                self.assertEqual(self.mutated(lambda r: r['manager_absent']['attempts'][10].update(fields)),
                                 'fail')

    def test_every_expected_pid_must_be_read_back_inside_the_leaf(self):
        cases = [lambda a: a['run_pids'].update({'4102': OUTSIDE}),
                 lambda a: a['run_pids'].update({'4102': LEAF + '/../escape'}),
                 lambda a: a['run_pids'].update({'4102': LEAF + '-evil'}),
                 lambda a: a['run_pids'].pop('4102'),                        # subset read back
                 lambda a: a['run_pids'].update({'4999': LEAF}),             # unexpected pid
                 lambda a: a.update(run_pids={}),
                 lambda a: a.update(run_pids={'abc': LEAF, '4101': LEAF}),
                 lambda a: a.update(run_pids={'04101': LEAF, '4102': LEAF}),
                 lambda a: a.update(expected_pids=[4102]),                   # caller missing
                 lambda a: a.update(expected_pids=[4101, 4101, 4102]),
                 lambda a: a.update(expected_pids=[4101, True]),
                 lambda a: a.update(expected_pids='4101')]
        for index in (0, 15, 25):
            for i, change in enumerate(cases):
                with self.subTest(index=index, case=i):
                    self.assertEqual(self.mutated(lambda r: change(r['manager_absent']['attempts'][index])),
                                     'fail')

    def test_int_pid_keys_accepted(self):
        record = host_record()
        record['manager_absent']['attempts'][0]['run_pids'] = {4101: LEAF, 4102: LEAF}
        self.assertEqual(self.verdict(record), 'pass')

    def test_n10_per_method(self):
        for drop in (0, 19, 29):
            with self.subTest(drop=drop):
                self.assertEqual(self.mutated(lambda r: r['manager_absent']['attempts'].pop(drop)), 'fail')
        self.assertEqual(self.mutated(lambda r: r['manager_absent']['attempts'].append({'method': 'ptrace'})),
                         'fail')
        self.assertEqual(self.mutated(lambda r: r['manager_absent']['attempts'].append('junk')), 'fail')

    def test_record_without_attach_probe_fails(self):
        record = host_record()  # scope and direct-write only, as at b993f986
        record['manager_absent']['attempts'] = record['manager_absent']['attempts'][:20]
        result = evaluate(record)
        self.assertEqual(result['verdict'], 'fail')
        self.assertTrue(any('attach_unit: need n=10, got 0' in r for r in result['reasons']))

    def test_attach_accepted_in_absent_arm_fails(self):
        record = host_record()
        record['manager_absent']['attempts'][22].update(refused=False)
        result = evaluate(record)
        self.assertEqual(result['verdict'], 'fail')
        self.assertTrue(any('attach_unit #3' in r for r in result['reasons']))

    def test_attach_attempt_shape(self):
        for fields in ({'bus': 'session'}, {'bus': None}, {'call': 'AttachProcessesToUnit'},
                       {'unit': ''}, {'unit': None}, {'unit': '../x.service'}, {'subcgroup': None},
                       {'pids': [999]}, {'pids': [4101, 4102]}, {'pids': 4101}, {'pids': []},
                       {'refused': None}, {'refused': 'yes'}, {'uid': OPERATOR_UID}):
            with self.subTest(fields=fields):
                self.assertEqual(self.mutated(lambda r: r['manager_absent']['attempts'][20].update(fields)),
                                 'fail')

    def test_attach_moving_a_pid_out_fails(self):
        self.assertEqual(self.mutated(
            lambda r: r['manager_absent']['attempts'][21]['run_pids'].update({'4101': OUTSIDE})), 'fail')

    def test_control_needs_n10_attach_with_recorded_outcome(self):
        for mutate in (lambda c: c['attempts'].pop(25),
                       lambda c: c['attempts'][22].pop('refused'),
                       lambda c: c['attempts'][22].update(bus='session'),
                       lambda c: c.update(attempts=c['attempts'][:20])):
            record = host_record()
            mutate(record['manager_present'])
            self.assertEqual(self.verdict(record), 'invalid')
        record = host_record()  # a refused attach in the control is fine: it is recorded either way
        for a in record['manager_present']['attempts'][20:]:
            a['refused'] = True
        self.assertEqual(self.verdict(record), 'pass')

    def test_wrong_scope_argv_fails(self):
        for argv in (['systemd-run', '--user', 'sleep', '60'], 'systemd-run --user --scope -- sleep 60', None):
            with self.subTest(argv=argv):
                self.assertEqual(self.mutated(lambda r: r['manager_absent']['attempts'][0].update(argv=argv)),
                                 'fail')

    def test_absent_arm_must_record_manager_absent(self):
        for key, value in (('user_manager', 'active'), ('manager_socket', 'present')):
            with self.subTest(key=key):
                self.assertEqual(self.mutated(lambda r: r['manager_absent'].update({key: value})), 'fail')

    def test_control_that_does_not_escape_is_invalid(self):
        mutations = [
            lambda c: c['attempts'][0].update(exit_code=1),
            lambda c: c['attempts'][4].update(sleep_cgroup=LEAF),
            lambda c: c['attempts'][4].update(sleep_cgroup='relative/x.scope'),
            lambda c: c['attempts'][4].update(sleep_cgroup='/user.slice/../x'),
            lambda c: c['attempts'][4].update(sleep_cgroup=None),
            lambda c: c['attempts'].pop(0),
            lambda c: c.update(user_manager='inactive'),
            lambda c: c.update(attempts=[]),
        ]
        for i, mutate in enumerate(mutations):
            record = host_record()
            mutate(record['manager_present'])
            with self.subTest(case=i):
                result = evaluate(record)
                self.assertEqual(result['verdict'], 'invalid')
                self.assertTrue(any('not reaching the user manager' in r for r in result['reasons']))

    def test_control_needs_n10_direct_writes_with_recorded_outcome(self):
        for mutate in (lambda c: c['attempts'].pop(15),
                       lambda c: c['attempts'][12].pop('refused'),
                       lambda c: c['attempts'][12].update(target='rel/cgroup.procs'),
                       lambda c: c['attempts'][12].update(uid=OPERATOR_UID),
                       lambda c: c.update(attempts=c['attempts'][:10])):
            record = host_record()
            mutate(record['manager_present'])
            self.assertEqual(self.verdict(record), 'invalid')
        record = host_record()  # a refused direct write in the control is fine: only scope must escape
        for a in record['manager_present']['attempts'][10:]:
            a['refused'] = True
        self.assertEqual(self.verdict(record), 'pass')

    def test_pdeathsig_after_uid_switch(self):
        for order in (['pdeathsig', 'uid_switch', 'exec'], ['uid_switch', 'exec', 'pdeathsig'],
                      ['uid_switch', 'exec'], ['uid_switch', 'pdeathsig', 'exec', 'pdeathsig'],
                      'uid_switch,pdeathsig,exec', None, [1, 2]):
            with self.subTest(order=order):
                self.assertEqual(self.mutated(lambda r: r.update(spawn_order=order)), 'fail')

    def test_pdeathsig_from_one_long_lived_spawning_thread(self):
        cases = [None, 'x', {}, {'tid': 4000, 'spawn_tids': [4000]},                  # long_lived missing
                 {'tid': 4000, 'long_lived': False, 'spawn_tids': [4000]},
                 {'tid': 4000, 'long_lived': 'yes', 'spawn_tids': [4000]},
                 {'tid': 4000, 'long_lived': True, 'spawn_tids': [4000, 4001]},       # a second thread
                 {'tid': 4000, 'long_lived': True, 'spawn_tids': [4001]},
                 {'tid': 4000, 'long_lived': True, 'spawn_tids': []},
                 {'tid': 4000, 'long_lived': True, 'spawn_tids': 4000},
                 {'tid': 4000, 'long_lived': True},
                 {'tid': 0, 'long_lived': True, 'spawn_tids': [0]},
                 {'tid': True, 'long_lived': True, 'spawn_tids': [True]},
                 {'tid': '4000', 'long_lived': True, 'spawn_tids': ['4000']}]
        for spawner in cases:
            with self.subTest(spawner=spawner):
                result = evaluate(dict(host_record(), pdeathsig_spawner=spawner))
                self.assertEqual(result['verdict'], 'fail')
                self.assertTrue(any('B33' in r for r in result['reasons']))
        record = host_record()
        del record['pdeathsig_spawner']
        self.assertEqual(self.verdict(record), 'fail')

    def test_malformed_records_fail_closed_without_raising(self):
        self.assertEqual(self.verdict(None), 'fail')
        self.assertEqual(self.verdict({'leaf': 'relative/leaf'}), 'fail')
        self.assertEqual(self.verdict({'leaf': '/'}), 'fail')
        for arm in ('manager_absent', 'manager_present'):
            for value in (None, [], 'x', {'attempts': 'x'}, {'attempts': [None, 3]}):
                with self.subTest(arm=arm, value=value):
                    self.assertNotEqual(self.mutated(lambda r: r.update({arm: value})), 'pass')

    def test_random_corruption_never_raises_or_passes(self):
        rng = random.Random(728)
        junk = [None, True, 0, -1, '', '..', [], {}, 'x', 1.5, ['a'], {'k': 'v'}]

        def paths(obj, prefix=()):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    yield prefix + (k,)
                    yield from paths(v, prefix + (k,))
            elif isinstance(obj, list):
                for i, v in enumerate(obj):
                    yield prefix + (i,)
                    yield from paths(v, prefix + (i,))
        all_paths = list(paths(host_record()))
        for _ in range(300):
            record = host_record()
            path = rng.choice(all_paths)
            target = record
            for step in path[:-1]:
                target = target[step]
            value = rng.choice(junk)
            target[path[-1]] = value
            result = evaluate(record)  # must not raise
            self.assertIn(result['verdict'], ('pass', 'fail', 'invalid', 'structural'))

    def test_evaluate_does_not_mutate(self):
        record = host_record()
        before = copy.deepcopy(record)
        evaluate(record)
        self.assertEqual(record, before)

    def test_module_runs_nothing(self):
        source = open(b6_evidence.__file__).read()
        for forbidden in ('import subprocess', 'import os', 'Popen', 'os.system', 'open(', 'setresuid', 'prctl('):
            self.assertNotIn(forbidden, source)


if __name__ == '__main__':
    unittest.main()
