"""Find space held by files that were unlinked while still open.

A file that is deleted while a process holds it open has no directory entry, so
it is invisible to ``du``, to ``ls``, to ``ncdu`` and to any ``scandir`` walk --
including this tool's own. The blocks are still allocated, and on a
quota-enforced filesystem they may still be charged.

Verified on Midway3 GPFS: 512 MiB written, unlinked with the fd held.

    scandir walk (== what du sees)        128 MiB   <- the 512 MiB is gone
    /proc/self/fd/3 -> /scratch/.../probe.bin (deleted)
    fstat via that fd                     512 MiB, nlink=0

So the *detection* half works on GPFS, with a pid attached. Two limits are
built into the mechanism and are reported rather than papered over:

* **Only your own processes are readable.** ``/proc/<pid>/fd`` on another user's
  process is EACCES for an unprivileged caller. In a shared group quota -- the
  motivating case -- you can prove a gap exists but cannot name the labmate
  holding it. We report the number of processes we could not inspect.
* **Only this node.** A deleted fd held by a job on a compute node is invisible
  from the login node. This is a local scan, and says so.
"""

import errno
import os
import stat
from typing import Dict, List, Optional, Tuple  # noqa: F401  (used in `# type:` comments)

_DELETED_SUFFIX = " (deleted)"
_PROC = "/proc"


class DeletedFile:
    """One unlinked-but-open inode, with the processes holding it."""

    def __init__(self, dev: int, ino: int, size: int, path: str) -> None:
        self.dev = dev
        self.ino = ino
        self.size = size  # allocated blocks, st_blocks*512
        self.path = path  # the path it had before it was unlinked
        self.holders = []  # type: List[Tuple[int, str]]  # (pid, command)

    def add_holder(self, pid: int, comm: str) -> None:
        if (pid, comm) not in self.holders:
            self.holders.append((pid, comm))

    @property
    def pids(self) -> List[int]:
        return [p for p, _ in self.holders]


class DeletedScan:
    """Result of one ``/proc`` sweep."""

    def __init__(self) -> None:
        self.files = []  # type: List[DeletedFile]
        self.scanned_pids = 0
        self.unreadable_pids = 0  # other users' processes: EACCES
        self.available = True
        self.reason = ""

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def complete(self) -> bool:
        """False when other users' processes could not be inspected."""
        return self.unreadable_pids == 0

    def under(self, prefix: str) -> "DeletedScan":
        """Restrict to inodes whose pre-deletion path was under ``prefix``."""
        pref = os.path.abspath(prefix).rstrip("/")
        out = DeletedScan()
        out.scanned_pids = self.scanned_pids
        out.unreadable_pids = self.unreadable_pids
        out.available = self.available
        out.reason = self.reason
        out.files = [f for f in self.files if f.path == pref or f.path.startswith(pref + "/")]
        return out


def _read_comm(pid: int) -> str:
    for name in ("comm", "cmdline"):
        try:
            with open("{}/{}/{}".format(_PROC, pid, name), "rb") as fh:
                raw = fh.read(256)
        except OSError:
            continue
        text = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
        if text:
            return text
    return "?"


def scan(prefix: Optional[str] = None) -> DeletedScan:
    """Sweep ``/proc/*/fd`` for unlinked-but-open regular files.

    ``prefix`` restricts the result to one subtree. Inodes are deduplicated by
    ``(st_dev, st_ino)``: several processes -- or several fds in one process --
    may hold the same inode, and the blocks are allocated once.
    """
    res = DeletedScan()
    if not os.path.isdir(_PROC):
        res.available = False
        res.reason = "/proc is not available on this platform"
        return res

    by_inode = {}  # type: Dict[Tuple[int, int], DeletedFile]
    pref = os.path.abspath(prefix).rstrip("/") if prefix else None

    for entry in os.listdir(_PROC):
        if not entry.isdigit():
            continue
        pid = int(entry)
        fd_dir = "{}/{}/fd".format(_PROC, pid)
        try:
            fds = os.listdir(fd_dir)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EPERM):
                res.unreadable_pids += 1
            # ESRCH/ENOENT: the process exited between listdir and here.
            continue
        res.scanned_pids += 1

        comm = None  # type: Optional[str]
        for fd in fds:
            link = "{}/{}".format(fd_dir, fd)
            try:
                target = os.readlink(link)
            except OSError:
                continue
            if not target.endswith(_DELETED_SUFFIX):
                continue
            path = target[: -len(_DELETED_SUFFIX)]
            if not path.startswith("/"):
                # pipe:[...], socket:[...], anon_inode:[...] and friends.
                continue
            if pref is not None and path != pref and not path.startswith(pref + "/"):
                continue
            try:
                # stat() through /proc/<pid>/fd/<n> reaches the inode even
                # though it has no directory entry left.
                st = os.stat(link)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            key = (st.st_dev, st.st_ino)
            rec = by_inode.get(key)
            if rec is None:
                rec = DeletedFile(st.st_dev, st.st_ino, st.st_blocks * 512, path)
                by_inode[key] = rec
            if comm is None:
                comm = _read_comm(pid)
            rec.add_holder(pid, comm)

    res.files = sorted(by_inode.values(), key=lambda f: f.size, reverse=True)
    return res
