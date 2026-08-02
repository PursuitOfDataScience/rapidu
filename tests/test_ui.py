"""Presentation rules: colour is opt-in, bars are honest, glyphs degrade."""

import argparse
import re

from rapidu import cli, ui

ANSI = re.compile(r"\033\[[0-9;]*m")


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
    """Colour encodes rank within what is shown, which is relative.

    The top row is therefore the hottest colour on every listing, including a
    perfectly balanced tree. Red there would cry wolf on every run; it is
    reserved for a near-full quota, an interrupted walk, or a floor.
    """
    assert not any("red" in tone for tone in ui.RAMP_8)
    # 196-203 and 160-167 are the red block of the xterm-256 cube. Both the text
    # colour and the wash the bar is filled with have to stay out of it.
    assert not any(160 <= c <= 167 or 196 <= c <= 203 for pair in ui.RAMP_256 for c in pair)


def test_equal_values_get_equal_tones():
    s = ui.resolve_style("never", stream=FakeTTY())
    assert s.heat(0.42) == s.heat(0.42)


# --- heat_scale: the listing is coloured as a set -------------------------

# The listing that started this: a real 2 TiB model cache. Colouring each row on
# its own put 106.0, 65.8 and 58.8 GiB in one blue and 29.1, 12.6 and 11.3 in
# another, so three rows that differ by up to 2x looked identical.
REAL_LISTING = (661.5, 593.5, 343.8, 177.5, 106.0, 65.8, 58.8, 29.1, 12.6, 11.3)


def test_the_middle_of_a_real_listing_no_longer_collides():
    """The reported bug: 106.0, 65.8 and 58.8 GiB all came out the same blue."""
    for depth in (8, 256):
        a, b, c = ui.heat_scale(REAL_LISTING, depth)[4:7]
        assert a != b != c and a != c, "{}-colour: {} {} {}".format(depth, a, b, c)


def test_a_real_listing_gets_a_distinct_tone_per_row():
    assert len(set(ui.heat_scale(REAL_LISTING, 256))) == len(REAL_LISTING)


def test_eight_colours_run_out_at_the_cold_end_not_in_the_middle():
    """Nine steps cannot give ten rows a tone each, so something must share.

    What matters is *which* rows share: the tail, where the entries are already
    too small to act on. Never the middle, which is where the original collision
    was and where the reader is deciding what to delete.
    """
    tones = ui.heat_scale(REAL_LISTING, 8)
    assert len(set(tones[:8])) == 8
    assert set(tones[8:]) == {ui.heat(0.0, 8)}


def test_tones_never_warm_up_as_rows_get_smaller():
    """The ramp has to stay monotone, or colour stops meaning size at all."""
    for depth in (8, 256):
        tones = ui.heat_scale(REAL_LISTING, depth)
        ramp = [ui._tone(i, depth) for i in range(ui._ramp_len(depth))]
        steps = [ramp.index(t) for t in tones]
        assert steps == sorted(steps, reverse=True), steps


def test_rows_of_the_same_size_keep_the_same_tone():
    """Forcing a step between every pair would invent differences.

    A tree of near-identical siblings must not be painted as a gradient: that
    would claim a ranking the numbers do not support.
    """
    flat = [100.0, 100.0, 99.8, 100.0]
    assert len(set(ui.heat_scale(flat, 256))) == 1


def test_heat_scale_bottoms_out_instead_of_wrapping():
    """-n 0 on a big tree asks for more rows than the ramp has steps."""
    tones = ui.heat_scale([1000.0 / (i + 1) for i in range(60)], 256)
    assert tones[-1] == ui.heat(0.0, 256)
    assert tones[0] == ui.heat(1.0, 256)


def test_heat_scale_survives_an_all_zero_listing():
    assert ui.heat_scale([0, 0, 0], 256) == [ui.heat(0.0, 256)] * 3
    assert ui.heat_scale([], 256) == []


# --- the bar is a wash of the text tone ------------------------------------


def test_the_bar_is_washed_back_from_the_text_it_matches():
    """Eighteen cells of the text colour is a slab; the fill is knocked back."""
    s = ui.resolve_style("always", stream=FakeTTY())
    s.depth = 256
    tone = s.heat(1.0)
    assert s.translucent(tone) != (tone,)
    assert ui.bar(1.0, 10, s, accent=tone) != s.paint("█" * 10, tone)


