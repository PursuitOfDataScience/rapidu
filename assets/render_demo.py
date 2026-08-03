#!/usr/bin/env python3
"""Render the README hero GIF (``assets/demo.gif``).

Runs the **real** ``rapidu`` CLI, captures its real ANSI output, and paints it
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

Overridable with environment variables: ``RAPIDU_DEMO_OUTPUT``,
``RAPIDU_DEMO_FONT``, ``RAPIDU_DEMO_FONT_SIZE``, ``RAPIDU_DEMO_COLS``.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src")

OUTPUT = os.environ.get("RAPIDU_DEMO_OUTPUT", os.path.join(REPO, "assets", "demo.gif"))
FONT_SIZE = int(os.environ.get("RAPIDU_DEMO_FONT_SIZE", "17"))
COLS = int(os.environ.get("RAPIDU_DEMO_COLS", "96"))

_FONT_CANDIDATES = (
    os.environ.get("RAPIDU_DEMO_FONT", ""),
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
            elif p == 38 and i + 4 < len(params) and params[i + 1] == 2:
                # 24-bit. The report's frame emits this where COLORTERM says the
                # terminal can take it; without this branch the parameters fell
                # through and painted the border in whatever colour was current.
                self.color = (params[i + 2], params[i + 3], params[i + 4])
                i += 4
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
    raise SystemExit("no monospace TTF found; set RAPIDU_DEMO_FONT to a DejaVu Sans Mono path")


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
        title = "rapidu — midway3 login node"
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
    # The *inner* width of the frame, not the full canvas. The CLI is run with
    # --no-box (see `frame_scene`), so it lays its columns out against the whole
    # terminal -- and if that is wider than the space inside the frame, `box` has
    # to re-wrap the prose when it goes in. A re-wrapped continuation starts at the
    # margin instead of under the paragraph it continues, which reads as text
    # falling out of the box even though the border is intact. Laying out against
    # the inner width means nothing needs re-wrapping and every indent survives.
    env["COLUMNS"] = str(COLS - _chrome_cols())
    env["TERM"] = "xterm-256color"
    # Pinned, not inherited. The frame's gradient is 24-bit when COLORTERM
    # advertises it and 256-colour otherwise, so leaving this to the ambient
    # environment made the GIF depend on whose terminal rendered it.
    env["COLORTERM"] = "truecolor"
    env.update(env_extra or {})
    proc = subprocess.run(
        [sys.executable, "-m", "rapidu"]
        + argv
        + ["--color", "always", "--no-progress", "--no-box"],
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


# ---------------------------------------------------------------------------
# The demo tree, and why it is synthetic.
#
# This renderer used to walk `~` and print the live `quota` table. Both went
# into a GIF committed to a public repository, which meant the hero image
# shipped a real home directory's contents, real research-project names, and
# the quota rows of *other people's* groups on a shared filesystem. None of that
# is the reader's business and none of it is needed to show what the tool does.
#
# So the demo now builds its own tree and its own quota snapshot. Everything
# still runs for real -- the walker really walks this tree, `render_quota`
# really renders these rows -- but nothing on screen belongs to anyone.
# ---------------------------------------------------------------------------

# (relative dir, file count, bytes each). Shaped like a real ML project so the
# rankings are worth looking at: a few enormous checkpoints, a dataset of many
# small files that dominates the *inode* ranking without troubling the byte
# ranking, and a hidden cache that `ls` would not show.
_DEMO_LAYOUT = (
    ("checkpoints", 6, 5 << 20),
    ("model-weights", 3, 7 << 20),
    ("datasets/train", 900, 6 << 10),
    ("datasets/val", 220, 6 << 10),
    ("results", 40, 96 << 10),
    ("logs", 350, 3 << 10),
    ("envs/py311/lib", 1400, 5 << 10),
    (".cache/pip", 800, 4 << 10),
    ("notebooks", 12, 256 << 10),
)


def _chrome_cols():
    """Columns the frame's border and padding take from each line."""
    sys.path.insert(0, SRC)
    from rapidu.ui import BOX_CHROME

    return BOX_CHROME


