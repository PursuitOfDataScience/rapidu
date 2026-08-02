"""Terminal presentation: colour, bars, and column layout.

Kept separate from :mod:`slurmdisk.report` so that *what* is reported and *how*
it looks are not tangled together, and so the colour rules live in exactly one
place.

Colour is off unless stdout is a terminal. It also honours ``NO_COLOR``
(https://no-color.org) and ``TERM=dumb``, because this output is routinely
redirected into a support ticket and escape codes in a pasted ticket are worse
than no colour at all.
"""

import os
import sys
from typing import List, Optional

_RESET = "\033[0m"
_CODES = {
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "cyan": "\033[36m",
    "bold_red": "\033[1;31m",
    "bold_cyan": "\033[1;36m",
    "bold_blue": "\033[1;34m",
}

# Share-of-tree thresholds for the heat ramp, largest first.
#
# A single accent colour for every row wastes the one channel that carries
# information for free: a reader scanning the list wants to know where the space
# went, and identical hues for a 30% row and a 2% row make them look equally
# worth investigating. Only the basic 8 ANSI colours are used, because TERM is
# often `screen` or `xterm` over ssh and bright/256-colour codes are not
# universally honoured.
HEAT = (
    (0.25, "bold_red"),  # dominant: this is where the space went
    (0.10, "yellow"),  # substantial
    (0.03, "green"),  # ordinary
    (0.0, "cyan"),  # negligible
)


def heat(fraction: Optional[float]) -> str:
    """Tone for a share of the whole. ``None`` -> the calmest tone."""
    if fraction is None:
        return HEAT[-1][1]
    for threshold, tone in HEAT:
        if fraction >= threshold:
            return tone
    return HEAT[-1][1]


# Full block / light shade. Falls back to ASCII where the encoding cannot
# represent them -- a mojibake bar is worse than a plain one.
_BAR_FULL, _BAR_EMPTY = "█", "░"
_BAR_FULL_ASCII, _BAR_EMPTY_ASCII = "#", "-"


