"""The decision for one Windows plugin-read handle, on facts a caller has already gathered.

Pure: no file is opened, no OS call is made, and nothing in the product calls this yet. Windows
refuses every plugin read at the shared adapter boundary (PRODUCT 616), and that stays the
behaviour everywhere; this module is the decision a later handle reader will consult.

It judges ONE handle, so a folder-by-folder walk can call it at each step and a leaf-only open can
call it once. The caller supplies the facts of that handle: its attributes, reparse tag, link
count, file type, its final path, the final path of the pinned root (taken from a handle on the
root, so the operator's typed spelling never enters the compare), the expected relative name below
that root, and whether the owner and access list are the operator's alone.

The answer is ``None`` (read it) or an existing refusal cause:

* any reparse attribute or tag: ``plugin_file_unreadable`` (the class of today's no-follow error);
* final path not equal, exact case, to the root final path joined with the expected name:
  ``plugin_file_unreadable`` (an ancestor junction, a short name, a case-only rename);
* not a disk file of the expected kind: ``plugin_file_untrusted``;
* link count above 1: ``plugin_file_untrusted``;
* owner not operator-only: ``plugin_file_untrusted``;
* the revocation store: always ``platform_unsupported`` (no checked handle can feed sqlite yet);
* facts that cannot be judged, or an unknown kind: ``platform_unsupported``.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "CAUSES", "FILE_ATTRIBUTE_ARCHIVE", "FILE_ATTRIBUTE_DIRECTORY", "FILE_ATTRIBUTE_REPARSE_POINT",
    "FILE_TYPE_DISK", "IO_REPARSE_TAG_MOUNT_POINT", "IO_REPARSE_TAG_SYMLINK", "KIND_DIRECTORY", "KIND_FILE",
    "KIND_REVOCATION_STORE", "WindowsHandleFacts", "judge_handle",
]

# Windows constants, written out so that no Windows module is imported.
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_ARCHIVE = 0x20
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_TYPE_DISK = 0x0001
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
IO_REPARSE_TAG_SYMLINK = 0xA000000C

KIND_FILE = "file"
KIND_DIRECTORY = "directory"
KIND_REVOCATION_STORE = "revocation_store"

PLATFORM_UNSUPPORTED = "platform_unsupported"
PLUGIN_FILE_UNREADABLE = "plugin_file_unreadable"
PLUGIN_FILE_UNTRUSTED = "plugin_file_untrusted"
CAUSES = (PLATFORM_UNSUPPORTED, PLUGIN_FILE_UNREADABLE, PLUGIN_FILE_UNTRUSTED)

_SEPARATOR = "\\"


@dataclass(frozen=True)
class WindowsHandleFacts:
    """What one open handle says about the object it names. Every field comes from that handle."""

    attributes: int
    reparse_tag: int
    link_count: int
    file_type: int
    final_path: str
    root_final_path: str
    expected_name: str
    owner_ok: bool


def _is_count(value: object, low: int) -> bool:
    return type(value) is int and value >= low


def _judgeable(facts: object) -> bool:
    if not isinstance(facts, WindowsHandleFacts):
        return False
    if not (_is_count(facts.attributes, 0) and _is_count(facts.reparse_tag, 0)
            and _is_count(facts.link_count, 1) and _is_count(facts.file_type, 0)):
        return False
    if not all(type(v) is str for v in (facts.final_path, facts.root_final_path, facts.expected_name)):
        return False
    return type(facts.owner_ok) is bool and bool(facts.root_final_path.rstrip(_SEPARATOR))


def _expected_final_path(facts: WindowsHandleFacts) -> str:
    root = facts.root_final_path.rstrip(_SEPARATOR)
    return root + _SEPARATOR + facts.expected_name if facts.expected_name else facts.root_final_path


def judge_handle(facts: WindowsHandleFacts, kind: str) -> str | None:
    """``None`` when the handle may be read (or walked through), else the refusal cause."""
    # Only a plugin file or a directory step can pass; the revocation store and any other kind
    # always answer platform_unsupported, whatever its facts say.
    if kind not in (KIND_FILE, KIND_DIRECTORY) or not _judgeable(facts):
        return PLATFORM_UNSUPPORTED
    if facts.reparse_tag != 0 or facts.attributes & FILE_ATTRIBUTE_REPARSE_POINT:
        return PLUGIN_FILE_UNREADABLE
    if facts.final_path != _expected_final_path(facts):
        return PLUGIN_FILE_UNREADABLE
    is_directory = bool(facts.attributes & FILE_ATTRIBUTE_DIRECTORY)
    if facts.file_type != FILE_TYPE_DISK or is_directory != (kind == KIND_DIRECTORY):
        return PLUGIN_FILE_UNTRUSTED
    if facts.link_count > 1:
        return PLUGIN_FILE_UNTRUSTED
    if not facts.owner_ok:
        return PLUGIN_FILE_UNTRUSTED
    return None
