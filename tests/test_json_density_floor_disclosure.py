"""``--json`` published a two-row density ranking of a fourteen-entry tree.

The fourth drift of the class :mod:`test_json_terminal_parity` exists to catch,
and the one :mod:`test_json_truncation_parity` deliberately left open. That module
gave the document ``walk.top_hidden`` for the ``-n`` truncation of the two sibling
rankings and said, in as many words, that it *does not* speak for the density one::

    ``top_by_density`` is ranked over an inode floor, so what is missing there is
    mostly below the floor and ``-n`` would not bring it back -- the same reason
    the table refuses to print "use -n 0 for all" on a density listing.

Right, and it left nothing that did. Measured on this repository's sibling tree,
``rdu /home/youzhi/nodetop -d 1 -n 0 --json``::

    top_by_size      14 entries
    top_by_inodes    14 entries
    top_by_density    2 entries
    top_hidden       {"count": 0, ...}

``count: 0`` is *correct* -- ``-n 0`` cut nothing -- so every truncation figure in
the document read "complete" beside a ranking missing twelve of its fourteen
entries. The terminal, on the same walk, printed the reason::

    12 of 14 entries hold fewer than 100 inodes and cannot be ranked by density

**It was not derivable either.** ``density_floor`` is ``max(100, inodes // 100)``,
a rule the document publishes nowhere -- the same reason ``rows[].limit`` exists
rather than leaving a consumer to reimplement soft-or-hard -- and ``top_dirs``
drops a zero-byte subtree as well, which no inode figure predicts. At any ``-n``
but 0 the per-row ``inodes`` a consumer would have to count are themselves cut.

``walk.top_density_hidden`` says it now, with the table's policy and not a new
one -- **two reasons, two figures**, which is what :func:`report._density_floor_note`
was fixed to state after "3 of 4 entries hold fewer than 100 files" was printed
about a tree where zero of the four were:

* ``below_floor`` was never rankable, and ``-n`` will not bring it back;
* ``truncated_by_limit`` cleared the floor and was cut by the limit, so ``-n 0``
  will. Keeping them apart is the document's version of the table's refusal to
  print "use -n 0 for all" under a density listing;
* ``inode_floor`` is the threshold the sentence quotes, so a consumer can say
  *which* entries went rather than only how many;
* ``null`` under ``-c``, like ``top_by_density`` itself: no size was measured, so
  ``top_dirs`` coerces the request to ``files`` and there is no density ranking to
  be short. Raw, it would have published ``below_floor: 0`` -- a filter that never
  ran, reported as a filter that dropped nothing.

Both figures come from :func:`report._density_floor_counts`, which the table now
reads too, so the document cannot disagree with the sentence printed beside it.
``TestParityWithTheSentence`` asserts that against the rendered text, including on
an interrupted walk, where the two would otherwise be free to filter differently.

Adding a key is the one document change ``schema`` deliberately does not move:
"Bumped when a key changes meaning or disappears, not when one is added."
``TestControls`` pins that it did not, that ``top_hidden`` still speaks for exactly
the two rankings it always did, and that the table's own two sentences are
unchanged by the extraction.
"""

import io
import os
import re

from rapidu import report, ui
from rapidu.walk import walk

PLAIN = ui.resolve_style("never", True)

#: Files per entry, and bytes per file. 150 * 400 B is comfortably above the
#: floor on inodes and non-zero on `st_blocks`, which are `top_dirs`' two
#: conditions for entering a density ranking.
_FILES, _BYTES = 150, 400


def _entry(root, name, files=_FILES):
    sub = os.path.join(root, name)
    os.makedirs(sub)
    for i in range(files):
        with io.open(os.path.join(sub, "f%03d" % i), "wb") as handle:
            handle.write(b"x" * _BYTES)
    return sub


