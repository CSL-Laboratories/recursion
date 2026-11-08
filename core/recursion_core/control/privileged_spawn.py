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
import math
import os
import pwd
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
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
SUDO_TARGET_CANDIDATES = ("nobody", "daemon")
DEFAULT_N = 20
PTRACE_ATTACH = 16
PTRACE_DETACH = 17

BackendName = Literal["setuid_helper", "sudo", "systemd_run", "subuid_userns"]
PathName = Literal[
    "setuid_helper", "sudo", "systemd_run", "subuid_userns", "managed_login_folder"
]
ProbeName = Literal["proc_pid_root", "ptrace", "abs_store_read"]
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
    name: ProbeName
    n: int
    blocked_count: int
    detail: str

    @property
    def blocked(self) -> bool:
        return self.n > 0 and self.blocked_count == self.n


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


def select_path(backends: Sequence[Backend]) -> SelectedPath:
    """Pick the first backend that already changes the host uid; else beta fallback."""

    order: tuple[BackendName, ...] = (
        "setuid_helper",
        "sudo",
        "systemd_run",
        "subuid_userns",
    )
    by_name = {b.name: b for b in backends}
    for name in order:
        backend = by_name.get(name)
        if backend is not None and backend.available and backend.changes_host_uid:
            return SelectedPath(
                name,
                False,
                f"{name} can spawn another host uid without mutating system config",
                backend,
            )
    return SelectedPath(
        "managed_login_folder",
        True,
        "no other-uid backend is available without installing a helper or "
        "editing sudoers/polkit; managed-login-folder fallback ships as beta "
        '(Lucas, Nov 7: "Yes the fallback ships as beta")',
        None,
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


def _is_setuid_root(st: os.stat_result) -> bool:
    return bool(st.st_mode & stat.S_ISUID) and st.st_uid == 0


def _inspect_helpers(
    paths: Sequence[str],
    stat_fn: StatFn,
) -> tuple[str | None, str]:
    notes: list[str] = []
    found: str | None = None
    for path in paths:
        try:
            st = stat_fn(path)
        except OSError:
            notes.append(f"{path} absent")
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


def _probe_sudo_target(run_fn: RunFn, our_uid: int) -> tuple[str | None, int | None, str]:
    notes: list[str] = []
    for user in SUDO_TARGET_CANDIDATES:
        result = run_fn(("sudo", "-n", "-u", user, "--", "id", "-u"), 8.0)
        uid = _parse_uid_output(result.stdout)
        if result.returncode == 0 and uid is not None and uid != our_uid:
            notes.append(f"sudo -n -u {user} -- id -u -> {uid}")
            return user, uid, "; ".join(notes)
        err = (result.stderr or result.stdout).strip().replace("\n", " ")
        notes.append(
            f"sudo -n -u {user} -- id -u rc={result.returncode} uid={uid} err={err[:160]}"
        )
    return None, None, "; ".join(notes) if notes else "no sudo target candidates"


def _probe_systemd_run(run_fn: RunFn, user: str, our_uid: int) -> tuple[bool, int | None, str]:
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
    ok = result.returncode == 0 and uid is not None and uid != our_uid
    reason = (
        f"systemd-run --uid={user} --wait --pipe --collect /usr/bin/id -u "
        f"rc={result.returncode} uid={uid}"
    )
    if err:
        reason = f"{reason} err={err[:240]}"
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


def host_uid_of(pid: int) -> int | None:
    field = _proc_field(pid, "Uid")
    if not field:
        return None
    token = field.split()[0]
    return int(token) if token.isdigit() else None


def _children_of(pid: int) -> list[int]:
    try:
        raw = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="utf-8")
    except OSError:
        return []
    return [int(part) for part in raw.split() if part.isdigit()]


def find_other_uid_pid(root_pid: int, our_uid: int, *, timeout_s: float = 1.5) -> tuple[int, int]:
    """Walk ``root_pid`` and its descendants; prefer a process whose host uid differs."""

    deadline = time.monotonic() + timeout_s
    last = (root_pid, host_uid_of(root_pid) if host_uid_of(root_pid) is not None else our_uid)
    while time.monotonic() < deadline:
        seen: set[int] = set()
        stack = [root_pid]
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            uid = host_uid_of(pid)
            if uid is not None:
                last = (pid, uid)
                if uid != our_uid:
                    return pid, uid
            stack.extend(_children_of(pid))
        time.sleep(0.02)
    return last


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


