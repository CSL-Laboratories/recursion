"""Check a plugin registry folder in the marketplace layout.

The check is local: it reads the directory the operator named, follows nothing
under it, and does not fetch. A pass means the entry, the index, the manifest
bytes and the vendored artifact agree, and a declared signature matches the
manifest's own developer key when this interpreter can verify one. It does not
mean the key is on this install's trusted list. ``verified`` stays a label with
no meaning defined, so a release that claims it is refused.

Rule codes match the marketplace checker. This module is not that program and
does not copy a registry into the tree.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field

from .errors import Refused
from .manifest import MANIFEST_SCHEMA, parse_manifest
from .signing import backend_available, key_id, verify

__all__ = ["CheckedRelease", "Finding", "Report", "build_index", "check_registry", "index_bytes"]

ENTRY_SCHEMA = "csl-marketplace-entry/v0"
INDEX_SCHEMA = "csl-marketplace-index/v0"
ENTRY_FILE, INDEX_FILE, INDEX_SIGNATURE_FILE, README_FILE = "entry.json", "index.json", "index.sig", "README.md"
MANIFEST_FILE, SIGNATURE_FILE, ARTIFACT_FILE = "manifest.json", "manifest.sig", "plugin.py"
ENTRY_REQUIRED = frozenset({
    "schema", "plugin_id", "name", "summary", "publisher", "releases", "ratings", "install_count",
})
ENTRY_OPTIONAL = frozenset({"example"})
RELEASE_FIELDS = frozenset({
    "version", "status", "status_reason", "label", "label_basis", "trust_tier",
    "manifest_sha256", "artifact_sha256", "signature", "package", "source", "compatibility",
})
SOURCE_FIELDS = frozenset({"repository", "commit", "path"})
COMPATIBILITY_FIELDS = frozenset({
    "manifest_schema", "interface", "runtime", "recursion_commit", "recursion_versions",
})
SIGNATURE_FIELDS = frozenset({"format", "key_id"})
PUBLISHER_FIELDS = frozenset({"name", "url"})
LABELS = ("verified", "non_verified")
STATUSES = ("active", "deprecated", "withdrawn")
TRUST_TIERS = ("trusted_in_process",)
RESERVED_TRUST_TIERS = ("restricted_process", "restricted_wasm")
KNOWN_MANIFEST_SCHEMAS = (MANIFEST_SCHEMA,)
KNOWN_INTERFACES = ("csl-core/v0",)
KNOWN_RUNTIMES = ("python-inprocess",)
TIER_RUNTIMES = {"trusted_in_process": ("python-inprocess",)}
SIGNATURE_FORMATS = ("ed25519-hex",)
MAX_ENTRY_BYTES = 256 * 1024
MAX_INDEX_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_SIGNATURE_BYTES = 4096
NAME_MAX, SUMMARY_MAX, PUBLISHER_NAME_MAX, REASON_MAX, URL_MAX = 80, 300, 80, 300, 200

_ID = re.compile(r"[a-z][a-z0-9_.-]{1,63}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_SIG = re.compile(r"[0-9a-f]{128}")
_VERSION_DIR = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_KEY_ID = re.compile(r"ed25519:[0-9a-f]{32}")
_HTTPS_URL = re.compile(r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~%!$&'()*+,;=:@-]*)*")
_REPOSITORY = re.compile(r"https://[A-Za-z0-9.-]+(/[A-Za-z0-9._-]+)+")
_PATH_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")
_TEXT_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\ufeff]")
_TEXT_SPACE = re.compile(r"[\x20\xa0\u1680\u2000-\u200a\u202f\u205f\u3000]")
_WINDOWS_DEVICES = frozenset(
    {"con", "prn", "aux", "nul"} | {f"com{n}" for n in range(10)} | {f"lpt{n}" for n in range(10)})
_RESERVED_PLUGIN_IDS = frozenset({"host"})
_RULES = frozenset({
    "root_unavailable", "registry_missing", "registry_symlink", "registry_unexpected_file",
    "entry_missing", "entry_too_large", "entry_not_json", "entry_fields", "entry_schema",
    "entry_plugin_id", "entry_directory_name", "entry_text", "entry_publisher",
    "entry_reserved_field", "entry_example_flag", "entry_releases", "entry_unexpected_file",
    "entry_release_unlisted", "release_fields", "release_version", "release_directory_missing",
    "release_unexpected_file", "release_status", "release_label", "release_label_verified_undefined",
    "release_label_basis_reserved", "release_trust_tier", "release_trust_tier_reserved",
    "release_package_reserved", "release_source", "release_compatibility", "release_digest_format",
    "release_signature", "manifest_missing", "manifest_too_large", "release_manifest_digest_mismatch",
    "release_artifact_digest_mismatch", "release_plugin_id_mismatch", "release_version_mismatch",
    "release_manifest_schema_mismatch", "signature_file_unexpected", "signature_file_missing",
    "signature_too_large", "signature_malformed", "signature_key_id_mismatch", "signature_invalid",
    "artifact_too_large", "artifact_digest_mismatch", "index_missing", "index_not_json",
    "index_fields", "index_stale", "index_signature_reserved", "noncanonical_value", "manifest_fields",
    "manifest_schema", "manifest_plugin_id", "manifest_plugin_id_reserved", "manifest_version",
    "manifest_artifact_digest", "manifest_dependency_lock", "malformed_public_key",
    "manifest_unknown_permission", "manifest_permission_order", "manifest_network_destinations",
    "manifest_credential_refs", "manifest_network_mismatch", "manifest_credential_mismatch",
    "manifest_install_scripts",
})
_NOTES = frozenset({"signature_unchecked", "artifact_unchecked"})


@dataclass(frozen=True)
class Finding:
    """One checker finding. ``code`` is the rule; ``detail`` is safe to print."""

    level: str
    path: str
    code: str
    detail: str = ""


@dataclass(frozen=True)
class CheckedRelease:
    """One release whose manifest parsed. ``artifact`` is None when no ``plugin.py`` is vendored."""

    plugin_id: str
    version: str
    status: str
    label: str
    manifest: bytes
    artifact: bytes | None
    signature: bytes | None
    signature_text: str | None
    declared_signature: bool
    status_reason: str | None = None
    in_examples: bool = False
    source_commit: str | None = None
    source_path: str | None = None
    source_repository: str | None = None
    manifest_sha256: str | None = None
    artifact_sha256: str | None = None


@dataclass
class Report:
    """Errors and notes for one registry root. ``codes`` is the sorted set of error codes."""

    root: str
    findings: list[Finding] = field(default_factory=list)
    checked: list[CheckedRelease] = field(default_factory=list)

    def error(self, path: str, code: str, detail: str = "") -> None:
        if code not in _RULES:
            raise KeyError(code)
        self.findings.append(Finding("error", _rel(self.root, path), code, detail))

    def note(self, path: str, code: str, detail: str = "") -> None:
        if code not in _NOTES:
            raise KeyError(code)
        self.findings.append(Finding("note", _rel(self.root, path), code, detail))

    @property
    def errors(self) -> list[Finding]:
        return [item for item in self.findings if item.level == "error"]

    @property
    def notes(self) -> list[Finding]:
        return [item for item in self.findings if item.level == "note"]

    def codes(self, level: str = "error") -> list[str]:
        return sorted({item.code for item in self.findings if item.level == level})


def _rel(root: str, path: str) -> str:
    if os.path.isabs(path):
        path = os.path.relpath(path, root)
    return path.replace(os.sep, "/")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_json(raw: bytes) -> object:
    """UTF-8 JSON with no duplicate keys and no floats. Entry files are not canonical."""

    def no_duplicates(pairs: list) -> dict:
        out: dict = {}
        for key, item in pairs:
            if key in out:
                raise ValueError(f"duplicate key {key!r}")
            out[key] = item
        return out

    def no_number(text: str) -> object:
        raise ValueError(f"number {text!r} is not allowed")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates, parse_float=no_number,
                      parse_constant=no_number)


def _is_text(value: object, maximum: int) -> bool:
    if type(value) is not str or not 0 < len(value) <= maximum or _TEXT_FORBIDDEN.search(value):
        return False
    return _TEXT_SPACE.fullmatch(value[0]) is None and _TEXT_SPACE.fullmatch(value[-1]) is None


def _unportable(name: str) -> str | None:
    if name.endswith((".", " ")):
        return "it ends with a dot or a space"
    if name.split(".", 1)[0].lower() in _WINDOWS_DEVICES:
        return "its base name is a Windows device name"
    return None


def _fields_detail(missing: set, unknown: set) -> str:
    parts = []
    if missing:
        parts.append("missing " + ", ".join(sorted(missing)))
    if unknown:
        parts.append("unknown " + ", ".join(sorted(map(str, unknown))))
    return "; ".join(parts)


def build_index(entries: list) -> dict:
    """One index record per release, sorted by plugin id, release order kept within an id."""
    records = []
    for entry in entries:
        if type(entry) is not dict or type(entry.get("releases")) is not list:
            continue
        for release in entry["releases"]:
            if type(release) is not dict:
                continue
            signature = release.get("signature")
            compat = release.get("compatibility") if type(release.get("compatibility")) is dict else {}
            records.append({
                "artifact_sha256": release.get("artifact_sha256"),
                "interface": compat.get("interface"),
                "key_id": signature.get("key_id") if type(signature) is dict else None,
                "label": release.get("label"),
                "manifest_sha256": release.get("manifest_sha256"),
                "plugin_id": entry.get("plugin_id"),
                "runtime": compat.get("runtime"),
                "status": release.get("status"),
                "trust_tier": release.get("trust_tier"),
                "version": release.get("version"),
            })
    records.sort(key=lambda record: str(record["plugin_id"]))
    return {"releases": records, "schema": INDEX_SCHEMA}


def index_bytes(index: dict) -> bytes:
    """The index file bytes. A stored index that differs is ``index_stale``."""
    return (json.dumps(index, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


def _check_source(source: object) -> str | None:
    if type(source) is not dict or set(source) != SOURCE_FIELDS:
        return "fields must be exactly repository, commit, path"
    repo, commit, inner = source["repository"], source["commit"], source["path"]
    if type(repo) is not str or len(repo) > URL_MAX or _REPOSITORY.fullmatch(repo) is None or repo.endswith("/"):
        return "repository must be an https URL to a repository"
    if type(commit) is not str or _COMMIT.fullmatch(commit) is None:
        return "commit must be 40 lowercase hex"
    if not _is_relative_path(inner):
        return "path must be a relative path"
    return None


def _source_part(release: dict, name: str) -> str | None:
    """The source field when the release source is well formed, else None."""
    source = release.get("source")
    if _check_source(source) is not None:
        return None
    value = source[name]
    return value if type(value) is str else None


def _hex64_or_none(value: object) -> str | None:
    if type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None:
        return value
    return None


def _is_relative_path(value: object) -> bool:
    if type(value) is not str or not value or len(value) > 512 or value.startswith("/"):
        return False
    return all(_PATH_SEGMENT.fullmatch(part) is not None and part not in (".", "..") for part in value.split("/"))


def _check_compatibility(compat: object) -> str | None:
    if type(compat) is not dict or set(compat) != COMPATIBILITY_FIELDS:
        return "fields must be exactly " + ", ".join(sorted(COMPATIBILITY_FIELDS))
    if compat["manifest_schema"] not in KNOWN_MANIFEST_SCHEMAS:
        return "manifest_schema is not defined"
    if compat["interface"] not in KNOWN_INTERFACES:
        return "interface is not defined"
    if compat["runtime"] not in KNOWN_RUNTIMES:
        return "runtime is not defined"
    if type(compat["recursion_commit"]) is not str or _COMMIT.fullmatch(compat["recursion_commit"]) is None:
        return "recursion_commit must be 40 lowercase hex"
    if compat["recursion_versions"] is not None:
        return "recursion_versions is reserved"
    return None


def _check_publisher(report: Report, path: str, publisher: object) -> None:
    url = publisher.get("url") if type(publisher) is dict else None
    if (type(publisher) is not dict or set(publisher) != PUBLISHER_FIELDS
            or not _is_text(publisher.get("name"), PUBLISHER_NAME_MAX)
            or not (url is None or (type(url) is str and len(url) <= URL_MAX and _HTTPS_URL.fullmatch(url) is not None))):
        report.error(path, "entry_publisher")


def _check_release_fields(report: Report, path: str, release: dict, seen_versions: set) -> bool:
    """Return whether ``version`` can name a directory. A duplicate is not checked again."""
    missing, unknown = RELEASE_FIELDS - set(release), set(release) - RELEASE_FIELDS
    if missing or unknown:
        report.error(path, "release_fields", _fields_detail(missing, unknown))
    can_name = False
    if "version" in release:
        version = release["version"]
        if type(version) is not str or _VERSION_DIR.fullmatch(version) is None:
            report.error(path, "release_version", "version is not a portable directory name")
        elif _unportable(version):
            report.error(path, "release_version", _unportable(version) or "")
        elif version.lower() in seen_versions:
            report.error(path, "release_version", "version appears twice")
        else:
            seen_versions.add(version.lower())
            can_name = True
    if "status" in release:
        status, reason = release["status"], release.get("status_reason")
        if status not in STATUSES:
            report.error(path, "release_status")
        elif "status_reason" in release:
            if status == "active" and reason is not None:
                report.error(path, "release_status", "status_reason must be null for an active release")
            elif status == "withdrawn" and not _is_text(reason, REASON_MAX):
                report.error(path, "release_status", "a withdrawn release needs a status_reason")
            elif status == "deprecated" and not (reason is None or _is_text(reason, REASON_MAX)):
                report.error(path, "release_status")
    if "label" in release:
        label = release["label"]
        if label not in LABELS:
            report.error(path, "release_label")
        elif label == "verified":
            report.error(path, "release_label_verified_undefined")
    if "label_basis" in release and release["label_basis"] is not None:
        report.error(path, "release_label_basis_reserved")
    if "trust_tier" in release:
        tier = release["trust_tier"]
        if tier in RESERVED_TRUST_TIERS:
            report.error(path, "release_trust_tier_reserved")
        elif tier not in TRUST_TIERS:
            report.error(path, "release_trust_tier")
    if "package" in release and release["package"] is not None:
        report.error(path, "release_package_reserved")
    if "source" in release:
        problem = _check_source(release["source"])
        if problem:
            report.error(path, "release_source", problem)
    if "compatibility" in release:
        problem = _check_compatibility(release["compatibility"])
        if problem:
            report.error(path, "release_compatibility", problem)
        elif (release.get("trust_tier") in TRUST_TIERS
              and release["compatibility"]["runtime"] not in TIER_RUNTIMES.get(release["trust_tier"], ())):
            report.error(path, "release_compatibility", "runtime is not one this trust tier covers")
    for name in ("manifest_sha256", "artifact_sha256"):
        if name in release and (type(release[name]) is not str or _HEX64.fullmatch(release[name]) is None):
            report.error(path, "release_digest_format", name)
    if "signature" in release:
        sig = release["signature"]
        if sig is not None and (type(sig) is not dict or set(sig) != SIGNATURE_FIELDS
                                or sig.get("format") not in SIGNATURE_FORMATS
                                or type(sig.get("key_id")) is not str or _KEY_ID.fullmatch(sig["key_id"]) is None):
            report.error(path, "release_signature")
    return can_name


class _Symlink(Exception):
    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path


class _NotRegular(Exception):
    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path


class _TooLarge(Exception):
    pass


class _RootUnavailable(Exception):
    pass


def _kind_of(info: os.stat_result) -> str:
    if stat.S_ISLNK(info.st_mode):
        return "link"
    if stat.S_ISDIR(info.st_mode):
        return "dir"
    if stat.S_ISREG(info.st_mode):
        return "file"
    return "other"


def _read_bounded(fd: int, limit: int) -> bytes:
    chunks = []
    size = 0
    while size <= limit:
        chunk = os.read(fd, min(1 << 20, limit + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    if size > limit:
        raise _TooLarge()
    return b"".join(chunks)


class _Tree:
    """The operator's root, then every child opened with ``O_NOFOLLOW``."""

    def __init__(self, root: str) -> None:
        self.fd: int | None = None
        self.fd, self.path = self._open_root(os.path.abspath(root))

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _open_root(self, path: str) -> tuple[int, str]:
        # The operator chose this path, so the open of the root itself may follow it.
        try:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except FileNotFoundError:
            raise _RootUnavailable(f"{path} does not exist") from None
        except NotADirectoryError:
            raise _RootUnavailable(f"{path} is not a directory") from None
        except OSError as exc:
            raise _RootUnavailable(f"{path}: {exc.strerror or type(exc).__name__}") from None
        try:
            held = os.fstat(fd)
            shown = os.path.realpath(path)
            named = os.stat(shown)
            if (named.st_dev, named.st_ino) != (held.st_dev, held.st_ino):
                raise _RootUnavailable(f"{path} changed while it was being opened")
        except BaseException:
            os.close(fd)
            raise
        return fd, shown

    def _parts(self, path: str) -> list[str]:
        rel = os.path.relpath(path, self.path)
        if rel == os.curdir:
            return []
        parts = rel.split(os.sep)
        if any(part in ("", ".", "..") for part in parts):
            raise ValueError(path)
        return parts

    def _kind_at(self, parent: int, name: str) -> str | None:
        try:
            return _kind_of(os.stat(name, dir_fd=parent, follow_symlinks=False))
        except FileNotFoundError:
            return None

    def _open_dir(self, parent: int, name: str) -> int:
        kind = self._kind_at(parent, name)
        if kind is None:
            raise FileNotFoundError(name)
        if kind == "link":
            raise _Symlink(name)
        if kind != "dir":
            raise _NotRegular(name)
        try:
            return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                raise _Symlink(name) from None
            if exc.errno == errno.ENOTDIR:
                raise _NotRegular(name) from None
            raise

    def _walk(self, parts: list[str]) -> int:
        current = self.fd
        opened: list[int] = []
        if current is None:
            raise _RootUnavailable("closed")
        try:
            for part in parts:
                opened.append(self._open_dir(current, part))
                current = opened[-1]
        except BaseException:
            for fd in opened:
                os.close(fd)
            raise
        for fd in opened[:-1]:
            os.close(fd)
        return current

    def _release(self, handle: int) -> None:
        if handle != self.fd:
            os.close(handle)

    def kind(self, path: str) -> str | None:
        parts = self._parts(path)
        if not parts:
            return "dir"
        try:
            parent = self._walk(parts[:-1])
        except FileNotFoundError:
            return None
        try:
            return self._kind_at(parent, parts[-1])
        finally:
            self._release(parent)

    def children(self, path: str) -> list[tuple[str, str, str]]:
        handle = self._walk(self._parts(path))
        try:
            names = sorted(os.listdir(handle))
            found = []
            for name in names:
                kind = self._kind_at(handle, name)
                if kind is not None:
                    found.append((name, os.path.join(path, name), kind))
            return found
        finally:
            self._release(handle)

    def read(self, path: str, limit: int) -> bytes | None:
        parts = self._parts(path)
        try:
            parent = self._walk(parts[:-1])
        except FileNotFoundError:
            return None
        try:
            kind = self._kind_at(parent, parts[-1])
            if kind is None:
                return None
            if kind == "link":
                raise _Symlink(path)
            if kind != "file":
                raise _NotRegular(path)
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0),
                         dir_fd=parent)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise _NotRegular(path)
                if info.st_size > limit:
                    raise _TooLarge()
                return _read_bounded(fd, limit)
            finally:
                os.close(fd)
        finally:
            self._release(parent)