def _tree(root, above, below):
    """``above`` entries that clear the density floor and ``below`` that cannot.

    A "below" entry holds one file, which is the ordinary shape the floor exists
    for: files-per-GiB is won on the denominator, so a directory of three files
    would otherwise top the table.
    """
    os.makedirs(root)
    for i in range(above):
        _entry(root, "dense%02d" % i)
    for i in range(below):
        _entry(root, "sparse%02d" % i, files=1)
    return root


def _walked(root, above, below, depth=1):
    res = walk(_tree(root, above, below), threads=2, depth=depth)
    assert res.complete and not res.partial, "the fixture is not a clean walk"
    # The fixture's whole premise, asserted rather than assumed: the floor really
    # does split these entries the way the names say.
    qualifying = len(res.top_dirs(10**9, "density"))
    assert qualifying == above, ("the floor did not split the fixture", qualifying, above)
    return res


def _doc(res, top):
    return report.to_json(res, None, None, None, None, top)["walk"]


def _note(res, top):
    """The table's density note, unwrapped back into one line.

    Rendered through `_table` at the same `sort` the document ranks by, because
    the sentence and the key are the two surfaces this module compares.
    """
    return " ".join("\n".join(report._table(res, top, False, PLAIN, "density")).split())


def _figures(text):
    """``(below_floor, entries, inode_floor)`` as the sentence states them."""
    found = re.search(r"([\d,]+) of ([\d,]+) entr\w+ holds? fewer than ([\d,]+) inodes", text)
    if not found:
        return None
    return tuple(int(g.replace(",", "")) for g in found.groups())


def _truncated(text):
    """``N`` from "N more clears the floor but was cut by -n", or ``None``."""
    found = re.search(r"([\d,]+) more clears? the floor", text)
    return int(found.group(1).replace(",", "")) if found else None


