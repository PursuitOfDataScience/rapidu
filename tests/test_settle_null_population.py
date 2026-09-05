"""What ``rechecked: 0`` means, and what the document is allowed to say about it.

``checked == 0`` reaches the settling block by three different roads and the
document has to keep them apart:

* the recent population was **empty** -- nothing was written inside the window,
  so there is nothing that could be drifting and a null result is fine;
* the population **existed and was deleted** underneath the re-stat, so
  ``drift == 0`` is the absence of a reading (``recheck_measured_nothing``,
  already pinned in ``test_walk_throttles``);
* the walk **never read an mtime**, because ``-c`` skips ``stat`` entirely, so
  the population is unknown rather than empty.

The first and third are indistinguishable inside the check -- both arrive with an
empty ``recent_sample`` and ``sampled_of == 0`` -- and were told apart only by
``cmd_walk`` forcing ``--no-settle-check`` under ``-c``. And the first published
a gap it had not waited: the early return assigned ``chk.gap = wait`` without
sleeping, so ``rdu --json --settle-window 1 --settle-wait 30`` returned in 0.08s
and reported ``recheck_gap_seconds: 30.0``. ``conclusive`` is derived from that
number, which is why the same two runs of the same non-measurement disagreed
about the verdict across ``--settle-wait 5``.

The terminal shows none of this: ``render_settle`` returns ``[]`` for an empty
population, so both defects were visible only to a machine consumer, in the two
fields it would use to weigh the result.
"""

import io
import os

from rapidu import report
from rapidu.walk import MIN_CONCLUSIVE_GAP_S, SettleCheck, WalkResult, recheck_settling, walk

# Every key the settling block publishes about the re-stat itself, i.e. every
# field the fix below could reach. Pinned as literals in the control so the
# ordinary full-walk block is asserted whole rather than field by field.
_RESTAT_KEYS = (
    "rechecked",
    "recheck_gap_seconds",
    "recheck_ran",
    "conclusive",
    "settled",
    "moved",
    "drift_bytes",
    "sampled",
    "vanished_files",
    "vanished_allocated_bytes",
    "recheck_measured_nothing",
    "headline_provisional",
)


def _tree(root, nfiles=8, payload=65536):
    os.makedirs(root)
    for i in range(nfiles):
        with io.open(os.path.join(root, "f%02d" % i), "wb") as handle:
            handle.write(b"q" * payload)
    return root


def _settling(res, chk):
    return report.to_json(res, chk, None, None, None)["settling"]


def _idle():
    """A walk result whose recent population is empty, with the window *on*.

    Built directly rather than backdated on disk: ``os.utime`` cannot rewind
    ``st_ctime``, so a backdated file lands in ``touched_files`` and the
    population is not empty at all -- which is the split
    ``test_the_settle_window_shrinks_the_population_it_reports_on`` pins. This is
    the idle tree the window exists to report on, and it keeps the window at its
    default so the verdict cannot be read as an artefact of ``--settle-window 0``.
    """
    res = WalkResult("/tmp/idle")
    res.settle_window = 120.0
    res.files = 8
    res.size = 8 << 16
    res.apparent = 8 << 16
    return res


# ---------------------------------------------------------------------------
# 1. an empty population does not get to publish a wait it never took


def test_an_empty_population_publishes_no_gap_it_did_not_wait(tmp_path):
    """The early return does not sleep, so there is no gap to report.

    ``gap`` is documented as "seconds between the walk reading and the re-stat".
    Nothing was re-stat'ed and nothing elapsed, and the value has to say so: the
    branch used to hand back ``wait`` verbatim, which is a duration the caller
    can only read as time the tool spent.

    The elapsed assertion is deliberately loose -- the branch provably does not
    sleep in either state, so it is here to state that ``gap`` is not a reading
    of the clock, not to time the machine.
    """
    root = _tree(str(tmp_path / "off"))
    res = walk(root, threads=2, depth=1, settle_window=0.0)
    assert res.recent_sample == [] and (res.recent_files, res.touched_files) == (0, 0)

    wait = 4 * MIN_CONCLUSIVE_GAP_S
    started = os.times()[4]
    chk = recheck_settling(res, wait)
    assert os.times()[4] - started < MIN_CONCLUSIVE_GAP_S, "this branch must not sleep"

    assert chk.checked == 0 and chk.gone == 0
    assert chk.gap == 0.0, "a wait that was not taken is not a gap"
    assert _settling(res, chk)["recheck_gap_seconds"] == 0.0


