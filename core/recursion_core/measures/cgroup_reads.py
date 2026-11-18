"""A pure reader of ONE cgroup v2 leaf directory: memory.peak, cpu.stat, pids.peak, io.stat.

It opens only the four names directly inside the directory it is given: no walk upward, no
other name, no write, no Linux-only call (it never calls require_linux_calls), so it runs the
same against a fake leaf on any platform. A missing, unreadable or malformed file gives
certainty "unknown" and value None, never 0 (PRODUCT bar 7, pillar P10); no file content makes
it raise. A counter the kernel cannot print (not plain decimal digits, more than 20 digits, or
above 2**64 - 1) is malformed. Not guarded here: a link or a FIFO under one of the four names
(a real cgroupfs holds neither) is opened as given.
"""
import os
import re

MAX_BYTES = 1 << 20  # a cgroup file is small; never read an unbounded file
U64_MAX = 2 ** 64 - 1  # the kernel prints these counters as u64
U64_DIGITS = len(str(U64_MAX))  # 20, checked before int() so a huge token never reaches it
DEVICE = re.compile(r'[0-9]+:[0-9]+')

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
    """A u64 written in plain decimal digits, or None."""
    if not token.isascii() or not token.isdigit() or len(token) > U64_DIGITS:
        return None
    value = int(token)
    return value if value <= U64_MAX else None


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

    The kernel prints a line only for a device that did io, so a readable empty file means no
    device counted io for this leaf: a known 0 (block io only; page-cache hits are not counted).
    A line whose first token is not MAJ:MIN, a token without '=', a key repeated inside one
    line, or a second line for the same device makes the whole file unknown: a real kernel
    writes none of them, and a wrong known figure is worse than the word unknown. A device line
    without the key, a bad value, or a sum past 2**64 - 1 makes that key unknown.
    """
    if text is None:
        return dict.fromkeys(IO_KEYS)
    sums = dict.fromkeys(IO_KEYS, 0)
    devices = set()
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if not DEVICE.fullmatch(parts[0]) or parts[0] in devices:
            return dict.fromkeys(IO_KEYS)
        devices.add(parts[0])
        fields = {}
        for token in parts[1:]:
            key, sep, value = token.partition('=')
            if not sep or key in fields:
                return dict.fromkeys(IO_KEYS)
            fields[key] = value
        for key in IO_KEYS:
            if sums[key] is None:
                continue
            value = _count(fields.get(key, ''))
            total = None if value is None else sums[key] + value
            sums[key] = total if total is not None and total <= U64_MAX else None
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
