"""Every sentence a person reads about a plugin's saved data, in ONE table a message catalog can take over.

Each refusal has four parts: ``what`` happened, ``why``, ``next`` (the one act that would allow it, or
what the plugin can do) and ``changed`` (whether anything was saved). The person's word is "saved data",
never "memory": beside megabytes that would read as the working memory of a process. No sentence names a
path. ``PLUGIN``, ``SIZE``, ``LIMIT``, ``ROW``, ``CAUSE`` and ``ACT`` are filled in by ``render``; nothing
else is.
"""

from __future__ import annotations

# The sentence of the settings row that grants the second half (the first half is the install permission).
ROW_PLUGINS = ("Plugins listed here may keep saved data between runs. Recursion keeps it for them in its own "
               "state folder; no other plugin can read it, and Recursion never hands it to a task it runs, "
               "though the plugin itself may put what it saved into a task it asks for. Read when Recursion "
               "starts. Empty as shipped.")

_CHANGED_NOTHING = "No: nothing was saved."

# code -> (what, why, next, changed). Append-only, like every list of the channel.
WORDS: dict[str, tuple[str, str, str, str]] = {
    "not_granted_permission": (
        "PLUGIN tried to keep saved data between runs.",
        "It was not given the permission to keep saved data when it was installed.",
        "To allow it, install it again and grant it saved data, then restart Recursion. Only your own act "
        "widens what a plugin can do.",
        _CHANGED_NOTHING),
    "not_granted_row": (
        "PLUGIN tried to keep saved data between runs.",
        "It is not listed in plugin_memory.plugins in your settings file.",
        "To allow it, add PLUGIN to plugin_memory.plugins and restart Recursion. Settings are read when "
        "Recursion starts. Only your own act widens what a plugin can do.",
        _CHANGED_NOTHING),
    "memory_full": (
        "PLUGIN could not save more.",
        "With this write its saved data would pass SIZE, the limit in ROW.",
        "Raise that row and restart Recursion, or set it to none for no limit.",
        "No: nothing of this write was saved."),
    "memory_full_disk": (
        "PLUGIN could not save more.",
        "Your disk is full, so nothing more could be written.",
        "Free some space on your disk; the saved data the plugin already had is kept.",
        "No: nothing of this write was saved."),
    "memory_paced": (
        "PLUGIN asked for its saved data too often.",
        "It made LIMIT calls in one minute, the limit in plugin_memory.calls_per_minute.",
        "It can ask again in a minute; or raise that row and restart Recursion, or set it to none for no limit.",
        "No: nothing of this call was done."),
    "memory_item_too_large": (
        "PLUGIN tried to save one item of SIZE.",
        "One item can be at most LIMIT: that is what fits one message between Recursion and a plugin.",
        "The plugin can save it in smaller parts.",
        _CHANGED_NOTHING),
    "memory_item_too_large_row": (
        "PLUGIN tried to save one item of SIZE.",
        "One item can be at most LIMIT, the limit in plugin_memory.max_item_bytes.",
        "The plugin can save it in smaller parts; or raise that row and restart Recursion.",
        _CHANGED_NOTHING),
    "memory_unreadable": (
        "PLUGIN's saved data could not be read.",
        "Its file is damaged, so Recursion set it aside and gave the plugin none.",
        "The plugin goes on with no saved data. The damaged file stays in Recursion's state folder for you; no "
        "command of Recursion lists, shows or restores saved data yet.",
        "The damaged file was moved aside; nothing was deleted."),
    "memory_key_unknown": (
        "PLUGIN asked for an item of its saved data that it does not have.",
        "No item of its own saved data has that key.",
        "The plugin can list the keys it has.",
        "No: nothing was changed."),
    "memory_revision_moved": (
        "PLUGIN tried to change an item of its saved data that had changed since it read it.",
        "The item is at another revision than the one the plugin named.",
        "The plugin can read the item again and decide anew.",
        _CHANGED_NOTHING),
    # Appended in the round on 08f90f7e.
    "not_granted_both": (
        "PLUGIN tried to keep saved data between runs.",
        "It was not given the permission to keep saved data when it was installed, and it is not listed in "
        "plugin_memory.plugins in your settings file.",
        "To allow it, install it again and grant it saved data, add PLUGIN to plugin_memory.plugins, then "
        "restart Recursion. Only your own act widens what a plugin can do.",
        _CHANGED_NOTHING),
    "not_granted_unknown": (
        "PLUGIN tried to keep saved data between runs.",
        "Recursion could not learn whether PLUGIN holds the permission to keep saved data, so it allowed nothing.",
        "The plugin can ask again; if this goes on, restart Recursion. Only your own act widens what a plugin "
        "can do.",
        _CHANGED_NOTHING),
    "memory_unavailable": (
        "PLUGIN's saved data could not be opened just now.",
        "Recursion could not open its file because CAUSE; the file is not damaged as far as Recursion can tell.",
        "Recursion tries again at the plugin's next call; if this goes on, ACT.",
        "Nothing was moved or deleted: the file and its data stay where they were."),
    "memory_full_revision": (
        "PLUGIN could not change this item of its saved data again.",
        "The item is at revision SIZE, the largest number a message between Recursion and a plugin carries.",
        "The plugin can delete the item and save it anew, which starts it again at revision 1.",
        "No: nothing of this write was saved."),
}

# Which refusal code each entry answers with on the channel (the grant entries are call_not_offered).
CODE_OF = {name: ("memory_full" if name in ("memory_full", "memory_full_disk", "memory_full_revision")
                  else "memory_item_too_large" if name.startswith("memory_item_too_large")
                  else "call_not_offered" if name.startswith("not_granted") else name)
           for name in WORDS}


def count_text(count: int) -> str:
    """A count of items as a person reads it: 1 item, 2 items."""
    return f"{count} item" if count == 1 else f"{count} items"


def size_text(size: int) -> str:
    """A size as a person reads it: whole megabytes, else kilobytes, else bytes (1 MB = 1024 KB here)."""
    if size >= 1 << 20 and size % (1 << 20) == 0:
        return f"{size >> 20} MB"
    if size >= 1024:
        return f"{size // 1024} KB"
    return f"{size} bytes"


def render(entry: str, plugin: str, **fill: str) -> dict:
    """The four parts of ``entry`` for ``plugin``, with SIZE, LIMIT, ROW, CAUSE and ACT filled from ``fill``."""
    parts = []
    for text in WORDS[entry]:
        text = text.replace("PLUGIN", plugin)
        for mark in ("SIZE", "LIMIT", "ROW", "CAUSE", "ACT"):
            if mark.lower() in fill:
                text = text.replace(mark, fill[mark.lower()])
        parts.append(text)
    return dict(zip(("what", "why", "next", "changed"), parts))


def not_offered_detail(missing: str, plugin: str, name: str) -> str:
    """The detail of a ``call_not_offered`` for a memory call: which of the two grants is missing (or both, or
    that the core could not say), in words."""
    words = render("not_granted_" + missing, plugin)
    return f"{name}: " + " ".join(words[part] for part in ("what", "why", "next", "changed"))
