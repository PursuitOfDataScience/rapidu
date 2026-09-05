"""No figure may read as 1024 of a unit that has a larger sibling.

`human_bytes` decided which unit to use from the RAW value and then rounded, so a
byte count just under a boundary passed the `v < 1024.0` test and the format string
rounded it up across the boundary anyway:

    1 GiB - 1     = 1073741823  ->  "1024.0 MiB"     (should be "1.0 GiB")
    1 TiB - 100   ->                "1024.0 GiB"     (should be "1.0 TiB")

`du -h` and `numfmt --to=iec` both print "1.0G" for that byte count. The window is
about 52 KiB wide below a MiB boundary and about 52 MiB wide below every TiB one --
and a filesystem total is this tool's headline figure, so the wide end is where its
numbers actually live.

Two things make this worth a test rather than a one-line edit. The comment above
`_UNITS` already describes the same defect at the TOP of the scale ("rendering 1 EiB
as 1024.0 PiB") and fixed it by lengthening the unit list; the interior boundaries
had it for a different reason and nothing caught that. And a sibling package's
`format_bytes` has compared the ROUNDED value since its own sighting of this,
carrying the same worked example in its comment -- so the rule was already written
down in the family, just not here.
"""

from __future__ import annotations

import pytest

from rapidu.fmt import _UNITS, human_bytes

#: Every unit that has a larger sibling. The last one is exempt by definition:
#: there is nothing to promote an exabyte to.
PROMOTABLE = _UNITS[:-1]

BOUNDARIES = [1024**power for power in range(1, len(_UNITS))]

#: How far below a boundary to probe. 1 is the classic off-by-one; the larger
#: offsets cover the part of the window that rounding, not truncation, closes.
OFFSETS = [1, 2, 10, 100, 1000, 10_000, 50_000]


def _split(rendered: str) -> tuple[float, str]:
    value, unit = rendered.rsplit(" ", 1)
    return float(value), unit


class TestNothingRendersAsAFullNextUnit:
    @pytest.mark.parametrize("boundary", BOUNDARIES, ids=lambda b: f"1024^{b.bit_length() // 10}")
    @pytest.mark.parametrize("offset", OFFSETS)
    def test_just_below_a_boundary_promotes(self, boundary, offset):
        if offset >= boundary:
            pytest.skip("offset larger than the boundary itself")
        rendered = human_bytes(boundary - offset)
        value, unit = _split(rendered)
        if unit in PROMOTABLE:
            assert value < 1024.0, (
                f"human_bytes({boundary - offset}) = {rendered!r}; {unit} has a "
                f"larger sibling, so 1024 of it should have rolled over"
            )

    @pytest.mark.parametrize("precision", [0, 1, 2, 3])
    def test_every_precision_promotes(self, precision):
        """The comparison has to use the precision the output will use.

        A fix that hardcodes one decimal is right for the default and wrong for
        every other caller, and this package parameterises it.
        """
        rendered = human_bytes(1024**3 - 1, precision=precision)
        value, unit = _split(rendered)
        assert unit == "GiB" and value < 1024.0, rendered

    def test_a_higher_precision_does_not_promote_what_it_can_still_show(self):
        """The other half, and the one the obvious fix gets wrong.

        Hardcoding `round(v, 1)` promotes anything from 1023.95 upward, which is
        right at one decimal and too eager at three: 1023.960 KiB fits in KiB at
        that precision and reads "1.000 MiB" instead, losing the digits the caller
        asked for. Rounding at the SAME precision the format string uses is what
        makes both directions right, and without this assertion the suite passes
        for either implementation.
        """
        count = 1_048_535  # 1023.9599... KiB
        assert human_bytes(count, precision=3) == "1023.960 KiB"
        assert human_bytes(count, precision=1) == "1.0 MiB"

    @pytest.mark.parametrize("boundary", BOUNDARIES)
    def test_the_boundary_itself_is_one_of_the_next_unit(self, boundary):
        value, unit = _split(human_bytes(boundary))
        assert value == 1.0, human_bytes(boundary)

    def test_negative_values_promote_too(self):
        # `neg` is applied after the unit choice, so the sign must not skip it.
        assert human_bytes(-(1024**3 - 1)) == "-1.0 GiB"


class TestTheOrdinaryOutputIsUnchanged:
    """Controls. Promoting too eagerly would be the opposite defect."""

    @pytest.mark.parametrize(
        "count,expected",
        [
            (0, "0 B"),
            (1, "1 B"),
            (512, "512 B"),
            (1023, "1023 B"),
            (1024, "1.0 KiB"),
            (1536, "1.5 KiB"),
            (1024**2, "1.0 MiB"),
            (1024**2 * 3 // 2, "1.5 MiB"),
            (1024**3 * 2, "2.0 GiB"),
            (1024**4 * 5, "5.0 TiB"),
        ],
    )
    def test_values_away_from_a_boundary_are_untouched(self, count, expected):
        assert human_bytes(count) == expected

    def test_none_is_still_not_zero(self):
        # Constraint 10, which this change must not disturb.
        assert human_bytes(None) == "n/a"

    def test_the_top_unit_is_allowed_to_exceed_1024(self):
        # Nothing to promote to, so this is the one case where a big number is
        # the honest answer rather than a missed rollover.
        value, unit = _split(human_bytes(1024**7))
        assert unit == _UNITS[-1] and value >= 1024.0