class Style:
    """Resolved presentation settings for one run."""

    def __init__(self, color: bool, unicode_ok: bool, width: int) -> None:
        self.color = color
        self.unicode = unicode_ok
        self.width = width

    def paint(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        prefix = "".join(_CODES.get(s, "") for s in styles)
        return prefix + text + _RESET if prefix else text

    @property
    def bar_chars(self):
        if self.unicode:
            return _BAR_FULL, _BAR_EMPTY
        return _BAR_FULL_ASCII, _BAR_EMPTY_ASCII


def _supports_unicode(stream) -> bool:
    enc = getattr(stream, "encoding", None) or ""
    if "utf" in enc.lower():
        return True
    return "utf" in (os.environ.get("LC_ALL") or os.environ.get("LANG") or "").lower()


def resolve_style(mode: str = "auto", ascii_only: bool = False, stream=None) -> Style:
    """Decide colour and glyphs for this invocation.

    ``mode`` is ``auto`` / ``always`` / ``never``.
    """
    stream = stream if stream is not None else sys.stdout
    if mode == "always":
        color = True
    elif mode == "never":
        color = False
    else:
        color = bool(
            getattr(stream, "isatty", lambda: False)()
            and not os.environ.get("NO_COLOR")
            and os.environ.get("TERM", "") != "dumb"
        )
    return Style(
        color=color,
        unicode_ok=not ascii_only and _supports_unicode(stream),
        width=terminal_width(),
    )


def terminal_width(default: int = 100) -> int:
    try:
        import shutil

        w = shutil.get_terminal_size((default, 24)).columns
    except Exception:
        w = default
    # Narrow enough to paste, wide enough for the table.
    return max(60, min(w, 160))


def bar(
    fraction: float, width: int, style: Style, accent: str = "cyan", min_tick: bool = True
) -> str:
    """A proportional bar. ``fraction`` is clamped to [0, 1].

    ``min_tick`` renders any non-zero share as at least one cell, so a small but
    real entry is not mistaken for nothing. Turn it off where the bar is a gauge
    against a limit rather than a comparison between rows: showing a filled cell
    for 0.04% of a quota reads as "some usage" when the honest answer is "none
    worth seeing".
    """
    if width <= 0:
        return ""
    full_ch, empty_ch = style.bar_chars
    f = 0.0 if fraction < 0 else (1.0 if fraction > 1 else fraction)
    filled = int(round(f * width))
    if min_tick and filled == 0 and f > 0:
        filled = 1
    return style.paint(full_ch * filled, accent) + style.paint(empty_ch * (width - filled), "dim")


def truncate(text: str, width: int) -> str:
    """Shorten to ``width``, keeping the tail, which is the distinguishing part.

    ``.../checkpoints/step-4000`` says more than ``experiments/run-a/che...``.
    """
    if width <= 0 or len(text) <= width:
        return text
    if width <= 3:
        return text[-width:]
    return "..." + text[-(width - 3) :]


def rule(style: Style, width: Optional[int] = None) -> str:
    return style.paint("-" * (width or style.width), "dim")


def heading(text: str, style: Style) -> str:
    return style.paint(text, "bold")


def key_value(label: str, value: str, style: Style, label_width: int = 20) -> str:
    return "  {}  {}".format(
        style.paint(label.ljust(label_width), "dim"),
        value,
    )


def warn(text: str, style: Style) -> str:
    return style.paint("! " + text, "yellow")


def alarm(text: str, style: Style) -> str:
    return style.paint("! " + text, "red")


def ok(text: str, style: Style) -> str:
    return style.paint(text, "green")


def columns(rows: List[List[str]], aligns: str) -> List[str]:
    """Lay out plain (already-uncoloured) cells into aligned columns."""
    if not rows:
        return []
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    out = []
    for r in rows:
        cells = []
        for i, cell in enumerate(r):
            cells.append(cell.rjust(widths[i]) if aligns[i] == "r" else cell.ljust(widths[i]))
        out.append(" ".join(cells).rstrip())
    return out


# Spinner frames. Braille reads as smooth motion at 100 ms; the ASCII fallback
# is for the same terminals that get ASCII bars.
_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_SPIN_ASCII = "|/-\\"

# A walk shorter than this finishes before a human registers the spinner, and
# painting one would only produce a flicker.
PROGRESS_DELAY_S = 0.4
PROGRESS_INTERVAL_S = 0.1


class Spinner:
    """Single-line progress on stderr, erased when the work finishes.

    **stderr, not stdout.** The report is meant to be piped into a file or a
    support ticket, and a progress line interleaved with it would corrupt that.
    Redirecting stdout still shows progress on the terminal; redirecting both
    shows none, because stderr is then not a tty.
    """

    def __init__(self, style: Style, stream=None) -> None:
        self.style = style
        self.stream = stream if stream is not None else sys.stderr
        self.frames = _SPIN if style.unicode else _SPIN_ASCII
        self._i = 0
        self._painted = 0
        self.enabled = bool(getattr(self.stream, "isatty", lambda: False)())

    def frame(self) -> str:
        ch = self.frames[self._i % len(self.frames)]
        self._i += 1
        return ch

    def paint(self, text: str) -> None:
        if not self.enabled:
            return
        line = "{} {}".format(self.frame(), text)
        # Truncate rather than wrap: a wrapped progress line cannot be erased
        # with a single carriage return and leaves debris behind.
        line = line[: max(0, self.style.width - 1)]
        pad = " " * max(0, self._painted - len(line))
        self.stream.write("\r" + self.style.paint(line, "dim") + pad)
        self.stream.flush()
        self._painted = len(line)

    def clear(self) -> None:
        if not self.enabled or not self._painted:
            return
        self.stream.write("\r" + " " * self._painted + "\r")
        self.stream.flush()
        self._painted = 0


def progress_text(path: str, inodes: int, dirs: int, rate: float, elapsed: float) -> str:
    """What to say while walking. No percentage: the total is unknowable.

    A walk cannot know how many inodes it will find until it has found them, so a
    progress *bar* would have to invent a denominator. Throughput and elapsed
    time are both real, and together they answer the question the user actually
    has, which is "is this moving, and roughly how fast".
    """
    return "scanning {}  {:,} files  {:,} dirs  {:,.0f}/s  {:.0f}s".format(
        path, inodes, dirs, rate, elapsed
    )
