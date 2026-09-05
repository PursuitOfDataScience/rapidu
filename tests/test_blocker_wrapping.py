"""The one line in ``RECONCILE`` that was appended instead of wrapped.

Polish pass, 2026-09-01. ``report.render_reconcile``'s
``INCONCLUSIVE`` / ``UNEXPLAINED GAP`` branch rendered each blocker with a bare
``out.append``, while the candidate and note loops immediately below it -- and
the blocker loop in the ``CLOSES`` branch above it -- all go through
``_wrapped``. So the one class of line in this section that is unbounded prose
straight out of ``reconcile`` was the one class never soft-wrapped, and it set
the width of the whole report on its own: 187 rendered columns against a layout
of 80, on a terminal of 80 or of 100 alike.

It was already overflowing before this session -- the older "taken 1800s ago"
sentence rendered at 123 columns -- and the gate fix that made that sentence
name the walk's own duration as well as the read staleness pushed it to 187.

The tests below are deliberately split. The teeth measure the *long* blocker,
in columns rather than characters, because these lines carry SGR escapes and
``len`` would score the painted 187-column line at 195. The controls measure
the *short* blocker, which must come out on exactly one line with exactly the
text it had before: wrapping unconditionally, or re-indenting a line that
needed no wrapping, would satisfy any width assertion while making every short
blocker worse.
"""

import os
import time

from rapidu import reconcile as rc
from rapidu import report, ui
from rapidu.deleted import DeletedScan
from rapidu.quota import QuotaRow, QuotaSnapshot
from rapidu.walk import SettleCheck, WalkResult

MOUNT = "/mnt/fake"

# The prefix and margin the branch under test writes. The margin is the section's
# prose margin -- the same six columns the candidates, the notes and the
# unlinked-but-open line already use -- and the prefix stays *inside* the wrapped
# text rather than becoming the indent, so a continuation lands under "cannot"
# and not off the end of the prefix.
PREFIX = "cannot call this a finding: "
INDENT = "      "

QUOTA_USED = 50_000_000


def _walk(elapsed=0.0, partial=False):
    res = WalkResult(MOUNT)
    res.size = 1000
    res.files = 9
    res.dirs = 1
    res.elapsed = elapsed
    res.partial = partial
    res.by_uid = {os.getuid(): (1000, 10)}
    res.by_dev = {42: (1000, 10)}
    return res


def _snap(age=0.0):
    snap = QuotaSnapshot("test")
    snap.available = True
    snap.read_at = time.time()
    snap.taken_at = snap.read_at - age
    snap.rows = [
        QuotaRow("fs", "blocks", "user", QUOTA_USED, QUOTA_USED * 10, QUOTA_USED * 11, "", MOUNT)
    ]
    return snap


def _settle():
    check = SettleCheck()
    check.ran = True
    check.gap = 60.0
    return check


def _real_blocker(marker, age=0.0, elapsed=0.0, partial=False):
    """A blocker sentence as ``reconcile`` actually writes it.

    Taken from the real function rather than pasted, so that shortening one of
    these sentences later cannot leave a test asserting about text the package
    no longer emits.
    """
    rec = rc.reconcile(
        _walk(elapsed=elapsed, partial=partial),
        _settle(),
        _snap(age=age),
        DeletedScan(),
        "blocks",
        rc.DEFAULT_MAX_SNAPSHOT_AGE_S,
    )
    (found,) = [b for b in rec.blockers if marker in b]
    return found


# The longest sentence this package writes, and the one that motivated the fix.
LONG = _real_blocker("snapshot", age=400.0, elapsed=1800.0)
# A real blocker short enough to need no wrapping at all: the control.
SHORT = _real_blocker("interrupted", partial=True)


def _rendered(blocker, width):
    """The blocker's own rendered lines, painted, from the gap branch.

    ``candidates`` and ``notes`` are emptied so the slice below is unambiguous:
    everything after the headline belongs to the blocker.
    """
    rec = rc.Reconciliation("blocks")
    rec.verdict = rc.INCONCLUSIVE
    rec.walk_value = 1000
    rec.quota_value = QUOTA_USED
    rec.gap = QUOTA_USED - 1000
    rec.tolerance = rc.MIN_TOLERANCE_BYTES
    rec.blockers = [blocker]
    lines = report.render_reconcile([rec], ui.Style(color=True, unicode_ok=True, width=width))
    assert "INCONCLUSIVE" in lines[2], lines
    body = _below_the_headline(lines)
    assert body, lines
    return body


def _unpainted(line):
    return ui._ANSI_RE.sub("", line)


