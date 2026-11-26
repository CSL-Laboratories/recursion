"""Install, list, remove, enable, disable and roll back a plugin.

A generation is one complete copy of the profile the daemon already reads.
Generations live beside ``plugins/``, never inside it: the daemon treats every
directory there as a plugin, including a dot directory.

    <config>/plugin-generations/<n>/install.json
    <config>/plugin-generations/<n>/.disabled
    <config>/plugin-generations/<n>/<plugin_id>/{manifest.json,plugin.py,manifest.sig?}
    <config>/plugin-generations/current
    <config>/plugin-generations/.parent-<n>

``current`` is an ASCII integer and a newline, matching ``^[1-9][0-9]*\\n$``.
It is written only after generation ``n`` is complete. The live ``plugins/``
tree is published after that. ``plugin-generations/.publishing`` names, in the
same form, the generation a command publishes, and on a second line the
generation ``current`` named before it, when there was one. It is written and
synced before that generation is renamed into place, so before any live folder
changes, and removed once the live tree holds it whole. The next list, install,
remove, enable, disable or rollback finishes that publish when ``.publishing`` names ``current``, and
drops ``.publishing`` when it does not. A ``.publishing`` that is not in that
form refuses with the steps that end the refusal.

No publish, and no finish of a stopped one, removes a live entry it has not
proven is its own: every live entry the swap would move (a folder, a file, a
link or anything else under a name the generation writes, and every folder and
link) must be a whole copy, file for file and byte for byte, of the entry of
that name in one of the two generations the record names. One that is not
refuses ``plugin_directory_untrusted`` with the steps that end the refusal, and
nothing is moved or deleted. A plugin folder that holds anything its generation
does not hold is not a whole copy, with one exception when no stopped publish is
being finished: a folder of a plugin the current generation holds, whose own
files are each that generation's copy byte for byte. Its other entries are moved
into ``plugin-generations/.kept-<n>/<plugin_id>``, the command says in one line
which plugin and where, and it goes on. The finishing of a stopped publish
proves the live ``install.json`` too: it is replaced only when it is absent or
byte for byte the copy of one of the two generations the record names; one
changed by hand refuses with the steps that end the refusal. The swap moves each replaced entry into
``plugin-generations/.old``, proves it again there and only then removes it.
An entry that is not proven there (one put in place between the check and the
swap) is kept under ``plugin-generations/.kept-<n>``, and the command says
where and exits non-zero. The swap of one name is two renames: an empty folder,
or an ``install.json``, put at that name by another process between them is
replaced; anything else there fails the rename and is left in place. Mode bits
are not part of the proof. Each line that names a kept place is given to the
caller's ``report_kept`` the moment the entries are moved, before any later step can fail, and a failure
after the move carries every such line; ``list`` names every kept place that holds entries. An extra entry
that cannot be moved aside (a folder without write permission) puts back what was moved, leaves no kept
folder and refuses with the steps that end the refusal. Every refusal caused by a link prints those steps
too; no link is followed. Older
generations stay on disk. ``.parent-<n>`` names the generation that was
current when generation ``n`` was written, and is absent for the first. It
lives beside the generations, never inside one, so a generation is the exact
tree its publish copies into ``plugins/``. Rollback publishes that
generation, never one a rollback already left, by the same record and the same
proof as every publish. A generation that ``current`` does
not name (a stop between its rename and the pointer) is left as it is; the next
command takes the number after every generation on disk.

``.disabled``, when present, is sorted plugin ids, one per line. The name
starts with a dot, so it is not a plugin id: a plugin may be named
``disabled``. A generation that still has the older regular file ``disabled``
is read the same way. A directory of that name is the plugin, not the list.
The list lives in the generation only. Publish omits those plugin directories
from ``plugins/`` and does not copy the file. Grants and labels stay in
``install.json``. ``install.json`` matches the plugin-id grammar and is the
record the daemon reads, so that id is refused before a generation is
published. A disabled plugin stays listed. Enable and disable each write one
generation. A disabled plugin's folder is out of ``plugins/`` by design, so every
command reads it, and the list, from the current generation.

A local bare git repository is read only when ``git_repository`` is passed.
The entry's https repository is a name. This module does not fetch it. The
file bytes are checked against the manifest digest before the profile lock
and again before the generation is written. Nothing from the fetched tree is
executed. With no ``git_repository`` and no vendored ``plugin.py``, the install
refuses ``source_unavailable`` and does not fetch.

The live ``plugins/`` tree is the truth. Every command reads the trusted keys,
grants, reserved names and labels from the live ``install.json`` the daemon
reads and carries them unchanged into the next generation: a key or a grant
taken out by hand stays out, and one put in by hand stays in. A plugin folder
taken out by hand stays out. Once a generation is current, every live plugin
folder must be the copy this module wrote there. A folder it did not write, or
a folder whose name is not its manifest's plugin id, or a link under a plugin's
name, refuses ``plugin_directory_untrusted``, names the folder and the id and
says the steps that end the refusal; nothing is written and nothing is renamed.

Every check of the live ``plugins/`` path runs before a generation is written,
so a refused command leaves ``current`` and the generations as they were. The
publish moves ``install.json`` in first, by one rename over the old file, then
moves each old plugin directory out whole and each new one in whole: at every
step the live tree holds the whole old or the whole new ``install.json``. Each
written file and its directory are synced before the rename that publishes
them, and ``current`` is synced after its rename.

The registry is checked before any profile file is created. A signature is
checked on every install. When this interpreter cannot verify one, the install
is refused and nothing is written. A signature that does not verify against
the manifest's own key refuses the registry. A signature that verifies, but
whose key is not on this install's trusted list, still installs and loads as
unsigned, unless the plugin is pinned (below). This command does not add a trusted key.

On an explicit yes, this command writes one grant into the new generation for
the requested permissions except ``order``. The grant is bound to that
release's artifact SHA-256 and to the signer key id, which is null when the
release is unsigned. ``order`` stays its own flow: by hand behind a warning
when unsigned, asked once when signed, and it is not part of this grant.
``--no-grant`` writes no grant. A run with no terminal and no explicit yes
writes no grant.

On a first install this command creates ``<state>/plugin-security/revoked.sqlite3``
through the public ``create_revocation_store``. A first install means that file
is absent, ``plugin-generations`` is absent, and ``plugins/install.json`` is
absent. In every other state an absent store is refused
``revocation_store_missing`` and is not created. No command in this module
creates a store that has gone.

A plugin installed signed is pinned to its key: ``install.json`` holds, under ``pins``, the signing key id
and the version for that plugin id, written by the same commit as the generation. A release of a pinned
plugin that is unsigned, fails its check or is signed by a key not on the trusted list refuses
``update_unsigned_over_signed``, writes nothing, and names the two ways forward. A release signed by
another key that is on the trusted list drops the grant as before and moves the pin only after the user's
typed yes; without it nothing is written. ``unpin`` drops the pin and every grant of one plugin after a
typed yes; the plugin stays installed as a local unsigned plugin. A remove keeps the pin. An
``install.json`` without ``pins`` (written before pins existed) has each installed plugin whose grant names
a signing key pinned to that key when it is read, and nothing else changes. A file that holds no pin is written
without ``pins``, byte for byte as before. Once the file has held ``pins``, every later write keeps it, as an empty
map after the last unpin, so pins are derived from grants only for a file that never held the key.

A release of a pinned plugin, signed by a trusted key, that asks for more than the running release (a
permission, a network destination, a credential reference or an install script that release did not request)
is not installed: its checked files are kept under ``plugin-quarantine/<id>/<version>/`` in the config folder,
owner-only and written and read with the same checks as a generation, outside ``plugins/`` and never a
generation or ``current``. The running release stays live with its grant, and the install answers
``quarantined`` with one line for each thing it asks for more. A release held before for that plugin is moved
into a kept place and named. ``quarantine_list`` shows what is held. ``quarantine_accept`` checks the held
release again and installs it only after the user's typed yes, with a new grant of what it requests except
``order``, which is never granted this way; its held copy is removed once the new generation holds it.
``quarantine_drop`` moves the held files into a kept place and names it. Nothing is accepted by a flag or a
default.

A read or write the system refuses (a full or read-only disk, no permission) raises the typed refusal
``filesystem_refused``, which keeps the system's message; no caller gets a bare ``OSError``.
"""

from __future__ import annotations

import dataclasses
import errno
import functools
import hashlib
import json
import os
import re
import shlex
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .errors import Refused
from .git_source import SourceRefused, fetch_plugin_source
from .loader import CoreHealth, PluginLoader
from .manifest import PERMISSIONS, Grant, ReservedEffects, TrustStore, parse_manifest
from .registry import check_registry, platform_gap
from .signing import key_id

try:
    import fcntl
except ImportError:  # Windows: every command refuses platform_unsupported before it opens a file
    fcntl = None

__all__ = ["FilesystemRefused", "InstallRefused", "NothingChanged", "disable_plugin", "enable_plugin",
           "install_plugin", "list_plugins", "quarantine_accept", "quarantine_drop", "quarantine_list",
           "remove_plugin", "rollback_generation", "unpin_plugin"]

_PLUGIN_ID = re.compile(r"[a-z][a-z0-9_.-]{1,63}")
_CURRENT = re.compile(r"[1-9][0-9]*\n")
_GENERATION = re.compile(r"[1-9][0-9]{0,17}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_KEY_ID = re.compile(r"ed25519:[0-9a-f]{32}")
# Names the generation a command publishes, and the one it replaces, until the live tree holds it whole.
_PUBLISHING = ".publishing"
_RECORD = re.compile(r"([1-9][0-9]*)\n(?:([1-9][0-9]*)\n)?")
# A kept place: ``.kept-<generation>`` or ``.kept-<generation>-<k>``.
_KEPT = re.compile(r"\.kept-[1-9][0-9]*(?:-[1-9][0-9]*)?")
# Names the profile itself uses inside a generation and inside plugins/: never a plugin id.
_PROFILE_NAMES = frozenset({"install.json"})
_LABELS = frozenset({"verified", "non_verified"})
_INSTALL_KEYS = frozenset({"trusted_keys", "grants", "reserved", "authority_anchor", "registry_labels", "pins"})
# A pin: the key a plugin was installed signed by, and that release's version.
_PIN_KEYS = frozenset({"signer_key_id", "version"})
# Names this store writes beside plugin directories that also match a plugin id.
# install.json stays: the daemon reads it by this name. The disabled-id list
# does not: it is ".disabled", which cannot be a plugin id.
_STORE_RESERVED_IDS = frozenset({"install.json"})
_GRANT_KEYS = frozenset({
    "plugin_id", "artifact_sha256", "signer_key_id", "permissions", "unsigned_order_acknowledged",
    "order_after_revoked_key", "order_after_revocation_generation", "developer_key_id",
})
_MAX_MANIFEST = 64 * 1024
_MAX_ARTIFACT = 8 * 1024 * 1024
_MAX_SIGNATURE = 4096
_MAX_INSTALL = 1024 * 1024
# A held release: <config>/plugin-quarantine/<plugin_id>/<version>/ with its checked files and its registry label.
_QUARANTINE = "plugin-quarantine"
_HELD_LABEL = "registry-label"
_HELD_LIMITS = {"manifest.json": _MAX_MANIFEST, "plugin.py": _MAX_ARTIFACT, "manifest.sig": _MAX_SIGNATURE,
                _HELD_LABEL: 64}
_VERSION_DIR = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}")
_DEVELOPER_CHECKS = (
    "In-process plugin code can launch engine binaries past the launch check.",
    "In-process plugin code can stall other work.",
    "The loader reads the GIL after every load.",
    "An untrusted plugin config folder refuses every plugin.",
    "A missing revocation store reads as empty: no key counts as revoked.",
    "The registry label is a label, not a gate.",
    "A signature that fails this install's trusted-key check loads as unsigned.",
    "Signed order is asked once. This command does not grant it.",
    "Unsigned order is by hand, behind a warning. This command does not grant it.",
    "A yes writes one grant for the requested permissions except order, "
    "bound to the artifact digest and the signer key id.",
)


class InstallRefused(Exception):
    """The command did not finish a new profile. ``code`` is stable."""

    def __init__(self, code: str, codes: list[str] | None = None, detail: str = "", *,
                 show_detail: bool = False) -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.codes = list(codes) if codes else [code]
        self.detail = detail
        # The command line prints the detail under the code: it tells the user how to end the refusal.
        self.show_detail = show_detail


