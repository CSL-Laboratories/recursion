"""Each saved key carries the hosts the person bound it to when the key was added, and is handed out only for one of
them: ``KeyStore.add(..., destinations=...)``, ``KeyStore.credential_for(key_id, destination)``.

The binding is on the record in the index AND stored with the value in every backend (it travels in the save's token),
so an index edited by hand can neither widen where a key goes nor drop the binding to send a bound key into an engine's
environment. A key saved with no hosts goes to none. Every key here is made up; no test reaches a real keychain.
"""
from __future__ import annotations

import dataclasses
import io
import json
import unittest

from recursion_core.keys import __main__ as entry
from recursion_core.keys import validate as v
from recursion_core.keys.errors import KeyRefused, KeyStoreError
from recursion_core.keys.store import FIELDS, INDEX_NAME
from recursion_core.keys.tests.support import MARKER, TempStoreCase, file_system, keychain_system, needs_posix
from recursion_core.keys.tests.test_macos import mac_system
from recursion_core.plugin_security import manifest

HOSTS = ('upload.example.com:8443', 'api.example.com')          # given unsorted; kept sorted
BOUND = tuple(sorted(HOSTS))
BAD = ('API.example.com', 'localhost', '1.2.3.4', '10.0.0.1:443', '192.168.0.1', 'api.example.com:0',
       'api.example.com:65536', 'api.example.com:99999', 'api.example.com:0443', '', 'api.example.com/x',
       'https://api.example.com', 'example.123', ' api.example.com', 'api.example.com:')


class Recorder:
    """A stand-in reserved set: records every host it is asked about; ``reserve`` says which it reserves."""

    def __init__(self, reserve=()):
        self.calls, self.reserve = [], set(reserve)

    def host_reserved(self, destination: str) -> bool:
        self.calls.append(destination)
        return destination in self.reserve


class GrammarTest(unittest.TestCase):
    def test_the_grammar_is_the_manifests_network_destinations(self) -> None:
        self.assertEqual(v.DESTINATION.pattern, manifest._HOST.pattern)
        self.assertEqual(v.DESTINATION.flags, manifest._HOST.flags)

    def test_ip_literals_and_ports_outside_1_to_65535_are_refused(self) -> None:
        for good in ('api.example.com', 'a.b:1', 'a.b:65535', 'x-1.example.org:443'):
            with self.subTest(good=good):
                self.assertTrue(v.valid_destination(good))
        for bad in BAD + (None, 7, b'api.example.com'):
            with self.subTest(bad=bad):
                self.assertFalse(v.valid_destination(bad))


class Case(TempStoreCase):
    def bind(self, store, hosts=HOSTS, key_id='github-token', provider='openai', env='OPENAI_API_KEY'):
        return store.add(key_id, provider, env, MARKER, metered_spend_allowed=True, cap_usd=5, destinations=hosts)

    def index(self) -> dict:
        return json.loads((self.root / INDEX_NAME).read_text())

    def edit_index(self, change) -> None:
        path = self.root / INDEX_NAME
        doc = json.loads(path.read_text())
        change(doc['keys']['github-token'])
        with open(path, 'w') as fh:                    # same file, same mode: an edit by hand
            fh.write(json.dumps(doc, indent=1) + '\n')

    def refused(self, call, *args) -> str:
        with self.assertRaises(KeyRefused) as caught:
            call(*args)
        self.assertNotIn(MARKER, repr(caught.exception.args) + str(caught.exception))
        return caught.exception.code


