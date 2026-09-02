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


# ---- the NFS half has no `nlink == 0` to fall back on ---------------------
#
# A silly-rename entry is a *linked* file with nlink == 1, indistinguishable from
# any other file by stat, so `_SILLY_RENAME_RE` is the entire proof. The pair
# below is the `.nfsXXXX` counterpart of
# `test_a_file_named_deleted_is_not_reported_as_deleted`: one half checks that a
# real file is not turned into a finding, the other that the detection it relies
# on is still switched on.

SILLY = ".nfs00000002945e149d00002b83"  # the form measured on an NFSv3 home


def _hold_named(root, name):
    """Create and hold open ``root/name``, or return None if the name is illegal.

    A newline is a legal byte in a POSIX filename, but not every filesystem
    agrees, so an EINVAL/ENAMETOOLONG here means the fixture has no subject
    rather than that the code is wrong.
    """
    try:
        fh = open(os.path.join(root, name), "wb")
    except (OSError, ValueError):
        return None
    fh.write(b"x" * 4096)
    fh.flush()
    os.fsync(fh.fileno())
    return fh


def test_a_linked_file_whose_name_ends_in_a_newline_is_not_silly_renamed(tmp_path):
    """`$` matches before a trailing newline; a filename may contain one.

    So ``.nfs00000002945e149d00002b83\\n`` -- a real file, still linked, still
    visible to ``du``, still charged once -- matched the pattern that is the only
    proof this branch has, and its blocks were reported as space held by a
    deleted-but-open file and added to ``silly_renamed_size``. The fabricated
    finding the module refuses to make from the " (deleted)" suffix, made from a
    filename instead. ``\\Z`` is the anchor that means what this pattern needs.
    """
    root = str(tmp_path)
    fh = _hold_named(root, SILLY + "\n")
    if fh is None:
        pytest.skip("this filesystem does not accept a newline in a filename")
    try:
        assert os.lstat(os.path.join(root, SILLY + "\n")).st_nlink == 1
        scan = D.scan(root)
        assert scan.silly_renamed == [], (
            "a linked file must never be reported as deleted-but-open: "
            + repr([f.path for f in scan.silly_renamed])
        )
        assert scan.silly_renamed_size == 0
    finally:
        fh.close()


def test_the_real_silly_rename_form_is_still_detected(tmp_path):
    """Control for the test above: the anchor must not switch the branch off.

    Tightening `$` to `\\Z` would pass the newline test just as well by never
    matching anything at all, and then rapidu would report "none found" on every
    NFS site -- which is the exact failure `_SILLY_RENAME_RE`'s comment says the
    branch was added to fix. Nothing here is unlinked, so this runs on every
    filesystem.
    """
    root = str(tmp_path)
    fh = _hold_named(root, SILLY)
    assert fh is not None
    try:
        scan = D.scan(root)
        assert [os.path.basename(f.path) for f in scan.silly_renamed] == [SILLY]
        assert scan.silly_renamed_size == os.fstat(fh.fileno()).st_blocks * 512
        assert scan.files == [], "nothing here was unlinked"
        assert os.getpid() in scan.silly_renamed[0].pids
    finally:
        fh.close()


def _memfd(name):
    """A real ``memfd_create`` fd, or ``None`` where the call is unavailable.

    ``os.memfd_create`` exists from 3.8, but only when CPython was built against a
    libc that exposed it -- the interpreter this suite runs under here was not --
    so the syscall is reached through ``ctypes``, which is stdlib and needs no
    build support. Returns ``None`` rather than skipping inline, so the caller
    decides whether the fixture is essential.
    """
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.memfd_create.argtypes = (ctypes.c_char_p, ctypes.c_uint)
        libc.memfd_create.restype = ctypes.c_int
        fd = libc.memfd_create(name.encode("ascii"), 0)
    except (AttributeError, OSError, ValueError):
        return None
    return fd if fd >= 0 else None