class TestTheDocumentSaysWhyTheDensityRankingIsShort:
    def test_the_floor_is_disclosed_where_n_cut_nothing(self, tmp_path):
        """The measured defect: a short ranking with every ``-n`` figure at zero.

        `-n 0` publishes every entry of the other two rankings and hides nothing,
        so `top_hidden` is honestly empty -- and was the document's only account
        of a listing that is missing most of the tree.
        """
        res = _walked(str(tmp_path / "t"), above=2, below=12)
        doc = _doc(res, 0)
        assert len(doc["top_by_size"]) == 14 and len(doc["top_by_inodes"]) == 14
        assert len(doc["top_by_density"]) == 2
        assert doc["top_hidden"]["count"] == 0
        hidden = doc["top_density_hidden"]
        assert hidden["below_floor"] == 12, hidden
        assert hidden["truncated_by_limit"] == 0, hidden

    def test_the_key_names_the_threshold_it_measured_against(self, tmp_path):
        res = _walked(str(tmp_path / "t"), above=2, below=12)
        hidden = _doc(res, 0)["top_density_hidden"]
        assert hidden["inode_floor"] == res.density_floor
        # Not the constant: the floor is `max(100, inodes // 100)` and a consumer
        # cannot reconstruct the rule from a document that never states it.
        assert hidden["inode_floor"] == max(100, res.inodes // 100)

    def test_the_floor_and_the_limit_are_never_one_figure(self, tmp_path):
        """Four entries, all above the floor, three cut by ``-n 1``.

        The false statement `_density_floor_note` was fixed for, in the document's
        units: charging the floor for what the limit cut would say twelve entries
        "cannot be ranked" when raising `-n` shows all four.
        """
        res = _walked(str(tmp_path / "t"), above=4, below=0)
        hidden = _doc(res, 1)["top_density_hidden"]
        assert hidden["below_floor"] == 0, hidden
        assert hidden["truncated_by_limit"] == 3, hidden

    def test_both_reasons_are_stated_when_both_apply(self, tmp_path):
        res = _walked(str(tmp_path / "t"), above=3, below=2)
        hidden = _doc(res, 1)["top_density_hidden"]
        assert hidden["below_floor"] == 2, hidden
        assert hidden["truncated_by_limit"] == 2, hidden

    def test_the_two_reasons_account_for_every_missing_entry(self, tmp_path):
        """Listed + below the floor + cut by ``-n`` is the entry count exactly.

        The completeness property `top_hidden`'s remainders have in bytes, in the
        only unit a density ranking has: a consumer can close the ranking.
        """
        res = _walked(str(tmp_path / "t"), above=3, below=2)
        for top in (0, 1, 2, 3, 5):
            doc = _doc(res, top)
            hidden = doc["top_density_hidden"]
            listed = len(doc["top_by_density"])
            entries = doc["top_hidden"]["count"] + len(doc["top_by_size"])
            assert listed + hidden["below_floor"] + hidden["truncated_by_limit"] == entries, (
                top,
                hidden,
                listed,
            )

    def test_an_empty_density_ranking_is_explained_rather_than_bare(self, tmp_path):
        """The ordinary tree: the floor takes everything and the table says so.

        `_density_floor_note`'s own subject -- "on an ordinary tree it removes
        *everything*, and ``rdu --sort density`` printed a headline, no table, and
        exited 0". The document printed `[]`, which reads the same way.
        """
        res = _walked(str(tmp_path / "t"), above=0, below=4)
        doc = _doc(res, 0)
        assert doc["top_by_density"] == []
        hidden = doc["top_density_hidden"]
        assert hidden["below_floor"] == 4, hidden
        assert hidden["truncated_by_limit"] == 0, hidden

    def test_nothing_missing_is_stated_as_zero_rather_than_omitted(self, tmp_path):
        # A document is not a table: where the table prints no note at all, the
        # key still answers, and "the ranking is complete" is a claim worth being
        # able to read. Same asymmetry `top_hidden.count` carries.
        res = _walked(str(tmp_path / "t"), above=4, below=0)
        assert _figures(_note(res, 0)) is None and _truncated(_note(res, 0)) is None
        hidden = _doc(res, 0)["top_density_hidden"]
        assert hidden["below_floor"] == 0 and hidden["truncated_by_limit"] == 0, hidden

    def test_a_count_only_walk_publishes_null(self, tmp_path):
        """``-c`` ranks no densities, so a figure here would describe a filter that
        never ran -- `top_dirs` coerces the request to ``files``."""
        root = _tree(str(tmp_path / "t"), above=2, below=12)
        res = walk(root, threads=2, depth=1, count_only=True)
        doc = _doc(res, 5)
        assert doc["top_by_density"] is None
        assert doc["top_density_hidden"] is None, doc["top_density_hidden"]
        # ...and the sibling key still answers there, so this is the density
        # ranking's absence and not a `-c` document that says nothing.
        assert doc["top_hidden"]["count"] == 9


class TestParityWithTheSentence:
    """One filter, one count. The table has disclosed it in prose since round
    five; the document must not compute it a second way."""

    def test_the_figures_are_the_ones_the_table_prints(self, tmp_path):
        res = _walked(str(tmp_path / "t"), above=3, below=2)
        for top in (1, 2, 3):
            hidden, text = _doc(res, top)["top_density_hidden"], _note(res, top)
            below, entries, floor = _figures(text)
            assert (below, floor) == (hidden["below_floor"], hidden["inode_floor"]), (top, text)
            assert entries == hidden["below_floor"] + len(res.top_dirs(10**9, "density"))
            assert _truncated(text) == hidden["truncated_by_limit"] or (
                hidden["truncated_by_limit"] == 0 and _truncated(text) is None
            ), (top, text, hidden)

    def test_an_interrupted_walk_counts_the_same_entries_on_both_surfaces(self, tmp_path):
        """`finished_only=res.partial` is the table's rule, and half of a shared
        counter's value: a half-counted subtree is not rankable, and if only one
        surface dropped it the two would state different floors of the same tree.
        """
        res = _walked(str(tmp_path / "t"), above=4, below=0)
        res.partial = True  # what an interrupt leaves behind
        res.finished_tops = {"dense00", "dense01"}
        hidden, text = _doc(res, 0)["top_density_hidden"], _note(res, 0)
        below, _entries, floor = _figures(text)
        assert (below, floor) == (hidden["below_floor"], hidden["inode_floor"]), (text, hidden)
        # The premise: two of the four subtrees were still being walked, so a
        # surface that ranked them anyway would report a different figure.
        assert hidden["below_floor"] == 2, hidden
        assert len(_doc(res, 0)["top_by_density"]) == 2

    def test_the_key_is_stated_where_the_table_prints_no_note(self, tmp_path):
        # The table's note is suppressed by having nothing to say; the key is not
        # suppressible, which is the point of publishing it.
        res = _walked(str(tmp_path / "t"), above=4, below=0)
        assert _figures(_note(res, 0)) is None
        assert "top_density_hidden" in _doc(res, 0)


class TestControls:
    """Behaviour that must not change. Each passes in BOTH states."""

    def test_control_top_hidden_speaks_for_exactly_two_rankings(self, tmp_path):
        # The new key is a sibling, not a member: `top_hidden.count` is the shared
        # `-n` figure of the two rankings that cut the same sibling set, and a
        # density entry below the floor was never in that set.
        res = _walked(str(tmp_path / "t"), above=2, below=12)
        doc = _doc(res, 5)
        assert set(doc["top_hidden"]) == {"count", "by_size", "by_inodes"}
        assert doc["top_hidden"]["count"] == 9

    def test_control_the_schema_counter_did_not_move(self, tmp_path):
        # "Bumped when a key changes meaning or disappears, not when one is added."
        res = _walked(str(tmp_path / "t"), above=2, below=12)
        assert report.to_json(res, None, None, None, None, 5)["schema"] == 5

    def test_control_the_table_still_prints_both_sentences(self, tmp_path):
        """The extraction of `_density_floor_counts` must not have touched the
        text. Asserted on the wording, which is the thing round five settled."""
        res = _walked(str(tmp_path / "t"), above=3, below=2)
        text = _note(res, 1)
        assert "2 of 5 entries hold fewer than" in text, text
        assert "2 more clear the floor but were cut by -n" in text, text
        assert "use -n 0 for all" in text, text

    def test_control_the_table_still_singularises_one_truncated_row(self, tmp_path):
        res = _walked(str(tmp_path / "t"), above=4, below=0)
        assert "1 more clears the floor but was cut by -n" in _note(res, 3)

    def test_control_the_floor_is_still_not_blamed_for_what_n_cut(self, tmp_path):
        # Round five's own assertion, kept here because this module reads the same
        # two counts the note does.
        res = _walked(str(tmp_path / "t"), above=4, below=0)
        for top in (1, 2, 3):
            assert "hold fewer than" not in _note(res, top), top

    def test_control_the_density_ranking_itself_is_unchanged(self, tmp_path):
        res = _walked(str(tmp_path / "t"), above=3, below=2)
        for top, expected in ((0, 3), (1, 1), (2, 2), (9, 3)):
            rows = _doc(res, top)["top_by_density"]
            assert len(rows) == expected, (top, rows)
            for row in rows:
                assert row["inodes"] >= res.density_floor, row
                assert row["files_per_gib"] is not None, row

    def test_control_the_other_rankings_are_still_cut_to_n(self, tmp_path):
        res = _walked(str(tmp_path / "t"), above=2, below=12)
        for top, expected in ((5, 5), (2, 2), (0, 14)):
            doc = _doc(res, top)
            assert len(doc["top_by_size"]) == expected, (top, doc["top_by_size"])
            assert len(doc["top_by_inodes"]) == expected, top

    def test_control_the_walk_figures_are_untouched(self, tmp_path):
        res = _walked(str(tmp_path / "t"), above=2, below=12)
        doc = _doc(res, 5)
        assert doc["inodes"] == res.inodes and doc["size_bytes"] == res.size
        assert doc["files"] == res.files and doc["dirs"] == res.dirs