def frame_scene(text):
    """Put the report's frame on, after redaction has finished moving text around.

    **The order matters and getting it wrong is invisible until you look.**
    ``anonymize`` rewrites paths, and its replacements are not the same length as
    what they replace -- a username becomes ``researcher`` (four columns wider) and
    ``$TMPDIR`` becomes ``/scratch/$USER`` (sixteen narrower). Framing before that
    means every border was measured against text that no longer exists: lines end
    up longer than the frame and spill past it, or shorter and pull the right
    border inward. Both were visible in the GIF, as rows of output hanging outside
    a box that was supposed to close.

    So the CLI is run with ``--no-box``, the paths are rewritten, and the frame is
    measured last -- against the text that will actually be drawn.
    """
    from rapidu.ui import BOX_CHROME, Style, box

    style = Style(True, True, COLS - BOX_CHROME, 256)
    framed = box(text.rstrip("\n").split("\n"), style, width=COLS)
    # A frame is either square or it is a bug. Cheap to assert, and it is exactly
    # the failure that shipped twice.
    from rapidu.ui import visible_width

    widths = {visible_width(line) for line in framed}
    if len(widths) != 1:
        raise SystemExit("frame is ragged: widths {}".format(sorted(widths)))
    return "\n".join(framed) + "\n"


def anonymize(text, tree, scratch):
    """Replace machine-specific paths in captured output with generic ones.

    The tree is synthetic and the quota rows are invented, but the *paths* are
    still wherever this happened to run -- which on a cluster means
    ``/scratch/<site>/<username>/...``. Every figure on screen stays exactly as
    the tool produced it; only the directory the reader has no business knowing
    is relabelled. Applied to the finished capture rather than by passing a fake
    path to the CLI, so the walker really did walk a real tree.
    """
    user = os.environ.get("USER") or ""
    for real, shown in (
        (tree, "/home/researcher/project"),
        (os.path.realpath(tree), "/home/researcher/project"),
        (scratch, "/scratch/$USER"),
        (os.path.realpath(scratch), "/scratch/$USER"),
    ):
        if real and real not in ("/", ""):
            text = text.replace(real, shown)
    if user:
        text = text.replace(user, "researcher")
    return text