def test_a_memfd_is_not_reported_as_space_held_by_a_deleted_file():
    """An object that was never linked is not a file that somebody deleted.

    ``memfd_create`` returns a regular file on a kernel-internal shmem mount with
    no directory entry, ever. ``d_path`` renders it as ``/memfd:<name>`` and
    appends " (deleted)" unconditionally, so ``S_ISREG`` is true and ``st_nlink``
    is 0: both gates that make the unlinked finding trustworthy pass, and 16 MiB
    of anonymous memory was reported under a header promising blocks that "may
    still be charged" to a quota, at a path that has never existed in any
    filesystem. ``/memfd:pulseaudio`` was in the node-wide scan on this login node
    with no setup at all.
    """
    fd = _memfd("rapidu-probe-memfd")
    if fd is None:
        pytest.skip("memfd_create is not reachable through libc on this platform")
    try:
        os.write(fd, b"m" * SIZE)
        target = os.readlink("/proc/self/fd/{}".format(fd))
        assert target == "/memfd:rapidu-probe-memfd (deleted)", target
        st = os.stat("/proc/self/fd/{}".format(fd))
        assert st.st_nlink == 0 and st.st_blocks * 512 >= SIZE, "the two gates do pass"

        scan = D.scan()  # the whole node: exactly what `rdu -D` with no path does
        assert [f.path for f in scan.files if "memfd" in f.path] == []
        assert [f.path for f in scan.files if os.path.dirname(f.path) == "/"] == [], (
            "no anonymous kernel object may be reported as a deleted file"
        )
    finally:
        os.close(fd)


@needs_real_unlink
def test_a_genuinely_unlinked_file_survives_the_anonymous_object_guard(tmp_path):
    """Control for the test above, at the same level: end-to-end through ``scan()``.

    Dropping every single-component path, or every ``st_dev`` that is not the root
    filesystem's, would pass the memfd test just as well by reporting nothing --
    and this scan's entire job is to report exactly this file. A node-wide scan,
    not a prefixed one, because the prefix filter would hide the memfd on its own
    and the guard would never be reached.
    """
    root = str(tmp_path)
    path = os.path.join(root, "held.bin")
    fh = open(path, "wb")
    try:
        fh.write(b"\0" * SIZE)
        fh.flush()
        os.fsync(fh.fileno())
        allocated = os.fstat(fh.fileno()).st_blocks * 512
        os.unlink(path)

        scan = D.scan()
        mine = [f for f in scan.files if f.path == path]
        assert len(mine) == 1, [f.path for f in scan.files]
        assert mine[0].size == allocated
        assert os.getpid() in mine[0].pids
    finally:
        fh.close()


def test_the_anonymous_object_guard_keeps_a_real_file_in_the_root_directory():
    """The other half of the control, for the case a test cannot create.

    An unlinked ``/core.1234`` on the root filesystem also has a single-component
    path, and it is a real file whose blocks are really charged. It is told apart
    by ``st_dev``, which is what makes the test exact rather than a name match: a
    path with no directory component means the file sat in the root directory, so
    its device *is* the root filesystem's by definition. Unwritable by a test, so
    asserted against the predicate.
    """
    root_dev = os.stat("/").st_dev
    assert D._is_anonymous_kernel_object("/memfd:torch-shm", root_dev + 1, root_dev)
    assert D._is_anonymous_kernel_object("/SYSV00000000", root_dev + 1, root_dev)
    assert D._is_anonymous_kernel_object("/[aio]", root_dev + 1, root_dev)
    # A real file in the root directory, and anything with a directory component.
    assert not D._is_anonymous_kernel_object("/core.1234", root_dev, root_dev)
    assert not D._is_anonymous_kernel_object("/scratch/lab/ckpt.bin", root_dev + 1, root_dev)
    # And where "/" itself could not be stat'ed the check goes inoperative rather
    # than dropping a finding on evidence nobody has.
    assert not D._is_anonymous_kernel_object("/memfd:torch-shm", root_dev + 1, None)
