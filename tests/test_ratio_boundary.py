"""`ratio_x` guarded the bottom of its range and rounded across the top.

`test_pct_boundary.py` fixed `pct` by appealing to `ratio_x` as the authority --
"an inequality is still a measurement" -- and `pct` came out of that round
guarding **both** ends: `<0.1%` below and `>99.9%` just under 100, with the exact
boundaries printing as themselves. Its docstring says an interior value gets an
inequality "exactly as ``ratio_x`` does".

That was true of one end. `ratio_x`'s own sub-parity branch is `{:.2f}`, which
takes a value just under 1 and rounds it across:

    ratio_x(0.9999)  ->  "1.00x"     # allocated equals apparent. It does not.
    ratio_x(1.0)     ->  "1.0x"

Two things wrong at once. The claim is false -- parity is exactly the reading the
ALLOCATION panel is scanned for, and a mildly sparse tree sits just below it. And
the two renderings differ only in decimal count, which resurrects the very
disagreement this helper was extracted to remove: its docstring opens with "the
report has two places to say it and they disagreed: the ``WALK`` facts line
formatted it ``{:.1f}`` and the ``ALLOCATION`` panel ``{:.2f}``".

`human_bytes`, ten lines up in the same module, records the identical defect and
the remedy: "took a value just under a boundary through and then rounded it *up*
across it -- ... which no formatter should ever emit", fixed by comparing "at the
same precision the format string will use, so the test and the output cannot
disagree". The guard here is `round(r, 2) >= 1.0` for that reason.

The exact boundaries are untouched: 0 stays `0x`, parity stays `1.0x`, and
anything at or above 1 keeps its old rendering. Only the strictly interior band
moved, and `<1.00x` is the same width as the `<0.01x` this function already
emits, so no column can be pushed.
"""

import pytest

from rapidu import report, ui
from rapidu.fmt import pct, ratio_x
from rapidu.walk import SettleCheck, WalkResult

PLAIN = ui.resolve_style("never")

#: Ratios `{:.2f}` renders as exactly "1.00" -- the band that was misreported.
ACROSS = [0.996, 0.997, 0.999, 0.9999, 0.99999]

#: Ratios below the band: `{:.2f}` keeps them under parity on its own.
UNDER = [0.5, 0.98, 0.99, 0.994, 0.995]


def _flat(lines):
    return " ".join(" ".join(lines).split())


def _near_parity():
    """A walk whose allocated/apparent lands inside the misreported band."""
    res = WalkResult("/tmp/t")
    res.files, res.dirs = 6, 7
    res.apparent = 1000000
    res.size = 999000
    return res


class TestTheTopOfTheRangeNoLongerNamesParity:
    @pytest.mark.parametrize("r", ACROSS)
    def test_a_ratio_just_under_one_is_an_inequality(self, r):
        assert ratio_x(r) == "<1.00x"

    @pytest.mark.parametrize("r", ACROSS)
    def test_the_naive_formatting_really_did_name_the_boundary(self, r):
        """Vacuity guard, in the style of the sibling file: the band has to be
        one `{:.2f}` gets wrong, or the cases above would pass against any
        implementation at all."""
        assert "{:.2f}x".format(r) == "1.00x"

    def test_the_guard_and_the_format_string_cannot_disagree(self):
        """The invariant, rather than a list of values.

        For every ratio under 1, the inequality is returned exactly when the
        format string would have printed parity -- which is what tying the
        guard to `round(r, 2)` buys.
        """
        r = 0.9
        while r < 1.0:
            rounds_across = "{:.2f}".format(r) == "1.00"
            assert (ratio_x(r) == "<1.00x") is rounds_across, (r, ratio_x(r))
            r += 0.0005

    def test_it_now_agrees_with_its_neighbour_at_BOTH_ends(self):
        """Completes the pairing `test_pct_boundary.py` could only half-state."""
        # bottom: neither reports a real measurement as the zero that means
        # "this failed", and both keep the true zero.
        assert ratio_x(0.0) == "0x" and pct(0.0, 1.0) == "0.0%"
        assert ratio_x(0.001) == "<0.01x" and pct(0.000001, 1.0) == "<0.1%"
        # top: neither names the boundary it has not reached, and both keep it
        # when it is really reached.
        assert ratio_x(0.9999) == "<1.00x" and pct(99.999, 100.0) == ">99.9%"
        assert ratio_x(1.0) == "1.0x" and pct(100.0, 100.0) == "100.0%"

    def test_the_bound_fits_the_columns_that_show_it(self):
        """Same check the sibling file makes: a bound that overran would push a
        row rather than fix a claim. This one is the width already emitted."""
        assert len("<1.00x") == len("<0.01x") == 6

    def test_the_inequality_reaches_the_facts_line(self):
        """End to end on the surface that actually carries this band.

        The `WALK` facts line prints the ratio whenever there is one, so it is
        where a near-parity value is read. The `ALLOCATION` panel cannot show
        it at all -- see the control below -- so the facts line is the whole of
        the user-visible fix, and the earlier draft of this test asserting both
        surfaces was wrong about the panel.
        """
        res = _near_parity()
        assert 0.995 < res.alloc_ratio < 1.0, res.alloc_ratio
        facts = _flat(report.render_walk(res, SettleCheck(), style=PLAIN))
        assert "<1.00x" in facts, facts
        # The false claim is gone: no bare "1.00x" survives.
        assert "1.00x" not in facts.replace("<1.00x", ""), facts


