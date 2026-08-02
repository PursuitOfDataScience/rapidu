"""Threaded ``os.scandir`` walker.

Three things about this walker are load-bearing, and getting any of them wrong
makes the tool worse than ``du`` rather than better:

1. **Sum ``st_blocks * 512``, never ``st_size``.** A single 1 GiB sparse file
   makes an ``st_size`` walker report 4.5x too much on an otherwise ordinary
   tree. Sparse checkpoints and preallocated files are common in ML trees.
2. **Dedupe multiply-linked inodes by ``(st_dev, st_ino)``.** ``du`` does this
   within a run; conda envs and checkpoint trees are full of hard links.
3. **Cap the thread pool.** Measured on 790k GPFS inodes, the walk gets *slower*
   past 16 threads (32 threads was 31% worse than 16). The fast setting and the
   polite setting are the same setting, so the cap is both a performance and an
   etiquette control: this walk is metadata load on a shared filesystem, which
   is the exact sin the tool exists to diagnose.

A correct walker agrees with ``du -s --block-size=1`` byte-for-byte. It does not
beat it on accuracy, and any claim that it does is a bug. What it adds is speed
(``du`` is single-threaded and spends ~95% of its wall time blocked on
filesystem latency) and the bookkeeping in :class:`WalkResult` that ``du`` does
not collect at all.
"""

import os
import stat
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional, Set, Tuple  # noqa: F401  (`# type:` use)

# Past this the walk measurably slows down AND the metadata load stops being
# polite. Requests above it are clamped, loudly.
MAX_THREADS = 16
DEFAULT_THREADS = 8

# A file modified this recently may not have its blocks allocated yet on GPFS.
DEFAULT_SETTLE_WINDOW_S = 120.0

# What skipping `stat` is worth. Held on both trees it has been measured on --
# 782k GPFS inodes (27.3s -> 3.4s) and 1.69M (58.7s -> 7.1s) -- which is why the
# README states one figure for both. Defined here once because the walk
# docstring and the `--count` help text previously published 9.1x and 8x for the
# same claim, and Constraint 18 says a number you cannot defend does not go on
# the screen; two numbers for one measurement cannot both be defended.
COUNT_SPEEDUP = 8.0

# Below this an allocation is not a block, it is the filesystem storing the data
# inside the inode. No mainstream filesystem allocates data in units under 4 KiB
# -- ext4 and xfs are 4 KiB, GPFS subblocks here are 16 KiB, Lustre is larger --
# so an allocation under it is evidence of inlining rather than of the unit.
#
# It has to be excluded from the unit measurement or it swallows it. Measured on
# /home: GPFS gives a 100-byte file one 512-byte sector, and since 512 > 100 that
# file is "padded", so it joined the allocation-unit estimate and dragged the
# reported unit from the true 16 KiB down to 512 B. The one number that made the
# whole diagnosis actionable was set by the files it does not describe.
MIN_ALLOC_UNIT = 4096

# Bound on how many recently-modified files we retain for the re-stat pass.
_RECENT_SAMPLE_CAP = 4096


class TokenBucket:
    """Rate limiter over directory opens. Disabled when ``rate <= 0``."""

    def __init__(self, rate: float, burst: Optional[float] = None) -> None:
        self.rate = float(rate)
        self.capacity = float(burst if burst is not None else max(rate, 1.0))
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def take(self) -> None:
        if self.rate <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                deficit = (1.0 - self._tokens) / self.rate
            time.sleep(min(deficit, 0.25))


class Progress:
    """Live counters for a walk in flight, for a progress display.

    Each worker owns one slot and writes only to that slot, so no lock is needed:
    a list item assignment is atomic under the GIL, and a progress display that
    is briefly a few hundred inodes stale is fine. The alternative -- taking a
    lock per inode to keep a counter exact -- would slow the walk down to make a
    spinner more accurate, which is the wrong trade.
    """

    __slots__ = ("inode_slots", "dir_slots", "started", "finished", "current")

    def __init__(self, nthreads: int) -> None:
        self.inode_slots = [0] * nthreads
        self.dir_slots = [0] * nthreads
        self.started = time.monotonic()
        self.finished = False
        self.current = ""  # most recent directory, for context

    @property
    def inodes(self) -> int:
        return sum(self.inode_slots)

    @property
    def dirs(self) -> int:
        return sum(self.dir_slots)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    @property
    def rate(self) -> float:
        el = self.elapsed
        return self.inodes / el if el > 0 else 0.0


