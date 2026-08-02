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