def probe_proc_pid_root(pid: int) -> tuple[bool, str]:
    path = f"/proc/{pid}/root"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        return True, f"{type(exc).__name__}:{exc.errno}:{exc.strerror}"
    os.close(fd)
    return False, "opened"


def probe_ptrace(pid: int) -> tuple[bool, str]:
    rc, err = _ptrace(PTRACE_ATTACH, pid)
    if rc == 0:
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        _ptrace(PTRACE_DETACH, pid)
        return False, "attached"
    return True, f"rc={rc}:errno={err}:{os.strerror(err) if err else 'ptrace failed'}"


def probe_abs_store_read(write_cmd: Sequence[str], directory: str) -> tuple[bool, str]:
    store = Path(directory) / f"store-{os.getpid()}-{time.time_ns()}.secret"
    quoted = repr(str(store))
    script = (
        "import os; p=" + quoted + "; "
        "fd=os.open(p, os.O_CREAT|os.O_WRONLY|os.O_TRUNC, 0o600); "
        "os.write(fd, b'store-bytes'); os.close(fd); os.chmod(p, 0o600)"
    )
    result = _default_run([*write_cmd, "python3", "-c", script], 8.0)
    if result.returncode != 0:
        return True, f"writer_failed rc={result.returncode} err={(result.stderr or '')[:160]}"
    try:
        data = store.read_bytes()
    except OSError as exc:
        owner = None
        mode = None
        try:
            st = store.stat()
            owner = st.st_uid
            mode = oct(st.st_mode)
        except OSError:
            pass
        return True, f"{type(exc).__name__}:{exc.errno}:{exc.strerror} owner={owner} mode={mode}"
    finally:
        try:
            store.unlink()
        except OSError:
            pass
    return False, f"read {len(data)} bytes"


def spawn_command(backend: Backend, argv: Sequence[str]) -> list[str]:
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
            "--wait",
            "--pipe",
            "--collect",
            "--",
            *argv,
        ]
    if backend.name == "setuid_helper":
        raise ValueError("setuid helper invocation protocol is not recorded")
    if backend.name == "subuid_userns":
        raise ValueError("subuid user-namespace spawn is not available without newuidmap")
    raise ValueError(f"unknown backend: {backend.name}")


def measure_overhead(backend: Backend, n: int = DEFAULT_N) -> Overhead:
    if n < 10:
        raise ValueError(f"overhead n must be >= 10, got {n}")
    cmd = tuple(spawn_command(backend, ("true",)))
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


def run_same_uid_probes(backend: Backend, n: int = DEFAULT_N) -> tuple[ProbeSummary, ...]:
    if n < 10:
        raise ValueError(f"probe n must be >= 10, got {n}")
    our_uid = os.getuid()
    counts = {"proc_pid_root": 0, "ptrace": 0, "abs_store_read": 0}
    details = {"proc_pid_root": "", "ptrace": "", "abs_store_read": ""}
    write_prefix = spawn_command(backend, ())
    with tempfile.TemporaryDirectory(prefix="csl734-store-") as tmp:
        os.chmod(tmp, 0o1777)
        for _ in range(n):
            blocked, detail = probe_abs_store_read(write_prefix, tmp)
            counts["abs_store_read"] += int(blocked)
            details["abs_store_read"] = detail
            cmd = spawn_command(backend, ("sleep", "8"))
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                pid, _uid = find_other_uid_pid(proc.pid, our_uid)
                blocked, detail = probe_proc_pid_root(pid)
                counts["proc_pid_root"] += int(blocked)
                details["proc_pid_root"] = detail
                blocked, detail = probe_ptrace(pid)
                counts["ptrace"] += int(blocked)
                details["ptrace"] = detail
            finally:
                _terminate(proc)
    return (
        ProbeSummary("proc_pid_root", n, counts["proc_pid_root"], details["proc_pid_root"]),
        ProbeSummary("ptrace", n, counts["ptrace"], details["ptrace"]),
        ProbeSummary("abs_store_read", n, counts["abs_store_read"], details["abs_store_read"]),
    )


