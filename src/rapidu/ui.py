"""Terminal presentation: colour, bars, and column layout.

Kept separate from :mod:`rapidu.report` so that *what* is reported and *how*
it looks are not tangled together, and so the colour rules live in exactly one
place.

Colour is off unless stdout is a terminal. It also honours ``NO_COLOR``
(https://no-color.org) and ``TERM=dumb``, because this output is routinely
redirected into a support ticket and escape codes in a pasted ticket are worse
than no colour at all.
"""

import os
import re
import sys
import unicodedata
from typing import List, Optional, Sequence, Tuple

_RESET = "\033[0m"
_CODES = {
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "bold_red": "\033[1;31m",
    "bold_green": "\033[1;32m",
    "bold_yellow": "\033[1;33m",
    "bold_cyan": "\033[1;36m",
    "bold_blue": "\033[1;34m",
    "bold_magenta": "\033[1;35m",
}

# Heat ramps, coolest first.
#
# No red at either end. The ramp encodes rank within what is shown, which is
# relative -- so the top row is the hottest colour on *every* listing, including
# a perfectly balanced tree. If that top colour were red, the tool would cry wolf
# every single run. Red is reserved for things that are actually wrong: a quota
# near its limit, an interrupted walk, a floor instead of a total.
#
# The 8-colour ramp matters more than it looks like it should, because TERM is
# frequently `screen` or a bare `xterm` over ssh, which advertise 8. Nine steps
# are available there by pairing each hue with its bold variant. Magenta anchors
# the cold end for the same reason viridis starts in dark purple: it is the one
# hue left that nothing else in the output uses.
RAMP_8 = (
    "magenta",
    "blue",
    "bold_blue",
    "cyan",
    "bold_cyan",
    "green",
    "bold_green",
    "yellow",
    "bold_yellow",
)

# xterm-256, as (text, fill) pairs: deep blue up to amber, and for each step a
# darker twin of the same hue that the bar is filled with. See `translucent`.
#
# Twelve steps, not more. A finer ramp sounds strictly better and is not: the
# 6x6x6 cube has little perceptual room between 5fff5f and 87ff5f, so an
# eighteen-step version spent four of them inside one green and produced exactly
# the "these rows look the same" complaint it was meant to fix. Twelve steps that
# are each visibly a step beats eighteen that are not, and twelve is still more
# than a default ten-row listing can use.
RAMP_256 = (
    (25, 18),  # 005faf / 000087   deep blue
    (32, 24),  # 0087d7 / 005f87   blue
    (39, 31),  # 00afff / 0087af   azure
    (45, 32),  # 00d7ff / 0087d7   sky
    (51, 37),  # 00ffff / 00afaf   cyan
    (49, 36),  # 00ffaf / 00af87   spring
    (84, 35),  # 5fff87 / 00af5f   mint
    (119, 70),  # 87ff5f / 5faf00   green
    (155, 106),  # afff5f / 87af00   yellow-green
    (227, 142),  # ffff5f / afaf00   yellow
    (220, 178),  # ffd700 / d7af00   gold
    (214, 172),  # ffaf00 / d78700   amber
)

# Directory sizes are heavily skewed: one subtree is usually a third of the tree
# and the whole tail is a few percent each. Indexing the ramp linearly therefore
# spends most of it on the top two rows and crushes everything below into one
# colour. The exponent spreads the tail back out over the ramp it deserves; 0.45
# is the sRGB encoding gamma, and it is the right neighbourhood for the same
# reason it is there -- perceived brightness follows roughly the same curve.
_RAMP_GAMMA = 0.45

# Two rows count as "the same size" for colouring when they are within this of
# each other. Below it a visible colour step would claim a difference the reader
# cannot check; above it, `heat_scale` insists on one.
_SAME_SIZE = 0.01


def _ramp_len(depth: int) -> int:
    return len(RAMP_256) if depth >= 256 else len(RAMP_8)


def _tone(index: int, depth: int) -> str:
    if depth >= 256:
        return "c256:{}".format(RAMP_256[index][0])
    return RAMP_8[index]


def _ramp_index(fraction: Optional[float], n: int) -> int:
    if not fraction or fraction <= 0:
        return 0
    if fraction >= 1:
        return n - 1
    return min(n - 1, int((fraction**_RAMP_GAMMA) * n))


