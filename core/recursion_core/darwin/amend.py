"""Add the run cookie to the environment FIFO, then exec the engine line.

Run as ``python -I -S amend.py <cookie fd> <cookie name> <engine argv...>``.
The standard library only: ``-I`` does not put this tree on the path.

The launcher writes the FIFO after ``start`` returns, so this process is the
leader, blocked on that FIFO. It then puts the cookie into the object and execs
the engine line (``engine_exec``) with a fresh FIFO. The leader pid does not
change. The cookie is read from the pipe, not from argv.
"""

from __future__ import annotations

import errno
import json
import os
import sys

_MAX_ENV = 60000


def _read_all(fd: int, limit: int) -> bytes:
    data = b''
    while len(data) <= limit:
        chunk = os.read(fd, limit + 1)
        if not chunk:
            return data
        data += chunk
    raise OSError('too long')


def _read_env(path: str) -> dict:
    flags = os.O_RDONLY | os.O_CLOEXEC
    nofollow = getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags | nofollow)
    try:
        header = b''
        while not header.endswith(b'\n'):
            byte = os.read(fd, 1)
            if not byte or len(header) > 8:
                raise OSError('environment header')
            header += byte
        size = int(header)
        if not 0 <= size <= _MAX_ENV:
            raise OSError('environment size')
        body = b''
        while len(body) < size:
            chunk = os.read(fd, size - len(body))
            if not chunk:
                raise OSError('environment truncated')
            body += chunk
    finally:
        os.close(fd)
    env = json.loads(body)
    if not isinstance(env, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in env.items()):
        raise OSError('environment shape')
    return env


def _start_ticks(pid: int) -> int | None:
    """Field 22 of ``/proc/<pid>/stat``, or None when this host has no such row."""

    try:
        raw = open('/proc/%d/stat' % pid, 'r', encoding='ascii', errors='replace').read()
    except OSError:
        return None
    try:
        rest = raw[raw.rindex(')') + 2:].split()
        return int(rest[19])
    except (ValueError, IndexError, OSError):
        return None


def _spawn_writer(path: str, payload: bytes) -> tuple[int, int | None]:
    """Write ``payload`` from a grandchild. Return its pid and start ticks.

    The intermediate child forks the writer and exits. This process reaps
    that child before it execs, so the writer is reparented and is not a
    child of the engine. The engine does not keep the writer's zombie.
    """

    read_fd, write_fd = os.pipe()
    intermediate = os.fork()
    if intermediate == 0:
        os.close(read_fd)
        try:
            writer = os.fork()
        except OSError:
            os.close(write_fd)
            os._exit(127)
        if writer == 0:
            os.close(write_fd)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CLOEXEC)
                data = ('%d\n' % len(payload)).encode('ascii') + payload
                while data:
                    wrote = os.write(fd, data)
                    if wrote <= 0:
                        raise OSError('short write')
                    data = data[wrote:]
                os.close(fd)
            except Exception:
                os._exit(127)
            os._exit(0)
        start = _start_ticks(writer)
        token = ('%d %s\n' % (writer, '' if start is None else start)).encode('ascii')
        try:
            os.write(write_fd, token)
        except Exception:
            os.close(write_fd)
            os._exit(127)
        os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    try:
        while True:
            try:
                os.waitpid(intermediate, 0)
                break
            except OSError as exc:
                if exc.errno != errno.EINTR:
                    raise
        raw = _read_all(read_fd, 64)
    finally:
        os.close(read_fd)
    text = raw.decode('ascii', errors='replace').strip()
    pid_text, _, start_text = text.partition(' ')
    return int(pid_text), int(start_text) if start_text else None


def _env_name(name: str) -> str:
    if not name or '=' in name or '\0' in name or not name.isascii() or not name.replace('_', '').isalnum():
        raise OSError('cookie name')
    return name


def main(argv: list[str]) -> None:
    cookie_fd = int(argv[1])
    name = _env_name(argv[2])
    command = argv[3:]
    try:
        cookie = _read_all(cookie_fd, 64)
    finally:
        os.close(cookie_fd)
    if len(cookie) != 32 or any(byte not in b'0123456789abcdef' for byte in cookie):
        raise OSError('cookie')
    dash = command.index('--')
    fifo = command[dash - 1]
    if not fifo or not os.path.isabs(fifo):
        raise OSError('env fifo')
    env = _read_env(fifo)
    env[name] = cookie.decode('ascii')
    payload = json.dumps(env).encode('utf-8')
    if len(payload) > _MAX_ENV:
        raise OSError('environment size')
    amended = fifo + '.cookie'
    os.mkfifo(amended, 0o600)
    os.chmod(amended, 0o600)
    writer, writer_start = _spawn_writer(amended, payload)
    command = list(command)
    command[dash - 1] = amended
    try:
        os.execv(command[0], command)
    except OSError:
        pass
    # The writer is no longer our child. Signal it only when the start
    # ticks still name the process we forked. A reused pid is left alone,
    # and a host with no proc row is not signalled.
    if writer_start is not None and _start_ticks(writer) == writer_start:
        try:
            os.kill(writer, 9)
        except OSError:
            pass
    os._exit(127)


if __name__ == '__main__':
    try:
        main(sys.argv)
    except Exception:
        os._exit(127)