def test_the_document_for_an_empty_population_does_not_move_with_the_wait(tmp_path):
    """Same tree, same non-measurement, so the whole block has to agree.

    ``conclusive`` reads the gap and the gap read ``wait``, so ``--settle-wait
    0`` and ``--settle-wait 30`` published different verdicts for two runs that
    each observed exactly nothing and each returned instantly. With the gap no
    longer invented, the block is one block.

    ``conclusive: false`` is the right end of that: this check measured nothing,
    so there is no null result of *its* to believe. The verdict a reader wants is
    ``settled``, which ``to_json`` takes from the walk's own recent-file counts
    being zero -- true, and true for a reason the re-stat had no part in.
    """
    root = _tree(str(tmp_path / "either"))
    res = walk(root, threads=2, depth=1, settle_window=0.0)

    quick = _settling(res, recheck_settling(res, 0.0))
    patient = _settling(res, recheck_settling(res, 4 * MIN_CONCLUSIVE_GAP_S))
    assert quick == patient, "the wait changed the document without changing the reading"
    assert quick["settled"] is True, "nothing was written recently, so nothing is unsettled"
    assert quick["recheck_ran"] is True
    assert quick["conclusive"] is False, "the check measured nothing, so it settles nothing"
    assert quick["recheck_measured_nothing"] is False, "and nothing was deleted from under it"


def test_the_gap_is_zero_with_the_window_on_too():
    """``--settle-window 0`` is not what makes this happen.

    The window is at its default here and the population is still empty, which is
    the idle tree rather than the switched-off check -- and the reported gap is
    still zero rather than whatever was asked for, at every wait including the
    two either side of ``MIN_CONCLUSIVE_GAP_S``.
    """
    res = _idle()
    for wait in (0.0, MIN_CONCLUSIVE_GAP_S - 0.1, MIN_CONCLUSIVE_GAP_S, 60.0):
        chk = recheck_settling(res, wait)
        assert (chk.ran, chk.checked, chk.gap) == (True, 0, 0.0), wait
        assert chk.conclusive is False, wait
        assert chk.recheck_measured_nothing is False, wait


# ---------------------------------------------------------------------------
# 2. a stat-free walk hands back a check that did not run


def test_a_stat_free_walk_cannot_hand_back_a_check_that_ran(tmp_path):
    """``-c`` read no mtime, so its empty sample is an absence and not a zero.

    Handing back ``ran=True`` dressed that absence as a reading, and a wait long
    enough for the gap test then believed it: measured before the guard,
    ``recheck_settling(counted, 6.0)`` returned ``ran=True gap=6.0
    conclusive=True`` over five freshly written files -- "believe this null" from
    a walk with no mtime in it.
    """
    root = _tree(str(tmp_path / "lean"), nfiles=5)
    counted = walk(root, threads=2, depth=1, count_only=True)
    assert counted.recent_sample == []

    chk = recheck_settling(counted, 4 * MIN_CONCLUSIVE_GAP_S)
    assert chk.ran is False, "no re-stat is possible on a stat-free walk"
    assert (chk.checked, chk.gone, chk.gap) == (0, 0, 0.0)
    assert chk.conclusive is False, "an unmeasured population is not a believable null"


def test_the_stat_free_document_does_not_depend_on_the_cli_skipping_the_check(tmp_path):
    """``cmd_walk`` forces ``--no-settle-check`` under ``-c``; that is not the guard.

    The published block has to be the same whether the caller skipped the check
    or asked for it, because under ``-c`` there is nothing to ask for. Compared
    against the ``SettleCheck()`` the CLI substitutes, so this pins the document
    the CLI actually emits today rather than a reading of it.
    """
    root = _tree(str(tmp_path / "lean2"), nfiles=5)
    counted = walk(root, threads=2, depth=1, count_only=True)

    skipped = _settling(counted, SettleCheck())
    asked = _settling(counted, recheck_settling(counted, 4 * MIN_CONCLUSIVE_GAP_S))
    assert asked == skipped

    assert asked["recheck_ran"] is False
    assert asked["conclusive"] is False
    assert asked["settled"] is None
    assert asked["rechecked"] == 0


