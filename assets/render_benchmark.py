#!/usr/bin/env python3
"""Render the README benchmark chart (``assets/benchmark-{light,dark}.svg``).

Hand-written SVG, standard library only, for the same reason the package has no
dependencies: this repo should build its own artefacts on a login node with
nothing installed.

    python assets/render_benchmark.py

Two files are emitted because GitHub honours ``prefers-color-scheme`` through a
``<picture>`` element, and a dark-mode reader should not be handed a white
rectangle. The dark variant is *stepped for the dark surface* rather than being
the light one inverted.

Design notes, so the next person does not have to re-derive them:

* **Horizontal bars.** The measure is duration and the categories carry prose
  labels; horizontal keeps the labels readable without rotation.
* **One axis.** Seconds. The file counts live in the row label, not on a second
  scale.
* **Colours are two categorical slots**, validated against both surfaces
  (adjacent CVD ΔE 24.7 light / 26.8 dark against an ≥8 target, normal-vision
  33.6 / 31.8 against a ≥15 floor, both ≥3:1 on their surface).
* **Every bar is directly labelled** with its own value, so the chart is
  readable without measuring against the axis, and identity never rests on
  colour alone.
"""

import os

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)))

# (label, files, du seconds, rapidu seconds)
DATA = [
    ("a package cache", 792_225, 168.08, 25.40),
    ("a whole project directory", 1_686_589, 298.46, 57.43),
]

THEMES = {
    "light": {
        "surface": "#fcfcfb",
        "text": "#0b0b0b",
        "muted": "#52514e",
        "grid": "#e3e2df",
        "du": "#2a78d6",
        "rapidu": "#eb6834",
    },
    "dark": {
        "surface": "#1a1a19",
        "text": "#ffffff",
        "muted": "#c3c2b7",
        "grid": "#333330",
        "du": "#3987e5",
        "rapidu": "#d95926",
    },
}

W, H = 720, 232
LEFT, RIGHT, TOP = 188, 134, 56
BAR, GAP, GROUP = 26, 2, 34  # bar height, gap within a pair, gap between pairs
RADIUS = 4  # rounded data-end, per the mark spec
AXIS_MAX = 300.0


def _bar(x, y, w, h, fill):
    """A bar with only its data-end rounded: square at the baseline."""
    w = max(w, RADIUS + 0.5)
    r = min(RADIUS, w)
    return (
        '<path d="M{x},{y} H{a} a{r},{r} 0 0 1 {r},{r} V{b} a{r},{r} 0 0 1 -{r},{r} '
        'H{x} Z" fill="{f}"/>'
    ).format(x=x, y=y, a=x + w - r, r=r, b=y + h - r, f=fill)


def render(mode):
    t = THEMES[mode]
    plot_w = W - LEFT - RIGHT
    font = "ui-sans-serif,-apple-system,Segoe UI,Helvetica,Arial,sans-serif"
    out = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="{}" height="{}" '
        'viewBox="0 0 {} {}" font-family="{}">'.format(W, H, W, H, font),
        '<rect width="{}" height="{}" fill="{}"/>'.format(W, H, t["surface"]),
        # Title carries the finding; the chart shows it.
        '<text x="{}" y="26" font-size="15" font-weight="600" fill="{}">'
        "Cold walk of a GPFS tree, wall time</text>".format(LEFT - 148, t["text"]),
        '<text x="{}" y="44" font-size="12" fill="{}">'
        "lower is better · same byte total either way</text>".format(LEFT - 148, t["muted"]),
    ]

    # Legend. Present because there are two series; direct labels back it up.
    lx = LEFT + plot_w - 150
    for i, (name, key) in enumerate((("du", "du"), ("rapiDU", "rapidu"))):
        x = lx + i * 78
        out.append(
            '<rect x="{}" y="18" width="10" height="10" rx="2" fill="{}"/>'.format(x, t[key])
        )
        out.append(
            '<text x="{}" y="27" font-size="12" fill="{}">{}</text>'.format(
                x + 15, t["muted"], name
            )
        )

    y = TOP
    for label, files, du_s, rd_s in DATA:
        out.append(
            '<text x="{}" y="{}" font-size="13" fill="{}">{}</text>'.format(
                8, y + 16, t["text"], label
            )
        )
        out.append(
            '<text x="{}" y="{}" font-size="11" fill="{}">{:,} files</text>'.format(
                8, y + 33, t["muted"], files
            )
        )
        for value, key in ((du_s, "du"), (rd_s, "rapidu")):
            w = plot_w * (value / AXIS_MAX)
            out.append(_bar(LEFT, y, w, BAR, t[key]))
            out.append(
                '<text x="{:.1f}" y="{}" font-size="12" font-weight="600" fill="{}">'
                "{:.1f}s</text>".format(LEFT + w + 8, y + 18, t["text"], value)
            )
            y += BAR + GAP
        # The ratio is the headline, so it is stated, not left to be measured.
        out.append(
            '<text x="{}" y="{:.0f}" font-size="17" font-weight="700" fill="{}" '
            'text-anchor="end">{:.1f}x</text>'.format(
                W - 12, y - BAR - GAP / 2 + 1, t["rapidu"], du_s / rd_s
            )
        )
        y += GROUP - GAP

    out.append("</svg>")
    return "\n".join(out)


if __name__ == "__main__":
    for mode in THEMES:
        path = os.path.join(OUT_DIR, "benchmark-{}.svg".format(mode))
        with open(path, "w") as fh:
            fh.write(render(mode) + "\n")
        print("wrote {} ({:,} bytes)".format(path, os.path.getsize(path)))
