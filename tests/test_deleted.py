"""Unlinked-but-open detection.

The end-to-end test writes a real file, unlinks it while holding the fd, and
requires the scan to find the space that ``du`` and the walk both miss. This is
the one capability that cannot be tested with fixtures, because the whole point
is that it is invisible to anything that reads directory entries.
"""

import os

import pytest
from conftest import NEEDS_REAL_UNLINK, UNLINK_HIDES_ENTRY

from rapidu import deleted as D
from rapidu.walk import walk

# The scan's whole subject is a file with no directory entry. Where the
# filesystem keeps one -- NFS silly-rename, see `conftest` -- these fixtures
# cannot be built, and the two tests below that need no unlink still run.
needs_real_unlink = pytest.mark.skipif(not UNLINK_HIDES_ENTRY, reason=NEEDS_REAL_UNLINK)

SIZE = 8 << 20  # 8 MiB: big enough to be unambiguous, small enough to be quick


@needs_real_unlink
def test_finds_space_the_walk_cannot_see(tmp_path):
    root = str(tmp_path)
    path = os.path.join(root, "ckpt.bin")
    fh = open(path, "wb")
    try:
        fh.write(b"\0" * SIZE)
        fh.flush()
        os.fsync(fh.fileno())
        # Ground truth from the fd itself. Asserting against SIZE instead would
        # be flaky on a filesystem that has not finished allocating blocks --
        # the very effect this package reports elsewhere.
        allocated = os.fstat(fh.fileno()).st_blocks * 512
        os.unlink(path)  # fd still held: no directory entry remains

        walked = walk(root, threads=2)
        scan = D.scan(root)
        # Read again *after* the scan, and accept anything between the two.
        #
        # `scan` reads `st_blocks` too, so this compares like with like -- but at
        # a different moment, and on GPFS that is the whole difference. Taking
        # ground truth from the fd removed the flake in one direction (blocks not
        # yet allocated) and left it in the other: this 8 MiB file measured
        # 16,711,680 bytes from the fd and 8,388,608 from the scan a moment
        # later, because GPFS over-allocates and then trims. `test_walk._settle`
        # exists for exactly this and cannot be used here, since the file has no
        # name left to settle on.
        #
        # On a filesystem that is not moving the two readings are equal and this
        # is the old exact assertion; where they differ, the bracket is the
        # strongest true statement available.
        after = os.fstat(fh.fileno()).st_blocks * 512
        low, high = min(allocated, after), max(allocated, after)

        assert walked.size < low, "the walk should not be able to see it"
        assert len(scan.files) == 1
        assert low <= scan.total_size <= high, (scan.total_size, allocated, after)

        found = scan.files[0]
        assert found.path == path
        assert os.getpid() in found.pids
    finally:
        fh.close()


@needs_real_unlink
def test_space_is_released_when_the_fd_closes(tmp_path):
    root = str(tmp_path)
    path = os.path.join(root, "tmp.bin")
    fh = open(path, "wb")
    fh.write(b"\0" * SIZE)
    fh.flush()
    os.fsync(fh.fileno())
    os.unlink(path)
    assert len(D.scan(root).files) == 1
    fh.close()
    assert D.scan(root).files == []


@needs_real_unlink
def test_prefix_filter(tmp_path):
    root = str(tmp_path)
    inside = os.path.join(root, "inside")
    os.makedirs(inside)
    fh = open(os.path.join(inside, "x.bin"), "wb")
    try:
        fh.write(b"\0" * SIZE)
        fh.flush()
        os.unlink(os.path.join(inside, "x.bin"))
        assert D.scan(inside).files
        assert D.scan(os.path.join(root, "elsewhere")).files == []
    finally:
        fh.close()


