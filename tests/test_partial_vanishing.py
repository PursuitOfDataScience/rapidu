"""When part of the re-stat's sample is unlinked, is the total still "settled"?

The round before this one blocked the extreme case: a re-stat whose *whole*
sample had been deleted reported ``checked=0 gone=8 drift=0`` and still said
"found no change in 0 files; the figure looks settled" -- a verdict from an
instrument that took no reading. It left the partial case alone as a judgement
call, on the grounds that the survivors genuinely had not moved and the deletion
was now disclosed.

The survivors had not moved. But "the figure looks settled" is a claim about the
*figure*, and the figure counts the deleted files. Built for real -- eight 64 KiB
files walked, some unlinked, then re-stat'ed -- the total overstates the tree by:

    1 of 8 gone   512.0 KiB read, 448.0 KiB there    1.14x
    4 of 8 gone   512.0 KiB read, 256.0 KiB there    2.00x
    7 of 8 gone   512.0 KiB read,  64.0 KiB there    8.00x

The bottom two are the same magnitude as the drift the whole check exists to
catch (5.58x up, 3.3x down on GPFS), announced as settled. The top one is a
64 KiB error that would be a rounding error on any real tree, so the fix is
gated on :func:`report._freed_since_walk_is_material` -- the module's existing
bound, *an error at least as large as what is left over*, which is what puts
one deletion in eight on the other side of it.

``SettleCheck.gone_bytes`` is what makes the ratio knowable at all: the walk's
own reading for the paths the re-stat could not find, which
:func:`test_the_vanished_blocks_are_the_exact_staleness` pins against a second
walk of the same tree.
"""

import io
import os

from rapidu import report, ui
from rapidu import walk as walkmod
from rapidu.walk import recheck_settling, walk

PLAIN = ui.resolve_style("never")


def _tree(root, nfiles=8, payload=65536):
    os.makedirs(root)
    for i in range(nfiles):
        with io.open(os.path.join(root, "f%03d" % i), "wb") as handle:
            handle.write(b"q" * payload)
    return root


def _walked_then_thinned(root, nfiles, ngone, payload=65536):
    """Walk a tree of fresh files, unlink some, re-stat. The real sequence.

    The gap is set to 60s afterwards rather than slept: ``MIN_CONCLUSIVE_GAP_S``
    is 5s and no test may sleep for it. The gap is an input to the judgement, not
    an observation about the tree, so setting it is not faking a measurement --
    and a believable gap is the *only* state in which the question arises, since
    a re-stat too brief to see drift is already reported as provisional.
    """
    res = walk(_tree(root, nfiles, payload), threads=2, depth=1)
    assert res.recent_files == nfiles and len(res.recent_sample) == nfiles
    for name in sorted(os.listdir(res.root))[:ngone]:
        os.unlink(os.path.join(res.root, name))
    chk = recheck_settling(res, 0.0)
    chk.gap = 60.0
    assert (chk.checked, chk.gone) == (nfiles - ngone, ngone)
    return res, chk


def _flat(lines):
    return " ".join(" ".join(lines).split())


# --------------------------------------------------------------------------
# the measurement the verdict is built on
# --------------------------------------------------------------------------


def test_the_vanished_blocks_are_the_exact_staleness(tmp_path):
    """``gone_bytes`` is the amount the total is already known to be high by.

    Not an estimate: it is the walk's own ``st_blocks`` reading for the paths the
    re-stat found unlinked, so it must equal the difference between the total the
    reader is looking at and the total a walk run now would report. Checked at
    every ratio, against a second real walk rather than against arithmetic.

    ``drift`` cannot carry this figure and should not: it leaves a vanished file
    out of *both* sides of its subtraction so that a deletion cannot masquerade
    as the tree shrinking. Correct for the drift -- and the reason the one change
    the re-stat positively observed was the one it reported as zero.
    """
    for nfiles, ngone in ((8, 1), (8, 4), (8, 7), (8, 8)):
        res, chk = _walked_then_thinned(str(tmp_path / ("t%d_%d" % (nfiles, ngone))), nfiles, ngone)
        after = walk(res.root, threads=2, depth=1)
        assert chk.gone_bytes == res.size - after.size, (nfiles, ngone, chk.gone_bytes)
        assert chk.drift == 0, "a deletion is not the tree shrinking"


# --------------------------------------------------------------------------
# fix 1 -- the verdict, in the -a view and in the document
# --------------------------------------------------------------------------


