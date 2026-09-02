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
import re
import stat
import threading
from typing import Dict, List, Optional, Tuple  # noqa: F401  (used in `# type:` comments)

_DELETED_SUFFIX = " (deleted)"
_PROC = "/proc"

# NFS does not unlink an open file: the client renames the entry out of the way
# and removes it when the last descriptor closes. `nfs_sillyrename` in
# fs/nfs/unlink.c builds the name as ".nfs" + the file id + a counter, both hex,
# so `.nfs00000002945e149d00002b83` is what a deleted-but-open file looks like on
# an NFS home -- measured on one.
#
# This matters because it is the *same event* as the rest of this module: someone
# deleted a file, a process still holds it open, and the blocks are still charged.
# The scan was blind to it by construction -- there is no " (deleted)" suffix and
# `st_nlink` is 1, so the two gates that make the local finding trustworthy both
# reject it -- which meant the section reported "none found" on every NFS site,
# always, no matter how much space was held. That is a true sentence and a
# useless one.
#
# Kept apart from `files` rather than folded in, because the two are not
# interchangeable: an unlinked inode is invisible to `du` and this one is not.
# The header this section prints promises invisibility, so merging them would
# make that claim false.
#
# **And `reconcile` would double-count.** It adds this scan's bytes to the walk's
# to explain a quota, through `owned_by`/`owned_by_gid`, which read `files`. A
# `.nfsXXXX` entry is an ordinary directory entry that the walk has already
# charged, so folding these in would add them twice and invent a gap -- in the
# section whose job is to close one.
#
# The width is not pinned to 24: the field sizes come from kernel types that have
# changed before, and a prefix plus all-hex is already specific enough that no
# ordinary filename reaches it.
#
# `\Z`, not `$`. This pattern is the *whole* proof for the NFS half -- unlike the
# unlinked half there is no `nlink == 0` to fall back on, since a silly-rename
# entry has nlink == 1 exactly like every ordinary file -- so it has to mean what
# it looks like it means. Python's `$` also matches immediately before a trailing
# newline, and a newline is a legal byte in a filename: a real, still-linked,
# still-charged `.nfs00000002945e149d\n` held open by any process was reported as
# a deleted-but-open file, and its blocks were added to `silly_renamed_size` and
# to the report's "held by deleted-but-open files on NFS" line. That is a
# fabricated finding of the same class as trusting the " (deleted)" suffix, which
# `_sweep` already refuses to do.
_SILLY_RENAME_RE = re.compile(r"^\.nfs[0-9a-fA-F]{8,}\Z")


# `st_uid` of an inode whose owner was never read. Not 0: that is root, and a
# root-owned deleted file is an ordinary thing to find.
UID_UNKNOWN = -1


class DeletedFile:
    """One unlinked-but-open inode, with the processes holding it."""

    def __init__(
        self,
        dev: int,
        ino: int,
        size: int,
        path: str,
        uid: int = UID_UNKNOWN,
        gid: int = UID_UNKNOWN,
    ) -> None:
        self.dev = dev
        self.ino = ino
        self.size = size  # allocated blocks, st_blocks*512
        self.path = path  # the path it had before it was unlinked
        # Who the *inode* is charged to. The holding process may not own it -- a
        # shared group directory is the whole reason this scan exists -- so a
        # user-scoped quota can only be reconciled against the subset this
        # matches. See `reconcile`.
        self.uid = uid
        # And which *group* it is charged to, for the same reason: a group quota is
        # charged by gid, so narrowing the walk by gid while adding every unlinked
        # inode regardless would compare two different populations on the two
        # halves of one sum. The stat that produced `uid` already carried this.
        self.gid = gid
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
        # The NFS form of the same event: deleted, still open, still charged --
        # but with a `.nfsXXXX` entry standing in for the name. See
        # `_SILLY_RENAME_RE` for why these are counted separately from `files`.
        self.silly_renamed = []  # type: List[DeletedFile]

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def silly_renamed_size(self) -> int:
        """Allocated bytes held by deleted-but-open files on an NFS mount."""
        return sum(f.size for f in self.silly_renamed)

    def owned_by(self, uid: int) -> "List[DeletedFile]":
        """The inodes charged to ``uid``, plus any whose owner was never read.

        An unknown owner is included rather than dropped: this figure is already
        documented as a floor, and silently discarding an inode that may well be
        yours would make the floor lower than the evidence supports.
        """
        return [f for f in self.files if f.uid in (uid, UID_UNKNOWN)]

    def owned_by_gid(self, gid: int) -> "List[DeletedFile]":
        """The inodes charged to ``gid``, plus any whose group was never read.

        The group counterpart of :meth:`owned_by`, and unknown is included for the
        same reason: this figure is documented as a floor.
        """
        return [f for f in self.files if f.gid in (gid, UID_UNKNOWN)]

    @property
    def complete(self) -> bool:
        """False when anything was hidden from the sweep -- the sweep included.

        ``available`` is the first term because a scan that never ran hid
        *everything*. With ``--no-deleted``, or on a platform with no ``/proc``,
        every counter below is 0 and the three tests all pass, so this said True
        and ``--json`` published ``"complete": true`` beside
        ``"available": false`` for a sweep that had not happened. A consumer
        reading the completeness flag -- which is the field that exists to say
        whether the figures can be trusted -- got the most reassuring possible
        answer from the least informative possible run.

        The other three are the ways a sweep that *did* run can still be partial:
        another user's process (EACCES), a PID namespace that lists only its own
        processes, and a sweep abandoned on a hung mount.
        """
        return (
            self.available
            and self.unreadable_pids == 0
            and not self.namespaced
            and not self.timed_out
        )

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
        out.silly_renamed = [
            f for f in self.silly_renamed if f.path == pref or f.path.startswith(pref + "/")
        ]
        return out