def heat(fraction: Optional[float], depth: int = 8) -> str:
    """Tone name for a 0..1 position on the ramp.

    The caller passes ``value / largest_value``, not share-of-total, so the whole
    ramp gets used on every listing however lopsided or flat the tree is. Share
    is not lost -- it is the bar length and its own printed column.

    For a listing, prefer :func:`heat_scale`, which colours the rows as a set and
    will not put two different sizes in the same tone.
    """
    return _tone(_ramp_index(fraction, _ramp_len(depth)), depth)


def heat_scale(values: Sequence[float], depth: int = 8) -> List[str]:
    """Tones for a whole listing at once, in the order given.

    Colouring each row independently is what produced the original complaint:
    106 GiB, 65.8 GiB and 58.8 GiB all landed on one blue, which reads as "these
    three are the same" when the first is nearly twice the last. Any per-row rule
    has that failure mode, because a fixed number of bands cannot know which
    values happen to fall inside one of them.

    Colouring the set fixes it. Every row still starts at the tone its own
    magnitude earns; the rows are then walked largest-first, and one that is
    *measurably* smaller than the row above -- more than ``_SAME_SIZE`` apart --
    is forced at least one step cooler. Rows that really are the same size still
    come out the same colour, which is the property that makes the ramp mean
    anything at all. Ties in the tail bottom out at the coldest step rather than
    wrapping round, so a long ``-n 0`` listing ends in flat blue instead of
    starting over in amber.
    """
    n = _ramp_len(depth)
    if not values:
        return []
    peak = max(values)
    if peak <= 0:
        return [_tone(0, depth)] * len(values)

    index = [0] * len(values)
    previous = None  # type: Optional[Tuple[int, float]]
    for at in sorted(range(len(values)), key=lambda i: values[i], reverse=True):
        value = values[at]
        i = _ramp_index(value / float(peak), n)
        if previous is not None:
            prev_i, prev_value = previous
            if value >= prev_value * (1.0 - _SAME_SIZE):
                i = prev_i
            elif i >= prev_i:
                i = max(0, prev_i - 1)
        index[at] = i
        previous = (i, value)
    return [_tone(i, depth) for i in index]


# Bar fill is knocked back from the tone the row's text carries. A bar is a slab
# and text is a line: the colour that reads as "bright" in a number reads as
# shouting across eighteen filled cells, and ten shouting bars are a wall.
# Terminals have no alpha channel, so the wash is a genuinely darker colour of
# the same hue -- which is what compositing that hue at ~60% over a dark
# background would have produced anyway.
_WASH_256 = dict(RAMP_256)
# The gauge tones, which are not on the ramp: dark red, dark amber, dark green.
_WASH_SEMANTIC = {"red": 124, "yellow": 136, "green": 28}

# The empty part of a bar. It is a reference mark for "100% of the tree", not
# content, so in 256 colours it drops to a near-background grey instead of the
# `dim` grey that ordinary de-emphasised *text* uses.
_TRACK_256 = "c256:238"

# Secondary *content*: a real measurement that is not what the listing was
# ranked by. It must stay readable, which SGR `dim` does not reliably manage --
# on a good many terminals faint grey on dark grey is close to invisible, and a
# whole column of file counts rendered that way reads as decoration rather than
# data. Where 256 colours are available this is a mid grey that is plainly
# legible and still visibly quieter than a ramp tone.
_MUTED_256 = "c256:247"


# Full block / light shade, plus the seven partial blocks. The partials give a
# bar eight times the horizontal resolution of its cell count, which is the
# difference between a bar that looks measured and one that looks rounded off.
_BAR_FULL, _BAR_EMPTY = "█", "░"
_BAR_PARTIALS = ("", "▏", "▎", "▍", "▌", "▋", "▊", "▉")
_BAR_FULL_ASCII, _BAR_EMPTY_ASCII = "#", "-"
# The remainder row's fill: medium shade, so it reads as "many things" against
# the solid slab of a single directory. Partials are dropped with it -- a
# sub-cell tail on a hatched bar is invisible and only costs alignment.
_BAR_HATCH, _BAR_HATCH_ASCII = "▒", ":"

