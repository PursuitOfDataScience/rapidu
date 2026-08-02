"""Presentation rules: colour is opt-in, bars are honest, glyphs degrade."""

from slurmdisk import ui


class FakeTTY:
    """A stand-in stream: `io.StringIO.encoding` is read-only, so not that."""

    def __init__(self, tty=True, encoding="utf-8"):
        self._tty = tty
        self.encoding = encoding

    def isatty(self):
        return self._tty


def test_no_colour_when_not_a_terminal():
    """Output is routinely redirected into a ticket; escape codes there are junk."""
    s = ui.resolve_style("auto", stream=FakeTTY(tty=False))
    assert not s.color
    assert s.paint("x", "red") == "x"


def test_colour_when_a_terminal(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    assert ui.resolve_style("auto", stream=FakeTTY(tty=True)).color


def test_no_color_env_is_honoured(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert not ui.resolve_style("auto", stream=FakeTTY(tty=True)).color


def test_dumb_terminal_gets_no_colour(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "dumb")
    assert not ui.resolve_style("auto", stream=FakeTTY(tty=True)).color


def test_explicit_modes_override(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert ui.resolve_style("always", stream=FakeTTY(tty=False)).color
    assert not ui.resolve_style("never", stream=FakeTTY(tty=True)).color


def test_ascii_fallback_on_non_utf8(monkeypatch):
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LANG", raising=False)
    s = ui.resolve_style("never", stream=FakeTTY(encoding="ascii"))
    assert s.bar_chars == ("#", "-")


def test_ascii_flag_forces_ascii():
    s = ui.resolve_style("never", ascii_only=True, stream=FakeTTY(encoding="utf-8"))
    assert s.bar_chars == ("#", "-")


def test_bar_is_proportional_and_clamped():
    s = ui.resolve_style("never", stream=FakeTTY())
    assert ui.bar(0.0, 10, s).count(s.bar_chars[0]) == 0
    assert ui.bar(1.0, 10, s).count(s.bar_chars[0]) == 10
    assert ui.bar(0.5, 10, s).count(s.bar_chars[0]) == 5
    assert ui.bar(5.0, 10, s).count(s.bar_chars[0]) == 10, "must clamp above 1"
    assert ui.bar(-1.0, 10, s).count(s.bar_chars[0]) == 0, "must clamp below 0"


def test_min_tick_shows_a_tiny_but_real_share():
    """A small but non-zero row must not render as an empty bar.

    With partial blocks available the tick is the thinnest one rather than a
    whole cell, which is both visible and honest about the magnitude.
    """
    s = ui.resolve_style("never", stream=FakeTTY())
    drawn = ui.bar(0.001, 10, s)
    empty = ui.bar(0.0, 10, s)
    assert drawn != empty


def test_min_tick_off_leaves_a_negligible_gauge_empty():
    """0.04% of a quota must not render as 'some usage'."""
    s = ui.resolve_style("never", stream=FakeTTY())
    assert ui.bar(0.0004, 10, s, min_tick=False) == ui.bar(0.0, 10, s, min_tick=False)


def test_truncate_keeps_the_tail():
    assert ui.truncate("a/very/long/path/step-4000", 12).endswith("step-4000")
    assert len(ui.truncate("a/very/long/path/step-4000", 12)) == 12
    assert ui.truncate("short", 20) == "short"


def test_heat_ramp_differentiates_adjacent_rows():
    """Four coarse bands put six of ten real rows in the same colour."""
    s8 = ui.resolve_style("never", stream=FakeTTY())
    s8.depth = 8
    tones = {s8.heat(f) for f in (0.05, 0.2, 0.4, 0.6, 0.8, 1.0)}
    assert len(tones) >= 5, "8-colour ramp must resolve at least 5 steps"

    s256 = ui.resolve_style("never", stream=FakeTTY())
    s256.depth = 256
    tones = {s256.heat(f) for f in (0.05, 0.2, 0.4, 0.6, 0.8, 1.0)}
    assert len(tones) >= 5
    assert all(t.startswith("c256:") for t in tones)


def test_ramp_has_no_red():
    """Colour encodes 'largest shown', which is relative.

    The top row is therefore the hottest colour on every listing, including a
    perfectly balanced tree. Red there would cry wolf on every run; it is
    reserved for a near-full quota, an interrupted walk, or a floor.
    """
    assert not any("red" in tone for tone in ui.RAMP_8)
    # 196-203 and 160-167 are the red block of the xterm-256 cube.
    assert not any(160 <= c <= 167 or 196 <= c <= 203 for c in ui.RAMP_256)


def test_equal_values_get_equal_tones():
    s = ui.resolve_style("never", stream=FakeTTY())
    assert s.heat(0.42) == s.heat(0.42)


def test_bar_has_sub_cell_resolution():
    """Partial blocks give 8x the horizontal resolution of the cell count."""
    s = ui.resolve_style("never", stream=FakeTTY())
    a = ui.bar(0.50, 8, s)
    b = ui.bar(0.53, 8, s)
    assert a != b, "a 3% difference must be visible with partial blocks"
    assert any(ch in b for ch in ui._BAR_PARTIALS[1:])


def test_ascii_bar_has_no_partials():
    s = ui.resolve_style("never", ascii_only=True, stream=FakeTTY())
    out = ui.bar(0.53, 8, s)
    assert set(out) <= {"#", "-"}


def test_separators_degrade_to_ascii():
    u = ui.resolve_style("never", stream=FakeTTY(encoding="utf-8"))
    a = ui.resolve_style("never", ascii_only=True, stream=FakeTTY())
    assert ui.sep(u) != ui.sep(a) and ui.sep(a).isascii()
    assert ui.dash(a).isascii()


def test_color_depth_is_conservative(monkeypatch):
    """256-colour codes to an 8-colour terminal render as visible garbage."""
    monkeypatch.delenv("COLORTERM", raising=False)
    monkeypatch.setenv("TERM", "screen")
    assert ui.color_depth() == 8
    monkeypatch.setenv("TERM", "screen-256color")
    assert ui.color_depth() == 256
    monkeypatch.setenv("TERM", "screen")
    monkeypatch.setenv("COLORTERM", "truecolor")
    assert ui.color_depth() == 256
