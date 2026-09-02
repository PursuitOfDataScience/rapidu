"""The ``-a`` panel's own withdrawal of the ``--settle-wait`` advice.

``render_settle`` draws two forms of one check, switching on
:func:`report._settling_is_material`: a compact line for a handful of files, and
the long ``SETTLING`` panel once the population could move the total. The
whole-sample-deleted case -- a re-stat that found every file it meant to measure
already unlinked -- reaches both, and for one round it was fixed in only one of
them. The compact line said what it observed; the panel, for the same
``SettleCheck``, fell through to *"figure is PROVISIONAL -- use --settle-wait 60
to measure the drift"*, which is wrong here twice over and for two separate
reasons: waiting longer deletes more of a sample that is being deleted, and
``gap`` is on the object, so the sixty seconds offered can be *less* than the
wait the reader has already sat through.

``test_control_c_the_whole_sample_deleted_keeps_last_rounds_verdict`` in
``test_partial_vanishing.py`` looks like it covers this, and asserts the right
thing -- but it builds 8 files, which is below the materiality bar, so it renders
the *compact* branch. Disabling the panel branch outright left the whole suite
green at 1249 passed, which is what this file is for: the population has to be
material AND the whole sample gone, and no other test builds that pair.
"""

import io
import os

from rapidu import report, ui
from rapidu.walk import recheck_settling, walk

PLAIN = ui.resolve_style("never")


def _tree(root, nfiles, payload=65536):
    os.makedirs(root)
    for i in range(nfiles):
        with io.open(os.path.join(root, "f%03d" % i), "wb") as handle:
            handle.write(b"q" * payload)
    return root


def _walked_then_thinned(root, nfiles, ngone, payload=65536):
    """The real sequence: walk fresh files, unlink some, re-stat.

    The gap is set afterwards rather than slept, for the reason
    ``test_partial_vanishing.py`` gives: ``MIN_CONCLUSIVE_GAP_S`` is 5s, no test
    may sleep for it, and the gap is an input to the judgement rather than an
    observation about the tree.
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


def test_the_panel_withdraws_the_settle_wait_advice_it_had_already_performed(tmp_path):
    """80 files, all 80 unlinked: material population, nothing left to measure.

    The pair that reaches the panel branch. What the reader must not be told is
    to wait sixty seconds -- they waited sixty seconds, and the sample they would
    be waiting on no longer exists.
    """
    res, chk = _walked_then_thinned(str(tmp_path / "panel_all"), 80, 80)
    assert report._settling_is_material(res), "otherwise this renders the compact line"
    assert chk.recheck_measured_nothing is True
    assert chk.conclusive is False and chk.moved is False

    text = _flat(report.render_settle(res, chk, PLAIN))
    assert "found 80 files already deleted and none left to measure" in text, text
    assert "so the figure is provisional" in text, text
    # The defect, in the words it used: a remedy that is both wrong and already spent.
    assert "PROVISIONAL" not in text, text
    assert "--settle-wait 60" not in text, text
    assert "measure the drift" not in text, text
    # No verdict from a blind instrument, either.
    assert "looks settled" not in text, text
    # The clause names the count as its own subject, so the separate disclosure
    # line must not repeat it.
    assert "disappeared between the walk and the re-stat" not in text, text
    for line in report.render_settle(res, chk, PLAIN):
        assert ui.visible_width(line) <= PLAIN.width, line


def test_control_the_panel_keeps_the_advice_where_it_is_the_right_advice(tmp_path):
    """CONTROL, and it must pass with the fix present or absent.

    A re-stat taken immediately over a material population that lost nothing:
    the instrument really did read too early, waiting really is the remedy, and
    ``--settle-wait 60`` is exactly what the reader should be told. This is the
    line the fix must leave alone, and the reason the fix is a new branch rather
    than an edit to the ``else``.
    """
    res = walk(_tree(str(tmp_path / "fresh"), 80), threads=2, depth=1)
    chk = recheck_settling(res, 0.0)
    assert report._settling_is_material(res)
    assert chk.recheck_measured_nothing is False and chk.gone == 0
    assert chk.conclusive is False, "an immediate re-stat cannot be conclusive"

    text = _flat(report.render_settle(res, chk, PLAIN))
    assert "figure is PROVISIONAL -- use --settle-wait 60 to measure the drift" in text, text
    assert "already deleted" not in text, text


def test_control_a_material_partial_vanishing_is_not_this_branch(tmp_path):
    """CONTROL, passing in both states. 70 of 80 gone is material and partial.

    It has survivors, so the instrument took a reading and this branch must not
    claim otherwise -- the panel keeps the sentence the earlier round gave it.
    Pins the boundary: what selects the new branch is *nothing measured*, not
    *something deleted*.
    """
    res, chk = _walked_then_thinned(str(tmp_path / "partial"), 80, 70)
    assert report._settling_is_material(res)
    assert chk.recheck_measured_nothing is False

    text = _flat(report.render_settle(res, chk, PLAIN))
    assert "no change in the 10 files still there" in text, text
    assert "70 of them disappeared between the walk and the re-stat" in text, text
    assert "already deleted and none left to measure" not in text, text