class Entry:
    """One reported child of the walked tree: a directory or a large file.

    ``size`` is **cumulative** for a directory -- it includes everything beneath
    it, which is what ``du`` reports and what anyone reading a disk-usage list
    expects. An earlier version charged a directory only with the entries
    immediately inside it, so one subtree read 70.6 MiB against ``du``'s
    94.3 MiB and a parent could rank *below* its own child. Worse, a directory
    whose bulk lived one level down vanished from the listing entirely while its
    children appeared, which reads exactly like missing data.

    Plain files are entries too. Three 63.3 MiB ``.db`` files sitting directly in
    a home directory are 27% of it, and a directory-only listing cannot see them.
    """

    __slots__ = ("path", "size", "files", "dirs", "is_dir")

    def __init__(self, path: str, is_dir: bool) -> None:
        self.path = path
        self.is_dir = is_dir
        self.size = 0
        self.files = 0
        self.dirs = 0

    def add(self, size: int, files: int, dirs: int) -> None:
        self.size += size
        self.files += files
        self.dirs += dirs

    @property
    def inodes(self) -> int:
        """Directories are inodes too, and a files-quota charges for them."""
        return self.files + self.dirs


class WalkResult:
    """Everything one walk learned. All byte figures are allocated blocks."""

    def __init__(self, root: str) -> None:
        self.root = root
        self.size = 0  # sum of st_blocks*512, hardlink-deduped
        self.apparent = 0  # sum of st_size, for the sparse/settling diagnosis
        # Allocation accounting: why `size` and `apparent` disagree, split by
        # direction because a filesystem inflates one directory and deflates the
        # next. Files allocated MORE than their length are paying for a partly
        # filled allocation unit; files allocated LESS are sparse, compressed, or
        # small enough that the data lives in the inode.
        self.padded_files = 0
        self.padded_apparent = 0
        self.padded_alloc = 0
        self.under_files = 0
        self.under_apparent = 0
        self.under_alloc = 0
        # Files whose whole allocation is under one block: the data lives in
        # the inode. Counted apart from both classes above because it is
        # neither padding nor sparseness, and because including it in the
        # padded class set the measured allocation unit to 512 B.
        self.inline_files = 0
        # Bitwise OR of the allocated sizes of padded files. Every allocation is
        # a whole number of allocation units, and the unit is a power of two, so
        # the lowest set bit of the OR is the unit -- measured from the tree
        # itself rather than assumed from the vendor. statvfs cannot supply it:
        # on this GPFS it reports the 4 MiB *block* size while files actually
        # allocate in 16 KiB subblocks, a 256x difference in the wrong direction.
        self.alloc_bits = 0
        self.files = 0
        self.dirs = 0
        self.symlinks = 0
        self.hardlinked_inodes = 0  # distinct inodes seen with st_nlink > 1
        self.hardlink_extra_refs = 0  # directory entries suppressed by dedup
        # uid/dev -> (bytes, inodes). Inodes, not directory entries: directories
        # are counted and hard-link duplicates are not, because that is what a
        # "files" quota charges.
        self.by_uid = {}  # type: Dict[int, Tuple[int, int]]
        self.by_dev = {}  # type: Dict[int, Tuple[int, int]]
        self.dir_agg = {}  # type: Dict[str, Entry]
        self.unreadable_dirs = []  # type: List[Tuple[str, str]]
        self.unstatable = 0
        self.recent_files = 0
        self.recent_apparent = 0
        self.recent_size = 0
        # (path, blocks_at_walk_time) so the re-stat pass can compare like with like.
        self.recent_sample = []  # type: List[Tuple[str, int]]
        self.elapsed = 0.0
        self.threads = 0
        self.settle_window = DEFAULT_SETTLE_WINDOW_S
        self.partial = False  # walk was cut short (interrupt / error)
        # True when the walk skipped stat: counts are exact, sizes are absent and
        # hard links are counted once per name rather than once per inode.
        self.count_only = False
        # Names of depth-1 children whose subtree was walked to completion. On a
        # full walk this is all of them; after an interrupt it is the only part of
        # the result that can honestly be reported, because a subtree still in
        # flight has an arbitrary fraction of its contents counted.
        self.finished_tops = set()  # type: Set[str]

    @property
    def complete(self) -> bool:
        """False when anything was skipped, so the total is a floor not a total."""
        return not self.unreadable_dirs and not self.unstatable and not self.partial

    @property
    def alloc_unit(self) -> Optional[int]:
        """The filesystem's allocation unit, measured from the padded files.

        ``None`` when nothing in the tree was padded, which is the honest answer
        rather than a guess: a tree of exactly-block-sized files carries no
        evidence of the unit at all.
        """
        return (self.alloc_bits & -self.alloc_bits) or None

    @property
    def padding(self) -> int:
        """Bytes charged for partly filled allocation units."""
        return self.padded_alloc - self.padded_apparent

    @property
    def alloc_ratio(self) -> Optional[float]:
        """Allocated over apparent. Above 1 the tree costs more than it holds."""
        if not self.apparent:
            return None
        return self.size / float(self.apparent)

    @property
    def inodes(self) -> int:
        """Distinct inodes, which is what a *files* quota counts.

        ``files`` counts directory entries; a hard-linked file has several of
        those and one inode. Subtracting the suppressed references is what makes
        this number comparable to the quota's file count.
        """
        return self.files + self.dirs - self.hardlink_extra_refs

    def top_dirs(self, n: int, key: str = "size", finished_only: bool = False) -> List[Entry]:
        """Reported directories ranked by ``size``, ``files`` or ``density``.

        ``finished_only`` drops entries whose subtree was still being walked,
        which is what an interrupted run must report: a half-counted directory
        placed in a ranking is not a small error, it is the wrong answer.
        """
        aggs = [a for a in self.dir_agg.values() if a.path != self.root]
        if finished_only:
            aggs = [a for a in aggs if self.is_finished(a)]
        if key == "files":
            aggs.sort(key=lambda a: a.inodes, reverse=True)
        elif key == "density":
            # Files per GiB: the "what should I pack" signal. Restricted to
            # subtrees that hold enough inodes to be worth packing, so the
            # ranking is not won by a 4 KiB directory with three files in it.
            floor = max(100, self.inodes // 100)
            aggs = [a for a in aggs if a.inodes >= floor and a.size > 0]
            aggs.sort(key=lambda a: a.inodes / max(a.size / float(1 << 30), 1e-9), reverse=True)
        else:
            aggs.sort(key=lambda a: a.size, reverse=True)
        return aggs[:n]

    def is_finished(self, entry: "Entry") -> bool:
        """Was this entry's whole subtree walked?"""
        if not self.partial:
            return True
        rel = os.path.relpath(entry.path, self.root)
        top = rel.split(os.sep)[0]
        return top in self.finished_tops


def _entry_keys(root: str, rel_parts: Tuple[str, ...], depth: int) -> List[str]:
    """Every reported ancestor of an object, plus the object itself if shallow.

    For an object at ``a/b/c`` with ``depth=2`` this returns ``[a, a/b]``: its
    bytes are charged to both, which is what makes directory totals cumulative.
    For ``a`` it returns ``[a]`` -- the object is its own entry because it sits
    within the reported depth.

    Relative parts are carried on the work queue rather than recovered with
    ``os.path.relpath`` per object, which would be a string operation per inode
    on a million-inode walk.
    """
    n = min(len(rel_parts), depth)
    if n <= 0:
        return []
    keys = []
    acc = root
    for i in range(n):
        acc = acc + os.sep + rel_parts[i]
        keys.append(acc)
    return keys


def walk(
    root: str,
    threads: int = DEFAULT_THREADS,
    depth: int = 2,
    max_dirs_per_sec: float = 0.0,
    settle_window: float = DEFAULT_SETTLE_WINDOW_S,
    one_file_system: bool = False,
    stop: Optional[threading.Event] = None,
    progress: Optional["Progress"] = None,
    count_only: bool = False,
) -> WalkResult:
    """Walk ``root`` and return a :class:`WalkResult`.

    ``threads`` is clamped to :data:`MAX_THREADS`. ``depth`` controls only how
    coarsely per-directory aggregates are *reported*; the walk itself is always
    complete. ``max_dirs_per_sec`` of 0 disables rate limiting.

    ``count_only`` skips ``stat`` entirely and counts directory entries from
    ``getdents`` alone. That is :data:`COUNT_SPEEDUP`\\ **x faster**, on both
    trees it has been measured on -- 782k GPFS inodes at 27.3s against 3.4s, and
    1.69M at 58.7s against 7.1s -- because ``stat`` is ~90% of a normal walk's
    wall time and ``d_type`` already distinguishes a directory from everything
    else. The cost is that there are no sizes and hard links cannot be
    deduplicated, both of which the caller must report rather than paper over.

    One figure, stated once, from :data:`COUNT_SPEEDUP`. This docstring used to
    say "9.1x" while the ``--count`` help text said "8x" for the same claim.
    """
    root = os.path.abspath(root)
    nthreads = max(1, min(int(threads), MAX_THREADS))
    bucket = TokenBucket(max_dirs_per_sec) if max_dirs_per_sec > 0 else None
    stop_ev = stop if stop is not None else threading.Event()

    res = WalkResult(root)
    res.threads = nthreads
    res.count_only = count_only
    res.settle_window = settle_window

    try:
        root_st = os.lstat(root)
    except OSError as exc:
        raise OSError("cannot stat {}: {}".format(root, exc.strerror)) from exc
    if not stat.S_ISDIR(root_st.st_mode):
        raise NotADirectoryError("{} is not a directory".format(root))

    root_dev = root_st.st_dev
    now = time.time()
    recent_cutoff = now - settle_window

    # (abspath, parts relative to root) -- the parts are what make the
    # cumulative ancestor keys cheap to compute.
    queue = deque([(root, ())])  # type: deque
    # Directories queued but not yet fully processed. The walk is done when the
    # queue is empty AND this reaches zero -- a worker holding the last directory
    # may still be about to enqueue children, so an empty queue alone is not
    # termination.
    pending_box = [1]
    # depth-1 name -> directories queued beneath it that have not finished. When
    # a counter reaches zero that whole subtree is final.
    outstanding = {}  # type: Dict[str, int]
    finished_tops = set()  # type: Set[str]
    cv = threading.Condition()

    # Hardlink bookkeeping is global: the same inode may be reached from
    # different directories in different threads, so dedup cannot be thread-local
    # if the count is to be exact.
    seen_links = {}  # type: Dict[Tuple[int, int], int]
    links_lock = threading.Lock()

    merge_lock = threading.Lock()

    def account_root() -> None:
        # du counts the root directory's own inode. In count mode there are no
        # sizes at all, and reporting the root's own 512 bytes as "the size"
        # would be a number with no meaning attached.
        if not count_only:
            res.size += root_st.st_blocks * 512
            res.apparent += root_st.st_size
        res.dirs += 1
        # The root is not itself a reported entry -- it is the total.
        res.by_uid[root_st.st_uid] = (root_st.st_blocks * 512, 1)
        res.by_dev[root_dev] = (root_st.st_blocks * 512, 1)

    def worker(slot_id: int = 0) -> None:
        l_size = l_app = l_files = l_dirs = l_sym = l_unstat = 0
        seen_here = 0
        l_recent = l_recent_app = l_recent_size = 0
        l_extra = 0
        l_padn = l_pada = l_padb = 0
        l_inline = 0
        l_undn = l_unda = l_undb = 0
        l_bits = 0
        l_uid = {}  # type: Dict[int, List[int]]
        l_dev = {}  # type: Dict[int, List[int]]
        # key -> [bytes, files, dirs, is_dir]
        l_agg = {}  # type: Dict[str, List[int]]
        l_unreadable = []  # type: List[Tuple[str, str]]
        l_sample = []  # type: List[Tuple[str, int]]

        # Hot-loop locals. Every one of these is touched once per inode on a
        # million-inode walk, and a global lookup is a dict miss plus a builtins
        # miss each time.
        stop_is_set = stop_ev.is_set
        scandir = os.scandir
        sep = os.sep
        agg_get = l_agg.get
        uid_get = l_uid.get
        dev_get = l_dev.get
        S_IFMT, S_IFDIR, S_IFLNK = 0o170000, 0o040000, 0o120000
        ofs = one_file_system
        cap = _RECENT_SAMPLE_CAP

        while True:
            with cv:
                while not queue and pending_box[0] > 0 and not stop_ev.is_set():
                    cv.wait(0.5)
                if stop_ev.is_set() or (not queue and pending_box[0] == 0):
                    cv.notify_all()
                    break
                d, d_parts = queue.popleft()
            seen_here += 1

            children = []  # type: List[Tuple[str, Tuple[str, ...]]]

            # Every entry in this directory charges the same set of reported
            # ancestors, so resolve their accumulator slots once here rather than
            # rebuilding the key list and re-hashing it per inode.
            ndp = len(d_parts)
            own_level = ndp < depth
            base = []  # type: List[List[int]]
            acc = root
            for i in range(ndp if own_level else depth):
                acc = acc + sep + d_parts[i]
                slot = agg_get(acc)
                if slot is None:
                    slot = l_agg[acc] = [0, 0, 0, 1]
                base.append(slot)
            nbase = len(base)
            b0 = base[0] if nbase == 1 else None
            dsep = d if d.endswith(sep) else d + sep

            if bucket is not None:
                bucket.take()

            k = 0
            try:
                with scandir(d) as it:
                    if count_only:
                        # No stat: d_type from getdents is enough to tell a
                        # directory from anything else, and that is all a count
                        # needs. This is the fast path.
                        for entry in it:
                            k += 1
                            if not (k & 1023) and stop_is_set():
                                break
                            try:
                                isdir = entry.is_dir(follow_symlinks=False)
                            except OSError:
                                l_unstat += 1
                                continue
                            if isdir:
                                name = entry.name
                                children.append((dsep + name, d_parts + (name,)))
                                l_dirs += 1
                                if b0 is not None:
                                    b0[2] += 1
                                else:
                                    for s in base:
                                        s[2] += 1
                                if own_level:
                                    key = dsep + name
                                    slot = agg_get(key)
                                    if slot is None:
                                        slot = l_agg[key] = [0, 0, 0, 1]
                                    slot[2] += 1
                            else:
                                l_files += 1
                                if b0 is not None:
                                    b0[1] += 1
                                else:
                                    for s in base:
                                        s[1] += 1
                                if own_level:
                                    key = dsep + entry.name
                                    slot = agg_get(key)
                                    if slot is None:
                                        slot = l_agg[key] = [0, 0, 0, 0]
                                    slot[1] += 1
                        # NOTE: no `continue` here. It would target the worker's
                        # outer loop and skip the block that decrements the
                        # pending-directory counter, so the walk would never
                        # reach termination and would hang forever.
                        entries = []  # type: Any
                    else:
                        entries = it
                    for entry in entries:
                        # Checked every 1024 entries rather than every entry:
                        # Event.is_set is a Python-level call, and one directory
                        # can hold a million names.
                        k += 1
                        if not (k & 1023) and stop_is_set():
                            break
                        try:
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            l_unstat += 1
                            continue

                        blocks = st.st_blocks * 512
                        mode = st.st_mode
                        ftype = mode & S_IFMT

                        if ftype == S_IFDIR:
                            if ofs and st.st_dev != root_dev:
                                continue
                            name = entry.name
                            children.append((dsep + name, d_parts + (name,)))
                            if b0 is not None:
                                b0[0] += blocks
                                b0[2] += 1
                            else:
                                for s in base:
                                    s[0] += blocks
                                    s[2] += 1
                            if own_level:
                                key = dsep + name
                                slot = agg_get(key)
                                if slot is None:
                                    slot = l_agg[key] = [0, 0, 0, 1]
                                slot[0] += blocks
                                slot[2] += 1
                            l_size += blocks
                            l_app += st.st_size
                            l_dirs += 1
                            # A directory is an inode and a files-quota counts it.
                            uid = st.st_uid
                            slot = uid_get(uid)
                            if slot is None:
                                l_uid[uid] = [blocks, 1]
                            else:
                                slot[0] += blocks
                                slot[1] += 1
                            dev = st.st_dev
                            slot = dev_get(dev)
                            if slot is None:
                                l_dev[dev] = [blocks, 1]
                            else:
                                slot[0] += blocks
                                slot[1] += 1
                            continue

                        if ofs and st.st_dev != root_dev:
                            continue

                        if ftype == S_IFLNK:
                            l_sym += 1

                        l_files += 1

                        if st.st_nlink > 1:
                            ikey = (st.st_dev, st.st_ino)
                            with links_lock:
                                if ikey in seen_links:
                                    seen_links[ikey] += 1
                                    l_extra += 1
                                    continue
                                seen_links[ikey] = 1

                        sz = st.st_size
                        l_size += blocks
                        l_app += sz
                        # Which way this file's allocation went, and by how much.
                        # blocks == 0 is a fast symlink or an empty file and is
                        # evidence of neither direction.
                        if blocks:
                            if blocks < MIN_ALLOC_UNIT:
                                l_inline += 1
                            elif blocks > sz:
                                l_padn += 1
                                l_pada += sz
                                l_padb += blocks
                                l_bits |= blocks
                            elif blocks < sz:
                                l_undn += 1
                                l_unda += sz
                                l_undb += blocks
                        if b0 is not None:
                            b0[0] += blocks
                            b0[1] += 1
                        else:
                            for s in base:
                                s[0] += blocks
                                s[1] += 1
                        if own_level:
                            key = dsep + entry.name
                            slot = agg_get(key)
                            if slot is None:
                                slot = l_agg[key] = [0, 0, 0, 0]
                            slot[0] += blocks
                            slot[1] += 1
                        uid = st.st_uid
                        slot = uid_get(uid)
                        if slot is None:
                            l_uid[uid] = [blocks, 1]
                        else:
                            slot[0] += blocks
                            slot[1] += 1
                        dev = st.st_dev
                        slot = dev_get(dev)
                        if slot is None:
                            l_dev[dev] = [blocks, 1]
                        else:
                            slot[0] += blocks
                            slot[1] += 1

                        if st.st_mtime >= recent_cutoff or st.st_ctime >= recent_cutoff:
                            l_recent += 1
                            l_recent_app += st.st_size
                            l_recent_size += blocks
                            if len(l_sample) < cap:
                                l_sample.append((dsep + entry.name, blocks))
            except OSError as exc:
                l_unreadable.append((d, exc.strerror or "unreadable"))

            if progress is not None:
                # Published per directory rather than per inode: one list write
                # every few hundred entries costs nothing measurable.
                progress.inode_slots[slot_id] = l_files + l_dirs
                progress.dir_slots[slot_id] = seen_here
                progress.current = d

            with cv:
                if children and not stop_ev.is_set():
                    queue.extend(children)
                    pending_box[0] += len(children)
                    for _, cparts in children:
                        outstanding[cparts[0]] = outstanding.get(cparts[0], 0) + 1
                pending_box[0] -= 1
                if d_parts:
                    top = d_parts[0]
                    outstanding[top] -= 1
                    if outstanding[top] == 0:
                        finished_tops.add(top)
                # Wake one thread per directory actually queued. notify_all here
                # woke every idle worker on every one of ~190k directories, and
                # all but one found the queue empty again.
                if children:
                    cv.notify(len(children))
                elif pending_box[0] == 0:
                    cv.notify_all()

        with merge_lock:
            res.size += l_size
            res.apparent += l_app
            res.files += l_files
            res.dirs += l_dirs
            res.symlinks += l_sym
            res.unstatable += l_unstat
            res.hardlink_extra_refs += l_extra
            res.recent_files += l_recent
            res.recent_apparent += l_recent_app
            res.recent_size += l_recent_size
            res.padded_files += l_padn
            res.padded_apparent += l_pada
            res.padded_alloc += l_padb
            res.under_files += l_undn
            res.under_apparent += l_unda
            res.under_alloc += l_undb
            res.inline_files += l_inline
            res.alloc_bits |= l_bits
            res.unreadable_dirs.extend(l_unreadable)
            room = _RECENT_SAMPLE_CAP - len(res.recent_sample)
            if room > 0:
                res.recent_sample.extend(l_sample[:room])
            for uid, (b, f) in l_uid.items():
                pb, pf = res.by_uid.get(uid, (0, 0))
                res.by_uid[uid] = (pb + b, pf + f)
            for dev, (b, f) in l_dev.items():
                pb, pf = res.by_dev.get(dev, (0, 0))
                res.by_dev[dev] = (pb + b, pf + f)
            for kk, (b, f, dcount, dir_flag) in l_agg.items():
                ent = res.dir_agg.get(kk)
                if ent is None:
                    ent = res.dir_agg[kk] = Entry(kk, bool(dir_flag))
                elif dir_flag:
                    ent.is_dir = True
                ent.add(b, f, dcount)

    t0 = time.perf_counter()
    account_root()
    workers = [
        threading.Thread(target=worker, args=(i,), name="rapidu-walk-%d" % i, daemon=True)
        for i in range(nthreads)
    ]
    for w in workers:
        w.start()
    try:
        for w in workers:
            w.join()
    except KeyboardInterrupt:
        stop_ev.set()
        with cv:
            cv.notify_all()
        for w in workers:
            w.join(timeout=5.0)
        res.partial = True
    res.elapsed = time.perf_counter() - t0
    res.hardlinked_inodes = len(seen_links)
    res.finished_tops = finished_tops
    # A depth-1 plain file is complete the moment the root was scanned; only
    # directories can be caught mid-walk. The dirname check is load-bearing: at
    # depth > 1 dir_agg also holds deeper entries, and adding the basename of a
    # file at `a/b` would mark a *different*, still-unfinished top-level
    # directory named `b` as complete.
    for entry in res.dir_agg.values():
        if not entry.is_dir and os.path.dirname(entry.path) == root:
            res.finished_tops.add(os.path.basename(entry.path))
    if progress is not None:
        progress.finished = True
    return res


def _bump(d: Dict[int, List[int]], key: int, size: int, files: int) -> None:
    slot = d.get(key)
    if slot is None:
        d[key] = [size, files]
    else:
        slot[0] += size
        slot[1] += files


# A re-stat taken immediately after the walk cannot observe drift: the blocks
# move over tens of seconds. Below this gap a null result means nothing.
MIN_CONCLUSIVE_GAP_S = 5.0


class SettleCheck:
    """Result of re-stat'ing the recently-modified files after the walk.

    ``drift`` is **signed**, because GPFS moves in both directions. Measured on
    Midway3 scratch, on two different trees:

        4 KiB payload files   du 117 MB -> 655 MB over ~60 s   (5.58x LOW)
        8 KiB payload files   du 1.2 GiB -> 375 MiB over 75 s  (3.3x HIGH)

    The first is delayed allocation, the second is transient over-allocation
    compacting down to the subblock size. Either way the figure a reader sees is
    not the figure the filesystem will settle on, so a checker that only looks
    for growth reports "settled" during a window in which the number is moving
    by hundreds of megabytes.
    """

    def __init__(self) -> None:
        self.checked = 0  # files successfully re-stat'ed
        self.sampled_of = 0  # how many recent files existed, if we sampled
        self.drift = 0  # SIGNED change in allocated blocks since the walk
        self.gone = 0  # files that disappeared between walk and re-stat
        self.window = DEFAULT_SETTLE_WINDOW_S
        self.gap = 0.0  # seconds between the walk reading and the re-stat
        self.ran = False

    @property
    def moved(self) -> bool:
        """True when the re-stat positively observed the tree changing."""
        return self.drift != 0

    @property
    def conclusive(self) -> bool:
        """Can a *null* result from this check be believed?

        Only if the check actually ran and had long enough to see the effect.
        Constraint 1: before believing a null result, ask whether the instrument
        can see the effect at all.
        """
        return self.ran and (self.moved or self.gap >= MIN_CONCLUSIVE_GAP_S)

    @property
    def sampled(self) -> bool:
        """True when the re-stat covered only part of the recent-file set."""
        return self.sampled_of > self.checked + self.gone


def recheck_settling(res: WalkResult, wait: float = 0.0) -> SettleCheck:
    """Re-stat the recent-file sample and report how far the tree has moved.

    On GPFS a freshly written tree does not report its final allocation for tens
    of seconds, in either direction (see :class:`SettleCheck`). ``du`` hands you
    whichever number happens to be current and says nothing about it.

    ``wait`` is the delay before re-stat'ing. **It defaults to 0, which makes a
    null result uninformative** -- blocks move over tens of seconds, so a re-stat
    taken microseconds after the walk will find nothing no matter how unsettled
    the tree is. With ``wait=0`` the caller gets ``conclusive == False`` and must
    report the recent-write warning on its own; pass a real delay to measure the
    drift instead of merely suspecting it.
    """
    chk = SettleCheck()
    chk.window = res.settle_window
    chk.sampled_of = res.recent_files
    if not res.recent_sample:
        # Nothing was written recently, so there is nothing to be unsettled.
        chk.ran = True
        chk.gap = wait
        return chk

    if wait > 0:
        time.sleep(wait)
    chk.ran = True
    chk.gap = wait
    # Compare like with like: only the files actually re-stat'ed contribute to
    # both sides of the subtraction, so a truncated sample cannot manufacture a
    # phantom growth figure.
    before = 0
    after = 0
    for path, blocks_then in res.recent_sample:
        try:
            st = os.lstat(path)
        except OSError:
            chk.gone += 1
            continue
        before += blocks_then
        after += st.st_blocks * 512
        chk.checked += 1
    chk.drift = after - before
    return chk
