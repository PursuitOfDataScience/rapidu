"""Reconciliation, and the refusals that keep it honest.

The behaviour under test is mostly *not* reporting things: a stale quota
snapshot, an unsettled tree, or an incomplete walk must each turn a difference
into INCONCLUSIVE rather than into a finding (Constraint 20).
"""

import os
import time

from rapidu import reconcile as rc
from rapidu.deleted import DeletedFile, DeletedScan
from rapidu.quota import QuotaRow, QuotaSnapshot
from rapidu.walk import SettleCheck, WalkResult

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


def test_a_blocked_verdict_still_offers_its_hypotheses():
    """Blocking the *finding* must not also suppress the explanations.

    Reaching GAP needs zero blockers, and on a real cluster the quota snapshot
    alone is routinely half an hour old against a 300s threshold -- so gating
    the candidate list on GAP made the module's entire explanatory payload
    unreachable on essentially every run. The verdict stays INCONCLUSIVE,
    because a stale number genuinely cannot support a finding; the candidates
    are hypotheses and are printed as "not asserted".
    """
    r = rc.reconcile(
        make_walk(1000), fresh_settle(), make_snap(50_000_000, age=1800.0), empty_scan(), "blocks"
    )
    assert r.verdict == rc.INCONCLUSIVE
    assert r.blockers, "the refusal itself must survive"
    assert r.candidates, "but the reader still gets somewhere to look"
    assert any("snapshot" in c for c in r.candidates)
    assert any("replication" in c for c in r.candidates)


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
    """No backend degrades to a note, and the note does not restate the reason.

    RD-4: the reason is a *backend* fact, printed once by the QUOTA panel and
    carried in the JSON document. This note is emitted once per kind, so a
    multi-line GPFS failure interpolated here appeared three times in one report
    and was longer than the report.
    """
    snap = QuotaSnapshot("none")
    snap.available = False
    snap.reason = "mmlsquota: No quota enabled file system found. Error code 22."
    r = rc.reconcile(make_walk(), fresh_settle(), snap, empty_scan(), "blocks")
    assert r.verdict == rc.NOT_COMPARED
    assert "no quota backend available" in r.notes[0]
    assert "QUOTA" in r.notes[0], "it has to say where the reason is"
    assert "Error code 22" not in r.notes[0]


def test_crossing_filesystems_blocks_a_finding():
    w = make_walk(1000)
    w.by_dev = {1: (500, 5), 2: (500, 5)}
    r = rc.reconcile(w, fresh_settle(), make_snap(50_000_000, age=1.0), empty_scan(), "blocks")
    assert r.verdict == rc.INCONCLUSIVE
    assert any("filesystem" in b for b in r.blockers)


# ---- a stat-free walk has nothing to reconcile ---------------------------


def count_only_walk(root=MOUNT):
    """What `-c` leaves behind: counts, and no bytes or ownership at all.

    `account_root` still records the root's own inode under the caller's uid,
    which is exactly what made the bug convincing -- `by_uid` was non-empty, so
    nothing downstream noticed the other 300 files were never stat'ed.
    """
    r = WalkResult(root)
    r.count_only = True
    r.files, r.dirs = 300, 20
    r.by_uid = {os.getuid(): (512, 1)}
    r.by_dev = {42: (512, 1)}
    return r


def test_count_mode_refuses_the_block_comparison():
    """`rdu <mount> -c -a` used to report the whole quota as an UNEXPLAINED GAP.

    A -c walk never calls stat, so its byte total is 0. Comparing that against a
    live quota figure manufactured a finding the size of the quota -- the exact
    failure this module exists to prevent.
    """
    rec = rc.reconcile(
        count_only_walk(), fresh_settle(), make_snap(used=700 << 20), empty_scan(), "blocks"
    )
    assert rec.verdict == rc.NOT_COMPARED
    assert "-c" in rec.notes[0]
    assert rec.gap is None


def test_count_mode_refuses_a_user_scoped_file_comparison():
    """-c cannot read who owns a file, so it cannot answer a user quota."""
    rec = rc.reconcile(
        count_only_walk(),
        fresh_settle(),
        make_snap(used=21638, kind="files", scope="user"),
        empty_scan(),
        "files",
    )
    assert rec.verdict == rc.NOT_COMPARED
    assert "who owns" in rec.notes[0]


def test_count_mode_file_comparison_is_blocked_by_hardlinks():
    """A fileset-scoped file quota is comparable, but never conclusive.

    -c counts one entry per name; the quota counts inodes. They differ by
    however many hard links the tree holds, which -c cannot know.
    """
    rec = rc.reconcile(
        count_only_walk(),
        fresh_settle(),
        make_snap(used=21638, kind="files", scope="fileset"),
        empty_scan(),
        "files",
    )
    assert rec.verdict == rc.INCONCLUSIVE
    assert any("one entry per name" in b for b in rec.blockers)


# ---- SUBTREE prints a number, so the number has to say what it is --------


def test_a_subtree_share_computed_from_an_incomplete_walk_is_a_lower_bound():
    """SUBTREE returns before the blocker list, and still prints a percentage.

    Every other verdict routes an unreadable directory or an interrupted walk into
    ``blockers`` and downgrades to INCONCLUSIVE. SUBTREE returns first, so
    ``blockers`` was empty and ``verdict_line`` read "this subtree is 25.0% of the
    fs quota figure" for a walk that had been interrupted and could not read
    11,267 of its own directories -- a floor over a fixed denominator, stated as a
    measurement. SUBTREE is not a finding and needs no downgrade; it is the figure
    that has to be labelled.
    """
    w = make_walk(1000, root=MOUNT + "/subdir")
    w.unreadable_dirs = [(MOUNT + "/subdir/x", "Permission denied")]
    w.unreadable_dirs_dropped = 11266
    w.unstatable = 40
    w.partial = True
    r = rc.reconcile(w, fresh_settle(), make_snap(4000), empty_scan(), "blocks")

    assert r.verdict == rc.SUBTREE
    assert r.walk_is_floor
    line = rc.verdict_line(r)
    assert line == "this subtree is at least 25.0% of the fs quota figure", line
    # And the note has to name the causes, since the blocker list never runs here.
    floor_notes = [n for n in r.notes if "floor" in n]
    assert len(floor_notes) == 1, r.notes
    note = floor_notes[0]
    assert "lower bound" in note
    assert "not a measurement" in note
    assert "11,267 directories could not be read" in note
    assert "40 entries could not be stat'ed" in note
    assert "interrupted" in note