def test_every_ramp_step_has_its_own_wash():
    """A shared wash would undo the de-collision the ramp just did."""
    washes = [ui._WASH_256[text] for text, _ in ui.RAMP_256]
    assert len(set(washes)) == len(ui.RAMP_256)


def test_eight_colour_bars_keep_the_row_tone():
    """Faint would collapse bold_blue onto blue on the terminals that honour it."""
    s = ui.resolve_style("always", stream=FakeTTY())
    s.depth = 8
    assert s.translucent("bold_blue") == ("bold_blue",)


def test_a_ranking_bar_has_no_track_but_a_gauge_does():
    """The track means 'the limit'. A ranking has no limit, only a total."""
    s = ui.resolve_style("never", stream=FakeTTY())
    assert s.bar_chars[1] not in ui.bar(0.3, 10, s, track=False)
    assert s.bar_chars[1] in ui.bar(0.3, 10, s, track=True)
    assert len(ui.bar(0.3, 10, s, track=False)) == len(ui.bar(0.3, 10, s, track=True))


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


# --- --help ----------------------------------------------------------------

FAKE_HELP = "\n".join(
    (
        "usage: rdu [PATH ...] [options]",
        "",
        "options:",
        "  -n N, --top N         how many to list (default: 10)",
        "  --settle-wait SECONDS",
        "                        try `du -s --block-size=1` and --no-quota first",
        "",
        "examples:",
        "  rdu . -i               rank by file count",
        "",
    )
)


def test_help_colour_is_off_when_colour_is_off():
    plain = ui.resolve_style("never", stream=FakeTTY())
    assert ui.colorize_help(FAKE_HELP, plain) == FAKE_HELP


def test_help_colour_occupies_no_columns():
    """argparse laid these columns out with len(); colour must not move them.

    This is the whole reason the painting happens after formatting rather than
    on the strings argparse is handed.
    """
    style = ui.resolve_style("always", stream=FakeTTY())
    real = argparse.ArgumentParser.format_help(cli.build_parser())
    for text in (FAKE_HELP, real):
        assert ANSI.sub("", ui.colorize_help(text, style)) == text


def test_a_quoted_command_is_painted_as_one_span():
    """Painting flags and quoted spans in separate passes nested a reset.

    ``ESC[0m`` landed in the middle of `du -s --block-size=1` and dropped the
    tail of it back to plain, mid-word.
    """
    style = ui.resolve_style("always", stream=FakeTTY())
    out = ui.colorize_help(FAKE_HELP, style)
    assert style.paint("`du -s --block-size=1`", "cyan") in out
    assert style.paint("--no-quota", "cyan") in out


def test_help_separates_what_you_type_from_what_you_substitute():
    style = ui.resolve_style("always", stream=FakeTTY())
    out = ui.colorize_help(FAKE_HELP, style)
    assert style.paint("--top", "bold_cyan") in out, "flags are what you type"
    assert style.paint("N", "yellow") in out, "metavars are what you substitute"
    assert style.paint("options:", "bold") in out, "headings are structure"
    assert style.paint("(default: 10)", "dim") in out, "defaults are context"


def test_an_example_is_a_command_plus_a_dim_explanation():
    style = ui.resolve_style("always", stream=FakeTTY())
    out = ui.colorize_help(FAKE_HELP, style)
    assert style.paint("rank by file count", "dim") in out
    assert style.paint("-i", "bold_cyan") in out


def test_color_never_survives_into_help(capsys):
    """`rdu --help > ticket.txt` must not paste escape codes into a ticket."""
    parser = cli.build_parser()
    parser.style = ui.resolve_style("never", stream=FakeTTY())
    assert "\033[" not in parser.format_help()


def test_color_always_reaches_help_before_argparse_parses_it():
    """-h is handled *during* parse_args, so args.color is never available."""
    assert cli._peek_color(["--color", "always", "-h"]) == "always"
    assert cli._peek_color(["--color=never"]) == "never"
    assert cli._peek_color(["--color", "nonsense"]) == "auto"
    assert cli._peek_color([]) == "auto"


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
