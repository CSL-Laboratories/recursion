"""Anchored paths: open what lies below a folder without following a link on the way.

The one way to refuse links: hold the folder open, then open each single name below it relative to
the held parent (on Linux and macOS ``os.open(name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC,
dir_fd=parent)``), so that no part of a path is resolved again by its text after it was checked; a
link in any part refuses. The module that does this comes in the next slice. This package holds
only the system facts it will rest on: ``tests/test_system_facts.py`` and ``FACTS.md``.

The module is here now (``_posix.py``), and nothing uses it yet:

- ``open_anchor(path, *, above="owner_trusted" | "operator_root") -> Anchor`` vets one folder, opens
  it, holds it, and checks that the folder it holds is the folder it vetted (the same device and file
  number). ``owner_trusted``: on the way to the folder a link is followed only when root or the user
  owns it, every folder on the way is root's or the user's, a folder group or world can write is
  passed only when it is sticky, at most 40 links are followed, and ``..`` goes up from where the
  look-up got to (the trust store's rule). ``operator_root``: a root the operator typed for one
  command is followed as typed, once, and then held (the registry checker's rule).
- An ``Anchor`` offers ``child``, ``ensure_dir``, ``read``, ``write_atomic``, ``entries``, ``kind``,
  ``rename``, ``remove`` and ``release``, and is a context manager. Every name is a single part
  (``a/b`` is two ``child`` calls); ``.``, ``..``, the empty name and a name with a separator refuse.
  Below the anchor no link is ever followed, whoever owns it.
- Every refusal is one ``AnchorRefused(cause, path, way_out)``: ``cause`` is one of ``CAUSES``, a
  finite set that is only ever appended to; ``way_out`` is one plain sentence naming the absolute,
  quoted path and the step that ends the refusal. Nothing is written, moved or removed by a refusal.
  An error that is not about trust (a full disk, a folder that is not empty) stays an ``OSError``.
- On a system without folder-relative opens, ``O_NOFOLLOW`` or ``O_DIRECTORY`` (Windows today),
  ``open_anchor`` refuses ``platform_unsupported`` before anything is opened.

It is not a wall against code running as the same user, which can change the user's own files.
"""

from recursion_core.anchored._posix import ABOVE, CAUSES, Anchor, AnchorRefused, open_anchor

__all__ = ["ABOVE", "CAUSES", "Anchor", "AnchorRefused", "open_anchor"]