# Every non-ASCII character this module can emit, in one string, so the
# capability probe in `_supports_unicode` tests exactly what will be printed. A
# probe that checked a subset would clear a stream that then crashed on the
# glyph it had not been asked about -- which is how U+00B7 escaped the em-dash
# fix. `_SPIN` is appended where it is defined, below.
_GLYPHS = _BAR_FULL + _BAR_EMPTY + "".join(_BAR_PARTIALS) + _BAR_HATCH + "—·─"


class Style:
    """Resolved presentation settings for one run."""

    def __init__(self, color: bool, unicode_ok: bool, width: int, depth: int = 8) -> None:
        self.color = color
        self.unicode = unicode_ok
        self.width = width
        self.depth = depth  # advertised colour count: 8 or 256

    def paint(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        parts = []
        for name in styles:
            if name.startswith("c256:"):
                parts.append("\033[38;5;{}m".format(name[5:]))
            elif name.startswith("rgb:"):
                parts.append("\033[38;2;{}m".format(name[4:].replace(",", ";")))
            else:
                parts.append(_CODES.get(name, ""))
        prefix = "".join(parts)
        return prefix + text + _RESET if prefix else text

    def heat(self, fraction: Optional[float]) -> str:
        return heat(fraction, self.depth)

    def heat_scale(self, values: Sequence[float]) -> List[str]:
        return heat_scale(values, self.depth)

    def translucent(self, tone: str) -> Tuple[str, ...]:
        """The bar-fill twin of a text tone.

        Only 256-colour terminals get one. Eight colours have no room for a
        second, dimmer copy of every ramp step -- the SGR faint attribute would
        collapse ``bold_blue`` onto ``blue`` on the terminals that implement it
        as "not bold" -- and a ramp whose steps blur into each other is the
        problem this file exists to avoid. There the bar simply keeps the row's
        tone.
        """
        if self.depth < 256:
            return (tone,)
        if tone.startswith("c256:"):
            twin = _WASH_256.get(int(tone[5:]))
        else:
            twin = _WASH_SEMANTIC.get(tone)
        return ("c256:{}".format(twin),) if twin else (tone,)

    @property
    def track(self) -> str:
        return _TRACK_256 if self.depth >= 256 else "dim"

    @property
    def muted(self) -> str:
        """A real number that is not the one this listing was ranked by.

        Distinct from ``dim``, which means "context, not content" -- a snapshot
        age, a caveat, a hint. A file count is content even when bytes are what
        the rows were sorted on, and painting the two the same grey is what made
        the count column read as furniture.
        """
        return _MUTED_256 if self.depth >= 256 else ""

    @property
    def bar_chars(self):
        if self.unicode:
            return _BAR_FULL, _BAR_EMPTY
        return _BAR_FULL_ASCII, _BAR_EMPTY_ASCII

    @property
    def partials(self):
        return _BAR_PARTIALS if self.unicode else ("",)


def _supports_unicode(stream) -> bool:
    """Can this stream actually encode the glyph set?

    **The stream's own encoding is decisive, and it is the only thing that is.**
    Falling back to ``LC_ALL``/``LANG`` when the encoding was not UTF meant the
    environment could promise what the stream could not deliver, and every such
    disagreement ends in a ``UnicodeEncodeError`` traceback mid-report:

    * ``LC_ALL=C.UTF-8`` on Python 3.6 -- the interpreter this package advertises
      as its floor -- leaves ``sys.stdout.encoding`` at ``ANSI_X3.4-1968`` while
      the environment says utf, so the separator ``U+00B7`` crashed.
    * ``PYTHONIOENCODING=ascii`` does the same on any version.

    Asking the stream directly, with an encode probe as the arbiter, cannot
    disagree with itself. A stream that reports no encoding at all (a plain
    ``StringIO`` under test) accepts anything, so it is treated as capable.
    """
    enc = getattr(stream, "encoding", None)
    if not enc:
        return True
    try:
        _GLYPHS.encode(enc)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


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
        depth=color_depth(),
    )


def color_depth() -> int:
    """8 or 256, from what the terminal advertises.

    Deliberately conservative: emitting 256-colour codes to a terminal that
    cannot render them produces visible garbage, and the 8-colour ramp is fine.
    """
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return 256
    term = os.environ.get("TERM", "")
    if "256color" in term or "direct" in term or term in ("xterm-kitty", "alacritty"):
        return 256
    return 8