def _declared_signature(release: dict) -> tuple[object, bool]:
    declared = release.get("signature")
    ok = (type(declared) is dict and set(declared) == SIGNATURE_FIELDS
          and declared.get("format") in SIGNATURE_FORMATS
          and type(declared.get("key_id")) is str and _KEY_ID.fullmatch(declared["key_id"]) is not None)
    return declared, ok


def _check_release_directory(report: Report, tree: _Tree, release_dir: str, entry_plugin_id: object,
                             release: dict, in_examples: bool = False) -> None:
    files: dict[str, str] = {}
    for name, path, kind in tree.children(release_dir):
        if kind == "link":
            report.error(path, "registry_symlink")
        elif kind != "file" or name not in (MANIFEST_FILE, SIGNATURE_FILE, ARTIFACT_FILE):
            report.error(path, "release_unexpected_file")
        else:
            files[name] = path
    manifest_path = os.path.join(release_dir, MANIFEST_FILE)
    if MANIFEST_FILE not in files:
        if tree.kind(manifest_path) is None:
            report.error(manifest_path, "manifest_missing")
        return
    try:
        raw = tree.read(manifest_path, MAX_MANIFEST_BYTES)
    except _TooLarge:
        report.error(manifest_path, "manifest_too_large")
        return
    if raw is None:
        report.error(manifest_path, "manifest_missing")
        return
    parsed = None
    try:
        parsed = parse_manifest(raw)
    except Refused as exc:
        report.error(manifest_path, exc.code, exc.detail)
    if parsed is not None:
        digest = _sha256(raw)
        manifest_sha = release.get("manifest_sha256")
        if type(manifest_sha) is str and _HEX64.fullmatch(manifest_sha) is not None and manifest_sha != digest:
            report.error(manifest_path, "release_manifest_digest_mismatch")
        artifact_sha = release.get("artifact_sha256")
        if (type(artifact_sha) is str and _HEX64.fullmatch(artifact_sha) is not None
                and artifact_sha != parsed.artifact_sha256):
            report.error(manifest_path, "release_artifact_digest_mismatch")
        if type(entry_plugin_id) is str and parsed.plugin_id != entry_plugin_id:
            report.error(manifest_path, "release_plugin_id_mismatch")
        if type(release.get("version")) is str and parsed.version != release["version"]:
            report.error(manifest_path, "release_version_mismatch")
        compat = release.get("compatibility")
        if _check_compatibility(compat) is None and compat["manifest_schema"] != MANIFEST_SCHEMA:
            report.error(manifest_path, "release_manifest_schema_mismatch")
    declared, declared_ok = _declared_signature(release)
    sig_path = os.path.join(release_dir, SIGNATURE_FILE)
    signature_bytes = None
    signature_text = None
    signature_ok = False
    if SIGNATURE_FILE in files:
        if "signature" in release and declared is None:
            report.error(sig_path, "signature_file_unexpected")
        try:
            signature_bytes = tree.read(sig_path, MAX_SIGNATURE_BYTES)
        except _TooLarge:
            report.error(sig_path, "signature_too_large")
            signature_bytes = None
        if signature_bytes is not None:
            signature_text = signature_bytes.decode("ascii", "replace").strip()
            if _SIG.fullmatch(signature_text) is None:
                report.error(sig_path, "signature_malformed")
            else:
                signature_ok = True
    elif declared_ok:
        report.error(sig_path, "signature_file_missing")
    if parsed is not None and declared_ok:
        if key_id(parsed.developer_key) != declared["key_id"]:
            report.error(manifest_path, "signature_key_id_mismatch")
        elif signature_ok:
            _verify_developer_signature(report, sig_path, parsed.developer_key, signature_text, raw)
    expected = parsed.artifact_sha256 if parsed is not None else release.get("artifact_sha256")
    artifact_bytes = None
    if ARTIFACT_FILE in files:
        artifact_path = os.path.join(release_dir, ARTIFACT_FILE)
        try:
            artifact_bytes = tree.read(artifact_path, MAX_ARTIFACT_BYTES)
        except _TooLarge:
            report.error(artifact_path, "artifact_too_large")
            artifact_bytes = None
        if artifact_bytes is not None and type(expected) is str and _sha256(artifact_bytes) != expected:
            report.error(artifact_path, "artifact_digest_mismatch")
    else:
        report.note(manifest_path, "artifact_unchecked")
    if parsed is not None and type(entry_plugin_id) is str and type(release.get("version")) is str:
        report.checked.append(CheckedRelease(
            entry_plugin_id, release["version"],
            release["status"] if type(release.get("status")) is str else "",
            release["label"] if type(release.get("label")) is str else "",
            raw, artifact_bytes, signature_bytes, signature_text if signature_ok else None,
            declared is not None,
            release["status_reason"] if type(release.get("status_reason")) is str else None,
            in_examples, _source_part(release, "commit"), _source_part(release, "path"),
            _source_part(release, "repository"), _hex64_or_none(release.get("manifest_sha256")),
            _hex64_or_none(release.get("artifact_sha256"))))


