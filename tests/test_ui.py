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
    assert ui.bar(0.0, 10, s) == "-" * 10 or ui.bar(0.0, 10, s).count("█") == 0
    assert ui.bar(1.0, 10, s).count(s.bar_chars[0]) == 10
    assert ui.bar(0.5, 10, s).count(s.bar_chars[0]) == 5
    assert ui.bar(5.0, 10, s).count(s.bar_chars[0]) == 10, "must clamp above 1"
    assert ui.bar(-1.0, 10, s).count(s.bar_chars[0]) == 0, "must clamp below 0"


def test_min_tick_shows_a_tiny_but_real_share():
    s = ui.resolve_style("never", stream=FakeTTY())
    assert ui.bar(0.001, 10, s).count(s.bar_chars[0]) == 1


def test_min_tick_off_leaves_a_negligible_gauge_empty():
    """0.04% of a quota must not render as 'some usage'."""
    s = ui.resolve_style("never", stream=FakeTTY())
    assert ui.bar(0.0004, 10, s, min_tick=False).count(s.bar_chars[0]) == 0


def test_truncate_keeps_the_tail():
    assert ui.truncate("a/very/long/path/step-4000", 12).endswith("step-4000")
    assert len(ui.truncate("a/very/long/path/step-4000", 12)) == 12
    assert ui.truncate("short", 20) == "short"
