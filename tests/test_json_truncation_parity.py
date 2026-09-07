"""``--json`` truncated the rankings to ``-n`` and said nothing about it.

The third drift of the class :mod:`test_json_terminal_parity` exists to catch --
"two renderings of one measurement drift apart" -- and it sat inside that
module's own control case. On a thirteen-inode tree at ``-n 3`` the terminal
prints::

      9 more - use -n 0 for all

while the document published three rows of ``top_by_size`` and carried no key a
consumer could find the nine in. ``dirs`` counts every directory walked, not the
root's children, so the number was not recoverable from anywhere else in the
document: five listed rows read identically whether the tree had five children
or fourteen.

Both sides state the rule that makes this a defect rather than a preference:

* the table, in ``report.py``: *"A truncated listing that does not tell you it is
  truncated, or how to expand it, is just missing data."*
* the ranking keys themselves, in ``to_json``: *"One result object must not have
  two honesty policies"* -- written by the round that stopped these same three
  keys publishing subtrees the table refused to show.

``walk.top_hidden`` says it now, and says it with the table's own policy rather
than a new one:

* ``count`` is stated at any depth -- and on an interrupted walk, where the table
  prints no truncation row at all (both ``say_hidden`` and the remainder row
  require ``not res.partial``). So this key says *more* than ``say_hidden`` does,
  not the same thing: a document is not a table, and a consumer cannot see the
  absence of a key it did not know to look for. Same asymmetry as ``count: 0``,
  where the table simply omits its row.
* the FIGURES belong to a ranking. ``top_by_size`` and ``top_by_inodes`` select
  different entries, so they leave different things out, and one ``bytes``/
  ``inodes`` pair beside both completed only one of them -- see
  ``TestEachRankingHasItsOwnRemainder``, which is the defect that gave this key
  its shape. Each carries its own remainder under ``by_size`` / ``by_inodes``,
  computed from the entries that ranking listed, exactly as the terminal computes
  its row from the rows it is printing.
* both are ``null`` under precisely the conditions that suppress the table's
  remainder row -- an interrupted walk has no known remainder, and at depth > 1
  the rows nest so a leftover would double-count;
* under ``-c`` no size was measured, so ``bytes`` is ``null`` and not ``0``,
  through :func:`report._unmeasured` like every sibling byte figure. There the two
  remainders coincide, because ``top_dirs`` coerces a ``size`` request to
  ``files`` when there is no size to rank on.

It describes the ``-n`` truncation of the two sibling rankings. ``top_by_density``
is ranked over an inode floor, so what is missing there is mostly *below the
floor* and ``-n`` would not bring it back -- which is why the table refuses to
print "use -n 0 for all" on a density listing, and why this key does not claim to
speak for it.

Adding a key is the one document change the ``schema`` counter deliberately does
not move: "Bumped when a key changes meaning or disappears, not when one is
added." ``TestControls`` pins that it did not move, and that the three rankings
still cut to ``-n`` exactly as before.
"""

import io
import os
import re

from rapidu import report, ui
from rapidu.walk import recheck_settling, walk

PLAIN = ui.resolve_style("never")

#: Fourteen children of the root: enough that a `-n` cut hides a knowable number.
_KIDS = 14


def _tree(root):
    """`_KIDS` sibling directories, each with one file, all under one root."""
    os.makedirs(root)
    for i in range(_KIDS):
        sub = os.path.join(root, "d%02d" % i)
        os.makedirs(sub)
        with io.open(os.path.join(sub, "f"), "wb") as handle:
            handle.write(b"x" * (1000 * (i + 1)))
    return root


def _walked(root, depth=1):
    res = walk(_tree(root), threads=2, depth=depth)
    assert res.complete and not res.partial, "the fixture is not a clean walk"
    return res


#: A tree the two rankings disagree about, which `_tree` cannot be: its children
#: have monotonic sizes and two inodes each, so `top_by_size` and `top_by_inodes`
#: list the same five entries and one shared remainder completes both by accident.
_BIG, _MANY, _FILES = 6, 6, 40