# ---------------------------------------------------------------------------
# controls


def test_a_full_walk_that_re_stats_is_unchanged(tmp_path):
    """CONTROL -- passes before and after. The ordinary block, pinned whole.

    Eight files written moments earlier, all eight re-stat'ed, nothing deleted
    and nothing moved: every field about the re-stat is a literal here, and the
    remaining fields are asserted to be the walk's own measurements. The two byte
    figures come off ``res`` rather than from a constant because a literal there
    would be asserting the block size of whatever filesystem ``tmp_path`` landed
    on -- the mistake ``test_freed_materiality_bound`` documents.

    The gap is assigned rather than slept: ``MIN_CONCLUSIVE_GAP_S`` is 5s, no test
    may sleep for it, and the gap is an input to the judgement rather than an
    observation about the tree -- ``test_walk_throttles._believable`` and
    ``test_json_terminal_parity`` both say so in those words.
    """
    root = _tree(str(tmp_path / "fresh"))
    res = walk(root, threads=2, depth=1)
    assert res.recent_files == 8 and len(res.recent_sample) == 8

    chk = recheck_settling(res, 0.0)
    chk.gap = 60.0
    assert chk.checked == 8 and chk.gone == 0

    doc = _settling(res, chk)
    assert {k: doc[k] for k in _RESTAT_KEYS} == {
        "rechecked": 8,
        "recheck_gap_seconds": 60.0,
        "recheck_ran": True,
        "conclusive": True,
        "settled": True,
        "moved": False,
        "drift_bytes": 0,
        "sampled": False,
        "vanished_files": 0,
        "vanished_allocated_bytes": 0,
        "recheck_measured_nothing": False,
        "headline_provisional": False,
    }
    # ...and the fields that are the walk's, not the re-stat's.
    assert doc["window_seconds"] == res.settle_window
    assert (doc["recent_files"], doc["touched_files"]) == (8, 0)
    assert doc["future_mtime_files"] == 0
    assert doc["recent_allocated_bytes"] == res.recent_size
    assert doc["recent_apparent_bytes"] == res.recent_apparent
    assert doc["unlanded_bytes"] == max(0, res.recent_apparent - res.recent_size)
    assert sorted(doc) == sorted(_RESTAT_KEYS + tuple(k for k in doc if k not in _RESTAT_KEYS))


def test_a_deleted_sample_is_still_an_inconclusive_null(tmp_path):
    """CONTROL -- passes before and after; the blind instrument stays blind.

    This is the case the empty-population branch must not swallow: the population
    was not empty, it was *unlinked* between the walk and the re-stat, so
    ``checked == 0`` is a missing reading. ``sampled_of`` is what tells the two
    apart, and a 60s gap does not buy the verdict back.
    """
    root = _tree(str(tmp_path / "gone"))
    res = walk(root, threads=2, depth=1)
    for name in os.listdir(res.root):
        os.unlink(os.path.join(res.root, name))

    chk = recheck_settling(res, 0.0)
    chk.gap = 60.0
    assert (chk.sampled_of, chk.checked, chk.gone) == (8, 0, 8)
    assert chk.recheck_measured_nothing is True
    assert chk.conclusive is False, "a re-stat that re-stat'ed nothing cannot answer this"
    assert _settling(res, chk)["settled"] is None


def test_the_terminal_still_says_nothing_about_an_empty_population(tmp_path):
    """CONTROL -- passes before and after. This was a document-only defect.

    ``render_settle`` returns ``[]`` when there is no recent population, and
    ``_provisional_note``'s settle branches are all gated on materiality, which
    ``moving == 0`` fails. Neither view ever printed the fabricated gap, and
    neither view gains a line now.
    """
    root = _tree(str(tmp_path / "quiet"))
    res = walk(root, threads=2, depth=1, settle_window=0.0)
    chk = recheck_settling(res, 4 * MIN_CONCLUSIVE_GAP_S)
    style = report.ui.resolve_style("never")
    assert report.render_settle(res, chk, style) == []
    assert report._provisional_note(res, chk, style) == []
    assert report._settling_is_material(res) is False
