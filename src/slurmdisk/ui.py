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
    "bold_cyan": "\033[1;36m",
    "bold_blue": "\033[1;34m",
}

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
