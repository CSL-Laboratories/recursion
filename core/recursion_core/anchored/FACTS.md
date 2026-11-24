# System facts for the anchored-path module

What each system does with links when a folder is walked from a held descriptor. The rows come from
`core/recursion_core/anchored/tests/test_system_facts.py`. Each case prints one `FACT {...}` line on
stderr, and the numbers below are copied from those lines.

**This file is APPEND-ONLY.** A later host or system adds its own section and rows. No row is ever
rewritten or removed: a later run that disagrees adds a row that says so. A system or case with no
run says NOT RUN and gives the reason.

Legend: "asserted" means the test fails if the fact does not hold; "recorded" means the number is
written down and never checked.

## Run 1: Linux on WSL2, Wed Nov 18 2026, master ba82fb92

- Host: the build host's WSL2 (Ubuntu 24.04), kernel `6.6.114.1-microsoft-standard-WSL2`, machine
  `x86_64`, ext4. An ordinary user (uid 1000), with no sudo and no root.
- Interpreters: CPython 3.12.3 (`-X utf8 -B`, GIL on) and CPython 3.14.8 free-threaded
  (`-X gil=0 -B`, GIL off, `sys._is_gil_enabled()` False).
- Command, run from the repository root, one process at a time under a memory ceiling:
  `PYTHONPATH=core python -X utf8 -B -m unittest -v recursion_core.anchored.tests.test_system_facts`
  (3.12), and the same with `-X gil=0 -B` (3.14t).
- Result: `Ran 7 tests ... OK (skipped=2)` on both interpreters. The two skips are L5 and W5 (below).
  The numbers below come from one run of each interpreter (20:47 PST). The recorded counts vary from
  run to run. An earlier run that day gave L3 (i) 78 files outside (3.12) and 403 (3.14t), and the
  asserted facts held on every run.

| Case | Interpreter | Result | Raw numbers |
|---|---|---|---|
| L1 per-part walk | 3.12 | asserted, holds | a link in the middle part refuses: `os.open(part, O_RDONLY\|O_DIRECTORY\|O_NOFOLLOW\|O_CLOEXEC, dir_fd=parent)` raised ENOTDIR |
| L1 one multi-part open | 3.12 | asserted, holds | `os.open("a/link/c", same flags, dir_fd=held)` opened the folder behind the link (same inode). O_NOFOLLOW guards the last name only |
| L1 per-part walk | 3.14t | asserted, holds | ENOTDIR |
| L1 one multi-part open | 3.14t | asserted, holds | opened the folder behind the link |
| L2 openat2 | 3.12 | asserted, holds | syscall 437; `RESOLVE_BENEATH\|RESOLVE_NO_SYMLINKS`: middle link ELOOP, `../outside` EXDEV, plain 5-part path opened |
| L2 numbers | 3.12 | recorded | 10,000 opens: EAGAIN 0, other errors 0; median per open: openat2 3,290 ns, per-part walk (5 parts) 11,170 ns |
| L2 openat2 | 3.14t | asserted, holds | syscall 437; ELOOP, EXDEV, plain opened |
| L2 numbers | 3.14t | recorded | 10,000 opens: EAGAIN 0, other errors 0; median per open: openat2 3,230 ns, per-part walk 10,530 ns |
| L3 (i) the shape of today | 3.12 | recorded | 2,000 rounds: 303 made, 1,697 refused by `islink`, 0 errors; **107 files landed in the outside folder**, 196 inside; 1,639 swaps |
| L3 (ii) held walk | 3.12 | asserted, holds | 2,000 rounds: 543 made, 1,457 refused by the walk (ELOOP/ENOTDIR), 0 errors; **0 files outside**, 543 inside; 6,702 swaps |
| L3 (i) the shape of today | 3.14t, GIL off | recorded | 2,000 rounds: 1,130 made, 870 refused, 0 errors; **386 files landed in the outside folder**, 744 inside; 10,088 swaps |
| L3 (ii) held walk | 3.14t, GIL off | asserted, holds | 2,000 rounds: 1,760 made, 240 refused, 0 errors; **0 files outside**, 1,760 inside; 11,568 swaps |
| L4 ostree-shaped home | 3.12 | asserted, holds | `unshare -r` (uid map `0 1000 1`), chroot into a root with `/home -> var/home`: the copied owner check passed `/home/u/data` |
| L4 ostree-shaped home | 3.14t | asserted, holds | same: passed |
| L4 side fact | both | recorded | in the same namespace before the chroot, the host's `/` shows as uid 65534, and the owner check refuses it: `store_untrusted: / is owned by another user`. In a one-uid user namespace the check cannot pass any real host path |
| L5 link owned by another uid | both | **NOT RUN** | `chown -h 65534` answered EINVAL inside `unshare -r`: the namespace maps one uid (`0 1000 1`). Mapping more needs `newuidmap`, which is not installed, and installing it needs root. The test skips with this reason |
| W5 junction through interop | — | **NOT RUN ON REAL WINDOWS** | nothing was started on the host's Windows side: W5 runs `cmd.exe` and runs only when `RECURSION_FACTS_WSL_INTEROP=1` is set. It waits for a Windows run arranged for it |