def terminal_width(default: int = 100) -> int:
    try:
        import shutil

        w = shutil.get_terminal_size((default, 24)).columns
    except Exception:
        w = default
    # Narrow enough to paste, wide enough for the table.
    return max(60, min(w, 160))


def bar(
    fraction: float,
    width: int,
    style: Style,
    accent: str = "cyan",
    min_tick: bool = True,
    track: bool = True,
    hatched: bool = False,
) -> str:
    """A proportional bar. ``fraction`` is clamped to [0, 1].

    The full width is always the whole of whatever the caller is measuring
    against -- the tree total, or a quota limit -- so a bar can be read as a
    fraction on its own, without comparing it to its neighbours.

    ``track`` draws the unfilled remainder as shading, so the bar occupies a
    visible box of a fixed width rather than trailing off into blank space.

    It is on everywhere. The rankings used to leave it off, on the argument that
    ten boxes of grey behind ten short bars are heavier on the page than the
    data. That was wrong about what the blank space costs: an 18-column channel
    was reserved on every row and most of it read as nothing at all, so the eye
    had no edge to measure a short bar against and the table looked as though it
    had a hole in it. A track is what makes "4.0%" and "14.1%" comparable at a
    glance -- the reader sees the same box each time and the fill within it. In
    256 colours the track drops to a near-background grey (:data:`_TRACK_256`),
    which is quiet enough that it frames the bar without competing with it.

    ``min_tick`` renders any non-zero share as at least one cell, so a small but
    real entry is not mistaken for nothing. Turn it off where the bar is a gauge
    against a limit rather than a comparison between rows: showing a filled cell
    for 0.04% of a quota reads as "some usage" when the honest answer is "none
    worth seeing".

    ``hatched`` fills with shading instead of solid blocks. It marks a bar that
    measures *several* things at once -- the everything-else remainder row -- so
    that a quarter of the tree collapsed into one line is still drawn at its
    real length, but cannot be misread as a single directory that size.
    """
    if width <= 0:
        return ""
    full_ch, empty_ch = style.bar_chars
    if hatched:
        full_ch = _BAR_HATCH if style.unicode else _BAR_HATCH_ASCII
    partials = style.partials if not hatched else ("",)  # type: Tuple[str, ...]
    f = 0.0 if fraction < 0 else (1.0 if fraction > 1 else fraction)

    exact = f * width
    filled = int(exact)
    remainder = exact - filled
    tail = ""
    if len(partials) > 1 and filled < width:
        step = int(remainder * len(partials))
        tail = partials[step]
    elif filled < width and int(round(remainder)):
        filled += 1

    if min_tick and filled == 0 and not tail and f > 0:
        tail = partials[1] if len(partials) > 1 else full_ch
    used = filled + (1 if tail else 0)
    rest = max(0, width - used)
    return style.paint(full_ch * filled + tail, *style.translucent(accent)) + (
        style.paint(empty_ch * rest, style.track) if track else " " * rest
    )


def sep(style: Style) -> str:
    """Separator between header facts. Degrades where UTF-8 is unavailable."""
    return "\u00b7" if style.unicode else "|"


def dash(style: Style) -> str:
    return "\u2014" if style.unicode else "--"


# Escape sequences occupy no columns. Any width arithmetic done on a painted
# string is wrong by the length of its escapes -- which is how a bar drawn from
# `len()` ends up ragged -- so every measurement below goes through
# `visible_width`, never `len`.
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")

# What the frame costs a line: "| " on the left and " |" on the right.
BOX_CHROME = 4

# A frame narrower than this is not worth drawing around anything.
_MIN_INNER = 20


def visible_width(text: str) -> int:
    """Columns ``text`` occupies on a terminal.

    Three things make this different from ``len``: SGR escapes are invisible,
    combining marks attach to the previous cell rather than taking their own, and
    East Asian wide characters take two. The last matters here because a path is
    user data -- a directory named in Chinese or Japanese is perfectly ordinary,
    and measuring it with ``len`` puts the right-hand border one column short per
    character.
    """
    plain = _ANSI_RE.sub("", text)
    width = 0
    for ch in plain:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


