"""Install a plugin by its id from a registry named by its address and a full commit.

``recursion plugin install <plugin_id> --registry <address>@<full commit>`` reads the registry's tree
at that one commit with the git read of ``git_source`` (the git program only, into a scratch folder
that is removed on every path) and hands that tree to ``install_plugin`` as its registry root, so every
check of the registry, the entry, the index, the digests and the signature is the one the install of a
registry folder makes. ``--registry <folder>`` installs from a registry folder already on disk, as
before. An address is a local bare repository, its ``file://`` address, or an ``https://`` address.

Nothing is written into the profile before every check has passed, and a refusal writes nothing: the
scratch folder goes, and the profile is as it was. A registry that cannot be read is said to be so, and
no other registry is tried in its place.
"""

from __future__ import annotations

import os
import re

from .git_source import SourceRefused, registry_address, registry_checkout
from .install import InstallRefused, install_plugin

__all__ = ["install_from_registry", "list_not_wired", "registry_words", "shown_lines", "split_registry"]

# The most lines of a registry's failed check that one refusal prints. It becomes a row of the person's
# settings file once that file is on this base; until then its shipped first value stands here.
_MAX_FINDINGS_SHOWN = 20
# A registry's name in the list (registry_list.py): a short lowercase word with no slash.
_LIST_NAME = re.compile(r"[a-z][a-z0-9_.-]{0,63}")


def list_not_wired(what: str) -> InstallRefused:
    """The refusal of a command that would read or rewrite the registry list, which no file holds yet."""
    return _refused("registry_list_not_wired", given=what)

# What stopped the person and what to do next, for each refusal of this module. ``{given}`` is the
# registry as the person named it, ``{address}`` as it was read, ``{commit}`` the commit, ``{path}`` the file.
_WORDS = {
    "registry_not_named": (
        "No registry is named, so there is nothing to install from. Name one for this install with "
        "--registry <address>@<full commit> or --registry <folder>."),
    "registry_check_failed": (
        "The registry {given} did not pass its check, so nothing was installed. Each file "
        "and the rule it broke is below. Ask the registry's keepers, or name another registry."),
    "registry_list_not_wired": (
        "The list of registries lives in your settings file, and this recursion does not read that file "
        "yet, so the list was neither read nor changed ({given}). Name the registry for each install "
        "with --registry <address>@<full commit> or --registry <folder>."),
    "registry_commit_refused": (
        "The registry {given} is not named at a full commit. Name it as <address>@<the 40-character "
        "commit id>: a branch, a tag or a short id can move, so it is not read."),
    "registry_address_refused": (
        "The registry address {given} is not one this install reads. Use a local bare repository's "
        "folder, its file:// address, or an https:// address, each followed by @<full commit>."),
    "registry_unreachable": (
        "The registry {given} could not be read at commit {commit}: the address did not answer, the "
        "commit is not there, or the fetch took longer than this install waits. Check the address and "
        "the commit and try again. Nothing was installed and no other registry was tried."),
    "registry_link_refused": (
        "The registry {given} holds a link or a submodule at {path}, so it is not read: a registry "
        "is files only. Nothing was installed. Ask the registry's keepers, or name another registry."),
    "registry_too_large": (
        "The registry {given} at commit {commit} holds more files or more bytes than this install "
        "reads. Nothing was installed. Name another registry."),
    "registry_install_script": (
        "The registry {given} holds an install script at {path}, so it is not read: nothing in a "
        "registry is run. Nothing was installed. Name another registry."),
    "registry_path_refused": (
        "The registry {given} holds a file whose name this install does not read: {path}. Nothing "
        "was installed. Name another registry."),
    "registry_not_regular": (
        "The registry {given} holds something that is not a plain file at {path}. Nothing was "
        "installed. Name another registry."),
}

# A fetch refusal of git_source, as this module's refusal.
_FROM_SOURCE = {
    "source_commit_refused": "registry_commit_refused",
    "registry_address_refused": "registry_address_refused",
    "source_not_local": "registry_unreachable",
    "source_unavailable": "registry_unreachable",
    "source_commit_mismatch": "registry_unreachable",
    "registry_link_refused": "registry_link_refused",
    "source_symlink_escapes": "registry_link_refused",
    "registry_too_large": "registry_too_large",
    "source_install_script": "registry_install_script",
    "source_path_escapes": "registry_path_refused",
    "source_not_regular": "registry_not_regular",
}


