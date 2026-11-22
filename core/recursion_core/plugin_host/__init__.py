"""Core code that runs inside a plugin host process, before any plugin byte.

``wall_linux``: the wall a host applies to itself on Linux (Landlock and one seccomp filter, through
``ctypes`` and ``os``) and the probe that proves it from inside and returns the record ``wall.v1``.
Nothing here imports the daemon, and importing this package walls nothing: only a call does.
"""
