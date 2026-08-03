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
import threading
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
        # True when /proc shows only a PID namespace's processes rather than the
        # node's. The coverage line reads as node-wide, so under Apptainer,
        # Docker or a Slurm cgroup with proc remounted, "1 of 1 processes" is a
        # 100%-coverage sentence produced from a namespace holding one process --
        # on a node running 1,400. Honest about EACCES, blind to this, until now.
        self.namespaced = False
        # True when the sweep was abandoned mid-flight, almost certainly because
        # a stat() blocked on a hung mount. See `scan`.
        self.timed_out = False

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def complete(self) -> bool:
        """False when other users' processes could not be inspected."""
        return self.unreadable_pids == 0 and not self.namespaced and not self.timed_out

    def under(self, prefix: str) -> "DeletedScan":
        """Restrict to inodes whose pre-deletion path was under ``prefix``."""
        pref = os.path.abspath(prefix).rstrip("/")
        out = DeletedScan()
        out.scanned_pids = self.scanned_pids
        out.unreadable_pids = self.unreadable_pids
        out.available = self.available
        out.reason = self.reason
        out.namespaced = self.namespaced
        out.timed_out = self.timed_out
        out.files = [f for f in self.files if f.path == pref or f.path.startswith(pref + "/")]
        return out


def _in_pid_namespace() -> bool:
    """Does /proc show a PID namespace rather than the whole node?

    Two independent signals, either of which is sufficient:

    * ``/proc/self/status``'s ``NSpid`` lists one entry per namespace this
      process is visible in, so more than one means we are nested.
    * pid 1 in the root namespace is the init system. Inside a container it is
      whatever the container started.

    Both are read-only files in procfs and cannot block. A false *negative* just
    restores the previous behaviour, so this is safe to get wrong quietly.
    """
    try:
        with open("{}/self/status".format(_PROC)) as fh:
            for line in fh:
                if line.startswith("NSpid:"):
                    if len(line.split()) > 2:
                        return True
                    break
    except OSError:
        pass
    try:
        with open("{}/1/comm".format(_PROC)) as fh:
            return fh.read().strip() not in ("systemd", "init", "openrc-init")
    except OSError:
        # pid 1 not visible at all is itself evidence of a restricted view.
        return True


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


# A whole-node sweep of readable fds takes ~0.01s here (30 inspectable processes
# of 1,440). Anything beyond this is not slowness, it is a blocked stat.
DEFAULT_SCAN_TIMEOUT_S = 10.0


def scan(prefix: Optional[str] = None, timeout: float = DEFAULT_SCAN_TIMEOUT_S) -> DeletedScan:
    """Sweep ``/proc/*/fd`` for unlinked-but-open regular files.

    ``prefix`` restricts the result to one subtree. Inodes are deduplicated by
    ``(st_dev, st_ino)``: several processes -- or several fds in one process --
    may hold the same inode, and the blocks are allocated once.

    **Bounded, because the sweep is on the emergency path.** ``os.stat`` through
    ``/proc/<pid>/fd/<n>`` resolves the real inode, so if that inode lives on a
    hung NFS, Lustre or autofs mount the call blocks in uninterruptible sleep
    with no timeout and no signal that will reach it. ``rdu -a`` runs this scan
    unconditionally, and a tool for storage emergencies must not have an
    unbounded blocking call on the emergency path -- a degraded MDS is the same
    afternoon someone reaches for it.

    There is no way to interrupt a blocked syscall from the thread making it, so
    the sweep runs in a daemon thread and is *abandoned* if it overruns. The
    abandoned thread stays parked in D state until the mount recovers; being a
    daemon, it does not delay interpreter exit. Whatever it had already found is
    reported, with ``timed_out`` set so the caller can say coverage is partial.
    """
    res = DeletedScan()
    if not os.path.isdir(_PROC):
        res.available = False
        res.reason = "/proc is not available on this platform"
        return res
    res.namespaced = _in_pid_namespace()

    # Completed records only. `list.append` is atomic under the GIL, so the main
    # thread can safely read a prefix of this list after abandoning the worker.
    found = []  # type: List[DeletedFile]
    done = threading.Event()
    worker = threading.Thread(
        target=_sweep, args=(res, found, prefix, done), name="rapidu-deleted", daemon=True
    )
    worker.start()
    done.wait(timeout)
    if not done.is_set():
        res.timed_out = True
        res.reason = (
            "the /proc sweep was abandoned after {:.0f}s, which means a stat() "
            "blocked on an unresponsive mount; results below are partial".format(timeout)
        )
    res.files = sorted(found[:], key=lambda f: f.size, reverse=True)
    return res


def _sweep(
    res: DeletedScan, found: "List[DeletedFile]", prefix: Optional[str], done: "threading.Event"
) -> None:
    """The body of :func:`scan`, run in a thread so it can be abandoned."""
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
            # The suffix is a hint; nlink == 0 is the proof. The kernel appends
            # " (deleted)" to the readlink target of an unlinked inode, and a
            # file genuinely *named* `report (deleted).pdf` produces a target
            # that ends the same way and is indistinguishable by string match.
            # Trusting the string would attribute space to a file that is not
            # unlinked at all -- a fabricated finding, in the one section of this
            # tool whose entire job is to avoid making them. We already hold the
            # fd, so the authoritative test costs nothing.
            if st.st_nlink != 0:
                continue
            key = (st.st_dev, st.st_ino)
            rec = by_inode.get(key)
            if rec is None:
                rec = DeletedFile(st.st_dev, st.st_ino, st.st_blocks * 512, path)
                by_inode[key] = rec
                found.append(rec)
            if comm is None:
                comm = _read_comm(pid)
            rec.add_holder(pid, comm)

    done.set()
