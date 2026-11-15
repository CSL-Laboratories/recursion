"""Exec an engine child with exactly the environment the daemon built (CSL-738).

Runs as the main process of a transient ``recursion-run-*.service`` unit, started with
``python -I -S <this file> <env fifo> -- <executable> <argv...>``. It reads one
length-prefixed JSON object of strings from the FIFO and replaces itself with the
engine through ``execve``. The engine therefore keeps the unit's main pid and cgroup,
and it never sees the user manager's environment: only the names the child-environment
policy (``recursion_core.child_env``, B1) accepted.

The environment is never an argument or a unit property, so a credential in it is not
visible in ``/proc/<pid>/cmdline`` or ``systemctl show``. This file imports only the
standard library and nothing from ``recursion_core``. Any failure exits 127 without
running the engine.
"""
import json
import os
import sys

MAX_ENV_BYTES = 60000  # under the 64 KiB a Linux FIFO buffers, so the daemon's write never blocks


def read_env(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        header = b''
        while not header.endswith(b'\n'):
            byte = os.read(fd, 1)
            if not byte or len(header) > 8:
                raise ValueError('environment header')
            header += byte
        size = int(header)
        if not 0 <= size <= MAX_ENV_BYTES:
            raise ValueError('environment size')
        body = b''
        while len(body) < size:
            chunk = os.read(fd, size - len(body))
            if not chunk:
                raise ValueError('environment truncated')
            body += chunk
    finally:
        os.close(fd)
    env = json.loads(body)
    if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and k and '=' not in k and '\0' not in k + v
            for k, v in env.items()):
        raise ValueError('environment shape')
    return env


def main(argv):
    try:
        if len(argv) < 4 or argv[2] != '--' or not os.path.isabs(argv[3]):
            raise ValueError('usage')
        env = read_env(argv[1])
        os.execve(argv[3], argv[3:], env)
    except BaseException as exc:  # noqa: BLE001 - nothing may fall through to a half-built launch
        # The class only: an exception message could quote a value.
        sys.stderr.write('recursion engine_exec: launch refused: %s\n' % type(exc).__name__)
        os._exit(127)


if __name__ == '__main__':
    main(sys.argv)
