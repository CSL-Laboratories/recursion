"""The list of registries an install reads, kept in storage the caller hands in.

The list has a shape of its own (``shape``). The shipped registries are data the product ships
(``registries.shipped.json`` beside this module, read by ``shipped_registries``), never a value in
code, and they are kept apart from the person's own: the stored text holds the person's registries,
the commits the person pinned for shipped ones, and the shipped list as it was last read. A later
release that changes, renames or removes a shipped registry never touches or reorders the person's
entries; ``load`` says in one line each what changed. A reader that meets a newer shape refuses and
says which shape it reads.

Each registry has a name the person gave it, its kind (``official``, ``published`` or ``local``), its
address, and the full commit it is read at (none for a folder of the kind ``local``). The kind is a
label a person reads and never a gate: nothing here or in the install lets one kind pass a check
another fails. Which registry a plugin comes from is never guessed: ``choose`` takes the one listed
registry that holds the id and refuses, naming them, when more than one does. The order of the list
never decides.

This module reads and writes no file of its own: ``load`` and ``save`` take a store with ``read()``
(bytes, or None when nothing is stored) and ``write(bytes)``. The product hands it the person's one
settings file once that file is on this base; until then no command writes the list.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

from .install import InstallRefused

__all__ = ["READ_SHAPE", "Registry", "RegistryList", "choose", "load", "save", "shipped_registries"]

# The newest shape of the stored list this reader reads. A newer one is refused, never guessed.
READ_SHAPE = 1
KINDS = ("official", "published", "local")
_NAME = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_SHIPPED_FILE = Path(__file__).with_name("registries.shipped.json")
_STORED_KEYS = frozenset({"shape", "registries", "pins", "shipped_seen"})
_ENTRY_KEYS = frozenset({"name", "kind", "address", "commit"})


@dataclasses.dataclass(frozen=True)
class Registry:
    name: str
    kind: str
    address: str
    commit: str | None
    shipped: bool = False

    def as_given(self) -> str:
        """The registry as the install reads it: ``<address>@<commit>``, or the folder."""
        return self.address if self.commit is None else f"{self.address}@{self.commit}"


@dataclasses.dataclass
class RegistryList:
    own: list
    pins: dict
    shipped: list
    # One line for each change of the shipped registries since the stored list last read them.
    changed: list = dataclasses.field(default_factory=list)

    def registries(self) -> list:
        """The person's registries, then the shipped ones with the person's pins applied."""
        listed = list(self.own)
        for item in self.shipped:
            commit = self.pins.get(item.name, item.commit)
            listed.append(dataclasses.replace(item, commit=commit))
        return listed

    def named(self, name: str) -> Registry | None:
        return next((item for item in self.registries() if item.name == name), None)

    def add(self, registry: Registry) -> None:
        if self.named(registry.name) is not None:
            raise _refused("registry_name_taken", f"A registry named {registry.name} is listed already. "
                           "Give the new one another name, or remove the listed one first.")
        self.own.append(_checked_entry(dataclasses.asdict(registry) | {"shipped": False}))

    def remove(self, name: str) -> None:
        """Take a registry of the person's off the list. Its installed plugins stay installed."""
        kept = [item for item in self.own if item.name != name]
        if len(kept) == len(self.own):
            if any(item.name == name for item in self.shipped):
                raise _refused("registry_shipped", f"{name} is a registry this release ships; it cannot be "
                               "removed, only pinned to another commit.")
            raise _refused("registry_not_listed", f"No registry named {name} is listed.")
        self.own = kept

    def pin(self, name: str, commit: str) -> None:
        """Move the pin of one registry to ``commit``: only on the person's yes, asked by the caller."""
        if type(commit) is not str or _COMMIT.fullmatch(commit) is None:
            raise _refused("registry_commit_refused", "A pin is a full commit of 40 lowercase hex.")
        for position, item in enumerate(self.own):
            if item.name == name:
                if item.commit is None:
                    raise _refused("registry_folder_unpinned", f"{name} is a folder; a folder has no commit.")
                self.own[position] = dataclasses.replace(item, commit=commit)
                return
        if any(item.name == name for item in self.shipped):
            self.pins[name] = commit
            return
        raise _refused("registry_not_listed", f"No registry named {name} is listed.")


def _refused(code: str, detail: str) -> InstallRefused:
    return InstallRefused(code, detail=detail, show_detail=True)


def _checked_entry(item: object, *, shipped: bool = False) -> Registry:
    if type(item) is not dict or set(item) - {"shipped"} != _ENTRY_KEYS:
        raise _refused("registry_list_damaged", "A registry in the list does not have exactly name, kind, "
                       "address and commit. Correct it in your settings, or remove it and add it again.")
    name, kind, address, commit = item["name"], item["kind"], item["address"], item["commit"]
    if type(name) is not str or _NAME.fullmatch(name) is None:
        raise _refused("registry_list_damaged", "A registry's name is not a short lowercase name.")
    if kind not in KINDS:
        raise _refused("registry_list_damaged", f"The registry {name} has the kind {kind!s}; the kinds "
                       "are official, published and local.")
    if type(address) is not str or not address:
        raise _refused("registry_list_damaged", f"The registry {name} has no address.")
    if commit is None:
        if kind != "local":
            raise _refused("registry_list_damaged", f"The registry {name} has no full commit; only a "
                           "folder of the kind local has none.")
    elif type(commit) is not str or _COMMIT.fullmatch(commit) is None:
        raise _refused("registry_list_damaged", f"The registry {name} is not at a full commit.")
    return Registry(name, kind, address, commit, shipped)


