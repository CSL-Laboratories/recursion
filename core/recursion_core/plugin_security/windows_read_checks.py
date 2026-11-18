"""The decision for one Windows plugin-read handle, on facts a caller has already gathered.

Pure: no file is opened, no OS call is made, and nothing in the product calls this yet. Windows
refuses every plugin read at the shared adapter boundary (PRODUCT 616), and that stays the
behaviour everywhere; this module is the decision a later handle reader will consult.

It judges ONE handle, so a folder-by-folder walk can call it at each step and a leaf-only open can
call it once. Which fact comes from where is written on ``WindowsHandleFacts``: the handle's own
facts come from the one handle that is then read; the root final path comes from the pinned root's
handle; the expected name is the code's own name below the root, never taken from a final path.

The answer is ``None`` (read it, or walk through it) or an existing refusal cause. A plugin file
answers ``plugin_file_unreadable`` or ``plugin_file_untrusted``; a directory step answers
``plugin_directory_untrusted``, the cause the daemon gives a directory it will not use:

* an expected name that cannot be judged (empty for a file; an empty part, ``.`` or ``..``; a
  ``:``; a ``/``; a part ending in a dot or a space; a device name such as ``CON`` or ``COM1.py``
  in any letter case, ``COM0``, ``LPT0`` and the superscript-digit forms included; a reserved
  character ``< > " | ? *``; a control character):
  ``plugin_file_unreadable``;
* one handle fact that could not be read (``None``), or a link count of 0: ``plugin_file_unreadable``;
* any reparse attribute or tag: ``plugin_file_unreadable``;
* final path not equal, exact case, to the root final path joined with the expected name:
  ``plugin_file_unreadable`` (an ancestor junction, a short name, a case-only rename, a root
  spelled as typed rather than as the root's handle reports it);
* not a disk file of the expected kind: ``plugin_file_untrusted``;
* an offline or recall attribute bit (the data is not on this disk): ``plugin_file_untrusted``;
* link count above 1: ``plugin_file_untrusted``;
* owner not operator-only: ``plugin_file_untrusted``;
* the revocation store: always ``platform_unsupported`` (no checked handle can feed sqlite yet);
* facts that cannot be judged at all, or an unknown kind: ``platform_unsupported``.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "CAUSES", "FILE_ATTRIBUTE_ARCHIVE", "FILE_ATTRIBUTE_DIRECTORY", "FILE_ATTRIBUTE_OFFLINE",
    "FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS", "FILE_ATTRIBUTE_RECALL_ON_OPEN", "FILE_ATTRIBUTE_REPARSE_POINT",
    "FILE_TYPE_DISK", "IO_REPARSE_TAG_MOUNT_POINT", "IO_REPARSE_TAG_SYMLINK", "KIND_DIRECTORY", "KIND_FILE",
    "KIND_REVOCATION_STORE", "WindowsHandleFacts", "judge_handle",
]

# Windows constants, written out so that no Windows module is imported.
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_ARCHIVE = 0x20
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
FILE_TYPE_DISK = 0x0001
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
IO_REPARSE_TAG_SYMLINK = 0xA000000C

_NOT_LOCAL = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS

KIND_FILE = "file"
KIND_DIRECTORY = "directory"
KIND_REVOCATION_STORE = "revocation_store"

PLATFORM_UNSUPPORTED = "platform_unsupported"
PLUGIN_FILE_UNREADABLE = "plugin_file_unreadable"
PLUGIN_FILE_UNTRUSTED = "plugin_file_untrusted"
PLUGIN_DIRECTORY_UNTRUSTED = "plugin_directory_untrusted"
CAUSES = (PLATFORM_UNSUPPORTED, PLUGIN_FILE_UNREADABLE, PLUGIN_FILE_UNTRUSTED, PLUGIN_DIRECTORY_UNTRUSTED)

_SEPARATOR = "\\"
# Names Windows opens as a device in any folder, with or without an extension, in any letter case.
_DEVICE_NAMES = frozenset({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
                          | {f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)})
# COM0 and LPT0, which Microsoft's file naming page lists as reserved (CPython's list does not).
_DEVICE_NAMES_ZERO = frozenset({"COM0", "LPT0"})
# COM and LPT followed by a superscript one, two or three, which Windows also reserves.
_DEVICE_NAMES_SUPERSCRIPT = frozenset(f"{prefix}{digit}" for prefix in ("COM", "LPT")
                                      for digit in ("\u00b9", "\u00b2", "\u00b3"))
_ALL_DEVICE_NAMES = _DEVICE_NAMES | _DEVICE_NAMES_ZERO | _DEVICE_NAMES_SUPERSCRIPT
_RESERVED_CHARACTERS = frozenset('<>"|?*:/')
# The facts one query of the handle gives; ``None`` in one of them means that query failed.
_HANDLE_FACTS = ("attributes", "reparse_tag", "link_count", "file_type", "final_path", "owner_ok")


@dataclass(frozen=True)
class WindowsHandleFacts:
    """What the caller knows about one open handle, and the names it compares that handle against.

    From the handle that is then read (``None`` when that one query failed): ``attributes``,
    ``reparse_tag``, ``link_count``, ``file_type`` and ``final_path`` (the normalized final path,
    in a form that carries the volume). ``owner_ok`` is the caller's judgement of that same handle's
    owner and access list, which also needs the process's own identity.

    Supplied by the caller, not by this handle: ``root_final_path``, the final path of the pinned
    root's own handle; and ``expected_name``, the relative name below that root that the code itself
    typed to open this object (its last part is the name it asked for). ``expected_name`` is NEVER
    taken from ``final_path``: a name derived from the final path makes the compare always true.
    """

    attributes: int | None
    reparse_tag: int | None
    link_count: int | None
    file_type: int | None
    final_path: str | None
    root_final_path: str
    expected_name: str
    owner_ok: bool | None


def _is_count(value: object) -> bool:
    return type(value) is int and value >= 0


def _judgeable(facts: object) -> bool:
    """False when the facts cannot be judged at all: then the check could not be performed."""
    if not isinstance(facts, WindowsHandleFacts):
        return False
    if type(facts.root_final_path) is not str or type(facts.expected_name) is not str:
        return False
    if not facts.root_final_path.rstrip(_SEPARATOR):
        return False
    if all(getattr(facts, name) is None for name in _HANDLE_FACTS):
        return False
    for name in ("attributes", "reparse_tag", "link_count", "file_type"):
        value = getattr(facts, name)
        if value is not None and not _is_count(value):
            return False
    if facts.final_path is not None and type(facts.final_path) is not str:
        return False
    return facts.owner_ok is None or type(facts.owner_ok) is bool


def _plain_name(name: str, kind: str) -> bool:
    if not name:
        return kind == KIND_DIRECTORY  # the pinned root itself
    for part in name.split(_SEPARATOR):
        # A part ending in a dot covers "." and ".." as well.
        if not part or part[-1] in ". ":
            return False
        if any(char in _RESERVED_CHARACTERS or ord(char) < 32 for char in part):
            return False
        if part.split(".", 1)[0].rstrip(" ").upper() in _ALL_DEVICE_NAMES:
            return False
    return True


def _final_path_matches(facts: WindowsHandleFacts) -> bool:
    root = facts.root_final_path.rstrip(_SEPARATOR)
    if not facts.expected_name:
        # The pinned root itself: a trailing separator is taken off both sides alike.
        return facts.final_path.rstrip(_SEPARATOR) == root
    return facts.final_path == root + _SEPARATOR + facts.expected_name


def judge_handle(facts: WindowsHandleFacts, kind: str) -> str | None:
    """``None`` when the handle may be read (or walked through), else the refusal cause."""
    # Only a plugin file or a directory step can pass; the revocation store and any other kind
    # always answer platform_unsupported, whatever its facts say.
    if kind not in (KIND_FILE, KIND_DIRECTORY) or not _judgeable(facts):
        return PLATFORM_UNSUPPORTED
    directory = kind == KIND_DIRECTORY
    unreadable = PLUGIN_DIRECTORY_UNTRUSTED if directory else PLUGIN_FILE_UNREADABLE
    untrusted = PLUGIN_DIRECTORY_UNTRUSTED if directory else PLUGIN_FILE_UNTRUSTED
    if any(getattr(facts, name) is None for name in _HANDLE_FACTS) or facts.link_count == 0:
        return unreadable
    if not _plain_name(facts.expected_name, kind):
        return unreadable
    if facts.reparse_tag != 0 or facts.attributes & FILE_ATTRIBUTE_REPARSE_POINT:
        return unreadable
    if not _final_path_matches(facts):
        return unreadable
    is_directory = bool(facts.attributes & FILE_ATTRIBUTE_DIRECTORY)
    if facts.file_type != FILE_TYPE_DISK or is_directory != directory:
        return untrusted
    if facts.attributes & _NOT_LOCAL:
        return untrusted
    if facts.link_count > 1:
        return untrusted
    if not facts.owner_ok:
        return untrusted
    return None
