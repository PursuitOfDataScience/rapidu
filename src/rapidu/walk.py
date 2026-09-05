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

import errno
import os
import stat
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional, Set, Tuple  # noqa: F401  (`# type:` use)

# Walk concurrency. Both numbers are measured, and the honest summary of the
# measurement is that concurrency is worth a great deal on a parallel filesystem
# and is indistinguishable from noise on a high-latency NFS export.
#
# GPFS, one 1.19M-inode tree, three interleaved repetitions on a 6-core login
# node (wall seconds, and the median):
#
#     threads=8    46.86  47.10  45.99   -> 46.86
#     threads=16   36.91  36.62  35.15   -> 36.62      -22% against 8
#     threads=24   33.69  33.70  32.53   -> 33.69       -8% against 16
#
# Monotonic, and tight enough (<5% within each group) to believe. Beyond the cap
# it turns: 32 and 48 measured 33.1s and 35.0s on the same tree.
#
# The 2.14M-inode tree that one sits inside agrees about the default and not
# about the cap (two interleaved repetitions):
#
#     threads=8    79.60  80.60   -> 80.10
#     threads=16   64.01  63.72   -> 63.87      -20% against 8
#     threads=24   63.15  64.11   -> 63.63       -0.4% against 16, i.e. nothing
#
# So 8 -> 16 is the finding: a fifth of the wall time, on both trees, every
# repetition. 16 -> 24 is worth 8% on one tree and nothing on the other, which is
# exactly why it is the cap and not the default -- it is available to anyone who
# measures their own tree and finds it helps, and promised to nobody.
#
# NFS (Isilon export, 31,731 inodes) is a different story, and the first reading
# of it was wrong. One pair of runs looked like a 22% win for 16 threads; three
# interleaved pairs found nothing --
#
#     threads=8    108.9  123.0  112.1   -> 112.1
#     threads=16   112.6  111.7  122.7   -> 112.6
#
# -- 0.5% apart inside a 10% spread. That server is shared, so its run-to-run
# variance is larger than anything thread count does to it.
#
# **Single runs on a shared node do not settle this.** Both wrong readings above
# came from one pair of runs, and a lone `threads=24` measurement of a larger
# tree came back 13% *slower* than 16 while the interleaved series above had it
# 8% faster. Anything that changes these two numbers again wants repetitions,
# interleaved, with the load average written down.
#
# So: 16 is the default, worth 22% where thread count is worth anything and
# costing nothing measurable where it is not -- 8 was leaving that on the table.
# 24 is the cap rather than the default because what it adds is real on one tree,
# zero on another and absent on NFS, and because twenty-four threads of `fstatat`
# from one interactive command is already as much as is polite to ask of a shared
# node's metanode.
#
# This replaces a flat "past 16 the walk measurably slows down (32 threads was
# 31% worse than 16)". Neither half survives measurement: 32 threads is 10%
# *faster* than 16 on GPFS, and on NFS the difference is not resolvable at all. A
# number that is a property of the storage cannot be stated as a property of the
# tool.
#
# None of this is about the accounting. The walk is within 6% of the syscall
# floor -- a threaded walker that does nothing but `scandir` + `fstatat` and count
# runs the same tree in 35.2s against rapidu's 37.4s at 16 threads, with one
# `scandir` per directory and exactly one `fstatat` per entry and no redundancy.
# There is no bookkeeping left to remove, which is why concurrency is the only
# lever on wall time and why these two numbers are worth measuring rather than
# guessing.
MAX_THREADS = 24
DEFAULT_THREADS = 16

# Concurrency for a filesystem whose stats cost nothing to begin with.
#
# Threads exist here to hide latency. Where there is none to hide they are pure
# overhead -- GIL hand-offs and queue traffic buying nothing -- and the cost is
# not small. One 151k-inode tree on page-cached local xfs:
#
#     threads      1       2       4       8      16
#     wall      0.69s   0.96s   2.57s   2.15s   2.34s
#     cpu       0.69s   1.56s   3.83s   3.21s   3.60s
#
# So a threaded walk of local storage is 3.4x the wall time *and* 5.2x the CPU of
# a serial one. That is the opposite of the trade on GPFS, where 16 threads buy
# 21% of the wall time for 30% more CPU. One fixed number cannot serve both: the
# useful range spans 1 to 24.
LOCAL_THREADS = 1

# Filesystem types whose stat is a memory or local-block access. Deliberately a
# short, confident list: everything absent from it -- including every network and
# parallel filesystem, and anything unrecognised -- keeps `DEFAULT_THREADS`, so a
# type nobody here has heard of behaves exactly as it did before.
_LOCAL_FSTYPES = frozenset(
    (
        "tmpfs",
        "ramfs",
        "devtmpfs",
        "rootfs",
        "ext2",
        "ext3",
        "ext4",
        "xfs",
        "btrfs",
        "f2fs",
        "zfs",
        "jfs",
        "vfat",
        "exfat",
        "squashfs",
        "erofs",
        "bcachefs",
    )
)

# Above this, the entries sampled below took long enough that there is latency
# worth hiding and the thread pool earns its keep.
#
# Both signals are required and neither is sufficient. Median `lstat`, measured:
# tmpfs 1.7us, page-cached local xfs 2.0us -- and a *cached* GPFS home 7.2us,
# only 3.5x above local. So latency alone would read a warm parallel filesystem
# as local and cost the 9x that threads are worth there. Filesystem type alone
# would read a *cold* local disk as fast when a cold seek is 100us on NVMe and
# milliseconds on a platter, where threads help as much as they do on GPFS.
# Twenty is in the gap between warm local and any cold device, which is the only
# distinction this number has to make once the type check has run.
_LOCAL_LATENCY_US = 20.0

# How many entries the probe stats. Enough for a median to mean something,
# few enough to be free against a walk that is about to stat millions.
_PROBE_SAMPLE = 24

# A file modified this recently may not have its blocks allocated yet on GPFS.
DEFAULT_SETTLE_WINDOW_S = 120.0

# How long an interrupted walk waits for its workers *in total* before publishing
# what it has. A worker inside `scandir` on a hung mount cannot be woken -- the
# syscall is uninterruptible and no signal reaches it -- so the wait has to end
# somewhere, and ^C has to stay responsive when it does.
STOP_GRACE_S = 5.0

# Stands for "the root directory's own scan" in the per-worker in-flight sets. Not
# a possible depth-1 name: `os.scandir` never yields an empty one.
_ROOT_SCAN = ""

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

# Bound on how many `--one-file-system` skips we name. The *count* is exact
# whatever happens; this only limits the list, and three is all the report shows.
_CROSSED_SAMPLE_CAP = 64

# Age buckets for the cold-data report, in days, youngest first. The last bucket
# is open-ended.
#
# For a full quota "what is big" is not actionable on its own -- the big thing is
# usually the thing being worked on. "What is big *and* has not been touched in a
# year" is the answer, and `st_mtime` is already read for every file to drive the
# settling check and then discarded. This costs one comparison and one adder per
# file, no extra syscall.
# Errnos that mean "the tree changed", not "you may not look".
_VANISHED_ERRNOS = frozenset(
    x for x in (errno.ENOENT, errno.ESTALE, errno.ENOTDIR) if x is not None
)

# Enough to identify what went wrong without holding a path per failure on a
# tree where every stat fails.
_UNSTAT_SAMPLE_CAP = 64

# The same rule for unreadable directories, which had no cap while both of its
# siblings did.  The COUNT is kept exactly (`unreadable_dir_count`); only the
# paths are sampled, and the report never showed more than three of them plus an
# "and N more" that reads off the count.
_UNREADABLE_SAMPLE_CAP = 256

# How many watched (cache-shaped) directories may be tracked individually.
#
# This is the one growing structure in this walk that grows per *entry* and had
# no bound at all -- and it is the biggest of them on a real tree.  Walking a
# conda installation, 1,180,882 files in 128,093 directories, `watched` held
# **28,180 paths and 8.2 MB**, 36% of the walk's whole 35 MB of growth, because
# `WATCHED_DIR_NAMES` contains `__pycache__` and a Python tree has one per
# package.  Nothing downstream needs them individually: `reclaimable_groups`
# sums each pattern and the report prints a handful of examples.
#
# 4096, matching `_RECENT_SAMPLE_CAP`: two orders of magnitude more rows than
# any view shows, and a few hundred more than a home directory has cache
# directories at all, so the ordinary target is unaffected.  The bytes and
# inodes of what does not fit are NOT discarded -- they go to
# `watched_overflow`, so a total stays a total.
#
# **Divided by the thread count, not applied per thread.**  A worker's tallies
# live in a thread-local dict until it exits, so a per-thread cap of 4096 is
# 32,768 paths at the default -t 8 -- and measured, that is the difference
# between the cap saving 3.7 MB and saving 14 MB on the same tree.  A bound
# that scales with a tuning flag is not a bound.
_WATCHED_CAP = 4096

# Never fewer than this per worker, however many workers there are: a cap of two
# would make the reclaim section useless on a 64-thread host for the sake of a
# few kilobytes.
_WATCHED_CAP_MIN_PER_WORKER = 128

