"""Test fixture: an installed operator plugin on disk, the way ``plugins.PluginHost`` reads one.

Keys are generated per test and never stored; every value is fake. Signing needs ``cryptography``
(``plugin_security.tests.support.Key``); the unsigned fixture needs nothing.
"""
import hashlib
import json
import os
from pathlib import Path

from recursion_core.plugin_security import canonical
from recursion_core.plugin_security.signing import key_id

FAKE_DEVELOPER_KEY = '11' * 32  # 32 bytes of hex: well formed, on no trusted list, signs nothing

# A plugin that offers everything the daemon reads: panels, an O7 reader and the core client hook.
# Plain values only; it imports nothing from Recursion.
OPERATOR_SOURCE = b'''
CALLS = []
CORE = []


def registry():
    return {"panels": [{"id": "fixture.positions", "title": "Fixture positions",
                        "reads": ["operator.positions.inspect"], "venue": "kalshi"},
                       {"id": "fixture.health", "title": "Fixture health", "reads": ["operator.health.inspect"]}],
            "required": {"venue_use": ["kalshi:fixture_read"]}}


def operator(command):
    CALLS.append(command["command"])
    if command["command"] == "operator.health.inspect":
        raise RuntimeError("fixture health reader is broken")
    return {"api_version": "o7.operator.v0", "command": command["command"], "request_id": command["request_id"],
            "surface": command["surface"], "status_code": "ok",
            "visibility": {"uncertainty": "visible", "refusal": "visible", "stale_data": "visible",
                           "reconciliation": "reconciled"},
            "result": {"positions": [{"venue": "kalshi", "symbol": "FIXTURE-OWN-ROW", "quantity": 1},
                                     {"venue": "othervenue", "symbol": "FIXTURE-OTHER-ROW", "quantity": 2}],
                       "unlabelled_total": "FIXTURE-UNLABELLED-TOTAL"}}


def bind_core(call):
    CORE.append(call)
'''


def manifest_for(source, developer_key, *, plugin_id='fixture.operator', permissions=('read', 'panel')):
    return canonical({
        'schema': 'csl-plugin-manifest/vNext', 'plugin_id': plugin_id, 'version': '1.0.0',
        'artifact_sha256': hashlib.sha256(source).hexdigest(), 'dependency_lock_sha256': '0' * 64,
        'install_scripts': [], 'developer_key': developer_key, 'permissions': list(permissions),
        'network_destinations': [], 'credential_refs': []})


def write_plugin(directory, source=OPERATOR_SOURCE, *, key=None, manifest=None, signature='from key', **manifest_fields):
    """Write ``manifest.json``, ``plugin.py`` and (when signed) ``manifest.sig``. Returns the manifest bytes."""
    directory = Path(directory)
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    if manifest is None:
        manifest = manifest_for(source, key.public if key is not None else FAKE_DEVELOPER_KEY, **manifest_fields)
    if signature == 'from key':
        signature = key.sign(manifest) if key is not None else None
    for name, data in (('manifest.json', manifest), ('plugin.py', source),
                       ('manifest.sig', None if signature is None else signature.encode() + b'\n')):
        if data is not None:
            path = directory / name
            path.write_bytes(data)
            os.chmod(path, 0o600)
    return manifest


def write_install(config_dir, *, trusted=None, grants=(), **extra):
    """Write ``plugins/install.json`` under ``config_dir`` (``$XDG_CONFIG_HOME/recursion``)."""
    plugins = Path(config_dir) / 'plugins'
    plugins.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(plugins, 0o700)
    path = plugins / 'install.json'
    path.write_text(json.dumps({'trusted_keys': dict(trusted or {}), 'grants': list(grants), **extra}))
    os.chmod(path, 0o600)
    return plugins


def grant_for(source, *, key=None, plugin_id='fixture.operator', permissions=('read', 'panel'), **extra):
    return {'plugin_id': plugin_id, 'artifact_sha256': hashlib.sha256(source).hexdigest(),
            'signer_key_id': key_id(key.public) if key is not None else None, 'permissions': list(permissions), **extra}
