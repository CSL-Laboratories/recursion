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