def build_demo_tree(root):
    """Write a synthetic project tree and return its path."""
    payload = os.urandom(1 << 20)
    for rel, count, size in _DEMO_LAYOUT:
        d = os.path.join(root, rel)
        os.makedirs(d, exist_ok=True)
        blob = payload[:size] if size <= len(payload) else payload * (size // len(payload) + 1)
        for i in range(count):
            with open(os.path.join(d, "%s-%04d.bin" % (os.path.basename(rel), i)), "wb") as fh:
                fh.write(blob[:size])
    return root


def wait_until_settled(tree, timeout=180.0, quiet_for=2):
    """Do not render a tree the filesystem has not finished allocating.

    The first attempt at this demo walked the tree the instant it was written
    and captured ``0.43x`` with the ranking led by whichever directory happened
    to have been flushed -- on GPFS ``st_blocks`` is not final for tens of
    seconds. That is precisely the effect this package exists to warn about, so
    shipping a hero image of it would have been a poor advertisement.

    Waits until the walked total is unchanged over ``quiet_for`` consecutive
    reads, which is the same "two readings that agree" rule the CI uses against
    ``du``.
    """
    sys.path.insert(0, SRC)
    from rapidu.walk import walk

    os.sync()
    stable, previous, deadline = 0, None, time.time() + timeout
    while time.time() < deadline:
        size = walk(tree, threads=4).size
        stable = stable + 1 if size == previous else 0
        previous = size
        if stable >= quiet_for:
            return size
        time.sleep(10)
    return previous


def demo_quota_scene():
    """Render a real quota table from a synthetic snapshot.

    Calls the shipping renderer, so the layout, the colours, the bar and the
    staleness wording are all genuinely the tool's own output -- only the rows
    are invented.
    """
    sys.path.insert(0, SRC)
    from rapidu.quota import QuotaRow, QuotaSnapshot
    from rapidu.report import render_quota
    from rapidu.ui import Style

    snap = QuotaSnapshot("quota -s")
    snap.available = True
    snap.taken_at = snap.read_at - 691  # 11m 31s: stale enough to be the point
    snap.rows = [
        QuotaRow("home", "blocks", "user", 731 << 20, 30 << 30, 35 << 30, "", "/home"),
        QuotaRow("home", "files", "user", 21_842, 300_000, 1_000_000, "", "/home"),
        QuotaRow("scratch", "blocks", "user", 1932 << 30, 2 << 40, 4 << 40, "", "/scratch"),
        QuotaRow("labgroup", "blocks", "group", 79288 << 30, 202 << 40, 203 << 40, "", "/project"),
        QuotaRow(
            "labgroup", "files", "group", 43_583_258, 230_900_000, 231_900_000, "", "/project"
        ),
    ]
    # Unframed, like every other scene: `frame_scene` puts the border on after
    # redaction. See the note there.
    return "\n".join(render_quota(snap, style=Style(True, True, COLS - _chrome_cols(), 256))) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--tree",
        default=None,
        help="tree to walk in scenes 1-2 (default: a synthetic project tree in a "
        "temporary directory -- never pass a real path you would not publish)",
    )
    ap.add_argument("--scratch", default=None, help="writable dir for the deleted-fd scene")
    ap.add_argument("--settle", default=None, help="captured `-a --settle-wait` transcript")
    args = ap.parse_args()

    scratch = args.scratch or os.environ.get("TMPDIR") or "/tmp"

    tmp = None
    tree = args.tree
    if tree is None:
        tmp = tempfile.mkdtemp(prefix="rapidu-demo-", dir=scratch)
        tree = build_demo_tree(os.path.join(tmp, "project"))
        sys.stderr.write("waiting for the demo tree to settle...\n")
        wait_until_settled(tree)

    try:
        scenes = [
            ("rdu ~/project", run([tree, "-n", "8"])),
            ("rdu ~/project -i", run([tree, "-i", "-n", "6"])),
            ("rdu -Q", demo_quota_scene()),
            ("rdu -D", deleted_fd_scene(scratch)),
        ]
        scenes = [(label, frame_scene(anonymize(out, tree, scratch))) for label, out in scenes]
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    if args.settle:
        with open(args.settle) as fh:
            transcript = fh.read()
        # capture_settle.sh writes the exact command as a leading `# ` line, so
        # the label in the GIF cannot drift from the flags that produced it.
        label = "rdu $SCRATCH/run-217 -a --settle-wait 60"
        if transcript.startswith("# "):
            head, _, transcript = transcript.partition("\n")
            label = head[2:].strip()
        # The capture was taken on a real filesystem, so its paths go through
        # the same redaction as the live scenes -- label included, since the
        # label is the command line and the command line names the tree.
        sys.path.insert(0, SRC)
        scenes.append(
            (
                anonymize(label, tree, scratch),
                frame_scene(anonymize(transcript, tree, scratch)),
            )
        )

    # One canvas has to hold every scene, so its height is the tallest one plus
    # the prompt line and a single blank after the output. It used to carry four
    # rows of slack, which the tallest scene needed and the other four did not --
    # so most of the running time was spent showing an empty band at the bottom.
    # Exactly what `play` draws: the prompt line, every output row, and one blank
    # after it. This used to be measured on `to_cells(out)` while `play` drew
    # `to_cells(out.rstrip("\n"))`, so the budget and the content were counted from
    # two different strings -- and the tallest scene lost its last rows off the
    # bottom of the canvas, which on a framed report means losing the border.
    rows = max(1 + len(to_cells(out.rstrip("\n"))) + 1 for _, out in scenes)
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
