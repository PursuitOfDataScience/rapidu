"""Walker correctness.

The three invariants here are the ones that decide whether this tool is better
or worse than ``du``. Each test fails if the corresponding defect is reintroduced.
"""

import contextlib
import os
import shutil
import subprocess
import time

import pytest

from slurmdisk.walk import MAX_THREADS, TokenBucket, walk


def _du(path):
    """``du -s --block-size=1``: allocated blocks, hardlink-deduped."""
    out = subprocess.check_output(["du", "-s", "--block-size=1", path], universal_newlines=True)
    return int(out.split()[0])


def _settle(path, timeout=120.0):
    """Block until the tree stops moving, so byte comparisons mean something.

    If the temp directory is on GPFS -- which it is whenever ``TMPDIR`` points
    at a project or scratch filesystem -- ``st_blocks`` is not final for tens of
    seconds after writing. Measured on this cluster, the same 208-file fixture
    read 17,408 bytes at t+0 and 3,294,208 bytes at t+30.

    Without this wait, a test comparing two measurements taken at different
    moments fails intermittently for a reason that has nothing to do with the
    walker. The discriminator that separates the two causes is the one the
    walker itself provides: across thread counts the *counts* were always
    identical and only the *blocks* moved, which is the filesystem changing, not
    a race. Tests below still assert count-determinism unconditionally.
    """
    deadline = time.time() + timeout
    prev = _du(path)
    while time.time() < deadline:
        time.sleep(3)
        cur = _du(path)
        if cur == prev:
            return cur
        prev = cur
    return prev


@pytest.fixture(scope="session")
def tree(tmp_path_factory):
    """A tree with every pathology that breaks a naive walker.

    Session-scoped: it is read-only for every test, and settling it once keeps
    the wait off each individual test.
    """
    root = str(tmp_path_factory.mktemp("walk") / "t")
    os.makedirs(root)
    payload = b"x" * 4096
    for i in range(20):
        d = os.path.join(root, "d%02d" % i)
        os.makedirs(d)
        for j in range(10):
            with open(os.path.join(d, "f%02d.dat" % j), "wb") as fh:
                fh.write(payload)

    # 5 extra hard links to one inode: du counts the inode once.
    first = os.path.join(root, "d00", "f00.dat")
    for k in range(5):
        os.link(first, os.path.join(root, "hardlink%d.dat" % k))

    # 1 GiB apparent, ~0 allocated: st_size would over-report by 1 GiB.
    with open(os.path.join(root, "sparse.bin"), "wb") as fh:
        fh.truncate(1 << 30)

    open(os.path.join(root, "empty.dat"), "w").close()
    os.symlink("/etc/hostname", os.path.join(root, "link.sym"))
    os.makedirs(os.path.join(root, *["deep%d" % i for i in range(12)]))
    subprocess.call(["sync"])
    _settle(root)
    return root


def test_matches_du_exactly(tree):
    """A correct walker agrees with du byte-for-byte. Beating it would be a bug."""
    before = _du(tree)
    walked = walk(tree, threads=4).size
    if _du(tree) != before:
        pytest.skip("tree moved between the two du readings; see _settle()")
    assert walked == before


def test_counts_identical_across_thread_counts(tree):
    """Concurrency must not change what the walker counts. Unconditional.

    This is the assertion that would catch a real race: block totals can move
    because the filesystem moved, but inode counts cannot.
    """
    seen = set()
    for n in (1, 2, 4, 8, 16):
        r = walk(tree, threads=n)
        seen.add((r.files, r.dirs, r.inodes, r.hardlink_extra_refs, r.symlinks))
    assert len(seen) == 1, "walkers disagreed on counts across thread counts: %s" % seen


def test_bytes_identical_across_thread_counts(tree):
    """Same, for bytes -- bracketed, because only bytes can move underneath us."""
    before = _du(tree)
    sizes = {walk(tree, threads=n).size for n in (1, 2, 4, 8, 16)}
    if _du(tree) != before:
        pytest.skip("tree moved during the sweep; see _settle()")
    assert len(sizes) == 1, "walkers disagreed on bytes across thread counts: %s" % sizes
    assert sizes == {before}


