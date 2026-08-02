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
from typing import Dict, List, Optional, Tuple  # noqa: F401  (used in `# type:` comments)

# Past this the walk measurably slows down AND the metadata load stops being
# polite. Requests above it are clamped, loudly.
MAX_THREADS = 16
DEFAULT_THREADS = 8

# A file modified this recently may not have its blocks allocated yet on GPFS.
DEFAULT_SETTLE_WINDOW_S = 120.0

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


class Entry:
    """One reported child of the walked tree: a directory or a large file.

    ``size`` is **cumulative** for a directory -- it includes everything beneath
    it, which is what ``du`` reports and what anyone reading a disk-usage list
    expects. An earlier version charged a directory only with the entries
    immediately inside it, so ``notebooks`` read 70.6 MiB against ``du``'s
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


# Backwards-compatible alias; the type grew beyond directories.
DirAgg = Entry


class WalkResult:
    """Everything one walk learned. All byte figures are allocated blocks."""

    def __init__(self, root: str) -> None:
        self.root = root
        self.size = 0  # sum of st_blocks*512, hardlink-deduped
        self.apparent = 0  # sum of st_size, for the sparse/settling diagnosis
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
        self.dir_agg = {}  # type: Dict[str, DirAgg]
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

    @property
    def complete(self) -> bool:
        """False when anything was skipped, so the total is a floor not a total."""
        return not self.unreadable_dirs and not self.unstatable and not self.partial

    @property
    def inodes(self) -> int:
        """Distinct inodes, which is what a *files* quota counts.

        ``files`` counts directory entries; a hard-linked file has several of
        those and one inode. Subtracting the suppressed references is what makes
        this number comparable to the quota's file count.
        """
        return self.files + self.dirs - self.hardlink_extra_refs

    def top_dirs(self, n: int, key: str = "size") -> List[DirAgg]:
        """Reported directories ranked by ``size``, ``files`` or ``density``."""
        aggs = [a for a in self.dir_agg.values() if a.path != self.root]
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
) -> WalkResult:
    """Walk ``root`` and return a :class:`WalkResult`.

    ``threads`` is clamped to :data:`MAX_THREADS`. ``depth`` controls only how
    coarsely per-directory aggregates are *reported*; the walk itself is always
    complete. ``max_dirs_per_sec`` of 0 disables rate limiting.
    """
    root = os.path.abspath(root)
    nthreads = max(1, min(int(threads), MAX_THREADS))
    bucket = TokenBucket(max_dirs_per_sec) if max_dirs_per_sec > 0 else None
    stop_ev = stop if stop is not None else threading.Event()

    res = WalkResult(root)
    res.threads = nthreads
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
    cv = threading.Condition()

    # Hardlink bookkeeping is global: the same inode may be reached from
    # different directories in different threads, so dedup cannot be thread-local
    # if the count is to be exact.
    seen_links = {}  # type: Dict[Tuple[int, int], int]
    links_lock = threading.Lock()

    merge_lock = threading.Lock()

    def account_root() -> None:
        # du counts the root directory's own inode.
        res.size += root_st.st_blocks * 512
        res.apparent += root_st.st_size
        res.dirs += 1
        # The root is not itself a reported entry -- it is the total.
        res.by_uid[root_st.st_uid] = (root_st.st_blocks * 512, 1)
        res.by_dev[root_dev] = (root_st.st_blocks * 512, 1)

    def worker() -> None:
        l_size = l_app = l_files = l_dirs = l_sym = l_unstat = 0
        l_recent = l_recent_app = l_recent_size = 0
        l_extra = 0
        l_uid = {}  # type: Dict[int, List[int]]
        l_dev = {}  # type: Dict[int, List[int]]
        # key -> [bytes, files, dirs, is_dir]
        l_agg = {}  # type: Dict[str, List[int]]
        l_unreadable = []  # type: List[Tuple[str, str]]
        l_sample = []  # type: List[Tuple[str, int]]

        while True:
            with cv:
                while not queue and pending_box[0] > 0 and not stop_ev.is_set():
                    cv.wait(0.5)
                if stop_ev.is_set() or (not queue and pending_box[0] == 0):
                    cv.notify_all()
                    break
                d, d_parts = queue.popleft()

            children = []  # type: List[Tuple[str, Tuple[str, ...]]]

            def charge(parts, blocks, is_file, self_is_dir):
                """Add one object's cost to every reported ancestor, and to
                itself when it sits within the reported depth."""
                keys = _entry_keys(root, parts, depth)
                if not keys:
                    return
                last = len(keys) - 1
                for i, k in enumerate(keys):
                    slot = l_agg.get(k)
                    if slot is None:
                        # The final key is the object itself only when the walk
                        # has not been truncated by the depth limit.
                        own = i == last and len(parts) <= depth
                        slot = l_agg[k] = [0, 0, 0, 0 if (own and is_file) else 1]
                    slot[0] += blocks
                    if is_file:
                        slot[1] += 1
                    else:
                        slot[2] += 1

            if bucket is not None:
                bucket.take()

            try:
                with os.scandir(d) as it:
                    for entry in it:
                        if stop_ev.is_set():
                            break
                        try:
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            l_unstat += 1
                            continue

                        blocks = st.st_blocks * 512
                        mode = st.st_mode

                        if stat.S_ISDIR(mode):
                            if one_file_system and st.st_dev != root_dev:
                                continue
                            child_parts = d_parts + (entry.name,)
                            children.append((entry.path, child_parts))
                            charge(child_parts, blocks, False, True)
                            l_size += blocks
                            l_app += st.st_size
                            l_dirs += 1
                            # A directory is an inode and a files-quota counts it.
                            _bump(l_uid, st.st_uid, blocks, 1)
                            _bump(l_dev, st.st_dev, blocks, 1)
                            continue

                        if one_file_system and st.st_dev != root_dev:
                            continue

                        if stat.S_ISLNK(mode):
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

                        l_size += blocks
                        l_app += st.st_size
                        charge(d_parts + (entry.name,), blocks, True, False)
                        _bump(l_uid, st.st_uid, blocks, 1)
                        _bump(l_dev, st.st_dev, blocks, 1)

                        if st.st_mtime >= recent_cutoff or st.st_ctime >= recent_cutoff:
                            l_recent += 1
                            l_recent_app += st.st_size
                            l_recent_size += blocks
                            if len(l_sample) < _RECENT_SAMPLE_CAP:
                                l_sample.append((entry.path, blocks))
            except OSError as exc:
                l_unreadable.append((d, exc.strerror or "unreadable"))

            with cv:
                if children and not stop_ev.is_set():
                    queue.extend(children)
                    pending_box[0] += len(children)
                pending_box[0] -= 1
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
            for k, (b, f, dcount, isdir) in l_agg.items():
                ent = res.dir_agg.get(k)
                if ent is None:
                    ent = res.dir_agg[k] = Entry(k, bool(isdir))
                elif isdir:
                    ent.is_dir = True
                ent.add(b, f, dcount)

    t0 = time.perf_counter()
    account_root()
    workers = [
        threading.Thread(target=worker, name="slurmdisk-walk-%d" % i, daemon=True)
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
