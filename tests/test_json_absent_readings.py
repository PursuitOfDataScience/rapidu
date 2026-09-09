"""Two figures the document published as zero without having measured them.

Constraint 10, which :func:`report._unmeasured` states and enforces: *``None``
is not zero. A caller that has no measurement passes ``None`` and gets ``n/a``,
never ``0.0 B``.* Both cases below are that rule applied to a branch where a
sibling field already obeys it and these did not.

**1. Drift from a re-stat that never ran.** Under ``-c`` no stat is taken, so
``recheck_settling`` has nothing to compare and ``recheck_ran`` is ``false``.
``recent_files``, ``touched_files`` and ``future_mtime_files`` were already
nulled, and ``settled`` is explicitly ``None if res.count_only`` -- with a
comment calling that "the strongest claim in this section, made by an instrument
that was switched off". ``drift_bytes``, ``vanished_files`` and
``vanished_allocated_bytes`` were published raw, so a walk that read no sizes
reported that none of them had changed. The block's own words for that value,
twelve lines below where it was emitted: "``drift_bytes: 0`` is an absent
reading and not a settled tree".

**2. A tolerance for a comparison that never happened.** With no quota row
mapping to the path, ``reconcile`` returns ``verdict: not-compared`` and every
figure describing the comparison is ``None`` -- ``walked``, ``accounted``,
``quota``, ``difference``, ``share_of_quota``. ``deleted_but_open`` and
``tolerance`` read ``0``, because those two are the only fields
``Reconciliation.__init__`` starts at a number rather than at ``None``. Measured
on a **full** walk, so this is the not-compared branch and not ``-c``:
``walked: null`` beside ``tolerance: 0``. Neither is a measurement --
``_tolerance()`` needs a ``quota_value`` there is not one of, and
``deleted_but_open`` is an addend to a ``walked`` that is absent.

The controls are the cases neither fix may touch: a real walk still publishes
all three settling figures, a matched quota row still publishes both
reconciliation figures, the fields that describe the *check* rather than the
tree stay numeric, and ``Reconciliation`` keeps the numeric defaults its own
``within_tolerance`` reads.
"""

import pathlib

from rapidu import reconcile as rc
from rapidu import report
from rapidu.deleted import DeletedScan
from rapidu.quota import QuotaRow, QuotaSnapshot
from rapidu.walk import SettleCheck, recheck_settling, walk

_SETTLING = ("drift_bytes", "vanished_files", "vanished_allocated_bytes")


def _tree(root: pathlib.Path) -> str:
    """A small tree with real bytes in it, so a size is there to be read."""
    (root / "a").mkdir(parents=True)
    (root / "a" / "big.bin").write_bytes(b"x" * 4096)
    (root / "small.txt").write_text("hi\n")
    return str(root)


def _settled() -> SettleCheck:
    chk = SettleCheck()
    chk.ran = True
    chk.gap = 60.0  # long enough that a null result means something
    return chk


def _snap(mount: str, used: int, kind: str = "blocks") -> QuotaSnapshot:
    snap = QuotaSnapshot("test")
    # `available` matters to which branch this exercises: left False, `reconcile`
    # returns not-compared for "no quota backend available", which is a different
    # reason and would have tested the wrong thing.
    snap.available = True
    snap.read_at = 1_788_600_000.0  # fixed: a wall clock would differ per run
    snap.taken_at = snap.read_at
    snap.rows = [QuotaRow("fs", kind, "user", used, used, used, "", mount)]
    return snap


def _recon_row(res, snap):
    recs = [rc.reconcile(res, _settled(), snap, DeletedScan(), "blocks")]
    doc = report.to_json(res, _settled(), snap, DeletedScan(), recs)
    return doc["reconciliation"][0]


class TestDriftFromARestatThatNeverRan:
    def test_count_only_publishes_no_drift_figures(self, tmp_path) -> None:
        counted = walk(_tree(tmp_path / "counted"), threads=1, depth=1, count_only=True)
        settling = report.to_json(counted, recheck_settling(counted), None, None, None)["settling"]
        assert settling["recheck_ran"] is False
        for key in _SETTLING:
            assert settling[key] is None, (key, settling[key])

    def test_a_real_walk_still_publishes_all_three(self, tmp_path) -> None:
        """The control: nulling an absent reading must not null a present one."""
        res = walk(_tree(tmp_path / "full"), threads=1, depth=1)
        settling = report.to_json(res, recheck_settling(res), None, None, None)["settling"]
        for key in _SETTLING:
            assert isinstance(settling[key], int), (key, settling[key])

    def test_the_fields_describing_the_check_stay_numeric(self, tmp_path) -> None:
        """`_unscanned`'s carve-out, applied here.

        Figures that describe the *check* rather than the tree are how a
        consumer learns to expect the nulls, so they keep their values.
        """
        counted = walk(_tree(tmp_path / "sweep"), threads=1, depth=1, count_only=True)
        settling = report.to_json(counted, recheck_settling(counted), None, None, None)["settling"]
        assert settling["rechecked"] == 0
        assert settling["recheck_gap_seconds"] == 0.0
        assert settling["recheck_ran"] is False
        assert settling["moved"] is False


class TestAToleranceForAComparisonThatNeverHappened:
    def test_not_compared_nulls_what_its_siblings_already_null(self, tmp_path) -> None:
        res = walk(_tree(tmp_path / "orphan"), threads=1, depth=1)
        row = _recon_row(res, _snap("/rapidu-no-such-mount", 10 << 20))
        assert row["verdict"] == rc.NOT_COMPARED, row["notes"]
        # Pin WHICH not-compared this is: "no backend available" reaches the same
        # verdict by a different route, and the branch under test is the one where
        # a backend answered and no row maps to the path.
        assert any("no mount point matching this path" in n for n in row["notes"]), row["notes"]
        # The siblings, unchanged -- they are what makes the two below wrong.
        assert row["walked"] is None
        assert row["difference"] is None
        assert row["deleted_but_open"] is None
        assert row["tolerance"] is None

    def test_a_matched_row_still_publishes_both(self, tmp_path) -> None:
        """The control: a real comparison keeps its threshold and its addend."""
        root = _tree(tmp_path / "matched")
        res = walk(root, threads=1, depth=1)
        row = _recon_row(res, _snap(root, max(1, res.size)))
        assert row["verdict"] != rc.NOT_COMPARED, row["notes"]
        assert isinstance(row["tolerance"], int), row["tolerance"]
        assert isinstance(row["deleted_but_open"], int), row["deleted_but_open"]

    def test_the_object_keeps_the_numeric_defaults_it_reads_itself(self) -> None:
        """Only the published figure moved.

        ``within_tolerance`` compares ``abs(self.gap)`` against
        ``self.tolerance``; changing the attribute to ``None`` would make that a
        TypeError the moment a gap appeared. The fix is in the document, not in
        the object.
        """
        rec = rc.Reconciliation("blocks")
        assert rec.tolerance == 0
        assert rec.deleted_value == 0
        assert rec.within_tolerance is False  # no gap, so nothing agrees