# The frame's gradient, as anchor colours it is interpolated between. Every one
# of them is a *light* colour, and that is the whole point.
#
# The first version swept light-to-deep so the highlight landed at the top-left
# like a gloss on a card. On a dark terminal that puts the darkest end of the ramp
# at the bottom-right, where it simply disappears -- the bottom border read as
# barely there while the top read as fine, which is exactly how it was reported.
# A gradient whose range crosses out of the visible band is not a gradient with a
# subtle end, it is a gradient that is broken for half its length.
#
# So the sweep now moves in *hue* and stays put in brightness: light cyan through
# aqua and periwinkle to light violet. Every step is legible against black, none
# is legible as data (nothing in the table is ever this pale), and the border has
# the same weight at the bottom as at the top.
_FRAME_ANCHORS = ((140, 233, 255), (94, 234, 212), (129, 199, 255), (167, 160, 255))

# The same sweep on the xterm-256 cube, and held to the same rule: nothing below
# the bright band. The deep-blue tail this ramp used to end on (27, 21, 20, 19) is
# what made the bottom border vanish.
_FRAME_RAMP_256 = (123, 87, 80, 74, 75, 111, 147, 141, 177, 183)

# Eight colours, which is what `TERM=screen`, `TERM=xterm` and most tmux defaults
# advertise -- so this is the ramp most sessions actually get, and it is the one
# with no room to be clever.
#
# **Bright variants only.** Plain `blue` at eight colours is a murky navy that
# disappears against a dark background, and because the sweep runs diagonally it
# was landing on the bottom border. Two bold tones read as a deliberate two-tone
# frame; three tones where one of them is invisible reads as a rendering fault.
_FRAME_RAMP_8 = ("bold_cyan", "bold_blue")

# How many steps to quantise the truecolor sweep into. Fine enough that the bands
# are invisible, coarse enough that runs of equal colour still group into one
# escape sequence instead of one per column.
_FRAME_TRUECOLOR_STEPS = 24


def truecolor() -> bool:
    """Does the terminal advertise 24-bit colour?

    Only ``COLORTERM``, which is the one signal that means it. ``TERM`` says
    ``screen`` or ``xterm-256color`` on plenty of terminals that do support it and
    on plenty that do not, and emitting 24-bit codes at one that cannot renders
    them as visible garbage.
    """
    return os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")


def _lerp_anchors(position: float) -> str:
    """A tone from the anchor list at ``position`` in 0..1, linearly blended."""
    span = len(_FRAME_ANCHORS) - 1
    scaled = max(0.0, min(1.0, position)) * span
    low = min(span, int(scaled))
    high = min(span, low + 1)
    fraction = scaled - low
    channels = [
        int(
            round(
                _FRAME_ANCHORS[low][i]
                + (_FRAME_ANCHORS[high][i] - _FRAME_ANCHORS[low][i]) * fraction
            )
        )
        for i in range(3)
    ]
    return "rgb:{},{},{}".format(*channels)


def frame_ramp(style: Style) -> List[str]:
    """Tones for the frame gradient, lightest first, or empty when colour is off."""
    if not style.color:
        return []
    if style.depth >= 256 and truecolor():
        steps = _FRAME_TRUECOLOR_STEPS
        return [_lerp_anchors(i / float(steps - 1)) for i in range(steps)]
    if style.depth >= 256:
        return ["c256:{}".format(code) for code in _FRAME_RAMP_256]
    return list(_FRAME_RAMP_8)