def _below_the_headline(lines):
    """Everything under the verdict's own headline, however many lines that is.

    The headline is one line when the label, the verdict word and the figures
    all fit, and two when they do not -- for a byte comparison they almost never
    do, so the figures drop to this same six-column margin. Either way they
    belong to the headline and not to the blocker under test, so this skips them
    rather than letting the slice offset decide.
    """
    body = lines[3:]
    if body and " vs quota " in _unpainted(body[0]):
        body = body[1:]
    return body


def _layout(width):
    """What every other prose line in this section is wrapped to."""
    return min(width, report._LAYOUT_COLUMNS)


# --- teeth -------------------------------------------------------------------


def test_the_long_blocker_is_not_wider_than_the_layout():
    """Teeth. 187 columns at both widths before the fix; the layout is 80.

    Both terminal widths are checked because the overflow was insensitive to the
    terminal: the line was appended whole, so a wider terminal did not help and a
    narrower one made it worse without changing the number.
    """
    # Guard against the sentence being shortened until this is vacuous.
    assert ui.visible_width(PREFIX + LONG) > report._LAYOUT_COLUMNS, LONG

    for width in (100, 80):
        body = _rendered(LONG, width)
        widest = max(ui.visible_width(line) for line in body)
        assert widest <= _layout(width), (width, widest, body)


def test_the_long_blocker_wraps_at_the_prose_margin_like_its_neighbours():
    """Teeth. Before the fix there was one line, so there was no continuation.

    The continuation has to carry the section's six-column margin and must *not*
    repeat the prefix -- the failure mode of passing the prefix as ``_wrapped``'s
    indent instead of as part of its text.
    """
    body = _rendered(LONG, 100)

    assert len(body) > 1, body
    plain = [_unpainted(line) for line in body]
    assert plain[0].startswith(INDENT + PREFIX), plain
    for line in plain[1:]:
        assert line.startswith(INDENT), plain
        assert not line.startswith(INDENT + " "), plain
        assert PREFIX not in line, plain


# --- controls ----------------------------------------------------------------


def test_a_short_blocker_still_renders_on_one_unchanged_line():
    """CONTROL. Passes before and after, and is the one that matters.

    A fix that wrapped unconditionally, or that hung the continuations off a
    longer indent, would satisfy the width assertion above while making every
    blocker that already fitted worse. So this pins the exact bytes: one line,
    the section's margin, the prefix, the sentence.
    """
    for width in (100, 80):
        body = _rendered(SHORT, width)

        assert len(body) == 1, body
        assert _unpainted(body[0]) == INDENT + PREFIX + SHORT
        assert ui.visible_width(body[0]) <= _layout(width)


def test_wrapping_neither_loses_nor_repeats_any_of_the_sentence():
    """CONTROL. Passes before and after: one line or four, the words are the same.

    A soft wrap is presentation. Re-joining the rendered lines has to give back
    exactly the sentence ``reconcile`` wrote, which is what distinguishes this
    from clipping the line to the layout.
    """
    for blocker in (LONG, SHORT):
        for width in (100, 80):
            joined = " ".join(_unpainted(line).strip() for line in _rendered(blocker, width))
            assert joined == PREFIX + blocker, (width, joined)


def test_every_rendered_line_carries_its_own_colour_run():
    """CONTROL. Passes before and after.

    ``_wrapped`` paints per line rather than painting the paragraph, because an
    SGR run spanning a newline is reset by some terminals and inherited by
    others. Splitting a previously single painted line is only safe if each
    piece is self-contained.
    """
    for line in _rendered(LONG, 80):
        assert line.startswith("\033["), repr(line)
        assert line.endswith(ui._RESET), repr(line)


def test_the_reconciling_branch_blocker_was_already_wrapped():
    """CONTROL. Passes before and after: the sibling loop this fix copied.

    ``CLOSES`` renders the same blocker list as "caveat: ..." through
    ``_wrapped`` at the same margin. It was already correct, and this asserts the
    fix did not disturb the shape it was matched to.
    """
    rec = rc.Reconciliation("blocks")
    rec.verdict = rc.CLOSES
    rec.walk_value = QUOTA_USED
    rec.quota_value = QUOTA_USED
    rec.gap = 0
    rec.tolerance = rc.MIN_TOLERANCE_BYTES
    rec.blockers = [LONG]
    lines = report.render_reconcile([rec], ui.Style(color=True, unicode_ok=True, width=80))
    body = _below_the_headline(lines)

    assert len(body) > 1, body
    assert max(ui.visible_width(line) for line in body) <= _layout(80)
    assert _unpainted(body[0]).startswith(INDENT + "caveat: "), body
