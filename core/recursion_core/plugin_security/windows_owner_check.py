"""Whether one Windows file or folder is "operator-owned, nobody else can write" (default A3), on plain facts.

Pure: no file is opened and no OS call is made; nothing in the product calls this yet. The POSIX rule it
mirrors (daemon/plugins.py ``_read_owned`` / ``_owned_directory``) has two halves: the owner is the operator,
and no group or other may write. Here:

* the owner SID is the process user's SID, SYSTEM or Administrators. This half is WIDER than POSIX, which
  refuses any file the operator does not own, root's included (``st_uid != os.geteuid()``); it is the
  default because files installed from an administrator shell are owned by Administrators;
* every allow entry that applies to this object (not inherit-only) and grants a write-class right names one
  of those three, or OWNER RIGHTS (S-1-3-4), which is whoever owns the object and so is the owner already
  judged above. CPython 3.12.4+ makes ``mkdir(mode=0o700)`` folders (``tempfile`` among them) with exactly
  SYSTEM, Administrators and OWNER RIGHTS, and no entry for the user's own SID;
* deny entries are ignored (they only take rights away); an entry of any other type, an absent access list
  (a NULL DACL grants everyone everything; FAT and exFAT keep none), an owner or process identity that could
  not be read, or a fact of the wrong type (exactly a tuple or list of exactly ``AccessEntry``; each
  entry's fields are read once): not ok.

SIDs are compared in their string form (``S-1-...``, as ConvertSidToStringSidW writes them).
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ACCESS_ALLOWED_ACE_TYPE", "ACCESS_DENIED_ACE_TYPE", "ADMINISTRATORS", "CREATOR_OWNER", "INHERIT_ONLY_ACE",
    "OWNER_RIGHTS", "SYSTEM", "AccessEntry", "owner_ok",
]

SYSTEM = "S-1-5-18"
ADMINISTRATORS = "S-1-5-32-544"
OWNER_RIGHTS = "S-1-3-4"
CREATOR_OWNER = "S-1-3-0"  # only meaningful in inherit-only entries; an effective one is judged like any SID

ACCESS_ALLOWED_ACE_TYPE = 0
ACCESS_DENIED_ACE_TYPE = 1
INHERIT_ONLY_ACE = 0x08

# FILE_WRITE_DATA (= FILE_ADD_FILE), FILE_APPEND_DATA (= FILE_ADD_SUBDIRECTORY), FILE_WRITE_EA,
# FILE_WRITE_ATTRIBUTES, DELETE, WRITE_DAC, WRITE_OWNER, GENERIC_WRITE, GENERIC_ALL.
_WRITE_CLASS = 0x2 | 0x4 | 0x10 | 0x100 | 0x10000 | 0x40000 | 0x80000 | 0x40000000 | 0x10000000
_FILE_DELETE_CHILD = 0x40  # a write right on a folder only


@dataclass(frozen=True)
class AccessEntry:
    """One entry of an access list as read from the object: its type, flags, access mask and SID string.

    ``sid`` is ``None`` for an entry type whose SID is not read (any type but allow and deny)."""

    ace_type: int
    flags: int
    mask: int
    sid: str | None


def _is_u32(value: object) -> bool:
    return type(value) is int and 0 <= value <= 0xFFFFFFFF


def _is_sid(value: object) -> bool:
    return type(value) is str and value.startswith("S-1-")


def owner_ok(owner_sid: object, process_sid: object, entries: object, *, directory: bool) -> bool:
    """True only when the owner is trusted and no one else holds a write-class right on this object."""
    if not _is_sid(process_sid) or not _is_sid(owner_sid):
        return False
    trusted = {process_sid, SYSTEM, ADMINISTRATORS}
    if owner_sid not in trusted:
        return False
    if type(entries) not in (tuple, list):
        return False
    writers = trusted | {OWNER_RIGHTS}
    write = _WRITE_CLASS | (_FILE_DELETE_CHILD if directory else 0)
    for entry in tuple(entries):
        if type(entry) is not AccessEntry:
            return False
        ace_type, flags, mask, sid = entry.ace_type, entry.flags, entry.mask, entry.sid
        if not all(_is_u32(value) for value in (ace_type, flags, mask)):
            return False
        if ace_type == ACCESS_DENIED_ACE_TYPE:
            continue
        if ace_type != ACCESS_ALLOWED_ACE_TYPE or not _is_sid(sid):
            return False
        if flags & INHERIT_ONLY_ACE:
            continue
        if mask & write and sid not in writers:
            return False
    return True
