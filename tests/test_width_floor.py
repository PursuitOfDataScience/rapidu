"""The layout clamps to ``[60, 160]``, and the floor is the interesting end.

``report._layout_width`` described the pair as a cap that "only bites upwards".
That is true of the 60-column terminal its own example used -- which sits exactly
on the floor -- and false of every narrower one: ``ui.terminal_width`` returns 60
for a 40-column terminal, so the frame overruns instead of shrinking.

Measured before the wording was corrected: at ``COLUMNS`` of 30, 40, 45, 50 and 55
the box is 60 cells wide either way, and ten of its lines are wider than a
40-column terminal.

The floor itself is deliberate and stays -- below 60 the ranking table has no
readable form, since a size, a bar, a percentage and an inode count do not fit --
so what these tests pin is the clamp and the consequence, not a promise that
narrow terminals are honoured.
"""

from rapidu import report, ui


def _width(monkeypatch, columns):
    monkeypatch.setenv("COLUMNS", str(columns))
    monkeypatch.setenv("LINES", "24")
    return ui.terminal_width()


def test_the_layout_never_narrows_below_sixty(monkeypatch):
    """Every terminal under 60 columns lays out at 60."""
    for columns in (20, 30, 40, 45, 50, 55, 59):
        assert _width(monkeypatch, columns) == 60, columns


def test_the_frame_therefore_overruns_a_narrow_terminal(monkeypatch):
    """The consequence, stated rather than left for a user to discover.

    A 60-cell frame on a 40-column terminal is 20 cells of overrun. This is the
    trade the floor buys -- a readable table over a fitting one -- and it is
    asserted so that anyone changing the floor sees what it costs.
    """
    assert _width(monkeypatch, 40) == 60
    style = ui.Style(color=False, unicode_ok=True, width=_width(monkeypatch, 40))
    assert report._layout_width(style) == 60
    assert report._layout_width(style) > 40, "the layout is wider than the terminal"


def test_control_a_terminal_between_the_bounds_is_honoured(monkeypatch):
    """CONTROL, passing with the wording corrected or not.

    Between the floor and the ceiling the layout IS the terminal, which is the
    part of the docstring that was always true and must stay true.
    """
    for columns in (60, 61, 70, 100, 159, 160):
        assert _width(monkeypatch, columns) == columns, columns


def test_control_b_the_ceiling_still_bites(monkeypatch):
    """CONTROL, in both states. 160 is the readable-prose bound."""
    for columns in (161, 200, 400):
        assert _width(monkeypatch, columns) == 160, columns


def test_control_c_elastic_content_is_still_capped_at_eighty(monkeypatch):
    """CONTROL, in both states. ``_LAYOUT_COLUMNS`` narrows prose further.

    Two different bounds, and correcting the docstring must not merge them: the
    frame may be 160 wide while wrapped prose stops at 80.
    """
    style = ui.Style(color=False, unicode_ok=True, width=_width(monkeypatch, 200))
    assert style.width == 160
    assert report._layout_width(style) == report._LAYOUT_COLUMNS == 80