def run_same_uid_control_probes(n: int = DEFAULT_N) -> tuple[ProbeSummary, ...]:
    """Same-uid control: spawn as the calling uid. Channels should stay open."""

    if n < 10:
        raise ValueError(f"probe n must be >= 10, got {n}")
    counts = {"proc_pid_root": 0, "ptrace": 0, "abs_store_read": 0}
    details = {"proc_pid_root": "", "ptrace": "", "abs_store_read": ""}
    with tempfile.TemporaryDirectory(prefix="csl734-store-same-") as tmp:
        os.chmod(tmp, 0o1777)
        for _ in range(n):
            blocked, detail = probe_abs_store_read((), tmp)
            counts["abs_store_read"] += int(blocked)
            details["abs_store_read"] = detail
            proc = subprocess.Popen(
                ["sleep", "8"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            try:
                pid = proc.pid
                blocked, detail = probe_proc_pid_root(pid)
                counts["proc_pid_root"] += int(blocked)
                details["proc_pid_root"] = detail
                blocked, detail = probe_ptrace(pid)
                counts["ptrace"] += int(blocked)
                details["ptrace"] = detail
            finally:
                _terminate(proc)
    return (
        ProbeSummary("proc_pid_root", n, counts["proc_pid_root"], details["proc_pid_root"]),
        ProbeSummary("ptrace", n, counts["ptrace"], details["ptrace"]),
        ProbeSummary("abs_store_read", n, counts["abs_store_read"], details["abs_store_read"]),
    )


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
) -> tuple[Backend, ...]:
    run = run_fn or _default_run
    stat_fn = stat_fn or os.stat
    facts = facts or collect_host_facts(run_fn=run)
    our_uid = facts.uid

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

    if not facts.sudo_n_true:
        sudo_backend = Backend(
            "sudo",
            False,
            False,
            "sudo -n true failed; not editing sudoers",
        )
    else:
        user, target_uid, sudo_note = _probe_sudo_target(run, our_uid)
        if user is not None and target_uid is not None:
            sudo_backend = Backend(
                "sudo",
                True,
                True,
                f"sudo -n true ok; {sudo_note}",
                target_user=user,
                target_uid=target_uid,
            )
        else:
            sudo_backend = Backend(
                "sudo",
                False,
                False,
                f"sudo -n true ok, but cannot sudo to another uid: {sudo_note}; "
                "not editing sudoers",
            )

    systemd_user = sudo_backend.target_user or "nobody"
    sys_ok, sys_uid, sys_reason = _probe_systemd_run(run, systemd_user, our_uid)
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
        # A working newuidmap could map subordinate uids. Probe map-auto; only
        # accept the backend if the host uid of the child actually changes.
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
            child_host_uid = find_other_uid_pid(proc.pid, our_uid)[1]
            host_changed = child_host_uid != our_uid
        except FileNotFoundError:
            mapped = ProcResult(127, "", "unshare not found")
        finally:
            if proc is not None:
                _terminate(proc)
        available = mapped.returncode == 0 and host_changed
        subuid_backend = Backend(
            "subuid_userns",
            available,
            available,
            (
                f"{range_note}; newuidmap={facts.newuidmap_path} setuid_root; "
                f"unshare --map-auto rc={mapped.returncode} host_uid={child_host_uid}"
            ),
            target_uid=child_host_uid if available else None,
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


def evaluate_host(*, n: int = DEFAULT_N, run_fn: RunFn | None = None) -> Evaluation:
    facts = collect_host_facts(run_fn=run_fn)
    backends = detect_backends(facts=facts, run_fn=run_fn)
    selected = select_path(backends)
    overhead: dict[str, Overhead] = {}
    probes: dict[str, tuple[ProbeSummary, ...]] = {}
    for backend in backends:
        if not (backend.available and backend.changes_host_uid):
            continue
        try:
            spawn_command(backend, ("true",))
        except ValueError:
            continue
        overhead[backend.name] = measure_overhead(backend, n)
        probes[backend.name] = run_same_uid_probes(backend, n)
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
    )


__all__ = [
    "CLAUDE_CODE_SIGNIN_SURVIVES_REBOOT",
    "CODEX_SIGNIN_SURVIVES_REBOOT",
    "DEFAULT_N",
    "ENGINE_USER_CREATED_AT_INSTALL",
    "HEADLESS_SECRET_SERVICE_LIVE",
    "PRODUCT_MD_COMMIT",
    "PRODUCT_MD_SHA256",
    "Backend",
    "Evaluation",
    "HostFacts",
    "Overhead",
    "ProbeSummary",
    "SelectedPath",
    "byok_storage_when_secret_service_fails_closed",
    "collect_host_facts",
    "detect_backends",
    "evaluate_host",
    "measure_overhead",
    "percentile",
    "run_same_uid_control_probes",
    "run_same_uid_probes",
    "select_path",
    "spawn_command",
]
