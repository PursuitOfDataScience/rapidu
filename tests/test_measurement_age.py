"""Two flags that answered "can this figure be trusted" from too narrow a fact.

Polish pass, 2026-09-01. Both defects have the same shape.

* ``reconcile``'s snapshot-age gate could not see the walk it was gating. The cap
  is about how far apart the two sides of the comparison were taken, and the gate
  tested only ``QuotaSnapshot.age_seconds`` -- which, deliberately, is how stale
  the quota figures were *when they were read*, and ``cli.cmd_walk`` reads them
  before the walk starts. A backend that computes on demand reports 0.0 there, so
  a half-hour walk was reconciled against a figure half an hour older than it,
  with ``blockers == []`` and a confident ``gap``. Even when the gate did fire,
  the sentence understated the separation by the whole duration of the walk.
* ``DeletedScan.complete`` ignored ``available``, so a sweep that never ran --
  ``--no-deleted``, or a platform with no ``/proc`` -- published
  ``"complete": true`` beside ``"available": false``.
"""

import json
import os
import time

from rapidu import cli, report
from rapidu import reconcile as rc
from rapidu.deleted import DeletedScan
from rapidu.quota import QuotaRow, QuotaSnapshot
from rapidu.walk import SettleCheck, WalkResult

MOUNT = "/mnt/fake"


def _walk(size=1000, elapsed=0.0):
    """A complete, settled walk of the whole mount, so nothing else blocks."""
    res = WalkResult(MOUNT)
    res.size = size
    res.files = 9
    res.dirs = 1
    res.elapsed = elapsed
    res.by_uid = {os.getuid(): (size, 10)}
    res.by_dev = {42: (size, 10)}
    return res


def _snap(used=50_000_000, age=0.0):
    snap = QuotaSnapshot("test")
    snap.available = True
    snap.read_at = time.time()
    snap.taken_at = snap.read_at - age
    snap.rows = [QuotaRow("fs", "blocks", "user", used, used * 10, used * 11, "", MOUNT)]
    return snap


def _settle():
    check = SettleCheck()
    check.ran = True
    check.gap = 60.0
    return check


def _reconcile(age, elapsed, cap=rc.DEFAULT_MAX_SNAPSHOT_AGE_S):
    return rc.reconcile(
        _walk(elapsed=elapsed),
        _settle(),
        _snap(age=age),
        DeletedScan(),
        "blocks",
        cap,
    )


def _age_blockers(rec):
    return [b for b in rec.blockers if "snapshot" in b]


# --- the snapshot-age gate must include the walk's own duration ---------------


def test_a_long_walk_against_a_fresh_snapshot_is_not_a_finding():
    """Teeth. `mmlsquota` and friends compute on demand: taken_at == read_at.

    Before the fix this returned GAP with an empty blocker list -- a 47 GiB
    discrepancy asserted between a quota figure and a walk taken half an hour
    apart, and nothing in the report or the JSON said the two clocks differed.
    """
    rec = _reconcile(age=0.0, elapsed=1800.0)

    assert rec.verdict == rc.INCONCLUSIVE
    assert _age_blockers(rec), rec.blockers


def test_the_blocker_reports_the_gap_between_the_two_measurements():
    """Teeth. "a snapshot taken 400s ago" was off by the whole walk.

    400 + 1800 = 2200, and both components are named so a reader can tell a slow
    backend from a slow walk.
    """
    (text,) = _age_blockers(_reconcile(age=400.0, elapsed=1800.0))

    assert "2200s" in text, text
    assert "400s stale when read" in text, text
    assert "1800s walk" in text, text
    # And it no longer claims the figure is that old *now*, which was the reading
    # "ago" invited: it is that far from the other side of the comparison.
    assert "2200s ago" not in text, text


def test_the_split_is_omitted_when_the_walk_took_no_time():
    """CONTROL. Passes before and after: a sub-second walk contributes nothing to
    the separation, so it is not narrated and the sentence reads as it always did."""
    (text,) = _age_blockers(_reconcile(age=1800.0, elapsed=0.4))

    assert "1800s" in text, text
    assert "stale when read" not in text, text


