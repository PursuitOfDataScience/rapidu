"""The RECONCILE verdict headline, which was a fixed three-part line.

Polish pass, 2026-09-01. ``report.render_reconcile`` builds its verdict
headline as ``"  {}  {}  {}".format(label, headline, figures)`` with no width
test anywhere in it -- and it is the *first* line of the section, the one a
reader's eye lands on. Rendered it came out 84 to 93 columns against a layout
of 80, at a terminal of 100 and of 80 alike: the line was appended whole, so a
wider terminal never helped and a narrower one only moved the damage.

For a ``blocks`` comparison it overflowed on every realistic pair of figures,
not just large ones. ``4.0 KiB accounted for vs quota 8.0 KiB, difference
-4.0 KiB`` -- the smallest comparison the tool can report -- renders the line at
85 columns. Only a degenerate all-zero triple fitted. The ``files`` kind is the
one that does fit at four- and five-digit counts, and that is what the controls
below pin.

The fix moves the figures rather than wrapping them, and the distinction is the
whole point. ``textwrap`` breaks on whitespace and every figure here *contains*
whitespace, so soft-wrapping this line would break between ``823.4`` and
``GiB``. That is exactly what the terminal's hard wrap was already doing: an
87-column line on an 80-column terminal left ``difference -82`` at the end of
one row and ``3.4 GiB`` alone at column zero of the next, level with the
section's own margin, reading as an unrelated statement. So the figures stay
whole and drop to the six-column prose margin the candidates, the notes, the
blockers and the unlinked-but-open figure already share -- and the verdict word
itself never moves, because it is the one thing the line exists to say.
"""

from rapidu import reconcile as rc
from rapidu import report, ui
from rapidu.fmt import human_bytes, human_count

MOUNT = "/mnt/fake"

# The margin the figures drop to, and the margin every other continuation in
# this section already hangs at.
MARGIN = "      "

GiB = 1 << 30
TiB = 1 << 40

# The two templates the section composes its figures from, kept here so the
# expectations below are built rather than pasted.
GAP_FIGURES = "{} accounted for vs quota {}, difference {}"
CLOSES_FIGURES = "{} vs quota {}, difference {} (within {})"


def _rec(kind, verdict, accounted, quota, gap, tolerance=0):
    rec = rc.Reconciliation(kind)
    rec.verdict = verdict
    rec.walk_value = accounted
    rec.quota_value = quota
    rec.gap = gap
    rec.tolerance = tolerance
    rec.row = rc.QuotaRow("fs", kind, "group", quota, None, quota, "", MOUNT)
    return rec


def _figures(rec):
    """The figures column as the section composes it, for this reconciliation."""
    show = human_count if rec.kind == "files" else human_bytes
    if rec.verdict == rc.CLOSES:
        return CLOSES_FIGURES.format(
            show(rec.accounted), show(rec.quota_value), show(rec.gap), show(rec.tolerance)
        )
    return GAP_FIGURES.format(show(rec.accounted), show(rec.quota_value), show(rec.gap))


def _word(rec):
    if rec.verdict == rc.CLOSES:
        return "reconciles"
    if rec.verdict == rc.INCONCLUSIVE:
        return "INCONCLUSIVE"
    return "UNEXPLAINED GAP"


def _label(rec):
    return "bytes" if rec.kind == "blocks" else "files"


def _rendered(rec, width, color=True):
    """The verdict's own lines: the heading and the leading blank dropped.

    Nothing else is populated on these reconciliations, so every remaining line
    belongs to the headline.
    """
    style = ui.Style(color=color, unicode_ok=True, width=width)
    lines = report.render_reconcile([rec], style)
    assert lines[1] == ui.heading("RECONCILE", style), lines
    return lines[2:]


def _unpainted(line):
    return ui._ANSI_RE.sub("", line)


def _layout(width):
    """What everything elastic in this report is laid out to."""
    return min(width, report._LAYOUT_COLUMNS)


def _one_line(rec):
    """The line the section used to build unconditionally."""
    return "  {}  {}  {}".format(_label(rec), _word(rec), _figures(rec))


# A byte comparison against a real cluster quota: 87 columns before the fix.
WIDE_GAP = _rec("blocks", rc.GAP, 1234 * GiB, 2 * TiB, -(823 * GiB + (400 << 20)))
# The same headline in its yellow form, 84 columns before the fix.
WIDE_INCONCLUSIVE = _rec("blocks", rc.INCONCLUSIVE, 1234 * GiB, 2 * TiB, -(823 * GiB))
# The agreeing verdict carries a fourth figure and was 89 columns before the fix.
WIDE_CLOSES = _rec("blocks", rc.CLOSES, 1234 * GiB, 1235 * GiB, -GiB, 40 * GiB)
# The overflow is not confined to bytes: an eight-digit inode count is 93.
WIDE_FILES = _rec("files", rc.GAP, 12345678, 20000000, -7654322)

WIDE = (WIDE_GAP, WIDE_INCONCLUSIVE, WIDE_CLOSES, WIDE_FILES)

# The common case on an inode quota, and the one that must not change: four-digit
# counts leave the whole line at 69 to 75 columns.
SHORT_GAP = _rec("files", rc.GAP, 1234, 1240, -6)
SHORT_INCONCLUSIVE = _rec("files", rc.INCONCLUSIVE, 1234, 1240, -6)
SHORT_CLOSES = _rec("files", rc.CLOSES, 1234, 1240, -6, 100)