def test_a_subtree_share_from_a_complete_walk_is_stated_plainly():
    """Control: the label must be attached to the floor, not to every subtree.

    Marking every SUBTREE "at least" would pass the test above and make the
    qualifier meaningless -- a hedge on a number that is not hedged is the same
    failure as an unhedged floor, one direction over. Identical walk, nothing
    unread, nothing unstat'ed, not interrupted.
    """
    w = make_walk(1000, root=MOUNT + "/subdir")
    assert w.complete
    r = rc.reconcile(w, fresh_settle(), make_snap(4000), empty_scan(), "blocks")

    assert r.verdict == rc.SUBTREE
    assert not r.walk_is_floor
    assert rc.verdict_line(r) == "this subtree is 25.0% of the fs quota figure"
    assert not [n for n in r.notes if "floor" in n or "lower bound" in n]


def gap_candidates(scan):
    """The candidate causes for a large positive gap, given one /proc scan."""
    r = rc.reconcile(make_walk(1000), fresh_settle(), make_snap(50_000_000), scan, "blocks")
    assert r.gap > 0, r.gap
    return r.candidates


def test_a_namespaced_scan_does_not_blame_zero_other_users():
    """A cause naming a population the scan measured as empty is not a cause.

    ``DeletedScan.complete`` is False for three unrelated reasons -- EACCES, a PID
    namespace, an abandoned sweep -- and the candidate list was gated on it while
    printing only the first. Inside Apptainer, which is how most work on this
    cluster runs, ``unreadable_pids`` is 0 and every gap listed
    "unlinked-but-open files held by 0 processes belonging to other users" while
    the limit that actually applied went unmentioned.
    """
    scan = DeletedScan()
    scan.scanned_pids = 1
    scan.namespaced = True
    assert not scan.complete
    cands = gap_candidates(scan)

    assert not [c for c in cands if "0 process" in c], cands
    assert not [c for c in cands if "belonging to other users" in c], cands
    assert [c for c in cands if "outside this PID namespace" in c], cands


def test_an_abandoned_sweep_names_itself_rather_than_other_users():
    """The same substitution, for the other reason ``complete`` goes False."""
    scan = DeletedScan()
    scan.scanned_pids = 12
    scan.timed_out = True
    assert not scan.complete
    cands = gap_candidates(scan)

    assert not [c for c in cands if "0 process" in c], cands
    assert [c for c in cands if "abandoned on an unresponsive mount" in c], cands


def test_a_scan_that_never_ran_is_not_credited_with_covering_this_node():
    """``complete`` said True for a scan that did not happen, which reads backwards.

    With ``--no-deleted`` -- or on a platform with no ``/proc`` -- ``available`` is
    False and every counter is 0, so ``complete`` said yes and the candidate list
    offered only "this scan only sees this node". An unlinked-but-open file on
    *this* node is precisely what a scan that did not run cannot rule out, so the
    one caveat printed was the one that did not apply.
    """
    scan = DeletedScan()
    scan.available = False
    scan.reason = "skipped (--no-deleted)"
    # `complete` now tests `available` first, so the flag agrees with the wording:
    # see `test_deleted.TestCompleteRequiresAScanThatRan`.
    assert not scan.complete
    cands = gap_candidates(scan)

    assert not [c for c in cands if "only sees this node" in c], cands
    did_not_run = [c for c in cands if "did not run" in c]
    assert len(did_not_run) == 1, cands
    assert "this node" in did_not_run[0]
    assert "skipped (--no-deleted)" in did_not_run[0]
    # The rest of the list is untouched: this is about coverage, not about
    # withholding the other hypotheses.
    assert [c for c in cands if "snapshots" in c], cands


def test_refused_processes_are_still_named_as_a_cause():
    """Control: the EACCES caveat is the point of the list and must survive.

    Silencing the other-users candidate whenever ``complete`` is False would pass
    all three tests above and delete the module's most useful hypothesis -- on a
    shared login node 579 of 646 processes are another user's, which is the whole
    reason a group quota can show a gap nobody can attribute. Both figures also
    have to agree with their noun; the old wording was a fixed plural.
    """
    scan = DeletedScan()
    scan.scanned_pids = 67
    scan.unreadable_pids = 579
    many = gap_candidates(scan)
    assert [c for c in many if "579 processes belonging to other users" in c], many
    assert [c for c in many if "only sees this node" in c], many

    scan.unreadable_pids = 1
    one = gap_candidates(scan)
    assert [c for c in one if "1 process belonging to other users" in c], one


def test_a_complete_local_scan_lists_only_the_other_nodes_caveat():
    """Control: a scan with nothing to caveat must not invent one."""
    scan = DeletedScan()
    scan.scanned_pids = 646
    assert scan.complete
    cands = gap_candidates(scan)

    assert [c for c in cands if "only sees this node" in c], cands
    assert not [c for c in cands if "belonging to other users" in c], cands
    assert not [c for c in cands if "PID namespace" in c], cands
    assert not [c for c in cands if "did not run" in c], cands
