#!/usr/bin/env python3
"""Render the README hero GIF (``assets/demo.gif``).

Runs the **real** ``slurmdisk`` CLI, captures its real ANSI output, and paints it
into an animated GIF frame by frame. There is no recording of a hand-typed
session anywhere in here: every character in the GIF below the prompt came out
of the tool.

Why not vhs/asciinema, which the sibling projects use? Neither ``vhs``, ``ttyd``
nor ``ffmpeg`` exists on an HPC login node, and this package's whole argument is
that it runs where nothing is installed. So the terminal is emulated here: a
small SGR parser turns the CLI's escape codes into coloured cells, Pillow draws
them, and the frames are assembled with per-frame durations so a two-second
pause costs one frame instead of forty.

Usage (from the repo root)::

    pip install pillow
    python assets/render_demo.py                 # scenes 1-4, live
    python assets/render_demo.py --settle FILE   # also replay a settling capture

``--settle`` takes a text file holding the output of a ``-a --settle-wait``
run over a freshly written tree. That scene needs a GPFS filesystem and a
minute of drift to exist at all, so it is captured once rather than re-measured
on every render; ``assets/capture_settle.sh`` is what captures it.

Overridable with environment variables: ``SLURMDISK_DEMO_OUTPUT``,
``SLURMDISK_DEMO_FONT``, ``SLURMDISK_DEMO_FONT_SIZE``, ``SLURMDISK_DEMO_COLS``.
"""

import argparse
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src")

OUTPUT = os.environ.get("SLURMDISK_DEMO_OUTPUT", os.path.join(REPO, "assets", "demo.gif"))
FONT_SIZE = int(os.environ.get("SLURMDISK_DEMO_FONT_SIZE", "17"))
COLS = int(os.environ.get("SLURMDISK_DEMO_COLS", "96"))

_FONT_CANDIDATES = (
    os.environ.get("SLURMDISK_DEMO_FONT", ""),
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "/Library/Fonts/DejaVuSansMono.ttf",
)

# ---------------------------------------------------------------------------
# Theme. A warm near-black surface, and a base-16 ramp chosen so the walker's
# blue->amber heat ramp reads as one continuous gradient rather than as eight
# unrelated hues.
# ---------------------------------------------------------------------------
BG = (13, 17, 23)
CHROME = (22, 27, 34)
FG = (201, 209, 217)
PROMPT = (126, 231, 135)
CURSOR = (240, 246, 252)

BASE16 = {
    30: (72, 79, 88),
    31: (255, 123, 114),
    32: (86, 211, 100),
    33: (232, 176, 71),
    34: (88, 166, 255),
    35: (188, 140, 255),
    36: (86, 202, 219),
    37: (201, 209, 217),
}
BRIGHT16 = {
    30: (110, 118, 129),
    31: (255, 166, 158),
    32: (126, 231, 135),
    33: (240, 200, 110),
    34: (121, 192, 255),
    35: (210, 168, 255),
    36: (118, 224, 240),
    37: (240, 246, 252),
}

_CUBE = (0, 95, 135, 175, 215, 255)


