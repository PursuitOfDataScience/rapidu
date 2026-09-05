"""Three strings were built identically in `reconcile.py` and `report.py`.

Found by diffing the two modules' string literals, which is a comparison nothing
here made: `test_audit_round_six.py::test_no_message_pairs_a_count_with_a_fixed_verb`
sweeps for a placeholder beside an agreement-sensitive verb, and slurmpast's
equivalent compares its two FRONT ENDS -- neither looks for one clause spelled
twice across an analysis module and its renderer.

    "{} changed without being written".format(plural(res.touched_files, "inode"))
        reconcile.py:190   report.py:2447        <- byte-identical, plural() and all
    "UNEXPLAINED GAP"
        reconcile.py:974   report.py:3514
    "not compared"
        reconcile.py:950   report.py:3465

The clause is load bearing: `st_ctime` moves for a permission change, an ownership
change, a rename or a hard link, none of which touch a block, and calling those a
write "stated something false about the tree, in the section whose whole job is to
say how much the headline number can be trusted". Two copies means a reword lands
in one place and the two surfaces disagree about what the reader is being told.

**What is deliberately NOT shared, checked before touching it.** The sentences
AROUND the clause differ on purpose: `report._settle_subject` yields a noun phrase
("N files written") because its eight call sites supply the tense, while
`reconcile._changed_phrase` yields a full clause ("N files were written") -- and
that verb is pinned by `test_portability_midway2.py::
test_subject_verb_agreement_on_one_file` as "the agreement rule this package states
once". `report` also renders `CLOSES` and `SUBTREE` with its own richer wording (a
headline plus a detail line) rather than `verdict_line`'s. Only the three identical
strings moved.
"""

import os

from rapidu import quota as Q
from rapidu import reconcile as rc
from rapidu import report, ui
from rapidu import walk as walkmod
from rapidu.fmt import inode_change_clause


def _plain():
    return ui.resolve_style("never")


def _no_deleted():
    from rapidu.deleted import DeletedScan

    return DeletedScan()


def _tree_with(tmp_path, written=0, touched=0):
    """``touched`` files get an ancient mtime and a fresh ctime -- what a
    ``chmod -R`` leaves behind on a tree nobody has written to."""
    import time

    for n in range(written):
        (tmp_path / "w{}.bin".format(n)).write_bytes(b"x" * 4096)
    for n in range(touched):
        p = tmp_path / "t{}.bin".format(n)
        p.write_bytes(b"x" * 4096)
        old = time.time() - 400 * 86400
        os.utime(str(p), (old, old))
    return walkmod.walk(str(tmp_path), threads=2, depth=1)


def _reconciled(res, tmp_path):
    snap = Q.QuotaSnapshot("t")
    snap.available = True
    snap.taken_at = snap.read_at
    snap.rows = [Q.QuotaRow("fs", "blocks", "user", 10**9, None, None, "", str(tmp_path))]
    return rc.reconcile(res, walkmod.recheck_settling(res, 0.0), snap, _no_deleted(), "blocks")


