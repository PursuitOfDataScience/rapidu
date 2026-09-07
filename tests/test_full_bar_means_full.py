"""A bar filled to its last cell must mean the fraction really reached 1.0.

The partial-block path never claimed the boundary early -- ``filled`` floors, so
short of 1.0 the last cell is always a partial glyph. The two paths with no
partials to spend did: ASCII (``Style.partials`` is ``("",)``) and ``hatched``,
which discards them by design. Both rounded the final cell up, so at the widths
the report uses a bar went solid from 93.8% at 8 cells and 97.2% at 18 -- beside
a label that :func:`rapidu.fmt.pct` had gone to some trouble to keep honest.
``pct`` returns ``>99.9%`` throughout 99.95-99.99 and prints ``100.0%`` only when
part really equals whole, so 1.0 is the only fraction a solid bar may stand for.

The family's other two members were fixed the same way, each keyed to the
precision of its own labels: ``slurmpast.render.bar_cells`` on
``round(percent, 1)``, ``slurmwatch.tui`` on the unrounded percent under a
``>99%`` label.
"""

import pytest

from rapidu import report, ui
from rapidu.fmt import pct
from rapidu.quota import QuotaRow, QuotaSnapshot

PLAIN = ui.resolve_style("never")
ASCII = ui.resolve_style("never", ascii_only=True)

# Either side of both turnover points: ASCII/hatched round up from
# 1 - 1/(2 * width), the partial path from nothing at all.
BOUNDARY = [90.0, 96.6, 98.0, 99.0, 99.4, 99.6, 99.9, 99.94, 99.96, 100.0]
# 8 and 18 for the sweep; 10 and 12 are what `render_quota` and the age
# histogram really pass, and `_BAR_W` is 18. 1 is the degenerate end, where the
# reserve and `min_tick` collide over the only cell there is.
WIDTHS = [1, 8, 10, 12, 18]


def fill_char(style, hatched):
    if hatched:
        return ui._BAR_HATCH if style.unicode else ui._BAR_HATCH_ASCII
    return style.bar_chars[0]


def solid(drawn, style, hatched=False):
    """Is every cell of `drawn` a whole fill cell -- no partial, no track?"""
    return set(drawn) == {fill_char(style, hatched)}


class TestTheBoundary:
    @pytest.mark.parametrize("width", WIDTHS)
    @pytest.mark.parametrize("percent", BOUNDARY)
    @pytest.mark.parametrize("hatched", [False, True])
    @pytest.mark.parametrize("track", [False, True])
    def test_a_solid_bar_means_the_whole_of_it(self, width, percent, hatched, track):
        for style in (PLAIN, ASCII):
            drawn = ui.bar(percent / 100.0, width, style, hatched=hatched, track=track)
            assert solid(drawn, style, hatched) is (percent >= 100.0), (
                percent,
                width,
                style.unicode,
                drawn,
            )

    @pytest.mark.parametrize("width", WIDTHS)
    def test_the_bar_agrees_with_the_label_beside_it(self, width):
        """The bar and `pct` are handed the same number, so they must say the same thing."""
        for percent in BOUNDARY:
            f = percent / 100.0
            label = pct(f, 1.0)
            for style in (PLAIN, ASCII):
                for hatched in (False, True):
                    drawn = ui.bar(f, width, style, hatched=hatched)
                    assert solid(drawn, style, hatched) is (label == "100.0%"), (
                        label,
                        width,
                        drawn,
                    )

    def test_a_one_cell_gauge_would_rather_read_empty_than_full(self):
        # At width 1 the reserve and `min_tick` want the same single cell, and
        # the reserve wins: a one-cell gauge cannot say "a little" and "not
        # full" at once, and claiming the boundary is the worse of the two
        # errors. `slurmpast.render.bar_cells` resolves the same collision the
        # same way -- its top guard runs after its low-end guard and overrides
        # it. No production width is anywhere near this; `_BAR_W` is 18.
        for style in (PLAIN, ASCII):
            for hatched in (False, True):
                drawn = ui.bar(0.3, 1, style, hatched=hatched)
                assert not solid(drawn, style, hatched), (style.unicode, hatched, drawn)
                assert solid(ui.bar(1.0, 1, style, hatched=hatched), style, hatched)


class TestTheSurfacesThatDrawIt:
    def test_an_ascii_quota_row_short_of_full_keeps_a_cell_of_track(self):
        # `rdu --ascii` on a 97.5%-full quota: ten cells, and rounding lit all
        # ten. The row then said "full" in the picture and "97.5%" in the number.
        snap = QuotaSnapshot("test")
        snap.available = True
        snap.taken_at = snap.read_at - 60.0
        snap.rows = [
            QuotaRow("lab", "files", "group", 3_900_000, 4_000_000, None, mount="/project")
        ]
        rows = [ln for ln in report.render_quota(snap, style=ASCII) if "/project" in ln]
        assert rows, "the row must render at all"
        assert "97.5%" in rows[0], rows[0]
        assert "#" * 10 not in rows[0], rows[0]
        assert "#########-" in rows[0], rows[0]

    def test_a_quota_really_at_its_limit_still_fills(self):
        snap = QuotaSnapshot("test")
        snap.available = True
        snap.taken_at = snap.read_at - 60.0
        snap.rows = [
            QuotaRow("lab", "files", "group", 4_000_000, 4_000_000, None, mount="/project")
        ]
        rows = [ln for ln in report.render_quota(snap, style=ASCII) if "/project" in ln]
        assert "100.0%" in rows[0] and "#" * 10 in rows[0], rows[0]

    def test_the_hatched_remainder_row_reserves_too(self):
        # The remainder row is hatched on EVERY listing, so this path is the one
        # a Unicode terminal hits: one visible child and 97.5% of the tree
        # hidden behind "N more" drew eighteen solid cells beside "97.5%".
        res = _listing([0.025, 0.02, 0.02] + [0.0201] * 45)
        rows = report.render_entries(res, 1, False, PLAIN)
        rest = rows[-1]
        assert "more" in rest, rest
        assert ui._BAR_HATCH * report._BAR_W not in rest, rest
        assert ui._BAR_HATCH * (report._BAR_W - 1) + PLAIN.bar_chars[1] in rest, rest


