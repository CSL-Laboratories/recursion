"""See whether git and the configured provider CLIs are usable.

Sign-in probes are local. Command output and credential file contents are
not returned. Claude Code's ``auth status`` can exit 1 while still printing
``{"loggedIn": true}``. That boolean decides. The exit code is only the
fallback when the output has no ``loggedIn`` field.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from scripts.v3.harness.providers import adapters


_PROBE_TIMEOUT = 10
_CLAUDE_METHODS = {"none", "claude.ai", "oauth_token", "api_key", "api_key_helper", "third_party"}
_SECRET_ENV = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization|credential)")
_TOKEN_RE = re.compile(r"(?i)(?:sk-[A-Za-z0-9]{8,}|xai-[A-Za-z0-9]{8,}|bearer\s+[A-Za-z0-9._-]{12,})")


def resolve_executable(
    name: str,
    *,
    path: str | None = None,
    platform: str | None = None,
) -> Path | None:
    """Return the first PATH match for ``name``, including Windows shims."""

    if not name:
        return None
    direct = Path(name)
    if direct.is_absolute():
        return direct if direct.is_file() else None
    if "/" in name or "\\" in name:
        return None
    system = sys.platform if platform is None else platform
    win32 = system == "win32"
    path_value = os.environ.get("PATH", "") if path is None else path
    suffixes = ("",) if not win32 else ("", ".exe", ".cmd", ".bat", ".ps1")
    for directory_text in path_value.split(os.pathsep):
        if not directory_text:
            continue
        directory = Path(directory_text)
        found = _find_named(directory, name, suffixes, win32=win32)
        if found is not None:
            return found
    return None


def discover_installed(
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    path: str | None = None,
) -> list[str]:
    """Return each offline fixture plus every provider whose command resolves."""

    env = os.environ if environ is None else environ
    search = env.get("PATH", "") if path is None else path
    found = []
    for name, cls in adapters().items():
        if getattr(cls, "OFFLINE", False) or resolve_executable(
            cls.STATIC_ARGV[0], path=search, platform=platform
        ) is not None:
            found.append(name)
    return found


def diagnose(
    config: dict,
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
    path: str | None = None,
    run=None,
    git_run=None,
) -> dict:
    """Report git and one row per configured provider.

    ``ok`` is true when at least one enabled provider is usable. An offline
    fixture is usable whenever it is enabled. A disabled provider is not probed.
    """

    env = os.environ if environ is None else environ
    system = sys.platform if platform is None else platform
    root = Path.home() if home is None else home
    search = env.get("PATH", "") if path is None else path
    invoke = _default_run if run is None else run
    git_invoke = _default_run if git_run is None else git_run
    rows = [
        _provider_row(item, platform=system, home=root, path=search, run=invoke, environ=env)
        for item in config["providers"]
    ]
    report = {
        "git": _git_row(platform=system, run=git_invoke),
        "ok": any(row["usable"] for row in rows),
        "providers": rows,
    }
    return _scrub(report, _secret_values(env))


def _provider_row(item: dict, *, platform: str, home: Path, path: str, run, environ) -> dict:
    name = item["name"]
    command = item["argv"][0]
    enabled = bool(item["enabled"])
    cls = adapters().get(name)
    if cls is not None and getattr(cls, "OFFLINE", False):
        return _row(
            name=name,
            command=command,
            enabled=enabled,
            installed=True,
            resolved=None,
            version=None,
            signed_in="not-applicable",
            auth_method=None,
            usable=enabled,
        )
    resolved = resolve_executable(command, path=path, platform=platform)
    installed = resolved is not None
    if not enabled:
        return _row(
            name=name,
            command=command,
            enabled=False,
            installed=installed,
            resolved=None if resolved is None else str(resolved),
            version=None,
            signed_in="skipped",
            auth_method=None,
            usable=False,
        )
    version = _version(run, resolved) if resolved is not None else None
    signed_in, auth_method = _signed_in(name, resolved, home=home, run=run, environ=environ)
    return _row(
        name=name,
        command=command,
        enabled=True,
        installed=installed,
        resolved=None if resolved is None else str(resolved),
        version=version,
        signed_in=signed_in,
        auth_method=auth_method,
        usable=installed and signed_in != "signed-out",
    )


def _row(**fields) -> dict:
    return fields


def _signed_in(
    name: str, resolved: Path | None, *, home: Path, run, environ: Mapping[str, str]
) -> tuple[str, str | None]:
    if name == "grok":
        state, method = _file_auth(home / ".grok" / "auth.json")
        if state == "signed-out" and str(environ.get("XAI_API_KEY", "")).strip():
            return "signed-in", "env"
        return state, method
    if resolved is None:
        return "unknown", None
    if name == "claude":
        return _claude_auth(run, resolved)
    if name == "codex":
        return _codex_auth(run, resolved)
    return "unknown", None


def _file_auth(path: Path) -> tuple[str, str | None]:
    try:
        if path.is_file() and path.stat().st_size > 0:
            return "signed-in", "cached"
    except OSError:
        return "unknown", None
    return "signed-out", None


def _claude_auth(run, resolved: Path) -> tuple[str, str | None]:
    completed = _capture(run, [str(resolved), "auth", "status"])
    if completed is None:
        return "unknown", None
    payload = _json_object(completed.stdout or "") or _json_object(completed.stderr or "")
    if isinstance(payload, dict) and isinstance(payload.get("loggedIn"), bool):
        method = _claude_method(payload)
        if payload["loggedIn"]:
            return "signed-in", method or "status"
        return "signed-out", method or "none"
    method = _claude_method(payload) if isinstance(payload, dict) else None
    if method is not None and method != "none":
        return "signed-in", method
    if completed.returncode == 0:
        return "signed-in", method or "status"
    if completed.returncode == 1:
        return "signed-out", method
    return "unknown", None


def _claude_method(payload: dict) -> str | None:
    method = payload.get("authMethod")
    if isinstance(method, str) and method in _CLAUDE_METHODS:
        return method
    return None


def _codex_auth(run, resolved: Path) -> tuple[str, str | None]:
    completed = _capture(run, [str(resolved), "login", "status"])
    if completed is None:
        return "unknown", None
    if completed.returncode == 0:
        return "signed-in", "login-status"
    if completed.returncode == 1:
        return "signed-out", "login-status"
    return "unknown", None


def _version(run, resolved: Path) -> str | None:
    completed = _capture(run, [str(resolved), "--version"])
    if completed is None or completed.returncode != 0:
        return None
    for line in (completed.stdout or "").splitlines():
        text = line.strip()
        if text:
            return text[:200]
    return None


def _git_row(*, platform: str, run) -> dict:
    del platform
    completed = _capture(run, ["git", "--version"])
    if completed is None or completed.returncode != 0:
        return {"available": False, "version": None}
    version = None
    for line in (completed.stdout or "").splitlines():
        text = line.strip()
        if text:
            version = text[:200]
            break
    return {"available": True, "version": version}


def _capture(run, argv: list[str]):
    try:
        completed = run(list(argv), _PROBE_TIMEOUT)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if not isinstance(completed, subprocess.CompletedProcess):
        return None
    return completed


def _default_run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
    )


def _find_named(directory: Path, name: str, suffixes: tuple[str, ...], *, win32: bool) -> Path | None:
    if not win32:
        for suffix in suffixes:
            candidate = directory / f"{name}{suffix}"
            if candidate.is_file():
                return candidate
        return None
    if not directory.is_dir():
        return None
    wanted = {f"{name}{suffix}".casefold() for suffix in suffixes}
    try:
        entries = list(directory.iterdir())
    except OSError:
        return None
    for entry in entries:
        if entry.is_file() and entry.name.casefold() in wanted:
            return entry
    return None


def _json_object(text: str) -> dict | None:
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            payload = None
        else:
            if isinstance(payload, dict):
                return payload
    for line in reversed(text.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{") or not candidate.endswith("}"):
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _secret_values(environ: Mapping[str, str]) -> list[str]:
    values = []
    for key, value in environ.items():
        if isinstance(value, str) and len(value) >= 8 and _SECRET_ENV.search(key):
            values.append(value)
    values.sort(key=len, reverse=True)
    return values


def _scrub(value: object, secrets: list[str]) -> object:
    if isinstance(value, str):
        redacted = value
        for secret in secrets:
            if secret and secret in redacted:
                redacted = redacted.replace(secret, "[redacted]")
        return _TOKEN_RE.sub("[redacted]", redacted)
    if isinstance(value, list):
        return [_scrub(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _scrub(item, secrets) for key, item in value.items()}
    return value