def test_a_partial_vanishing_past_the_bar_withdraws_the_settled_verdict(tmp_path):
    """7 of 8 gone: 512.0 KiB announced as settled for a tree holding 64.0 KiB.

    The survivor had not moved, so the check is entitled to say so -- but not to
    say it about the total. Both terminal forms now state the error instead, and
    the document agrees with them rather than publishing ``settled: true`` beside
    ``vanished_files: 7``.
    """
    res, chk = _walked_then_thinned(str(tmp_path / "seven"), 8, 7)
    assert chk.conclusive and not chk.moved, "the survivors genuinely did not move"
    assert chk.gone_bytes == 458752
    assert report._freed_since_walk_is_material(res, chk)

    compact = _flat(report.render_settle(res, chk, PLAIN))
    assert "looks settled" not in compact, compact
    assert "no change in the 1 file still there" in compact, compact
    assert "7 holding 448.0 KiB vanished in between" in compact, compact
    assert "provisional" in compact, compact

    doc = report.to_json(res, chk, None, None, None)["settling"]
    assert doc["settled"] is False
    assert doc["headline_provisional"] is True
    assert doc["vanished_allocated_bytes"] == 458752
    # The disclosure the previous round added is still there, and the drift
    # figure still reports the null it actually measured.
    assert doc["vanished_files"] == 7 and doc["rechecked"] == 1
    assert doc["moved"] is False and doc["drift_bytes"] == 0


def test_the_long_panel_withdraws_it_too(tmp_path):
    """The ``-a`` panel is the other rendering of the same check.

    80 files so the population is material enough for the panel form rather than
    the compact line; 70 unlinked, which is the same 8.01x.
    """
    res, chk = _walked_then_thinned(str(tmp_path / "panel"), 80, 70)
    assert report._settling_is_material(res), "otherwise this is the compact form"

    text = _flat(report.render_settle(res, chk, PLAIN))
    assert "looks settled" not in text, text
    assert "no change in the 10 files still there" in text, text
    assert "at least 4.4 MiB of the total above belongs to files that no longer exist" in text, text
    # The panel's own disclosure line is untouched and carries the count.
    assert "70 of them disappeared between the walk and the re-stat" in text, text
    for line in report.render_settle(res, chk, PLAIN):
        assert ui.visible_width(line) <= PLAIN.width, line


# --------------------------------------------------------------------------
# fix 2 -- the default view, which had no settling line to fall back on
# --------------------------------------------------------------------------


def test_the_default_view_says_the_total_counts_freed_blocks(tmp_path):
    """``rdu .`` was the *quietest* rendering of the worst case.

    ``render_settle`` lives in ``-a``; the default view's only channel for this
    is ``_provisional_note``, and that was gated on unlanded bytes -- of which a
    tree whose blocks landed immediately has none. So the run that lost seven of
    its eight recent files printed the headline, a table of eight entries of
    which one still existed, and nothing else.
    """
    res, chk = _walked_then_thinned(str(tmp_path / "quiet"), 8, 7)
    text = _flat(report.render_compact(res, chk, 10, False, PLAIN))
    assert "of which 7 holding 448.0 KiB vanished between the walk" in text, text
    assert "provisional" in text, text
    # `_hard_warnings` reports measured drift and there was none; this must not
    # claim the tree is still growing, which is a different finding.
    assert "still settling" not in text, text
    assert report.to_json(res, chk, None, None, None)["settling"]["headline_provisional"] is True, (
        "the document has to reach the terminal's conclusion"
    )


# --------------------------------------------------------------------------
# controls -- these must read identically before and after the fix
# --------------------------------------------------------------------------


def test_control_a_clean_restat_keeps_its_exact_sentence(tmp_path):
    """CONTROL. Nothing deleted, so nothing about this run may change.

    The believable null result is what ``--settle-wait`` is *for*; a fix aimed at
    deletions that made this quieter or more hedged would have taken the tool's
    one affirmative answer away.
    """
    res, chk = _walked_then_thinned(str(tmp_path / "clean"), 8, 0)
    assert chk.gone_bytes == 0
    assert not report._freed_since_walk_is_material(res, chk)

    text = _flat(report.render_settle(res, chk, PLAIN))
    assert "found no change in 8 files; the figure looks settled" in text, text
    assert "provisional" not in text and "disappeared" not in text, text
    assert _flat(report.render_compact(res, chk, 10, False, PLAIN)).count("provisional") == 0

    doc = report.to_json(res, chk, None, None, None)["settling"]
    assert doc["settled"] is True
    assert doc["headline_provisional"] is False
    assert doc["vanished_files"] == 0 and doc["vanished_allocated_bytes"] == 0