def test_a_fresh_snapshot_and_a_fast_walk_still_reaches_a_verdict():
    """CONTROL. Passes before and after: the gate must not fire on a sound pair.

    Summing two numbers into a threshold test is one line away from blockering
    every comparison in the package, which would "fix" the bug by deleting the
    verdict. 0.5s against a 300s cap has to stay a finding.
    """
    rec = _reconcile(age=0.0, elapsed=0.5)

    assert rec.verdict == rc.GAP
    assert rec.blockers == []


def test_a_sum_below_the_cap_is_still_below_the_cap():
    """CONTROL. Passes before and after. 100 + 100 < 300 -- no blocker either way."""
    rec = _reconcile(age=100.0, elapsed=100.0)

    assert rec.verdict == rc.GAP
    assert rec.blockers == []


def test_a_stale_snapshot_alone_still_blocks():
    """CONTROL. Passes before and after: the original refusal is untouched."""
    rec = _reconcile(age=1800.0, elapsed=0.0)

    assert rec.verdict == rc.INCONCLUSIVE
    assert any("1800s" in b for b in _age_blockers(rec)), rec.blockers


def test_a_backend_with_no_timestamp_still_blocks():
    """CONTROL. Passes before and after: an unknown age is not zero.

    The `None` branch is deliberately left out of the sum -- there is nothing to
    add to -- and folding the walk in must not have swallowed it.
    """
    snap = _snap()
    snap.taken_at = None
    rec = rc.reconcile(_walk(elapsed=1800.0), _settle(), snap, DeletedScan(), "blocks")

    assert rec.verdict == rc.INCONCLUSIVE
    assert any("no timestamp" in b for b in rec.blockers), rec.blockers


# --- `complete` must mean "and the sweep happened" ---------------------------


def test_a_scan_that_never_ran_is_not_complete():
    """Teeth. Every counter is 0 when `available` is False, so all three tests
    passed and the flag said the sweep had seen everything."""
    scan = DeletedScan()
    scan.available = False
    scan.reason = "skipped (--no-deleted)"

    assert scan.complete is False


def test_the_json_does_not_publish_a_complete_scan_that_did_not_run(tmp_path, capsys):
    """Teeth, end to end: `rdu --no-deleted --json` said `"complete": true`."""
    (tmp_path / "f").write_bytes(b"x" * 4096)
    assert cli.main([str(tmp_path), "--no-quota", "--no-deleted", "--json"]) == cli.EXIT_OK
    block = json.loads(capsys.readouterr().out)["deleted_but_open"]

    assert block["available"] is False
    assert block["reason"] == "skipped (--no-deleted)"
    assert block["complete"] is False


def test_a_clean_sweep_is_still_complete():
    """CONTROL. Passes before and after -- the flag's positive case is the point.

    Making `complete` False whenever anything at all is unusual would satisfy the
    two tests above and destroy the field: a scan that ran, saw every process and
    had nothing hidden from it must still say so.
    """
    scan = DeletedScan()
    scan.scanned_pids = 646

    assert scan.available is True
    assert scan.complete is True
    assert report.to_json(None, None, None, scan, None, 10)["deleted_but_open"]["complete"] is True


def test_each_original_reason_still_flips_the_flag():
    """CONTROL. Passes before and after: `available` is a fourth term, not a swap."""
    for field in ("unreadable_pids", "namespaced", "timed_out"):
        scan = DeletedScan()
        scan.scanned_pids = 646
        setattr(scan, field, 1 if field == "unreadable_pids" else True)
        assert scan.complete is False, field


def test_under_carries_the_flag_into_the_narrowed_scan():
    """Teeth. `cli.cmd_walk` reconciles against `scan.under(path)`, not the sweep
    itself, so the object the report is built from is the one that has to answer.
    `under` already copied `available`; the flag just never read it."""
    scan = DeletedScan()
    scan.available = False

    assert scan.under("/").complete is False


def test_under_does_not_downgrade_a_scan_that_ran():
    """CONTROL. Passes before and after: narrowing is not itself a limit on
    coverage. `under` restricts the file list by path; it does not make the sweep
    that produced it any less complete."""
    ran = DeletedScan()
    ran.scanned_pids = 12

    assert ran.under("/").complete is True
