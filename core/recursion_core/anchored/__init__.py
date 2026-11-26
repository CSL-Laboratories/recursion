"""Anchored paths: open what lies below a folder without following a link on the way.

The one way to refuse links: hold the folder open, then open each single name below it relative to
the held parent (on Linux and macOS ``os.open(name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC,
dir_fd=parent)``), so that no part of a path is resolved again by its text after it was checked; a
link in any part refuses. The module that does this comes in the next slice. This package holds
only the system facts it will rest on: ``tests/test_system_facts.py`` and ``FACTS.md``.
"""