@needs_real_unlink
def test_same_inode_from_two_fds_counted_once(tmp_path):
    """Two descriptors on one inode allocate the blocks once.

    Against the inode's *allocation*, read from the fd, not against ``SIZE``.
    ``total_size < SIZE * 2`` reads like a safe margin and is really an
    assumption that no filesystem allocates twice what it is given: xfs
    speculative preallocation gives this 8 MiB file exactly 16 MiB of blocks, so
    on the ``/tmp`` of a compute node the check read ``16777216 < 16777216`` and
    failed while the code was right -- and had it been off by one the other way
    it would have *passed* with the blocks counted twice, which is the bug it
    exists to catch. The same reading on OneFS gives a 1 B file 24 KiB.

    Bracketed for the reason given in the first test: `st_blocks` moves.
    """
    root = str(tmp_path)
    path = os.path.join(root, "shared.bin")
    a = open(path, "wb")
    try:
        a.write(b"\0" * SIZE)
        a.flush()
        os.fsync(a.fileno())
        b = open(path, "rb")
        try:
            os.unlink(path)
            before = os.fstat(a.fileno()).st_blocks * 512
            scan = D.scan(root)
            after = os.fstat(a.fileno()).st_blocks * 512
            assert len(scan.files) == 1
            low, high = min(before, after), max(before, after)
            assert low <= scan.total_size <= high, (scan.total_size, before, after)
        finally:
            b.close()
    finally:
        a.close()


def test_reports_what_it_could_not_inspect():
    """On a shared node other users' processes are EACCES, and that is a floor.

    ``complete`` means "nothing was hidden from us", and there are three ways to
    be hidden, not one: another user's process (EACCES), a PID namespace that
    shows only its own processes, and a sweep abandoned on a hung mount. This
    used to assert ``complete == (unreadable_pids == 0)``, which was the
    definition *before* the other two were added -- so it passed on a login node
    (775 processes unreadable, both sides False) and on GitHub's runners, and
    failed in any container where you are the only user: nothing unreadable,
    namespaced anyway.
    """
    scan = D.scan()
    assert scan.available
    assert scan.scanned_pids > 0
    assert scan.complete == (
        scan.unreadable_pids == 0 and not scan.namespaced and not scan.timed_out
    )
    # And the part that was worth asserting all along: any one of the three is
    # enough to make the figure a floor.
    if scan.unreadable_pids or scan.namespaced or scan.timed_out:
        assert not scan.complete


def test_under_preserves_incompleteness():
    scan = D.scan()
    narrowed = scan.under("/")
    assert narrowed.unreadable_pids == scan.unreadable_pids
    assert narrowed.scanned_pids == scan.scanned_pids


def test_a_file_named_deleted_is_not_reported_as_deleted(tmp_path):
    """`" (deleted)"` is a hint from the kernel, not proof, and a real filename
    can end the same way. The authoritative test is ``st_nlink == 0`` on the fd
    we already hold. Trusting the string attributed space to a file that was
    never unlinked -- a fabricated finding, in the one section whose whole job
    is to avoid making them."""
    root = str(tmp_path)
    # The name must *end* with the suffix for the readlink target to be
    # ambiguous -- `report (deleted).pdf` is not, because ".pdf" follows it.
    path = os.path.join(root, "quarterly report (deleted)")
    fh = open(path, "wb")
    try:
        fh.write(b"\0" * SIZE)
        fh.flush()
        os.fsync(fh.fileno())
        # Still linked. /proc/<pid>/fd/<n> resolves to a target ending in
        # " (deleted)" purely because of the name.
        assert os.lstat(path).st_nlink == 1
        scan = D.scan(root)
        assert scan.files == [], "a linked file must never be reported as unlinked"

        # ...and unlinking the very same file must still be found. Guarded
        # rather than skipping the whole test: the half above -- a *linked* file
        # whose name ends in " (deleted)" must not be reported -- is the half
        # that catches a fabricated finding, and it holds on every filesystem.
        if UNLINK_HIDES_ENTRY:
            os.unlink(path)
            assert len(D.scan(root).files) == 1
    finally:
        fh.close()
