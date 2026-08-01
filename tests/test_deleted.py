"""Unlinked-but-open detection.

The end-to-end test writes a real file, unlinks it while holding the fd, and
requires the scan to find the space that ``du`` and the walk both miss. This is
the one capability that cannot be tested with fixtures, because the whole point
is that it is invisible to anything that reads directory entries.
"""

import os

from slurmdisk import deleted as D
from slurmdisk.walk import walk

SIZE = 8 << 20  # 8 MiB: big enough to be unambiguous, small enough to be quick


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

        assert walked.size < allocated, "the walk should not be able to see it"
        assert len(scan.files) == 1
        assert scan.total_size == allocated

        found = scan.files[0]
        assert found.path == path
        assert os.getpid() in found.pids
    finally:
        fh.close()


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


def test_same_inode_from_two_fds_counted_once(tmp_path):
    """Two descriptors on one inode allocate the blocks once."""
    root = str(tmp_path)
    path = os.path.join(root, "shared.bin")
    a = open(path, "wb")
    try:
        a.write(b"\0" * SIZE)
        a.flush()
        b = open(path, "rb")
        try:
            os.unlink(path)
            scan = D.scan(root)
            assert len(scan.files) == 1
            assert scan.total_size < SIZE * 2
        finally:
            b.close()
    finally:
        a.close()


def test_reports_what_it_could_not_inspect():
    """On a shared node other users' processes are EACCES, and that is a floor."""
    scan = D.scan()
    assert scan.available
    assert scan.scanned_pids > 0
    # complete == "no process was hidden from us"
    assert scan.complete == (scan.unreadable_pids == 0)


def test_under_preserves_incompleteness():
    scan = D.scan()
    narrowed = scan.under("/")
    assert narrowed.unreadable_pids == scan.unreadable_pids
    assert narrowed.scanned_pids == scan.scanned_pids