def _divergent_tree(root):
    """Big-and-few beside small-and-many, so the rankings are disjoint.

    `_BIG` directories of one 5 MiB file win a byte ranking with two inodes each;
    `_MANY` directories of ~`_FILES` one-byte files win an inode ranking with
    almost no bytes. At `-n 5` neither listing contains an entry from the other.
    """
    os.makedirs(root)
    for i in range(_BIG):
        sub = os.path.join(root, "big%02d" % i)
        os.makedirs(sub)
        with io.open(os.path.join(sub, "f"), "wb") as handle:
            handle.write(b"x" * (5 * 1024 * 1024))
    for i in range(_MANY):
        sub = os.path.join(root, "many%02d" % i)
        os.makedirs(sub)
        for j in range(_FILES + i):
            with io.open(os.path.join(sub, "f%03d" % j), "wb") as handle:
                handle.write(b"y")
    return root


def _walked_divergent(root):
    res = walk(_divergent_tree(root), threads=2, depth=1)
    assert res.complete and not res.partial, "the fixture is not a clean walk"
    doc = _doc(res, 5)
    listed = [{e["path"] for e in doc[k]} for k in ("top_by_size", "top_by_inodes")]
    assert not listed[0] & listed[1], ("the rankings agree, so the fixture is blind", listed)
    return res


def _doc(res, top):
    return report.to_json(res, None, None, None, None, top)["walk"]


def _tail(res, top, by_inodes=False):
    """The terminal's own truncation row, as text.

    `render_compact` needs a real `SettleCheck` -- passing `None` reaches an
    attribute access on it -- so the settle re-check runs here exactly as the
    parity module's control does.

    `by_inodes` is `rdu -i`: the same walk ranked the other way, which is the
    second row the document has to agree with.
    """
    lines = report.render_compact(res, recheck_settling(res), top, by_inodes, PLAIN)
    return next((ln for ln in lines if "more" in ln), None)


def _tail_count(res, top):
    line = _tail(res, top)
    assert line is not None, "the terminal printed no truncation row"
    found = re.search(r"([\d,]+) more", line)
    assert found, line
    return int(found.group(1).replace(",", ""))