class TestControls:
    """Behaviour that must not change. Each passes in BOTH states."""

    @pytest.mark.parametrize("r", UNDER)
    def test_a_ratio_the_format_keeps_under_parity_is_printed_as_itself(self, r):
        assert ratio_x(r) == "{:.2f}x".format(r)

    def test_0_995_still_prints_as_itself(self):
        # The near boundary, called out because it looks like it should cross:
        # `round(0.995, 2)` is 0.99 and so is `"{:.2f}".format(0.995)`. Tying
        # the guard to the format is what keeps these two answers together.
        assert ratio_x(0.995) == "0.99x"

    def test_the_bottom_of_the_range_is_untouched(self):
        assert ratio_x(0.0) == "0x"
        assert ratio_x(1.6e-05) == "<0.01x"
        assert ratio_x(0.0001) == "<0.01x"
        assert ratio_x(0.01) == "0.01x"
        assert ratio_x(None) == "n/a"

    def test_at_or_above_parity_keeps_its_old_rendering(self):
        assert ratio_x(1.0) == "1.0x"
        assert ratio_x(1.04) == "1.0x"
        assert ratio_x(8.03) == "8.0x"

    def test_the_allocation_panel_is_silent_near_parity_either_way(self):
        """Why the fix shows up on one surface only, stated as a measurement.

        `allocation_is_material` returns False unless the ratio is >= 1.15 or
        <= 1/1.15, because that panel exists to flag *divergence*. The whole
        `<1.00x` band is inside the suppressed zone, so the panel neither
        printed the false `1.00x` before nor prints the inequality now.
        """
        res = _near_parity()
        assert report.allocation_is_material(res) is False
        assert report.render_allocation(res, PLAIN) == []
        assert report._ALLOC_RATIO == 1.15

    def test_pct_is_not_touched(self):
        assert pct(0.0, 1.0) == "0.0%"
        assert pct(50.0, 100.0) == "50.0%"
        assert pct(99.999, 100.0) == ">99.9%"
        assert pct(100.0, 100.0) == "100.0%"
        assert pct(102.0, 100.0) == "102.0%"
        assert pct(None, 100.0) == "n/a"
        assert pct(1.0, 0) == "n/a"

    def test_an_ordinary_ratio_still_renders_in_both_places(self):
        # The existing end-to-end case, restated so this file's own use of the
        # render helpers cannot drift from `test_audit_round_six.py`'s.
        res = WalkResult("/tmp/t")
        res.files, res.dirs = 6, 7
        res.apparent = 10132881
        res.size = 120320
        token = ratio_x(res.alloc_ratio)
        assert token == "0.01x"
        assert token in _flat(report.render_walk(res, SettleCheck(), style=PLAIN))
        assert token in _flat(report.render_allocation(res, PLAIN))