def shown_lines(result: dict) -> list:
    """What a person reads after an install by registry: the label, the requested permissions, the status."""
    lines = [f"Registry label: {result.get('registry_label')} (a label, not a gate).",
             "Requested permissions (requested by the release, not enforced by this install): "
             + (", ".join(result.get("requested_permissions") or ()) or "none") + "."]
    if result.get("status") in ("deprecated", "withdrawn"):
        reason = (result.get("status_reason") or "no reason given").rstrip(".")
        lines.append(f"This release is {result['status']}: {reason}.")
    return lines


def registry_words(code: str, **values: str) -> str:
    """The sentence a person reads for ``code``, with the values filled in."""
    filled = {"given": "", "address": "", "commit": "", "path": ""}
    filled.update({key: str(value) for key, value in values.items() if value is not None})
    return _WORDS[code].format(**filled)


def _refused(code: str, **values: str) -> InstallRefused:
    return InstallRefused(code, detail=registry_words(code, **values), show_detail=True)


def split_registry(value: object) -> tuple[str, str, str | None]:
    """``(kind, address, commit)`` for a registry the person named.

    ``kind`` is ``pinned`` for ``<address>@<full commit>`` and ``folder`` for a folder on disk. An address
    with a scheme or a ``git@`` form and no full commit, and an ``@`` followed by anything but a full
    commit, refuse ``registry_commit_refused``.
    """
    if type(value) is not str or not value:
        raise _refused("registry_not_named")
    head, sep, tail = value.rpartition("@")
    networked = "://" in value or value.lower().startswith("git@")
    if sep and head and tail and "/" not in tail and ":" not in tail:
        # The commit is proven to be a full commit by the fetch itself, before git starts.
        return "pinned", head, tail
    if networked:
        raise _refused("registry_commit_refused", given=value)
    if _LIST_NAME.fullmatch(value) is not None and not os.path.lexists(value):
        # A registry named by its name in the list: the list lives in the settings file, not read yet.
        raise _refused("registry_list_not_wired", given=value)
    return "folder", value, None


def _checked_install(config, root, plugin_id, given, commit, **options) -> dict:
    """``install_plugin`` on ``root``; a registry that fails its check is refused with each file named."""
    try:
        # A withdrawn release is refused unless the person named it (allow_withdrawn) for this install.
        return install_plugin(config, root, plugin_id, refuse_withdrawn=True, **options)
    except InstallRefused as exc:
        findings = getattr(exc, "findings", None)
        if not findings:
            raise
        lines = [registry_words("registry_check_failed", given=given, commit=commit)]
        for item in findings[:_MAX_FINDINGS_SHOWN]:
            lines.append(f"  {item.path}: {item.code}" + (f" ({item.detail})" if item.detail else ""))
        if len(findings) > _MAX_FINDINGS_SHOWN:
            lines.append(f"  and {len(findings) - _MAX_FINDINGS_SHOWN} more.")
        raise InstallRefused(exc.code, exc.codes, detail="\n".join(lines), show_detail=True) from None


def install_from_registry(config: os.PathLike | str, plugin_id: str, *, registry: str | None = None,
                          version: str | None = None, state: os.PathLike | str | None = None,
                          **install_options) -> dict:
    """Install ``plugin_id`` from the registry named for this call, after every check, or refuse.

    ``install_options`` are ``install_plugin``'s own keywords and are passed on unchanged.
    """
    kind, address, commit = split_registry(registry)
    if kind == "folder":
        result = _checked_install(config, address, plugin_id, registry, None, version=version, state=state,
                                  **install_options)
        result["registry"] = {"given": registry, "kind": "local", "address": address, "commit": None}
        return result
    try:
        resolved = registry_address(address)[1]
    except SourceRefused:
        raise _refused("registry_address_refused", given=registry) from None
    try:
        with registry_checkout(address, commit) as root:
            result = _checked_install(config, root, plugin_id, registry, commit, version=version, state=state,
                                      **install_options)
    except SourceRefused as exc:
        code = _FROM_SOURCE.get(exc.code, "registry_unreachable")
        raise _refused(code, given=registry, address=resolved, commit=commit, path=exc.path) from None
    result["registry"] = {"given": registry, "kind": "pinned", "address": resolved, "commit": commit}
    return result