def test_control_b_one_deletion_in_eight_is_below_the_bar(tmp_path):
    """CONTROL, and the proportion this fix turns on.

    1.14x. On the tree this tool is pointed at -- eight files written into a
    multi-terabyte scratch directory, one rotated away -- it is 64 KiB against
    terabytes, and demoting the figure for it would put a caveat on every run of
    every tree that rotates anything. So the sentence the previous round shipped
    stands exactly as it was, disclosure included, and the default view stays
    silent.
    """
    res, chk = _walked_then_thinned(str(tmp_path / "one"), 8, 1)
    assert not report._freed_since_walk_is_material(res, chk)

    text = _flat(report.render_settle(res, chk, PLAIN))
    assert "found no change in 7 files" in text, text
    assert "(1 disappeared between the walk and the re-stat)" in text, text
    assert "the figure looks settled" in text, text
    assert "provisional" not in text, text

    doc = report.to_json(res, chk, None, None, None)["settling"]
    assert doc["settled"] is True and doc["headline_provisional"] is False
    assert doc["vanished_files"] == 1
    assert "provisional" not in _flat(report.render_compact(res, chk, 10, False, PLAIN))


def test_control_c_the_whole_sample_deleted_keeps_last_rounds_verdict(tmp_path):
    """CONTROL. The extreme case the previous round settled is not re-litigated.

    ``conclusive`` stays False, ``settled`` stays ``null`` rather than becoming a
    positive ``false``, the sentence stays the one it was given, and the advice to
    wait sixty seconds stays out of it. Only the default view changes, and only by
    saying something where it said nothing.
    """
    res, chk = _walked_then_thinned(str(tmp_path / "all"), 8, 8)
    assert chk.recheck_measured_nothing is True
    assert chk.conclusive is False and chk.moved is False

    text = _flat(report.render_settle(res, chk, PLAIN))
    assert "found 8 files already deleted and none left to measure" in text, text
    assert "so the figure is provisional" in text, text
    assert "looks settled" not in text and "--settle-wait 60" not in text, text
    # The verdict is now the *only* reason this line is provisional, so the
    # unallocated clause must not appear with a zero in it.
    assert "0 B unallocated" not in text, text

    doc = report.to_json(res, chk, None, None, None)["settling"]
    assert doc["settled"] is None
    assert doc["recheck_measured_nothing"] is True
    assert doc["vanished_files"] == 8 and doc["rechecked"] == 0


def test_the_bar_is_placed_where_the_ratios_say(tmp_path):
    """The threshold itself: what fires, what does not, and why.

    A bar invented for the occasion would be its own defect. This one is
    :func:`report._headline_is_provisional`'s -- *an error at least as large as
    what is left over* -- so the table is the evidence and this is the check that
    it was read off correctly rather than rounded to a convenient place.
    """
    seen = []
    for ngone in range(9):
        res, chk = _walked_then_thinned(str(tmp_path / ("r%d" % ngone)), 8, ngone)
        after = walk(res.root, threads=2, depth=1)
        factor = (float(res.size) / after.size) if after.size else None
        seen.append((ngone, factor, report._freed_since_walk_is_material(res, chk)))

    # The rule, read off each row's OWN measured ratio. A table of ratios is not
    # portable and this one was not: `(4, True)  # 2.00x` holds where deleting
    # four of eight files halves the tree exactly, and on the CI runner the three
    # directories cost a block each, so the same row measured 1.98x and correctly
    # did not fire. Pinning which row fires was pinning the host's block
    # accounting; the ratios are carried in `seen` so a failure still prints them.
    for ngone, factor, fired in seen:
        if factor is not None:
            assert fired == (factor >= 2.0), (ngone, factor)
    # The endpoints, which no per-directory overhead can move: an untouched tree
    # is not material, and losing seven or eight of eight files is.
    assert seen[0][2] is False, seen
    assert seen[7][2] is True and seen[8][2] is True, seen
    # And it is a threshold rather than a pattern -- once it fires it keeps
    # firing, which is the shape the table was really there to show.
    fired_at = [g for g, _, fired in seen if fired]
    assert fired_at, seen
    assert fired_at == list(range(fired_at[0], 9)), seen


def test_control_d_count_only_publishes_no_verdict_it_could_not_reach():
    """CONTROL. ``-c`` reads no blocks, so it cannot know any were freed.

    Same rule as every other figure in this section: ``None`` is not zero, and a
    verdict from a measurement that was not taken is the defect this whole line
    of work is about.
    """
    res = walkmod.WalkResult("/tmp/counted")
    res.count_only = True
    res.files = 8
    res.recent_files = 8
    chk = walkmod.SettleCheck()
    chk.ran = True
    chk.gap = 60.0
    chk.checked = 1
    chk.gone = 7
    chk.gone_bytes = 458752  # cannot actually happen under -c; asserted anyway
    assert report._freed_since_walk_is_material(res, chk) is False
    doc = report.to_json(res, chk, None, None, None)["settling"]
    assert doc["settled"] is None
    assert doc["headline_provisional"] is None