class TestControls:
    """These hold in both states -- before the reserve and after it."""

    @pytest.mark.parametrize("width", WIDTHS)
    @pytest.mark.parametrize("percent", BOUNDARY)
    def test_control_the_bar_is_still_a_box_of_exactly_width_cells(self, width, percent):
        # The reserve moves one cell from fill to track. It must not move one out
        # of the column: `test_report.test_every_bar_is_drawn_as_a_full_width_box`
        # is the whole reason the track exists.
        for style in (PLAIN, ASCII):
            for hatched in (False, True):
                for track in (False, True):
                    drawn = ui.bar(percent / 100.0, width, style, hatched=hatched, track=track)
                    assert len(drawn) == width, (percent, width, hatched, track, drawn)

    @pytest.mark.parametrize(
        "fraction,plain,ascii_,hatch",
        [
            (0.0, "░░░░░░░░", "--------", "░░░░░░░░"),
            (0.25, "██░░░░░░", "##------", "▒▒░░░░░░"),
            (0.5, "████░░░░", "####----", "▒▒▒▒░░░░"),
            (0.75, "██████░░", "######--", "▒▒▒▒▒▒░░"),
            (0.9, "███████▏", "#######-", "▒▒▒▒▒▒▒░"),
        ],
    )
    def test_control_nothing_below_the_turnover_moved(self, fraction, plain, ascii_, hatch):
        # The reserve can only bite where the round-up would have reached the
        # last cell -- above 93.75% at eight cells. Every share a reader normally
        # looks at draws exactly what it drew before.
        assert ui.bar(fraction, 8, PLAIN) == plain
        assert ui.bar(fraction, 8, ASCII) == ascii_
        assert ui.bar(fraction, 8, PLAIN, hatched=True) == hatch

    def test_control_min_tick_still_shows_a_tiny_but_real_share(self):
        # The low-end half of the rule, which this change must not touch. Note
        # rapidu's is deliberately *any* non-zero share, not the siblings'
        # "prints as >= 1%": these bars carry one decimal and 0.1% is a row.
        assert ui.bar(0.001, 10, PLAIN) == "▏░░░░░░░░░"
        assert ui.bar(0.001, 10, ASCII) == "#---------"
        assert ui.bar(0.0, 10, PLAIN) == "░" * 10
        # And off, it is still off: 0.04% of a quota is "none worth seeing".
        assert ui.bar(0.0004, 10, PLAIN, min_tick=False) == ui.bar(0.0, 10, PLAIN, min_tick=False)

    def test_control_the_partial_block_path_is_untouched(self):
        # Unicode, unhatched -- the one path that already held the invariant.
        # It must keep its sub-cell resolution, tip included.
        assert ui.bar(0.99, 18, PLAIN) == "█" * 17 + "▊"
        assert ui.bar(0.999, 18, PLAIN) == "█" * 17 + "▉"
        assert ui.bar(1.0, 18, PLAIN) == "█" * 18
        assert ui.bar(0.50, 8, PLAIN) != ui.bar(0.53, 8, PLAIN)

    def test_control_a_ranked_row_still_fills_its_box(self):
        # `test_report` pins both of these: fill plus track comes to `_BAR_W` on
        # every row, and the top row of a one-child tree is drawn full length.
        rows = report.render_entries(_listing([0.99]), 10, False, PLAIN)
        occupied = sum(rows[0].count(ch) for ch in (PLAIN.bar_chars[0],) + ui._BAR_PARTIALS[1:])
        assert occupied == report._BAR_W, rows[0]

    def test_control_over_the_limit_is_still_solid(self):
        # A bar clamps at full and 105.6% is the one state it cannot express, so
        # the reserve must not steal a cell there -- the `OVER` marker and a full
        # bar are what say it.
        for style in (PLAIN, ASCII):
            for hatched in (False, True):
                assert solid(ui.bar(1.056, 12, style, hatched=hatched), style, hatched)


def _listing(shares, root="/tmp/tree", total=1 << 40):
    """A walk whose children hold the given shares of the tree. Mirrors test_report."""
    import os

    from rapidu.walk import Entry, WalkResult

    r = WalkResult(root)
    r.size = total
    r.apparent = total
    r.files = 100 * len(shares)
    r.dirs = 1
    r.elapsed = 1.0
    r.threads = 8
    r.by_uid = {os.getuid(): (total, 100 * len(shares))}
    r.by_dev = {42: (total, 100 * len(shares))}
    for i, share in enumerate(shares):
        e = Entry(os.path.join(root, "child{}".format(i)), True)
        e.add(int(total * share), 99, 1)
        r.dir_agg[e.path] = e
    r.dir_agg[root] = Entry(root, True)
    r.files = sum(e.files for e in r.dir_agg.values())
    r.dirs = sum(e.dirs for e in r.dir_agg.values())
    return r
