"""Fake native engine binaries for the R2a tests (CSL-732). No real engine is ever run.

With a C compiler, one tiny program is compiled per test run. Each fixture is a copy
of it with a fixed-size config block patched in place: the version line it prints, an
exit code, a sleep, an optional file to dump its environment into, and a nonce so every
fixture has its own SHA-256. Patching keeps the file size, which the same-size-rewrite
tests rely on.

Without a compiler, fixtures are copies of a system native ELF (``/bin/true``) with a
unique trailer, and ``Factory.probe`` is a test-only seam returning the configured line;
tests that need a real child process are skipped.
"""

from __future__ import annotations

import itertools
import os
import shutil
import stat
import subprocess
import tempfile

from recursion_core.launch_check.probe import ProbeResult

MARK = b"CSLR2AFIXTURECFG:"
CFG_LEN = 1024
SEP = "\x1f"

C_SOURCE = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
extern char **environ;
static const char cfg[%(total)d] = "%(mark)s" %(pad)s;
int main(int argc, char **argv) {
    const char * volatile p = cfg;
    char buf[%(len)d + 1];
    memcpy(buf, p + %(marklen)d, %(len)d);
    buf[%(len)d] = 0;
    char *f[6] = {0};
    int n = 0;
    char *s = buf;
    f[n++] = s;
    while (*s && n < 6) { if (*s == '\x1f') { *s = 0; f[n++] = s + 1; } s++; }
    if (n < 6) return 99;
    int code = atoi(f[0]);
    long ms = atol(f[1]);
    if (f[2][0]) {
        FILE *d = fopen(f[2], "w");
        if (d) {
            for (char **e = environ; *e; e++) fprintf(d, "%%s\n", *e);
            fprintf(d, "--argv--\n");
            for (int i = 0; i < argc; i++) fprintf(d, "%%s\n", argv[i]);
            char l[512];
            ssize_t k = readlink("/proc/self/fd/0", l, sizeof l - 1);
            fprintf(d, "--stdin--\n%%s\n", k > 0 ? (l[k] = 0, l) : "?");
            fclose(d);
        }
    }
    if (ms > 0) { struct timespec t = {ms / 1000, (ms %% 1000) * 1000000L}; nanosleep(&t, 0); }
    fputs(f[3], stdout);
    fputs(f[4], stderr);
    return code;
}
"""


def _source() -> str:
    pad = '"' + "x" * CFG_LEN + '"'
    return C_SOURCE % {
        "total": len(MARK) + CFG_LEN + 1,
        "mark": MARK.decode(),
        "pad": pad,
        "len": CFG_LEN,
        "marklen": len(MARK),
    }


class Factory:
    """Builds fixtures under one temp root. ``probe`` is None when real binaries run."""

    def __init__(self) -> None:
        self.root = tempfile.mkdtemp(prefix="csl732-fx-")
        self._n = itertools.count()
        self._fake: dict[str, tuple[str, int]] = {}
        self.compiled = False
        cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
        base = os.path.join(self.root, "base")
        if cc:
            src = os.path.join(self.root, "fake_engine.c")
            with open(src, "w") as f:
                f.write(_source())
            r = subprocess.run([cc, "-O0", "-o", base, src], capture_output=True, timeout=60)
            self.compiled = r.returncode == 0
        if not self.compiled:
            shutil.copyfile("/bin/true", base)
            os.chmod(base, 0o755)
        with open(base, "rb") as f:
            self.base = f.read()
        if self.compiled:
            self.offset = self.base.index(MARK) + len(MARK)

    @property
    def probe(self):
        if self.compiled:
            return None
        table = self._fake

        def fake_probe(spec, realpath, timeout):
            line, code = table[_digest(realpath)]
            return ProbeResult(line.strip().splitlines()[0] if line.strip() else "", code, 0)

        return fake_probe

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def dir(self, name: str | None = None) -> str:
        d = os.path.join(self.root, name or f"d{next(self._n)}")
        os.makedirs(d, exist_ok=True)
        return d

    def content(self, line: str, *, code: int = 0, sleep_ms: int = 0, dump: str = "", stderr: str = "",
                nonce: str | None = None) -> bytes:
        nonce = nonce or f"n{next(self._n)}"
        if not self.compiled:
            return self.base + f"\0{nonce}\0".encode()
        cfg = SEP.join([str(code), str(sleep_ms), dump, line + "\n" if line else "", stderr, nonce]).encode()
        if len(cfg) > CFG_LEN:
            raise ValueError("fixture config too long")
        cfg = cfg + b"\0" * (CFG_LEN - len(cfg))
        return self.base[: self.offset] + cfg + self.base[self.offset + CFG_LEN:]

    def write(self, path: str, line: str, **kw) -> str:
        data = self.content(line, **kw)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o755)
        os.replace(tmp, path)
        if not self.compiled:
            self._fake[_digest(path)] = (line, kw.get("code", 0))
        return path

    def make(self, name: str, line: str, *, where: str | None = None, **kw) -> str:
        return self.write(os.path.join(where or self.dir(), name), line, **kw)

    def script(self, path: str, text: str = "#!/bin/sh\necho 2.1.318 (Claude Code)\n") -> str:
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return path


def _digest(path: str) -> str:
    import hashlib

    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()