# Entries after which the walk's own memory is worth mentioning.
#
# The remaining growth is the breadth-first frontier and the hard-link set, and
# neither can be bounded without changing what the walk measures -- so the
# honest move is the one this codebase already makes for every other bound it
# cannot remove: publish it.  Measured **17-30 bytes of RSS per entry** across
# trees of 23k to 1.3M entries (the spread is hard-link density and frontier
# width, not inode count), so 10M entries is a few hundred MB and 100M is a few
# GB -- and a production scratch filesystem is commonly 10-100M inodes, which is
# exactly what a tool like this gets pointed at.
#
# 5,000,000: about 100-150 MB by the measured rate, which is where a reader
# would want to know before the number gets interesting rather than after.
_MEMORY_NOTE_ENTRIES = 5_000_000

#: Bytes of resident memory per walked entry, measured.  Used only to put a
#: figure on the note above; deliberately the top of the observed range.
_BYTES_PER_ENTRY = 30

AGE_BUCKET_DAYS = (7, 30, 90, 365)
AGE_BUCKET_LABELS = ("< 7d", "7-30d", "30-90d", "90d-1y", "> 1y")

# Directory *names* worth accumulating a subtree total for, wherever they appear
# and however deep. `depth` controls the reported breakdown, so `dir_agg` holds
# only depth-1 entries by default -- and every cache worth naming sits three or
# four levels down (`~/.cache/huggingface/hub`, `~/.conda/pkgs`). A detector
# reading `dir_agg` therefore finds nothing on a default run, which is how this
# feature would have shipped looking implemented and doing nothing.
#
# Basenames only, deliberately over-broad: `pip` matches any directory called
# pip, and `report._reclaimable_match` does the real filtering on the full path.
# Cheap to be generous here (a set lookup per path component per directory) and
# expensive to be wrong in the other direction.
WATCHED_DIR_NAMES = frozenset(
    (
        "pkgs",
        "pip",
        "uv",
        "huggingface",
        "hub",
        "torch",
        "cache",
        "ComputeCache",
        "Trash",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "objects",
        "wandb",
        "mlruns",
        "lightning_logs",
        "runtime",
    )
)


class _NoEntries:
    """An empty stand-in for a ``ScandirIterator``, for a directory never opened.

    Used when the walk was stopped while the rate limiter held this directory:
    the bookkeeping at the end of the loop body still has to run -- it releases
    the directory's pending count, and skipping it deadlocks the walk -- so the
    scan is emptied rather than jumped over.
    """

    def __enter__(self) -> Tuple:
        return ()

    def __exit__(self, *exc: Any) -> None:
        # None, not False: `typing.Literal` is the alternative mypy accepts here
        # and it does not exist on the 3.6 floor this package still runs on.
        return None


_NO_ENTRIES = _NoEntries()


def _seed_watch(
    slots: Dict[str, List[int]],
    path: str,
    blocks: int,
    overflow: Optional[List[int]] = None,
    cap: int = _WATCHED_CAP,
) -> None:
    """Charge a watched directory's own inode to its own subtree total.

    A worker resolves the *watched ancestors* of every directory it scans, so a
    cache directory's contents are charged to it -- but nothing ever charged the
    directory itself, because at the moment its own name is recognised the slot
    it belongs to is the parent's, not its own. Called from the parent's scan,
    where both its inode and its allocated blocks are known.

    ``slots`` is a thread-local dict merged under the lock at the end, so a
    directory discovered by one worker and scanned by another still sums.

    ``overflow`` is where the charge goes once ``slots`` is full -- see
    :data:`_WATCHED_CAP`.  It is a *slot*, not a flag, so the bytes and inodes
    are still summed exactly; only the path is given up.  Passing ``None``
    disables the cap, which is what the unit tests of this function want.
    """
    slot = slots.get(path)
    if slot is None:
        if overflow is not None and len(slots) >= cap:
            overflow[0] += blocks
            overflow[1] += 1
            return
        slot = slots[path] = [0, 0]
    slot[0] += blocks
    slot[1] += 1


