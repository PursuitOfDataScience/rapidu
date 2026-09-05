"""`pct` named a boundary it had not reached — the rule `ratio_x` states above it.

`ratio_x` sits twenty lines up in the same module and settles the question in its
own docstring: "an inequality is still a measurement, where ``0.00x`` reads as one
that failed", and "a ratio of exactly zero is not that case: nothing was allocated,
which is a fact, and it prints ``0x``". `pct` did not follow it. ``{:.1f}`` rounds to
``0.0`` anywhere below 0.05 and to ``100.0`` from 99.95 up, so both ends of the range
were printed as a value the measurement had not reached.

Both ends are reachable and both mislead in the direction that matters:

* The quota line prints the bytes AND the percentage in one sentence — "the mount at
  X reports 32.0 GiB of 32.0 GiB used (100.0%), 16.0 MiB free". The figure a reader
  acts on says full; the figure beside it says there is room. 99.95% of a 32 GiB home
  is 16 MiB still writable.
* ``"({} of the tree)"`` (`report.py:1145`) is the other end: a subtree holding one
  inode of 13,051 printed ``0.0% of the tree``, i.e. none of it, and "which
  directory holds this" is the question the tree report exists to answer.

The exact boundaries still print as themselves — 0 of anything is a fact, and
part == whole really is 100% — so only the strictly interior band moved. Nothing in
the existing suite changed: three tests pin `pct` and all three are outside the band.
"""

import pytest

from rapidu import report, ui
from rapidu.fmt import pct, ratio_x
from rapidu.quota import MountReport

PLAIN = ui.resolve_style("never")

GIB = 1024**3
MIB = 1024**2


def _flat(lines):
    return " ".join(" ".join(lines).split())


class TestTheBoundaryIsNotNamedFromInside:
    @pytest.mark.parametrize(
        ("part", "whole"),
        [(1, 13051), (1e-9, 1.0), (0.0004, 1.0), (0.00049, 1.0), (1, 2500)],
    )
    def test_a_nonzero_share_under_a_tenth_is_bounded_not_zeroed(self, part, whole):
        assert pct(part, whole) == "<0.1%"

    @pytest.mark.parametrize(
        ("part", "whole"),
        [(0.9995, 1.0), (0.9996, 1.0), (13050, 13051), (2499, 2500)],
    )
    def test_a_share_under_the_whole_is_bounded_not_rounded_up(self, part, whole):
        assert pct(part, whole) == ">99.9%"

    def test_the_naive_formatting_really_did_name_the_boundary(self):
        """Vacuity guard: the band has to be one ``{:.1f}`` gets wrong, or the
        cases above would pass against any implementation at all."""
        assert "{:.1f}%".format(100.0 * 1 / 13051) == "0.0%"
        assert "{:.1f}%".format(100.0 * 13050 / 13051) == "100.0%"

    def test_it_now_agrees_with_the_neighbour_it_should_have_followed(self):
        """One principle, two formatters: neither reports a real measurement as
        the zero that means "this failed", and both keep the true zero."""
        assert ratio_x(0.0) == "0x" and pct(0.0, 1.0) == "0.0%"
        assert ratio_x(0.001) == "<0.01x" and pct(0.000001, 1.0) == "<0.1%"

    def test_the_bound_fits_the_columns_that_show_it(self):
        """`report.py` right-aligns this into 6 and 7 cells; a bound that overran
        would push the row rather than fix a claim."""
        assert len(">99.9%") == 6
        assert len("<0.1%") == 5
        assert "{:>6}".format(">99.9%") == ">99.9%"
        assert "{:>7}".format(">99.9%") == " >99.9%"


class TestTheQuotaSentenceNoLongerContradictsItself:
    """End to end, because a formatter is not what a reader complains about."""

    @staticmethod
    def _mount(used, avail):
        return MountReport(
            path="/home/me",
            mount="/home",
            total=used + avail,
            used=used,
            avail=avail,
            inodes_total=1000,
            inodes_free=500,
            # Set explicitly: the same line reports an inode percentage too, and
            # leaving `inodes_avail` unset put THAT figure at 100.0% -- which an
            # unanchored assertion then read as the block figure. 500 of 1000
            # used keeps it at a plain 50.0% so the assertions below can only be
            # about the bytes.
            inodes_avail=500,
        )

    def _line(self, monkeypatch, used, avail, tmp_path):
        monkeypatch.setattr(report.quotamod, "mount_report", lambda _p: self._mount(used, avail))
        return _flat(report._mount_fallback([str(tmp_path)], PLAIN))

    def test_a_home_with_room_left_is_not_reported_as_full(self, monkeypatch, tmp_path):
        """32 GiB with 16 MiB free: fraction 0.99951, inside the band."""
        line = self._line(monkeypatch, 32 * GIB - 16 * MIB, 16 * MIB, tmp_path)
        assert "used (>99.9%)" in line, line
        assert "used (100.0%)" not in line, line
        # The two figures in one sentence now say the same thing.
        assert "16.0 MiB free" in line, line

    def test_control_a_genuinely_full_home_still_says_one_hundred(self, monkeypatch, tmp_path):
        """CONTROL. Nothing available at all is exactly 100%, and that claim is
        true — the fix must not have made "full" unsayable. Passes in both states.
        """
        line = self._line(monkeypatch, 32 * GIB, 0, tmp_path)
        assert "used (100.0%)" in line, line
        assert "0 B free" in line, line

    def test_control_an_ordinary_home_is_unchanged(self, monkeypatch, tmp_path):
        """CONTROL. Half full reads exactly as it did."""
        line = self._line(monkeypatch, 16 * GIB, 16 * GIB, tmp_path)
        assert "used (50.0%)" in line, line


class TestControls:
    def test_control_the_three_existing_pins_still_hold(self):
        """`test_cli.py` pins these three; repeated here so this file fails rather
        than silently disagreeing with them."""
        assert pct(1, 0) == "n/a"
        assert pct(None, 10) == "n/a"
        assert pct(0.5, 1.0) == "50.0%"

    @pytest.mark.parametrize(
        ("part", "whole", "shown"),
        [
            (0.0, 1.0, "0.0%"),
            (0, 13051, "0.0%"),
            (0.0005, 1.0, "0.1%"),
            (0.9994, 1.0, "99.9%"),
            (1.0, 1.0, "100.0%"),
            (13051, 13051, "100.0%"),
            (1.02, 1.0, "102.0%"),
            (-0.1, 1.0, "-10.0%"),
        ],
    )
    def test_control_every_value_outside_the_band_is_unchanged(self, part, whole, shown):
        """The two edges the fix must not have swallowed are here: 0.0005 is the
        first value that rounds to a real 0.1%, and 0.9994 the last that rounds to
        a real 99.9%. Over 100% stays — a quota can be over its limit, and that is
        the measurement, not an overflow."""
        assert pct(part, whole) == shown

    def test_control_a_missing_reading_is_still_not_a_number(self):
        assert pct(None, 1.0) == "n/a"
        assert pct(1.0, None) == "n/a"
        assert pct(1.0, 0) == "n/a"