# Inode of the initial PID namespace. A kernel constant -- PROC_PID_INIT_INO in
# include/linux/proc_ns.h -- and the only *authoritative* way to ask "is this the
# node's namespace or a container's" without root.
_INIT_PID_NS_INO = 0xEFFFFFFC


def _in_pid_namespace() -> bool:
    """Does /proc show a PID namespace rather than the whole node?

    Two signals, in order of how much they prove:

    * ``/proc/self/ns/pid``'s inode. In the initial namespace it is the kernel
      constant :data:`_INIT_PID_NS_INO`; anywhere else it is an allocated one.
      This is decisive in *both* directions and is checked first.
    * ``/proc/self/status``'s ``NSpid``, as a fallback for a kernel without
      ``/proc/*/ns``. More than one entry means we are nested. One entry proves
      nothing -- the field lists only the namespaces the reader is in, so a
      container reading its own status sees exactly one.

    **No pid-1 name test.** It used to finish with "pid 1's ``comm`` is not one of
    ``{systemd, init, openrc-init}``, therefore we are in a container", and that
    is a false positive on every host running runit, s6 or dinit: it flips
    ``complete`` to False and prints a container caveat on a bare-metal node. The
    docstring justified the guess by saying a wrong answer was safe, but only
    checked that reasoning against a false *negative* -- which restores the old
    behaviour -- and not against the false positive, which invents a finding. The
    namespace inode answers the question outright, so nothing has to be guessed.
    """
    try:
        return os.stat("{}/self/ns/pid".format(_PROC)).st_ino != _INIT_PID_NS_INO
    except OSError:
        pass
    try:
        with open("{}/self/status".format(_PROC)) as fh:
            for line in fh:
                if line.startswith("NSpid:"):
                    return len(line.split()) > 2
    except OSError:
        pass
    # Neither signal available: report the wider view rather than claiming a
    # restriction we could not observe.
    return False


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

    **What is returned is a snapshot.** The abandoned thread keeps going, so it is
    given a scan object of its own to write into and the counters are copied out
    once. Handing it the returned object meant ``scanned_pids`` kept climbing
    after the caller had it: the coverage sentence a reader saw
    ("none found in the 30 of 1440 processes this scan can inspect") could not be
    reproduced from the object it was printed from, and two consumers of one scan
    disagreed about the denominator.
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
    # The worker's own object, never handed out. Same signature as before, so a
    # test that substitutes `_sweep` still sees four arguments.
    work = DeletedScan()
    worker = threading.Thread(
        target=_sweep, args=(work, found, prefix, done), name="rapidu-deleted", daemon=True
    )
    worker.start()
    done.wait(timeout)
    if not done.is_set():
        res.timed_out = True
        res.reason = (
            "the /proc sweep was abandoned after {:.0f}s, which means a stat() "
            "blocked on an unresponsive mount; results below are partial".format(timeout)
        )
    res.scanned_pids = work.scanned_pids
    res.unreadable_pids = work.unreadable_pids
    res.files = sorted(found[:], key=lambda f: f.size, reverse=True)
    res.silly_renamed = sorted(work.silly_renamed[:], key=lambda f: f.size, reverse=True)
    return res


def _record_silly_rename(
    target: str,
    link: str,
    pref: Optional[str],
    by_inode: "Dict[Tuple[int, int], DeletedFile]",
    res: DeletedScan,
    pid: int,
) -> None:
    """Record one NFS deleted-but-open file, if that is what ``target`` is.

    Same shape as the unlinked case above it, with the two gates that cannot
    apply here replaced by the one that can. There is no " (deleted)" suffix to
    check and ``st_nlink`` is 1, so the proof is the name itself
    (:data:`_SILLY_RENAME_RE`) plus the fact that a process holds it open -- which
    is the whole of what "deleted but still charged" means on NFS.

    ``st_nlink == 1`` is required rather than merely observed: a silly-rename
    entry is the only link to its inode, so a file that a user has *hard-linked*
    to a `.nfsXXXX` name -- or an ordinary file that happens to match, which the
    all-hex pattern makes unlikely but not impossible -- is not one of these and
    must not be reported as deleted. The same reasoning as the ``nlink == 0``
    gate: a name is a hint, and this section does not make findings out of hints.
    """
    if pref is not None and target != pref and not target.startswith(pref + "/"):
        return
    try:
        st = os.stat(link)
    except OSError:
        return
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        return
    key = (st.st_dev, st.st_ino)
    rec = by_inode.get(key)
    if rec is None:
        rec = DeletedFile(st.st_dev, st.st_ino, st.st_blocks * 512, target, st.st_uid, st.st_gid)
        by_inode[key] = rec
        res.silly_renamed.append(rec)
    rec.add_holder(pid, _read_comm(pid))