class TokenBucket:
    """Rate limiter over directory opens. Disabled when ``rate <= 0``."""

    def __init__(self, rate: float, burst: Optional[float] = None) -> None:
        self.rate = float(rate)
        self.capacity = float(burst if burst is not None else max(rate, 1.0))
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def take(self, stop: Optional["threading.Event"] = None) -> bool:
        """Wait for one token. False when ``stop`` was set before one arrived.

        **The stop event is checked, because ``--max-dirs-per-sec`` is a
        deliberate way to park a worker for a long time.** At 0.2 dirs/sec a
        thread sits here for seconds per directory, and a version of this loop
        that only slept could not be woken: after ^C the walk's bounded join
        expired with workers still queued for a token, and they were abandoned
        holding measurements the report then had to do without. A rate limit is
        the one place in the walk where the blocking is ours to interrupt, so it
        is interruptible.
        """
        if self.rate <= 0:
            return True
        while True:
            if stop is not None and stop.is_set():
                return False
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                deficit = (1.0 - self._tokens) / self.rate
            if stop is not None:
                # Woken by the stop event rather than by the clock, so a ^C is
                # felt immediately instead of up to 0.25s later.
                if stop.wait(min(deficit, 0.25)):
                    return False
            else:
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
        # Files the filesystem allocated *nothing* for while they still hold
        # data: the bytes live in the inode. Counted apart from both classes above
        # because it is neither padding nor sparseness.
        #
        # This used to mean "allocated less than 4 KiB", which is a different
        # claim and a false one -- such a file has blocks, and the gap between its
        # data and those blocks is padding. Keeping small allocations out of the
        # *unit* estimate is still necessary and is done at that site instead.
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
        # Non-directory entries that are neither regular files nor symlinks:
        # sockets, fifos, block and character devices. They are counted in
        # `files` like any other non-directory entry -- which is what an inode
        # quota charges -- but naming them lets the breakdown that explains
        # `inodes` keep the term "files" meaning files. Without it a home
        # directory holding one ssh ControlMaster socket printed one more
        # "file" than `find -type f` could find, with nothing accounting for
        # the difference. Zero unless the tree has any, so the term is absent
        # from almost every report. `-c` cannot fill this in: it never stats.
        self.specials = 0
        self.hardlinked_inodes = 0  # distinct inodes seen with st_nlink > 1
        self.hardlink_extra_refs = 0  # directory entries suppressed by dedup
        # uid/dev -> (bytes, inodes). Inodes, not directory entries: directories
        # are counted and hard-link duplicates are not, because that is what a
        # "files" quota charges.
        self.by_uid = {}  # type: Dict[int, Tuple[int, int]]
        self.by_dev = {}  # type: Dict[int, Tuple[int, int]]
        # A group quota is charged by gid, not uid, and the two diverge exactly
        # when it matters: a file written into a shared project directory whose
        # setgid bit is missing lands in the writer's personal group, so it is
        # charged somewhere nobody is looking. `render_walk` already says "a group
        # quota charges all of these" over the *uid* table, which answers a
        # question nobody asked.
        self.by_gid = {}  # type: Dict[int, Tuple[int, int]]
        # (bytes, *files*) per `AGE_BUCKET_LABELS` entry, by mtime -- every
        # non-directory entry, symlinks included, hard-link duplicates suppressed
        # only, deliberately: a directory's mtime tracks its contents changing, so
        # bucketing it would count the same event twice.
        #
        # The second element is a **file** count, not an inode count, and it used
        # to be commented as `(bytes, inodes)`. The name drifted from the quantity
        # here, again where `report` unpacked it, and again in the `--json` key, and
        # only the terminal's format string ("N files") restored the truth. A
        # consumer summing the JSON's `by_age[].inodes` against the document's
        # `inodes` was short by exactly `dirs` -- 42% on a config directory.
        self.by_age = [(0, 0)] * len(AGE_BUCKET_LABELS)  # type: List[Tuple[int, int]]
        # Subtree totals for directories named in `WATCHED_DIR_NAMES`, at any
        # depth. Kept apart from `dir_agg` on purpose: these are deeper than the
        # reported depth, and letting them into `dir_agg` would put nested rows in
        # a ranking that is supposed to partition the tree, and break the
        # remainder row that depends on that.
        #
        # (bytes, inodes) with the same meaning `Entry` gives them -- directories
        # included, the watched directory itself included -- so the RECLAIMABLE
        # table's columns mean one thing whichever of the two sources a row came
        # from. It is populated on *both* walk paths: `-c` has no bytes and leaves
        # those at zero (the report prints `n/a`), but its inode counts are exact
        # and are what that mode ranks on.
        self.watched = {}  # type: Dict[str, Tuple[int, int]]
        # ``(blocks, inodes)`` charged to watched directories that did not fit
        # `_WATCHED_CAP`, so no path could be attached to them.
        #
        # A bound that drops data silently is worse than no bound: a reclaim
        # figure is acted on, and one that quietly excludes 24,000 directories
        # is a wrong number rather than a partial one. The paths are what cost
        # memory, so the paths are what is given up; the bytes and inodes are
        # summed exactly and the report says so.
        self.watched_overflow = (0, 0)  # type: Tuple[int, int]
        # How many watched directories the cap gave up the path of.
        #
        # `watched_seen` is derived from this rather than stored, for the same
        # reason as `unreadable_dirs_dropped`: a hand-built result -- a fixture,
        # or a caller assembling one -- sets `watched` and nothing else, and a
        # stored total would then contradict it.
        self.watched_dropped = 0
        self.dir_agg = {}  # type: Dict[str, Entry]
        # Sampled to `_UNREADABLE_SAMPLE_CAP`; `unreadable_dir_count` is exact.
        self.unreadable_dirs = []  # type: List[Tuple[str, str]]
        # How many unreadable directories the sampling cap discarded the path of.
        #
        # The count is what is load-bearing -- `complete`, the `! this is a
        # FLOOR, not a total: N dirs unreadable` line, the
        # `refused = count - vanished` arithmetic and `--json` all read it -- so
        # it is DERIVED from the list plus this, rather than being a second field
        # to keep in step. A pair of fields that must agree is a pair that
        # eventually does not: every caller that appends a path (including a
        # test building a fixture) gets the right count for free this way.
        self.unreadable_dirs_dropped = 0
        # How many of those were *gone* rather than refused. A directory deleted
        # between being listed and being opened is not a permissions problem, and
        # calling it one sends the reader to chase access they already have: on a
        # shared filesystem the usual cause is another job -- often their own --
        # writing to the tree while it is walked. Counted separately rather than
        # widened into the tuple above, so every existing consumer of
        # `unreadable_dirs` keeps its count, its paths and its `complete`.
        #
        # Classified from `errno`, never from the message: `strerror` is localised,
        # so matching "No such file or directory" would silently stop working on a
        # host with a non-English locale.
        self.vanished_dirs = 0
        # The same two questions for entries as for directories, which only the
        # directory half could answer. `unstatable` was a bare count with no cause
        # and no paths: a file deleted mid-walk and one that may not be stat'ed
        # were the same number, and "40 entries unstatable" named none of them, so
        # there was nothing to act on. `unreadable_dirs` has carried its paths from
        # the start -- *"a consumer that knows three directories were unreadable
        # cannot act on it; one that knows which can"* -- and this is that same
        # rule, applied to the sibling counter.
        self.vanished_entries = 0
        self.unstatable_paths = []  # type: List[str]
        self.unstatable = 0
        # What `--one-file-system` refused, because it sat on another filesystem.
        #
        # This is a cap the tool applies at the user's request, and it was applied
        # in complete silence: `rdu -x /scratch` on a cluster whose /scratch holds
        # three cluster filesystems reported `0 B - 1 files` and nothing else,
        # which reads as "/scratch is empty". Every other bound in this walk is
        # published -- unreadable directories, unstatable entries, an interrupt --
        # so this one is too. `complete` deliberately does *not* consider it: the
        # walk did exactly what it was asked, and a requested scope is not a
        # failure. It is the total's meaning that changes, not its validity.
        self.crossed = 0
        self.crossed_paths = []  # type: List[str]
        # Whether `-x` was in force, which `crossed` does not answer: a bounded
        # walk that had nothing to skip reports 0 there, the same as an unbounded
        # one. Anything that reproduces this walk -- notably the `find` command
        # `BY AGE` prints -- has to know, or it enumerates a different population
        # from the one the report counted.
        self.one_file_system = False
        # Files whose *data* changed inside the settle window: `st_mtime`, which
        # is the signal this window was defined for ("a file modified this
        # recently may not have its blocks allocated yet").
        self.recent_files = 0
        # Files whose *inode* changed inside the window without being written --
        # `st_ctime` new, `st_mtime` old. Counted apart because the two cannot be
        # reported as the same thing: `chmod -R`, a `chgrp` to share a directory,
        # or the `utime` pass at the end of `tar -x` bumps ctime on every file in
        # a tree without touching one block, and folding those into
        # `recent_files` made the report state "N files were written in the last
        # 120s and their blocks may not be final" about a tree nobody had written
        # to. The trigger still fires on them -- a delayed allocation completing
        # also bumps ctime alone, and that genuinely does move `st_blocks` -- but
        # which of the two it was is not knowable from a stat, so the report names
        # both instead of asserting the write.
        self.touched_files = 0
        # Files whose mtime is *ahead* of this node's clock. Impossible as an age,
        # ordinary as an observation: a client clock behind the fileserver's
        # stamps every write in the future, and a restored archive carries
        # whatever timestamps it was built with. They are inside any window
        # forever, so counting them as "just written" makes a tree permanently
        # unsettled for a reason that is not true.
        self.future_files = 0
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
        # Worker threads still blocked when an interrupted walk stopped waiting
        # for them. Their measurements were discarded (see `walk`), so this is not
        # a diagnostic detail -- it is the reason the figures below are lower than
        # what the walk had already counted, and the report says so.
        self.abandoned_workers = 0

    @property
    def complete(self) -> bool:
        """False when anything was skipped, so the total is a floor not a total."""
        return not self.unreadable_dir_count and not self.unstatable and not self.partial

    @property
    def unreadable_dir_count(self) -> int:
        """Every unreadable directory, counted -- paths sampled or not."""
        return len(self.unreadable_dirs) + self.unreadable_dirs_dropped

    @property
    def watched_seen(self) -> int:
        """Every watched directory the walk saw, tracked individually or not."""
        return len(self.watched) + self.watched_dropped

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
    def unit_padding_ceiling(self) -> Optional[int]:
        """The most of :attr:`padding` that partly filled units could account for.

        A file allocated in whole units of ``u`` is charged ``ceil(size/u)*u``,
        so its own padding is at most ``u - 1``; across ``padded_files`` files
        the whole class is bounded by ``padded_files * (u - 1)``. ``None`` where
        no unit could be measured, which is the same condition under which the
        report already drops the unit from its sentence.

        It exists because ``padding`` *above* this bound cannot be a partly
        filled unit, and the two causes have different remedies. Unit padding is
        returned by packing the files; a per-byte overhead -- replication,
        erasure coding, per-block checksums -- is charged on the archive too, and
        packing returns none of it.

        Measured on an NFS-exported OneFS home: 29,132 padded files, an 8 KiB
        measured unit, and 1021.3 MiB of padding against a 227.6 MiB ceiling --
        4.5x what the stated cause can produce. The report said "so they occupy
        1.0 GiB of padding. Packing them ... returns it", which was a mechanism
        its own two other figures refuted. On this filesystem files up to 128 KiB
        all report 24 KiB allocated and a 4 MiB file reports 1.26x its length, so
        the gap scales with the bytes stored and survives any repacking.

        The GPFS trees the packing advice was written for are unaffected. A
        1.2M-inode one measures 711,302 padded files against a 16 KiB subblock,
        4.7 GiB of padding under a 10.9 GiB ceiling; the shape the panel actually
        prints for -- half a million 2 KiB files each paying for a whole subblock
        -- sits at 6.7 GiB under 7.6 GiB, because that padding really is the
        remainder and packing really does return it.
        """
        unit = self.alloc_unit
        if not unit or not self.padded_files:
            return None
        return self.padded_files * (unit - 1)

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

    @property
    def density_floor(self) -> int:
        """Inodes a subtree needs before it may enter a density ranking.

        Exposed rather than kept local to :meth:`top_dirs` because a filter that
        can empty a whole table has to be nameable by whatever prints "nothing
        here met it" -- an empty table with no explanation is the same failure as
        a wrong one.
        """
        return max(100, self.inodes // 100)

    def top_dirs(self, n: int, key: str = "size", finished_only: bool = False) -> List[Entry]:
        """Reported directories ranked by ``size``, ``files`` or ``density``.

        ``finished_only`` drops entries whose subtree was still being walked,
        which is what an interrupted run must report: a half-counted directory
        placed in a ranking is not a small error, it is the wrong answer.

        **A stat-free walk cannot be ranked by bytes.** Every ``size`` is zero
        after ``-c``, and sorting an all-zero key is not a ranking: it returns
        dict insertion order, which is thread merge order, which changes from run
        to run. Six consecutive ``rdu -c`` runs on one tree produced four
        different orderings, and at ``-n 3`` the second-largest directory in the
        tree was hidden behind "2 more". So the key falls back to the one
        measurement the walk actually has, rather than silently ranking by a
        column of zeroes.

        **Ties are broken deterministically**, on the other measurement and then
        on the path. Python's sort is stable, so without a secondary key equal
        entries kept ``dir_agg`` insertion order -- which is the order worker
        threads took ``merge_lock``, and therefore different on every run. Ties
        are not exotic: every directory whose contents round to one allocation
        unit lands on the same byte figure, and ``--sort files`` ties more
        readily still. That made two reports of an unchanged tree fail to
        ``diff``, and under ``-n`` it changed *which* entries were listed rather
        than only their order. ``path`` is an absolute path and unique within
        ``dir_agg``, so the ordering it completes is total.
        """
        if self.count_only and key in ("size", "density"):
            key = "files"
        aggs = [a for a in self.dir_agg.values() if a.path != self.root]
        if finished_only:
            aggs = [a for a in aggs if self.is_finished(a)]
        # The path tiebreaker ascends while the metrics descend, so it is applied
        # as a separate stable pre-sort rather than negated inside the key -- a
        # string has no negation, and `reverse=True` would otherwise order tied
        # rows z-to-a.
        aggs.sort(key=lambda a: a.path)
        if key == "files":
            aggs.sort(key=lambda a: (a.inodes, a.size), reverse=True)
        elif key == "density":
            # Files per GiB: the "what should I pack" signal. Restricted to
            # subtrees that hold enough inodes to be worth packing, so the
            # ranking is not won by a 4 KiB directory with three files in it.
            floor = self.density_floor
            aggs = [a for a in aggs if a.inodes >= floor and a.size > 0]
            aggs.sort(
                key=lambda a: (a.inodes / max(a.size / float(1 << 30), 1e-9), a.inodes),
                reverse=True,
            )
        else:
            aggs.sort(key=lambda a: (a.size, a.inodes), reverse=True)
        return aggs[:n]

    def is_finished(self, entry: "Entry") -> bool:
        """Was this entry's whole subtree walked?"""
        if not self.partial:
            return True
        rel = os.path.relpath(entry.path, self.root)
        top = rel.split(os.sep)[0]
        return top in self.finished_tops


def _fstype_of(path: str, table: str = "/proc/mounts") -> str:
    """The filesystem type of the mount that holds ``path``, or ``""``.

    Longest matching mount point wins, which is the only reading that works
    where mounts nest -- an autofs `/home` with one NFS mount per user under it
    is an ordinary layout, and the answer there is the user's own mount, not the
    map above it.

    Read here rather than borrowed from :mod:`rapidu.quota`, which has a richer
    version of the same loop: this module is a leaf, and importing the quota
    layer to answer one question would pull `subprocess` and `socket` into every
    `-c` walk that never asks a quota anything. The duplication is twelve lines
    and the alternative is a dependency edge.
    """
    target = os.path.abspath(path).rstrip(os.sep) or os.sep
    best, best_type = "", ""
    try:
        with open(table, "rb") as handle:
            for raw in handle:
                fields = raw.decode("utf-8", "replace").split()
                if len(fields) < 3:
                    continue
                # The kernel octal-escapes mount points; a space or a tab in one
                # is unusual and entirely legal.
                point = (
                    fields[1]
                    .replace("\\040", " ")
                    .replace("\\011", "\t")
                    .replace("\\012", "\n")
                    .replace("\\134", "\\")
                )
                stem = point.rstrip(os.sep) or os.sep
                covers = target == stem or target.startswith(
                    stem if stem.endswith(os.sep) else stem + os.sep
                )
                if covers and len(stem) >= len(best):
                    best, best_type = stem, fields[2]
    except (OSError, UnicodeDecodeError):
        return ""
    return best_type


def _probe_latency_us(root: str, sample: int = _PROBE_SAMPLE) -> Optional[float]:
    """Median ``lstat`` cost in microseconds over a few entries of ``root``.

    ``None`` when there is nothing to measure -- an empty or unreadable
    directory -- which the caller treats as "no evidence" rather than as fast.

    The entries are re-stat'd by the walk moments later, so on any filesystem
    that caches metadata this probe is paid back rather than added.
    """
    try:
        with os.scandir(root) as it:
            paths = []
            for entry in it:
                paths.append(entry.path)
                if len(paths) >= sample:
                    break
    except OSError:
        return None
    # `perf_counter`, not `perf_counter_ns`: the nanosecond variant arrived in
    # 3.7 and this package's floor is 3.6, which the suite checks by running the
    # whole tool under the system interpreter. `perf_counter` is sub-microsecond
    # on Linux, and the threshold it feeds is twenty.
    timings = []
    for path in paths:
        started = time.perf_counter()
        try:
            os.lstat(path)
        except OSError:
            continue
        timings.append((time.perf_counter() - started) * 1e6)
    if not timings:
        return None
    timings.sort()
    middle = len(timings) // 2
    if len(timings) % 2:
        return timings[middle]
    return (timings[middle - 1] + timings[middle]) / 2.0


def choose_threads(root: str, requested: Optional[int] = None) -> int:
    """How many workers to walk ``root`` with.

    ``requested`` is honoured whenever it is given -- an explicit ``-t`` is a
    decision, not a hint -- clamped to :data:`MAX_THREADS` as always. Only the
    unset case is chosen here.

    **The choice only ever goes down, and only on two agreeing signals.** A
    filesystem type from :data:`_LOCAL_FSTYPES` says stats are local; a median
    probe under :data:`_LOCAL_LATENCY_US` says they are *actually* cheap right
    now rather than merely capable of being. Both, and the walk runs serially
    for a third of the wall time and a fifth of the CPU. Either missing --
    unrecognised type, cold device, empty directory, unreadable `/proc/mounts` --
    and it is :data:`DEFAULT_THREADS`, exactly as before. Every way this can be
    wrong is the previous behaviour.
    """
    if requested is not None:
        return max(1, min(int(requested), MAX_THREADS))
    if _fstype_of(root) not in _LOCAL_FSTYPES:
        return DEFAULT_THREADS
    latency = _probe_latency_us(root)
    if latency is None or latency > _LOCAL_LATENCY_US:
        return DEFAULT_THREADS
    return LOCAL_THREADS


def walk(
    root: str,
    threads: Optional[int] = None,
    depth: int = 2,
    max_dirs_per_sec: float = 0.0,
    settle_window: float = DEFAULT_SETTLE_WINDOW_S,
    one_file_system: bool = False,
    stop: Optional[threading.Event] = None,
    progress: Optional["Progress"] = None,
    count_only: bool = False,
) -> WalkResult:
    """Walk ``root`` and return a :class:`WalkResult`.

    ``threads`` is clamped to :data:`MAX_THREADS`; ``None`` asks
    :func:`choose_threads` to pick, which is the default because the right number
    is a property of the filesystem and spans 1 to 24. ``depth`` controls only how
    coarsely per-directory aggregates are *reported*; the walk itself is always
    complete. ``max_dirs_per_sec`` of 0 disables rate limiting.

    ``count_only`` skips ``stat`` entirely and counts directory entries from
    ``getdents`` alone. That is :data:`COUNT_SPEEDUP`\\ **x faster on GPFS**, on
    both trees it has been measured on -- 782k inodes at 27.3s against 3.4s, and
    1.69M at 58.7s against 7.1s -- because ``stat`` is ~90% of a *parallel
    filesystem* walk's wall time and ``d_type`` already distinguishes a directory
    from everything else. The cost is that there are no sizes and hard links
    cannot be deduplicated, both of which the caller must report rather than
    paper over.

    **The ratio is a property of the filesystem, not of this walker,** and naming
    GPFS is load-bearing rather than decorative. Re-measured across three trees:
    8.5x on a large GPFS one, 2.1x on a page-cached local one, 1.6x on a small
    warm GPFS one. Anything derived from :data:`COUNT_SPEEDUP` must therefore say
    where it was measured and must not predict a runtime for the filesystem in
    front of the user -- doing that was out by -74% and -80% on two of the three.

    Two further limits on the fast path, both real and neither yet measurable
    here:

    * ``entry.is_dir(follow_symlinks=False)`` is answered from ``d_type`` only
      where the filesystem fills it in. On one that returns ``DT_UNKNOWN`` -- XFS
      formatted with ``ftype=0``, some NFS exports without readdirplus, a few
      FUSE layers -- CPython falls back to a real ``stat`` per entry, so ``-c``
      costs about what a full walk costs while still reporting no sizes and no
      hard-link dedup: strictly worse than not passing it. Every filesystem
      reachable from this cluster fills ``d_type`` in, so the fallback has not
      been observed here, only reasoned about.
    * ``one_file_system`` costs one ``lstat`` per *directory* on this path, since
      a child cannot change filesystem unless it is itself a mount point. That is
      a few percent of inodes and keeps the flag honest; before, it was accepted
      and silently ignored.

    **An interrupted walk publishes a snapshot, not a live object.** ``stop`` and
    ^C cannot reach a worker blocked inside ``scandir`` on a hung mount, so after
    :data:`STOP_GRACE_S` the walk stops waiting and shuts the merge door: threads
    that are still running can no longer write to the returned
    :class:`WalkResult`, and every depth-1 subtree they took work from is excluded
    from ``finished_tops``. The cost is real and is reported rather than hidden --
    ``abandoned_workers`` counts the threads whose tallies were dropped, so the
    reader knows the figures are lower than what the walk had already counted.

    Memory grows with the tree and is not bounded. Measured at 19-35 bytes of RSS
    per inode, but the spread is the point: the per-inode figure is a property of
    hard-link density and frontier width, not of inode count, so it does not
    extrapolate. The growing structures are the breadth-first ``queue``
    (which can hold one whole level of a wide tree), ``seen_links`` (one entry per
    multiply-linked inode -- 8.6% of a conda env, near zero for a checkpoint
    tree), and ``dir_agg``, which holds one :class:`Entry` per *reported* object:
    at the default depth that is one per top-level child, but a single directory
    holding a million files costs a million ``Entry`` objects, which is exactly
    the "too many inodes" case this tool is reached for.

    ``watched`` used to be a fourth and this list did not mention it, which is
    how it came to be the largest: 28,180 paths and 8.2 MB on a conda tree,
    36% of that walk's growth, because `WATCHED_DIR_NAMES` holds ``__pycache__``
    and a Python installation has one per package. It is now capped at
    `_WATCHED_CAP` with the excess totalled in ``watched_overflow``, and
    ``unreadable_dirs`` -- which had no cap while both its siblings did -- is
    sampled to `_UNREADABLE_SAMPLE_CAP` behind an exact
    ``unreadable_dir_count``. Both bounds are published in the report rather
    than applied quietly: a truncation nobody is told about reads as a total.
    """
    root = os.path.abspath(root)
    # `None` means "choose": see `choose_threads`. An explicit count is honoured.
    nthreads = choose_threads(root, threads)
    bucket = TokenBucket(max_dirs_per_sec) if max_dirs_per_sec > 0 else None
    stop_ev = stop if stop is not None else threading.Event()

    res = WalkResult(root)
    res.threads = nthreads
    # See `_WATCHED_CAP`: the bound is on the whole walk, so each of `nthreads`
    # thread-local dicts gets a share of it rather than the whole thing.
    watch_cap = max(_WATCHED_CAP_MIN_PER_WORKER, _WATCHED_CAP // max(1, nthreads))
    # Distinct watched directories DISCOVERED, summed over the workers.  Exact:
    # `_seed_watch` runs once per watched directory across the whole walk (from
    # its parent's scan; the root is never watched, because ancestor resolution
    # starts below it), whereas a path can be created in several workers'
    # thread-local dicts and so cannot be counted there.
    seen_box = [0]
    res.count_only = count_only
    res.one_file_system = one_file_system
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
    # depth-1 names beneath which the walk abandoned work when it was stopped, or
    # whose own directory scan was cut short. Subtracted from `finished_tops` at
    # the end: a counter reaching zero proves only that nothing is *outstanding*,
    # not that everything was done.
    abandoned_tops = set()  # type: Set[str]
    cv = threading.Condition()

    # Hardlink bookkeeping is global: the same inode may be reached from
    # different directories in different threads, so dedup cannot be thread-local
    # if the count is to be exact.
    seen_links = {}  # type: Dict[Tuple[int, int], int]
    links_lock = threading.Lock()

    merge_lock = threading.Lock()
    # The merge door. An interrupted walk stops waiting for workers that are
    # blocked in an uninterruptible `scandir` (a hung mount is the case that
    # matters), and those threads stay live. Before this existed they went on to
    # merge into `res` minutes after `walk` had returned it: measured on a hung
    # fixture, the caller was handed 2.3 MiB / 601 files and the *same object*
    # read 8.3 MiB / 1,600 files thirty seconds later, with the renderer already
    # iterating `dir_agg` while it grew. `deleted.scan` bounds exactly this hazard
    # by snapshotting its results; the walk now does the same, by shutting the
    # door under the lock every merge already takes.
    #
    # [0] closed, [1] how many workers never got through it.
    door = [False, 0]
    merged = [False] * nthreads
    # Per worker, the depth-1 subtrees it has taken work from. A worker's tallies
    # live in thread locals until it exits, so if it never merges, every subtree
    # it touched is missing an unknown fraction of its contents -- which is
    # precisely what `finished_tops` promises cannot happen.
    inflight = [set() for _ in range(nthreads)]  # type: List[Set[str]]

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
        res.by_gid[root_st.st_gid] = (root_st.st_blocks * 512, 1)

    def worker(slot_id: int = 0) -> None:
        l_size = l_app = l_files = l_dirs = l_sym = l_unstat = 0
        l_spec = 0
        l_vanished = 0
        l_vanished_entries = 0
        l_unstat_paths = []  # type: List[str]

        def unstatable(exc, path):
            """Record one failed stat: where it was, and whether it had vanished.

            Only ever reached from an `except` block, so the cost sits on the
            failure path and the hot loop is untouched. Returns 1 when the cause
            was the entry going away rather than a permission -- classified from
            `errno`, because `strerror` is localised.
            """
            if len(l_unstat_paths) < _UNSTAT_SAMPLE_CAP:
                l_unstat_paths.append(path)
            return 1 if exc.errno in _VANISHED_ERRNOS else 0

        l_crossed = 0
        l_crossed_paths = []  # type: List[str]
        seen_here = 0
        l_recent = l_recent_app = l_recent_size = 0
        l_touched = l_future = 0
        l_extra = 0
        l_padn = l_pada = l_padb = 0
        l_inline = 0
        l_undn = l_unda = l_undb = 0
        l_bits = 0
        l_uid = {}  # type: Dict[int, List[int]]
        l_dev = {}  # type: Dict[int, List[int]]
        l_gid = {}  # type: Dict[int, List[int]]
        l_age = [[0, 0] for _ in AGE_BUCKET_LABELS]
        l_watch = {}  # type: Dict[str, List[int]]
        # Where a watched directory's blocks go once `l_watch` is full. A slot
        # rather than a counter pair so the same `[blocks, inodes]` writes that
        # a real slot takes work unchanged -- `watch` below can hold it in place
        # of a missing slot and the hot loop needs no branch.
        l_watch_over = [0, 0]  # type: List[int]
        # Every watched directory this worker DISCOVERED, tracked or not.
        #
        # Exact, and the only exact count available: `_seed_watch` runs once per
        # watched directory across the whole walk (from its parent's scan, and
        # the root is never watched because ancestor resolution starts below it),
        # whereas a path can be created in several workers' `l_watch` and so
        # cannot be counted there.
        l_watch_seen = 0
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
        # "" when root is "/", so ancestor keys and child keys agree. See #21.
        root_stem = root.rstrip(sep) if root != sep else ""
        agg_get = l_agg.get
        uid_get = l_uid.get
        dev_get = l_dev.get
        gid_get = l_gid.get
        # Cutoffs as absolute epoch seconds, computed once: comparing against
        # these is one float compare per file rather than an arithmetic per file.
        age_cutoffs = [now - days * 86400.0 for days in AGE_BUCKET_DAYS]
        # Read again -- and only -- when a file's mtime is ahead of `now`. See the
        # `l_future` branch below: the window's cutoffs must stay fixed for the
        # whole walk, but "ahead of this node's clock" is a live question.
        wall_clock = time.time
        n_buckets = len(l_age)
        watch_names = WATCHED_DIR_NAMES
        watch_get = l_watch.get
        S_IFMT, S_IFDIR, S_IFLNK = 0o170000, 0o040000, 0o120000
        S_IFREG = 0o100000
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
                # Recorded under `cv`, which this already holds, and never
                # cleared: a worker merges once, at exit, so until then every
                # subtree in here has tallies that exist nowhere else. No worker
                # reaches this line after `stop_ev` is set, so the main thread can
                # read these sets as final once it has stopped the walk.
                #
                # `_ROOT_SCAN` for the root itself, because the root's scan is what
                # charges every depth-1 child with its *own* inode and its own
                # blocks -- `d_parts` is empty there, so keying by `d_parts[0]`
                # recorded nothing and a stranded root scanner looked harmless. It
                # is not: CI caught a `fast_3` ranked at 40 inodes instead of 41,
                # short by exactly the directory itself, on the run where the
                # worker that scanned the root went on to wedge.
                inflight[slot_id].add(d_parts[0] if d_parts else _ROOT_SCAN)
            seen_here += 1

            children = []  # type: List[Tuple[str, Tuple[str, ...]]]
            # Anything raised between here and the bookkeeping block at the end
            # must not escape the worker: the dead thread would never release this
            # directory's pending count, the survivors would spin on `cv.wait`
            # while `pending_box[0] > 0` forever, and `join()` would never return.
            # A narrower guard around only the `scandir` call was not enough --
            # proved by a shadowed-variable bug in the *setup* below, which hung
            # the walk instead of crashing it.
            failure = ""

            # Every entry in this directory charges the same set of reported
            # ancestors, so resolve their accumulator slots once here rather than
            # rebuilding the key list and re-hashing it per inode.
            ndp = len(d_parts)
            own_level = ndp < depth
            base = []  # type: List[List[int]]
            # `root_stem` is empty when root is "/", so a reported ancestor's key
            # is built exactly the way a child's own key is (`dsep + name`). Using
            # `root` verbatim gave "/" + "/" + "etc" = "//etc" for the ancestor
            # and "/etc" for the child: two Entry objects per directory, both
            # relpath'ing to the same displayed name, so `rdu /` listed every
            # top-level entry twice and os.path.dirname("//etc") == "//" broke the
            # remainder row. Only root "/" was affected, which is why no test saw it.
            acc = root_stem
            for i in range(ndp if own_level else depth):
                acc = acc + sep + d_parts[i]
                slot = agg_get(acc)
                if slot is None:
                    slot = l_agg[acc] = [0, 0, 0, 1]
                base.append(slot)
            nbase = len(base)
            b0 = base[0] if nbase == 1 else None
            # Watched ancestors, resolved once per directory for the same reason
            # `base` is: this is per-directory work, not per-inode work. Runs over
            # the *full* relative path rather than stopping at `depth`.
            watch = []  # type: List[List[int]]
            if ndp:
                wacc = root_stem
                for i in range(ndp):
                    wacc = wacc + sep + d_parts[i]
                    if d_parts[i] in watch_names:
                        wslot = watch_get(wacc)
                        if wslot is None:
                            # Over the cap: charge the shared overflow slot
                            # instead of minting a new path. The subtree's bytes
                            # and inodes are still counted -- what is lost is
                            # which cache they belong to, and that is what the
                            # report discloses.
                            wslot = (
                                l_watch_over
                                if len(l_watch) >= watch_cap
                                else l_watch.setdefault(wacc, [0, 0])
                            )
                        watch.append(wslot)
            dsep = d if d.endswith(sep) else d + sep

            k = 0
            # Did this directory's own scan stop early? A truncated scan means the
            # subtree is not complete even when it enqueued no children, so it
            # cannot be allowed to mark its top-level ancestor finished. See #19.
            d_truncated = False
            try:
                if bucket is not None and not bucket.take(stop_ev):
                    # Stopped while queued for a token, so this directory was
                    # never opened at all. Not an error and not unreadable -- just
                    # work the walk did not do, which is what `d_truncated` means.
                    d_truncated = True
            except Exception as exc:  # noqa: BLE001  (a hang is worse than a report)
                failure = "rate limiter: {}".format(exc)

            try:
                with _NO_ENTRIES if d_truncated else scandir(d) as it:
                    if count_only:
                        # No stat: d_type from getdents is enough to tell a
                        # directory from anything else, and that is all a count
                        # needs. This is the fast path.
                        for entry in it:
                            k += 1
                            if not (k & 1023) and stop_is_set():
                                d_truncated = True
                                break
                            try:
                                isdir = entry.is_dir(follow_symlinks=False)
                            except OSError as exc:
                                l_unstat += 1
                                l_vanished_entries += unstatable(exc, dsep + entry.name)
                                continue
                            if isdir:
                                name = entry.name
                                # --one-file-system was accepted and silently
                                # ignored here, because this path never calls
                                # stat and so never reads st_dev. The two flags a
                                # user is steered toward combining -- `-i -c` is
                                # the hint the tool itself prints, and
                                # --one-file-system is documented as "use this
                                # when reconciling against a per-filesystem
                                # quota" -- were exactly the pair that disagreed.
                                #
                                # One lstat per *directory*, not per inode: a
                                # child is on its parent's filesystem unless it is
                                # itself a mount point, so only directories can
                                # cross. Directories are a few percent of inodes,
                                # so the fast path stays fast.
                                if ofs:
                                    try:
                                        if entry.stat(follow_symlinks=False).st_dev != root_dev:
                                            l_crossed += 1
                                            if len(l_crossed_paths) < _CROSSED_SAMPLE_CAP:
                                                l_crossed_paths.append(dsep + name)
                                            continue
                                    except OSError as exc:
                                        l_unstat += 1
                                        l_vanished_entries += unstatable(exc, dsep + name)
                                        continue
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
                                if watch:
                                    for wslot in watch:
                                        wslot[1] += 1
                                if name in watch_names:
                                    l_watch_seen += 1
                                    _seed_watch(l_watch, dsep + name, 0, l_watch_over, watch_cap)
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
                                if watch:
                                    for wslot in watch:
                                        wslot[1] += 1
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
                            d_truncated = True
                            break
                        try:
                            st = entry.stat(follow_symlinks=False)
                        except OSError as exc:
                            l_unstat += 1
                            l_vanished_entries += unstatable(exc, dsep + entry.name)
                            continue

                        blocks = st.st_blocks * 512
                        mode = st.st_mode
                        ftype = mode & S_IFMT

                        if ftype == S_IFDIR:
                            if ofs and st.st_dev != root_dev:
                                l_crossed += 1
                                if len(l_crossed_paths) < _CROSSED_SAMPLE_CAP:
                                    l_crossed_paths.append(dsep + entry.name)
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
                            # A directory is part of the subtree it sits in, so it
                            # is part of what deleting a cache reclaims. This block
                            # used to sit below the `continue` at the end of this
                            # branch, so `watched` held regular files only while
                            # `dir_agg` held files plus directories -- two different
                            # meanings for one `files` column in RECLAIMABLE.
                            if watch:
                                for wslot in watch:
                                    wslot[0] += blocks
                                    wslot[1] += 1
                            if name in watch_names:
                                # The watched directory's own inode. Charged by the
                                # parent's scan, because `watch` for a directory
                                # holds its watched *ancestors* and never itself.
                                l_watch_seen += 1
                                _seed_watch(l_watch, dsep + name, blocks, l_watch_over, watch_cap)
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
                            # ...and a *group* quota counts it too. `by_uid` was
                            # charged here and `by_gid` was not, so the two tables
                            # disagreed by exactly the directory count -- which is
                            # the one discrepancy a reader comparing them against a
                            # group quota would notice first.
                            gid = st.st_gid
                            slot = gid_get(gid)
                            if slot is None:
                                l_gid[gid] = [blocks, 1]
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
                            l_crossed += 1
                            if len(l_crossed_paths) < _CROSSED_SAMPLE_CAP:
                                l_crossed_paths.append(dsep + entry.name)
                            continue

                        # One comparison on the common path: a regular file is
                        # neither, and it is almost every entry in almost every
                        # tree.
                        if ftype != S_IFREG:
                            if ftype == S_IFLNK:
                                l_sym += 1
                            else:
                                l_spec += 1

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
                        #
                        # Two separate questions, which used to share one branch.
                        # An allocation under `MIN_ALLOC_UNIT` must stay out of the
                        # *unit estimate* -- see that constant for the measurement
                        # -- but that is not evidence the file was inlined, and it
                        # was being counted as `inline` on the strength of it. A
                        # 64-byte file holding one 512-byte sector has `st_blocks
                        # == 1`: blocks were allocated, and 448 bytes of them are
                        # padding. Measured on 50,000 such files: `inline_files:
                        # 50000`, `padding_bytes: 0`, and the packing advice --
                        # the whole point of the panel -- never printed, on the
                        # most packable tree there is.
                        #
                        # `blocks == 0` with data present is the real inline case:
                        # the filesystem allocated nothing and the bytes live in
                        # the inode.
                        #
                        # Except when they simply have not landed yet. A file
                        # written seconds ago on a delayed-allocation filesystem
                        # also reports `st_blocks == 0`, and calling that "inlined"
                        # is the same mistake the cold-data finding once made:
                        # being wrong on exactly the freshly written trees this
                        # tool is pointed at. Caught by a fixture whose only
                        # regular file was 4 KiB and one second old. The original
                        # comment here -- "evidence of neither direction" -- was
                        # right about that, so recency decides, using the same
                        # cutoff `SETTLING` reports against.
                        #
                        # Symlinks are excluded too: a fast symlink is stored in
                        # its inode by construction and says nothing about how this
                        # tree stores its data.
                        if not blocks:
                            settled = st.st_mtime < recent_cutoff and st.st_ctime < recent_cutoff
                            if sz and settled and ftype != S_IFLNK:
                                l_inline += 1
                        elif blocks > sz:
                            l_padn += 1
                            l_pada += sz
                            l_padb += blocks
                            # The unit estimate takes only allocations big enough
                            # to *be* a unit; a 512-byte sector would drag the
                            # measured unit down from 16 KiB, which is the
                            # regression `MIN_ALLOC_UNIT` was introduced to fix.
                            if blocks >= MIN_ALLOC_UNIT:
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
                        if watch:
                            for wslot in watch:
                                wslot[0] += blocks
                                wslot[1] += 1
                        gid = st.st_gid
                        slot = gid_get(gid)
                        if slot is None:
                            l_gid[gid] = [blocks, 1]
                        else:
                            slot[0] += blocks
                            slot[1] += 1

                        mtime = st.st_mtime
                        # Not `bucket`: that name is the TokenBucket, captured
                        # from the enclosing scope. Assigning it here made it a
                        # local to the whole worker, so `if bucket is not None`
                        # above read an unbound local and the thread died on its
                        # first directory.
                        age_bucket = n_buckets - 1
                        for at, cutoff in enumerate(age_cutoffs):
                            if mtime >= cutoff:
                                age_bucket = at
                                break
                        slot = l_age[age_bucket]
                        slot[0] += blocks
                        slot[1] += 1

                        # The union still drives the re-stat sample and the
                        # byte figures, because re-stat is how you find out
                        # whether blocks are moving and that must stay
                        # conservative. Only the attribution is split.
                        if st.st_mtime >= recent_cutoff or st.st_ctime >= recent_cutoff:
                            if st.st_mtime >= recent_cutoff:
                                l_recent += 1
                                # `now` is the walk's *start*, so a file written
                                # while the walk was running is ahead of it -- and
                                # the report names that "an mtime ahead of this
                                # node's clock ... most likely a clock difference
                                # between this node and the fileserver", which is
                                # a false claim about a node whose clock is fine.
                                # Measured on a 2.05s walk: one file written 1.5s
                                # in, `future_files: 1`. This walk takes tens of
                                # seconds on the trees it exists for, and an
                                # actively written tree is exactly what it is
                                # pointed at, so the count is not a rare edge --
                                # it is one per file written during the run.
                                #
                                # Re-read the clock rather than move `now`: the
                                # window's cutoffs must stay fixed for the whole
                                # walk or the recent/age tallies stop being one
                                # measurement. Only this branch is live, and it is
                                # reached only for a file that already looks
                                # future-dated, so the extra `time.time()` is off
                                # the hot path.
                                if st.st_mtime > now and st.st_mtime > wall_clock():
                                    l_future += 1
                            else:
                                l_touched += 1
                            l_recent_app += st.st_size
                            l_recent_size += blocks
                            if len(l_sample) < cap:
                                l_sample.append((dsep + entry.name, blocks))
            except OSError as exc:
                l_unreadable.append((d, exc.strerror or "unreadable"))
                # A scan that raised is the extreme case of the truncated scan
                # `d_truncated` was introduced for: zero entries read, so the
                # subtree is short by everything under this directory. Every other
                # way a scan can stop early sets the flag -- the rate limiter, the
                # stop check inside the entry loop, the non-OSError guard below --
                # and this one did not, so the subtree's top-level ancestor still
                # reached `outstanding[top] == 0` and was published in
                # `finished_tops` as a completed measurement.
                #
                # Reproduced with one unreadable directory holding 20 files inside
                # a depth-1 child holding 40: the interrupted walk ranked that
                # child at 42 inodes against a true 62, in
                # `top_dirs(finished_only=True)` -- the one table whose entire
                # promise is that what it shows is exact. `res.complete` was
                # already False, but that is a statement about the *total*; the
                # per-subtree claim is what `finished_tops` makes and it was wrong.
                d_truncated = True
                # ENOENT: deleted under us. ESTALE: an NFS handle whose target was
                # removed or replaced on the server -- the same event seen through
                # a network filesystem. ENOTDIR: it was a directory when we listed
                # it and is not one now. All three are the tree moving, not a
                # permission being withheld.
                if exc.errno in _VANISHED_ERRNOS:
                    l_vanished += 1
            except Exception as exc:  # noqa: BLE001  (a hang is worse than a report)
                # Anything not an OSError -- MemoryError on a huge frontier, a
                # latent TypeError, a shadowed name -- must not escape. See the
                # note beside `failure` above for what escaping costs.
                l_unreadable.append((d, "internal error: {}".format(exc)))
                d_truncated = True
            if failure:
                l_unreadable.append((d, failure))
                d_truncated = True

            if progress is not None:
                # Published per directory rather than per inode: one list write
                # every few hundred entries costs nothing measurable.
                progress.inode_slots[slot_id] = l_files + l_dirs
                progress.dir_slots[slot_id] = seen_here
                progress.current = d

            with cv:
                dropped = bool(children) and stop_ev.is_set()
                if children and not dropped:
                    queue.extend(children)
                    pending_box[0] += len(children)
                    for _, cparts in children:
                        outstanding[cparts[0]] = outstanding.get(cparts[0], 0) + 1
                pending_box[0] -= 1
                # Work this directory found but will not do. Dropping the children
                # and then decrementing the subtree's counter anyway let the
                # counter reach zero, which marked the subtree *complete* -- so an
                # interrupted run ranked a directory it had barely entered as a
                # finished measurement. `finished_tops` is the entire basis of the
                # interrupt guarantee: `is_finished` reads it, `top_dirs(
                # finished_only=True)` filters on it, and `render_entries` uses
                # that filter precisely so a half-counted directory never appears
                # in a ranking. The producer was breaking the promise the consumer
                # was keeping.
                if (dropped or d_truncated) and d_parts:
                    abandoned_tops.add(d_parts[0])
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
            if door[0]:
                # The result was published without us. Merging now would rewrite
                # numbers the caller has already read and may already have
                # printed, so these tallies are dropped -- and `abandoned_workers`
                # is what tells the reader they existed.
                return
            merged[slot_id] = True
            res.size += l_size
            res.apparent += l_app
            res.files += l_files
            res.dirs += l_dirs
            res.symlinks += l_sym
            res.specials += l_spec
            res.unstatable += l_unstat
            res.vanished_entries += l_vanished_entries
            if len(res.unstatable_paths) < _UNSTAT_SAMPLE_CAP:
                res.unstatable_paths.extend(
                    l_unstat_paths[: _UNSTAT_SAMPLE_CAP - len(res.unstatable_paths)]
                )
            res.crossed += l_crossed
            room = _CROSSED_SAMPLE_CAP - len(res.crossed_paths)
            if room > 0:
                res.crossed_paths.extend(l_crossed_paths[:room])
            res.hardlink_extra_refs += l_extra
            res.recent_files += l_recent
            res.touched_files += l_touched
            res.future_files += l_future
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
            room = max(0, _UNREADABLE_SAMPLE_CAP - len(res.unreadable_dirs))
            res.unreadable_dirs.extend(l_unreadable[:room])
            res.unreadable_dirs_dropped += max(0, len(l_unreadable) - room)
            res.vanished_dirs += l_vanished
            room = _RECENT_SAMPLE_CAP - len(res.recent_sample)
            if room > 0:
                res.recent_sample.extend(l_sample[:room])
            for uid, (b, f) in l_uid.items():
                pb, pf = res.by_uid.get(uid, (0, 0))
                res.by_uid[uid] = (pb + b, pf + f)
            for dev, (b, f) in l_dev.items():
                pb, pf = res.by_dev.get(dev, (0, 0))
                res.by_dev[dev] = (pb + b, pf + f)
            for gid, (b, f) in l_gid.items():
                pb, pf = res.by_gid.get(gid, (0, 0))
                res.by_gid[gid] = (pb + b, pf + f)
            for at, (b, f) in enumerate(l_age):
                pb, pf = res.by_age[at]
                res.by_age[at] = (pb + b, pf + f)
            seen_box[0] += l_watch_seen
            ob, oi = res.watched_overflow
            res.watched_overflow = (ob + l_watch_over[0], oi + l_watch_over[1])
            for wpath, (b, f) in l_watch.items():
                prior = res.watched.get(wpath)
                if prior is None and len(res.watched) >= _WATCHED_CAP:
                    # The same cap again, now across workers: each worker's dict
                    # is bounded, and eight bounded dicts still merge to eight
                    # times the bound. Adding to a path already tracked is always
                    # allowed, so a directory whose contents arrive from several
                    # workers is never half-counted once it is in.
                    ob, oi = res.watched_overflow
                    res.watched_overflow = (ob + b, oi + f)
                    continue
                pb, pf = prior or (0, 0)
                res.watched[wpath] = (pb + b, pf + f)
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
    # Unbounded until something asks the walk to stop, bounded after. A slow walk
    # is not an error and has to be allowed to finish; a *stopped* one must not
    # leave the caller waiting on a syscall that will never return.
    #
    # One deadline for the whole wait, not one per worker. `join(timeout=5.0)` in a
    # loop is five seconds *each*: measured at 11s with two blocked workers and 40s
    # at the default -t 8, so the bound that exists to keep ^C responsive scaled
    # with the thread count it was meant to be independent of. And the bound now
    # covers the `stop` parameter too, which had none at all -- a caller that set
    # `stop` against a hung mount waited on `join()` forever, which is the same
    # unbounded-blocking-call-on-the-emergency-path that `deleted.scan` exists to
    # avoid.
    deadline = None  # type: Optional[float]
    pending = list(workers)
    while pending:
        if deadline is not None and time.monotonic() >= deadline:
            break
        try:
            pending[0].join(timeout=0.25)
            if not pending[0].is_alive():
                pending.pop(0)
        except KeyboardInterrupt:
            stop_ev.set()
            with cv:
                cv.notify_all()
        if deadline is None and stop_ev.is_set():
            deadline = time.monotonic() + STOP_GRACE_S
    # Any stop, not just a KeyboardInterrupt. `stop` is a documented parameter, and
    # a caller that used it got early termination with `partial` still False -- so
    # `complete` depended only on unreadable/unstatable counts and a walk that
    # halted at 18% of the tree could report as a finished measurement.
    if stop_ev.is_set():
        res.partial = True
    # Shut the merge door before reading anything out of `res`. Everything below
    # -- and everything the caller does afterwards -- then runs against a result
    # no worker can still be writing to. It also makes the `dir_agg` iteration
    # below safe: a worker merging concurrently would resize the dict mid-loop.
    with merge_lock:
        door[0] = True
        stranded = [i for i, ok in enumerate(merged) if not ok]
        door[1] = len(stranded)
    # A subtree an abandoned worker took work from is missing an unknown fraction
    # of its contents, whatever `outstanding` says: the counter reaching zero
    # proves the *directories* were processed, not that their tallies arrived.
    # Ranking such a subtree is the exact failure `finished_tops` exists to
    # prevent, so it is abandoned like any other unfinished one.
    root_stranded = False
    for i in stranded:
        if _ROOT_SCAN in inflight[i]:
            root_stranded = True
        abandoned_tops |= inflight[i]
    res.abandoned_workers = door[1]
    # Every watched directory seen, minus those whose path survived the cap.
    # Recorded here, once, rather than incremented alongside `watched` -- a
    # figure that has to be kept in step with a dict is a figure that drifts.
    res.watched_dropped = max(0, seen_box[0] - len(res.watched))
    res.elapsed = time.perf_counter() - t0
    with links_lock:
        res.hardlinked_inodes = len(seen_links)
    if root_stranded:
        # The root's scan charges every depth-1 entry with its own inode and its
        # own blocks, so losing it leaves *all* of them short by exactly
        # themselves. A uniform small error is still an error, and the promise
        # `finished_tops` makes is exactness, not closeness -- so nothing is
        # rankable and the report says the walk was abandoned.
        res.finished_tops = set()
        abandoned_tops.add(_ROOT_SCAN)
    else:
        res.finished_tops = finished_tops - abandoned_tops
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
        # ...and what the walk had counted for them. The blocks are already in
        # `recent_sample`, so this costs one addition, and it is the only figure
        # in this class that says how *wrong* the total is rather than how far it
        # moved. `drift` cannot say it by construction: a vanished file is left
        # out of both sides of that subtraction so a deletion cannot masquerade
        # as the tree shrinking -- correct for the drift, but it means the one
        # change the re-stat positively observed is the one it reports as zero.
        # Measured on eight 64 KiB files with seven unlinked during a 7s
        # `--settle-wait`: the walk read 512.0 KiB, the tree held 64.0 KiB, and
        # this held the 448.0 KiB difference exactly, at every ratio tried.
        self.gone_bytes = 0
        # No `window` field. It was assigned here and again in
        # `recheck_settling`, and read nowhere: every consumer -- `render_settle`,
        # `to_json` -- reads `WalkResult.settle_window`, which is where the window
        # is actually decided. Two homes for one number is how they drift apart.
        self.gap = 0.0  # seconds between the walk reading and the re-stat
        self.ran = False

    @property
    def moved(self) -> bool:
        """True when the re-stat positively observed the tree changing."""
        return self.drift != 0

    @property
    def recheck_measured_nothing(self) -> bool:
        """True when the re-stat had files to measure and measured none of them.

        Every path in the sample was gone by the time the re-stat reached it, so
        ``drift == 0`` is the *absence* of a reading, not a reading of zero. This
        is the tree this tool is pointed at: checkpoint rotation deletes the old
        ``.pt`` while the new one is written, and a ``--settle-wait`` long enough
        to be believed is long enough for the whole sample to be unlinked.

        Distinct from ``checked == 0`` on its own, which is also how "nothing was
        written recently" arrives here -- an empty population has nothing to be
        unsettled about and its null result is fine to believe.
        """
        return self.gone > 0 and self.checked == 0

    @property
    def conclusive(self) -> bool:
        """Can a *null* result from this check be believed?

        Only if the check actually ran and had long enough to see the effect.
        Constraint 1: before believing a null result, ask whether the instrument
        can see the effect at all.

        **A re-stat that re-stat'ed nothing is that instrument.** The gap test
        alone said yes to a check whose whole sample had been deleted: measured
        with eight recently written files removed between the walk and a 6s
        re-check, ``checked=0 gone=8 drift=0`` reported *"a re-stat 6s later
        found no change in 0 files; the figure looks settled"* and
        ``"settled": true`` -- the strongest claim in this section, from a
        reading that never happened. See :attr:`recheck_measured_nothing`.
        """
        if not self.ran:
            return False
        return self.moved or (
            self.gap >= MIN_CONCLUSIVE_GAP_S and not self.recheck_measured_nothing
        )

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

    A ``count_only`` result gets a check that did not run (``ran == False``),
    because there is nothing to re-stat and no population to have been empty.
    """
    chk = SettleCheck()
    if res.count_only:
        # A stat-free walk read no mtime, so its empty `recent_sample` is the
        # *absence* of a measurement, not an empty population -- exactly the
        # distinction `_unmeasured` draws for every other figure `-c` does not
        # collect. Returning `ran = True` here dressed that absence as a reading,
        # and with a wait long enough the gap test then believed it: measured
        # before this guard, on a five-file tree walked with `count_only=True`,
        # `recheck_settling(res, 6.0)` returned `checked=0 gone=0 ran=True
        # gap=6.0 conclusive=True` -- "believe this null" from a walk that never
        # read an mtime and could not have seen drift at all.
        #
        # `cmd_walk` forces `--no-settle-check` under `-c`, which is why the
        # document never showed it -- the honesty of the `-c` settling block
        # rested on that one line in the CLI rather than on anything here. A
        # check that did not run is what this mode has to hand back, whoever asks.
        return chk
    # The population the sample was actually drawn from, which is the *union* of
    # written-recently and inode-touched-recently -- `recent_sample` is filled in
    # that branch. Setting it from `recent_files` alone was correct while the two
    # were one counter and wrong the moment they were split: a tree with 1,000
    # written files and 9,000 touched inodes truncates the 4,096-entry sample by
    # six thousand, and `sampled` compared 1,000 against 4,096 and reported no
    # truncation at all. A cap that hides itself is the defect this report has
    # filed twice already.
    chk.sampled_of = res.recent_files + res.touched_files
    if not res.recent_sample:
        # Nothing was written recently, so there is nothing to be unsettled --
        # and nothing to wait for either: this branch does not sleep. `gap` stays
        # 0.0 because it is documented as "seconds between the walk reading and
        # the re-stat", and assigning `wait` here published a duration that had
        # not elapsed. Measured with `--settle-window 1 --settle-wait 30` over a
        # five-file tree: the command returned in 0.08s and `--json` reported
        # `rechecked: 0, recheck_gap_seconds: 30.0`, which reads as "waited 30s,
        # re-stat'ed nothing" rather than "there was nothing to wait for". The
        # terminal never showed it -- `render_settle` returns [] for an empty
        # population -- so the fabrication was visible only to a machine
        # consumer, in the one field it would use to weigh the result.
        #
        # `conclusive` reads the gap, so it stops moving with the wait too: the
        # same tree published `conclusive: false` at `--settle-wait 0` and
        # `conclusive: true` at 30, from two runs that observed exactly nothing.
        # It is now `false` for every wait, which is what the check earned -- it
        # measured nothing, so there is no null result of *its* to believe. The
        # verdict a reader wants is `settled`, and `to_json` takes that from the
        # walk's own `recent_files`/`touched_files` being zero rather than from
        # this check, so it stays `true` and stays right for the stated reason.
        #
        # No `sampled_of == 0` branch in `conclusive`, and that is now settled by
        # measurement rather than left open. Adding one (`sampled_of == 0 ->
        # True`) fails nine tests, which reads like a semantic obstacle and is
        # not one: those fixtures set `checked` to 5 or 60 while leaving
        # `sampled_of` at 0, and this function cannot produce that pairing. The
        # sample is appended in the same branch that increments
        # `recent_files`/`touched_files` and the loop below visits each entry
        # once, so `checked + gone == len(recent_sample) <= sampled_of` always,
        # and `sampled_of == 0` holds exactly when `recent_sample` is empty. Give
        # those fixtures the `sampled_of` this function would have set and all
        # nine pass with the branch installed -- they pin a state the walk cannot
        # reach, not a premise about what the property means.
        #
        # The branch is unnecessary regardless. Swept across every reachable
        # state -- no check asked for, `count_only`, this empty population, a
        # full re-stat at gap 0 and at 60, measured drift, the whole sample
        # deleted, a partial vanishing, and a 5,000-file sample truncated to the
        # 4,096 cap -- it moves exactly one published value: `settling.conclusive`
        # for this branch. Both terminal renderings, `_headline_is_provisional`,
        # `_provisional_note`, `settled`, and every `reconcile` verdict, blocker
        # and line are byte-identical in all nine. `reconcile` cannot reach it
        # here at all: its read sits inside `elif res.recent_files or
        # res.touched_files`, which an empty population does not enter.
        #
        # And `false` is the better of the two values for that one field, because
        # this branch does not sleep. `conclusive: true` beside
        # `recheck_gap_seconds: 0.0` would assert a believable null from a
        # zero-gap re-stat, which is the single claim this class exists to refuse
        # -- see the RD-16 note declining a 0.5s default for the same reason. The
        # belief an empty population does license is about the tree, not about
        # this check, and the document already publishes it as `settled: true`
        # from the walk's own counters. Pinned in
        # `tests/test_settle_population_invariant.py`.
        chk.ran = True
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
            chk.gone_bytes += blocks_then
            continue
        before += blocks_then
        after += st.st_blocks * 512
        chk.checked += 1
    chk.drift = after - before
    return chk
