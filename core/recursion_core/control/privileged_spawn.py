"""F21 / B7: privileged spawn path detection and measured backends (CSL-734).

PRODUCT.md at csl-research ``055cc7a5c793b7135e5d0565dc4198cf96230531`` (SHA-256
``e64783152f6d67d4ede774dc41d112fe46c2d7536d53cb2c04e71a3566044daf``): engines
run under a separate engine OS user (grill Q17). Where admin rights are refused,
managed login folders are the fallback. If the engine-user test on a privileged
Linux machine (fleet briefing F21) has not fully run by Nov 11, that fallback
ships as the beta default (Lucas, Nov 7: "Yes the fallback ships as beta").
Section 11.5: same-uid attackers beat every detection and prevention arm through
``/proc`` and ptrace, 20/20; only running engines under a separate uid (A3 arm
P3) closes these. The F21 same-uid channels named in the a3-prevention-fixes
record are ``/proc/<pid>/root``, ptrace, and an absolute-path store read.

This module detects spawn backends WITHOUT mutating system config: it does not
install a setuid helper, edit sudoers, edit polkit rules, create a permanent
system user, or write to ``/etc``. It measures p50/p95 spawn overhead (n>=10)
of every backend that can already spawn another *host* uid, runs the three F21
same-uid probes under each, and selects the managed-login-folder fallback as
beta when no such backend is available.

Selection follows O19: while any F21 engine-user item below is unmeasured, the
managed-login-folder fallback stays the beta default even when an other-uid
backend works; that backend is reported as ``SelectedPath.candidate``, not
selected. A backend is selectable only when all three F21 channels are blocked
on every attempt. ``nobody`` and ``daemon`` are shared system accounts: other
processes run under them, so they are same-uid attackers against an engine
there, and ``nobody`` has no home to hold logins. They are measurement
stand-ins only and are never selected as the engine user.

The dedicated engine user is named by the installer, not by this module:
``detect_backends(engine_user=...)`` or ``RECURSION_ENGINE_USER``. Detection
tries that user first and only then the stand-ins, so a host where sudo or
systemd-run reaches the engine user reports it as ``SelectedPath.candidate``
(and selects it once F21 has run). With no engine user configured, detection
can only reach stand-ins and selection is always the beta fallback.

The probes target the engine process itself: a process whose real,
effective, saved and filesystem uids all equal the backend's target uid (for
systemd-run, the transient unit's MainPID). They never fall back to the
launcher. A probe that cannot run is counted as an error, never as blocked.
Only ``EACCES`` or ``EPERM`` counts as blocked; any other errno (for example
``ENOENT`` or ``ESRCH`` when the engine exited first) is an error.

Unmeasured here (need a founder Linux box, or would require creating a real
other-uid home and signing in):

* creating the engine user at install
* Claude Code sign-in into that home surviving a reboot
* Codex sign-in into that home surviving a reboot
* live Secret Service / keyring behaviour for a headless user

The intended BYOK storage *decision* when Secret Service fails closed is
encoded from PRODUCT.md A44 / section 1 and can be unit-tested without
touching real keys: model-provider keys may use an owner-only file; broker
and venue keys stay keyring-only and therefore fail closed.

No provider or venue calls. No Claude Code or Codex binary is launched. No
credentials are copied or written.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import math
import os
import pwd
import signal
import stat
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

PRODUCT_MD_COMMIT = "055cc7a5c793b7135e5d0565dc4198cf96230531"
PRODUCT_MD_SHA256 = "e64783152f6d67d4ede774dc41d112fe46c2d7536d53cb2c04e71a3566044daf"

# Honest unmeasured items. None means "not measured on this path".
ENGINE_USER_CREATED_AT_INSTALL: None = None
CLAUDE_CODE_SIGNIN_SURVIVES_REBOOT: None = None
CODEX_SIGNIN_SURVIVES_REBOOT: None = None
HEADLESS_SECRET_SERVICE_LIVE: None = None

SETUID_HELPER_CANDIDATES = (
    "/usr/local/libexec/recursion-spawn-as",
    "/usr/libexec/recursion-spawn-as",
    "/usr/local/bin/recursion-spawn-as",
)
HELPER_ENV = "RECURSION_SPAWN_HELPER"
# Names the dedicated engine user detection should try first. No default:
# the name and its creation at install are F21 install work.
ENGINE_USER_ENV = "RECURSION_ENGINE_USER"
# Measurement stand-ins for the engine user. Never selected (see
# SHARED_SYSTEM_ACCOUNTS): they only show whether sudo reaches another uid.
SUDO_TARGET_CANDIDATES = ("nobody", "daemon")
# Shared system accounts that other processes also run as. An engine under
# one of these uids shares it with them, so the F21 same-uid channels stay
# open to those processes. 65534 is the kernel overflow uid (nobody).
SHARED_SYSTEM_ACCOUNTS: frozenset[str] = frozenset({"nobody", "daemon"})
SHARED_SYSTEM_UIDS: frozenset[int] = frozenset({65534})
DEFAULT_N = 20
# Commands every measured backend must be able to run as the target user.
# Overhead times ``true``. Process probes launch ``sleep 8`` (not a stand-in
# such as ``sleep 0``). The store writer runs ``python3 -c`` with
# ``store_writer_script`` (not ``python3 -c pass``). Sudoers rules are
# per-command, per-target and per-argument, so detection checks those
# launches rather than shorter aliases.
SLEEP_PROBE_ARGV: tuple[str, ...] = ("sleep", "8")
OVERHEAD_ARGV: tuple[str, ...] = ("true",)
SUDO_REQUIRED_COMMANDS: tuple[tuple[str, ...], ...] = (
    OVERHEAD_ARGV,
    SLEEP_PROBE_ARGV,
)
# Backends ``spawn_command`` can actually launch. Detection may still report
# others (for example a working subuid map) but they are never selected.
SPAWNABLE_BACKENDS: frozenset[str] = frozenset({"sudo", "systemd_run"})
PTRACE_ATTACH = 16
PTRACE_DETACH = 17
# Only a permission denial counts as a blocked F21 channel. Any other errno
# (the target exited or was reaped: ENOENT, ESRCH; or EIO, ENOTDIR, ...) means
# the channel was not tested on that attempt, so it is an error.
BLOCKED_ERRNOS: frozenset[int] = frozenset({errno.EACCES, errno.EPERM})

BackendName = Literal["setuid_helper", "sudo", "systemd_run", "subuid_userns"]
PathName = Literal[
    "setuid_helper", "sudo", "systemd_run", "subuid_userns", "managed_login_folder"
]
ProbeName = Literal["proc_pid_root", "ptrace", "abs_store_read"]
# True = blocked, False = open, None = the probe could not run (not a result).
ProbeOutcome = tuple[bool | None, str]
ByokKind = Literal["model_provider", "broker", "venue"]
ByokStorage = Literal["owner_only_file", "fail_closed"]

RunFn = Callable[[Sequence[str], float], "ProcResult"]
StatFn = Callable[[str], os.stat_result]


@dataclass(frozen=True, slots=True)
class ProcResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class Backend:
    name: BackendName
    available: bool
    changes_host_uid: bool
    reason: str
    target_user: str | None = None
    target_uid: int | None = None
    helper_path: str | None = None


@dataclass(frozen=True, slots=True)
class SelectedPath:
    name: PathName
    beta: bool
    reason: str
    backend: Backend | None = None
    # A working engine-user backend that is not selected because F21 has not
    # fully run (O19). Only set when ``name`` is ``managed_login_folder``.
    candidate: Backend | None = None


@dataclass(frozen=True, slots=True)
class Overhead:
    n: int
    p50_s: float
    p95_s: float
    min_s: float
    max_s: float
    command: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProbeSummary:
    """``n`` attempts = ``blocked_count`` + open + ``error_count``.

    ``targets`` names the processes or files actually probed, for example
    ``sleep uid=65534 x20``, so a reader can check which uid was measured.
    """

    name: ProbeName
    n: int
    blocked_count: int
    detail: str
    error_count: int = 0
    targets: str = ""
    # Attempts whose error was a launch failure (engine process never appeared,
    # store writer failed), counted per attempt so a later success cannot hide
    # one; ``launch_failure_detail`` keeps the first such diagnostic.
    launch_failure_count: int = 0
    launch_failure_detail: str = ""

    @property
    def open_count(self) -> int:
        return self.n - self.blocked_count - self.error_count

    @property
    def blocked(self) -> bool:
        return self.n > 0 and self.error_count == 0 and self.blocked_count == self.n


@dataclass(frozen=True, slots=True)
class HostFacts:
    uid: int
    username: str
    sudo_n_true: bool
    subuid_lines: tuple[str, ...]
    subgid_lines: tuple[str, ...]
    newuidmap_path: str | None
    newuidmap_is_setuid_root: bool
    user_ns_max: int | None
    unshare_user_works: bool
    unshare_host_uid: int | None
    systemd_is_pid1: bool


@dataclass(frozen=True, slots=True)
class Evaluation:
    product_commit: str
    product_sha256: str
    facts: HostFacts
    backends: tuple[Backend, ...]
    selected: SelectedPath
    overhead: dict[str, Overhead]
    probes: dict[str, tuple[ProbeSummary, ...]]
    unmeasured: dict[str, None]
    errors: dict[str, str]


def byok_storage_when_secret_service_fails_closed(kind: str) -> ByokStorage:
    """Intended PRODUCT.md A44 / section 1 decision; not a live keyring probe.

    Live Secret Service behaviour on a headless engine user remains
    ``HEADLESS_SECRET_SERVICE_LIVE`` (unmeasured).
    """

    if kind == "model_provider":
        return "owner_only_file"
    if kind in {"broker", "venue"}:
        return "fail_closed"
    raise ValueError(f"unknown BYOK kind: {kind!r}")


def percentile(sorted_samples: Sequence[float], p: float) -> float:
    """Nearest-rank percentile; ``p`` is 0..100. ``sorted_samples`` must be non-empty."""

    if not sorted_samples:
        raise ValueError("percentile() requires at least one sample")
    if not 0 < p <= 100:
        raise ValueError(f"percentile p must be in (0, 100], got {p!r}")
    rank = max(1, math.ceil(p / 100.0 * len(sorted_samples)))
    return sorted_samples[rank - 1]


def can_spawn(backend: Backend) -> bool:
    """True when ``spawn_command`` can launch an engine through ``backend``."""

    if backend.name not in SPAWNABLE_BACKENDS:
        return False
    try:
        spawn_command(backend, ("true",))
    except ValueError:
        return False
    return True


def f21_open_items() -> tuple[str, ...]:
    """F21 engine-user items not yet measured (``None``). Empty once F21 has run."""

    items = {
        "engine_user_created_at_install": ENGINE_USER_CREATED_AT_INSTALL,
        "claude_code_signin_survives_reboot": CLAUDE_CODE_SIGNIN_SURVIVES_REBOOT,
        "codex_signin_survives_reboot": CODEX_SIGNIN_SURVIVES_REBOOT,
        "headless_secret_service_live": HEADLESS_SECRET_SERVICE_LIVE,
    }
    return tuple(name for name, value in items.items() if value is None)


def is_shared_system_account(backend: Backend) -> bool:
    """True when ``backend`` targets a shared system account such as ``nobody``."""

    return (
        backend.target_user in SHARED_SYSTEM_ACCOUNTS
        or backend.target_uid in SHARED_SYSTEM_UIDS
    )


def _normalised_uid_token(user: str) -> int | None:
    """Uid spelled by a sudo ``#uid`` token or a systemd numeric uid.

    ``#00`` and ``00`` are uid 0. A username returns ``None``.
    """

    text = user[1:] if user.startswith("#") else user
    if not text.isascii() or not text.isdigit():
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _is_root_user_token(user: str | None) -> bool:
    """True for root, any numeric spelling of uid 0, or a local uid-0 account.

    Configured names are resolved with ``pwd`` so an alias such as ``toor``
    is refused before a launch. The username match for the literal is the
    exact token ``root``; ``ROOT`` is not that token when it is not uid 0.
    """

    if not user:
        return False
    uid = _normalised_uid_token(user)
    if uid is not None:
        return uid == 0
    if user == "root":
        return True
    try:
        return pwd.getpwnam(user).pw_uid == 0
    except KeyError:
        return False


def is_root_target(backend: Backend) -> bool:
    """True when ``backend`` targets root; root is never the engine user.

    ``target_user`` of ``"0"``, ``"#0"``, ``"00"`` or ``"#00"`` is uid 0 even
    when ``target_uid`` was not filled in.
    """

    return backend.target_uid == 0 or _is_root_user_token(backend.target_user)


def select_path(
    backends: Sequence[Backend],
    *,
    f21_open: Sequence[str] | None = None,
) -> SelectedPath:
    """Pick the first spawnable engine-user backend once F21 has run; else beta fallback.

    ``f21_open`` lists unmeasured F21 items (default: ``f21_open_items()``).
    While it is non-empty the managed-login-folder fallback is the beta
    default (O19) and a working engine-user backend is only the ``candidate``.
    Backends that target root or a shared system account are never selected.
    """

    open_items = tuple(f21_open_items() if f21_open is None else f21_open)
    order: tuple[BackendName, ...] = (
        "setuid_helper",
        "sudo",
        "systemd_run",
        "subuid_userns",
    )
    by_name = {b.name: b for b in backends}
    stand_ins: list[str] = []
    candidate: Backend | None = None
    for name in order:
        backend = by_name.get(name)
        if not (
            backend is not None
            and backend.available
            and backend.changes_host_uid
            and can_spawn(backend)
        ):
            continue
        if is_root_target(backend):
            continue
        if is_shared_system_account(backend):
            stand_ins.append(f"{name} as {backend.target_user or backend.target_uid}")
            continue
        candidate = backend
        break
    if candidate is not None and not open_items:
        return SelectedPath(
            candidate.name,
            False,
            f"{candidate.name} can spawn the engine user without mutating system "
            "config, and F21 has run",
            candidate,
        )
    why: list[str] = []
    if candidate is not None:
        why.append(
            f"{candidate.name} can spawn the engine user but F21 has not fully run "
            f"(unmeasured: {', '.join(open_items)})"
        )
    else:
        why.append(
            "no spawnable engine-user backend is available without installing a "
            "helper or editing sudoers/polkit"
        )
    if stand_ins:
        why.append(
            "shared system accounts are measurement stand-ins, never the engine "
            f"user: {', '.join(stand_ins)}"
        )
    return SelectedPath(
        "managed_login_folder",
        True,
        f"{'; '.join(why)}; managed-login-folder fallback ships as beta "
        '(Lucas, Nov 7: "Yes the fallback ships as beta")',
        None,
        candidate,
    )


def _default_run(cmd: Sequence[str], timeout: float) -> ProcResult:
    try:
        proc = subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        return ProcResult(127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        return ProcResult(124, exc.stdout or "", exc.stderr or "timeout")
    return ProcResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def _read_text(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _parse_uid_output(text: str) -> int | None:
    stripped = (text or "").strip().splitlines()
    if not stripped:
        return None
    token = stripped[-1].strip()
    if token.isdigit() or (token.startswith("-") and token[1:].isdigit()):
        return int(token)
    # ``id`` full form: uid=65534(nobody) ...
    if token.startswith("uid="):
        body = token.split()[0][4:]
        body = body.split("(", 1)[0]
        if body.isdigit():
            return int(body)
    return None


def _helper_paths(env: Mapping[str, str] | None) -> tuple[str, ...]:
    extra: list[str] = []
    source = env if env is not None else os.environ
    configured = source.get(HELPER_ENV, "").strip()
    if configured:
        extra.append(configured)
    return tuple(extra + list(SETUID_HELPER_CANDIDATES))


def _configured_engine_user(
    engine_user: str | None, env: Mapping[str, str] | None
) -> str | None:
    """``engine_user`` if given, else ``RECURSION_ENGINE_USER``; ``None`` when unset."""

    if engine_user is None:
        source = env if env is not None else os.environ
        engine_user = source.get(ENGINE_USER_ENV, "")
    engine_user = engine_user.strip()
    return engine_user or None


def _is_setuid_root(st: os.stat_result) -> bool:
    return bool(st.st_mode & stat.S_ISUID) and st.st_uid == 0


# How many times a detach may be restarted after EINTR, and how many times an
# ESRCH may be retried once the stop has already been observed. A further
# blocking waitpid would wait forever: that stop was already consumed.
_PTRACE_DETACH_EINTR = 8
_PTRACE_DETACH_RETRIES = 2


def _opath_flag() -> int:
    """``O_PATH``, or ``OSError`` where the platform has no such flag.

    Read at call time. Windows and macOS have no ``O_PATH``, and evaluating it
    while the module is imported makes test collection fail there.
    """

    flag = getattr(os, "O_PATH", None)
    if not isinstance(flag, int):
        raise OSError(errno.ENOTSUP, "O_PATH is not available")
    return flag


def _dir_traversal_flags() -> int:
    """``O_PATH|O_DIRECTORY|O_NOFOLLOW`` for one directory step.

    ``O_PATH`` yields a dir fd through a search-only (0111) ancestor.
    ``O_RDONLY`` on that directory returns ``EACCES`` even when create and
    read of the leaf would succeed. ``O_NOFOLLOW`` applies at every component.
    """

    return _opath_flag() | os.O_DIRECTORY | os.O_NOFOLLOW


def _open_leaf_nofollow(path: str, flags: int, mode: int = 0o777) -> tuple[int, int, str]:
    """Open the last component via a directory fd.

    Each directory component is opened with ``O_PATH|O_DIRECTORY|O_NOFOLLOW``,
    then the leaf with ``O_NOFOLLOW``. ``O_NOFOLLOW`` on a single ``open`` of
    the full path only covers the last component. Returns ``(leaf_fd, dir_fd, name)``.
    The caller closes both descriptors. Raises ``OSError`` when ``O_PATH`` is
    not available.
    """

    traversal = _dir_traversal_flags()
    absolute = os.path.isabs(path)
    parts = [part for part in path.split(os.sep) if part]
    if not parts:
        raise OSError(errno.ENOENT, os.strerror(errno.ENOENT), path)
    name = parts.pop()
    cur = os.open(os.sep if absolute else ".", traversal)
    try:
        for part in parts:
            nxt = os.open(part, traversal, dir_fd=cur)
            os.close(cur)
            cur = nxt
        leaf = os.open(name, flags | os.O_NOFOLLOW, mode, dir_fd=cur)
    except BaseException:
        os.close(cur)
        raise
    return leaf, cur, name


def _fstat_nofollow(path: str) -> os.stat_result:
    """Stat one inode without following a symlink at any component.

    Ancestors are walked with directory fds. The leaf is opened
    ``O_PATH|O_NOFOLLOW`` and that fd is what ``fstat`` reports.
    """

    fd, dirfd, _name = _open_leaf_nofollow(path, _opath_flag())
    try:
        return os.fstat(fd)
    finally:
        os.close(fd)
        os.close(dirfd)


def _inspect_helpers(
    paths: Sequence[str],
    stat_fn: StatFn,
) -> tuple[str | None, str]:
    notes: list[str] = []
    found: str | None = None
    # The default ``os.stat`` follows a symlink swapped in after ``lstat``,
    # including a symlinked ancestor. The walk opens each directory
    # ``O_NOFOLLOW`` and fstats the leaf fd. Injected stats stay available
    # for tests that have no real file.
    follow_free = stat_fn is os.stat
    for path in paths:
        try:
            st = _fstat_nofollow(path) if follow_free else stat_fn(path)
        except OSError as exc:
            # ELOOP: O_NOFOLLOW stopped on a symlink. ENOTDIR: an ancestor
            # opened O_DIRECTORY|O_NOFOLLOW was a symlink (or not a directory).
            if follow_free and exc.errno in (errno.ELOOP, errno.ENOTDIR):
                notes.append(f"{path} is a symlink; not a setuid helper")
            else:
                notes.append(f"{path} absent")
            continue
        if stat.S_ISLNK(st.st_mode):
            notes.append(f"{path} is a symlink; not a setuid helper")
            continue
        mode = stat.filemode(st.st_mode)
        setuid_root = _is_setuid_root(st)
        notes.append(
            f"{path} exists mode={mode} owner_uid={st.st_uid} setuid_root={setuid_root}"
        )
        if setuid_root and found is None:
            found = path
    if found is None:
        notes.append("not installing a setuid helper")
    else:
        notes.append(
            "found a setuid-root helper; invocation protocol is not recorded, "
            "so it is not executed"
        )
    return found, "; ".join(notes)


def _sudo_n_true(run_fn: RunFn) -> bool:
    result = run_fn(("sudo", "-n", "true"), 8.0)
    return result.returncode == 0


def store_writer_script(path: str) -> str:
    """Python ``-c`` body used by the absolute-path store probe (and sudo detection).

    The leaf is created through a directory fd. Ancestors are opened
    ``O_PATH|O_DIRECTORY|O_NOFOLLOW`` so a search-only directory still works,
    and the mode is set with ``fchmod`` on the leaf fd before it is closed.
    """

    quoted = repr(str(path))
    return (
        "import os; p=" + quoted + "; "
        "abs=os.path.isabs(p); parts=[part for part in p.split(os.sep) if part]; "
        "name=parts.pop(); "
        "cur=os.open(os.sep if abs else '.', os.O_PATH|os.O_DIRECTORY|os.O_NOFOLLOW); "
        "\n"
        "for part in parts:\n"
        " nxt=os.open(part, os.O_PATH|os.O_DIRECTORY|os.O_NOFOLLOW, dir_fd=cur); os.close(cur); cur=nxt\n"
        "fd=os.open(name, os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_WRONLY, 0o600, dir_fd=cur); "
        "os.write(fd, b'store-bytes'); os.fchmod(fd, 0o600); os.close(fd); os.close(cur)"
    )


def _launch_timeout(argv: Sequence[str]) -> float:
    """Timeout for one detection launch: 8 s, plus the sleep itself for ``sleep N``.

    The launch is the exact measured command (``sleep 8``); only the runner's
    timeout grows, so an authorised sleep is not reported as rc 124.
    """

    base = 8.0
    if len(argv) == 2 and argv[0] == "sleep":
        try:
            return base + float(argv[1])
        except ValueError:
            return base
    return base


def _probe_sudo_target(
    run_fn: RunFn,
    our_uid: int,
    candidates: Sequence[str] = SUDO_TARGET_CANDIDATES,
) -> tuple[str | None, int | None, str]:
    """First candidate sudo reaches as a non-root other uid for every launch
    the measured path actually runs. Independent of ``sudo -n true`` (root)."""

    notes: list[str] = []
    for user in candidates:
        if _is_root_user_token(user):
            notes.append(
                f"sudo -n -u {user} refused before launch; "
                "rejected: engine user must not be root or the calling uid"
            )
            continue
        result = run_fn(("sudo", "-n", "-u", user, "--", "id", "-u"), 8.0)
        uid = _parse_uid_output(result.stdout)
        if result.returncode != 0 or uid is None or uid == our_uid or uid == 0:
            err = (result.stderr or result.stdout).strip().replace("\n", " ")
            notes.append(
                f"sudo -n -u {user} -- id -u rc={result.returncode} uid={uid} err={err[:160]}"
            )
            continue
        notes.append(f"sudo -n -u {user} -- id -u -> {uid}")
        denied = None
        with tempfile.TemporaryDirectory(prefix="csl734-sudo-probe-") as tmp:
            # The writer runs as the target user: like the probe store
            # directory, let another uid create its file (sticky, 1777).
            # chmod after the sleep launches so a local user cannot spend
            # those eight seconds planting a file the writer will open.
            # Unpredictable name: a pre-created probe.secret (r4507085798)
            # must not be the path the target-user writer opens.
            for argv in SUDO_REQUIRED_COMMANDS:
                check = run_fn(
                    ("sudo", "-n", "-u", user, "--", *argv), _launch_timeout(argv)
                )
                if check.returncode != 0:
                    err = (check.stderr or check.stdout).strip().replace("\n", " ")
                    shown = " ".join(argv)
                    denied = (
                        f"sudo -n -u {user} -- {shown} "
                        f"rc={check.returncode} err={err[:160]}"
                    )
                    break
            else:
                os.chmod(tmp, 0o1777)
                store = str(Path(tmp) / f"probe-{os.getpid()}-{time.time_ns()}.secret")
                argv = ("python3", "-c", store_writer_script(store))
                check = run_fn(
                    ("sudo", "-n", "-u", user, "--", *argv), _launch_timeout(argv)
                )
                if check.returncode != 0:
                    err = (check.stderr or check.stdout).strip().replace("\n", " ")
                    denied = (
                        f"sudo -n -u {user} -- python3 -c "
                        f"rc={check.returncode} err={err[:160]}"
                    )
        if denied is not None:
            notes.append(denied)
            continue
        notes.append(
            "sudo -n -u "
            f"{user} runs true, sleep 8 and the store-writer python3 -c script"
        )
        return user, uid, "; ".join(notes)
    return None, None, "; ".join(notes) if notes else "no sudo target candidates"


def _probe_systemd_run(run_fn: RunFn, user: str, our_uid: int) -> tuple[bool, int | None, str]:
    if _is_root_user_token(user):
        return False, None, (
            f"systemd-run --uid={user} refused before launch; "
            "rejected: engine user must not be root or the calling uid"
        )
    result = run_fn(
        (
            "systemd-run",
            f"--uid={user}",
            "--wait",
            "--pipe",
            "--collect",
            "/usr/bin/id",
            "-u",
        ),
        8.0,
    )
    uid = _parse_uid_output(result.stdout)
    err = (result.stderr or "").strip().replace("\n", " ")
    # Like the sudo target: never root, never our own uid. Root passes every
    # F21 probe from uid 1000, so the gate cannot catch it later.
    ok = result.returncode == 0 and uid is not None and uid != our_uid and uid != 0
    reason = (
        f"systemd-run --uid={user} --wait --pipe --collect /usr/bin/id -u "
        f"rc={result.returncode} uid={uid}"
    )
    if err:
        reason = f"{reason} err={err[:240]}"
    if result.returncode == 0 and uid in (0, our_uid):
        reason = f"{reason}; rejected: engine user must not be root or the calling uid"
    return ok, uid if ok else None, reason


def _subid_lines(username: str, text: str | None) -> tuple[str, ...]:
    if not text:
        return ()
    prefix = f"{username}:"
    return tuple(
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#") and line.startswith(prefix)
    )


def _proc_field(pid: int, key: str) -> str | None:
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    prefix = f"{key}:"
    for line in status.splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip()
    return None


def host_uids_of(pid: int) -> tuple[int, ...] | None:
    """Real, effective, saved and filesystem uid of ``pid`` (``/proc`` Uid line)."""

    field = _proc_field(pid, "Uid")
    if not field:
        return None
    tokens = field.split()
    if not tokens or not all(token.isdigit() for token in tokens):
        return None
    return tuple(int(token) for token in tokens)


def host_uid_of(pid: int) -> int | None:
    uids = host_uids_of(pid)
    return uids[0] if uids else None


def _comm_of(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return "?"


def _children_of(pid: int) -> list[int]:
    try:
        raw = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="utf-8")
    except OSError:
        return []
    return [int(part) for part in raw.split() if part.isdigit()]


def find_target_uid_pid(
    root_pid: int,
    target_uid: int,
    *,
    timeout_s: float = 1.5,
    uids_fn: Callable[[int], tuple[int, ...] | None] = host_uids_of,
    children_fn: Callable[[int], list[int]] = _children_of,
) -> int | None:
    """Walk ``root_pid`` and its descendants for the engine process.

    Matches only a process whose real, effective, saved and filesystem uids
    all equal ``target_uid``, so a launcher such as sudo (real uid = caller,
    effective uid = 0) is never returned. ``None`` when nothing matches before
    the timeout; callers record that as an error and never probe another pid.
    """

    deadline = time.monotonic() + timeout_s
    while True:
        seen: set[int] = set()
        stack = [root_pid]
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            uids = uids_fn(pid)
            if uids and all(uid == target_uid for uid in uids):
                return pid
            stack.extend(children_fn(pid))
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.02)


def systemd_unit_main_pid(
    unit: str,
    target_uid: int,
    *,
    run_fn: RunFn,
    timeout_s: float = 3.0,
    uids_fn: Callable[[int], tuple[int, ...] | None] = host_uids_of,
) -> int | None:
    """MainPID of a transient systemd-run unit, if it runs entirely as ``target_uid``.

    The unit is forked by PID 1, not by the systemd-run client, so a walk of
    the client's descendants cannot find it.
    """

    deadline = time.monotonic() + timeout_s
    while True:
        result = run_fn(("systemctl", "show", "-p", "MainPID", "--value", unit), 5.0)
        token = (result.stdout or "").strip()
        if result.returncode == 0 and token.isdigit() and int(token) > 0:
            pid = int(token)
            uids = uids_fn(pid)
            if uids and all(uid == target_uid for uid in uids):
                return pid
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.05)


def _ptrace(request: int, pid: int) -> tuple[int, int]:
    libc_name = ctypes.util.find_library("c")
    if not libc_name:
        return -1, 0
    libc = ctypes.CDLL(libc_name, use_errno=True)
    libc.ptrace.argtypes = [
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    libc.ptrace.restype = ctypes.c_long
    ctypes.set_errno(0)
    rc = int(libc.ptrace(request, pid, None, None))
    return rc, ctypes.get_errno()


def probe_proc_pid_root(pid: int) -> ProbeOutcome:
    path = f"/proc/{pid}/root"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        detail = f"{type(exc).__name__}:{exc.errno}:{exc.strerror}"
        if exc.errno in BLOCKED_ERRNOS:
            return True, detail
        return None, f"not_denied {detail}"
    os.close(fd)
    return False, "opened"


def _await_ptrace_stop(pid: int) -> str:
    """Block until ``pid`` stops, exits, or is reaped.

    Returns ``stopped``, ``exited``, or ``reaped``. ``EINTR`` is retried.
    ``ChildProcessError`` and ``ECHILD`` mean the tracee is already gone.
    """

    while True:
        try:
            _pid, status = os.waitpid(pid, 0)
        except ChildProcessError:
            return "reaped"
        except OSError as exc:
            if exc.errno == errno.EINTR:
                continue
            if exc.errno == errno.ECHILD:
                return "reaped"
            raise
        if os.WIFSTOPPED(status):
            return "stopped"
        if os.WIFEXITED(status) or os.WIFSIGNALED(status):
            return "exited"


def _poll_ptrace(pid: int) -> str:
    """One non-blocking wait after the attach stop was already consumed.

    Returns ``pending`` (nothing new), ``stopped``, ``exited``, or ``reaped``.
    A blocking ``waitpid`` here waits forever: the stop is gone, and the
    tracee stays attached until detach succeeds.
    """

    try:
        waited, status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return "reaped"
    except OSError as exc:
        if exc.errno == errno.ECHILD:
            return "reaped"
        if exc.errno == errno.EINTR:
            return "pending"
        raise
    if waited == 0:
        return "pending"
    if os.WIFSTOPPED(status):
        return "stopped"
    if os.WIFEXITED(status) or os.WIFSIGNALED(status):
        return "exited"
    return "pending"


def _detach_ptrace(pid: int) -> tuple[int, int, str]:
    """Wait for the attach stop, then detach. Returns ``(rc, errno, state)``.

    ``PTRACE_DETACH`` before that stop returns ``ESRCH`` and leaves the
    tracee attached, so an ``EINTR`` from the first ``waitpid`` is retried.
    Once the stop has been seen, a later ``ESRCH`` is polled with ``WNOHANG``
    only, and both that retry and a detach ``EINTR`` are bounded. An exit or
    reap means nothing remains attached. A wait error other than ``EINTR``
    still detaches once; the detach result is returned and the wait error
    does not replace it.
    """

    try:
        state = _await_ptrace_stop(pid)
    except OSError:
        rc, err = _ptrace(PTRACE_DETACH, pid)
        return rc, err, "reaped" if err == errno.ECHILD else "detach-failed"
    eintr_left = _PTRACE_DETACH_EINTR
    retries_left = _PTRACE_DETACH_RETRIES
    while True:
        rc, err = _ptrace(PTRACE_DETACH, pid)
        if rc == 0:
            return rc, err, state
        if err == errno.EINTR:
            if eintr_left <= 0:
                return rc, err, state
            eintr_left -= 1
            continue
        if err == errno.ESRCH and state in {"reaped", "exited"}:
            return rc, err, state
        if err != errno.ESRCH or retries_left <= 0 or state != "stopped":
            return rc, err, state
        retries_left -= 1
        try:
            polled = _poll_ptrace(pid)
        except OSError:
            return rc, err, state
        if polled == "exited":
            state = "exited"
        elif polled == "reaped":
            state = "reaped"
        # ``pending`` or another stop: try detach again, without blocking.


# After a failed detach the tracee is SIGKILLed, then the tracer must collect
# the death notification. A non-child (sudo's descendant, systemd's MainPID)
# stays a traced zombie until that wait; the real parent's WNOHANG returns
# (0, 0) while TracerPid is still the probe. The wait is bounded so a kill
# that did not land cannot hang the probe.
_KILL_WAIT_S = 2.0
_KILL_POLL_S = 0.05


def _kill_traced(pid: int) -> str:
    """SIGKILL ``pid`` and collect its death notification.

    Returns ``reaped`` when ``waitpid`` reports the exit, ``gone`` when the
    tracee is already reaped (``ECHILD``), or ``unreaped`` when the bounded
    wait ends with no report. ``EINTR`` is retried inside that bound.
    """

    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    deadline = time.monotonic() + _KILL_WAIT_S
    while time.monotonic() < deadline:
        try:
            waited, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return "gone"
        except OSError as exc:
            if exc.errno == errno.ECHILD:
                return "gone"
            if exc.errno != errno.EINTR:
                return "unreaped"
            waited = 0
            status = 0
        if waited != 0 and (os.WIFEXITED(status) or os.WIFSIGNALED(status)):
            return "reaped"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(_KILL_POLL_S, remaining))
    return "unreaped"


def probe_ptrace(pid: int) -> ProbeOutcome:
    rc, err = _ptrace(PTRACE_ATTACH, pid)
    if rc == -1 and err == 0:
        return None, "ptrace unavailable: libc not found"
    if rc != 0:
        detail = f"rc={rc}:errno={err}:{os.strerror(err) if err else 'ptrace failed'}"
        if err in BLOCKED_ERRNOS:
            return True, detail
        return None, f"not_denied {detail}"
    detach_rc, detach_err, state = _detach_ptrace(pid)
    # ESRCH after a reap or exit is the tracee already being gone, not a live attach.
    released = detach_rc == 0 or (
        state in {"reaped", "exited"} and detach_err in (errno.ESRCH, errno.ECHILD)
    )
    if not released:
        reap = _kill_traced(pid)
        detail = (
            f"rc={detach_rc}:errno={detach_err}:"
            f"{os.strerror(detach_err) if detach_err else 'ptrace detach failed'}"
            f"; kill {reap}"
        )
        return None, f"ptrace detach failed {detail}"
    return False, "attached"


def probe_abs_store_read(
    write_cmd: Sequence[str],
    directory: str,
    *,
    expected_owner: int | None = None,
    run_fn: RunFn = _default_run,
) -> ProbeOutcome:
    """Have ``write_cmd`` create a 0600 store, then try to read it by absolute path.

    A writer that fails, leaves no file, leaves a symlink, or wrote as a uid
    other than ``expected_owner`` is an error (``None``), not a blocked read.
    The read walks the path with ``O_NOFOLLOW`` on every component, not only
    the leaf. The inode is pinned with ``O_PATH`` and checked (owner and mode).
    The read reopens that same fd through ``/proc/self/fd``; a 0600 file owned
    by another uid is refused there, and that ``EACCES``/``EPERM`` is blocked,
    not a missing store and not a later name in the directory. Callers still
    pass a directory this process created and a unique final name
    (``store-<pid>-<ns>.secret``).
    """

    store = Path(directory) / f"store-{os.getpid()}-{time.time_ns()}.secret"
    result = run_fn([*write_cmd, "python3", "-c", store_writer_script(str(store))], 8.0)
    if result.returncode != 0:
        return None, f"writer_failed rc={result.returncode} err={(result.stderr or '')[:160]}"
    try:
        path_fd, dirfd, name = _open_leaf_nofollow(str(store), _opath_flag())
    except OSError as exc:
        # ELOOP: the leaf or an ancestor is a symlink. ENOTDIR: an earlier
        # component is not a directory (O_DIRECTORY|O_NOFOLLOW did not follow it).
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            return None, f"store_symlink {exc.errno}"
        return None, f"writer_left_no_store {type(exc).__name__}:{exc.errno}"
    read_fd: int | None = None
    data = b""
    try:
        st = os.fstat(path_fd)
        owner_uid = st.st_uid
        # O_PATH|O_NOFOLLOW on a final symlink returns the link itself, not ELOOP.
        if stat.S_ISLNK(st.st_mode):
            return None, f"store_symlink {errno.ELOOP}"
        if expected_owner is not None and owner_uid != expected_owner:
            return None, f"writer_wrong_uid owner={owner_uid} expected={expected_owner}"
        if not stat.S_ISREG(st.st_mode):
            return None, f"not_denied store_not_regular mode={oct(st.st_mode)}"
        try:
            # Reopen the pinned inode. A second lookup of ``name`` can be a
            # different file by the time it runs (the probe directory is 01777).
            read_fd = os.open(f"/proc/self/fd/{path_fd}", os.O_RDONLY)
        except OSError as exc:
            detail = (
                f"{type(exc).__name__}:{exc.errno}:{exc.strerror} "
                f"owner={owner_uid} mode={oct(st.st_mode)}"
            )
            if exc.errno in BLOCKED_ERRNOS:
                return True, detail
            return None, f"not_denied {detail}"
        try:
            data = os.read(read_fd, 1024 * 1024)
        except OSError as exc:
            detail = (
                f"{type(exc).__name__}:{exc.errno}:{exc.strerror} "
                f"owner={owner_uid} mode={oct(st.st_mode)}"
            )
            if exc.errno in BLOCKED_ERRNOS:
                return True, detail
            return None, f"not_denied {detail}"
    finally:
        if read_fd is not None:
            os.close(read_fd)
        os.close(path_fd)
        try:
            os.unlink(name, dir_fd=dirfd)
        except OSError:
            pass
        os.close(dirfd)
    return False, f"read {len(data)} bytes"


def spawn_command(
    backend: Backend, argv: Sequence[str], *, unit: str | None = None
) -> list[str]:
    """``unit`` names the transient systemd-run unit; other backends ignore it."""

    if is_root_target(backend):
        raise ValueError("refusing to launch uid 0")
    if backend.name == "sudo":
        if not backend.target_user:
            raise ValueError("sudo backend needs target_user")
        return ["sudo", "-n", "-u", backend.target_user, "--", *argv]
    if backend.name == "systemd_run":
        if not backend.target_user:
            raise ValueError("systemd_run backend needs target_user")
        return [
            "systemd-run",
            f"--uid={backend.target_user}",
            *([f"--unit={unit}"] if unit else []),
            "--wait",
            "--pipe",
            "--collect",
            "--",
            *argv,
        ]
    if backend.name == "setuid_helper":
        raise ValueError("setuid helper invocation protocol is not recorded")
    if backend.name == "subuid_userns":
        raise ValueError(
            "subuid user-namespace spawn is not implemented or measured; detection only"
        )
    raise ValueError(f"unknown backend: {backend.name}")


def measure_overhead(backend: Backend, n: int = DEFAULT_N) -> Overhead:
    if n < 10:
        raise ValueError(f"overhead n must be >= 10, got {n}")
    cmd = tuple(spawn_command(backend, OVERHEAD_ARGV))
    samples: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        result = _default_run(cmd, 10.0)
        dt = time.perf_counter() - t0
        if result.returncode != 0:
            raise RuntimeError(
                f"overhead command failed rc={result.returncode} "
                f"err={(result.stderr or '')[:200]} cmd={cmd}"
            )
        samples.append(dt)
    samples.sort()
    return Overhead(
        n=n,
        p50_s=percentile(samples, 50),
        p95_s=percentile(samples, 95),
        min_s=samples[0],
        max_s=samples[-1],
        command=cmd,
    )


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=1)
        except Exception:
            pass


class _Tally:
    """Per-channel counts: blocked, open, or error (probe could not run)."""

    def __init__(self) -> None:
        self.blocked = {"proc_pid_root": 0, "ptrace": 0, "abs_store_read": 0}
        self.errors = {"proc_pid_root": 0, "ptrace": 0, "abs_store_read": 0}
        self.details = {"proc_pid_root": "", "ptrace": "", "abs_store_read": ""}
        self.launch_failures = {"proc_pid_root": 0, "ptrace": 0, "abs_store_read": 0}
        self.launch_details = {"proc_pid_root": "", "ptrace": "", "abs_store_read": ""}
        self.targets: dict[str, dict[str, int]] = {
            "proc_pid_root": {},
            "ptrace": {},
            "abs_store_read": {},
        }

    def add(self, name: str, outcome: ProbeOutcome, target: str) -> None:
        result, detail = outcome
        if result is None:
            self.errors[name] += 1
            if _is_launch_failure(name, detail):
                self.launch_failures[name] += 1
                if not self.launch_details[name]:
                    self.launch_details[name] = detail
        elif result:
            self.blocked[name] += 1
        self.details[name] = detail
        seen = self.targets[name]
        seen[target] = seen.get(target, 0) + 1

    def summaries(self, n: int) -> tuple[ProbeSummary, ...]:
        def targets(name: str) -> str:
            return ", ".join(f"{k} x{v}" for k, v in sorted(self.targets[name].items()))

        return tuple(
            ProbeSummary(
                name,  # type: ignore[arg-type]
                n,
                self.blocked[name],
                self.details[name],
                self.errors[name],
                targets(name),
                self.launch_failures[name],
                self.launch_details[name],
            )
            for name in ("proc_pid_root", "ptrace", "abs_store_read")
        )


def _probe_pid(tally: _Tally, pid: int | None, missing: str) -> None:
    if pid is None:
        tally.add("proc_pid_root", (None, missing), "not found")
        tally.add("ptrace", (None, missing), "not found")
        return
    uids = host_uids_of(pid)
    target = f"{_comm_of(pid)} uid={uids[0] if uids else '?'}"
    tally.add("proc_pid_root", probe_proc_pid_root(pid), target)
    tally.add("ptrace", probe_ptrace(pid), target)


def run_same_uid_probes(
    backend: Backend,
    n: int = DEFAULT_N,
    *,
    run_fn: RunFn = _default_run,
) -> tuple[ProbeSummary, ...]:
    """F21 probes from the calling uid against an engine running as ``backend.target_uid``.

    The probed process is the one whose uids all equal ``target_uid`` (for
    systemd-run, the unit's MainPID), never the launcher. When it cannot be
    found, the attempt counts as an error, not as blocked.
    """

    if n < 10:
        raise ValueError(f"probe n must be >= 10, got {n}")
    target_uid = backend.target_uid
    if target_uid is None:
        raise ValueError(f"{backend.name} backend has no target_uid to probe")
    if is_root_target(backend):
        raise ValueError("refusing to probe uid 0")
    tally = _Tally()
    write_prefix = spawn_command(backend, ())
    with tempfile.TemporaryDirectory(prefix="csl734-store-") as tmp:
        os.chmod(tmp, 0o1777)
        for _ in range(n):
            tally.add(
                "abs_store_read",
                probe_abs_store_read(
                    write_prefix, tmp, expected_owner=target_uid, run_fn=run_fn
                ),
                f"0600 store owner uid={target_uid}",
            )
            unit = None
            if backend.name == "systemd_run":
                unit = f"csl734-probe-{uuid.uuid4().hex[:12]}"
            cmd = spawn_command(backend, SLEEP_PROBE_ARGV, unit=unit)
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                if unit is not None:
                    pid = systemd_unit_main_pid(unit, target_uid, run_fn=run_fn)
                    missing = f"no MainPID with uid {target_uid} for unit {unit}"
                else:
                    pid = find_target_uid_pid(proc.pid, target_uid)
                    missing = f"no process with uid {target_uid} under launcher pid {proc.pid}"
                _probe_pid(tally, pid, missing)
            finally:
                _terminate(proc)
                if unit is not None:
                    run_fn(("systemctl", "stop", unit), 5.0)
    return tally.summaries(n)


def run_same_uid_control_probes(n: int = DEFAULT_N) -> tuple[ProbeSummary, ...]:
    """Same-uid control: spawn as the calling uid. Channels should stay open."""

    if n < 10:
        raise ValueError(f"probe n must be >= 10, got {n}")
    our_uid = os.getuid()
    tally = _Tally()
    with tempfile.TemporaryDirectory(prefix="csl734-store-same-") as tmp:
        os.chmod(tmp, 0o1777)
        for _ in range(n):
            tally.add(
                "abs_store_read",
                probe_abs_store_read((), tmp, expected_owner=our_uid),
                f"0600 store owner uid={our_uid}",
            )
            proc = subprocess.Popen(
                list(SLEEP_PROBE_ARGV), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            try:
                pid = find_target_uid_pid(proc.pid, our_uid)
                _probe_pid(tally, pid, f"no process with uid {our_uid} at pid {proc.pid}")
            finally:
                _terminate(proc)
    return tally.summaries(n)


def collect_host_facts(*, run_fn: RunFn | None = None) -> HostFacts:
    run = run_fn or _default_run
    uid = os.getuid()
    try:
        username = pwd.getpwuid(uid).pw_name
    except KeyError:
        username = str(uid)
    subuid = _subid_lines(username, _read_text("/etc/subuid"))
    subgid = _subid_lines(username, _read_text("/etc/subgid"))
    newuidmap = None
    newuidmap_setuid = False
    for candidate in ("/usr/bin/newuidmap", "/usr/sbin/newuidmap"):
        try:
            st = os.stat(candidate)
        except OSError:
            continue
        newuidmap = candidate
        newuidmap_setuid = _is_setuid_root(st)
        break
    raw_max = _read_text("/proc/sys/user/max_user_namespaces")
    user_ns_max = int(raw_max.strip()) if raw_max and raw_max.strip().isdigit() else None
    unshare = run(("unshare", "--user", "id", "-u"), 8.0)
    unshare_user_works = unshare.returncode == 0
    unshare_host_uid = None
    proc = None
    try:
        proc = subprocess.Popen(
            ["unshare", "--user", "sleep", "3"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.05)
        unshare_host_uid = host_uid_of(proc.pid)
    except FileNotFoundError:
        unshare_user_works = False
    finally:
        if proc is not None:
            _terminate(proc)
    systemd_is_pid1 = False
    try:
        comm = Path("/proc/1/comm").read_text(encoding="utf-8", errors="replace").strip()
        systemd_is_pid1 = comm == "systemd"
    except OSError:
        systemd_is_pid1 = False
    return HostFacts(
        uid=uid,
        username=username,
        sudo_n_true=_sudo_n_true(run),
        subuid_lines=subuid,
        subgid_lines=subgid,
        newuidmap_path=newuidmap,
        newuidmap_is_setuid_root=newuidmap_setuid,
        user_ns_max=user_ns_max,
        unshare_user_works=unshare_user_works,
        unshare_host_uid=unshare_host_uid,
        systemd_is_pid1=systemd_is_pid1,
    )


def detect_backends(
    *,
    facts: HostFacts | None = None,
    run_fn: RunFn | None = None,
    stat_fn: StatFn | None = None,
    env: Mapping[str, str] | None = None,
    helper_paths: Sequence[str] | None = None,
    engine_user: str | None = None,
) -> tuple[Backend, ...]:
    """Detect spawn backends. ``engine_user`` (else ``RECURSION_ENGINE_USER``)
    is tried before the ``nobody``/``daemon`` measurement stand-ins."""

    run = run_fn or _default_run
    stat_fn = stat_fn or os.stat
    facts = facts or collect_host_facts(run_fn=run)
    our_uid = facts.uid
    engine = _configured_engine_user(engine_user, env)
    candidates = SUDO_TARGET_CANDIDATES
    # Numeric uid 0 (``0``, ``#0``, ``00``, ``#00``), the name ``root``, and any
    # local account ``pwd`` resolves to uid 0 are refused before a launch.
    if engine is not None and not _is_root_user_token(engine):
        candidates = (engine, *(u for u in SUDO_TARGET_CANDIDATES if u != engine))

    helper_found, helper_note = _inspect_helpers(
        helper_paths if helper_paths is not None else _helper_paths(env),
        stat_fn,
    )
    setuid_backend = Backend(
        "setuid_helper",
        available=False,
        changes_host_uid=False,
        reason=helper_note,
        helper_path=helper_found,
    )

    # Sudoers can grant a target user without granting root, so the target
    # probe runs whatever ``sudo -n true`` (root target) returned.
    root_note = "sudo -n true ok" if facts.sudo_n_true else "sudo -n true failed"
    user, target_uid, sudo_note = _probe_sudo_target(run, our_uid, candidates)
    if user is not None and target_uid is not None:
        sudo_backend = Backend(
            "sudo",
            True,
            True,
            f"{root_note}; {sudo_note}",
            target_user=user,
            target_uid=target_uid,
        )
    else:
        sudo_backend = Backend(
            "sudo",
            False,
            False,
            f"{root_note}; cannot sudo to another uid for the required commands: "
            f"{sudo_note}; not editing sudoers",
        )

    stand_in = sudo_backend.target_user or "nobody"
    systemd_user = engine or stand_in
    sys_ok, sys_uid, sys_reason = _probe_systemd_run(run, systemd_user, our_uid)
    if not sys_ok and engine is not None and stand_in != engine:
        # The configured engine user cannot be reached, but systemd-run may still reach the stand-in.
        # Probe it so that working path is still measured and reported (bot review 4512091152); a shared
        # stand-in is never selected (select_path), so this changes evidence, not selection.
        alt_ok, alt_uid, alt_reason = _probe_systemd_run(run, stand_in, our_uid)
        sys_reason = f"engine user {engine}: {sys_reason}; stand-in {stand_in}: {alt_reason}"
        if alt_ok:
            sys_ok, sys_uid, systemd_user = True, alt_uid, stand_in
    if not facts.systemd_is_pid1 and not sys_ok:
        sys_reason = f"systemd is not PID 1; {sys_reason}"
    systemd_backend = Backend(
        "systemd_run",
        sys_ok,
        sys_ok,
        sys_reason + ("" if sys_ok else "; not editing polkit rules"),
        target_user=systemd_user if sys_ok else None,
        target_uid=sys_uid,
    )

    range_note = (
        f"subuid={','.join(facts.subuid_lines) or 'none'} "
        f"subgid={','.join(facts.subgid_lines) or 'none'}"
    )
    if facts.newuidmap_path and facts.newuidmap_is_setuid_root and facts.subuid_lines:
        # A working newuidmap could map subordinate uids. Probe map-auto and
        # record whether the child's host uid changes. --map-auto maps the
        # first subuid block to ns uid 0, so ns uid 1 is block start + 1.
        # spawn_command does not implement this path, so it is detection only
        # and never available for selection.
        expected_uid = None
        fields = facts.subuid_lines[0].split(":")
        if len(fields) >= 2 and fields[1].isdigit():
            expected_uid = int(fields[1]) + 1
        mapped = run(
            ("unshare", "--user", "--map-auto", "--setuid", "1", "--setgid", "1", "id", "-u"),
            8.0,
        )
        host_changed = False
        child_host_uid = None
        proc = None
        try:
            proc = subprocess.Popen(
                ["unshare", "--user", "--map-auto", "--setuid", "1", "--setgid", "1", "sleep", "3"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if expected_uid is not None and expected_uid != our_uid:
                child_pid = find_target_uid_pid(proc.pid, expected_uid)
                if child_pid is not None:
                    child_host_uid = expected_uid
                    host_changed = True
        except FileNotFoundError:
            mapped = ProcResult(127, "", "unshare not found")
        finally:
            if proc is not None:
                _terminate(proc)
        host_changed = mapped.returncode == 0 and host_changed
        subuid_backend = Backend(
            "subuid_userns",
            False,
            host_changed,
            (
                f"{range_note}; newuidmap={facts.newuidmap_path} setuid_root; "
                f"unshare --map-auto rc={mapped.returncode} host_uid={child_host_uid}; "
                "spawn path not implemented or measured, so not selectable"
            ),
            target_uid=child_host_uid if host_changed else None,
        )
    else:
        why = []
        if not facts.subuid_lines:
            why.append("no /etc/subuid line for this user")
        if not facts.newuidmap_path:
            why.append("newuidmap absent")
        elif not facts.newuidmap_is_setuid_root:
            why.append(f"{facts.newuidmap_path} is not setuid root")
        extra = ""
        if facts.unshare_user_works:
            extra = (
                f"; unshare --user works but host uid stays "
                f"{facts.unshare_host_uid} (not an other-uid path)"
            )
        subuid_backend = Backend(
            "subuid_userns",
            False,
            False,
            f"{range_note}; {'; '.join(why)}; not installing newuidmap{extra}",
        )

    return (setuid_backend, sudo_backend, systemd_backend, subuid_backend)


def _is_launch_failure(name: str, detail: str) -> bool:
    """True when one probe error means the backend could not launch or write.

    Process-not-found and store-writer failures are permission or availability
    problems, not F21 channel outcomes (blocked vs open). Other probe errors,
    such as ptrace being unavailable on the host, stay with the summaries.
    """

    detail = detail or ""
    if name == "abs_store_read":
        return "writer_" in detail
    if name in {"proc_pid_root", "ptrace"}:
        # "ptrace unavailable: libc not found" is a host limit, not a launch.
        if detail.startswith("ptrace unavailable"):
            return False
        return (
            "not found" in detail
            or "no process with uid" in detail
            or "no MainPID" in detail
        )
    return False


def _launch_failure(probes: Sequence[ProbeSummary]) -> str | None:
    """Return a reason when any attempt shows the backend could not launch or write.

    Uses the per-attempt launch-failure count, so a failure on an early attempt
    is not hidden by a later success. Summaries built without that count fall
    back to classifying the last detail.
    """

    reasons: list[str] = []
    for probe in probes:
        count = probe.launch_failure_count
        detail = probe.launch_failure_detail
        if count <= 0 and probe.error_count > 0 and _is_launch_failure(probe.name, probe.detail):
            count, detail = probe.error_count, probe.detail
        if count <= 0:
            continue
        if probe.name == "abs_store_read":
            reasons.append(
                f"{probe.name} writer failed on {count}/{probe.n} attempts: {detail}"
            )
        else:
            reasons.append(
                f"{probe.name} engine process did not appear on "
                f"{count}/{probe.n} attempts: {detail}"
            )
    if not reasons:
        return None
    return "; ".join(reasons)


def _f21_not_blocked(probes: Sequence[ProbeSummary]) -> str | None:
    """Return a reason when any F21 channel is not blocked on every attempt."""

    reasons = [
        f"{probe.name} {probe.blocked_count} blocked/{probe.open_count} open/"
        f"{probe.error_count} error of {probe.n}: {probe.detail}"
        for probe in probes
        if not probe.blocked
    ]
    names = {probe.name for probe in probes}
    for name in ("proc_pid_root", "ptrace", "abs_store_read"):
        if name not in names:
            reasons.append(f"{name} not probed")
    if not reasons:
        return None
    return "; ".join(reasons)


def evaluate_host(
    *,
    n: int = DEFAULT_N,
    run_fn: RunFn | None = None,
    facts: HostFacts | None = None,
    backends: Sequence[Backend] | None = None,
    measure_fn: Callable[[Backend, int], Overhead] = measure_overhead,
    probe_fn: Callable[[Backend, int], tuple[ProbeSummary, ...]] = run_same_uid_probes,
    f21_open: Sequence[str] | None = None,
    engine_user: str | None = None,
    env: Mapping[str, str] | None = None,
) -> Evaluation:
    """Measure every spawnable other-uid backend, then select a path.

    A backend whose measurement fails (for example a sudoers rule that denies
    a command mid-run) is recorded in ``errors`` and marked unavailable, so
    selection falls through to the next backend or the beta fallback instead
    of aborting the evaluation. Failure to launch the probe process or write
    the store is treated the same way: those are not F21 channel outcomes,
    and they must not leave an unusable backend selected.

    A backend that launches but leaves any F21 channel open or errored is
    kept in ``overhead`` and ``probes`` as evidence, recorded in ``errors``,
    and marked unavailable: the separate uid exists to close those channels.
    ``f21_open`` is passed to ``select_path``; ``engine_user`` and ``env``
    to ``detect_backends`` when ``backends`` is not given.
    """

    if n < 10:
        raise ValueError(f"evaluate_host n must be >= 10, got {n}")

    facts = facts or collect_host_facts(run_fn=run_fn)
    detected = tuple(backends) if backends is not None else detect_backends(
        facts=facts, run_fn=run_fn, env=env, engine_user=engine_user
    )
    overhead: dict[str, Overhead] = {}
    probes: dict[str, tuple[ProbeSummary, ...]] = {}
    errors: dict[str, str] = {}
    final: list[Backend] = []
    for backend in detected:
        if not (backend.available and backend.changes_host_uid and can_spawn(backend)):
            final.append(backend)
            continue
        try:
            measured = measure_fn(backend, n)
            probed = probe_fn(backend, n)
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
            message = f"{type(exc).__name__}: {str(exc)[:300]}"
            errors[backend.name] = message
            final.append(
                replace(
                    backend,
                    available=False,
                    reason=f"{backend.reason}; measurement failed, not selectable: {message}",
                )
            )
            continue
        launch_err = _launch_failure(probed)
        if launch_err is not None:
            errors[backend.name] = launch_err
            final.append(
                replace(
                    backend,
                    available=False,
                    reason=f"{backend.reason}; launch failed, not selectable: {launch_err}",
                )
            )
            continue
        overhead[backend.name] = measured
        probes[backend.name] = probed
        not_blocked = _f21_not_blocked(probed)
        if not_blocked is not None:
            errors[backend.name] = f"F21 channels not blocked: {not_blocked}"
            final.append(
                replace(
                    backend,
                    available=False,
                    reason=(
                        f"{backend.reason}; F21 channels not blocked, not selectable: "
                        f"{not_blocked}"
                    ),
                )
            )
            continue
        final.append(backend)
    backends = tuple(final)
    selected = select_path(backends, f21_open=f21_open)
    return Evaluation(
        product_commit=PRODUCT_MD_COMMIT,
        product_sha256=PRODUCT_MD_SHA256,
        facts=facts,
        backends=backends,
        selected=selected,
        overhead=overhead,
        probes=probes,
        unmeasured={
            "engine_user_created_at_install": ENGINE_USER_CREATED_AT_INSTALL,
            "claude_code_signin_survives_reboot": CLAUDE_CODE_SIGNIN_SURVIVES_REBOOT,
            "codex_signin_survives_reboot": CODEX_SIGNIN_SURVIVES_REBOOT,
            "headless_secret_service_live": HEADLESS_SECRET_SERVICE_LIVE,
        },
        errors=errors,
    )


__all__ = [
    "BLOCKED_ERRNOS",
    "CLAUDE_CODE_SIGNIN_SURVIVES_REBOOT",
    "CODEX_SIGNIN_SURVIVES_REBOOT",
    "DEFAULT_N",
    "ENGINE_USER_CREATED_AT_INSTALL",
    "ENGINE_USER_ENV",
    "HEADLESS_SECRET_SERVICE_LIVE",
    "PRODUCT_MD_COMMIT",
    "PRODUCT_MD_SHA256",
    "SHARED_SYSTEM_ACCOUNTS",
    "SHARED_SYSTEM_UIDS",
    "SLEEP_PROBE_ARGV",
    "SPAWNABLE_BACKENDS",
    "SUDO_REQUIRED_COMMANDS",
    "Backend",
    "Evaluation",
    "HostFacts",
    "Overhead",
    "ProbeSummary",
    "SelectedPath",
    "byok_storage_when_secret_service_fails_closed",
    "can_spawn",
    "collect_host_facts",
    "detect_backends",
    "evaluate_host",
    "f21_open_items",
    "find_target_uid_pid",
    "is_root_target",
    "is_shared_system_account",
    "measure_overhead",
    "percentile",
    "run_same_uid_control_probes",
    "run_same_uid_probes",
    "select_path",
    "spawn_command",
    "store_writer_script",
    "systemd_unit_main_pid",
]