def _is_anonymous_kernel_object(path: str, dev: int, root_dev: Optional[int]) -> bool:
    """Is ``path`` an anonymous kernel object rather than a file someone unlinked?

    ``memfd_create``, a SysV shm segment, an aio/io_uring ring and a dma-buf each
    produce a *regular file* on a kernel-internal shmem mount that was never
    linked into any directory. ``d_path`` renders such a dentry as though it sat
    in the root directory -- ``/memfd:torch-shm``, ``/SYSV00000000``, ``/[aio]``,
    ``/dmabuf`` -- and, because it is unlinked by construction, appends
    " (deleted)" to it unconditionally.

    So ``S_ISREG`` is true and ``st_nlink`` is 0: both of the gates that make the
    unlinked finding trustworthy pass, and the object is recorded as space held by
    a deleted file. Measured on an ordinary login node here, with no setup at all,
    ``/memfd:pulseaudio`` was reported under a header promising files "invisible to
    ``du``" whose "blocks are still allocated, and on a quota-enforced filesystem
    may still be charged" -- for a path that has never existed in any filesystem
    and bytes no quota will ever show. A synthetic 16 MiB ``memfd`` was attributed
    in full, and ``reconcile`` added it to ``accounted``, closing part of a real
    quota gap with anonymous memory.

    :func:`_sweep` already drops this entire class wherever the kernel renders it
    *without* a leading slash -- ``pipe:[...]``, ``socket:[...]``,
    ``anon_inode:[...]``, which its own comment names. These are the same kind of
    object; they slip past that guard only because their rendering begins with "/".

    **The test is exact, not a name match.** A path with no directory component
    means the file sat in the root directory, so it lived on whatever is mounted at
    "/" and its ``st_dev`` is that filesystem's by definition. A different
    ``st_dev`` therefore proves the string is not a path. A genuine unlinked
    ``/core.1234`` on the root filesystem matches ``root_dev`` and is still
    reported, and anything with a directory component is not considered here at
    all -- so no real finding is dropped to catch these.
    """
    if root_dev is None or os.path.dirname(path) != "/":
        return False
    return dev != root_dev


def _sweep(
    res: DeletedScan, found: "List[DeletedFile]", prefix: Optional[str], done: "threading.Event"
) -> None:
    """The body of :func:`scan`, run in a thread so it can be abandoned."""
    by_inode = {}  # type: Dict[Tuple[int, int], DeletedFile]
    # The NFS half goes onto the worker's own scan object rather than through a
    # fifth parameter: `_sweep`'s four-argument signature is depended on, and
    # `list.append` is atomic under the GIL either way, so an abandoned sweep's
    # partial findings are readable exactly as `found` already is.
    silly_by_inode = {}  # type: Dict[Tuple[int, int], DeletedFile]
    pref = os.path.abspath(prefix).rstrip("/") if prefix else None
    # For `_is_anonymous_kernel_object`. Read once: it is the same answer for every
    # fd in the sweep, and "/" is never on the kind of mount this sweep is bounded
    # against. `None` where even that cannot be read, which leaves the check
    # inoperative rather than dropping a finding on evidence we do not have.
    try:
        root_dev = os.stat("/").st_dev  # type: Optional[int]
    except OSError:
        root_dev = None

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
                # Not unlinked here -- but on NFS it cannot be, so check the one
                # other thing a deleted-but-open file can look like before moving
                # on. The name is already in hand from the `readlink` above, so
                # the test is a string match and costs no syscall; only a match
                # goes on to stat.
                if target.startswith("/") and _SILLY_RENAME_RE.match(target.rsplit("/", 1)[-1]):
                    _record_silly_rename(target, link, pref, silly_by_inode, res, pid)
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
            # ...and nlink == 0 is not the whole proof either, because an object
            # that was never linked in the first place also has none. See
            # `_is_anonymous_kernel_object`.
            if _is_anonymous_kernel_object(path, st.st_dev, root_dev):
                continue
            key = (st.st_dev, st.st_ino)
            rec = by_inode.get(key)
            if rec is None:
                rec = DeletedFile(
                    st.st_dev, st.st_ino, st.st_blocks * 512, path, st.st_uid, st.st_gid
                )
                by_inode[key] = rec
                found.append(rec)
            if comm is None:
                comm = _read_comm(pid)
            rec.add_holder(pid, comm)

    done.set()