L3 setup: a second thread swaps `p3` of `p1/p2/p3/p4/p5` with a link to an outside folder that holds
`p4/p5`. Each swap is one atomic `renameat2(RENAME_EXCHANGE)`. The first thread makes and writes one file
per round. "Refused" counts the rounds in which the shape saw the link and made nothing.

Clean-up: every link, folder and file lives in a `tempfile.TemporaryDirectory`. The run's TMPDIR was
empty before and after each run (0 entries).

Not this run: Linux on a host that is not WSL2 (no such host was used; the hosted Linux job runs the
module but its numbers are not recorded here).

## NOT RUN yet (rows to be added by their own runs)

- Windows 11, real host (W1 to W4, W6, and W5's junction from the Windows side): NOT RUN. It needs a
  Windows host run.
- macOS (M1 to M3, and L1 there): NOT RUN. There is no macOS host.

## Run 2: real Windows 11, Wed Nov 18 2026, on d60dd995

This section adds the real Windows rows. It does not rewrite the rows above; the line "Windows 11,
real host ... NOT RUN" in the section above is answered here for W1 to W4 and W6. W5 is still NOT
RUN (it is a WSL2 case and was not started).

- Host: Windows 11, build `10.0.26200.9974` (`ver`), machine `AMD64`, NTFS. An ordinary account,
  not elevated; no setting was changed for the run.
- Interpreters: CPython 3.12.16 (`-X utf8 -B`, GIL on) and CPython 3.14.10 free-threaded
  (`-X gil=0 -B`, GIL off, `sys._is_gil_enabled()` False). The host has no other 3.12 or 3.14t.
- Command, run from the repository root, one process at a time:
  `PYTHONPATH=core python -X utf8 -B -m unittest -v recursion_core.anchored.tests.test_system_facts`
  (3.12), and the same with `-X gil=0 -B` (3.14t).
- Result: `Ran 12 tests ... OK (skipped=8)` on both interpreters, on each of two runs. The skips are
  L1 to L5 and W5 (not Linux) and W6 (below). Every call into the system goes through ctypes; the
  junctions come from `_winapi.CreateJunction`.

| Case | Interpreter | Result | Raw numbers |
|---|---|---|---|
| W1 junction in a middle part | 3.12 | asserted, holds | made by this account with no special right: `os.path.islink` False, `os.path.isjunction` True; `CreateFileW("held\a\j\c", FILE_READ_ATTRIBUTES, FILE_FLAG_OPEN_REPARSE_POINT\|FILE_FLAG_BACKUP_SEMANTICS)` opened it and `GetFinalPathNameByHandleW` gave `<tmp>\outside\c`: the flag guards the last name only and the junction in the middle was followed |
| W1 junction in a middle part | 3.14t, GIL off | asserted, holds | the same: islink False, isjunction True, final path `<tmp>\outside\c` |
| W2 NtCreateFile walk | 3.12 | asserted, holds | `NtCreateFile` with `RootDirectory` = the held parent, one part at a time, `FILE_OPEN_REPARSE_POINT\|FILE_DIRECTORY_FILE`: the junction part (part 1 of `a\j\c`) opened as itself, status `0x00000000`, `FILE_ATTRIBUTE_TAG_INFO` tag `0xA0000003` (mount point), so the walk refuses it; a plain part opened |
| W2 NtCreateFile walk | 3.14t, GIL off | asserted, holds | the same: part 1, status `0x00000000`, tag `0xA0000003` |
| W3 OBJ_DONT_REPARSE, middle junction | 3.12 | recorded | `NtCreateFile` with `RootDirectory` = the held folder, `OBJ_DONT_REPARSE\|OBJ_CASE_INSENSITIVE`, the relative name `a\j\c`: status `0xC000050B` (STATUS_REPARSE_POINT_ENCOUNTERED), nothing opened |
| W3 OBJ_DONT_REPARSE, plain name | 3.12 | recorded | the same call with the plain relative name `a\b\c`: status `0x00000000`, opened. The Windows 10 1507 fallback is noted, not run |
| W3 both statuses | 3.14t, GIL off | recorded | the same: middle junction `0xC000050B`, plain name `0x00000000` |
| W4 folder held with FILE_SHARE_READ only | 3.12 | asserted, holds | held by `CreateFileW(..., FILE_SHARE_READ, FILE_FLAG_BACKUP_SEMANTICS)`; a second interpreter process: rename of the folder winerror 32, `rmdir` of the folder 32, rename of its parent 5, `shutil.rmtree` of the parent 32 (its first step, the held folder, is refused); both folders still there afterwards |
| W4 the same | 3.14t, GIL off | asserted, holds | the same: 32, 32, 5, 32; both still there |
| W6 symbolic link in a middle part | both | **NOT RUN** | `os.symlink(..., target_is_directory=True)` answered winerror 1314 (ERROR_PRIVILEGE_NOT_HELD): this account cannot make a symbolic link. It needs Developer Mode or the symbolic-link right, a system setting the run did not change. The test skips with this reason |
| W5 junction through WSL2 interop | — | **NOT RUN** | a WSL2 case; not started on this host |

Red first: each assert of W1, W2 and W4 (13 in all) was shown to fail with its own condition flipped
in a scratch copy, on both interpreters, with exactly its own test red; the unchanged copy was green.
W3 asserts nothing (both statuses are recorded) and W6 did not run.

Clean-up: every junction, folder and file lives in a `tempfile.TemporaryDirectory`, every junction
points at a folder in that same directory, and each junction is removed by its own name before the
directory goes. The temp folder of the run held 0 entries before and after each run and each flip.

## Run 3: real Windows 11 and the host's own WSL2 layer, Mon Nov 23 2026, on 71381ea2

This section adds rows for W7 and W8 and the WSL2 side of W5 on the same laptop. It does not rewrite
the rows above.

- Host: Windows 11, build `10.0.26200.9974` (`ver`), machine `AMD64`, NTFS. An ordinary account,
  not elevated; no setting was changed for the run.
- Interpreters: CPython 3.12.16 (`-X utf8 -B`, GIL on) and CPython 3.14.10 free-threaded
  (`-X gil=0 -B`, GIL off).
- Command, run from `core/`, one process at a time, with `TEMP` and `TMP` pointed at an empty
  folder of the run's own: `python -X utf8 -B -m unittest -v recursion_core.anchored.tests.test_system_facts`
  (3.12), and the same with `-X gil=0 -B` (3.14t). The same file through the gate's own runner
  (`ci_gate.run_test_file`, 3.12) gave the same result.
- Result: `Ran 14 tests ... OK (skipped=8)` on both interpreters. The skips are L1 to L5 and W5 (not
  Linux) and W6 (winerror 1314, as in Run 2). W7 and W8 ran and passed on both.

| Case | Interpreter | Result | Raw numbers |
|---|---|---|---|
| W7 junction made by `cmd.exe /d /c mklink /J` | 3.12 | asserted, holds | `mklink /J` exit 0, no special right; `os.path.islink` False, `os.path.isjunction` True; the walk (`NtCreateFile` per part relative to the held parent, `FILE_OPEN_REPARSE_POINT`) refused part 1 of `a\j\c`, status `0x00000000`, tag `0xA0000003`; a plain part opened; afterwards the junction was gone and its target still held `c` |
| W7 the same | 3.14t, GIL off | asserted, holds | the same: exit 0, islink False, isjunction True, part 1, status `0x00000000`, tag `0xA0000003` |
| W8 junction swapped in after the check | 3.12 | asserted, holds | the part `a` opened through the held parent as itself, no reparse point (the check); then `a` was renamed to `a-moved` and `mklink /J` made a junction `a` to the outside folder (exit 0); `c` opened through the part already held gave `<tmp>\held\a-moved\c` (not redirected); a new walk of `a\c` refused part 0, tag `0xA0000003`. Recorded only: `CreateFileW` of the path's text `held\a\c` after the same swap gave `<tmp>\outside\c` |
| W8 the same | 3.14t, GIL off | asserted, holds | the same: held part `<tmp>\held\a-moved\c`, new walk refused at part 0, tag `0xA0000003`; by the path's text `<tmp>\outside\c` (recorded) |
| W5 the junction seen from the host's WSL2 layer | 3.12.3 (the layer's own python3) | **NOT RUN** | the host's only WSL2 distribution (kernel `6.6.87.2-microsoft-standard-WSL2`) has automount and interop turned off in its own `/etc/wsl.conf`: no Windows drive is mounted (`/mnt/c` is an empty folder) and no `cmd.exe` is reachable, so the layer cannot see a junction on the Windows drive at all. The module, fed on stdin with the opt-in set, skipped W5 with `cmd.exe or wslpath is not on PATH`. Nothing was installed, mounted or changed in the layer, so what it reports for a junction is still not known |

Red first: in a scratch copy, the walk's reparse-point check taken out, and separately the walk's
`FILE_OPEN_REPARSE_POINT` taken out, each turned W7 and W8 red (and W2, which uses the same walk);
W8's use made by the path's text instead of through the held part turned W8 alone red; the unchanged
copy was green. On both interpreters.

Clean-up: every junction, folder and file of W7 and W8 lives in a `tempfile.TemporaryDirectory`,
every junction points at a folder in that same directory, and each junction is removed by its own
name in a `finally` before the directory goes; each case asserts that the junction is gone and its
target was not emptied. The temp folder of the run held 0 entries after each run and each cut.
