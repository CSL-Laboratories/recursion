#!/usr/bin/env python3
"""Rewrite the hash-locked CI requirements files and their record from PyPI.

Run from the repository root (it needs the network; the check does not):

    python -X utf8 -B tools/release/relock.py

For each file in LOCKED it reads the pins (name==version, an optional marker; any hashes are
dropped and fetched again), asks PyPI's JSON API for every file published for that exact
version, and writes the file back with one --hash per published file, keeping the file's
leading comment block. It writes tools/release/ci-lock.json, which names the file behind
every hash, the release's Requires-Python and its dependencies, and then runs the check in
tools/release/hash_lock.py. It never changes a version: to move a pin, edit its version (or
add the pin a new dependency needs) and run this again.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hash_lock  # noqa: E402

ROOT = hash_lock.ROOT
LOCKED = ("requirements-ci.txt", "tools/release/requirements-ci-314t.txt")
INDEX = "https://pypi.org/pypi/{name}/{version}/json"
ABOUT = ("Written by tools/release/relock.py from PyPI's JSON API; read by tools/release/hash_lock.py. "
         "Each pin lists every file PyPI publishes for that version with its sha256; requires_dist "
         "leaves out the dependencies of extras, which CI never installs.")


def read_pins(text: str) -> list[tuple[str, str, str | None]]:
    pins = []
    for number, line in hash_lock.logical_lines(text):
        rest = re.sub(r"\s+", " ", hash_lock.HASH_TOKEN.sub(" ", line)).strip()
        match = hash_lock.PIN.fullmatch(rest)
        if not match:
            raise hash_lock.LockError(f"line {number}: not an exact pin: {rest!r}")
        pins.append((match.group(1), match.group(2), match.group(3)))
    return pins


def header(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            out.append(line.rstrip())
        else:
            break
    while out and not out[-1]:
        out.pop()
    return out


def fetch(name: str, version: str) -> dict:
    request = urllib.request.Request(INDEX.format(name=name, version=version),
                                     headers={"User-Agent": "recursion-relock", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def lock_pin(name: str, version: str, marker: str | None) -> dict:
    data = fetch(name, version)
    if data["info"]["version"] != version:
        raise hash_lock.LockError(f"PyPI answered {data['info']['version']} for {name}=={version}")
    dists = []
    for item in data["urls"]:
        if item.get("yanked"):
            raise hash_lock.LockError(f"{name}=={version}: {item['filename']} is yanked")
        digest = item["digests"]["sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise hash_lock.LockError(f"{item['filename']}: bad sha256 {digest!r}")
        dists.append({"filename": item["filename"], "sha256": digest,
                      "requires_python": item.get("requires_python") or ""})
    if not dists:
        raise hash_lock.LockError(f"{name}=={version}: PyPI lists no files")
    requires = [r for r in (data["info"].get("requires_dist") or []) if "extra" not in r.partition(";")[2]]
    return {"version": version, "marker": marker,
            "requires_python": data["info"].get("requires_python") or "",
            "requires_dist": sorted(requires),
            "dists": sorted(dists, key=lambda d: d["filename"])}


def render(head: list[str], pins: dict[str, tuple[str, dict]]) -> str:
    lines = list(head)
    for canonical_name in sorted(pins):
        raw_name, entry = pins[canonical_name]
        spec = f"{raw_name}=={entry['version']}" + (f" ; {entry['marker']}" if entry["marker"] else "")
        hashes = sorted({d["sha256"] for d in entry["dists"]})
        lines.append(spec + " \\")
        lines += [f"    --hash=sha256:{h} \\" for h in hashes[:-1]]
        lines.append(f"    --hash=sha256:{hashes[-1]}")
    return "\n".join(lines) + "\n"


def main() -> int:
    if Path.cwd().resolve() != ROOT.resolve():
        print(f"run from the repository root ({ROOT})", file=sys.stderr)
        return 2
    record = {"format": hash_lock.RECORD_FORMAT, "about": ABOUT, "files": {}}
    for rel in LOCKED:
        path = ROOT / rel
        text = path.read_text(encoding="utf-8")
        pins = {}
        for raw_name, version, marker in read_pins(text):
            entry = lock_pin(raw_name, version, marker)
            pins[hash_lock.canonical(raw_name)] = (raw_name, entry)
            print(f"{rel}: {raw_name}=={version}: {len(entry['dists'])} files")
        path.write_text(render(header(text), pins), encoding="utf-8", newline="\n")
        record["files"][rel] = {"pins": {name: entry for name, (_, entry) in sorted(pins.items())}}
    (ROOT / hash_lock.RECORD).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n",
                                         encoding="utf-8", newline="\n")
    return hash_lock.main()


if __name__ == "__main__":
    raise SystemExit(main())