def xterm256(n):
    """xterm-256 index -> RGB."""
    if n < 8:
        return BASE16[30 + n]
    if n < 16:
        return BRIGHT16[30 + (n - 8)]
    if n < 232:
        n -= 16
        return (_CUBE[n // 36], _CUBE[(n // 6) % 6], _CUBE[n % 6])
    v = 8 + (n - 232) * 10
    return (v, v, v)


def blend(fg, bg, alpha):
    return tuple(int(b + (f - b) * alpha) for f, b in zip(fg, bg))


# ---------------------------------------------------------------------------
# SGR parsing.  The CLI only ever emits `ESC[...m`, and `ui.Style.paint` never
# nests, so a flat scanner is enough.
# ---------------------------------------------------------------------------
_SGR = re.compile(r"\033\[([0-9;]*)m")


class Pen:
    __slots__ = ("color", "bold", "dim")

    def __init__(self):
        self.reset()

    def reset(self):
        self.color = None
        self.bold = False
        self.dim = False

    def rgb(self):
        base = self.color or (BRIGHT16[37] if self.bold else FG)
        if self.dim:
            return blend(base, BG, 0.45)
        return base

    def apply(self, params):
        i = 0
        while i < len(params):
            p = params[i]
            if p in (0, -1):
                self.reset()
            elif p == 1:
                self.bold = True
            elif p == 2:
                self.dim = True
            elif p == 22:
                self.bold = self.dim = False
            elif 30 <= p <= 37:
                self.color = BRIGHT16[p] if self.bold else BASE16[p]
            elif p == 39:
                self.color = None
            elif p == 38 and i + 2 < len(params) and params[i + 1] == 5:
                self.color = xterm256(params[i + 2])
                i += 2
            i += 1


def to_cells(text):
    """ANSI text -> list of lines, each a list of ``(char, rgb)``."""
    pen = Pen()
    lines = [[]]
    pos = 0
    for m in _SGR.finditer(text):
        for ch in text[pos : m.start()]:
            if ch == "\n":
                lines.append([])
            else:
                lines[-1].append((ch, pen.rgb()))
        params = [int(p) if p else 0 for p in m.group(1).split(";")]
        pen.apply(params or [0])
        pos = m.end()
    for ch in text[pos:]:
        if ch == "\n":
            lines.append([])
        else:
            lines[-1].append((ch, pen.rgb()))
    return lines


# ---------------------------------------------------------------------------
# Screen: a scrolling list of already-painted lines plus the line being typed.
# ---------------------------------------------------------------------------
class Screen:
    def __init__(self, rows):
        self.rows = rows
        self.lines = []

    def add(self, cells):
        self.lines.append(cells)

    def visible(self):
        return self.lines[-self.rows :]


def prompt_cells(typed, cursor=True):
    cells = [("$", PROMPT), (" ", FG)]
    cells += [(ch, FG) for ch in typed]
    if cursor:
        cells.append(("█", CURSOR))
    return cells


# ---------------------------------------------------------------------------
# Painting
# ---------------------------------------------------------------------------
PAD_X, PAD_Y = 22, 16
TITLEBAR = 34

# Block elements are drawn as rectangles, not as glyphs. DejaVu Sans Mono
# advances U+2588 by less than its own cell, so a run of full blocks paints with
# a hairline gap between every pair -- which turns the walker's bars, the one
# thing the eye reads first, into a dotted mess. Rectangles tile exactly.
#
# char -> (fraction of the cell filled from the left, opacity)
_BLOCKS = {
    "█": (1.0, 1.00),  # full block
    "▉": (7 / 8.0, 1.00),
    "▊": (6 / 8.0, 1.00),
    "▋": (5 / 8.0, 1.00),
    "▌": (4 / 8.0, 1.00),
    "▍": (3 / 8.0, 1.00),
    "▎": (2 / 8.0, 1.00),
    "▏": (1 / 8.0, 1.00),
    "▓": (1.0, 0.75),  # dark shade
    "▒": (1.0, 0.50),  # medium shade
    "░": (1.0, 0.28),  # light shade -- the empty half of every bar
}
_HLINE = "─"


def load_font():
    from PIL import ImageFont

    for path in _FONT_CANDIDATES:
        if path and os.path.exists(path):
            return ImageFont.truetype(path, FONT_SIZE)
    raise SystemExit("no monospace TTF found; set SLURMDISK_DEMO_FONT to a DejaVu Sans Mono path")


class Painter:
    def __init__(self, rows):
        from PIL import ImageDraw, ImageFont  # noqa: F401

        self.font = load_font()
        box = self.font.getbbox("M")
        self.cw = self.font.getlength("M")
        self.ch = int((box[3] - box[1]) * 1.62)
        self.rows = rows
        self.width = int(PAD_X * 2 + self.cw * COLS)
        self.height = TITLEBAR + PAD_Y * 2 + self.ch * rows
        self._chrome = self._make_chrome()

    def _make_chrome(self):
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (self.width, self.height), BG)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, self.width, TITLEBAR], fill=CHROME)
        for i, col in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
            cx = 20 + i * 20
            d.ellipse([cx - 6, TITLEBAR // 2 - 6, cx + 6, TITLEBAR // 2 + 6], fill=col)
        title = "slurmdisk — midway3 login node"
        d.text(
            ((self.width - self.font.getlength(title)) / 2, (TITLEBAR - self.ch) / 2 + 2),
            title,
            font=self.font,
            fill=blend(FG, CHROME, 0.55),
        )
        return img

    def paint(self, screen):
        from PIL import ImageDraw

        img = self._chrome.copy()
        d = ImageDraw.Draw(img)
        y = TITLEBAR + PAD_Y
        for line in screen.visible():
            for i, (ch, rgb) in enumerate(line):
                if ch == " ":
                    continue
                x0 = PAD_X + int(round(i * self.cw))
                x1 = PAD_X + int(round((i + 1) * self.cw))
                block = _BLOCKS.get(ch)
                if block is not None:
                    frac, alpha = block
                    d.rectangle(
                        [x0, y, x0 + max(1, int(round((x1 - x0) * frac))) - 1, y + self.ch - 1],
                        fill=blend(rgb, BG, alpha),
                    )
                elif ch == _HLINE:
                    mid = y + self.ch // 2
                    d.rectangle([x0, mid, x1 - 1, mid], fill=rgb)
                else:
                    d.text((x0, y), ch, font=self.font, fill=rgb)
            y += self.ch
        return img


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------
class Movie:
    """Frames with per-frame durations, so a long pause costs one frame."""

    def __init__(self, painter):
        self.painter = painter
        self.frames = []
        self.durations = []

    def shoot(self, screen, ms):
        self.frames.append(self.painter.paint(screen))
        self.durations.append(ms)

    def hold(self, ms):
        if self.frames:
            self.durations[-1] += ms

    def save(self, path):
        from PIL import Image

        base = self.frames[0]
        # One shared adaptive palette for the whole movie: per-frame palettes
        # make the heat ramp shimmer between frames that should look identical.
        strip = Image.new("RGB", (base.width, base.height * len(self.frames)))
        for i, f in enumerate(self.frames):
            strip.paste(f, (0, i * base.height))
        palette = strip.quantize(colors=128, method=Image.Quantize.MEDIANCUT)
        quantized = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in self.frames]
        quantized[0].save(
            path,
            save_all=True,
            append_images=quantized[1:],
            duration=self.durations,
            loop=0,
            optimize=True,
            disposal=2,
        )


TYPE_MS = 42
LINE_MS = 34


def play(movie, screen, command, output, settle_ms=900, read_ms=2600):
    """Type a command, reveal its output line by line, then pause to read."""
    for i in range(len(command) + 1):
        screen.lines.append(prompt_cells(command[:i]))
        movie.shoot(screen, TYPE_MS if i else 320)
        screen.lines.pop()
    screen.add(prompt_cells(command, cursor=False))
    movie.shoot(screen, settle_ms)

    lines = to_cells(output.rstrip("\n"))
    for line in lines:
        screen.add(line)
        movie.shoot(screen, LINE_MS)
    screen.add([])
    movie.hold(read_ms)


# ---------------------------------------------------------------------------
# Scenes: real commands, run for real.
# ---------------------------------------------------------------------------
def run(argv, env_extra=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["COLUMNS"] = str(COLS)
    env["TERM"] = "xterm-256color"
    env.update(env_extra or {})
    proc = subprocess.run(
        [sys.executable, "-m", "slurmdisk"] + argv + ["--color", "always", "--no-progress"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    return proc.stdout.decode("utf-8", "replace")


def deleted_fd_scene(tmpdir, megabytes=512):
    """Hold a real unlinked-but-open file open, then scan for it.

    This is the finding the whole ``-D`` mode exists for, and it is trivially
    reproducible anywhere: write a file, unlink it, keep the descriptor.
    """
    path = os.path.join(tmpdir, "ckpt-step-4000.bin")
    fh = open(path, "wb")
    chunk = b"\0" * (1 << 20)
    for _ in range(megabytes):
        fh.write(chunk)
    fh.flush()
    os.fsync(fh.fileno())
    os.unlink(path)  # the directory entry is gone; the blocks are not
    try:
        return run(["-D", tmpdir])
    finally:
        fh.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tree", default=os.path.expanduser("~"), help="tree to walk in scene 1")
    ap.add_argument("--scratch", default=None, help="writable dir for the deleted-fd scene")
    ap.add_argument("--settle", default=None, help="captured `-a --settle-wait` transcript")
    ap.add_argument("--quota-path", default="/project", help="which quota rows to show")
    args = ap.parse_args()

    scratch = args.scratch or os.environ.get("TMPDIR") or "/tmp"

    scenes = [
        ("sd ~", run([args.tree, "-n", "8"])),
        ("sd ~ -i", run([args.tree, "-i", "-n", "6"])),
        ("sd -Q " + args.quota_path, run(["-Q", args.quota_path])),
        ("sd -D", deleted_fd_scene(scratch)),
    ]
    if args.settle:
        with open(args.settle) as fh:
            transcript = fh.read()
        # capture_settle.sh writes the exact command as a leading `# ` line, so
        # the label in the GIF cannot drift from the flags that produced it.
        label = "sd $SCRATCH/run-217 -a --settle-wait 60"
        if transcript.startswith("# "):
            head, _, transcript = transcript.partition("\n")
            label = head[2:].strip()
        scenes.append((label, transcript))

    rows = max(len(to_cells(out)) + 3 for _, out in scenes) + 1
    painter = Painter(rows)
    screen = Screen(rows)
    movie = Movie(painter)
    print("canvas {}x{}, {} rows".format(painter.width, painter.height, rows))

    for command, output in scenes:
        n = len(to_cells(output))
        print("  scene: {:<44} {:>3} lines".format(command, n))
        play(movie, screen, command, output)
        screen.lines = []

    screen.add(prompt_cells(""))
    movie.shoot(screen, 1500)
    movie.save(OUTPUT)
    size = os.path.getsize(OUTPUT)
    total = sum(movie.durations) / 1000.0
    print(
        "wrote {} ({:.1f} MB, {} frames, {:.0f}s)".format(
            OUTPUT, size / 1e6, len(movie.frames), total
        )
    )


if __name__ == "__main__":
    main()