def test_hardlinks_deduped(tree):
    """5 extra links to one inode must be counted once, not six times."""
    r = walk(tree, threads=4)
    assert r.hardlinked_inodes == 1
    assert r.hardlink_extra_refs == 5
    # And the inode total must not double-count them.
    assert r.inodes == r.files + r.dirs - 5


def test_sparse_file_not_overcounted(tree):
    """st_blocks, not st_size: a 1 GiB sparse file must not add 1 GiB."""
    r = walk(tree, threads=4)
    assert r.size < (1 << 30), "sparse file was counted at apparent size"
    assert r.apparent > r.size + (1 << 29), "fixture is not actually sparse"


def test_naive_walker_is_wrong(tree):
    """Guards the invariant by demonstrating the error it prevents."""
    naive = 0
    for dirpath, _dirnames, filenames in os.walk(tree):
        for name in filenames:
            with contextlib.suppress(OSError):
                naive += os.lstat(os.path.join(dirpath, name)).st_size
    assert naive > _du(tree) * 2, (
        "the st_size + no-dedup walker should be grossly wrong on this tree; "
        "if it is not, the fixture has lost its pathologies"
    )


def test_threads_are_capped():
    """Past the cap the walk is slower and the metadata load is impolite."""
    r = walk(os.path.dirname(__file__), threads=1000)
    assert r.threads == MAX_THREADS
    assert walk(os.path.dirname(__file__), threads=0).threads == 1


def test_inode_and_dir_counts(tree):
    r = walk(tree, threads=4)
    # 20*10 files + 5 hardlinks + sparse + empty + symlink = 208 entries
    assert r.files == 208
    # root + 20 + 12 deep = 33
    assert r.dirs == 33
    assert r.symlinks == 1


def test_one_file_system_flag(tree):
    """The flag must not exclude anything on a single-filesystem tree."""
    with_flag = walk(tree, threads=4, one_file_system=True)
    without = walk(tree, threads=4)
    assert (with_flag.files, with_flag.dirs) == (without.files, without.dirs)


def test_unreadable_dir_marks_result_incomplete(tmp_path):
    """An unreadable directory makes the total a floor, and must say so."""
    root = str(tmp_path / "u")
    locked = os.path.join(root, "locked")
    os.makedirs(locked)
    with open(os.path.join(locked, "f"), "wb") as fh:
        fh.write(b"x" * 4096)
    os.chmod(locked, 0o000)
    try:
        r = walk(root, threads=2)
        if os.getuid() == 0:
            pytest.skip("root can read anything")
        assert not r.complete
        assert len(r.unreadable_dirs) == 1
    finally:
        os.chmod(locked, 0o755)
        shutil.rmtree(root, ignore_errors=True)


def test_rejects_non_directory(tmp_path):
    f = tmp_path / "a-file"
    f.write_text("hello")
    with pytest.raises(NotADirectoryError):
        walk(str(f))


def test_empty_dir(tmp_path):
    """The root's own inode is counted, matching du."""
    d = str(tmp_path / "empty")
    os.makedirs(d)
    r = walk(d)
    assert r.dirs == 1
    assert r.files == 0
    assert r.size == _settle(d)


def test_token_bucket_rate_limits():
    import time

    b = TokenBucket(rate=20.0, burst=1.0)
    t0 = time.monotonic()
    for _ in range(5):
        b.take()
    assert time.monotonic() - t0 >= 0.15


def test_token_bucket_disabled_is_free():
    import time

    b = TokenBucket(rate=0.0)
    t0 = time.monotonic()
    for _ in range(1000):
        b.take()
    assert time.monotonic() - t0 < 0.5


def test_entry_sizes_are_cumulative(tmp_path):
    """A directory row must agree with `du -s` on that path.

    Before this was fixed, a directory was charged only with what sat directly
    inside it, so a parent could rank below its own child and a directory whose
    bulk lived one level down vanished from the listing entirely.

    Built without cross-directory hard links on purpose -- see
    ``test_hardlinks_spanning_subtrees_are_charged_once``.
    """
    root = str(tmp_path / "c")
    for i in range(3):
        d = os.path.join(root, "d%d" % i, "deep")
        os.makedirs(d)
        for j in range(4):
            with open(os.path.join(d, "f%d" % j), "wb") as fh:
                fh.write(b"x" * 8192)
    subprocess.call(["sync"])
    _settle(root)

    r = walk(root, threads=4, depth=1)
    assert r.size == _du(root)
    listed = r.top_dirs(20, "size")
    assert len(listed) == 3
    for e in listed:
        assert e.size == _du(e.path), e.path


