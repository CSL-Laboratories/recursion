"""A pure reader of ONE cgroup v2 leaf directory: memory.peak, cpu.stat, pids.peak, io.stat.

It reads only the four named files directly inside the directory it is given: no walk upward,
no other path, no write, no Linux-only call (it never calls require_linux_calls), so it runs the
same against a fake leaf on any platform. A missing, unreadable or malformed file gives
certainty "unknown" and value None, never 0 (PRODUCT bar 7, pillar P10).
"""
import os

MAX_BYTES = 1 << 20  # a cgroup file is small; never read an unbounded file

CPU_KEYS = ('usage_usec', 'user_usec', 'system_usec')
IO_KEYS = ('rbytes', 'wbytes')


def _measure(klass, name, unit, value):
    return {'class': klass, 'name': name, 'value': value, 'unit': unit, 'source': 'daemon',
            'certainty': 'unknown' if value is None else 'known'}


def _read(leaf, name):
    """The text of one file directly inside ``leaf``, or None when it cannot be read."""
    try:
        with open(os.path.join(leaf, name), 'rb') as handle:
            data = handle.read(MAX_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_BYTES:
        return None
    try:
        return data.decode('ascii')
    except UnicodeDecodeError:
        return None


def _count(token):
    """A non-negative decimal integer, or None."""
    if not token.isascii() or not token.isdigit():
        return None
    return int(token)


def _single(text):
    if text is None:
        return None
    lines = text.split()
    return _count(lines[0]) if len(lines) == 1 else None


def _flat_keyed(text, keys):
    """cpu.stat: one ``key value`` per line. A malformed file makes every key unknown."""
    values = dict.fromkeys(keys)
    if text is None:
        return values
    seen = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 2:
            return dict.fromkeys(keys)
        seen[parts[0]] = parts[1]
    for key in keys:
        if key in seen:
            values[key] = _count(seen[key])
    return values


def _io_sums(text):
    """io.stat: ``MAJ:MIN key=value ...`` per device, summed over devices.

    A readable empty file is the kernel saying no device had any io: a known 0. A malformed line,
    or a device line without the key, makes that key unknown.
    """
    if text is None:
        return dict.fromkeys(IO_KEYS)
    sums = dict.fromkeys(IO_KEYS, 0)
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        fields = {}
        for token in parts[1:]:
            key, sep, value = token.partition('=')
            if not sep:
                return dict.fromkeys(IO_KEYS)
            fields[key] = value
        for key in IO_KEYS:
            if sums[key] is None:
                continue
            value = _count(fields.get(key, ''))
            sums[key] = None if value is None else sums[key] + value
    return sums


def read_leaf(leaf):
    """The measures of one cgroup v2 leaf directory, as a list of measure dicts (source daemon)."""
    leaf = os.fspath(leaf)
    measures = [_measure('memory', 'memory.peak', 'bytes', _single(_read(leaf, 'memory.peak')))]
    cpu = _flat_keyed(_read(leaf, 'cpu.stat'), CPU_KEYS)
    measures += [_measure('cpu', f'cpu.{key}', 'usec', cpu[key]) for key in CPU_KEYS]
    measures.append(_measure('pids', 'pids.peak', 'count', _single(_read(leaf, 'pids.peak'))))
    io = _io_sums(_read(leaf, 'io.stat'))
    measures += [_measure('io', f'io.{key}', 'bytes', io[key]) for key in IO_KEYS]
    return measures