SHORT = (SHORT_GAP, SHORT_INCONCLUSIVE, SHORT_CLOSES)


# --- teeth -------------------------------------------------------------------


def test_the_verdict_headline_is_not_wider_than_the_layout():
    """Teeth. 84-93 columns at both widths before the fix; the layout is 80.

    Measured in columns and not characters: these lines carry SGR escapes, and
    ``len`` scores the painted 87-column line at 104.
    """
    for rec in WIDE:
        # Guard against a shortened template making this vacuous.
        assert ui.visible_width(_one_line(rec)) > report._LAYOUT_COLUMNS, _one_line(rec)

        for width in (100, 80):
            lines = _rendered(rec, width)
            widest = max(ui.visible_width(line) for line in lines)
            assert widest <= _layout(width), (width, widest, lines)


def test_the_figures_drop_whole_to_the_prose_margin():
    """Teeth. Before the fix there was one line, so there was no second one.

    Two things are asserted together because either alone could be satisfied by
    the wrong fix: the figures are on their own line *and* that line is the
    figures entire and unbroken. A ``_wrapped`` call would pass a width
    assertion and fail this, having split ``-823.4 GiB`` into ``-823.4`` and
    ``GiB``.
    """
    for rec in WIDE:
        for width in (100, 80):
            lines = _rendered(rec, width)

            assert len(lines) == 2, lines
            assert _unpainted(lines[1]) == MARGIN + _figures(rec), lines
            # Not a deeper indent invented for this one line.
            assert not _unpainted(lines[1]).startswith(MARGIN + " "), lines


# --- controls ----------------------------------------------------------------


def test_a_short_verdict_still_renders_on_one_unchanged_line():
    """CONTROL. Passes before and after, and is the one that matters.

    A fix that moved the figures unconditionally would satisfy both teeth above
    while making every verdict that already fitted a line longer -- and this
    section's whole design is that a comparison which agrees says so on one line
    and gets out of the way. So this pins the exact bytes, painting included:
    the label, two spaces, the verdict word in its own tone, two spaces, the
    figures dimmed.
    """
    for rec in SHORT:
        for width in (100, 80):
            style = ui.Style(color=True, unicode_ok=True, width=width)
            tone = {rc.CLOSES: "green", rc.INCONCLUSIVE: "yellow"}.get(rec.verdict, "red")
            expected = "  {}  {}  {}".format(
                _label(rec), style.paint(_word(rec), tone), style.paint(_figures(rec), "dim")
            )

            lines = _rendered(rec, width)
            assert len(lines) == 1, lines
            assert lines[0] == expected, (lines[0], expected)
            assert _unpainted(lines[0]) == _one_line(rec)
            assert ui.visible_width(lines[0]) <= _layout(width)


def test_the_verdict_word_is_never_the_part_that_moves():
    """CONTROL. Passes before and after: one line or two, the first line says it.

    The label and the verdict word are why the section exists, and a reader
    scanning for a red ``UNEXPLAINED GAP`` has to find it on the line the
    section starts on. Displacing the headline to make room for the numbers
    would have been the other way to fit the budget, and the wrong one.
    """
    for rec in WIDE + SHORT:
        for width in (100, 80):
            first = _unpainted(_rendered(rec, width)[0])
            assert first.startswith("  {}  {}".format(_label(rec), _word(rec))), first


def test_no_figure_is_ever_broken_across_lines():
    """CONTROL. Passes before and after: the numbers survive the layout.

    Before the fix there was one line and nothing could be split; after it the
    split is at the figures boundary. Re-joining the rendered lines has to give
    back exactly the three parts the section composed, which is what separates
    moving the column from wrapping or clipping it.
    """
    for rec in WIDE + SHORT:
        for width in (100, 80):
            joined = "  ".join(_unpainted(line).strip() for line in _rendered(rec, width))
            assert joined == _one_line(rec).strip(), (width, joined)


def test_every_rendered_line_closes_the_colour_runs_it_opens():
    """CONTROL. Passes before and after: one line or two, each is self-contained.

    An SGR run spanning a newline is reset by some terminals and inherited by
    others, and a paste into a ticket keeps whichever happened. So the dimmed
    figures on the continuation have to open and close their own run rather than
    leaning on one the line above left open -- which is what splitting a
    previously single painted line would do if the parts were painted as a
    whole and then divided.
    """
    for rec in WIDE + SHORT:
        for line in _rendered(rec, 80):
            escapes = ui._ANSI_RE.findall(line)
            assert escapes, repr(line)
            assert escapes[-1] == ui._RESET, repr(line)
            assert 2 * escapes.count(ui._RESET) == len(escapes), repr(line)


def test_the_subtree_headline_has_only_two_parts_and_is_untouched():
    """CONTROL. Passes before and after: the sibling verdict with no figures column.

    ``SUBTREE`` is the most common verdict on a real cluster and renders a
    two-part line, its percentage already folded into the headline sentence.
    There is no third part to move, and this asserts the fix did not reach a
    branch it has no business in.
    """
    rec = _rec("blocks", rc.SUBTREE, 100 * GiB, 2 * TiB, -(1948 * GiB))
    style = ui.Style(color=False, unicode_ok=True, width=80)
    lines = report.render_reconcile([rec], style)[2:]

    assert lines == ["  bytes  {}".format(rc.verdict_line(rec))], lines
    assert ui.visible_width(lines[0]) <= _layout(80)