def _verify_developer_signature(report: Report, path: str, developer_key: str, signature: str | None,
                                raw: bytes) -> None:
    """Refuse a signature that is not this manifest's own key. A missing verifier is a note."""
    if not backend_available():
        report.note(path, "signature_unchecked", "no Ed25519 verifier")
        return
    try:
        valid = verify(developer_key, signature, raw)
    except Refused as exc:
        if exc.code == "signature_backend_unavailable":
            report.note(path, "signature_unchecked", exc.detail)
            return
        report.error(path, exc.code, exc.detail)
        return
    if not valid:
        report.error(path, "signature_invalid")


def _check_entry(report: Report, tree: _Tree, entry_dir: str, in_examples: bool) -> dict | None:
    entry_path = os.path.join(entry_dir, ENTRY_FILE)
    subdirs: dict[str, str] = {}
    for name, path, kind in tree.children(entry_dir):
        if kind == "link":
            report.error(path, "registry_symlink")
        elif name == ENTRY_FILE and kind == "file":
            continue
        elif kind == "dir":
            subdirs[name] = path
        else:
            report.error(path, "entry_unexpected_file")
    try:
        raw = tree.read(entry_path, MAX_ENTRY_BYTES)
    except _TooLarge:
        report.error(entry_path, "entry_too_large")
        return None
    except (_Symlink, _NotRegular):
        return None
    if raw is None:
        report.error(entry_path, "entry_missing")
        return None
    try:
        entry = _load_json(raw)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        report.error(entry_path, "entry_not_json", str(exc).splitlines()[0][:200])
        return None
    if type(entry) is not dict:
        report.error(entry_path, "entry_not_json", "not an object")
        return None
    missing, unknown = ENTRY_REQUIRED - set(entry), set(entry) - ENTRY_REQUIRED - ENTRY_OPTIONAL
    if missing or unknown:
        report.error(entry_path, "entry_fields", _fields_detail(missing, unknown))
    if "schema" in entry and entry["schema"] != ENTRY_SCHEMA:
        report.error(entry_path, "entry_schema")
    plugin_id = entry.get("plugin_id")
    if "plugin_id" in entry:
        if type(plugin_id) is not str or _ID.fullmatch(plugin_id) is None or plugin_id in _RESERVED_PLUGIN_IDS:
            report.error(entry_path, "entry_plugin_id")
        elif _unportable(plugin_id):
            report.error(entry_path, "entry_plugin_id", _unportable(plugin_id) or "")
        elif os.path.basename(entry_dir) != plugin_id:
            report.error(entry_dir, "entry_directory_name")
    for name, maximum in (("name", NAME_MAX), ("summary", SUMMARY_MAX)):
        if name in entry and not _is_text(entry[name], maximum):
            report.error(entry_path, "entry_text", name)
    if "publisher" in entry:
        _check_publisher(report, entry_path, entry["publisher"])
    for name in ("ratings", "install_count"):
        if name in entry and entry[name] is not None:
            report.error(entry_path, "entry_reserved_field", name)
    example = entry.get("example", False)
    if in_examples and example is not True:
        report.error(entry_path, "entry_example_flag")
    elif not in_examples and example is not False:
        report.error(entry_path, "entry_example_flag")
    releases = entry.get("releases")
    if "releases" in entry and (type(releases) is not list or not releases):
        report.error(entry_path, "entry_releases")
    named: set[str] = set()
    if type(releases) is list:
        seen: set[str] = set()
        for position, release in enumerate(releases):
            where = f"{_rel(report.root, entry_path)} releases[{position}]"
            if type(release) is not dict:
                report.error(where, "release_fields", "not an object")
                continue
            if not _check_release_fields(report, where, release, seen):
                continue
            version = release["version"]
            named.add(version)
            release_dir = os.path.join(entry_dir, version)
            if version not in subdirs:
                if tree.kind(release_dir) is not None:
                    continue
                report.error(release_dir, "release_directory_missing")
                continue
            _check_release_directory(report, tree, release_dir, plugin_id, release, in_examples)
    for name, path in subdirs.items():
        if name not in named:
            report.error(path, "entry_release_unlisted")
    return entry


