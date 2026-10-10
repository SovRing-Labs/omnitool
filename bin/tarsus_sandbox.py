#!/usr/bin/env python3
"""tarsus_sandbox — run a command with filesystem WRITES confined to given directories (Linux Landlock, unprivileged).

TARSUS-0 A3 (2026-10-06, review G1): write scopes were checked only for write/patch ops; a bash op could write
anywhere (`echo x > /var/tmp/y` ran). This launcher makes the kernel enforce the same scopes for the whole process
tree of a bash op: reads, exec and network are untouched; creating, writing, truncating, renaming or deleting
outside the granted directories fails with EACCES.

    python3 -S tarsus_sandbox.py --write DIR [--write DIR ...] [--mode enforce|best-effort] -- CMD [ARGS...]

Single-threaded by construction (applies the ruleset, then execv) — no preexec_fn in a threaded parent.
Field: Landlock (Linux >= 5.13; kernel docs Documentation/userspace-api/landlock.rst); same approach as Sandlock
(arXiv 2605.26298). Clean-room: written from the kernel UAPI, no third-party code.
Exit 126 = sandbox could not be applied in enforce mode (nothing ran).
"""
import ctypes
import os
import sys

# x86_64 / aarch64 / riscv share the generic syscall numbers for landlock (444-446).
SYS_CREATE, SYS_ADD_RULE, SYS_RESTRICT = 444, 445, 446
PR_SET_NO_NEW_PRIVS = 38
CREATE_RULESET_VERSION = 1
RULE_PATH_BENEATH = 1

# LANDLOCK_ACCESS_FS_* bits (include/uapi/linux/landlock.h)
WRITE_FILE, REMOVE_DIR, REMOVE_FILE = 1 << 1, 1 << 4, 1 << 5
MAKE_CHAR, MAKE_DIR, MAKE_REG, MAKE_SOCK, MAKE_FIFO, MAKE_BLOCK, MAKE_SYM = (1 << 6, 1 << 7, 1 << 8, 1 << 9, 1 << 10,
                                                                             1 << 11, 1 << 12)
REFER, TRUNCATE = 1 << 13, 1 << 14          # ABI 2, ABI 3
FILE_RIGHTS = WRITE_FILE | TRUNCATE          # the only write rights valid on a non-directory


class RulesetAttr(ctypes.Structure):          # first field only: older-ABI-compatible size
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class PathBeneathAttr(ctypes.Structure):      # __attribute__((packed))
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def write_rights(abi: int) -> int:
    r = WRITE_FILE | REMOVE_DIR | REMOVE_FILE | MAKE_CHAR | MAKE_DIR | MAKE_REG | MAKE_SOCK | MAKE_FIFO | MAKE_BLOCK | MAKE_SYM
    if abi >= 2:
        r |= REFER
    if abi >= 3:
        r |= TRUNCATE
    return r


def apply(dirs, libc=None) -> int:
    """Confine this process (and its future children) to writing beneath `dirs`. Returns the ABI used; raises OSError."""
    libc = libc or ctypes.CDLL(None, use_errno=True)
    abi = libc.syscall(SYS_CREATE, None, 0, CREATE_RULESET_VERSION)
    if abi < 1:
        raise OSError(ctypes.get_errno(), "landlock not available")
    handled = write_rights(abi)
    attr = RulesetAttr(handled)
    rfd = libc.syscall(SYS_CREATE, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if rfd < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset")
    try:
        for d in dirs:
            try:
                fd = os.open(d, os.O_PATH | os.O_CLOEXEC)
            except OSError:
                continue                       # a missing scope grants nothing; never fatal
            try:
                rights = handled if os.path.isdir(d) else (handled & FILE_RIGHTS)
                pb = PathBeneathAttr(rights, fd)
                if libc.syscall(SYS_ADD_RULE, rfd, RULE_PATH_BENEATH, ctypes.byref(pb), 0) != 0:
                    raise OSError(ctypes.get_errno(), f"landlock_add_rule {d}")
            finally:
                os.close(fd)
        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl(NO_NEW_PRIVS)")
        if libc.syscall(SYS_RESTRICT, rfd, 0) != 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self")
    finally:
        os.close(rfd)
    return abi


# Device files every shell needs to write (redirects to /dev/null etc.). /proc: `>/dev/stdout` and `/dev/fd/N` resolve
# through /proc/<pid>/fd to the inherited pipe — per-pid, so the whole of /proc is granted (procfs writes stay
# guarded by ordinary permissions).
DEVICE_WRITES = ["/dev/null", "/dev/zero", "/dev/full", "/dev/tty", "/dev/ptmx", "/dev/pts", "/dev/shm", "/proc"]


def main(argv) -> int:
    dirs, mode = [], "enforce"
    i = 0
    while i < len(argv) and argv[i] != "--":
        if argv[i] == "--write" and i + 1 < len(argv):
            dirs.append(os.path.expanduser(argv[i + 1])); i += 2
        elif argv[i] == "--mode" and i + 1 < len(argv):
            mode = argv[i + 1]; i += 2
        else:
            print(f"tarsus_sandbox: unknown argument {argv[i]!r}", file=sys.stderr); return 2
    cmd = argv[i + 1:]
    if not cmd:
        print("usage: tarsus_sandbox.py --write DIR ... -- CMD [ARGS]", file=sys.stderr); return 2
    # SQLite (and others) fall back to /var/tmp for temp files when TMPDIR is unset; /var/tmp is outside the grants,
    # so a big FTS rebuild died with "unable to open database file" (system-docs-indexer, 2026-10-06). Point TMPDIR
    # at /tmp when it is granted.
    if not os.environ.get("TMPDIR") and any(os.path.realpath(d) == "/tmp" for d in dirs):
        os.environ["TMPDIR"] = "/tmp"
    try:
        apply(dirs + DEVICE_WRITES)
    except OSError as e:
        if mode == "enforce":
            print(f"tarsus_sandbox: refusing to run unsandboxed ({e}); set sandbox = best-effort to allow", file=sys.stderr)
            return 126
        print(f"tarsus_sandbox: WARNING running unsandboxed ({e})", file=sys.stderr)
    os.execv(cmd[0], cmd)
    return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