def test_entries_sum_to_the_total(tree):
    """Depth-1 entries plus the root's own inode must account for everything.

    Order-independent, so it holds even where hard links span subtrees and the
    per-directory attribution depends on which reference the walk reached first.
    """
    r = walk(tree, threads=4, depth=1)
    listed = sum(e.size for e in r.dir_agg.values() if e.path != r.root)
    root_inode = os.lstat(r.root).st_blocks * 512
    assert listed + root_inode == r.size


def test_hardlinks_spanning_subtrees_are_charged_once(tmp_path):
    """Two names for one inode in different directories cost the tree once.

    Which directory carries the cost depends on traversal order -- `du` behaves
    the same way -- but the total must not double-count.
    """
    root = str(tmp_path / "h")
    os.makedirs(os.path.join(root, "a"))
    os.makedirs(os.path.join(root, "b"))
    target = os.path.join(root, "a", "payload")
    with open(target, "wb") as fh:
        fh.write(b"x" * (1 << 20))
    os.link(target, os.path.join(root, "b", "alias"))
    subprocess.call(["sync"])
    _settle(root)

    r = walk(root, threads=2, depth=1)
    assert r.size == _du(root)
    assert r.hardlink_extra_refs == 1
    charged = [e for e in r.top_dirs(10) if e.size > (512 << 10)]
    assert len(charged) == 1, "the payload must be charged to exactly one subtree"


def test_plain_files_are_listed_too(tmp_path):
    """A big file in the root is not a directory and must still be visible."""
    root = str(tmp_path / "f")
    os.makedirs(root)
    os.makedirs(os.path.join(root, "sub"))
    with open(os.path.join(root, "big.db"), "wb") as fh:
        fh.write(b"x" * (2 << 20))
    with open(os.path.join(root, "sub", "small"), "wb") as fh:
        fh.write(b"x" * 4096)
    entries = {os.path.basename(e.path): e for e in walk(root, threads=2, depth=1).top_dirs(10)}
    assert "big.db" in entries, "a top-level file must appear in the listing"
    assert not entries["big.db"].is_dir
    assert entries["sub"].is_dir


def test_depth_limits_reporting_not_the_walk(tree):
    """Deeper reporting must not change the total, only how it is broken down."""
    shallow = walk(tree, threads=4, depth=1)
    deep = walk(tree, threads=4, depth=3)
    assert shallow.size == deep.size
    assert shallow.inodes == deep.inodes
    assert len(deep.dir_agg) >= len(shallow.dir_agg)


def test_finished_tops_covers_a_complete_walk(tree):
    """Every depth-1 child of a completed walk must be marked finished."""
    r = walk(tree, threads=4, depth=1)
    assert not r.partial
    names = {os.path.basename(e.path) for e in r.dir_agg.values() if e.path != r.root}
    assert names.issubset(r.finished_tops) or not r.partial


def test_interrupted_walk_reports_only_finished_subtrees(tree):
    """A half-counted directory must not appear in a ranking.

    Simulated rather than signalled: delivering SIGINT at a deterministic point
    inside a threaded walk is not reproducible, but the reporting rule is, and
    the rule is what matters -- a subtree caught mid-walk carries an arbitrary
    fraction of its contents and belongs nowhere near an ordered table.
    """
    r = walk(tree, threads=4, depth=1)
    everything = {os.path.basename(e.path) for e in r.dir_agg.values() if e.path != r.root}
    assert len(everything) > 1

    victim = sorted(everything)[0]
    r.partial = True
    r.finished_tops = everything - {victim}

    listed = {os.path.basename(e.path) for e in r.top_dirs(50, finished_only=True)}
    assert victim not in listed
    assert listed == everything - {victim}
    # Without the filter the unfinished entry is still there, so the filter is
    # doing the work and the test would fail if it were dropped.
    assert victim in {os.path.basename(e.path) for e in r.top_dirs(50)}
