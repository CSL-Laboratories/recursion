"""Every sentence a person reads about a plugin's saved data, in ONE table a message catalog can take over.

Each refusal has four parts: ``what`` happened, ``why``, ``next`` (the one act that would allow it, or
what the plugin can do) and ``changed`` (whether anything was saved). The person's word is "saved data",
never "memory": beside megabytes that would read as the working memory of a process. No sentence names a
path. ``PLUGIN``, ``SIZE``, ``LIMIT`` and ``ROW`` are filled in by ``render``; nothing else is.
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
        "Its saved data has reached SIZE, the limit in ROW.",
        "Raise that row and restart Recursion, or set it to none for no limit.",
        "No: nothing of this write was saved."),
    "memory_full_disk": (
        "PLUGIN could not save more.",
        "Your disk is full, so nothing more could be written.",
        "Free some space on your disk; the saved data the plugin already had is kept.",
        "No: nothing of this write was saved."),
    "memory_full_pace": (
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
        "Nothing was deleted.",
        "The damaged file was moved aside."),
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
}

# Which refusal code each entry answers with on the channel (the two grant entries are call_not_offered).
CODE_OF = {name: ("memory_full" if name.startswith("memory_full")
                  else "memory_item_too_large" if name.startswith("memory_item_too_large")
                  else "call_not_offered" if name.startswith("not_granted") else name)
           for name in WORDS}


def size_text(size: int) -> str:
    """A size as a person reads it: whole megabytes, else kilobytes, else bytes (1 MB = 1024 KB here)."""
    if size >= 1 << 20 and size % (1 << 20) == 0:
        return f"{size >> 20} MB"
    if size >= 1024:
        return f"{size // 1024} KB"
    return f"{size} bytes"


def render(entry: str, plugin: str, **fill: str) -> dict:
    """The four parts of ``entry`` for ``plugin``, with SIZE, LIMIT and ROW filled from ``fill``."""
    parts = []
    for text in WORDS[entry]:
        text = text.replace("PLUGIN", plugin)
        for mark in ("SIZE", "LIMIT", "ROW"):
            if mark.lower() in fill:
                text = text.replace(mark, fill[mark.lower()])
        parts.append(text)
    return dict(zip(("what", "why", "next", "changed"), parts))


def not_offered_detail(missing: str, plugin: str, name: str) -> str:
    """The detail of a ``call_not_offered`` for a memory call: which of the two grants is missing, in words."""
    words = render("not_granted_permission" if missing == "permission" else "not_granted_row", plugin)
    return f"{name}: " + " ".join(words[part] for part in ("what", "why", "next", "changed"))