class TestTheClauseHasOneHome:
    def test_it_says_what_it_used_to_say(self, tmp_path):
        """Vacuity guard plus the wording, so a helper that returned "" would not
        make every containment check below trivially true."""
        assert inode_change_clause(4) == "4 inodes changed without being written"
        assert inode_change_clause(1) == "1 inode changed without being written"

    def test_both_modules_emit_the_shared_clause(self, tmp_path):
        """That the clause REACHES both surfaces -- the half a source check cannot do.

        The division of labour is deliberate, and measured rather than assumed:

        * this test catches a call site that stops emitting the clause (verified
          by removing it from `report._settle_subject`: 1 failed);
        * `test_neither_module_spells_it_itself` catches a second copy, which this
          one cannot -- a re-spelling that APPENDS ("... changed without being
          written **to**") leaves the original as a substring, and a span regex
          does not help either because the extra word is space-separated. Both
          neuters were run; only the source pin reddened.

        So the source pin is the load-bearing one for the single-sourcing, and
        this is the one that proves the sentence is on screen at all.
        """
        import re

        res = _tree_with(tmp_path, touched=4)
        assert res.recent_files == 0 and res.touched_files == 4
        clause = inode_change_clause(res.touched_files)
        span = re.compile(r"\d+ inodes? changed without being written\S*")

        text = "\n".join(report.render_settle(res, walkmod.recheck_settling(res, 0.0), _plain()))
        rec = _reconciled(res, tmp_path)
        joined = " ".join(rec.blockers)

        from_report = span.findall(text)
        from_reconcile = span.findall(joined)
        assert from_report, text
        assert from_reconcile, joined
        assert set(from_report) == {clause}, from_report
        assert set(from_reconcile) == {clause}, from_reconcile

    def test_neither_module_spells_it_itself(self):
        """The source check that would have caught the copy. `fmt` is the only
        place the words may appear."""
        from pathlib import Path

        import rapidu.fmt as fmt_mod

        needle = "changed without being written"
        for module in (rc, report):
            source = Path(module.__file__).read_text()
            assert needle not in source, module.__name__
            assert "inode_change_clause" in source, module.__name__
        assert needle in Path(fmt_mod.__file__).read_text()


class TestTheVerdictLabelsHaveOneHome:
    def test_the_gap_headline_is_declared_once(self):
        from pathlib import Path

        assert rc.GAP_HEADLINE == "UNEXPLAINED GAP"
        source = Path(report.__file__).read_text()
        assert "UNEXPLAINED GAP" not in source
        assert "rc.GAP_HEADLINE" in source

    def test_the_not_compared_label_is_declared_once(self):
        from pathlib import Path

        assert rc.NOT_COMPARED_LABEL == "not compared"
        source = Path(report.__file__).read_text()
        assert '"not compared"' not in source
        assert "rc.NOT_COMPARED_LABEL" in source

    def test_verdict_line_still_uses_them(self):
        """`verdict_line` is the other consumer, and it is reached in production
        only for `SUBTREE` -- so its `NOT_COMPARED` and `GAP` branches are exactly
        the ones a source-only check would let drift."""
        rec = rc.Reconciliation("blocks")
        assert rc.verdict_line(rec) == rc.NOT_COMPARED_LABEL
        rec.verdict = rc.GAP
        assert rc.verdict_line(rec) == rc.GAP_HEADLINE


class TestControls:
    def test_control_the_agreement_verb_is_untouched(self, tmp_path):
        """CONTROL. `reconcile`'s sentence keeps its verb -- pinned elsewhere as
        "the agreement rule this package states once" -- and sharing the clause
        must not have flattened it into `report`'s noun phrase."""
        res = _tree_with(tmp_path, written=1)
        assert res.recent_files == 1
        rec = _reconciled(res, tmp_path)
        joined = " ".join(rec.blockers)
        assert "1 file was written" in joined, joined
        assert "1 file were" not in joined

    def test_control_the_report_phrase_keeps_its_noun_form(self, tmp_path):
        """CONTROL, the other half: `report`'s eight call sites read
        "N files written in the last …", so it must NOT gain a verb."""
        res = _tree_with(tmp_path, written=3)
        text = "\n".join(report.render_settle(res, walkmod.recheck_settling(res, 0.0), _plain()))
        assert "3 files written" in text, text
        assert "3 files were written" not in text, text

    def test_control_an_inode_change_is_still_not_called_a_write(self, tmp_path):
        """CONTROL. The distinction the clause exists for. Holds in both states."""
        res = _tree_with(tmp_path, touched=4)
        text = "\n".join(report.render_settle(res, walkmod.recheck_settling(res, 0.0), _plain()))
        assert "4 files written" not in text, text

    def test_control_the_richer_verdict_wordings_are_still_reports_own(self):
        """CONTROL. `CLOSES` and `SUBTREE` are rendered by `report` with a headline
        plus a detail line, deliberately not `verdict_line`'s one-liner -- so those
        two must NOT have been folded in."""
        from pathlib import Path

        source = Path(report.__file__).read_text()
        assert '"reconciles"' in source
        assert '"INCONCLUSIVE"' in source