def box(lines: List[str], style: Style, width: Optional[int] = None) -> List[str]:
    """Wrap already-rendered lines in a single frame.

    Rounded corners and a **diagonal colour sweep** around the perimeter: hue
    advances with ``x + y``, so the top-left corner is the lightest point and the
    bottom-right the deepest, the way a highlight falls across a glossy surface.
    Painting it per character would cost an escape sequence per column, so runs of
    equal tone are emitted as one -- about ten escapes per border rather than
    eighty.

    The border carries no label. A version string in the top edge was tried and
    read as packaging rather than measurement -- it is the first thing the eye
    lands on and the last thing anyone needs from a disk-usage report.

    ``width`` is the **total** frame width including both borders, defaulting to
    the terminal. It is a parameter rather than a read of ``style.width`` because
    the caller has already reduced that for the renderers (see
    ``cli._box_style``): consulting it here subtracted the chrome a second time and
    left the frame four columns narrower than the space it had. Both halves now
    derive from :func:`terminal_width`, so they cannot disagree.

    **The frame always closes.** Every content line sits between two borders, on
    every row, at every terminal width. That is the one property a frame has to
    have: a border that stops halfway down the report is not a frame, it is a
    rendering bug that looks like one.

    Closure is what decides the other two questions. Because a line too wide for
    the frame is *wrapped* rather than allowed to run past it, the frame can be
    sized to the terminal without anything overrunning -- so it never gets
    soft-wrapped by the terminal itself, which is the other way a border comes
    apart. And because wrapping preserves the whole line, nothing is truncated: the
    path column is the answer being asked for, and dropping its tail to make a
    border meet would be trading the measurement for the chrome.
    """
    if style.unicode:
        top_l, top_r, bot_l, bot_r, horiz, vert = (
            "\u256d",
            "\u256e",
            "\u2570",
            "\u256f",
            "\u2500",
            "\u2502",
        )
    else:
        top_l = top_r = bot_l = bot_r = "+"
        horiz, vert = "-", "|"

    # Split on embedded newlines first. A caller handing one string that contains
    # "\n" means two *display* lines, and measuring it as one produced a single
    # pair of borders wrapped around both: the first half lost its right border and
    # the second lost its left. That is a frame that does not close, which is the
    # one thing this function has to get right, so it is fixed here rather than
    # only at the caller -- any future caller can make the same mistake.
    body = []  # type: List[str]
    for line in lines:
        body.extend(line.split("\n") if "\n" in line else [line])
    # The frame supplies the separation that leading and trailing blank lines
    # were there to provide.
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    if not body:
        return []

    total = terminal_width() if width is None else width
    inner = max(_MIN_INNER, total - BOX_CHROME)
    widest = max(visible_width(line) for line in body)
    if widest < inner:
        # Nothing needs the full width, so hug the content instead of floating a
        # half-empty frame out to the terminal edge.
        inner = max(widest, _MIN_INNER)

    # Wrap first, then measure: the row count is what the vertical half of the
    # gradient is computed from, and wrapping changes it.
    rows = []  # type: List[str]
    for line in body:
        rows.extend(_wrap_ansi(line, inner))

    total_w = inner + BOX_CHROME
    total_h = len(rows) + 2
    ramp = frame_ramp(style)

    def tone_at(x: int, y: int) -> str:
        """Hue for one frame cell, sweeping diagonally from top-left."""
        if not ramp:
            return style.track
        across = x / float(total_w - 1) if total_w > 1 else 0.0
        down = y / float(total_h - 1) if total_h > 1 else 0.0
        position = 0.5 * across + 0.5 * down
        return ramp[min(len(ramp) - 1, max(0, int(position * len(ramp))))]

    def sweep(text: str, y: int) -> str:
        """Paint a horizontal border run, grouping equal tones into one escape."""
        parts = []  # type: List[str]
        buffered = []  # type: List[str]
        current = None  # type: Optional[str]
        for x, char in enumerate(text):
            tone = tone_at(x, y)
            if current is not None and tone != current:
                parts.append(style.paint("".join(buffered), current))
                buffered = []
            current = tone
            buffered.append(char)
        if buffered and current is not None:
            parts.append(style.paint("".join(buffered), current))
        return "".join(parts)

    out = [sweep(top_l + horiz * (inner + 2) + top_r, 0)]
    for row, line in enumerate(rows):
        y = row + 1
        left = style.paint(vert, tone_at(0, y))
        right = style.paint(vert, tone_at(total_w - 1, y))
        out.append(left + " " + line + " " * (inner - visible_width(line)) + " " + right)
    out.append(sweep(bot_l + horiz * (inner + 2) + bot_r, total_h - 1))
    return out


