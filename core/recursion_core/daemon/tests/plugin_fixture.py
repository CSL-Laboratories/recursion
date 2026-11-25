"""Test fixture: an installed operator plugin on disk, the way ``plugins.PluginHost`` reads one.

Keys are generated per test and never stored; every value is fake. Signing needs ``cryptography``
(``plugin_security.tests.support.Key``); the unsigned fixture needs nothing.
"""
import hashlib
import json
import os
from pathlib import Path
import stat

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


def _owner_only_folders(path):
    """Make ``path`` and every missing folder above it owner-only. The mode is set on each folder as it is
    made, not left to the process mask; a folder that is already there is left as it is."""
    path = Path(path)
    missing = []
    while not os.path.lexists(path):
        missing.append(path)
        path = path.parent
    for folder in reversed(missing):
        folder.mkdir(mode=0o700)
        os.chmod(folder, 0o700)


def write_install(config_dir, *, trusted=None, grants=(), **extra):
    """Write ``plugins/install.json`` under ``config_dir`` (``$XDG_CONFIG_HOME/recursion``).

    Every folder it makes is owner-only whatever the process mask is, as the installer makes them.
    """
    plugins = Path(config_dir) / 'plugins'
    _owner_only_folders(plugins)
    os.chmod(plugins, 0o700)
    path = plugins / 'install.json'
    path.write_text(json.dumps({'trusted_keys': dict(trusted or {}), 'grants': list(grants), **extra}))
    os.chmod(path, 0o600)
    return plugins


def ensure_revocation_store(state_dir):
    """Create the installer's revocation store when this test has not prepared one.

    The daemon refuses ``revocation_store_missing`` rather than creating the file.
    A directory the test already made unusable is left alone, so that test still
    sees the daemon's own refusal. Returns the store path, or None when left alone.
    """
    from recursion_core.daemon.breaker import STORE_DIR
    from recursion_core.daemon.plugins import REVOCATION_FILE
    from recursion_core.plugin_security import create_revocation_store

    directory = Path(state_dir) / STORE_DIR
    path = directory / REVOCATION_FILE
    try:
        # A mode 000 folder raises on 3.12 while stating the file inside it.
        # Leave that folder for the daemon's own refusal.
        if path.is_symlink() or path.exists():
            return str(path)
        if directory.exists():
            if directory.is_symlink() or not directory.is_dir():
                return None
            info = directory.stat()
            if stat.S_IMODE(info.st_mode) & 0o077 or not os.access(directory, os.W_OK | os.X_OK):
                return None
    except OSError:
        return None
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    create_revocation_store(str(path))
    return str(path)


def grant_for(source, *, key=None, plugin_id='fixture.operator', permissions=('read', 'panel'), **extra):
    return {'plugin_id': plugin_id, 'artifact_sha256': hashlib.sha256(source).hexdigest(),
            'signer_key_id': key_id(key.public) if key is not None else None, 'permissions': list(permissions), **extra}