def _check_area(report: Report, tree: _Tree, area: str, in_examples: bool) -> list:
    entries = []
    for name, path, kind in tree.children(area):
        if kind == "link":
            report.error(path, "registry_symlink")
        elif kind == "dir":
            entry = _check_entry(report, tree, path, in_examples)
            if entry is not None:
                entries.append(entry)
        elif kind == "file" and (name == README_FILE or (not in_examples and name in (INDEX_FILE, INDEX_SIGNATURE_FILE))):
            continue
        else:
            report.error(path, "registry_unexpected_file")
    return entries


def _check_index(report: Report, tree: _Tree, registry: str, entries: list) -> None:
    signature = os.path.join(registry, INDEX_SIGNATURE_FILE)
    if tree.kind(signature) is not None:
        report.error(signature, "index_signature_reserved")
    path = os.path.join(registry, INDEX_FILE)
    try:
        raw = tree.read(path, MAX_INDEX_BYTES)
    except _TooLarge:
        report.error(path, "index_not_json", "over 8 MiB")
        return
    except (_Symlink, _NotRegular):
        return
    if raw is None:
        report.error(path, "index_missing")
        return
    try:
        index = _load_json(raw)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        report.error(path, "index_not_json", str(exc).splitlines()[0][:200])
        return
    if type(index) is not dict:
        report.error(path, "index_not_json", "not an object")
        return
    if set(index) != {"schema", "releases"} or index.get("schema") != INDEX_SCHEMA or type(index.get("releases")) is not list:
        report.error(path, "index_fields")
        return
    if raw != index_bytes(build_index(entries)):
        report.error(path, "index_stale")