class NothingChanged(Exception):
    """A command that asks a typed yes did not get one, or had nothing to change. Nothing was written."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class FilesystemRefused(InstallRefused, OSError):
    """``filesystem_refused``: the system refused a read or write (a full or read-only disk, no permission).

    The system's message is in ``detail``, its ``errno``, ``strerror`` and ``filename`` are kept, and the
    system's error is the cause. ``kept`` names what a command kept aside before the failure. It is an
    ``OSError`` too, so a caller that catches one still catches it.
    """

    def __str__(self) -> str:
        return BaseException.__str__(self)


def _system_refused(error: OSError) -> FilesystemRefused:
    refusal = FilesystemRefused("filesystem_refused", detail=f"The system refused a file read or write: {error}",
                                show_detail=True)
    refusal.errno, refusal.strerror, refusal.filename = error.errno, error.strerror, error.filename
    refusal.kept = list(getattr(error, "kept", ()))
    return refusal


def _typed_refusals(command):
    """Run ``command``; a read or write the system refuses leaves it as the typed refusal ``filesystem_refused``."""

    @functools.wraps(command)
    def run(*args, **kwargs):
        try:
            return command(*args, **kwargs)
        except InstallRefused:
            raise
        except OSError as error:
            raise _system_refused(error) from error

    return run


@dataclass
class StoredPlugin:
    manifest: bytes
    artifact: bytes
    signature: bytes | None = None


@dataclass
class Snapshot:
    trusted_keys: dict = field(default_factory=dict)
    grants: dict = field(default_factory=dict)
    hosts: tuple = ()
    prefixes: tuple = ()
    anchor: str | None = None
    labels: dict = field(default_factory=dict)
    plugins: dict = field(default_factory=dict)
    # A grant's developer_key_id as it was read, by plugin id; written back unchanged.
    developer_key_ids: dict = field(default_factory=dict)
    # By plugin id: {"signer_key_id", "version"}. None when the file has no ``pins`` (read: derived from grants).
    pins: dict | None = None
    # True when the file read held ``pins``: every later write keeps the key, also as an empty map.
    pins_held: bool = False
    # The generation's disabled-id list. Publish leaves these plugins out of plugins/.
    disabled: frozenset = field(default_factory=frozenset)

    def reserved(self) -> ReservedEffects:
        return ReservedEffects(frozenset(self.hosts), tuple(self.prefixes))


@_typed_refusals
def install_plugin(config: os.PathLike | str, registry_root: os.PathLike | str, plugin_id: str, *,
                   version: str | None = None, state: os.PathLike | str | None = None, wait: bool = True,
                   git_repository: str | None = None, report_kept=None, confirm=None,
                   write_grant: bool | None = None, confirm_grant=None, allow_withdrawn: str | None = None,
                   registry_name: str | None = None, registry_kind: str | None = None) -> dict:
    """Check ``registry_root``, then write the release as the next generation.

    The release chosen (by ``version``, or the newest active one) passes ``_installable`` on every call,
    and no keyword turns that off. A withdrawn release goes on only as ``_WITHDRAWN_RULE`` says for the way
    it was chosen, or when ``allow_withdrawn`` names that same version (the person's own step for this one
    install); an ``allow_withdrawn`` that names another release than the one chosen is refused
    ``allow_withdrawn_unmatched``. One that names the release chosen when that release is not withdrawn lets
    nothing through: the install goes on as it would without it, and the words that it was not needed go to
    ``report_kept`` when it is given and into the result as ``allow_withdrawn_not_needed``.
    A withdrawn release that goes on is said to be withdrawn before the install goes on: the words go to
    ``report_kept`` when it is given and into the result as ``withdrawn``. ``registry_name`` is the registry
    as the person named it, for those words (without it they name ``registry_root``); ``registry_kind`` is
    ``pinned`` for a registry read at a commit, ``folder`` for a registry folder read by
    ``install_from_registry``, and ``plain`` (or None) for a folder read as it is: a direct call, or the
    command line's ``--registry <folder>``. Any other value is decided as a registry folder.

    ``git_repository`` is a local bare path. The fetch, and every refusal of
    that fetch, finishes before the profile lock. A refusal does not fall
    back to a vendored ``plugin.py``.

    ``report_kept``, when given, is called with each line that names a kept place the moment it is made.
    ``confirm`` is asked one question and returns the user's answer; only ``yes`` moves a pin to another
    trusted key. Without it the pin does not move and nothing is written.

    ``write_grant`` True writes one grant (the user's yes). False writes none
    (``--no-grant``). None asks ``confirm_grant`` when that callable is set, and
    otherwise writes none. ``confirm_grant(requested, checks)`` must return True to
    grant. Any other return writes none. A grant never includes ``order``.
    """
    _require_platform()
    if write_grant is not None and type(write_grant) is not bool:
        raise InstallRefused("grant_flag")
    if confirm_grant is not None and not callable(confirm_grant):
        raise InstallRefused("grant_flag")
    config_path = Path(config)
    report = check_registry(registry_root)
    codes = report.codes()
    if codes:
        refusal = InstallRefused(codes[0], codes)
        # Each finding names its file and rule; a caller that shows them (the install by registry) can.
        refusal.findings = list(report.errors)
        raise refusal
    # A signature anywhere in the registry that could not be verified refuses the
    # whole install. Unknown must not read as valid, and nothing is written.
    if any(item.code == "signature_unchecked" for item in report.notes):
        raise InstallRefused("signature_backend_unavailable")
    if type(plugin_id) is str and plugin_id in _STORE_RESERVED_IDS:
        raise InstallRefused("plugin_id_reserved")
    if not _plugin_id_ok(plugin_id) or plugin_id in _PROFILE_NAMES:
        raise InstallRefused("plugin_not_in_registry")
    if version is not None and type(version) is not str:
        raise InstallRefused("release_not_in_registry")
    chosen, said, not_needed = _installable(_select(report, plugin_id, version), report, _Road(
        version, allow_withdrawn, registry_kind, os.fspath(registry_root) if registry_name is None else registry_name,
        config_path, state, git_repository))
    told = said if said is not None else not_needed
    if told is not None and report_kept is not None:
        # Said before anything is fetched or written, in words that cannot be missed.
        report_kept(told)
    artifact = chosen.artifact
    fetched = False
    if git_repository is not None or artifact is None:
        artifact = _fetch_named(git_repository, chosen)
        fetched = True
    if artifact is None:
        raise InstallRefused("source_unavailable")
    manifest = _parsed_manifest(chosen.manifest)
    if hashlib.sha256(artifact).hexdigest() != manifest.artifact_sha256:
        raise InstallRefused("source_digest_mismatch" if fetched else "artifact_digest_mismatch")
    fd = _lock(config_path, wait=wait)
    try:
        result = _install_locked(config_path, chosen, plugin_id, state, artifact, fetched, report_kept, confirm,
                                 write_grant, confirm_grant)
    finally:
        os.close(fd)
    if said is not None:
        # A withdrawn release went on: the result says so, installed or held, and how it was let through.
        result.update(status=chosen.status, status_reason=chosen.status_reason, withdrawn=said,
                      withdrawn_by=("allow_withdrawn" if allow_withdrawn is not None else "named_version"))
    if not_needed is not None:
        result["allow_withdrawn_not_needed"] = not_needed
    return result


@_typed_refusals
def list_plugins(config: os.PathLike | str, *, state: os.PathLike | str | None = None, wait: bool = True) -> dict:
    """Publish the current generation if a publish stopped early, then list what it holds."""
    _require_platform()
    del state  # list does not open the revocation store and does not create one
    config_path = Path(config)
    fd = _lock(config_path, wait=wait)
    try:
        _finish_publish(config_path)
        result = {"command": "list", "plugins": _rows(_load_snapshot(config_path))}
        # Every kept place that holds entries is named, also one whose command stopped before its line.
        return _with_notes(result, _kept_lines(config_path))
    finally:
        os.close(fd)


@_typed_refusals
def remove_plugin(config: os.PathLike | str, plugin_id: str, *, state: os.PathLike | str | None = None,
                  wait: bool = True, report_kept=None) -> dict:
    """Drop one plugin into a new generation. An unknown id writes nothing."""
    _require_platform()
    del state
    config_path = Path(config)
    if not _plugin_id_ok(plugin_id):
        raise InstallRefused("plugin_not_installed")
    fd = _lock(config_path, wait=wait)
    try:
        _finish_publish(config_path)
        snapshot = _load_snapshot(config_path, removing=plugin_id)
        if plugin_id not in snapshot.plugins:
            raise InstallRefused("plugin_not_installed")
        plugins = dict(snapshot.plugins)
        del plugins[plugin_id]
        grants = dict(snapshot.grants)
        grants.pop(plugin_id, None)
        # The pin stays: a removed plugin installed again unsigned is refused the same way.
        kept = Snapshot(snapshot.trusted_keys, grants, snapshot.hosts, snapshot.prefixes, snapshot.anchor,
                        _labels_for(snapshot.labels, plugins), plugins, snapshot.developer_key_ids,
                        pins=snapshot.pins, pins_held=snapshot.pins_held, disabled=snapshot.disabled - {plugin_id})
        notes = []
        generation = _commit(config_path, kept, notes, report_kept, removing=plugin_id)
        result = {"command": "remove", "plugin_id": plugin_id, "generation": generation, "plugins": _rows(kept)}
        return _with_notes(result, notes)
    finally:
        os.close(fd)


@_typed_refusals
def enable_plugin(config: os.PathLike | str, plugin_id: str, *, state: os.PathLike | str | None = None,
                  wait: bool = True, report_kept=None) -> dict:
    """Publish one installed plugin again. An unknown id writes nothing."""
    return _set_disabled(config, plugin_id, disable=False, state=state, wait=wait, report_kept=report_kept)


@_typed_refusals
def disable_plugin(config: os.PathLike | str, plugin_id: str, *, state: os.PathLike | str | None = None,
                   wait: bool = True, report_kept=None) -> dict:
    """Hide one installed plugin from ``plugins/``. The generation keeps its bytes."""
    return _set_disabled(config, plugin_id, disable=True, state=state, wait=wait, report_kept=report_kept)


@_typed_refusals
def rollback_generation(config: os.PathLike | str, *, state: os.PathLike | str | None = None,
                        wait: bool = True) -> dict:
    """Point ``current`` at the generation that was current when this one was written.

    The first generation, and a profile with no generation, refuse
    ``rollback_unavailable`` and write nothing. A missing parent directory
    refuses ``generation_missing`` before the pointer moves. The publish is the
    one every command makes: the live tree is proven against both generations
    before anything moves, and ``.publishing`` names both before the pointer is
    written, so if publishing raises after the pointer is written, the next list
    publishes that generation. A generation a rollback already left stays on
    disk and is not published again.
    """
    _require_platform()
    del state
    config_path = Path(config)
    fd = _lock(config_path, wait=wait)
    try:
        _finish_publish(config_path)
        current = _read_current(config_path)
        if current is None or current < 2:
            raise InstallRefused("rollback_unavailable")
        root = config_path / "plugin-generations"
        replaced = root / str(current)
        parent = _read_parent(root, current)
        if parent is None or parent >= current:
            raise InstallRefused("generation_missing" if parent is None else "generation_unreadable")
        dest = root / str(parent)
        if not _lexists(dest):
            raise InstallRefused("generation_missing")
        _require_owned_dir(dest, "generation_missing")
        snapshot = _read_profile(dest, missing_install_ok=False)
        sources = [dest, replaced]
        # Every live entry the swap would move, and the live install.json, are proven before anything moves.
        _check_live(config_path)
        _check_install_file(config_path, sources)
        _check_publish(config_path, sources, set(snapshot.plugins))
        _atomic_replace(root, _PUBLISHING, f"{parent}\n{current}\n".encode("ascii"))
        _write_pointer(root, parent)
        kept = _copy_generation_to_live(config_path, parent, sources)
        _clear_marker(root)
        _refuse_kept(kept)
        return {"command": "rollback", "generation": parent, "plugins": _rows(snapshot)}
    finally:
        os.close(fd)


@_typed_refusals
def _set_disabled(config: os.PathLike | str, plugin_id: str, *, disable: bool,
                  state: os.PathLike | str | None, wait: bool, report_kept=None) -> dict:
    _require_platform()
    del state
    config_path = Path(config)
    if not _plugin_id_ok(plugin_id):
        raise InstallRefused("plugin_not_installed")
    fd = _lock(config_path, wait=wait)
    try:
        _finish_publish(config_path)
        snapshot = _load_snapshot(config_path)
        if plugin_id not in snapshot.plugins:
            raise InstallRefused("plugin_not_installed")
        if disable:
            if plugin_id in snapshot.disabled:
                raise InstallRefused("plugin_already_disabled")
            disabled = snapshot.disabled | {plugin_id}
            command = "disable"
        else:
            if plugin_id not in snapshot.disabled:
                raise InstallRefused("plugin_not_disabled")
            disabled = snapshot.disabled - {plugin_id}
            command = "enable"
        # Everything else the live install.json holds is carried unchanged: keys, grants, pins.
        updated = dataclasses.replace(snapshot, disabled=disabled)
        notes = []
        generation = _commit(config_path, updated, notes, report_kept)
        result = {"command": command, "plugin_id": plugin_id, "generation": generation, "plugins": _rows(updated)}
        return _with_notes(result, notes)
    finally:
        os.close(fd)


def _fetch_named(git_repository: str | None, chosen) -> bytes:
    """Fetch the named commit. Refuse before creating a profile file."""
    if git_repository is None or chosen.source_commit is None or chosen.source_path is None:
        raise InstallRefused("source_unavailable")
    manifest = _parsed_manifest(chosen.manifest)
    if chosen.artifact_sha256 != manifest.artifact_sha256:
        raise InstallRefused("release_artifact_digest_mismatch")
    if chosen.manifest_sha256 != hashlib.sha256(chosen.manifest).hexdigest():
        raise InstallRefused("release_manifest_digest_mismatch")
    repository = git_repository if type(git_repository) is str else os.fspath(git_repository)
    try:
        return fetch_plugin_source(
            repository, chosen.source_commit, chosen.source_path,
            expected_sha256=manifest.artifact_sha256, install_scripts=manifest.install_scripts)
    except SourceRefused as exc:
        raise InstallRefused(exc.code) from None


def _parsed_manifest(raw: bytes):
    try:
        return parse_manifest(raw)
    except Refused as exc:
        raise InstallRefused(exc.code, detail=exc.detail) from None


@_typed_refusals
def unpin_plugin(config: os.PathLike | str, plugin_id: str, *, state: os.PathLike | str | None = None,
                 wait: bool = True, report_kept=None, confirm=None) -> dict:
    """Drop the pin and every grant of one plugin, after the user's typed yes, as a new generation.

    Nothing else changes: the plugin stays installed, as a local unsigned plugin. ``confirm`` is asked one
    question; any answer but ``yes``, or no ``confirm``, writes nothing.
    """
    _require_platform()
    del state
    config_path = Path(config)
    if not _plugin_id_ok(plugin_id):
        raise NothingChanged("no plugin of that id is pinned; nothing was written.")
    fd = _lock(config_path, wait=wait)
    try:
        finished = _finished_note(config_path, _finish_publish(config_path))
        snapshot = _load_snapshot(config_path)
        held = snapshot.pins.get(plugin_id)
        if held is None:
            if finished:
                raise NothingChanged(f"the plugin {plugin_id} is not pinned. {finished} Nothing else was written.")
            raise NothingChanged(f"the plugin {plugin_id} is not pinned; nothing was written.")
        key = held["signer_key_id"]
        question = " ".join([f"Unpin the plugin {plugin_id} from the key {key}?", *([finished] if finished else []),
                             f"This drops its pin and every grant of it, and it stays installed as a local unsigned "
                             f"plugin.", _extras_note(config_path, snapshot), "Type yes to unpin it:"])
        if not _confirmed(confirm, question):
            if finished:
                raise NothingChanged(f"the plugin {plugin_id} stays pinned to the key {key}: the answer was not "
                                     f"yes. {finished} Nothing else was written.")
            raise NothingChanged(f"the plugin {plugin_id} stays pinned to the key {key}: the answer was not yes, "
                                 f"and nothing was written.")
        pins = dict(snapshot.pins)
        del pins[plugin_id]
        grants = dict(snapshot.grants)
        grants.pop(plugin_id, None)
        updated = Snapshot(snapshot.trusted_keys, grants, snapshot.hosts, snapshot.prefixes, snapshot.anchor,
                           snapshot.labels, snapshot.plugins, snapshot.developer_key_ids, pins=pins,
                           pins_held=snapshot.pins_held, disabled=snapshot.disabled)
        notes = []
        generation = _commit(config_path, updated, notes, report_kept)
        result = {"command": "unpin", "plugin_id": plugin_id, "generation": generation, "plugins": _rows(updated)}
        return _with_notes(result, notes)
    finally:
        os.close(fd)


def _shown(path) -> str:
    """``path`` as an absolute path, quoted for the shell, so a printed command runs as printed."""
    return shlex.quote(str(Path(path).absolute()))


def _confirmed(confirm, question: str) -> bool:
    """True only when ``confirm`` answers ``question`` with the typed word yes."""
    if confirm is None:
        return False
    answer = confirm(question)
    return type(answer) is str and answer.strip() == "yes"


def _command(config, *words: str) -> str:
    """The command ``recursion plugin <words>`` for the profile ``config``: with ``--config`` and its absolute
    path, unless ``config`` is the default profile."""
    from ._entrypoints import _config_dir  # the one rule for the default profile

    line = " ".join(["recursion", "plugin", *words])
    if Path(config).absolute() == _config_dir(None).absolute():
        return line
    return f"{line} --config {_shown(config)}"


def _next_pin(snapshot: Snapshot, manifest, trust_name: str, signer, reason: str, signature_text, confirm,
              config=None, grants=None):
    """The pin this release leaves, or None. A pinned plugin's release that is not signed by a trusted key
    refuses; one signed by another trusted key moves the pin only after the user's typed yes."""
    held = snapshot.pins.get(manifest.plugin_id)
    if held is None:
        return {"signer_key_id": signer, "version": manifest.version} if trust_name == "signed" else None
    if trust_name != "signed":
        raise _update_refused(manifest, held["signer_key_id"], reason, signature_text, config,
                              _trusted_ids(snapshot))
    if signer != held["signer_key_id"]:
        question = (f"The plugin {manifest.plugin_id} is pinned to the key {held['signer_key_id']}; this release "
                    f"is signed by {signer}, which is on this install's trusted list, and "
                    f"{_grant_after(snapshot, grants, manifest.plugin_id, signer)}. "
                    f"Type yes to move the pin to {signer}:")
        if not _confirmed(confirm, question):
            raise NothingChanged(f"the plugin {manifest.plugin_id} stays pinned to the key "
                                 f"{held['signer_key_id']}: the answer was not yes, and nothing was written.")
    return {"signer_key_id": signer, "version": manifest.version}


def _grant_after(snapshot: Snapshot, grants, plugin_id: str, signer: str) -> str:
    """What the move leaves of the plugin's grant: kept and named, given and named, dropped, or none."""
    before = snapshot.grants.get(plugin_id)
    after = None if grants is None else grants.get(plugin_id)
    if after is not None:
        named = ", ".join(after.permissions) or "no permission"
        verb = "is kept" if after == before else "is given"
        return f"its grant of {named}, bound to this release and the key {signer}, {verb}"
    if before is not None:
        return "its grant is dropped"
    return "it holds no grant"


def _update_refused(manifest, key: str, reason: str, signature_text, config=None, trusted=None) -> InstallRefused:
    """``update_unsigned_over_signed``: what failed, and the two ways forward."""
    plugin_id = manifest.plugin_id
    if signature_text is None:
        what = "is unsigned"
    elif reason == "signer_not_on_trusted_list":
        what = f"is signed by the key {key_id(manifest.developer_key)}, which is not on this install's trusted list"
    else:
        what = f"does not pass this install's signature check ({reason})"
    if trusted is None or key in trusted:
        forward = f"Install a release signed by {key}"
    else:
        forward = (f"The key {key} is not on this install's trusted list: put it on the trusted list again, or "
                   f"install a release signed by a key that is on it")
    return InstallRefused("update_unsigned_over_signed", detail=(
        f"The plugin {plugin_id} is pinned to the key {key}, and its release {manifest.version} {what}. Nothing "
        f"was written: the installed release, its grant and its generation are as they were. "
        f"{forward}, or run: {_command(config, 'unpin', plugin_id)} (it asks a typed yes, then drops the pin "
        f"and every grant of {plugin_id}, which stays installed as a local unsigned plugin)."), show_detail=True)


def _trusted_ids(snapshot: Snapshot) -> frozenset:
    """The key ids of this install's trusted keys."""
    return frozenset(key_id(value) for value in snapshot.trusted_keys.values())


def _pins_from_grants(snapshot: Snapshot) -> dict:
    """For a file written before pins existed: each installed plugin whose grant names a signing key is pinned
    to it, at the installed version. A grant whose plugin is not installed pins nothing."""
    pins = {}
    for plugin_id, grant in snapshot.grants.items():
        plugin = snapshot.plugins.get(plugin_id)
        if grant.signer_key_id is None or plugin is None:
            continue
        pins[plugin_id] = {"signer_key_id": grant.signer_key_id, "version": parse_manifest(plugin.manifest).version}
    return pins


def _require_platform() -> None:
    # Same shape as daemon/plugins.py _read_owned: a typed refusal before any file is opened or made.
    # Windows has no fcntl, O_NOFOLLOW or O_DIRECTORY; a no-follow read for it is not built, so every
    # command refuses instead of dropping the guard (no default of 0, no open without it).
    # The guard names every POSIX call the commands make: the registry's reads, fcntl, geteuid, fchmod.
    missing = platform_gap()
    if missing is None and fcntl is None:
        missing = "fcntl"
    if missing is None:
        missing = next((name for name in ("geteuid", "fchmod") if not hasattr(os, name)), None)
    if missing is not None:
        raise InstallRefused("platform_unsupported", detail=f"{missing} is missing; no plugin file is opened")


def _lexists(path: Path) -> bool:
    """True if ``path`` is there, including a dangling symlink. ``Path.lexists`` is absent here."""
    return os.path.lexists(path)


def _refuse_by_arguments(plugin_id: object, version: object) -> None:
    """Refuse a reserved or malformed id, or a version that is not text, from the arguments alone.

    It reads nothing: an install that calls it first refuses such an id before any byte of a registry or
    of a git source is read, so a broken registry cannot hide the reason.
    """
    if type(plugin_id) is str and plugin_id in _STORE_RESERVED_IDS:
        raise InstallRefused("plugin_id_reserved",
                             detail=f"The name {plugin_id} is kept for the install's own files and is never a "
                                    f"plugin id. Pick another plugin id.", show_detail=True)
    if not _plugin_id_ok(plugin_id) or plugin_id in _PROFILE_NAMES:
        raise InstallRefused("plugin_not_in_registry")
    if version is not None and type(version) is not str:
        raise InstallRefused("release_not_in_registry")


def _plugin_id_ok(value: object) -> bool:
    return (type(value) is str and _PLUGIN_ID.fullmatch(value) is not None and value != "host"
            and value not in _STORE_RESERVED_IDS
            and value not in (".", "..") and "/" not in value and "\\" not in value)


def _releases(report, plugin_id: str) -> list:
    # Only releases under registry/ can be chosen. examples/ is checked, never indexed or installed.
    return [item for item in report.checked if item.plugin_id == plugin_id and not item.in_examples]


def _newest_active(matches: list):
    """The release an install by id alone takes: the newest active one, or None."""
    active = [item for item in matches if item.status == "active"]
    return active[-1] if active else None


def _select(report, plugin_id: str, version: str | None):
    """The release the arguments name. Its result goes straight into ``_installable``, on every road."""
    matches = _releases(report, plugin_id)
    if not matches:
        raise InstallRefused("plugin_not_in_registry")
    if version is None:
        newest = _newest_active(matches)
        if newest is None:
            raise InstallRefused("release_not_active")
        return newest
    for item in matches:
        if item.version == version:
            return item
    raise InstallRefused("release_not_in_registry")


@dataclass(frozen=True)
class _Road:
    """How a release was chosen for one install, and what the words of ``_installable`` name."""

    version: str | None
    allow_withdrawn: object
    registry_kind: str | None
    registry: str
    config: Path
    state: object
    git_repository: object

    def way(self) -> str:
        """The key of ``_WITHDRAWN_RULE`` for this road."""
        if self.version is None:
            return "chosen_for_the_person"
        if self.registry_kind == "pinned":
            return "named_at_a_registry_commit"
        if self.registry_kind in (None, "plain"):
            return "named_in_a_plain_folder"
        # "folder", and any value that is not a known kind, takes the registry folder's way.
        return "named_in_a_registry_folder"


# What a withdrawn release does on each way it can be chosen, when the person gave no --allow-withdrawn:
# "refuse" refuses release_withdrawn with the reason, the newest release that is not withdrawn and the one
# step; "say" installs it and says, before the install goes on, that it is withdrawn, why, and what is newer.
# A change of this policy changes this one rule. A pin and a range are "chosen_for_the_person" when they come.
# Each value is the behaviour of the base of this rule for that way: a registry at a commit and a registry
# folder read by install_from_registry refused a named withdrawn release; a folder read as it is (a direct
# call of install_plugin, or the command line's --registry <folder>) installed it, and now says so.
_WITHDRAWN_RULE = {
    "chosen_for_the_person": "refuse",
    "named_at_a_registry_commit": "refuse",
    "named_in_a_registry_folder": "refuse",
    "named_in_a_plain_folder": "say",
}


def _installable(chosen, report, road: _Road):
    """The one decision whether ``chosen`` may be installed: ``(chosen, words, not_needed)`` or a refusal.

    ``install_plugin`` calls it on what ``_select`` returns, on every road (an id alone, a named version, a
    registry folder, a registry at a commit, ``--git``, a direct call), before any source is fetched and
    before the profile lock, so before a release can be written or held. No keyword skips it. ``words`` is
    None for a release that is not withdrawn, else what the person is told before the install goes on. A
    step that names another release than the one chosen is refused, never silently ignored. A step that
    names the release chosen when that release is not withdrawn lets nothing through that would not go on
    without it: the install goes on as it did before the one decision, and ``not_needed`` (else None) tells
    the person so.
    """
    withdrawn = chosen.status == "withdrawn"
    allow = road.allow_withdrawn
    if allow is not None and allow != chosen.version:
        codes = ["release_withdrawn", "allow_withdrawn_unmatched"] if withdrawn else ["allow_withdrawn_unmatched"]
        raise InstallRefused(codes[0], codes, detail=_unmatched_words(chosen, report, road), show_detail=True)
    if not withdrawn:
        return chosen, None, (None if allow is None else _not_needed_words(chosen))
    if allow is None and _WITHDRAWN_RULE[road.way()] == "refuse":
        raise InstallRefused("release_withdrawn", detail=_withdrawn_words(chosen, report, road, installed=False),
                             show_detail=True)
    return chosen, _withdrawn_words(chosen, report, road, installed=True), None


def _install_command(road: _Road, plugin_id: str, version: str | None, allow: str | None = None) -> str:
    """The exact command that installs ``version`` of ``plugin_id`` from the road's registry, printable."""
    from ._entrypoints import _state_dir  # the one rule for the default state folder
    from .registry_source import printable

    words = ["install", plugin_id, "--registry", shlex.quote(road.registry)]
    if road.git_repository is not None:
        words += ["--git", shlex.quote(os.fspath(road.git_repository))]
    if version is not None:
        words += ["--version", shlex.quote(version)]
    if allow is not None:
        words += ["--allow-withdrawn", shlex.quote(allow)]
    if road.state is not None and Path(road.state).absolute() != _state_dir(None).absolute():
        words += ["--state", _shown(road.state)]
    return printable(_command(road.config, *words))


def _withdrawn_words(chosen, report, road: _Road, *, installed: bool) -> str:
    """That the release is withdrawn, by whom and why, what is newer, and what the person can do next."""
    from .registry_source import _reason_sentence, printable

    plugin_id, version = printable(chosen.plugin_id), printable(chosen.version)
    reason = _reason_sentence(chosen.status_reason)
    newest = _newest_active(_releases(report, chosen.plugin_id))
    if newest is None:
        instead = f"No release of {plugin_id} in that registry is active, so none can be taken in its place."
    else:
        instead = (f"The newest release of {plugin_id} there that is not withdrawn is {printable(newest.version)}; "
                   f"to install it run: {_install_command(road, chosen.plugin_id, newest.version)}")
    head = (f"WITHDRAWN: the release {version} of {plugin_id} was withdrawn by the keepers of the registry "
            f"{printable(road.registry)}, who give this reason: {reason}")
    if installed:
        how = ("you allowed it for this one install with --allow-withdrawn" if road.allow_withdrawn is not None
               else "you named it by its version")
        return f"{head} It is installed because {how}. {instead}"
    # Each printed command ends its line: nothing follows it, so it can be copied as printed.
    return (f"{head} Nothing was installed. {instead}\nTo install {version} anyway, at your own risk and for "
            f"this one install only, run: {_install_command(road, chosen.plugin_id, chosen.version, chosen.version)}")


def _unmatched_words(chosen, report, road: _Road) -> str:
    """The words of a step that names another release than the one chosen for this install."""
    from .registry_source import printable

    head = (f"The step --allow-withdrawn {printable(road.allow_withdrawn)} does not match this install: it lets "
            f"through only the one withdrawn release it names, and the release chosen here is "
            f"{printable(chosen.version)} of {printable(chosen.plugin_id)}, which is {printable(chosen.status)}. ")
    if chosen.status == "withdrawn":
        return head + _withdrawn_words(chosen, report, road, installed=False)
    return head + ("Nothing was installed. Run the install again without --allow-withdrawn; to install a "
                   "withdrawn release, name it with --version and with --allow-withdrawn, both with its version.")


def _not_needed_words(chosen) -> str:
    """The words of a step that names the release chosen for this install when it is not withdrawn."""
    from .registry_source import printable

    version = printable(chosen.version)
    return (f"The step --allow-withdrawn {version} is not needed: the release {version} of "
            f"{printable(chosen.plugin_id)} is {printable(chosen.status)}, not withdrawn, so the install goes on "
            f"as it would without the step, which you can leave out.")


def _install_locked(config: Path, chosen, plugin_id: str, state, artifact: bytes, fetched: bool,
                    report_kept=None, confirm=None, write_grant: bool | None = None,
                    confirm_grant=None) -> dict:
    manifest = parse_manifest(chosen.manifest)
    if manifest.plugin_id != plugin_id or not _plugin_id_ok(manifest.plugin_id):
        raise InstallRefused("release_plugin_id_mismatch")
    if chosen.label not in _LABELS:
        raise InstallRefused("release_label")
    if hashlib.sha256(artifact).hexdigest() != manifest.artifact_sha256:
        raise InstallRefused("source_digest_mismatch" if fetched else "artifact_digest_mismatch")
    signature_text = chosen.signature_text
    _finish_publish(config)
    snapshot = _load_snapshot(config)
    trust_name, reason, grants, findings, signer = _admit(snapshot, manifest, signature_text, artifact,
                                                           chosen.label, config, state, write_grant,
                                                           confirm_grant)
    refused = [item for item in findings if item.get("severity") == "refuse"]
    if refused:
        raise InstallRefused(refused[0]["code"], [item["code"] for item in refused])
    running = snapshot.plugins.get(manifest.plugin_id)
    if manifest.plugin_id in snapshot.pins and trust_name == "signed" and running is not None:
        asks = _asks_for_more(parse_manifest(running.manifest), manifest)
        if asks:
            return _hold(config, chosen, manifest, asks, artifact)
    pins = dict(snapshot.pins)
    pin = _next_pin(snapshot, manifest, trust_name, signer, reason, signature_text, confirm, config, grants)
    if pin is not None:
        pins[manifest.plugin_id] = pin
    plugins = dict(snapshot.plugins)
    plugins[manifest.plugin_id] = StoredPlugin(chosen.manifest, artifact, chosen.signature)
    labels = _labels_for(snapshot.labels, plugins)
    labels[hashlib.sha256(chosen.manifest).hexdigest()] = chosen.label
    updated = Snapshot(dict(snapshot.trusted_keys), grants, snapshot.hosts, snapshot.prefixes, snapshot.anchor,
                       labels, plugins, snapshot.developer_key_ids, pins=pins, pins_held=snapshot.pins_held,
                       disabled=snapshot.disabled - {manifest.plugin_id})
    notes = []
    generation = _commit(config, updated, notes, report_kept)
    # What the written install.json gives this plugin, as the loader's admit step computed it from those grants.
    effective = next((item["detail"] for item in findings if item.get("code") == "effective_permissions"), "")
    result = _with_notes({
        "command": "install",
        "plugin_id": manifest.plugin_id,
        "version": manifest.version,
        "status": chosen.status,
        "status_reason": chosen.status_reason,
        "registry_label": chosen.label,
        "trust": trust_name,
        "signature": reason,
        "generation": generation,
        "order": "granted" if "order" in effective.split(",") else "not granted",
        "developer_checks": list(_DEVELOPER_CHECKS),
        "findings": findings,
        "requested_permissions": list(manifest.permissions),
    }, notes)
    if fetched:
        result["fetched"] = True
    return result


@_typed_refusals
def quarantine_list(config: os.PathLike | str, *, state: os.PathLike | str | None = None,
                    wait: bool = True) -> dict:
    """Each held release: its plugin, version, place and what more it asks than the running release. A held
    release that does not read is a row marked ``damaged``, with no asks and the command that drops it."""
    _require_platform()
    del state
    config_path = Path(config)
    fd = _lock(config_path, wait=wait)
    try:
        _finish_publish(config_path)
        snapshot = _load_snapshot(config_path)
        rows = []
        quarantine = config_path / _QUARANTINE
        if _lexists(quarantine):
            _require_owned_dir(quarantine)
            for plugin_id in sorted(os.listdir(quarantine)):
                folder = quarantine / plugin_id
                if not _plugin_id_ok(plugin_id) or not _owned_dir(folder):
                    continue
                for version in sorted(os.listdir(folder)):
                    try:
                        manifest = _held_manifest(folder / version, plugin_id, _read_held(folder / version))
                    except InstallRefused:
                        if _VERSION_DIR.fullmatch(version) is not None:
                            rows.append({"plugin_id": plugin_id, "version": version,
                                         "held_at": str(folder / version), "asks": [], "damaged": True,
                                         "next": _damaged(folder / version, plugin_id)})
                        continue
                    running = snapshot.plugins.get(plugin_id)
                    rows.append({"plugin_id": plugin_id, "version": version, "held_at": str(folder / version),
                                 "asks": _asks_for_more(None if running is None else parse_manifest(running.manifest),
                                                        manifest)})
        return {"command": "quarantine list", "held": rows}
    finally:
        os.close(fd)


@_typed_refusals
def quarantine_accept(config: os.PathLike | str, plugin_id: str, *, state: os.PathLike | str | None = None,
                      wait: bool = True, report_kept=None, confirm=None) -> dict:
    """Check the held release of ``plugin_id`` again and install it after the user's typed yes.

    The new grant holds what the release requests except ``order``. Any answer but ``yes``, or no
    ``confirm``, writes nothing. A release that no longer passes the checks refuses as an install would.
    """
    _require_platform()
    config_path = Path(config)
    if not _plugin_id_ok(plugin_id):
        raise NothingChanged(_nothing_held(config_path, None))
    fd = _lock(config_path, wait=wait)
    try:
        _finish_publish(config_path)
        folder = _held_folder(config_path, plugin_id, create=False)
        names = sorted(os.listdir(folder)) if folder is not None else []
        if not names:
            raise NothingChanged(_nothing_held(config_path, plugin_id))
        if len(names) != 1 or _VERSION_DIR.fullmatch(names[0]) is None:
            raise InstallRefused("plugin_directory_untrusted", detail=(
                f"The folder {folder} holds more than one release, or an entry this command did not write. Run: "
                f"{_command(config_path, 'quarantine', 'drop', plugin_id)}; then install the release again."),
                show_detail=True)
        held = folder / names[0]
        files = _read_held(held)
        manifest = _held_manifest(held, plugin_id, files)
        label = files[_HELD_LABEL].decode("ascii").strip()
        signature = files.get("manifest.sig")
        signature_text = None if signature is None else signature.decode("ascii", "replace").strip()
        snapshot = _load_snapshot(config_path)
        trust_name, reason, grants, findings, signer = _admit(snapshot, manifest, signature_text, files["plugin.py"],
                                                               label, config_path, state)
        refused = [item for item in findings if item.get("severity") == "refuse"]
        if refused:
            raise InstallRefused(refused[0]["code"], [item["code"] for item in refused])
        if plugin_id in snapshot.pins and trust_name != "signed":
            raise _update_refused(manifest, snapshot.pins[plugin_id]["signer_key_id"], reason, signature_text,
                                  config_path, _trusted_ids(snapshot))
        granted = tuple(perm for perm in manifest.permissions if perm != "order")
        running = snapshot.plugins.get(plugin_id)
        asks = _asks_for_more(None if running is None else parse_manifest(running.manifest), manifest)
        question = " ".join([f"Install the held release {manifest.version} of the plugin {plugin_id} and grant it: "
                             f"{', '.join(granted) or 'no permission'} (order is never granted this way)?", *asks,
                             "Type yes to install it:"])
        if not _confirmed(confirm, question):
            raise NothingChanged(f"the release {manifest.version} of the plugin {plugin_id} stays held at {held}: "
                                 f"the answer was not yes, and nothing was written.")
        grants = dict(grants)
        grants[plugin_id] = Grant(plugin_id, manifest.artifact_sha256, signer, granted)
        pins = dict(snapshot.pins)
        pin = _next_pin(snapshot, manifest, trust_name, signer, reason, signature_text, confirm, config_path, grants)
        if pin is not None:
            pins[plugin_id] = pin
        developer_key_ids = {key: value for key, value in snapshot.developer_key_ids.items() if key != plugin_id}
        plugins = dict(snapshot.plugins)
        plugins[plugin_id] = StoredPlugin(files["manifest.json"], files["plugin.py"], signature)
        labels = _labels_for(snapshot.labels, plugins)
        labels[hashlib.sha256(files["manifest.json"]).hexdigest()] = label
        updated = Snapshot(dict(snapshot.trusted_keys), grants, snapshot.hosts, snapshot.prefixes, snapshot.anchor,
                           labels, plugins, developer_key_ids, pins=pins, pins_held=snapshot.pins_held,
                           disabled=snapshot.disabled - {plugin_id})
        notes = []
        generation = _commit(config_path, updated, notes, report_kept)
        notes.extend(_drop_held_copy(held, files))
        return _with_notes({"command": "quarantine accept", "plugin_id": plugin_id, "version": manifest.version,
                            "generation": generation, "trust": trust_name, "granted": list(granted),
                            "order": "not granted", "plugins": _rows(updated)}, notes)
    finally:
        os.close(fd)


@_typed_refusals
def quarantine_drop(config: os.PathLike | str, plugin_id: str, *, state: os.PathLike | str | None = None,
                    wait: bool = True) -> dict:
    """Move the held release of ``plugin_id`` into a kept place and name it. Nothing is deleted."""
    _require_platform()
    del state
    config_path = Path(config)
    if not _plugin_id_ok(plugin_id):
        raise NothingChanged(_nothing_held(config_path, None))
    fd = _lock(config_path, wait=wait)
    try:
        folder = _held_folder(config_path, plugin_id, create=False)
        names = sorted(os.listdir(folder)) if folder is not None else []
        if not names:
            raise NothingChanged(_nothing_held(config_path, plugin_id))
        return _with_notes({"command": "quarantine drop", "plugin_id": plugin_id},
                           _set_aside(config_path, plugin_id, folder, names))
    finally:
        os.close(fd)


def _nothing_held(config: Path, plugin_id: str | None) -> str:
    which = "of that id" if plugin_id is None else f"of the plugin {plugin_id}"
    return (f"no release {which} is held in quarantine; nothing was written. To see what is held run: "
            f"{_command(config, 'quarantine', 'list')}")


def _asks_for_more(running, release) -> list:
    """One line for each permission, network destination, credential reference or install script ``release``
    requests that the running release (None: no release is installed) did not."""
    def new(name: str) -> list:
        return [value for value in getattr(release, name) if running is None or value not in getattr(running, name)]

    more = ([f"the permission {perm}" for perm in new("permissions")]
            + [f"the network destination {host}" for host in new("network_destinations")]
            + [f"the credential reference {ref}" for ref in new("credential_refs")]
            + [f"the install script {script}" for script in new("install_scripts")])
    tail = ("and no release of it is installed" if running is None
            else f"which the running release {running.version} did not request")
    return [f"The release {release.version} of the plugin {release.plugin_id} asks for {what}, {tail}."
            for what in more]


def _hold(config: Path, chosen, manifest, asks: list, artifact: bytes) -> dict:
    """Keep the checked files of a release that asks for more under ``plugin-quarantine/<id>/<version>/``.

    Nothing is installed and no generation is written. A release held before for this plugin is moved into a
    kept place and named; the same release, byte for byte, is left as it is.
    """
    plugin_id, version = manifest.plugin_id, manifest.version
    if _VERSION_DIR.fullmatch(version) is None:
        raise InstallRefused("release_version")
    files = {"manifest.json": chosen.manifest, "plugin.py": artifact,
             _HELD_LABEL: f"{chosen.label}\n".encode("ascii")}
    if chosen.signature is not None:
        files["manifest.sig"] = chosen.signature
    folder = _held_folder(config, plugin_id, create=True)
    held = folder / version
    names = sorted(os.listdir(folder))
    notes = []
    try:
        same = names == [version] and _read_held(held) == files
    except InstallRefused:
        same = False
    if not same:
        if names:
            notes = _set_aside(config, plugin_id, folder, names)
        partial = folder / f".partial-{version}"
        partial.mkdir(mode=0o700)
        os.chmod(partial, 0o700)
        for name in sorted(files):
            _write_file(partial / name, files[name])
        _sync_dir(partial)
        os.rename(partial, held)
        _sync_dir(folder)
    step = (f"The release {version} of the plugin {plugin_id} was not installed: it is held at {held}, and the "
            f"running release stays live with its grant. To install it run: "
            f"{_command(config, 'quarantine', 'accept', plugin_id)} (it asks a typed yes); to drop it run: "
            f"{_command(config, 'quarantine', 'drop', plugin_id)}; to see what is held run: "
            f"{_command(config, 'quarantine', 'list')}")
    return _with_notes({"command": "install", "result": "quarantined", "plugin_id": plugin_id, "version": version,
                        "trust": "signed", "held_at": str(held), "asks": list(asks), "next": step}, notes)


def _held_folder(config: Path, plugin_id: str, *, create: bool) -> Path | None:
    """``plugin-quarantine/<plugin_id>``, owner-only; made when ``create``, else None when it is not there."""
    quarantine = config / _QUARANTINE
    if not _lexists(quarantine):
        if not create:
            return None
        _make_dir(quarantine)
        _sync_dir(config)
    _require_owned_dir(quarantine)
    folder = quarantine / plugin_id
    if not _lexists(folder):
        if not create:
            return None
        _make_dir(folder)
        _sync_dir(quarantine)
    _require_owned_dir(folder)
    return folder


def _read_held(held: Path) -> dict:
    """The files of a held release, read with the checks of a generation. Anything else there refuses."""
    _require_owned_dir(held)
    names = set(os.listdir(held))
    if not names <= set(_HELD_LIMITS) or not {"manifest.json", "plugin.py", _HELD_LABEL} <= names:
        raise InstallRefused("plugin_directory_untrusted", detail=(
            f"The folder {held} is not a release this command held. Run: "
            f"{_command(held.parents[2], 'quarantine', 'drop', held.parent.name)}; then install the release again."),
            show_detail=True)
    return {name: _read_file(held / name, _HELD_LIMITS[name]) for name in sorted(names)}


def _held_manifest(held: Path, plugin_id: str, files: dict):
    """The held release's manifest, when it is the one its place names and its label is one a release has."""
    try:
        manifest = parse_manifest(files["manifest.json"])
        label = files[_HELD_LABEL].decode("ascii").strip()
    except (Refused, UnicodeDecodeError):
        raise InstallRefused("generation_unreadable", detail=_damaged(held, plugin_id), show_detail=True) from None
    if (manifest.plugin_id != plugin_id or manifest.version != held.name or label not in _LABELS
            or hashlib.sha256(files["plugin.py"]).hexdigest() != manifest.artifact_sha256):
        raise InstallRefused("generation_unreadable", detail=_damaged(held, plugin_id), show_detail=True)
    return manifest


def _damaged(held: Path, plugin_id: str) -> str:
    """The way out for a held release whose files do not read as the release its place names."""
    return (f"The release held at {held} does not read as the release {held.name} of the plugin {plugin_id} "
            f"that this command held, so it cannot be accepted. Run: "
            f"{_command(held.parents[2], 'quarantine', 'drop', plugin_id)}")


def _set_aside(config: Path, plugin_id: str, folder: Path, names: list) -> list:
    """Move each entry of ``folder`` into ``plugin-generations/.kept-<n>/<plugin_id>``. One line for each."""
    root = config / "plugin-generations"
    if not _lexists(root):
        _make_dir(root)
        _sync_dir(config)
    _require_owned_dir(root)
    place = _kept_place(root, _next_number(root, _read_current(config)))
    place.mkdir(mode=0o700)
    os.chmod(place, 0o700)
    kept = place / plugin_id
    kept.mkdir(mode=0o700)
    os.chmod(kept, 0o700)
    lines = []
    try:
        for name in names:
            release = _VERSION_DIR.fullmatch(name) is not None and stat.S_ISDIR(os.lstat(folder / name).st_mode)
            os.rename(folder / name, kept / name)
            if release:
                lines.append(f"The held release {name} of the plugin {plugin_id} is kept at {kept / name}; nothing "
                             f"was deleted.")
            else:
                lines.append(f"The entry {folder / name} held for the plugin {plugin_id} is kept at {kept / name}; "
                             f"nothing was deleted.")
    except OSError as exc:
        _carry(exc, lines)
        raise
    _sync_dir(kept)
    _sync_dir(folder)
    _sync_dir(place)
    _sync_dir(root)
    return lines


def _drop_held_copy(held: Path, files: dict) -> list:
    """After the new generation holds the release: remove its held copy when it is still the one read."""
    try:
        same = _read_held(held) == files
    except InstallRefused:
        same = False
    if not same:
        return [f"The folder {held} changed while this command ran and was left in place."]
    _remove_at(held)
    _sync_dir(held.parent)
    return []


def _with_notes(result: dict, notes: list) -> dict:
    """``kept``: one line for each plugin whose extra entries the command kept aside (for ``list``: each kept
    place that holds entries). Absent when none."""
    if notes:
        result["kept"] = list(notes)
    return result


def _admit(snapshot: Snapshot, manifest, signature_text, artifact: bytes, label: str, config: Path,
           state, write_grant: bool | None = None, confirm_grant=None):
    """Classify and run doctor. A grant for another digest or signer is dropped.

    A yes writes one new grant for this release: requested permissions except
    ``order``, bound to the artifact digest and the signer key id. Anything
    short of an explicit yes writes no new grant.
    """
    path = _revocation_store(config, state)
    try:
        trust = TrustStore(dict(snapshot.trusted_keys), revocation_store=path)
    except Refused as exc:
        raise InstallRefused(exc.code, detail=exc.detail) from None
    try:
        try:
            trust_name, signer, reason = trust.classify(manifest, signature_text)
        except Refused as exc:
            raise InstallRefused(exc.code, detail=exc.detail) from None
        grants = _grants_kept(snapshot.grants, manifest, signer)
        if _wants_grant(write_grant, confirm_grant, manifest):
            grants = dict(grants)
            grants[manifest.plugin_id] = _grant_for_release(manifest, signer)
        loader = PluginLoader(trust, grants, snapshot.reserved(), CoreHealth())
        findings = loader.doctor(manifest.raw, signature_text, artifact, label)
    finally:
        trust.close()
    return trust_name, reason, grants, findings, signer


def _wants_grant(write_grant: bool | None, confirm, manifest) -> bool:
    """True only for an explicit yes. A missing answer, or any answer but yes, is no."""
    if write_grant is True:
        return True
    if write_grant is False or confirm is None:
        return False
    answer = confirm(list(manifest.permissions), list(_DEVELOPER_CHECKS))
    return answer is True


def _grant_for_release(manifest, signer: str | None) -> Grant:
    """One grant: requested permissions except ``order``, bound to digest and signer."""
    permissions = tuple(perm for perm in manifest.permissions if perm != "order")
    return Grant(manifest.plugin_id, manifest.artifact_sha256, signer, permissions)


def _grants_kept(grants: dict, manifest, signer: str | None) -> dict:
    """Keep this plugin's grant only when the artifact digest and the signer still match."""
    kept = dict(grants)
    grant = kept.get(manifest.plugin_id)
    if grant is None:
        return kept
    if grant.artifact_sha256 != manifest.artifact_sha256 or grant.signer_key_id != signer:
        del kept[manifest.plugin_id]
    return kept


def _revocation_store(config: Path, state) -> str | None:
    """Return the revocation store, creating it only on a first install.

    A first install is all three of: the store file absent, no
    ``plugin-generations`` folder, and no ``plugins/install.json``. Creation
    goes through the public ``create_revocation_store`` (mode 0600) after the
    same folder checks the test fixture applies: a symlink, a non-directory,
    any group or other bit, or a directory that cannot be written is refused
    and the file is not created. The folder, when created, is mode 0700.

    Any other absent store is refused ``revocation_store_missing``. The detail
    names the profile state that blocks a new file. A symlink, or anything
    else that is not a regular file, at the store path is
    ``revocation_store_invalid``. Called under the profile lock. ``state``
    None has no directory to create into and does not create one.
    """
    if state is None:
        return None
    path = Path(state) / "plugin-security" / "revoked.sqlite3"
    try:
        if path.is_symlink():
            raise _link_refusal(path, "revocation_store_invalid")
        if path.is_file():
            return str(path)
        if _lexists(path):
            raise InstallRefused("revocation_store_invalid")
    except InstallRefused:
        raise
    except OSError as exc:
        raise InstallRefused("revocation_store_invalid", detail=type(exc).__name__) from None
    if not _first_install(config, path):
        raise InstallRefused("revocation_store_missing", detail=_blocked_store_state(config, path))
    _create_first_store(path)
    return str(path)


def _first_install(config: Path, store: Path) -> bool:
    """True only when this profile has never had a store, a generation, or an install record."""
    if _lexists(store):
        return False
    for suffix in ("-wal", "-shm"):
        if _lexists(Path(str(store) + suffix)):
            return False
    if _lexists(config / "plugin-generations"):
        return False
    if _lexists(config / "plugins" / "install.json"):
        return False
    return True


def _blocked_store_state(config: Path, store: Path) -> str:
    """Name the profile facts that make an absent store a refusal, not a first install."""
    names = []
    if _lexists(config / "plugin-generations"):
        names.append("plugin-generations")
    if _lexists(config / "plugins" / "install.json"):
        names.append("install.json")
    if _lexists(store):
        names.append("revoked.sqlite3")
    for suffix in ("-wal", "-shm"):
        if _lexists(Path(str(store) + suffix)):
            names.append("revoked.sqlite3" + suffix)
    if not names:
        names.append("plugin-security/revoked.sqlite3")
    return " ".join(names)


def _create_first_store(path: Path) -> None:
    """Create ``path`` for a first install, or refuse and leave the file absent.

    The checks and the modes are the ones ``ensure_revocation_store`` applies
    before it calls ``create_revocation_store``: the directory is 0700 and
    writable, and the file is the public create at 0600.
    """
    import sqlite3

    directory = path.parent
    try:
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise InstallRefused("revocation_store_invalid")
        if directory.exists():
            info = directory.stat()
            if stat.S_IMODE(info.st_mode) & 0o077 or not os.access(directory, os.W_OK | os.X_OK):
                raise InstallRefused("revocation_store_invalid")
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        info = os.lstat(directory)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise InstallRefused("revocation_store_invalid")
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise InstallRefused("revocation_store_invalid")
        from recursion_core.plugin_security import create_revocation_store
        create_revocation_store(str(path))
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fchmod(fd, 0o600)
            file_info = os.fstat(fd)
        finally:
            os.close(fd)
        if not stat.S_ISREG(file_info.st_mode) or stat.S_IMODE(file_info.st_mode) != 0o600:
            raise InstallRefused("revocation_store_invalid")
    except InstallRefused:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise InstallRefused("revocation_store_invalid", detail=type(exc).__name__) from None


def _labels_for(previous: dict, plugins: dict) -> dict:
    digests = {hashlib.sha256(plugin.manifest).hexdigest() for plugin in plugins.values()}
    return {digest: label for digest, label in previous.items() if digest in digests and label in _LABELS}


def _rows(snapshot: Snapshot) -> list[dict]:
    rows = []
    for plugin in snapshot.plugins.values():
        manifest = parse_manifest(plugin.manifest)
        digest = hashlib.sha256(plugin.manifest).hexdigest()
        row = {
            "plugin_id": manifest.plugin_id,
            "version": manifest.version,
            "registry_label": snapshot.labels.get(digest, "unlisted"),
        }
        if manifest.plugin_id in snapshot.disabled:
            row["enabled"] = False
        rows.append(row)
    rows.sort(key=lambda row: row["plugin_id"])
    return rows


def _lock(config: Path, wait: bool = True) -> int:
    """Exclusive lock on the profile. The caller closes the returned descriptor."""
    _require_config(config)
    path = config / "plugin-install.lock"
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _link_refusal(path, "plugin_directory_untrusted") from None
        raise InstallRefused("plugin_directory_untrusted", detail=(
            f"The lock file {path} cannot be opened for writing ({exc.strerror or type(exc).__name__}). Make it "
            f"writable by its owner with: chmod 600 {_shown(path)}; or remove it while no plugin "
            f"command runs; then run the command again."), show_detail=True) from None
    try:
        os.fchmod(fd, 0o600)
        flags = fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(fd, flags)
    except BlockingIOError:
        os.close(fd)
        raise InstallRefused("install_busy") from None
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
            raise InstallRefused("install_busy") from None
        raise
    return fd


def _require_config(config: Path) -> None:
    # lstat first: a link, a non-directory, another owner or group or other write refuses, and nothing
    # is changed. Only a directory this call created gets its mode set.
    if not _lexists(config):
        config.parent.mkdir(parents=True, exist_ok=True)
        _make_dir(config)
    _require_owner_only_config(config)
    _require_owned_dir(config)


def _require_owner_only_config(config: Path) -> None:
    """A config folder of this user that group or other users may write refuses with the one command that
    makes it owner-only. Its mode and its contents are left as they are: nothing here narrows it."""
    try:
        info = os.lstat(config)
    except OSError:
        return  # _require_owned_dir refuses it
    if stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and info.st_mode & 0o022:
        raise InstallRefused("plugin_directory_untrusted", detail=(
            f"The folder {config} can be written by other users, and no plugin is installed or read from such a "
            f"folder. Make it owner-only with: chmod 700 {_shown(config)}; then run the command again."),
            show_detail=True)


def _make_dir(path: Path) -> None:
    """Make ``path`` owner-only. A name that is already there is left as it is for the caller's check."""
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        return
    os.chmod(path, 0o700)


def _finish_publish(config: Path) -> None:
    """Finish a publish that stopped, or drop the marker of one that never reached the pointer.

    Without ``.publishing`` the live tree is as the last command left it, or as the user edited it,
    and nothing here touches it.
    """
    record = _read_record(config)
    if record is None:
        return None
    publishing = int(record.group(1))
    root = config / "plugin-generations"
    kept = []
    finished = None
    if publishing == _read_current(config):
        replaced = int(record.group(2)) if record.group(2) else None
        sources = [root / str(number) for number in (publishing, replaced) if number is not None]
        published = sources[0]
        staged = set(os.listdir(published)) if os.path.isdir(published) else set()
        _check_install_file(config, sources)
        _check_publish(config, sources, staged - _PROFILE_NAMES)
        kept = _copy_generation_to_live(config, publishing, sources)
        finished = (publishing, replaced)
    _clear_marker(root)
    _refuse_kept(kept)
    return finished


def _finished_note(config: Path, finished) -> str:
    """One sentence for a stopped publish a command finished first: the generation, and the plugins it changed."""
    if finished is None:
        return ""
    publishing, replaced = finished
    root = config / "plugin-generations"
    try:
        new = _read_profile(root / str(publishing), missing_install_ok=False).plugins
        old = {} if replaced is None else _read_profile(root / str(replaced), missing_install_ok=False).plugins
        changed = sorted(name for name in set(new) | set(old) if new.get(name) != old.get(name))
    except (InstallRefused, OSError):
        changed = []
    which = f", which changes the plugin {', '.join(changed)}," if changed else ""
    return (f"A publish that had stopped part way was finished first: generation {publishing}{which} is now in "
            f"the live folder {config / 'plugins'}.")


def _extras_note(config: Path, snapshot: Snapshot) -> str:
    """For the question before a commit that keeps every plugin as it is: the entries it will move aside, as every
    commit does, and the kept place, or that none will be."""
    current = _read_current(config)
    root = config / "plugin-generations"
    extras = []
    if current is not None:
        installed = root / str(current)
        try:
            extras = _check_publish(config, [installed], set(snapshot.plugins), installed)
        except (InstallRefused, OSError):
            extras = []  # the commit refuses as before, after the answer
    extras = [(name, more) for name, more in extras if more]
    if not extras:
        return "No plugin folder holds an entry its plugin was not installed with, so nothing is moved aside."
    place = _kept_place(root, _next_number(root, current))
    moved = "; ".join(f"{', '.join(more)} from {config / 'plugins' / name}" for name, more in extras)
    return (f"As every change does, it also moves aside the entries plugin folders hold that their plugins were "
            f"not installed with: {moved}; they are kept at {place}.")


def _check_install_file(config: Path, sources: list) -> None:
    """Before a stopped publish is finished: the live install.json is absent or one of ``sources``' copies."""
    path = config / "plugins" / "install.json"
    copies = [source / "install.json" for source in sources]
    if not _lexists(path) or any(_same_tree(path, copy) for copy in copies):
        return
    if not _lexists(sources[0]):
        return  # the generation the record names is missing: the publish refuses generation_missing
    damaged = _damaged_record(sources)
    if damaged is not None:
        record = config / "plugin-generations" / _PUBLISHING
        raise InstallRefused("plugin_directory_untrusted", detail=(
            f"The file {path} is not the copy of the generation the record {record} names, and that record is "
            f"damaged: {damaged}. This command cannot tell whether anyone edited it. Save it elsewhere and put "
            f"{copies[0]} in its place; then run the command again."), show_detail=True)
    if len(copies) == 1:
        where = f"The copy it recorded is {copies[0]}. Save the changed file elsewhere and put that copy in its place"
    else:
        where = (f"The two copies it recorded are {copies[0]} and {copies[1]}. Save the changed file elsewhere "
                 f"and put either copy in its place")
    raise InstallRefused("plugin_directory_untrusted", detail=(f"The file {path} was changed after a command "
                                                               f"stopped part way. {where}; then run the command "
                                                               f"again."), show_detail=True)


def _damaged_record(sources: list) -> str | None:
    """Why the record cannot say which copies the live install.json may be; None when it can."""
    publishing = sources[0]
    if len(sources) == 2:
        if sources[1] == publishing:
            return f"it names {publishing} twice"
        if not _lexists(sources[1]):
            return f"the generation {sources[1]} it names is not on disk"
        return None
    older = [int(name) for name in os.listdir(publishing.parent)
             if _GENERATION.fullmatch(name) and int(name) < int(publishing.name)]
    if older:
        return f"it names no generation it replaced, though {publishing.parent / str(max(older))} is on disk"
    return None


def _read_record(config: Path):
    """The ``.publishing`` record. One this command cannot read, or a link, refuses with the steps that end it."""
    path = config / "plugin-generations" / _PUBLISHING
    try:
        return _read_pointer(config, _PUBLISHING, _RECORD)
    except InstallRefused as exc:
        link = exc.code == "plugin_directory_untrusted" and _owned_dir(path.parent) and path.is_symlink()
        if not link and exc.code not in ("generation_unreadable", "plugin_file_untrusted", "plugin_file_too_large"):
            raise
        raise InstallRefused(exc.code, detail=(f"The file {path} is not a publish record this command wrote. "
                                               f"Move it out of {path.parent}; then run the command again."),
                             show_detail=True) from None


def _check_publish(config: Path, sources: list, staged: set, installed: Path | None = None,
                   removing: str | None = None) -> list:
    """Before a publish changes the live tree, prove every live entry the swap would move is the installer's.

    The swap moves every live entry under a name the generation writes, of any kind, and every folder and
    link. Each must be a whole copy, file for file and byte for byte, of the entry of that name in one of
    ``sources``: the generation being published and the one it replaces, the two the record names. A
    folder that holds anything more, a file, a link or a FIFO is not: the command refuses with the steps
    that end the refusal and nothing is moved or deleted. Nothing here opens an entry that is not a
    regular file.

    ``installed`` is the current generation when no stopped publish is being finished. A folder of a
    plugin it holds, whose own files are each its copy byte for byte, does not refuse for entries it holds
    besides them: those are returned, as (plugin id, extra names), for the caller to keep aside.

    ``removing`` is the plugin a remove drops (only with ``installed``). Its live entry, of any kind, never
    refuses: when it is not a whole copy and not a proven folder with extras, it is returned as (plugin id,
    None), for the caller to keep aside whole.
    """
    live = config / "plugins"
    extras = []
    if not _lexists(live):
        return extras
    for name in sorted(os.listdir(live)):
        if name == "install.json":
            continue
        path = live / name
        info = os.lstat(path)
        dropped = installed is not None and name == removing
        if name not in staged and not dropped and not (stat.S_ISLNK(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            continue  # the swap leaves it where it is
        if any(_same_tree(path, source / name) for source in sources):
            continue
        more = _extra_entries(path, installed / name) if installed is not None else None
        if not more:
            if not dropped:
                raise _way_out(path)
            more = None  # the whole entry is kept aside
        extras.append((name, more))
    return extras


def _extra_entries(live: Path, written: Path) -> list | None:
    """The names in the live folder ``live`` that ``written`` does not hold, when every entry of ``written``
    is in ``live`` byte for byte; None when one is changed or missing, or either is not a folder."""
    try:
        seen, kept = os.lstat(live), os.lstat(written)
    except FileNotFoundError:
        return None
    if not (stat.S_ISDIR(seen.st_mode) and stat.S_ISDIR(kept.st_mode)):
        return None
    own, names = set(os.listdir(written)), set(os.listdir(live))
    if not own <= names or not all(_same_tree(live / name, written / name) for name in sorted(own)):
        return None
    return sorted(names - own) or None


def _keep_extras(config: Path, number: int, extras: list, report=None) -> list:
    """Move each extra entry into ``plugin-generations/.kept-<number>/<plugin id>``. One line for each plugin.

    An item whose extra names are None is a whole live entry (the plugin a remove drops): it is moved to
    ``.kept-<number>/<plugin id>`` in one rename, so nothing is half moved, and a link is moved itself,
    never followed.

    Each line goes to ``report`` as soon as every entry is moved, before any sync or later step can fail.
    When an entry cannot be moved, what was moved is put back, no kept folder is left, and the command
    refuses with the steps that end the refusal; an entry that cannot be put back is named by its line.
    """
    if not extras:
        return []
    root = config / "plugin-generations"
    place = _kept_place(root, number)
    place.mkdir(mode=0o700)
    made, moved, failed = [place], [], None
    try:
        os.chmod(place, 0o700)
        for name, more in extras:
            folder = config / "plugins" / name
            kept = place / name
            if more is None:
                try:
                    os.rename(folder, kept)
                except OSError as exc:
                    failed = (name, folder, None, exc)
                    raise
                moved.append((name, folder, kept))
                continue
            kept.mkdir(mode=0o700)
            made.append(kept)
            os.chmod(kept, 0o700)
            for entry in more:
                try:
                    os.rename(folder / entry, kept / entry)
                except OSError as exc:
                    failed = (name, folder, entry, exc)
                    raise
                moved.append((name, folder / entry, kept / entry))
    except Exception as exc:
        left = _put_back(moved, made)
        if failed is None:
            _carry(exc, left)
            raise
        name, folder, entry, error = failed
        if entry is None:
            raise InstallRefused("plugin_directory_untrusted", detail=" ".join([*left, (
                f"The folder {folder} of the plugin {name} is not the copy this command installed and cannot be "
                f"moved aside ({error.strerror or error}). Move it out of {folder.parent} yourself (a folder must "
                f"allow writing to be moved); then run the command again.")]), show_detail=True) from None
        raise InstallRefused("plugin_directory_untrusted", detail=" ".join([*left, (
            f"The folder {folder} holds {folder / entry}, which the plugin {name} was not installed with and "
            f"which cannot be moved aside ({error.strerror or error}). Move it out of {folder} yourself (a "
            f"folder must allow writing to be moved); then run the command again.")]), show_detail=True) from None
    notes = [_kept_note(config, place, name, more) for name, more in extras]
    if report is not None:
        for line in notes:
            report(line)
    for name, more in extras:
        if more is not None:
            _sync_dir(place / name)
            _sync_dir(config / "plugins" / name)
    if any(more is None for _name, more in extras):
        _sync_dir(config / "plugins")  # the moved entry itself is not opened: it may be a link
    _sync_dir(place)
    _sync_dir(root)
    return notes


def _kept_note(config: Path, place: Path, name: str, more) -> str:
    """The line that names where the entries of the plugin ``name`` are kept."""
    live = config / "plugins" / name
    if more is not None:
        return (f"The folder {live} held entries the plugin {name} was not installed with: they are kept at "
                f"{place / name}.")
    mode = os.lstat(place / name).st_mode
    kind = "link" if stat.S_ISLNK(mode) else "folder" if stat.S_ISDIR(mode) else "file"
    return (f"The {kind} {live} of the plugin {name} is not the copy this command installed: it is kept at "
            f"{place / name}.")


def _put_back(moved: list, made: list) -> list:
    """Move each kept entry back, newest first, and remove the kept folders left empty.

    Returns one line for each entry that could not be put back: it stays where it was kept.
    """
    left = []
    for name, live, kept in reversed(moved):
        try:
            if _lexists(live):
                raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST))
            os.rename(kept, live)
        except OSError:
            left.append(f"The entry {live} of the plugin {name} could not be put back: it is kept at {kept}.")
    for folder in reversed(made):
        try:
            os.rmdir(folder)
        except OSError:
            pass
    return left


def _carry(exc: BaseException, lines: list) -> None:
    """Put the lines of entries already kept on a failure raised after the move, so its refusal names them."""
    if not lines:
        return
    if isinstance(exc, InstallRefused):
        exc.detail = " ".join([*lines, *([exc.detail] if exc.show_detail and exc.detail else [])])
        exc.show_detail = True
    else:
        exc.kept = [*getattr(exc, "kept", ()), *lines]


def _kept_lines(config: Path) -> list:
    """One line for each entry of a kept place that is still there: which plugin, and where. Nothing is
    followed: a kept name that is a link or not a folder, and an empty folder in a kept place, are not named."""
    root = config / "plugin-generations"
    if not _owned_dir(root):
        return []
    lines = []
    for name in sorted(os.listdir(root)):
        place = root / name
        if _KEPT.fullmatch(name) is None or not stat.S_ISDIR(os.lstat(place).st_mode):
            continue
        try:
            children = sorted(os.listdir(place))
        except OSError:
            continue
        for child in children:
            try:
                if stat.S_ISDIR(os.lstat(place / child).st_mode) and not os.listdir(place / child):
                    continue
            except OSError:
                continue
            if _plugin_id_ok(child):
                lines.append(f"The entries kept aside from the plugin {child} are at {place / child}.")
            else:
                lines.append(f"The entry kept aside from {config / 'plugins' / child} is at {place / child}.")
    return lines


def _kept_place(root: Path, generation: int) -> Path:
    """``root/.kept-<generation>``, or the first free ``-<k>`` after it. No command removes one."""
    place = root / f".kept-{generation}"
    count = 1
    while _lexists(place):
        count += 1
        place = root / f".kept-{generation}-{count}"
    return place


def _refuse_kept(kept: list, notes: list = ()) -> None:
    """After a publish: say, in one line, where each entry the swap could not prove again is kept."""
    if kept:
        detail = " ".join([*notes, *(f"{live} was put in place while this command ran and was not deleted: it is "
                                     f"kept at {place}. Move it back or delete it yourself." for live, place in kept)])
        raise InstallRefused("plugin_directory_untrusted", detail=detail, show_detail=True)


def _same_tree(live: Path, written: Path) -> bool:
    """True when ``live`` holds the same folders and files, with the same bytes, as ``written``. No link.

    Only names, kinds and bytes are compared: mode bits are not part of the proof.
    """
    try:
        seen, kept = os.lstat(live), os.lstat(written)
    except FileNotFoundError:
        return False
    if stat.S_ISDIR(seen.st_mode) and stat.S_ISDIR(kept.st_mode):
        names = sorted(os.listdir(live))
        return names == sorted(os.listdir(written)) and all(_same_tree(live / name, written / name)
                                                            for name in names)
    if stat.S_ISREG(seen.st_mode) and stat.S_ISREG(kept.st_mode) and seen.st_size == kept.st_size:
        mine = _bytes_of(live, seen.st_size)
        return mine is not None and mine == _bytes_of(written, kept.st_size)
    return False


def _bytes_of(path: Path, size: int) -> bytes | None:
    """The bytes of a regular file; None when what is opened there is not one. The open never blocks."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return _read_all(fd, size)
    finally:
        os.close(fd)


def _clear_marker(root: Path) -> None:
    os.unlink(root / _PUBLISHING)
    _sync_dir(root)


def _read_current(config: Path) -> int | None:
    return _read_number(config, "current")


def _read_number(config: Path, name: str) -> int | None:
    match = _read_pointer(config, name, _CURRENT)
    return None if match is None else int(match.group(0))


def _read_pointer(config: Path, name: str, form: re.Pattern):
    root = config / "plugin-generations"
    if not _lexists(root):
        return None
    _require_owned_dir(root)
    pointer = root / name
    if not _lexists(pointer):
        return None
    if pointer.is_symlink():
        raise _link_refusal(pointer, "plugin_directory_untrusted")
    raw = _read_file(pointer, 64)
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise InstallRefused("generation_unreadable") from None
    match = form.fullmatch(text)
    if match is None:
        raise InstallRefused("generation_unreadable")
    return match


def _commit(config: Path, snapshot: Snapshot, notes: list | None = None, report=None,
            removing: str | None = None) -> int:
    # The live plugins/ path is checked before any generation is written or the pointer moves.
    _check_live(config)
    root = config / "plugin-generations"
    if not _lexists(root):
        _make_dir(root)
        # The new name in the config directory is on disk before the generations under it are trusted.
        _sync_dir(config)
    _require_owned_dir(root)
    current = _read_current(config)
    number = _next_number(root, current)
    dest = root / str(number)
    if _lexists(dest):
        raise InstallRefused("generation_exists")
    partial = root / f".partial-{number}"
    if _lexists(partial):
        _remove_at(partial)
    partial.mkdir(mode=0o700)
    os.chmod(partial, 0o700)
    lines = []
    try:
        _write_profile(partial, snapshot)
        # Every live entry the swap would move is proven the installer's before anything is published.
        sources = [partial] + ([root / str(current)] if current is not None else [])
        installed = root / str(current) if current is not None else None
        extras = _check_publish(config, sources, set(snapshot.plugins), installed, removing)
        # Extra entries of a proven installed plugin folder are kept aside, never deleted, and named at once.
        lines = _keep_extras(config, number, extras, report)
        if notes is not None:
            notes.extend(lines)
        # The record names this generation and the one it replaces, synced before any live folder changes.
        record = f"{number}\n" if current is None else f"{number}\n{current}\n"
        _atomic_replace(root, _PUBLISHING, record.encode("ascii"))
        # After every check: a refused command leaves the generations folder as it was.
        _write_parent(root, number, current)
        os.rename(partial, dest)
        _sync_dir(root)
        _write_pointer(root, number)
        # The pointer names a complete generation before the live tree is touched.
        # If publishing raises, the next command publishes this generation: .publishing names it.
        kept = _copy_generation_to_live(config, number, [dest] + sources[1:])
        _clear_marker(root)
    except Exception as exc:
        # A failure after the move names what was kept: the caller's report already did, or the refusal does.
        if report is None:
            _carry(exc, lines)
        if _lexists(partial) and not _lexists(dest):
            _remove_at(partial)
        if not _lexists(dest) and _lexists(root / f".parent-{number}"):
            _remove_at(root / f".parent-{number}")
        raise
    _refuse_kept(kept, lines if report is None else ())
    return number


def _next_number(root: Path, current: int | None) -> int:
    """The number after ``current`` and after every generation on disk.

    A generation that ``current`` does not name is left from a stop between its rename and the
    pointer write. It is no obstacle: it stays on disk and the next number is taken.
    """
    taken = [int(name) for name in os.listdir(root) if _GENERATION.fullmatch(name)]
    return max([current or 0, *taken]) + 1


def _write_pointer(root: Path, number: int) -> None:
    _atomic_replace(root, "current", f"{number}\n".encode("ascii"))


def _write_parent(root: Path, number: int, parent: int | None) -> None:
    """Record, beside the generations, the generation that was current when generation ``number`` was written.

    The record is ``.parent-<number>``. It is written and synced after every check of the command and before
    generation ``number`` is renamed into place; a record left by an earlier try at the same number is
    replaced, or removed for the first.
    """
    name = f".parent-{number}"
    if parent is None:
        if _lexists(root / name):
            _remove_at(root / name)
            _sync_dir(root)
        return
    _atomic_replace(root, name, f"{parent}\n".encode("ascii"))


def _read_parent(root: Path, number: int) -> int | None:
    path = root / f".parent-{number}"
    if not _lexists(path):
        return None
    raw = _read_file(path, 64)
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise InstallRefused("generation_unreadable") from None
    if _CURRENT.fullmatch(text) is None:
        raise InstallRefused("generation_unreadable")
    return int(text)


def _copy_generation_to_live(config: Path, generation: int, sources: list) -> list:
    """Replace the live ``plugins/`` tree with generation ``generation``.

    The stage directory is ``plugin-generations/.stage``, not a child of
    ``plugins/``. A dot directory under ``plugins/`` would be loaded. Each
    replaced entry is removed only once it is proven again against ``sources``;
    the rest is kept. Returns (live path, kept path) for each entry kept.
    """
    config = Path(config)
    source = config / "plugin-generations" / str(generation)
    if not _lexists(source):
        raise InstallRefused("generation_missing")
    _require_owned_dir(source, "generation_missing")
    _check_live(config)
    stage = config / "plugin-generations" / ".stage"
    old = config / "plugin-generations" / ".old"
    kept = []
    for leftover in (stage, config / "plugin-generations" / ".proven"):
        if _lexists(leftover):
            _remove_at(leftover)
    if _lexists(old):
        kept += _settle(config, old, sources, generation)
    stage.mkdir(mode=0o700)
    os.chmod(stage, 0o700)
    try:
        snapshot = _read_profile(source, missing_install_ok=False)
        visible = {plugin_id: plugin for plugin_id, plugin in snapshot.plugins.items()
                   if plugin_id not in snapshot.disabled}
        published = dataclasses.replace(snapshot, plugins=visible, disabled=frozenset())
        _write_profile(stage, published)
        plugins = config / "plugins"
        if not _lexists(plugins):
            plugins.mkdir(mode=0o700)
            os.chmod(plugins, 0o700)
            _sync_dir(config)
        old.mkdir(mode=0o700)
        os.chmod(old, 0o700)
        _swap_into(plugins, stage, old)
        kept += _settle(config, old, sources, generation)
    except Exception:
        # A failure part way: what the swap moved out is still proven before it is removed, and a
        # kept entry is named. A stop of the process leaves .old for the next command to settle.
        if _lexists(old):
            _refuse_kept(kept + _settle(config, old, sources, generation))
        raise
    finally:
        if _lexists(stage):
            _remove_at(stage)
    return kept


def _settle(config: Path, old: Path, sources: list, generation: int) -> list:
    """Remove each entry the swap moved into ``old`` once it is proven again; keep the rest.

    What is not a whole copy of its entry in ``sources`` was put in place after the check: ``old`` is
    renamed, with it, to ``plugin-generations/.kept-<generation>`` (or the first free ``-<k>`` after it),
    which no command removes. Returns (live path, kept path) for each entry kept.
    """
    names = sorted(os.listdir(old))
    kept = [name for name in names if not any(_same_tree(old / name, source / name) for source in sources)]
    # A proven entry is renamed whole into .proven before it is removed: a stop part way through a removal
    # leaves only proven bytes there, which the next command removes without a second proof.
    proven = old.parent / ".proven"
    if _lexists(proven):
        _remove_at(proven)
    if len(kept) < len(names):
        proven.mkdir(mode=0o700)
        os.chmod(proven, 0o700)
        for name in names:
            if name not in kept:
                _sync_tree(old / name)
                os.rename(old / name, proven / name)
        _sync_dir(proven)
        _remove_at(proven)
    if not kept:
        os.rmdir(old)
        return []
    place = _kept_place(old.parent, generation)
    _sync_tree(old)
    os.rename(old, place)
    _sync_dir(old.parent)
    return [(config / "plugins" / name, place / name) for name in kept]


def _sync_tree(path: Path) -> None:
    """Sync every folder and regular file at and under ``path``, so a rename of it moves what is on disk.

    Nothing else is opened: a link, a FIFO or a device is left as it is.
    """
    info = os.lstat(path)
    if stat.S_ISDIR(info.st_mode):
        for name in sorted(os.listdir(path)):
            _sync_tree(path / name)
        _sync_dir(path)
    elif stat.S_ISREG(info.st_mode):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _check_live(config: Path) -> None:
    """The checks of the live ``plugins/`` path. They run before anything is written."""
    plugins = config / "plugins"
    if _lexists(plugins):
        if plugins.is_symlink():
            raise _link_refusal(plugins, "plugin_directory_untrusted")
        _require_owned_dir(plugins)


def _swap_into(plugins: Path, stage: Path, old: Path) -> None:
    """Publish ``stage`` into ``plugins/``. Nothing is deleted from the live tree piece by piece.

    install.json goes in first, by one rename over the old file, and is never removed first. Then each
    old entry is moved out whole into ``old`` and each new plugin directory moved in whole.
    """
    staged = sorted(name for name in os.listdir(stage) if name != "install.json")
    live_install = plugins / "install.json"
    if _lexists(live_install) and stat.S_ISDIR(os.lstat(live_install).st_mode):
        os.rename(live_install, old / "install.json")
    os.rename(stage / "install.json", live_install)
    _sync_dir(plugins)
    for name in sorted(os.listdir(plugins)):
        if name == "install.json":
            continue
        path = plugins / name
        info = os.lstat(path)
        if name in staged or stat.S_ISLNK(info.st_mode) or stat.S_ISDIR(info.st_mode):
            os.rename(path, old / name)
    for name in staged:
        os.rename(stage / name, plugins / name)
    _sync_dir(plugins)


def _load_snapshot(config: Path, removing: str | None = None) -> Snapshot:
    """The live ``plugins/`` tree: its install.json and its plugin folders.

    Once a generation is current, each live plugin folder must be the copy written into it. ``removing`` is
    the plugin a remove drops: its live entry is not read, the current generation's copy stands for it, and
    the publish keeps the entry aside whole when it is not that copy.
    """
    current = _read_current(config)
    written = None
    generation = None
    if current is not None:
        path = config / "plugin-generations" / str(current)
        if not _lexists(path):
            raise InstallRefused("generation_missing")
        generation = _read_profile(path, missing_install_ok=False)
        written = generation.plugins
    live = config / "plugins"
    if not _lexists(live):
        return Snapshot(pins={})
    if live.is_symlink():
        raise _link_refusal(live, "plugin_directory_untrusted")
    snapshot = _read_profile(live, missing_install_ok=True, written=written, removing=removing)
    if generation is not None and generation.disabled:
        # A disabled plugin's folder is out of plugins/ by design: the current generation holds it and the list.
        plugins = dict(snapshot.plugins)
        for plugin_id in sorted(generation.disabled):
            plugins.setdefault(plugin_id, generation.plugins[plugin_id])
        snapshot.plugins = plugins
        snapshot.disabled = generation.disabled
    if snapshot.pins is None:
        snapshot.pins = _pins_from_grants(snapshot)
    return snapshot


def _way_out(path: Path, plugin_id: str | None = None) -> InstallRefused:
    """The refusal of a live plugin folder this module did not write, with the steps that end it."""
    if plugin_id is not None and plugin_id != path.name:
        detail = (f"The folder {path} holds the plugin {plugin_id}. Rename the folder to {plugin_id}, "
                  f"or move it out of the plugins directory; then run the command again.")
    else:
        detail = (f"The folder {path} is not a plugin this command installed. "
                  f"Move it out of the plugins directory; then run the command again.")
    return InstallRefused("plugin_directory_untrusted", detail=detail, show_detail=True)


def _link_refusal(path: Path, code: str) -> InstallRefused:
    """The refusal of a link met on a path the installer walks, with the steps that end it. ``code`` is unchanged."""
    refusal = InstallRefused(code, detail=(f"{path} is a link, and this command does not follow links. Move the "
                                           f"link out of {path.parent} and put the file or folder it stands for "
                                           f"in its place; then run the command again."), show_detail=True)
    refusal.link = True
    return refusal


def _named_way_out(path: Path) -> InstallRefused:
    """The refusal of a live folder whose name is not a plugin id: the id its manifest holds, if it holds one."""
    try:
        held = parse_manifest(_read_file(path / "manifest.json", _MAX_MANIFEST)).plugin_id
    except (InstallRefused, Refused, ValueError, UnicodeError, RecursionError):
        held = None
    return _way_out(path, held if _plugin_id_ok(held) else None)


def _read_profile(directory: Path, *, missing_install_ok: bool, written: dict | None = None,
                  removing: str | None = None) -> Snapshot:
    # missing_install_ok is True for the live plugins/ tree only. ``written`` holds the current
    # generation's plugins, against which each live folder is checked.
    _require_owned_dir(directory, "generation_unreadable" if not missing_install_ok else "plugin_directory_untrusted")
    install_path = directory / "install.json"
    if not _lexists(install_path):
        if not missing_install_ok:
            raise InstallRefused("generation_unreadable")
        parsed = _empty_install()
    else:
        if install_path.is_symlink():
            raise _link_refusal(install_path, "plugin_directory_untrusted")
        try:
            parsed = _parse_install(_read_file(install_path, _MAX_INSTALL))
        except InstallRefused:
            raise
        except (OSError, ValueError, UnicodeError):
            raise InstallRefused("generation_unreadable" if not missing_install_ok else "install_unreadable") from None
    plugins = {}
    for name in sorted(os.listdir(directory)):
        if name == "install.json":
            continue
        path = directory / name
        if written is not None and name == removing and name in written:
            # The plugin a remove drops: its live entry, of any kind, is not opened here.
            plugins[name] = written[name]
            continue
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            if missing_install_ok:
                raise _way_out(path)
            raise _link_refusal(path, "plugin_directory_untrusted")
        if not stat.S_ISDIR(info.st_mode):
            continue
        if not _plugin_id_ok(name):
            if missing_install_ok:
                raise _named_way_out(path)
            raise InstallRefused("plugin_directory_untrusted")
        try:
            plugin = _read_plugin(path, live=missing_install_ok)
        except InstallRefused as exc:
            # A folder no install wrote is moved out whole, also when a link inside it refused.
            if written is None or name in written or (exc.show_detail and not getattr(exc, "link", False)):
                raise
            raise _way_out(path) from None
        if written is not None and plugin != written.get(name):
            raise _changed_way_out(path, plugin, written.get(name))
        plugins[name] = plugin
    # The disabled-id list lives in a generation only; the live tree never holds it.
    disabled = frozenset() if missing_install_ok else _read_disabled(directory, plugins, missing_install_ok=False)
    return Snapshot(parsed["trusted_keys"], parsed["grants"], parsed["hosts"], parsed["prefixes"],
                    parsed["anchor"], parsed["labels"], plugins, parsed["developer_key_ids"], pins=parsed["pins"],
                    pins_held=parsed["pins"] is not None, disabled=disabled)


def _disabled_list_path(directory: Path) -> Path | None:
    """The disabled-id list, if this generation has one.

    ``.disabled`` is not a plugin id. An older generation may name the same
    list ``disabled``. A directory of that name is a plugin, not the list.
    """
    modern = directory / ".disabled"
    if _lexists(modern):
        return modern
    legacy = directory / "disabled"
    if not _lexists(legacy):
        return None
    if stat.S_ISDIR(os.lstat(legacy).st_mode):
        return None
    return legacy


def _read_disabled(directory: Path, plugins: dict, *, missing_install_ok: bool) -> frozenset:
    """Read the generation's disabled-id list. A missing file means none are disabled."""
    path = _disabled_list_path(directory)
    if path is None:
        return frozenset()
    raw = _read_file(path, _MAX_INSTALL)
    code = "install_unreadable" if missing_install_ok else "generation_unreadable"
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise InstallRefused(code) from None
    if not text or not text.endswith("\n") or "\n\n" in text:
        raise InstallRefused(code)
    lines = text[:-1].split("\n")
    if lines != sorted(lines) or len(set(lines)) != len(lines):
        raise InstallRefused(code)
    for line in lines:
        if not _plugin_id_ok(line) or line not in plugins:
            raise InstallRefused(code)
    return frozenset(lines)


def _changed_way_out(path: Path, plugin: StoredPlugin, mine: StoredPlugin | None) -> InstallRefused:
    """A live plugin folder that is not the copy written there. A manifest.sig added to an unsigned plugin's
    folder is named as what it is; anything else is a folder this command did not install."""
    if (mine is not None and mine.signature is None and plugin.signature is not None
            and (plugin.manifest, plugin.artifact) == (mine.manifest, mine.artifact)):
        return InstallRefused("plugin_directory_untrusted", detail=(
            f"The folder {path} holds a manifest.sig the plugin {path.name} was not installed with: it was "
            f"installed unsigned, and this command does not take a signature put in its folder. Move "
            f"{path / 'manifest.sig'} out of the folder; then run the command again."), show_detail=True)
    return _way_out(path)


def _empty_install() -> dict:
    return {"trusted_keys": {}, "grants": {}, "hosts": (), "prefixes": (), "anchor": None, "labels": {},
            "developer_key_ids": {}, "pins": None}


def _read_plugin(directory: Path, *, live: bool = False) -> StoredPlugin:
    _require_owned_dir(directory)
    manifest = _read_file(directory / "manifest.json", _MAX_MANIFEST)
    artifact = _read_file(directory / "plugin.py", _MAX_ARTIFACT)
    signature_path = directory / "manifest.sig"
    signature = None
    if _lexists(signature_path):
        if signature_path.is_symlink():
            raise _link_refusal(signature_path, "plugin_directory_untrusted")
        signature = _read_file(signature_path, _MAX_SIGNATURE)
    try:
        parsed = parse_manifest(manifest)
    except Refused as exc:
        raise InstallRefused("generation_unreadable", detail=exc.code) from None
    if parsed.plugin_id != directory.name:
        if live:
            raise _way_out(directory, parsed.plugin_id)
        raise InstallRefused("generation_unreadable")
    return StoredPlugin(manifest, artifact, signature)


def _parse_install(raw: bytes) -> dict:
    """Accept the install file the daemon accepts, and nothing past those fields."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise InstallRefused("install_unreadable") from None
    if not isinstance(data, dict) or set(data) - _INSTALL_KEYS:
        raise InstallRefused("install_unreadable")
    keys = data.get("trusted_keys", {})
    if not isinstance(keys, dict) or not all(isinstance(key, str) and isinstance(value, str)
                                             and _SHA256.fullmatch(value) is not None
                                             for key, value in keys.items()):
        raise InstallRefused("install_unreadable")
    listed = data.get("grants", [])
    if not isinstance(listed, list):
        raise InstallRefused("install_unreadable")
    grants = {}
    developer_key_ids = {}
    for item in listed:
        grant = _parse_grant(item)
        if grant.plugin_id in grants:
            raise InstallRefused("install_unreadable")
        grants[grant.plugin_id] = grant
        if "developer_key_id" in item:
            developer_key_ids[grant.plugin_id] = item["developer_key_id"]
    reserved = data.get("reserved", {})
    hosts = reserved.get("hosts", []) if isinstance(reserved, dict) else None
    prefixes = reserved.get("credential_prefixes", []) if isinstance(reserved, dict) else None
    if (not isinstance(reserved, dict) or set(reserved) - {"hosts", "credential_prefixes"}
            or not isinstance(hosts, list) or not isinstance(prefixes, list)
            or not all(isinstance(value, str) and value for value in [*hosts, *prefixes])):
        raise InstallRefused("install_unreadable")
    anchor = data.get("authority_anchor")
    if not (anchor is None or isinstance(anchor, str)):
        raise InstallRefused("install_unreadable")
    labels = data.get("registry_labels", {})
    if not isinstance(labels, dict):
        raise InstallRefused("install_unreadable")
    kept_labels = {}
    for digest, label in labels.items():
        if (not isinstance(digest, str) or _SHA256.fullmatch(digest) is None
                or not isinstance(label, str) or label not in _LABELS):
            raise InstallRefused("install_unreadable")
        kept_labels[digest] = label
    pins = _parse_pins(data["pins"]) if "pins" in data else None
    return {"trusted_keys": dict(keys), "grants": grants, "hosts": tuple(hosts), "prefixes": tuple(prefixes),
            "anchor": anchor, "labels": kept_labels, "developer_key_ids": developer_key_ids, "pins": pins}


def _parse_pins(value: object) -> dict:
    """``pins``: by plugin id, the signing key id and the version, and nothing else."""
    if not isinstance(value, dict):
        raise InstallRefused("install_unreadable")
    pins = {}
    for plugin_id, pin in value.items():
        if not _plugin_id_ok(plugin_id) or not isinstance(pin, dict) or set(pin) != _PIN_KEYS:
            raise InstallRefused("install_unreadable")
        signer, version = pin["signer_key_id"], pin["version"]
        if (not (isinstance(signer, str) and _KEY_ID.fullmatch(signer) is not None)
                or not (isinstance(version, str) and 0 < len(version) <= 64)):
            raise InstallRefused("install_unreadable")
        pins[plugin_id] = {"signer_key_id": signer, "version": version}
    return pins


def _parse_grant(item: object) -> Grant:
    if not isinstance(item, dict) or set(item) - _GRANT_KEYS or not {"plugin_id", "artifact_sha256",
                                                                     "permissions"} <= set(item):
        raise InstallRefused("install_unreadable")
    perms, signer = item["permissions"], item.get("signer_key_id")
    generation = item.get("order_after_revocation_generation")
    developer = item.get("developer_key_id")
    if (not isinstance(item["plugin_id"], str) or not isinstance(item["artifact_sha256"], str)
            or not isinstance(perms, list) or any(perm not in PERMISSIONS for perm in perms)
            or not (signer is None or (isinstance(signer, str) and _KEY_ID.fullmatch(signer) is not None))
            or not (developer is None or (isinstance(developer, str) and _KEY_ID.fullmatch(developer) is not None))
            or type(item.get("unsigned_order_acknowledged", False)) is not bool
            or not (item.get("order_after_revoked_key") is None or isinstance(item.get("order_after_revoked_key"), str))
            or not (generation is None or type(generation) is int)
            or not _plugin_id_ok(item["plugin_id"])):
        raise InstallRefused("install_unreadable")
    return Grant(item["plugin_id"], item["artifact_sha256"], signer, tuple(perms),
                 item.get("unsigned_order_acknowledged", False), item.get("order_after_revoked_key"), generation)


def _write_profile(directory: Path, snapshot: Snapshot) -> None:
    _write_file(directory / "install.json", _dump_install(snapshot))
    for plugin_id in sorted(snapshot.plugins):
        if not _plugin_id_ok(plugin_id):
            raise InstallRefused("plugin_directory_untrusted")
        plugin_dir = directory / plugin_id
        plugin_dir.mkdir(mode=0o700)
        os.chmod(plugin_dir, 0o700)
        plugin = snapshot.plugins[plugin_id]
        _write_file(plugin_dir / "manifest.json", plugin.manifest)
        _write_file(plugin_dir / "plugin.py", plugin.artifact)
        if plugin.signature is not None:
            _write_file(plugin_dir / "manifest.sig", plugin.signature)
        _sync_dir(plugin_dir)
    if snapshot.disabled:
        _write_file(directory / ".disabled", _disabled_bytes(snapshot.disabled))
    _sync_dir(directory)


def _disabled_bytes(disabled: frozenset) -> bytes:
    return "".join(f"{plugin_id}\n" for plugin_id in sorted(disabled)).encode("ascii")


def _dump_install(snapshot: Snapshot) -> bytes:
    payload = {
        "authority_anchor": snapshot.anchor,
        "grants": [_grant_json(snapshot.grants[plugin_id], snapshot.developer_key_ids)
                   for plugin_id in sorted(snapshot.grants)],
        "registry_labels": snapshot.labels,
        "reserved": {"credential_prefixes": list(snapshot.prefixes), "hosts": list(snapshot.hosts)},
        "trusted_keys": snapshot.trusted_keys,
    }
    if snapshot.pins or snapshot.pins_held:
        # Written when a plugin is pinned or the file held the key: a file that never held a pin is the bytes it
        # was before pins existed, and one that did keeps the key, so no pin is derived again after the last unpin.
        payload["pins"] = {plugin_id: dict(snapshot.pins[plugin_id]) for plugin_id in sorted(snapshot.pins)}
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _grant_json(grant: Grant, developer_key_ids: dict) -> dict:
    data = {
        "plugin_id": grant.plugin_id,
        "artifact_sha256": grant.artifact_sha256,
        "signer_key_id": grant.signer_key_id,
        "permissions": list(grant.permissions),
        "unsigned_order_acknowledged": bool(grant.unsigned_order_acknowledged),
        "order_after_revoked_key": grant.order_after_revoked_key,
        "order_after_revocation_generation": grant.order_after_revocation_generation,
    }
    if grant.plugin_id in developer_key_ids:
        data["developer_key_id"] = developer_key_ids[grant.plugin_id]
    return data


def _write_file(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fchmod(fd, 0o600)
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_dir(path: Path) -> None:
    """Sync a directory, so the names written or renamed in it are on disk."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_replace(directory: Path, name: str, data: bytes) -> None:
    temporary = directory / f".{name}.tmp"
    if _lexists(temporary):
        _remove_at(temporary)
    _write_file(temporary, data)
    os.rename(temporary, directory / name)
    _sync_dir(directory)


def _read_file(path: Path, limit: int) -> bytes:
    try:
        # O_NONBLOCK: a FIFO opens at once and is refused below, so no command waits on one holding the lock.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _link_refusal(path, "plugin_file_untrusted") from None
        raise InstallRefused("plugin_file_untrusted", detail=type(exc).__name__) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise InstallRefused("plugin_file_untrusted")
        if info.st_size > limit:
            raise InstallRefused("plugin_file_too_large")
        data = _read_all(fd, limit)
    finally:
        os.close(fd)
    return data


def _read_all(fd: int, limit: int) -> bytes:
    chunks = []
    size = 0
    while size <= limit:
        chunk = os.read(fd, min(1 << 20, limit + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    if size > limit:
        raise InstallRefused("plugin_file_too_large")
    return b"".join(chunks)


def _require_owned_dir(path: Path, code: str = "plugin_directory_untrusted") -> None:
    try:
        info = os.lstat(path)
    except OSError:
        raise InstallRefused(code) from None
    if stat.S_ISLNK(info.st_mode):
        raise _link_refusal(path, code)
    if not stat.S_ISDIR(info.st_mode):
        raise InstallRefused(code)
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise InstallRefused(code)


def _owned_dir(path: Path) -> bool:
    try:
        _require_owned_dir(path)
    except InstallRefused:
        return False
    return True


def _remove_at(path: Path) -> None:
    """Remove a file, a symlink or a directory. A symlink is unlinked, not followed."""
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        os.unlink(path)
        return
    for name in os.listdir(path):
        _remove_at(path / name)
    os.rmdir(path)