def _shape_of(value: object) -> int:
    if type(value) is not dict or type(value.get("shape")) is not int or value["shape"] < 1:
        raise _refused("registry_list_damaged", "The registry list has no shape number. Correct it in your "
                       "settings, or remove it to start an empty list.")
    if value["shape"] > READ_SHAPE:
        raise _refused("registry_list_shape_newer",
                       f"The registry list was written in shape {value['shape']}; this recursion reads shape "
                       f"{READ_SHAPE} and older. Use a recursion that reads shape {value['shape']}, or the list "
                       "stays as it is and this install names its registry with --registry.")
    return value["shape"]


def shipped_registries(raw: bytes | None = None) -> list:
    """The registries this release ships, from its data file (or ``raw``, the same form)."""
    data = json.loads((raw if raw is not None else _SHIPPED_FILE.read_bytes()).decode("utf-8"))
    _shape_of(data)
    if set(data) != {"shape", "registries"} or type(data["registries"]) is not list:
        raise _refused("registry_list_damaged", "The shipped registry list is not in its shape.")
    return [_checked_entry(item, shipped=True) for item in data["registries"]]


def load(store, *, shipped: list | None = None) -> RegistryList:
    """Read the list from ``store``; ``shipped`` stands in for the release's data file."""
    shipped = shipped_registries() if shipped is None else shipped
    raw = store.read()
    if raw is None:
        return RegistryList([], {}, shipped)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _refused("registry_list_damaged", "The registry list is not JSON. Correct it in your settings, "
                       "or remove it to start an empty list.") from None
    _shape_of(value)
    if type(value) is not dict or not set(value) <= _STORED_KEYS:
        raise _refused("registry_list_damaged", "The registry list holds a field this recursion does not read.")
    own = [_checked_entry(item) for item in value.get("registries", [])]
    pins = value.get("pins", {})
    if type(pins) is not dict or any(type(c) is not str or _COMMIT.fullmatch(c) is None for c in pins.values()):
        raise _refused("registry_list_damaged", "A pin in the registry list is not a full commit.")
    seen = [_checked_entry(item, shipped=True) for item in value.get("shipped_seen", [])]
    names = [item.name for item in own]
    if len(set(names)) != len(names) or set(names) & {item.name for item in shipped}:
        raise _refused("registry_list_damaged", "Two registries in the list have the same name. Rename one "
                       "in your settings.")
    result = RegistryList(own, dict(pins), shipped)
    result.changed = _shipped_changes(seen, shipped) if "shipped_seen" in value else []
    # A pin of a shipped registry that is no longer shipped stays stored, and is said.
    for name in sorted(set(pins) - {item.name for item in shipped}):
        result.changed.append(f"Your pin of the registry {name} is kept, but this release no longer ships it.")
    return result


def _shipped_changes(seen: list, shipped: list) -> list:
    before = {item.name: item for item in seen}
    now = {item.name: item for item in shipped}
    lines = []
    for name in sorted(set(before) - set(now)):
        lines.append(f"This release no longer ships the registry {name}; your own registries are unchanged.")
    for name in sorted(set(now) - set(before)):
        lines.append(f"This release ships a new registry {name} at {now[name].as_given()}.")
    for name in sorted(set(now) & set(before)):
        if now[name] != before[name]:
            lines.append(f"This release ships the registry {name} at {now[name].as_given()} "
                         f"(it was {before[name].as_given()}).")
    return lines


def save(store, value: RegistryList) -> None:
    """Write the person's part and the shipped list as read now, in the newest shape. Order is kept."""
    def entry(item: Registry) -> dict:
        return {"address": item.address, "commit": item.commit, "kind": item.kind, "name": item.name}

    text = {"shape": READ_SHAPE, "registries": [entry(item) for item in value.own],
            "pins": dict(sorted(value.pins.items())), "shipped_seen": [entry(item) for item in value.shipped]}
    store.write((json.dumps(text, indent=2, sort_keys=True) + "\n").encode("ascii"))


def choose(value: RegistryList, plugin_id: str, holds) -> Registry:
    """The one listed registry that holds ``plugin_id``; ``holds(registry)`` says whether it does.

    None that holds it refuses ``plugin_not_in_any_registry``; more than one refuses
    ``plugin_in_several_registries`` with their names and how to name one. The order never decides.
    """
    listed = value.registries()
    if not listed:
        raise _refused("registry_not_named", "No registry is named, so there is nothing to install from. Name "
                       "one for this install with --registry <address>@<full commit> or --registry <folder>.")
    holding = [item for item in listed if holds(item)]
    if not holding:
        names = ", ".join(item.name for item in listed)
        raise _refused("plugin_not_in_any_registry", f"No listed registry holds {plugin_id} (read: {names}). "
                       "Check the id, or name the registry that holds it with --registry.")
    if len(holding) > 1:
        names = ", ".join(f"{item.name} ({item.as_given()})" for item in sorted(holding, key=lambda i: i.name))
        raise _refused("plugin_in_several_registries",
                       f"{plugin_id} stands in more than one listed registry: {names}. Name the one to install "
                       "from with --registry <name>, or with its address and commit.")
    return holding[0]
