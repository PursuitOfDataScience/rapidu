"""Reconciliation, and the refusals that keep it honest.

The behaviour under test is mostly *not* reporting things: a stale quota
snapshot, an unsettled tree, or an incomplete walk must each turn a difference
into INCONCLUSIVE rather than into a finding (Constraint 20).
"""

import os
import time

from slurmdisk import reconcile as rc
from slurmdisk.deleted import DeletedFile, DeletedScan
from slurmdisk.quota import QuotaRow, QuotaSnapshot
from slurmdisk.walk import SettleCheck, WalkResult

MOUNT = "/mnt/fake"


def make_walk(size=1000, inodes=10, root=MOUNT, uid=None):
    r = WalkResult(root)
    r.size = size
    r.files = inodes - 1
    r.dirs = 1
    uid = os.getuid() if uid is None else uid
    r.by_uid = {uid: (size, inodes)}
    r.by_dev = {42: (size, inodes)}
    return r


def make_snap(used=1000, kind="blocks", scope="user", age=0.0, mount=MOUNT):
    s = QuotaSnapshot("test")
    s.available = True
    s.read_at = time.time()
    s.taken_at = s.read_at - age
    s.rows = [QuotaRow("fs", kind, scope, used, used * 10, used * 11, "", mount)]
    return s


def fresh_settle():
    c = SettleCheck()
    c.ran = True
    c.gap = 60.0  # long enough that a null result means something
    return c


def empty_scan():
    return DeletedScan()


def test_closes_when_everything_agrees():
    r = rc.reconcile(make_walk(1000), fresh_settle(), make_snap(1000), empty_scan(), "blocks")
    assert r.verdict == rc.CLOSES
    assert r.gap == 0


def test_small_difference_is_within_tolerance():
    r = rc.reconcile(make_walk(1000), fresh_settle(), make_snap(1005), empty_scan(), "blocks")
    assert r.verdict == rc.CLOSES


def test_tolerance_floor_cannot_swallow_the_measurement():
    """An 8 MiB floor must not make every small quota 'reconcile'."""
    assert rc._tolerance(100 * (1 << 20), "blocks") < 100 * (1 << 20)
    assert rc._tolerance(10, "files") == rc.MIN_TOLERANCE_FILES


def test_stale_snapshot_blocks_a_finding():
    """The core refusal: a 28-minute-old number cannot indict a live tree."""
    r = rc.reconcile(
        make_walk(1000), fresh_settle(), make_snap(50_000_000, age=1800.0), empty_scan(), "blocks"
    )
    assert r.verdict == rc.INCONCLUSIVE
    assert any("snapshot" in b for b in r.blockers)
    assert not r.candidates


def test_unknown_snapshot_age_blocks_a_finding():
    snap = make_snap(50_000_000)
    snap.taken_at = None
    r = rc.reconcile(make_walk(1000), fresh_settle(), snap, empty_scan(), "blocks")
    assert r.verdict == rc.INCONCLUSIVE
    assert any("no timestamp" in b for b in r.blockers)


def test_unsettled_tree_blocks_a_finding():
    settle = fresh_settle()
    settle.drift = 5 << 20
    r = rc.reconcile(make_walk(1000), settle, make_snap(50_000_000), empty_scan(), "blocks")
    assert r.verdict == rc.INCONCLUSIVE
    assert any("not settled" in b for b in r.blockers)


def test_drift_in_either_direction_blocks():
    """GPFS over-allocates as well as under-allocates."""
    for drift in (5 << 20, -(5 << 20)):
        settle = fresh_settle()
        settle.drift = drift
        r = rc.reconcile(make_walk(1000), settle, make_snap(50_000_000), empty_scan(), "blocks")
        assert r.verdict == rc.INCONCLUSIVE


def test_incomplete_walk_blocks_a_finding():
    w = make_walk(1000)
    w.unreadable_dirs = [("/mnt/fake/x", "Permission denied")]
    r = rc.reconcile(w, fresh_settle(), make_snap(50_000_000), empty_scan(), "blocks")
    assert r.verdict == rc.INCONCLUSIVE
    assert any("floor" in b for b in r.blockers)


def test_gap_when_all_inputs_are_clean():
    snap = make_snap(50_000_000, age=1.0)
    r = rc.reconcile(make_walk(1000), fresh_settle(), snap, empty_scan(), "blocks")
    assert r.verdict == rc.GAP
    assert r.candidates, "a gap must come with candidate explanations"
    # None of them may be phrased as a conclusion.
    assert all("must be" not in c and "is caused by" not in c for c in r.candidates)


def test_subtree_is_not_a_gap():
    """Walking part of a quota'd tree is not a discrepancy."""
    w = make_walk(1000, root=MOUNT + "/subdir")
    r = rc.reconcile(w, fresh_settle(), make_snap(50_000_000), empty_scan(), "blocks")
    assert r.verdict == rc.SUBTREE
    assert r.share is not None


def test_deleted_files_count_toward_what_we_can_see():
    scan = DeletedScan()
    f = DeletedFile(1, 2, 500, "/mnt/fake/gone.bin")
    f.add_holder(123, "python")
    scan.files = [f]
    r = rc.reconcile(make_walk(500), fresh_settle(), make_snap(1000), scan, "blocks")
    assert r.deleted_value == 500
    assert r.accounted == 1000
    assert r.verdict == rc.CLOSES


def test_files_kind_counts_directories():
    """A files-quota charges for directory inodes, so the walk must include them."""
    w = make_walk(size=1, inodes=21286)
    snap = make_snap(21276, kind="files", age=1.0)
    r = rc.reconcile(w, fresh_settle(), snap, empty_scan(), "files")
    assert r.walk_value == 21286
    assert r.verdict == rc.CLOSES


def test_unmapped_path_is_not_compared():
    snap = make_snap(1000, mount="/somewhere/else")
    r = rc.reconcile(make_walk(1000), fresh_settle(), snap, empty_scan(), "blocks")
    assert r.verdict == rc.NOT_COMPARED
    assert r.notes


def test_absent_quota_backend_degrades():
    snap = QuotaSnapshot("none")
    snap.available = False
    snap.reason = "`quota` is not on PATH"
    r = rc.reconcile(make_walk(), fresh_settle(), snap, empty_scan(), "blocks")
    assert r.verdict == rc.NOT_COMPARED
    assert "not on PATH" in r.notes[0]


def test_crossing_filesystems_blocks_a_finding():
    w = make_walk(1000)
    w.by_dev = {1: (500, 5), 2: (500, 5)}
    r = rc.reconcile(w, fresh_settle(), make_snap(50_000_000, age=1.0), empty_scan(), "blocks")
    assert r.verdict == rc.INCONCLUSIVE
    assert any("filesystem" in b for b in r.blockers)