def platform_gap() -> str | None:
    """The first name the registry reads need that this platform lacks, or None when it has them all."""
    if os.name == "nt":
        return "os.name posix"
    for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC"):
        if not hasattr(os, name):
            return name
    # By name: the sets hold the platform's own functions, and a caller may stand in for os.open.
    if not {"open", "stat"} <= {getattr(item, "__name__", "") for item in os.supports_dir_fd}:
        return "dir_fd"
    if "listdir" not in {getattr(item, "__name__", "") for item in os.supports_fd}:
        return "listdir of a descriptor"
    return None


def check_registry(root: str | os.PathLike) -> Report:
    """Check ``root`` (the folder that contains ``registry/``) and return the report."""
    text = os.fspath(root)
    # The marketplace checker's code for a system that cannot open a directory without following
    # links. The report comes back before the root or any child is opened.
    missing = platform_gap()
    if missing is not None:
        report = Report(os.path.abspath(text))
        report.error(".", "root_unavailable",
                     f"this system cannot open a directory without following links ({missing} is missing)")
        return report
    try:
        tree = _Tree(text)
    except _RootUnavailable as exc:
        report = Report(os.path.abspath(text))
        report.error(".", "root_unavailable", str(exc))
        return report
    try:
        return _check(tree)
    finally:
        tree.close()


def _check(tree: _Tree) -> Report:
    report = Report(tree.path)
    registry = os.path.join(tree.path, "registry")
    try:
        kind = tree.kind(registry)
        if kind != "dir":
            report.error(registry, "registry_symlink" if kind == "link" else "registry_missing")
            return report
        entries = _check_area(report, tree, registry, in_examples=False)
        _check_index(report, tree, registry, entries)
        examples = os.path.join(tree.path, "examples")
        kind = tree.kind(examples)
        if kind == "link":
            report.error(examples, "registry_symlink")
        elif kind == "dir":
            _check_area(report, tree, examples, in_examples=True)
    except _Symlink as exc:
        report.error(exc.path or registry, "registry_symlink")
    except _NotRegular as exc:
        report.error(exc.path or registry, "registry_unexpected_file")
    except FileNotFoundError:
        report.error(registry, "registry_missing")
    return report