class TestTheDocumentSaysWhatWasCutOff:
    def test_the_hidden_count_is_the_one_the_terminal_prints(self, tmp_path):
        res = _walked(str(tmp_path / "t"))
        doc = _doc(res, 5)
        assert len(doc["top_by_size"]) == 5
        assert doc["top_hidden"]["count"] == _KIDS - 5
        assert doc["top_hidden"]["count"] == _tail_count(res, 5)

    def test_the_remainder_figures_are_the_ones_the_terminal_shows(self, tmp_path):
        res = _walked(str(tmp_path / "t"))
        hidden = _doc(res, 5)["top_hidden"]["by_size"]
        assert hidden["bytes"] and hidden["inodes"]
        line = _tail(res, 5)
        # The row is formatted, so compare through the same formatter rather than
        # against a literal the filesystem gets to choose.
        assert report.human_bytes(hidden["bytes"]) in line, (line, hidden)
        assert str(hidden["inodes"]) in line, (line, hidden)

    def test_the_figures_complete_the_tree(self, tmp_path):
        res = _walked(str(tmp_path / "t"))
        doc = _doc(res, 5)
        listed_bytes = sum(e["bytes"] for e in doc["top_by_size"])
        listed_inodes = sum(e["inodes"] for e in doc["top_by_size"])
        assert listed_bytes + doc["top_hidden"]["by_size"]["bytes"] == doc["size_bytes"]
        assert listed_inodes + doc["top_hidden"]["by_size"]["inodes"] == doc["inodes"]

    def test_nesting_states_the_count_and_no_figures(self, tmp_path):
        """Depth > 1: the rows nest, so a remainder would double-count."""
        res = _walked(str(tmp_path / "t"), depth=2)
        doc = _doc(res, 3)
        hidden = doc["top_hidden"]
        assert hidden["count"] == _tail_count(res, 3)
        assert hidden["count"] > 0
        for ranking in ("by_size", "by_inodes"):
            figures = hidden[ranking]
            assert figures["bytes"] is None and figures["inodes"] is None, (ranking, hidden)

    def test_an_interrupted_walk_claims_no_remainder(self, tmp_path):
        res = _walked(str(tmp_path / "t"))
        res.partial = True  # what an interrupt leaves behind
        hidden = _doc(res, 5)["top_hidden"]
        for ranking in ("by_size", "by_inodes"):
            figures = hidden[ranking]
            assert figures["bytes"] is None and figures["inodes"] is None, (ranking, hidden)

    def test_the_count_is_stated_where_the_table_prints_no_row_at_all(self, tmp_path):
        """An interrupt suppresses the terminal's row entirely -- not this key.

        The docstrings used to say the count is stated "exactly as ``say_hidden``
        does". It is not: `say_hidden` and the remainder row both require
        ``not res.partial``, so after an interrupt the table says nothing about
        truncation while the document still answers.
        """
        res = _walked(str(tmp_path / "t"))
        res.partial = True
        assert _tail(res, 5) is None, "the table printed a remainder row on a partial walk"
        assert _tail(res, 5, by_inodes=True) is None
        assert _doc(res, 5)["top_hidden"]["count"] > 0

    def test_nothing_hidden_is_stated_as_zero(self, tmp_path):
        # A document is not a table: where the table suppresses its row, the key
        # still answers, and the answer is 0 rather than a missing key.
        res = _walked(str(tmp_path / "t"))
        hidden = _doc(res, 0)["top_hidden"]
        assert hidden["count"] == 0
        for ranking in ("by_size", "by_inodes"):
            figures = hidden[ranking]
            assert figures["bytes"] is None and figures["inodes"] is None, (ranking, hidden)

    def test_it_does_not_claim_to_speak_for_the_density_ranking(self, tmp_path):
        # Its omissions are the inode floor, not `-n`; the table says so by
        # refusing the "use -n 0" instruction on a density listing.
        res = _walked(str(tmp_path / "t"))
        doc = _doc(res, 5)
        assert "top_by_density" in doc
        assert set(doc["top_hidden"]) == {"count", "by_size", "by_inodes"}

    def test_a_count_only_walk_publishes_inodes_and_no_bytes(self, tmp_path):
        """``-c`` never stats, so a byte figure would be a fabrication."""
        root = _tree(str(tmp_path / "t"))
        res = walk(root, threads=2, depth=1, count_only=True)
        hidden = _doc(res, 5)["top_hidden"]
        assert hidden["count"] == _KIDS - 5
        for ranking in ("by_size", "by_inodes"):
            figures = hidden[ranking]
            assert figures["inodes"] > 0, (ranking, hidden)
            assert figures["bytes"] is None, (ranking, hidden)
        # `top_dirs` coerces a `size` request to `files` when there are no sizes,
        # so the two rankings are the same listing and their remainders agree.
        assert hidden["by_size"] == hidden["by_inodes"], hidden