def _wrap_ansi(text: str, width: int) -> List[str]:
    """Split ``text`` into runs of at most ``width`` visible columns.

    Colour makes this more than ``textwrap``. An SGR run has to be closed at the
    end of each piece and reopened at the start of the next, or the colour of a
    wrapped row bleeds across the border and down the rest of the report; the
    break has to be measured in visible columns, which is not where ``len`` would
    put it; and breaking mid-escape would emit a partial sequence and print raw
    bytes.

    **Breaks after a path separator, or at a space, before it breaks mid-token.**
    A first version broke wherever the column ran out, which split
    ``.../test_a_directory_named0/quota`` into ``...quot`` and ``a`` -- a path you
    can neither read nor grep for, which is most of what a path is for. Slashes are
    where a path is *meant* to come apart, so they are tried first; a space is the
    fallback for prose; and a token with neither still has to fit, so it is cut.
    """
    if visible_width(text) <= width or width <= 0:
        return [text]

    pieces = []  # type: List[str]
    buffered = []  # type: List[str]
    active = ""  # the SGR prefix in force, reopened on each continuation
    cut = -1  # index in `buffered` to break at, exclusive
    drop = 0  # buffered items to discard at the break (the space itself)
    index = 0

    def emit(upto: int) -> None:
        pieces.append("".join(buffered[:upto]) + (_RESET if active else ""))

    while index < len(text):
        match = _ANSI_RE.match(text, index)
        if match:
            sequence = match.group(0)
            buffered.append(sequence)
            active = "" if sequence == _RESET else active + sequence
            index = match.end()
            continue
        char = text[index]
        if unicodedata.combining(char):
            char_w = 0
        else:
            char_w = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if visible_width("".join(buffered)) + char_w > width and buffered:
            if cut > 0:
                emit(cut)
                carry = buffered[cut + drop :]
            else:
                emit(len(buffered))
                carry = []
            buffered = ([active] if active else []) + carry
            cut, drop = -1, 0
        if char == " ":
            # Break before the space and discard it: a line must not start with one.
            cut, drop = len(buffered), 1
        buffered.append(char)
        if char == os.sep:
            # Break after the separator and keep it, so the path reads as a path.
            cut, drop = len(buffered), 0
        index += 1
    if buffered:
        emit(len(buffered))
    return [piece for piece in pieces if _ANSI_RE.sub("", piece).strip()] or [""]


def truncate(text: str, width: int) -> str:
    """Shorten to ``width``, keeping the tail, which is the distinguishing part.

    ``.../checkpoints/step-4000`` says more than ``experiments/run-a/che...``.
    """
    if width <= 0 or len(text) <= width:
        return text
    if width <= 3:
        return text[-width:]
    return "..." + text[-(width - 3) :]


def heading(text: str, style: Style) -> str:
    return style.paint(text, "bold")


def warn(text: str, style: Style) -> str:
    return style.paint("! " + text, "yellow")


def alarm(text: str, style: Style) -> str:
    return style.paint("! " + text, "red")


# --------------------------------------------------------------------------
# --help
# --------------------------------------------------------------------------
#
# argparse lays its help out in columns using ``len()``, so colouring the strings
# it is given throws every column off by the width of the escape sequences.
# Colour is applied to the finished block instead: escapes occupy no columns, so
# the layout argparse already computed survives untouched.
#
# Four roles, and deliberately no more -- a help screen wearing a dozen colours
# is harder to read than one wearing none:
#
#     bold_cyan   what you type          flags, and the flags inside prose
#     yellow      what you substitute    PATH, N, SECONDS, WHEN
#     bold        structure              section headings, the program name
#     dim         context                defaults, the example explanations

_HELP_HEADING = re.compile(r"^([A-Za-z][A-Za-z ]*:)\s*$")
_HELP_SPLIT = re.compile(r"^(\s+)(\S.*?)(\s\s+|$)(.*)$")

# One alternation rather than one pass per role, because `re.sub` never overlaps
# its own matches: painting flags and then backticked spans separately nested a
# reset inside `du -s --block-size=1` and dropped the tail of it back to plain.
# Leftmost-first also gives the precedence that is wanted -- a flag inside a
# quoted command belongs to the command.
_HELP_SPANS = re.compile(
    r"(?P<default>\(default:[^)]*\))"
    r"|(?P<code>`[^`]+`)"
    r"|(?P<flag>(?<![\w-])--[a-z][\w-]+)"
    r"|(?P<caps>\b[A-Z][A-Z_]{3,}\b)"
)
_HELP_SPAN_TONES = {"default": "dim", "code": "cyan", "flag": "cyan", "caps": "yellow"}
_HELP_METAVAR = re.compile(r"^[A-Z][A-Z_]*$")


def _is_invocation(line: str) -> bool:
    """argparse indents an invocation by 2 and its help text by about 24.

    The indent alone tells them apart, which matters for a wrapped help line
    that happens to begin with a flag.
    """
    return bool(line.strip()) and len(line) - len(line.lstrip()) == 2