@needs_posix
class EnrollmentTest(Case):
    def test_the_record_carries_the_hosts_it_was_added_with(self) -> None:
        store = self.store()
        self.assertEqual(self.bind(store).destinations, BOUND)
        self.assertEqual(store.list().keys['github-token'].destinations, BOUND)
        self.assertEqual(self.index()['keys']['github-token']['destinations'], list(BOUND))

    def test_a_destination_outside_the_grammar_is_refused_and_nothing_is_written(self) -> None:
        store = self.store()
        for hosts in [(bad,) for bad in BAD] + [('api.example.com', 'api.example.com'), 'api.example.com', [7]]:
            with self.subTest(hosts=hosts), self.assertRaises(KeyStoreError) as caught:
                self.bind(store, hosts=hosts)
            self.assertEqual(caught.exception.code, 'destination_invalid')
        self.assertFalse(self.root.exists())

    def test_check_add_refuses_it_before_a_key_is_asked_for(self) -> None:
        with self.assertRaises(KeyStoreError) as caught:
            self.store().check_add('github-token', 'openai', 'OPENAI_API_KEY', destinations=('1.2.3.4',))
        self.assertEqual(caught.exception.code, 'destination_invalid')

    def test_a_host_the_reserved_set_reserves_is_refused_by_calling_it(self) -> None:
        reserved = manifest.ReservedEffects(hosts=frozenset({'venue.example'}))
        with self.assertRaises(KeyStoreError) as caught:
            self.bind(self.store(reserved=reserved), hosts=('api.example.com', 'api.venue.example'))
        self.assertEqual(caught.exception.code, 'destination_reserved')
        self.assertFalse(self.root.exists())
        recorder = Recorder()
        self.bind(self.store(reserved=recorder))
        self.assertEqual(sorted(recorder.calls), list(BOUND))

    def test_a_key_added_with_no_hosts_writes_the_record_as_before(self) -> None:
        self.add(self.store())
        self.assertEqual(set(self.index()['keys']['codex-key']), set(FIELDS))

    def test_the_command_takes_repeated_destinations(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        status = entry.main(['add', 'github-token', '--provider', 'openai', '--env', 'OPENAI_API_KEY', '--stdin',
                             '--destination', HOSTS[0], '--destination', HOSTS[1]], out=out, err=err,
                            store=self.store(), stdin=io.BytesIO(MARKER.encode() + b'\n'))
        self.assertEqual(status, 0, err.getvalue())
        self.assertEqual(self.store().list().keys['github-token'].destinations, BOUND)
        self.assertNotIn(MARKER, out.getvalue() + err.getvalue())

    def test_no_plugin_facing_call_sets_or_widens_it(self) -> None:
        from recursion_core.control.methods.key import key_list
        from recursion_core.keys import hooks

        class Table:
            def __init__(self):
                self.names = []

            def register(self, name, *rest):
                self.names.append(name)

        class Hooks:
            commands, methods = Table(), Table()

            def set(self, *args):
                pass

        stub = Hooks()
        hooks.register(stub)
        self.assertEqual(stub.methods.names, ['key.list'])            # the one method is the read
        self.assertEqual(key_list(None, {'destinations': ['evil.example.com']})['code'], 'request_invalid')
        record = self.bind(self.store())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            record.destinations = ('evil.example.com',)


@needs_posix
class HandOutTest(Case):
    def test_a_bound_host_gets_the_key(self) -> None:
        store = self.store()
        self.bind(store)
        for host in BOUND:
            with self.subTest(host=host):
                self.assertEqual(store.credential_for('github-token', host), MARKER)

    def test_any_other_host_is_refused_credential_not_bound(self) -> None:
        store = self.store()
        self.bind(store)
        for host in ('evil.example.com', 'api.example.com:443', 'example.com', 'upload.example.com'):
            with self.subTest(host=host):
                self.assertEqual(self.refused(store.credential_for, 'github-token', host), 'credential_not_bound')
        self.assertEqual(self.refused(store.credential_for, 'github-token', '1.2.3.4'), 'destination_invalid')
        with self.assertRaises(KeyError) as caught:
            store.credential_for('nobody', 'api.example.com')
        self.assertIs(type(caught.exception), KeyError)

    def test_a_host_reserved_after_enrollment_is_refused_at_hand_out(self) -> None:
        recorder = Recorder()
        store = self.store(reserved=recorder)
        self.bind(store)
        recorder.reserve.add('api.example.com')
        self.assertEqual(self.refused(store.credential_for, 'github-token', 'api.example.com'), 'destination_reserved')

    def test_a_bound_key_never_goes_to_an_engine_environment(self) -> None:
        store = self.store()
        self.bind(store)
        self.assertEqual(self.refused(store.credential, 'github-token', 'OPENAI_API_KEY'), 'destination_required')


@needs_posix
class UnboundTest(Case):
    def test_a_key_saved_with_no_hosts_is_handed_out_for_none(self) -> None:
        store = self.store()
        self.add(store)
        self.assertEqual(self.refused(store.credential_for, 'codex-key', 'api.example.com'), 'credential_not_bound')
        self.assertEqual(store.credential('codex-key', 'CODEX_API_KEY'), MARKER)     # its engine path is unchanged


@needs_posix
class ListTest(Case):
    def listing(self, store, *extra) -> str:
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(entry.main(['list', *extra], out=out, err=err, store=store), 0, err.getvalue())
        return out.getvalue()

    def test_list_shows_each_keys_hosts_and_store_and_which_have_none(self) -> None:
        store = self.store()
        self.bind(store)
        self.add(store)
        text = self.listing(store)
        lines = {line.split()[0]: line for line in text.splitlines()[1:]}
        self.assertIn('hosts=' + ','.join(BOUND), lines['github-token'])
        self.assertIn('store=owner_file', lines['github-token'])
        self.assertIn('hosts=none', lines['codex-key'])
        doc = json.loads(self.listing(store, '--json'))
        keys = {k['key_id']: k for k in doc['keys']}
        self.assertEqual(keys['github-token']['destinations'], list(BOUND))
        self.assertEqual(doc['unbound'], ['codex-key'])
        self.assertNotIn(MARKER, text + json.dumps(doc))


@needs_posix
class BindingWithTheValueTest(Case):
    """On every backend an index edited by hand can neither widen the hosts nor drop the binding."""

    def stores(self):
        system, chain = keychain_system()
        mac, security = mac_system()
        return (('owner_file', file_system(), None), ('secret_service', system, chain), ('macos_keychain', mac,
                                                                                         security))

    def check(self, name, system, holder) -> None:
        store = self.store(system)
        record = self.bind(store)
        token = f'{record.save_id}-{v.binding_digest(BOUND)}'
        if name == 'owner_file':
            held = json.loads((self.root / 'secrets.json').read_text())['secrets']['github-token']['save_id']
            self.assertEqual(held, token)
        elif name == 'secret_service':
            self.assertTrue(any(dict(zip(k[0::2], k[1::2])).get('recursion_save') == token for k in holder.items))
        else:
            self.assertTrue(any(token in (item.get('svce') or '') for item in holder.items))
        self.edit_index(lambda entry: entry.update(destinations=sorted(BOUND + ('evil.example.com',))))
        self.assertEqual(self.refused(store.credential_for, 'github-token', 'evil.example.com'), 'secret_missing')
        self.edit_index(lambda entry: entry.pop('destinations'))
        self.assertEqual(self.refused(store.credential, 'github-token', 'OPENAI_API_KEY'), 'secret_missing')

    def test_each_backend(self) -> None:
        for name, system, holder in self.stores():
            with self.subTest(backend=name):
                self.root = self.base / name / 'keys'          # a store of its own per backend
                self.check(name, system, holder)

if __name__ == '__main__':
    unittest.main()