class TestEachRankingHasItsOwnRemainder:
    """The document publishes two rankings; one remainder cannot complete both.

    The defect this reshape fixes. `top_by_size` and `top_by_inodes` select
    *different* entries, and the key stated one `bytes`/`inodes` pair -- computed
    from the size ranking -- beside both. Measured on `_divergent_tree` at
    ``--json -n 5``: ``inodes: 264`` against a `top_by_inodes` whose rows sum to
    220 on a 274-inode tree, so a consumer completing the inode ranking landed on
    484. The terminal was right on both, because its remainder row is computed
    from the ranking it is printing.
    """

    def test_either_ranking_completes_exactly(self, tmp_path):
        res = _walked_divergent(str(tmp_path / "t"))
        doc = _doc(res, 5)
        hidden = doc["top_hidden"]
        for ranking, figures in (
            ("top_by_size", hidden["by_size"]),
            ("top_by_inodes", hidden["by_inodes"]),
        ):
            rows = doc[ranking]
            assert len(rows) == 5, (ranking, rows)
            listed_bytes = sum(e["bytes"] for e in rows)
            listed_inodes = sum(e["inodes"] for e in rows)
            # Inodes first: it is the axis the shared remainder was most wrong on
            # (220 listed + 264 hidden = 484 on a 274-inode tree).
            assert listed_inodes + figures["inodes"] == doc["inodes"], (ranking, figures)
            assert listed_bytes + figures["bytes"] == doc["size_bytes"], (ranking, figures)

    def test_the_two_remainders_are_different_figures(self, tmp_path):
        # The premise: with the rankings disjoint, one pair of figures is wrong
        # for one of them. `_walked_divergent` asserts the disjointness.
        res = _walked_divergent(str(tmp_path / "t"))
        hidden = _doc(res, 5)["top_hidden"]
        assert hidden["by_size"]["inodes"] != hidden["by_inodes"]["inodes"], hidden
        assert hidden["by_size"]["bytes"] != hidden["by_inodes"]["bytes"], hidden

    def test_each_remainder_is_the_row_its_own_listing_prints(self, tmp_path):
        # `rdu -n 5` and `rdu -i -n 5` print different remainder rows for the same
        # `-n`; each side of the key has to match its own one.
        res = _walked_divergent(str(tmp_path / "t"))
        hidden = _doc(res, 5)["top_hidden"]
        for ranking, by_inodes in (("by_size", False), ("by_inodes", True)):
            figures, line = hidden[ranking], _tail(res, 5, by_inodes=by_inodes)
            assert line is not None, ranking
            assert report.human_bytes(figures["bytes"]) in line, (ranking, line, figures)
            assert str(figures["inodes"]) in line, (ranking, line, figures)

    def test_the_count_is_shared_because_both_rankings_cut_the_same_set(self, tmp_path):
        # Which is why `count` stayed one figure: the two listings hide different
        # entries but the same NUMBER of them, and the terminal prints one count
        # either way.
        res = _walked_divergent(str(tmp_path / "t"))
        hidden = _doc(res, 5)["top_hidden"]
        assert hidden["count"] == (_BIG + _MANY) - 5
        assert hidden["count"] == _tail_count(res, 5)
        line = _tail(res, 5, by_inodes=True)
        assert "%d more" % hidden["count"] in line, line


class TestControls:
    """Behaviour that must not change. Each passes in BOTH states."""

    def test_the_rankings_are_still_cut_to_n(self, tmp_path):
        res = _walked(str(tmp_path / "t"))
        for top, expected in ((5, 5), (2, 2), (0, _KIDS)):
            doc = _doc(res, top)
            assert len(doc["top_by_size"]) == expected, (top, doc["top_by_size"])
            assert len(doc["top_by_inodes"]) == expected, top

    def test_no_limit_still_publishes_every_entry(self, tmp_path):
        # The `-n 0` reading `_limit` exists to protect: 0 means every entry, and
        # with nothing hidden the terminal prints no truncation row either.
        res = _walked(str(tmp_path / "t"))
        assert len(_doc(res, 0)["top_by_size"]) == _KIDS
        assert _tail(res, 0) is None, "the terminal printed a truncation row"

    def test_the_schema_counter_did_not_move(self, tmp_path):
        # "Bumped when a key changes meaning or disappears, not when one is added."
        res = _walked(str(tmp_path / "t"))
        assert report.to_json(res, None, None, None, None, 5)["schema"] == 5

    def test_the_terminal_truncation_row_is_unchanged(self, tmp_path):
        res = _walked(str(tmp_path / "t"))
        line = _tail(res, 5)
        assert "more" in line and "use -n 0 for all" in line, line
        assert line == _tail(res, 5), "the row is not stable across renders"

    def test_the_other_walk_figures_are_untouched(self, tmp_path):
        res = _walked(str(tmp_path / "t"))
        doc = _doc(res, 5)
        assert doc["inodes"] == res.inodes
        assert doc["size_bytes"] == res.size
        assert doc["files"] == res.files and doc["dirs"] == res.dirs