def _metavars(text: str) -> frozenset:
    """The placeholders this parser actually declares.

    Yellow has to mean "you substitute this", so it is spent only on words the
    invocation column proves are placeholders. A blanket all-caps rule painted
    ``GPFS`` and ``ASCII`` -- prose nouns -- the same colour as ``SECONDS``,
    which drains the colour of its meaning.
    """
    found = set()
    for line in text.split("\n"):
        m = _HELP_SPLIT.match(line) if _is_invocation(line) else None
        if not m:
            continue
        for token in re.split(r"[\s,]+", m.group(2)):
            if token and not token.startswith("-") and _HELP_METAVAR.match(token):
                found.add(token)
    return frozenset(found)


def colorize_help(text: str, style: Style) -> str:
    """Paint an already-formatted argparse help block."""
    if not style.color:
        return text
    names = _metavars(text)
    out = []  # type: List[str]
    section = ""
    for line in text.split("\n"):
        heading = _HELP_HEADING.match(line)
        if heading:
            section = heading.group(1)[:-1]
            out.append(style.paint(heading.group(1), "bold"))
        elif line.startswith("usage:"):
            out.append(_paint_usage(line, style))
        elif not line.strip():
            out.append(line)
        elif section == "examples":
            # The closing note sits at column 0, under the same heading.
            painter = _paint_prose if line[0].strip() else _paint_example
            out.append(painter(line, style, names))
        elif section and _is_invocation(line):
            out.append(_paint_invocation_line(line, style, names))
        else:
            out.append(_paint_prose(line, style, names))
    return "\n".join(out)


def _paint_words(text: str, style: Style, first: Optional[str] = None) -> str:
    """A shell word run: flags cyan, everything else a substitutable value."""
    out = []
    for i, tok in enumerate(re.split(r"(\s+)", text)):
        if not tok or not tok.strip():
            out.append(tok)
        elif i == 0 and first:
            out.append(style.paint(tok, first))
        elif tok.startswith("-"):
            out.append(style.paint(tok, "bold_cyan"))
        else:
            out.append(style.paint(tok, "yellow"))
    return "".join(out)


def _paint_usage(line: str, style: Style) -> str:
    label, _, rest = line.partition(":")
    return style.paint(label + ":", "dim") + _paint_words(rest, style, first="bold")


def _paint_example(line: str, style: Style, names: frozenset = frozenset()) -> str:
    m = _HELP_SPLIT.match(line)
    if not m:
        return _paint_prose(line, style, names)
    pad, command, gap, note = m.groups()
    return pad + _paint_words(command, style, first="bold") + gap + style.paint(note, "dim")


def _paint_invocation_line(line: str, style: Style, names: frozenset = frozenset()) -> str:
    m = _HELP_SPLIT.match(line)
    if not m:
        return line
    pad, invocation, gap, rest = m.groups()
    parts = []
    for tok in re.split(r"([\s,]+)", invocation):
        if not tok:
            continue
        if not tok.strip():
            parts.append(style.paint(tok, "dim") if "," in tok else tok)
        elif tok.startswith("-"):
            parts.append(style.paint(tok, "bold_cyan"))
        else:
            parts.append(style.paint(tok, "yellow"))  # metavar
    return pad + "".join(parts) + gap + _paint_prose(rest, style, names)


def _paint_prose(text: str, style: Style, names: frozenset = frozenset()) -> str:
    if not text:
        return text

    def paint(match: "re.Match") -> str:
        word = match.group(0)
        # An all-caps word earns yellow by being a declared placeholder, or by
        # having the shape of an environment variable (NO_COLOR).
        if match.lastgroup == "caps" and word not in names and "_" not in word:
            return word
        return style.paint(word, _HELP_SPAN_TONES[match.lastgroup or "flag"])

    return _HELP_SPANS.sub(paint, text)


# Spinner frames. Braille reads as smooth motion at 100 ms; the ASCII fallback
# is for the same terminals that get ASCII bars.
_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_SPIN_ASCII = "|/-\\"
# Complete the probe set now that the spinner frames exist. Braille is outside
# latin-1, so a stream that can encode the bars may still not encode these.
_GLYPHS += _SPIN

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
